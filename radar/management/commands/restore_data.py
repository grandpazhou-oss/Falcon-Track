"""Validated archive restore with database locking and transactional writes."""

import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone
from filelock import FileLock, Timeout

from radar.management.commands.backup_data import (
    BUSINESS_MODELS, MAX_ARCHIVE_MEMBERS, MAX_JSON_BYTES, MAX_UNCOMPRESSED_BYTES,
    PORTABLE_MODEL_MAP, safe_raw_path,
)
from radar.business_archive import opened_backup
from radar.models import Document, RuntimeState, Source, SourceRun, AIConfig, AnalysisRun, KeywordCandidate


def validate_archive(archive):
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS or sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES:
        raise CommandError("备份展开超过本地试用的文件数量或 1GB 限制。")
    names = {}
    casefold_names = set()
    for member in members:
        name = member.filename
        if name != member.orig_filename or name.casefold() in casefold_names:
            raise CommandError("备份包含重复、大小写冲突或不安全文件名。")
        casefold_names.add(name.casefold())
        if member.flag_bits & 1:
            raise CommandError("不支持加密的备份归档。")
        kind = stat.S_IFMT(member.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise CommandError("备份包含符号链接或非普通文件。")
        if name in {"radar.json", "manifest.json"}:
            if member.is_dir():
                raise CommandError("备份元数据必须是文件。")
        else:
            safe_raw_path(name, directory=member.is_dir())
        names[name] = member
    if not {"radar.json", "manifest.json"}.issubset(names):
        raise CommandError("备份缺少 manifest.json 或 radar.json。")
    if names["manifest.json"].file_size > 1024 * 1024 or names["radar.json"].file_size > MAX_JSON_BYTES:
        raise CommandError("备份元数据大小超过限制。")
    manifest = json.loads(archive.read("manifest.json"))
    if not isinstance(manifest, dict) or type(manifest.get("format")) is not int or manifest["format"] not in {1,2}:
        raise CommandError("不支持的备份格式。")
    payload = archive.read("radar.json")
    if manifest.get("radar_sha256") and manifest["radar_sha256"] != hashlib.sha256(payload).hexdigest():
        raise CommandError("备份数据校验失败。")
    for name, digest in manifest.get("raw_sha256", {}).items():
        if name not in names or hashlib.sha256(archive.read(name)).hexdigest()!=digest:
            raise CommandError("原始证据哈希校验失败。")
    records = json.loads(payload)
    if not isinstance(records, list) or type(manifest.get("objects")) is not int or manifest["objects"] != len(records):
        raise CommandError("备份对象清单或数量无效。")
    if manifest["format"] == 1:
        # Explicit v0.1 additions only. Unknown/missing legacy fields still fail validation.
        additions = {
            "radar.source": {"options": {}, "validation": {}},
            "radar.company": {"profile": {}, "discovery_evidence": [], "subscribed": True},
            "radar.lead": {"subject_reason": "", "deadline_semantics": "unknown", "extraction_version": 0, "is_test": False},
        }
        legacy = {"radar.source","radar.company","radar.lead","radar.document","radar.review","radar.sourcerun"}
        for record in records:
            if not isinstance(record,dict) or record.get("model") not in legacy or not isinstance(record.get("fields"),dict):
                raise CommandError("无效 v0.1 数据记录。")
            for name,value in additions.get(record["model"],{}).items():
                if name in record["fields"]: raise CommandError("v0.1 备份出现新版本字段。")
                record["fields"][name] = value
    validate_records(records,names,set(manifest.get("omitted_probe_paths",[])))
    return records, names


def validate_records(records, names, omitted_probes=()):
    identifiers = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"model", "pk", "fields"}:
            raise CommandError("备份记录结构无效。")
        model = PORTABLE_MODEL_MAP.get(record["model"]) if isinstance(record["model"], str) else None
        if model is None:
            raise CommandError("备份包含未允许的数据表。")
        if type(record["pk"]) is not int or record["pk"] < 1:
            raise CommandError("备份主键必须是正整数。")
        identity = (record["model"], record["pk"])
        if identity in identifiers:
            raise CommandError("备份包含重复记录主键。")
        identifiers.add(identity)
        fields = record["fields"]
        expected = {field.name for field in model._meta.local_fields if field.serialize and not field.primary_key}
        if isinstance(fields, dict):
            # These two fields were added after the earlier portable backup format.
            for name, value in {"radar.searchcall": {"tavily_credits": None},
                                "radar.keywordcandidate": {"error_detail": {}}}.get(record["model"], {}).items():
                fields.setdefault(name, value.copy() if isinstance(value, dict) else value)
        if not isinstance(fields, dict) or set(fields) != expected:
            raise CommandError("备份字段与本版本不兼容，请使用相同版本恢复。")
        if model is KeywordCandidate and fields.get("raw_path"):
            path = str(safe_raw_path(fields["raw_path"]))
            if path not in names or names[path].is_dir():
                raise CommandError("发现候选引用的原始证据缺失。")
        sites = []
        if model is KeywordCandidate:
            result = fields.get('result') or {}
            sites = ([result.get('website_candidate', {}), result.get('discovery_feed', {}), result.get('research_recovery', {})]
                     + result.get('website_findings', []) + result.get('verification', {}).get('sources', []))
        elif model._meta.label_lower == 'radar.company':
            sites = (fields.get('profile') or {}).get('keyword_website_candidates', [])
        sites = sites + [page for site in sites for page in site.get('findings', [])]
        for site in sites:
            if site.get('raw_path') and str(safe_raw_path(site['raw_path'])) not in names:
                raise CommandError("官网发现引用的原始证据缺失。")
        if model is SourceRun:
            for entry in (fields.get("stats") or {}).get("requests", []):
                if isinstance(entry, dict) and entry.get("raw_path"):
                    path = str(safe_raw_path(entry["raw_path"]))
                    if path not in names and path not in omitted_probes:
                        raise CommandError("来源运行引用的原始证据缺失。")
        if model is Document:
            if not isinstance(fields["raw_path"], str):
                raise CommandError("原始证据路径必须是字符串。")
            if fields["raw_path"]:
                path = str(safe_raw_path(fields["raw_path"]))
                if path not in names or names[path].is_dir():
                    raise CommandError("原始证据路径没有对应的备份文件。")
            upload_path=(fields.get('metadata') or {}).get('upload_path')
            if upload_path:
                path=str(safe_raw_path(upload_path))
                if path not in names or names[path].is_dir():raise CommandError("用户材料原始文件没有对应的备份文件。")
    for record in records:
        model = PORTABLE_MODEL_MAP[record["model"]]
        for field in model._meta.local_fields:
            if not field.is_relation:
                continue
            value = record["fields"].get(field.name)
            if value is None and field.null:
                continue
            identity = (field.remote_field.model._meta.label_lower, value)
            if type(value) is not int or identity not in identifiers:
                raise CommandError("备份包含缺失或无效的关联记录。")


def lock_business_tables():
    """Protect the empty check from writes by both web and worker processes."""
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            tables = ", ".join(connection.ops.quote_name(model._meta.db_table) for model in BUSINESS_MODELS)
            cursor.execute(f"LOCK TABLE {tables} IN EXCLUSIVE MODE")
        elif connection.vendor == "sqlite":
            # Acquire a write transaction before any empty-table SELECTs.
            table = connection.ops.quote_name(RuntimeState._meta.db_table)
            cursor.execute(f'UPDATE {table} SET "key" = "key" WHERE 0')
        else:
            raise CommandError("本版本仅支持 SQLite 或 PostgreSQL 恢复。")


def reset_business_sequences():
    # Explicit-PK PostgreSQL inserts do not advance sequences; Django supplies
    # the backend's setval() statements. SQLite explicit inserts advance its PK.
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), BUSINESS_MODELS):
            cursor.execute(statement)


class Command(BaseCommand):
    help = "Restore into an empty business database; automatic collection remains disabled."

    def add_arguments(self, parser):
        parser.add_argument("file")

    def handle(self, *args, **options):
        if connection.in_atomic_block:
            raise CommandError("恢复需要独立数据库事务，请单独运行 restore_data 命令。")
        archive_path = Path(options["file"]).expanduser().resolve()
        data_dir = settings.DATA_DIR.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        raw_target = data_dir / "raw"
        promoted_raw = False
        committed = False
        had_empty_raw = False
        try:
            with FileLock(str(data_dir / "worker.lock"), timeout=0), opened_backup(archive_path,data_dir) as archive:
                records, members = validate_archive(archive)
                if raw_target.is_symlink() or (raw_target.exists() and (not raw_target.is_dir() or any(raw_target.iterdir()))):
                    raise CommandError("目标 raw/ 目录已有文件或不是普通目录，请使用新的数据目录恢复。")
                had_empty_raw = raw_target.is_dir()
                with tempfile.TemporaryDirectory(prefix=".radar-restore-", dir=data_dir) as staging_name:
                    staging = Path(staging_name)
                    for name, member in members.items():
                        if name in {"radar.json", "manifest.json"} or member.is_dir():
                            continue
                        destination = staging.joinpath(*safe_raw_path(name).parts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source_file, destination.open("xb") as target_file:
                            shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
                    with transaction.atomic():
                        lock_business_tables()
                        if any(model.objects.exists() for model in BUSINESS_MODELS):
                            raise CommandError("仅允许恢复到空业务库（包括来源及运行状态）。请 migrate 后直接 restore，不要先初始化示例或来源。")
                        for obj in serializers.deserialize("json", json.dumps(records)):
                            obj.save()
                        now = timezone.now()
                        SourceRun.objects.filter(status__in=["queued", "running"]).update(
                            status="cancelled", finished_at=now, error="从备份恢复，未自动重放旧任务。",
                        )
                        AnalysisRun.objects.filter(status__in=["queued", "running"]).update(status="cancelled",finished_at=now,error_code="restored_no_replay")
                        AIConfig.objects.all().update(enabled=False,auto_new=False,search_enabled=False,key_source="disabled",key_ref="",search_key_ref="",test_status="not_configured",tested_at=None)
                        Source.objects.all().update(enabled=False)
                        Source.objects.filter(status__in=["queued", "running"]).update(status="cancelled")
                        RuntimeState.objects.create(key="scheduler_control", value={"enabled": False})
                        reset_business_sequences()
                        connection.check_constraints(table_names=[model._meta.db_table for model in BUSINESS_MODELS])
                        staged_raw = staging / "raw"
                        if staged_raw.exists():
                            if raw_target.exists():
                                raw_target.rmdir()  # Only the pre-validated empty directory.
                            staged_raw.rename(raw_target)
                            promoted_raw = True
                    committed = True
                    promoted_raw = False  # Database and referenced files committed.
        except Timeout as exc:
            raise CommandError("后台正在运行，请先停止后恢复。") from exc
        except CommandError:
            raise
        except Exception as exc:
            if committed:
                raise CommandError(f"业务数据已恢复，但临时文件清理失败：{type(exc).__name__}: {exc}") from exc
            raise CommandError(f"恢复失败，未提交业务数据：{type(exc).__name__}: {exc}") from exc
        finally:
            # Filesystem moves cannot join a database transaction. Compensate
            # only files created by this restore if database commit fails.
            if promoted_raw:
                shutil.rmtree(raw_target)
                if had_empty_raw:
                    raw_target.mkdir()
        from radar.seed_dictionary import export_dictionary
        try:
            export_dictionary()
        except (OSError, ValueError) as exc:
            self.stderr.write("业务恢复成功；词典重建失败，请运行 export_japanese_seeds：" + type(exc).__name__)
        self.stdout.write(self.style.SUCCESS("恢复完成；全部来源及全局自动采集已关闭，旧排队/运行任务已取消。请检查数据后再启用。"))

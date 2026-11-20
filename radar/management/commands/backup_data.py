"""Portable business data with one database snapshot and referenced raw files."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from filelock import FileLock, Timeout

from radar.models import Company, Document, Lead, ModelCall, Review, RuntimeState, Source, SourceRun, AIConfig, BusinessCapability, AnalysisRun, ExtractionRun, EntityCandidate, SearchCall, SourceManifest, ResearchEvent, KeywordCandidate, KeywordCursor


# Credentials and scheduler controls are excluded; call history is preserved to avoid budget resets.
from radar.models import SalesFeedback

PORTABLE_MODELS = (Source, Company, Lead, Document, Review, SourceRun, AIConfig, BusinessCapability, AnalysisRun, ExtractionRun, EntityCandidate, ModelCall, SearchCall, SourceManifest, ResearchEvent, KeywordCandidate, KeywordCursor, SalesFeedback)
BUSINESS_MODELS = PORTABLE_MODELS + (RuntimeState,)
PORTABLE_MODEL_MAP = {model._meta.label_lower: model for model in PORTABLE_MODELS}
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50000


def safe_raw_path(value, *, directory=False):
    """Accept a portable raw/ path, including Windows path/ADS restrictions."""
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise CommandError("原始文件路径不安全。")
    candidate = value[:-1] if directory and value.endswith("/") else value
    parts = candidate.split("/")
    if parts[0] != "raw" or len(parts) < (1 if directory else 2):
        raise CommandError("原始文件必须位于 raw/ 目录。")
    for part in parts:
        if not part or part in {".", ".."} or part.rstrip(" .") != part or re.search(r'[<>:"|?*]', part):
            raise CommandError("原始文件路径不安全。")
        if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I):
            raise CommandError("原始文件路径包含不兼容的设备名称。")
    return PurePosixPath(*parts)


def raw_source_file(relative, *, legacy_probe=False):
    base = settings.DATA_DIR.resolve()
    path = base.joinpath(*safe_raw_path(relative).parts)
    current = base
    for part in path.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise CommandError("原始证据包含符号链接，未复制。")
    if not path.is_file():
        if legacy_probe and not relative.startswith("raw/probe_cache/"):
            return raw_source_file("raw/probe_cache/legacy/" + relative.removeprefix("raw/"))
        raise CommandError(f"数据库引用的原始文件不存在：{relative}")
    return path


@contextmanager
def database_snapshot():
    # A nested PostgreSQL transaction could already have read under READ COMMITTED.
    if connection.in_atomic_block:
        raise CommandError("备份需要独立数据库事务，请单独运行 backup_data 命令。")
    if connection.vendor not in {"sqlite", "postgresql"}:
        raise CommandError("本版本仅支持 SQLite 或 PostgreSQL 备份。")
    with transaction.atomic(using=connection.alias):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        # SQLite's first SELECT establishes the snapshot for this read transaction.
        yield


def snapshot_payload(*, compact=False):
    with database_snapshot():
        objects = []
        counts = {}
        for model in PORTABLE_MODELS:
            rows = list(model.objects.using(connection.alias).order_by("pk"))
            counts[model._meta.label_lower] = len(rows)
            if model is AIConfig:
                for row in rows:row.key_ref="";row.search_key_ref="";row.key_source="disabled"
            objects.extend(rows)
        payload = serializers.serialize("json", objects).encode("utf-8")
        raw_paths = sorted({obj.raw_path for obj in objects if isinstance(obj, Document) and obj.raw_path} | {e["raw_path"] for obj in objects if isinstance(obj,Company) for e in obj.discovery_evidence if e.get("raw_path")})
        raw_paths = sorted(set(raw_paths) | {obj.raw_path for obj in objects if isinstance(obj, KeywordCandidate) and obj.raw_path})
        site_records = [obj.result.get('website_candidate', {}) for obj in objects if isinstance(obj, KeywordCandidate)]
        site_records += [obj.result.get('research_recovery', {}) for obj in objects if isinstance(obj, KeywordCandidate)]
        site_records += [obj.result.get('discovery_feed', {}) for obj in objects if isinstance(obj, KeywordCandidate)]
        site_records += [e for obj in objects if isinstance(obj, KeywordCandidate) for e in obj.result.get('verification', {}).get('sources', [])]
        site_records += [item for obj in objects if isinstance(obj, KeywordCandidate) for item in obj.result.get('website_findings', [])]
        site_records += [item for obj in objects if isinstance(obj, KeywordCandidate) for item in obj.result.get('sales_recheck', {}).get('sources', [])]
        site_records += [item for obj in objects if isinstance(obj, Company) for item in obj.profile.get('keyword_website_candidates', [])]
        site_records += [obj.profile.get('official_website_evidence', {}) for obj in objects if isinstance(obj, Company)]
        site_records += [obj.profile.get('company_industry_evidence', {}) for obj in objects if isinstance(obj, Company)]
        site_records += [obj.result.get('website_evidence', {}) for obj in objects if isinstance(obj, KeywordCandidate)]
        site_records += [page for site in site_records for page in site.get('findings', [])]
        raw_paths = sorted(set(raw_paths) | {item['raw_path'] for item in site_records if item.get('raw_path')})
        raw_paths = sorted(set(raw_paths) | {obj.metadata['upload_path'] for obj in objects if isinstance(obj,Document) and obj.metadata.get('upload_path')})
        run_paths={e["raw_path"] for obj in objects if isinstance(obj,SourceRun)
                   for e in (obj.stats or {}).get("requests",[]) if isinstance(e,dict) and e.get("raw_path")}
        omitted=sorted(run_paths-set(raw_paths)) if compact else []
        if not compact:
            raw_paths=sorted(set(raw_paths)|run_paths)
        manifest = {
            "format": 2, "app": "0.2.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "objects": len(objects), "models": counts,
            "database": connection.vendor,
            "snapshot": "repeatable_read" if connection.vendor == "postgresql" else "read_transaction",
            "radar_sha256": hashlib.sha256(payload).hexdigest(),
            **({"omitted_probe_paths":omitted} if compact else {}),
        }
    return payload, raw_paths, manifest


class Command(BaseCommand):
    help = "Create a portable snapshot of business data; credentials and admin users are excluded."

    def add_arguments(self, parser):
        parser.add_argument("--output")
        parser.add_argument("--compact", action="store_true", help="SQLite business snapshot and referenced originals only")

    def handle(self, *args, **options):
        default = settings.DATA_DIR / "backups" / ("radar-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
        target = Path(options["output"] or default).expanduser().resolve()
        if target.exists():
            raise CommandError("目标文件已存在，请选择新文件名。")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with FileLock(str(settings.DATA_DIR / "worker.lock"), timeout=0):
                compact=options["compact"]
                if compact and connection.vendor!="sqlite":
                    raise CommandError('紧凑备份适用于 SQLite。')
                payload, raw_paths, manifest = snapshot_payload(compact=compact)
                if compact:
                    from radar.business_archive import write_compact
                    handle, name=tempfile.mkstemp(prefix=".radar-backup-",suffix=".zip",dir=target.parent)
                    os.close(handle)
                    temporary=Path(name)
                    write_compact(temporary,payload,raw_paths,manifest)
                    if target.exists():
                        raise CommandError("目标文件已存在，请选择新文件名。")
                    temporary.replace(target)
                    temporary=None
                    self.stdout.write(str(target))
                    return
                # Preserve directory snapshots even if no current database row
                # references them (e.g. an interrupted or superseded import).
                directory = settings.DATA_DIR / 'raw' / 'directories'
                if directory.is_symlink() or directory.parent.is_symlink():
                    raise CommandError("目录原始证据包含符号链接，未备份。")
                extra = []
                if directory.is_dir():
                    for path in directory.iterdir():
                        if path.suffix.lower() == '.html' and (path.is_file() or path.is_symlink()):
                            relative=path.relative_to(settings.DATA_DIR).as_posix()
                            raw_source_file(relative)
                            extra.append(relative)
                manifest['unreferenced_directory_evidence'] = sorted(set(extra)-set(raw_paths))
                raw_paths = sorted(set(raw_paths) | set(extra))
                if len(payload) > MAX_JSON_BYTES:
                    raise CommandError("备份数据 JSON 超过本地试用的 128MB 限制。")
                raw_files = [(relative, raw_source_file(relative, legacy_probe=True)) for relative in raw_paths]
                manifest["raw_sha256"] = {relative: hashlib.sha256(path.read_bytes()).hexdigest() for relative, path in raw_files}
                manifest_payload = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                if len({relative.casefold() for relative, _ in raw_files}) != len(raw_files):
                    raise CommandError("原始文件路径存在大小写冲突，无法生成跨平台备份。")
                if len(raw_files) + 2 > MAX_ARCHIVE_MEMBERS:
                    raise CommandError("备份文件数量超过本地试用限制。")
                if sum(path.stat().st_size for _, path in raw_files) + len(payload) + len(manifest_payload) > MAX_UNCOMPRESSED_BYTES:
                    raise CommandError("备份展开超过本地试用的 1GB 限制。")
                handle, name = tempfile.mkstemp(prefix=".radar-backup-", suffix=".zip", dir=target.parent)
                os.close(handle)
                temporary = Path(name)
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("radar.json", payload)
                    archive.writestr("manifest.json", manifest_payload)
                    # Content-addressed directory snapshots also include unreferenced
                    # files; arbitrary other raw files remain outside this backup.
                    for relative, path in raw_files:
                        before = path.stat()
                        archive.write(path, relative)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise CommandError("备份期间原始文件发生变化，请重试。")
                if target.exists():
                    raise CommandError("目标文件已存在，请选择新文件名。")
                temporary.replace(target)
                temporary = None
        except Timeout as exc:
            raise CommandError("采集正在写入，请等待本轮结束后再备份。") from exc
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f"备份失败：{type(exc).__name__}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.stdout.write(str(target))

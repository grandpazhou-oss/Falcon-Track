"""Portable backup/restore tests with real transactions and synthetic records."""

from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import stat
import sqlite3
import tempfile
from unittest.mock import patch
import zipfile

from django.core import serializers
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, connections
from django.test import TransactionTestCase, override_settings

from radar.management.commands.backup_data import BUSINESS_MODELS, PORTABLE_MODELS
from radar.models import Company, Document, Lead, ModelCall, Review, RuntimeState, Source, SourceRun


class BackupRestoreTests(TransactionTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="radar-backup-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.original = self.base / "original"
        self.restored = self.base / "restored"
        self.original.mkdir()
        self.restored.mkdir()
        self.configuration = override_settings(DATA_DIR=self.original, LEAD_AUTOMATIC_ENABLED=True)
        self.configuration.enable()
        self.addCleanup(self.configuration.disable)
        self.output = StringIO()

    def make_business_data(self):
        source = Source.objects.create(pk=31, key="synthetic", name="合成测试来源", url="https://example.invalid/", enabled=True, status="running")
        second_source = Source.objects.create(pk=32, key="synthetic_second", name="合成测试来源二", url="https://example.invalid/", enabled=True)
        company = Company.objects.create(pk=41, name="合成示例材料有限公司", ownership_status="unknown")
        lead = Lead.objects.create(
            pk=51, source=source, company=company, external_id="synthetic-1", title="合成运输采购",
            text="合成证据正文，不是真实公告。", content_hash="a" * 64,
            metadata={"synthetic": True}, review_status="accepted",
        )
        Document.objects.create(pk=61, lead=lead, content_hash=lead.content_hash, text=lead.text, raw_path="raw/synthetic/evidence.html")
        Review.objects.create(pk=71, lead=lead, status="accepted", note="合成营业复核记录。")
        SourceRun.objects.create(pk=81, source=source, status="running")
        SourceRun.objects.create(pk=82, source=second_source, status="queued")
        RuntimeState.objects.create(key="scheduler_control", value={"enabled": True})
        ModelCall.objects.create(status="success")
        raw = self.original / "raw/synthetic/evidence.html"
        raw.parent.mkdir(parents=True)
        raw.write_text("<p>合成原始证据</p>", encoding="utf-8")
        # Orphan files are not part of a database snapshot and must not be exported.
        (raw.parent / "unreferenced.html").write_text("不应备份", encoding="utf-8")
        return source, company, lead

    def clear_business_data(self):
        for model in (Review, Document, SourceRun, Lead, Company, Source, RuntimeState, ModelCall):
            model.objects.all().delete()

    def backup(self, name="snapshot.zip"):
        path = self.base / name
        call_command("backup_data", output=str(path), stdout=self.output)
        return path

    def restore(self, path):
        with override_settings(DATA_DIR=self.restored):
            call_command("restore_data", str(path), stdout=self.output)

    def archive(self, records, extra=(), name="crafted.zip"):
        path = self.base / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({"format": 2, "objects": len(records)}))
            archive.writestr("radar.json", json.dumps(records, ensure_ascii=False))
            for filename, content in extra:
                archive.writestr(filename, content)
        return path

    def records(self):
        objects = [obj for model in PORTABLE_MODELS for obj in model.objects.order_by("pk")]
        return json.loads(serializers.serialize("json", objects))

    def test_roundtrip_preserves_relations_evidence_and_resets_automation_and_sequences(self):
        self.make_business_data()
        archive = self.backup()
        with zipfile.ZipFile(archive) as snapshot:
            manifest = json.loads(snapshot.read("manifest.json"))
            self.assertEqual(manifest["objects"], 9)
            self.assertEqual(manifest["snapshot"], "read_transaction" if connection.vendor == "sqlite" else "repeatable_read")
            self.assertEqual(set(snapshot.namelist()), {"manifest.json", "radar.json", "raw/synthetic/evidence.html"})
            self.assertEqual(set(record["model"] for record in json.loads(snapshot.read("radar.json"))), {model._meta.label_lower for model in PORTABLE_MODELS if model.objects.exists()})
        self.clear_business_data()
        self.restore(archive)
        lead = Lead.objects.get(pk=51)
        self.assertEqual(lead.company.name, "合成示例材料有限公司")
        self.assertEqual(lead.source.key, "synthetic")
        self.assertEqual(lead.reviews.get().note, "合成营业复核记录。")
        self.assertEqual(lead.documents.get().raw_path, "raw/synthetic/evidence.html")
        self.assertEqual((self.restored / lead.documents.get().raw_path).read_text(encoding="utf-8"), "<p>合成原始证据</p>")
        self.assertFalse(Source.objects.filter(enabled=True).exists())
        self.assertEqual(set(SourceRun.objects.values_list("status", flat=True)), {"cancelled"})
        self.assertFalse(SourceRun.objects.filter(finished_at__isnull=True).exists())
        self.assertEqual(RuntimeState.objects.get(key="scheduler_control").value, {"enabled": False})
        self.assertEqual(ModelCall.objects.count(), 1)  # Historical requests retain their budget/audit record.
        from radar.services import automatic_enabled
        self.assertFalse(automatic_enabled())  # Even if the environment default is True.

        source = Source.objects.create(key="after-restore", name="恢复后来源", url="https://example.invalid/")
        company = Company.objects.create(name="恢复后新企业")
        new_lead = Lead.objects.create(source=source, company=company, external_id="new", title="恢复后公告", text="新正文", content_hash="b" * 64)
        document = Document.objects.create(lead=new_lead, content_hash=new_lead.content_hash, text=new_lead.text)
        review = Review.objects.create(lead=new_lead, status="new", note="恢复后复核")
        run = SourceRun.objects.create(source=source)
        for value, previous_max in ((source.pk, 32), (company.pk, 41), (new_lead.pk, 51), (document.pk, 61), (review.pk, 71), (run.pk, 82)):
            self.assertGreater(value, previous_max)

    def test_rejects_even_source_only_nonempty_database_without_deleting_it(self):
        source = Source.objects.create(key="existing", name="已有来源", url="https://example.invalid/")
        with self.assertRaisesMessage(CommandError, "空业务库"):
            self.restore(self.archive([]))
        self.assertTrue(Source.objects.filter(pk=source.pk).exists())
        self.assertEqual(RuntimeState.objects.count(), 0)

    def test_rejects_nonempty_runtime_and_budget_tables(self):
        archive = self.archive([])
        for model, fields in ((RuntimeState, {"key": "old", "value": {}}), (ModelCall, {"status": "success"})):
            with self.subTest(model=model._meta.label_lower):
                obj = model.objects.create(**fields)
                with self.assertRaisesMessage(CommandError, "空业务库"):
                    self.restore(archive)
                self.assertTrue(model.objects.filter(pk=obj.pk).exists())
                obj.delete()

    def test_rejects_zip_traversal_and_cross_platform_unsafe_paths(self):
        for index, filename in enumerate(("../outside.txt", "raw/../outside.txt", "/tmp/outside.txt", "raw\\outside.txt", "raw/source/file.html:stream", "raw/source/CON.txt")):
            with self.subTest(filename=filename):
                archive = self.archive([], [(filename, "bad")], name=f"unsafe-{index}.zip")
                with self.assertRaises(CommandError):
                    self.restore(archive)
                self.assertFalse((self.base / "outside.txt").exists())
                self.assertFalse(RuntimeState.objects.exists())

    def test_rejects_zip_symlink(self):
        member = zipfile.ZipInfo("raw/source/link")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesMessage(CommandError, "符号链接"):
            self.restore(self.archive([], [(member, "../../outside")]))

    def test_rejects_document_raw_path_escape_and_missing_evidence(self):
        self.make_business_data()
        records = self.records()
        self.clear_business_data()
        for index, raw_path in enumerate(("../outside.html", "raw/source/missing.html")):
            altered = deepcopy(records)
            next(record for record in altered if record["model"] == "radar.document")["fields"]["raw_path"] = raw_path
            with self.subTest(raw_path=raw_path), self.assertRaises(CommandError):
                self.restore(self.archive(altered, name=f"bad-reference-{index}.zip"))
            self.assertFalse(Source.objects.exists())

    def test_rejects_non_whitelisted_models_before_any_import(self):
        bad = {"model": "auth.user", "pk": 1, "fields": {"username": "injected"}}
        with self.assertRaisesMessage(CommandError, "未允许的数据表"):
            self.restore(self.archive([bad]))
        self.assertFalse(any(model.objects.exists() for model in BUSINESS_MODELS))

    def test_database_error_rolls_back_all_models_and_files(self):
        self.make_business_data()
        records = self.records()
        # A second company PK violates the unique name only when saved. This
        # tests actual database rollback, after a source and company were inserted.
        duplicate = deepcopy(next(record for record in records if record["model"] == "radar.company"))
        duplicate["pk"] = 42
        records.insert(3, duplicate)
        self.clear_business_data()
        archive = self.archive(records, [("raw/synthetic/evidence.html", "合成证据")])
        with self.assertRaisesMessage(CommandError, "未提交业务数据"):
            self.restore(archive)
        self.assertFalse(any(model.objects.exists() for model in BUSINESS_MODELS))
        self.assertFalse((self.restored / "raw").exists())
        self.assertEqual(list(self.restored.glob(".radar-restore-*")), [])

    def test_missing_raw_file_fails_backup_without_publishing_archive(self):
        self.make_business_data()
        (self.original / "raw/synthetic/evidence.html").unlink()
        with self.assertRaisesMessage(CommandError, "原始文件不存在"):
            self.backup()
        self.assertFalse((self.base / "snapshot.zip").exists())
        self.assertEqual(list(self.base.glob(".radar-backup-*")), [])

    def test_sequence_reset_uses_backend_sql(self):
        from radar.management.commands.restore_data import reset_business_sequences
        with patch.object(connection.ops, "sequence_reset_sql", return_value=["SELECT 1"]) as reset:
            reset_business_sequences()
        self.assertEqual(tuple(reset.call_args.args[1]), BUSINESS_MODELS)

    def test_commit_failure_compensates_promoted_raw_files(self):
        self.make_business_data()
        archive = self.backup()
        self.clear_business_data()
        with patch.object(connection, "commit", side_effect=IntegrityError("synthetic commit failure")):
            with self.assertRaisesMessage(CommandError, "未提交业务数据"):
                self.restore(archive)
        self.assertFalse(any(model.objects.exists() for model in BUSINESS_MODELS))
        self.assertFalse((self.restored / "raw").exists())

    def test_sqlite_snapshot_stays_consistent_across_an_interleaved_writer(self):
        """A real WAL writer commits between two exported table reads."""
        from django.db.backends.sqlite3.base import DatabaseWrapper
        from radar.management.commands import backup_data

        alias = "snapshot_contract"
        settings_dict = deepcopy(connection.settings_dict)
        database_file = self.base / "snapshot-consistency.sqlite3"
        settings_dict.update(ENGINE="django.db.backends.sqlite3", NAME=str(database_file), OPTIONS={})
        isolated = DatabaseWrapper(settings_dict, alias)
        connections[alias] = isolated
        try:
            with isolated.schema_editor() as editor:
                editor.create_model(Source)
                editor.create_model(Company)
            with isolated.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode=WAL")
            Source.objects.using(alias).create(key="snapshot", name="快照来源", url="https://example.invalid/")
            company = Company.objects.using(alias).create(name="快照开始时的企业")
            writer_committed = []

            def interleave(execute, sql, params, many, context):
                result = execute(sql, params, many, context)
                if 'FROM "radar_source"' in sql and not writer_committed:
                    with sqlite3.connect(database_file) as writer:
                        writer.execute("UPDATE radar_company SET name = ? WHERE id = ?", ("快照后的并发修改", company.pk))
                    writer_committed.append(True)
                return result

            with patch.object(backup_data, "connection", isolated), patch.object(backup_data, "PORTABLE_MODELS", (Source, Company)), isolated.execute_wrapper(interleave):
                payload, _, _ = backup_data.snapshot_payload()
            exported = next(record for record in json.loads(payload) if record["model"] == "radar.company")
            self.assertEqual(writer_committed, [True])
            self.assertEqual(exported["fields"]["name"], "快照开始时的企业")
            self.assertEqual(Company.objects.using(alias).get(pk=company.pk).name, "快照后的并发修改")
        finally:
            isolated.close()
            del connections[alias]

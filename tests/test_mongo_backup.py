"""Scheduled Mongo backup: config parsing, fail-open when unset, mongodump
command construction (subprocess mocked — mongodump is never actually run).
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.backup import mongo_backup
from app.backup.s3 import _parse_list_objects, _parse_s3_datetime
from app.config import Settings


# ─── config parsing ──────────────────────────────────────────────────────

def test_config_defaults_are_disabled_and_documented(monkeypatch):
    # No BACKUP_* env vars set anywhere in this process → the defaults below.
    for var in (
        "BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY",
        "BACKUP_S3_SECRET_KEY", "BACKUP_S3_REGION", "BACKUP_S3_PREFIX",
        "BACKUP_HOUR", "BACKUP_RETENTION_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.backup_s3_endpoint == ""
    assert s.backup_s3_bucket == ""
    assert s.backup_s3_access_key == ""
    assert s.backup_s3_secret_key == ""
    assert s.backup_s3_region == "auto"
    assert s.backup_s3_prefix == "mongo-backups"
    assert s.backup_hour == 4
    assert s.backup_retention_days == 30


def test_config_parses_env_vars(monkeypatch):
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("BACKUP_S3_ACCESS_KEY", "AKID")
    monkeypatch.setenv("BACKUP_S3_SECRET_KEY", "SECRET")
    monkeypatch.setenv("BACKUP_S3_REGION", "weur")
    monkeypatch.setenv("BACKUP_S3_PREFIX", "backups/prod")
    monkeypatch.setenv("BACKUP_HOUR", "2")
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "14")
    s = Settings(_env_file=None)
    assert s.backup_s3_endpoint == "https://acct.r2.cloudflarestorage.com"
    assert s.backup_s3_bucket == "my-bucket"
    assert s.backup_s3_access_key == "AKID"
    assert s.backup_s3_secret_key == "SECRET"
    assert s.backup_s3_region == "weur"
    assert s.backup_s3_prefix == "backups/prod"
    assert s.backup_hour == 2
    assert s.backup_retention_days == 14


# ─── fail-open / disabled-by-default ─────────────────────────────────────

def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        mongo_url="mongodb://localhost:27017",
        backup_s3_endpoint="",
        backup_s3_bucket="",
        backup_s3_access_key="",
        backup_s3_secret_key="",
        backup_s3_region="auto",
        backup_s3_prefix="mongo-backups",
        backup_hour=4,
        backup_retention_days=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_missing_required_vars_lists_each_unset_one():
    missing = mongo_backup._missing_required_vars(_settings())
    assert set(missing) == {
        "BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET",
        "BACKUP_S3_ACCESS_KEY", "BACKUP_S3_SECRET_KEY",
    }


def test_missing_required_vars_empty_when_all_set():
    s = _settings(
        backup_s3_endpoint="https://x", backup_s3_bucket="b",
        backup_s3_access_key="a", backup_s3_secret_key="s",
    )
    assert mongo_backup._missing_required_vars(s) == []


def test_run_mongo_backup_noops_when_disabled_no_exception(monkeypatch):
    monkeypatch.setattr(mongo_backup, "get_settings", lambda: _settings())

    async def boom(*args, **kwargs):
        raise AssertionError("subprocess must NOT be started when backup is disabled")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    # Must not raise — fail-open, mirrors qwen/transcribe optional-feature pattern.
    asyncio.run(mongo_backup.run_mongo_backup())


def test_run_mongo_backup_logs_disabled_once(monkeypatch, caplog):
    mongo_backup._logged_disabled = False  # reset module-level guard
    monkeypatch.setattr(mongo_backup, "get_settings", lambda: _settings())
    with caplog.at_level("INFO", logger="app.backup.mongo_backup"):
        asyncio.run(mongo_backup.run_mongo_backup())
        asyncio.run(mongo_backup.run_mongo_backup())
    disabled_lines = [r for r in caplog.records if "Mongo backup disabled" in r.message]
    assert len(disabled_lines) == 1
    mongo_backup._logged_disabled = False  # don't leak state into other tests


def test_run_mongo_backup_swallows_backup_failures(monkeypatch):
    fully_configured = _settings(
        backup_s3_endpoint="https://x", backup_s3_bucket="b",
        backup_s3_access_key="a", backup_s3_secret_key="s",
    )
    monkeypatch.setattr(mongo_backup, "get_settings", lambda: fully_configured)

    async def failing_run_backup(s):
        raise RuntimeError("mongodump exploded")

    monkeypatch.setattr(mongo_backup, "_run_backup", failing_run_backup)
    # Fail-open at the top level too: a real failure during dump/upload must
    # never raise into the scheduler.
    asyncio.run(mongo_backup.run_mongo_backup())


# ─── mongodump command construction (subprocess mocked, never actually run) ──

def test_build_mongodump_cmd():
    cmd = mongo_backup.build_mongodump_cmd("mongodb://user:pass@host/db", "/tmp/out.gz")
    assert cmd == [
        "mongodump",
        "--uri=mongodb://user:pass@host/db",
        "--archive=/tmp/out.gz",
        "--gzip",
    ]


def test_run_mongodump_invokes_expected_argv_and_succeeds(monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    asyncio.run(mongo_backup._run_mongodump("mongodb://host/db", "/tmp/a.gz"))
    assert calls == [("mongodump", "--uri=mongodb://host/db", "--archive=/tmp/a.gz", "--gzip")]


def test_run_mongodump_raises_on_nonzero_exit(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"connection refused"

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    with pytest.raises(RuntimeError, match="connection refused"):
        asyncio.run(mongo_backup._run_mongodump("mongodb://host/db", "/tmp/a.gz"))


# ─── credential redaction (secret-hygiene: URI must never reach a raised ──
# exception or a log call with its password intact) ──────────────────────

def test_redact_mongo_uri_masks_username_and_password():
    redacted = mongo_backup._redact_mongo_uri("mongodb://user:s3cr3t@host:27017/db")
    assert redacted == "mongodb://***:***@host:27017/db"
    assert "s3cr3t" not in redacted
    assert "user" not in redacted


def test_redact_mongo_uri_handles_srv_scheme_and_query_string():
    redacted = mongo_backup._redact_mongo_uri(
        "mongodb+srv://user:s3cr3t@cluster0.mongodb.net/db?retryWrites=true"
    )
    assert redacted == "mongodb+srv://***:***@cluster0.mongodb.net/db?retryWrites=true"
    assert "s3cr3t" not in redacted


def test_redact_mongo_uri_handles_multi_host_replica_set():
    uri = "mongodb://user:s3cr3t@host1:27017,host2:27017,host3:27017/db?replicaSet=rs0"
    redacted = mongo_backup._redact_mongo_uri(uri)
    assert redacted == "mongodb://***:***@host1:27017,host2:27017,host3:27017/db?replicaSet=rs0"
    assert "s3cr3t" not in redacted


def test_redact_mongo_uri_noop_when_no_credentials():
    assert mongo_backup._redact_mongo_uri("mongodb://localhost:27017/db") == (
        "mongodb://localhost:27017/db"
    )


def test_redact_mongo_uri_noop_when_username_only_no_password():
    # No ":" before the "@" — nothing that looks like a password to mask;
    # graceful no-op rather than guessing.
    uri = "mongodb://user@host/db"
    assert mongo_backup._redact_mongo_uri(uri) == uri


def test_run_mongodump_failure_redacts_uri_echoed_verbatim_in_stderr(monkeypatch):
    """Simulates the adversarial-review scenario: mongodump hits an early
    URI-parse error and echoes the --uri argument verbatim in stderr, before
    its own credential redaction would have kicked in. The RuntimeError raised
    by _run_mongodump must not carry the raw password."""
    mongo_url = "mongodb://dbuser:sup3rSecretPW@prod-host:27017/tg_ai_assistant"

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return (
                b"",
                f"Failed: error parsing uri: {mongo_url}".encode(),
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(mongo_backup._run_mongodump(mongo_url, "/tmp/a.gz"))

    message = str(exc_info.value)
    assert "sup3rSecretPW" not in message
    assert "dbuser" not in message
    assert "***:***@prod-host:27017" in message


# ─── S3 helper parsing (pure functions, no network) ──────────────────────

def test_parse_s3_datetime_with_and_without_fractional_seconds():
    assert _parse_s3_datetime("2026-07-01T04:00:00.123Z") is not None
    assert _parse_s3_datetime("2026-07-01T04:00:00Z") is not None
    assert _parse_s3_datetime("") is None


def test_parse_list_objects_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>my-bucket</Name>
  <Contents>
    <Key>mongo-backups/2026-07-01T04-00-00Z.archive.gz</Key>
    <LastModified>2026-07-01T04:00:01.000Z</LastModified>
    <Size>1234</Size>
  </Contents>
  <Contents>
    <Key>mongo-backups/2026-07-02T04-00-00Z.archive.gz</Key>
    <LastModified>2026-07-02T04:00:01.000Z</LastModified>
    <Size>1234</Size>
  </Contents>
</ListBucketResult>"""
    objects = _parse_list_objects(xml)
    assert [o.key for o in objects] == [
        "mongo-backups/2026-07-01T04-00-00Z.archive.gz",
        "mongo-backups/2026-07-02T04-00-00Z.archive.gz",
    ]
    assert all(o.last_modified is not None for o in objects)

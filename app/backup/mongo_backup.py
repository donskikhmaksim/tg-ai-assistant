"""Scheduled Mongo backup to an S3-compatible bucket (Cloudflare R2 recommended).

Runs on the shared `AsyncIOScheduler` (see app/main.py) once a day at
BACKUP_HOUR (default_timezone) — same cron pattern as `run_daily_summary`.

FAIL-OPEN / DISABLED BY DEFAULT, mirroring the qwen/transcribe optional-feature
pattern: `run_mongo_backup` is unconditionally scheduled, but if any required
`BACKUP_S3_*` var is unset it no-ops on every check (logged once per process,
not spammed on every daily run) and a fresh deploy is completely unaffected.
Any failure during the dump/upload/prune is caught, logged, and never raised
into the scheduler — the next scheduled run simply tries again.

Steps per successful run:
  1. `mongodump --uri=... --archive=<tmpfile> --gzip` as a subprocess. The
     DEPLOYED CONTAINER NEEDS `mongodb-database-tools` installed (see the
     Dockerfile — this was added alongside this feature; verify it's present
     if you're running a customized image).
  2. Upload the archive to `{BACKUP_S3_BUCKET}/{BACKUP_S3_PREFIX}/<ts>.archive.gz`
     via the hand-rolled SigV4 client in `s3.py`.
  3. Prune objects under that prefix older than BACKUP_RETENTION_DAYS.

Restore: `mongorestore --uri="$MONGO_URL" --archive=<file> --gzip` — see
DEPLOY.md for the full procedure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from ..config import Settings, get_settings
from .s3 import S3Client

logger = logging.getLogger(__name__)

# Logged once per process on the first disabled check, not on every scheduled
# run (this job runs daily, but repeating an identical "disabled" line forever
# is still noise worth avoiding — same spirit as the other optional features).
_logged_disabled = False


def _missing_required_vars(s: Settings) -> list[str]:
    """Which required backup vars are unset. Non-empty => the job no-ops."""
    missing = []
    if not s.mongo_url:
        missing.append("MONGO_URL")
    if not s.backup_s3_endpoint:
        missing.append("BACKUP_S3_ENDPOINT")
    if not s.backup_s3_bucket:
        missing.append("BACKUP_S3_BUCKET")
    if not s.backup_s3_access_key:
        missing.append("BACKUP_S3_ACCESS_KEY")
    if not s.backup_s3_secret_key:
        missing.append("BACKUP_S3_SECRET_KEY")
    return missing


def _redact_mongo_uri(uri: str) -> str:
    """Redact the userinfo (username:password) portion of a Mongo connection
    string so it's safe to embed in a raised exception or a log line, e.g.
    ``mongodb://user:pass@host/db`` -> ``mongodb://***:***@host/db``.

    Uses urllib.parse rather than a regex so it stays correct even if the
    password contains special characters. Returns the input unchanged if
    there's nothing to redact (no ``@``, or no ``:`` before it — i.e. no
    credentials present) — a safe no-op rather than guessing.
    """
    if "@" not in uri:
        return uri
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri
    netloc = parts.netloc
    if "@" not in netloc:
        return uri
    creds, _, host_part = netloc.rpartition("@")
    if ":" not in creds:
        return uri
    redacted_netloc = f"***:***@{host_part}"
    return urlunsplit((parts.scheme, redacted_netloc, parts.path, parts.query, parts.fragment))


def build_mongodump_cmd(mongo_url: str, archive_path: str) -> list[str]:
    """Pure command construction, kept separate from execution so it's
    testable without ever invoking a subprocess."""
    return [
        "mongodump",
        f"--uri={mongo_url}",
        f"--archive={archive_path}",
        "--gzip",
    ]


async def run_mongo_backup() -> None:
    """Scheduler entry point. Fail-open: never raises into the scheduler."""
    global _logged_disabled
    s = get_settings()
    missing = _missing_required_vars(s)
    if missing:
        if not _logged_disabled:
            logger.info(
                "Mongo backup disabled — missing %s (set BACKUP_S3_* to enable "
                "scheduled backups; see DEPLOY.md). This is optional and does "
                "not affect normal operation.",
                ", ".join(missing),
            )
            _logged_disabled = True
        return
    _logged_disabled = False

    try:
        await _run_backup(s)
    except Exception:  # noqa: BLE001 — a backup failure must never break the scheduler
        logger.exception("Mongo backup failed; will retry on the next scheduled run")


async def _run_backup(s: Settings) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        archive_path = os.path.join(tmp_dir, "mongo.archive.gz")
        await _run_mongodump(s.mongo_url, archive_path)

        size = os.path.getsize(archive_path)
        if size == 0:
            raise RuntimeError("mongodump produced an empty archive")

        with open(archive_path, "rb") as f:
            body = f.read()

        client = S3Client(
            endpoint=s.backup_s3_endpoint,
            bucket=s.backup_s3_bucket,
            access_key=s.backup_s3_access_key,
            secret_key=s.backup_s3_secret_key,
            region=s.backup_s3_region,
        )
        prefix = (s.backup_s3_prefix or "mongo-backups").strip("/")
        now = datetime.now(timezone.utc)
        key = f"{prefix}/{now.strftime('%Y-%m-%dT%H-%M-%SZ')}.archive.gz"

        await client.put_object(key, body, content_type="application/gzip")
        logger.info("Mongo backup uploaded: %s (%d bytes)", key, size)

        await _prune_old(client, prefix, s.backup_retention_days)


async def _run_mongodump(mongo_url: str, archive_path: str) -> None:
    cmd = build_mongodump_cmd(mongo_url, archive_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        stderr_text = stderr.decode(errors="replace")[:2000]
        # Defense in depth: if mongodump ever echoes the URI verbatim (e.g. an
        # early parse error before its own credential redaction kicks in),
        # strip the raw credentials out before they can reach a raised
        # exception (and, downstream, a `logger.exception(...)` call).
        stderr_text = stderr_text.replace(mongo_url, _redact_mongo_uri(mongo_url))
        raise RuntimeError(f"mongodump exited {proc.returncode}: {stderr_text}")


async def _prune_old(client: S3Client, prefix: str, retention_days: int) -> None:
    """Delete objects under the prefix older than retention_days. Best-effort —
    a prune failure is logged but never aborts (the backup we just made is
    already safely uploaded)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    try:
        objects = await client.list_objects(prefix=prefix + "/")
    except Exception:  # noqa: BLE001
        logger.warning("Mongo backup: failed to list objects for retention prune", exc_info=True)
        return

    pruned = 0
    for obj in objects:
        if obj.last_modified is not None and obj.last_modified < cutoff:
            try:
                await client.delete_object(obj.key)
                pruned += 1
            except Exception:  # noqa: BLE001 — one bad delete can't abort the prune
                logger.warning("Mongo backup: failed to prune %s", obj.key, exc_info=True)
    if pruned:
        logger.info(
            "Mongo backup retention: pruned %d object(s) older than %d day(s)",
            pruned, retention_days,
        )

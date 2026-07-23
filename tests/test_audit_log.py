"""Audit/restore plane (Phase 0): record→finalize round-trip, index presence,
fail-open gating, and the out-of-band reconciliation logic.

Follows the repo convention: pure-logic assertions + a tiny in-memory fake for
the few DB-touching paths (no real Mongo), driven with asyncio.run + monkeypatch.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bson import ObjectId

import app.repositories as repo
from app.audit import log as audit_log
from app.audit import poller
from app.audit import reconcile
from app.db import _ensure_indexes


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory fake Mongo (only the operations the audit helpers use)
# ─────────────────────────────────────────────────────────────────────────────
class FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    async def insert_one(self, doc):
        _id = doc.get("_id") or ObjectId()
        doc["_id"] = _id
        self.docs[_id] = dict(doc)
        return SimpleNamespace(inserted_id=_id)

    async def update_one(self, flt, update, upsert=False):
        _id = flt.get("_id")
        if _id in self.docs:
            self.docs[_id].update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1)

    async def find_one(self, flt, projection=None):
        _id = flt.get("_id")
        return dict(self.docs[_id]) if _id in self.docs else None


class FakeDB:
    def __init__(self):
        self.audit_log = FakeCollection()


def _enable(monkeypatch):
    monkeypatch.setattr(audit_log, "get_settings", lambda: SimpleNamespace(audit_enabled=True))


def _use_fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)
    return db


# ─────────────────────────────────────────────────────────────────────────────
# record_mutation → finalize_mutation round-trip
# ─────────────────────────────────────────────────────────────────────────────
def test_record_then_finalize_roundtrip(monkeypatch):
    _enable(monkeypatch)
    db = _use_fake_db(monkeypatch)

    async def scenario():
        rid = await audit_log.record_mutation(
            server="ticktick",
            tool="delta_poll",
            target={"id": "t1", "parent_id": "p1", "title": "Позвонить"},
            before={"title": "Позвонить", "due": None},
            op="update",
            capture_plane="out_of_band",
            actor={"kind": "owner_manual", "source": "delta_poll"},
        )
        assert rid is not None
        await audit_log.finalize_mutation(
            rid,
            after={"title": "Позвонить", "due": "2026-07-25"},
            result={"status": "success", "record_id": None, "error": None, "verified": True},
        )
        return rid, db.audit_log.docs[rid]

    rid, doc = _run(scenario())
    # Pre-record fields survive.
    assert doc["server"] == "ticktick"
    assert doc["capture_plane"] == "out_of_band"
    assert doc["op"] == "update"
    assert doc["schema_v"] == audit_log.SCHEMA_V
    assert doc["ts_local"].endswith(("-07:00", "-08:00"))  # America/Los_Angeles
    # Finalize patched after + result + recomputed diff.
    assert doc["after"] == {"title": "Позвонить", "due": "2026-07-25"}
    assert doc["result"]["status"] == "success" and doc["result"]["verified"] is True
    assert doc["diff"] == ["due: ∅ → 2026-07-25"]
    assert doc["diff_fields"] == ["due"]


def test_record_infers_op_when_omitted(monkeypatch):
    _enable(monkeypatch)
    db = _use_fake_db(monkeypatch)

    async def scenario():
        rid = await audit_log.record_mutation(
            server="ticktick", tool="delta_poll",
            target={"id": "t2"}, before=None, after={"title": "New"},
        )
        return db.audit_log.docs[rid]

    doc = _run(scenario())
    assert doc["op"] == "create"  # no before + after → create


# ─────────────────────────────────────────────────────────────────────────────
# Fail-open gating
# ─────────────────────────────────────────────────────────────────────────────
def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(audit_log, "get_settings", lambda: SimpleNamespace(audit_enabled=False))
    # get_db must never be called when disabled.
    def _boom():
        raise AssertionError("get_db should not be called when auditing is disabled")
    monkeypatch.setattr(repo, "get_db", _boom)

    rid = _run(audit_log.record_mutation(
        server="ticktick", tool="delta_poll", target={"id": "x"}, before={"a": 1},
    ))
    assert rid is None
    # finalize with a None id is a harmless no-op.
    _run(audit_log.finalize_mutation(None, after={"a": 2}))


def test_record_fails_open_on_write_error(monkeypatch):
    _enable(monkeypatch)

    async def boom(_doc):
        raise RuntimeError("mongo down")
    monkeypatch.setattr(repo, "insert_audit_record", boom)

    # A write blowing up returns None instead of raising into the pipeline.
    rid = _run(audit_log.record_mutation(
        server="ticktick", tool="delta_poll", target={"id": "x"}, before={"a": 1},
    ))
    assert rid is None


# ─────────────────────────────────────────────────────────────────────────────
# Index / TTL presence in _ensure_indexes
# ─────────────────────────────────────────────────────────────────────────────
class RecordingCollection:
    def __init__(self, name, store):
        self.name = name
        self.store = store

    async def create_index(self, keys, **kwargs):
        self.store.setdefault(self.name, {"single": [], "many": []})
        self.store[self.name]["single"].append({"keys": keys, "kwargs": kwargs})

    async def create_indexes(self, models):
        self.store.setdefault(self.name, {"single": [], "many": []})
        for m in models:
            self.store[self.name]["many"].append(m.document)

    async def drop_index(self, name):
        pass


class RecordingDB:
    def __init__(self):
        self.store = {}

    def __getattr__(self, name):
        # Every collection access returns a recorder writing into the shared store.
        return RecordingCollection(name, self.__dict__.setdefault("store", {}))


def test_audit_indexes_created():
    db = RecordingDB()
    _run(_ensure_indexes(db, raw_ttl_seconds=100, audit_ttl_seconds=4242))
    store = db.store

    # audit_log TTL on ts with the configured expiry + name.
    ttl = [s for s in store["audit_log"]["single"] if s["kwargs"].get("name") == "audit_ttl"]
    assert ttl, "audit_ttl index missing"
    assert ttl[0]["kwargs"]["expireAfterSeconds"] == 4242
    assert ttl[0]["keys"] == [("ts", 1)]

    # Supporting audit_log indexes (per-object history, trace_id rollback…).
    many_keys = [tuple(k for k, _ in doc["key"].items()) for doc in store["audit_log"]["many"]]
    assert ("target.id", "ts") in many_keys
    assert ("server", "op", "ts") in many_keys
    assert ("actor.trace_id",) in many_keys

    # state_snapshots unique (server, targetId); sync_cursors unique (provider).
    snap = store["state_snapshots"]["single"][0]
    assert snap["keys"] == [("server", 1), ("targetId", 1)] and snap["kwargs"].get("unique")
    cur = store["sync_cursors"]["single"][0]
    assert cur["keys"] == [("provider", 1)] and cur["kwargs"].get("unique")


# ─────────────────────────────────────────────────────────────────────────────
# reconcile: diff / op inference
# ─────────────────────────────────────────────────────────────────────────────
def test_build_diff_and_op():
    assert reconcile.infer_op(None, {"title": "x"}) == "create"
    assert reconcile.infer_op({"title": "x"}, None) == "delete"
    assert reconcile.infer_op({"title": "x"}, {"title": "y"}) == "update"
    # completion special-case
    assert reconcile.infer_op({"status": "0"}, {"status": "2"}) == "complete"
    assert reconcile.build_diff({"due": None}, {"due": "2026-07-25"}) == ["due: ∅ → 2026-07-25"]
    # server-bookkeeping fields are ignored
    assert reconcile.build_diff({"etag": "a"}, {"etag": "b"}) == []


# ─────────────────────────────────────────────────────────────────────────────
# reconcile: in-band echo detection (our own edit round-tripping is dropped)
# ─────────────────────────────────────────────────────────────────────────────
def _rec(target_id, ts, diff_fields):
    return {"target": {"id": target_id}, "ts": ts, "diff_fields": diff_fields}


def test_echo_dropped_when_matching_inband():
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    inband = [_rec("t1", now - timedelta(seconds=30), ["due"])]
    # Same target, in-window, overlapping field → it's our echo.
    assert reconcile.is_inband_echo("t1", {"due"}, now, inband, window_seconds=120) is True


def test_not_echo_when_outside_window():
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    inband = [_rec("t1", now - timedelta(seconds=600), ["due"])]
    assert reconcile.is_inband_echo("t1", {"due"}, now, inband, window_seconds=120) is False


def test_not_echo_when_fields_disagree():
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    # In-band changed `due`; the polled change touched `title` → a real hand-edit.
    inband = [_rec("t1", now - timedelta(seconds=10), ["due"])]
    assert reconcile.is_inband_echo("t1", {"title"}, now, inband, window_seconds=120) is False


def test_not_echo_for_different_target():
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    inband = [_rec("OTHER", now, ["due"])]
    assert reconcile.is_inband_echo("t1", {"due"}, now, inband) is False


# ─────────────────────────────────────────────────────────────────────────────
# reconcile: source attribution (owner_manual vs collaborator vs automation)
# ─────────────────────────────────────────────────────────────────────────────
def test_attribution_collaborator_when_named_non_owner():
    actor = reconcile.classify_source(
        {"user_id": "ttk_u_88", "name": "Иван"}, owner_identity="ttk_owner", is_echo=False
    )
    assert actor["kind"] == "collaborator"
    assert actor["who"]["name"] == "Иван"
    assert actor["attribution_confidence"] == "high"


def test_attribution_owner_manual_when_named_owner():
    actor = reconcile.classify_source(
        {"user_id": "ttk_owner"}, owner_identity="ttk_owner", is_echo=False
    )
    assert actor["kind"] == "owner_manual"
    assert actor["attribution_confidence"] == "high"


def test_attribution_owner_manual_low_when_unnamed():
    actor = reconcile.classify_source(None, owner_identity="ttk_owner", is_echo=False)
    assert actor["kind"] == "owner_manual"
    assert actor["attribution_confidence"] == "low"


def test_attribution_automation_when_echo():
    actor = reconcile.classify_source({"name": "whoever"}, owner_identity=None, is_echo=True)
    assert actor["kind"] == "automation"


# ─────────────────────────────────────────────────────────────────────────────
# poller: pure snapshot diff (creates + updates; disappearances deferred)
# ─────────────────────────────────────────────────────────────────────────────
def test_diff_states_detects_create_and_update():
    current = {
        "t1": {"title": "A", "projectId": "p"},          # unchanged
        "t2": {"title": "B2", "projectId": "p"},         # updated
        "t3": {"title": "C", "projectId": "p"},          # new
    }
    snapshots = {
        "t1": {"state": {"title": "A", "projectId": "p"}},
        "t2": {"state": {"title": "B", "projectId": "p"}},
        # t3 absent → create; t9 present in snapshots only → NOT emitted (deferred)
        "t9": {"state": {"title": "Z", "projectId": "p"}},
    }
    changes = {c["target_id"]: c for c in poller._diff_states(current, snapshots)}
    assert set(changes) == {"t2", "t3"}
    assert changes["t3"]["before"] is None                 # create
    assert changes["t2"]["before"]["title"] == "B"         # update carries prior state
    assert "t9" not in changes                             # disappearance deferred


# ─────────────────────────────────────────────────────────────────────────────
# poller: fail-open entry points
# ─────────────────────────────────────────────────────────────────────────────
def test_poll_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(audit_log, "get_settings", lambda: SimpleNamespace(audit_enabled=False))

    async def boom():
        raise AssertionError("resolve_ticktick should not be called when disabled")
    monkeypatch.setattr(poller, "resolve_ticktick", boom)
    _run(poller.run_ticktick_audit_poll())  # returns cleanly, no raise


def test_poll_noop_when_no_connector(monkeypatch):
    monkeypatch.setattr(audit_log, "get_settings", lambda: SimpleNamespace(audit_enabled=True))

    async def none():
        return None
    monkeypatch.setattr(poller, "resolve_ticktick", none)
    _run(poller.run_ticktick_audit_poll())  # no connector → clean no-op


# ─────────────────────────────────────────────────────────────────────────────
# poller: seeding gated on sync_cursor presence, not snapshot emptiness —
# regression tests for the false-creation-storm bug. A seed that's interrupted
# partway through (exception swallowed by the outer fail-open try/except, or
# the process just dying) used to leave `state_snapshots` partially populated
# but `sync_cursors` never written; gating `seeding = not snapshots` then
# treated the NEXT cycle as already-seeded and diffed against the partial
# snapshot, emitting a spurious OP_CREATE for every task the interrupted seed
# missed. Gating on the cursor's presence instead means a never-completed seed
# always safely re-seeds (upsert is idempotent).
# ─────────────────────────────────────────────────────────────────────────────
class FakeTT:
    """Minimal TickTickMCP stand-in: one project, a fixed set of task cards."""

    def __init__(self, tasks):
        self._tasks = tasks

    async def get_projects(self):
        return [{"id": "p1"}]

    async def get_project_tasks(self, project_id, limit=None):
        return self._tasks


def _cards(*ids):
    return [{"id": tid, "title": f"task-{tid}"} for tid in ids]


def test_fresh_state_seeds_and_writes_cursor(monkeypatch):
    """(1) No snapshots, no cursor → seeds correctly and writes the cursor."""
    upserted = []
    cursor_writes = []

    async def fake_get_cursor(provider):
        return None

    async def fake_upsert(server, target_id, state):
        upserted.append(target_id)

    async def fake_set_cursor(provider, cursor):
        cursor_writes.append(cursor)

    async def boom_list_snapshots(server):
        raise AssertionError("list_state_snapshots should not be called while seeding")

    monkeypatch.setattr(repo, "get_sync_cursor", fake_get_cursor)
    monkeypatch.setattr(repo, "upsert_state_snapshot", fake_upsert)
    monkeypatch.setattr(repo, "set_sync_cursor", fake_set_cursor)
    monkeypatch.setattr(repo, "list_state_snapshots", boom_list_snapshots)

    _run(poller._poll_ticktick(FakeTT(_cards("t1", "t2"))))

    assert set(upserted) == {"t1", "t2"}
    assert len(cursor_writes) == 1


def test_snapshots_without_cursor_reseeds_instead_of_diffing(monkeypatch):
    """(2) Snapshots exist but sync_cursor does NOT (simulated crash mid old-
    style seed) → the poller RE-SEEDS rather than treating it as already-seeded
    and emitting spurious creates."""
    upserted = []

    async def fake_get_cursor(provider):
        return None  # cursor never written → not "seeded", regardless of snapshots

    async def fake_upsert(server, target_id, state):
        upserted.append(target_id)

    async def fake_set_cursor(provider, cursor):
        pass

    async def boom_list_snapshots(server):
        raise AssertionError("should re-seed, not diff against the partial snapshot")

    async def boom_record_mutation(**kwargs):
        raise AssertionError("no audit records (spurious creates) while (re-)seeding")

    monkeypatch.setattr(repo, "get_sync_cursor", fake_get_cursor)
    monkeypatch.setattr(repo, "upsert_state_snapshot", fake_upsert)
    monkeypatch.setattr(repo, "set_sync_cursor", fake_set_cursor)
    monkeypatch.setattr(repo, "list_state_snapshots", boom_list_snapshots)
    monkeypatch.setattr(audit_log, "record_mutation", boom_record_mutation)

    _run(poller._poll_ticktick(FakeTT(_cards("t1", "t2", "t3"))))

    assert set(upserted) == {"t1", "t2", "t3"}  # re-seeded silently, no creates emitted


def test_seed_loop_survives_individual_upsert_failure(monkeypatch):
    """(3) A single snapshot upsert failure doesn't abort the whole seed loop,
    and the cursor is still written once the loop finishes."""
    upserted = []
    cursor_writes = []

    async def fake_get_cursor(provider):
        return None

    async def flaky_upsert(server, target_id, state):
        if target_id == "t2":
            raise RuntimeError("transient mongo write error")
        upserted.append(target_id)

    async def fake_set_cursor(provider, cursor):
        cursor_writes.append(cursor)

    monkeypatch.setattr(repo, "get_sync_cursor", fake_get_cursor)
    monkeypatch.setattr(repo, "upsert_state_snapshot", flaky_upsert)
    monkeypatch.setattr(repo, "set_sync_cursor", fake_set_cursor)

    _run(poller._poll_ticktick(FakeTT(_cards("t1", "t2", "t3"))))

    assert set(upserted) == {"t1", "t3"}  # t2 failed but didn't abort the loop
    assert len(cursor_writes) == 1  # cursor still written after the loop completes

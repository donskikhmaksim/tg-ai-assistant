"""scripts/push_local_tasks.py — reconciliation of freshly-created TickTick
tasks back onto their local Mongo docs.

Regression covered: two local docs (possibly from different chats) sharing
the exact same title text used to collide during reconciliation:
  - the old `doc_by_title` dict was keyed by TITLE ONLY, so the second doc
    silently overwrote the first — the first doc was never reconciled and
    kept ticktickTaskId=None forever (re-created as a duplicate on every
    future run).
  - the old local `search_task_id()` helper matched by SUBSTRING
    (`title.lower() in line.lower()`), so a shorter title could bind to a
    longer near-duplicate's TickTick id.

This test drives `main()` end-to-end against fakes (no real Mongo/MCP) and
asserts BOTH docs get a ticktickTaskId, and that they're bound to two
DIFFERENT TickTick ids — not the same one, and not left unset.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "push_local_tasks.py"

# scripts/push_local_tasks.py is deliberately .gitignore'd (manual, one-off
# maintenance script — never committed, never deployed). This test loads it
# straight off disk, so on a checkout where the file was never pulled down
# it simply isn't there; skip cleanly instead of failing CI elsewhere.
if not SCRIPT_PATH.exists():
    pytest.skip(f"{SCRIPT_PATH} not present (gitignored, local-only script)", allow_module_level=True)


def _load_module():
    spec = importlib.util.spec_from_file_location("push_local_tasks_under_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────
class FakeTasksCollection:
    def __init__(self, docs: list[dict]):
        self.docs = {d["_id"]: d for d in docs}

    def find(self, query):
        async def gen():
            for doc in list(self.docs.values()):
                if all(doc.get(k) == v for k, v in query.items()):
                    yield dict(doc)
        return gen()

    async def update_one(self, flt, update):
        _id = flt["_id"]
        self.docs[_id].update(update.get("$set", {}))


class FakeDB:
    def __init__(self, docs):
        self.tasks = FakeTasksCollection(docs)

    def __getitem__(self, name):
        assert name == "tasks"
        return self.tasks


class FakeMotorClient:
    """Stands in for AsyncIOMotorClient(mongo_url)."""

    def __init__(self, db: FakeDB):
        self._db = db

    def __call__(self, mongo_url):  # mimics AsyncIOMotorClient(url) constructor
        return self

    def __getitem__(self, name):
        return self._db

    def close(self):
        pass


class FakeTT:
    """Fakes TickTickMCP: no existing tasks up front; create_tasks() appends
    one (title, id) pair per task; find_task_id() does an EXACT-title lookup
    over what's been "created", honouring `exclude` — mirrors the real
    find_task_id contract this test is meant to validate."""

    def __init__(self, url=None):
        self._existing: list[tuple[str, str]] = []
        self._next_id = 0

    async def find_task_id(self, title, exclude=None):
        exclude = exclude or set()
        for t, tid in self._existing:
            if t == title and tid not in exclude:
                return tid
        return None

    async def create_tasks(self, tasks, summary):
        for t in tasks:
            self._next_id += 1
            tid = f"NEW{self._next_id}"
            self._existing.append((t["title"], tid))
        return f"Создано {len(tasks)}"


def _settings_stub(**overrides):
    base = dict(
        mongo_url="mongodb://fake",
        mongo_db="fakedb",
        ticktick_mcp_url="http://fake-mcp",
        default_project_id="inbox",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_duplicate_title_docs_bind_to_different_ticktick_ids(monkeypatch):
    docs = [
        {
            "_id": "docA",
            "chatId": "chatA",
            "task": "Позвонить в банк",
            "status": "open",
            "ticktickTaskId": None,
        },
        {
            "_id": "docB",
            "chatId": "chatB",
            "task": "Позвонить в банк",
            "status": "open",
            "ticktickTaskId": None,
        },
    ]
    db = FakeDB(docs)

    mod = _load_module()
    monkeypatch.setattr(mod, "AsyncIOMotorClient", FakeMotorClient(db))
    monkeypatch.setattr(mod, "TickTickMCP", FakeTT)
    monkeypatch.setattr(mod, "get_settings", lambda: _settings_stub())
    monkeypatch.setenv("MONGO_URL", "mongodb://fake")
    monkeypatch.setenv("MONGO_DB", "fakedb")
    monkeypatch.setenv("TICKTICK_MCP_URL", "http://fake-mcp")

    asyncio.run(mod.main())

    id_a = db.tasks.docs["docA"]["ticktickTaskId"]
    id_b = db.tasks.docs["docB"]["ticktickTaskId"]

    # Neither doc is left unreconciled (the old dict-keyed-by-title bug would
    # leave docA at None forever).
    assert id_a is not None
    assert id_b is not None
    # The two docs must NOT collapse onto the same TickTick task.
    assert id_a != id_b


def test_single_doc_still_reconciles_normally(monkeypatch):
    docs = [
        {
            "_id": "docA",
            "chatId": "chatA",
            "task": "Купить молоко",
            "status": "open",
            "ticktickTaskId": None,
        },
    ]
    db = FakeDB(docs)

    mod = _load_module()
    monkeypatch.setattr(mod, "AsyncIOMotorClient", FakeMotorClient(db))
    monkeypatch.setattr(mod, "TickTickMCP", FakeTT)
    monkeypatch.setattr(mod, "get_settings", lambda: _settings_stub())
    monkeypatch.setenv("MONGO_URL", "mongodb://fake")
    monkeypatch.setenv("MONGO_DB", "fakedb")
    monkeypatch.setenv("TICKTICK_MCP_URL", "http://fake-mcp")

    asyncio.run(mod.main())

    assert db.tasks.docs["docA"]["ticktickTaskId"] == "NEW1"

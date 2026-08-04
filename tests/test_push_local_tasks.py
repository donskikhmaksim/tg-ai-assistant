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
    """Fakes TickTickMCP. `_existing` rows are {"title", "id", "project_id",
    "content"}. `seed()` lets a test pre-load a task that already exists in
    TickTick BEFORE the script runs — the realistic "already pushed by the
    main pipeline, id write-back failed" scenario; `create_tasks()` appends
    freshly-created rows the same way.

    `find_task_id_for_chat()` mirrors the real
    TickTickMCP.find_task_id_for_chat contract this test validates: an
    EXACT-title match scoped to `project_id`, disambiguated by the chat_id
    marker embedded in `content` (via the real
    `app.ticktick.mcp_client._chat_id_from_content`) when it singles out
    exactly one candidate; otherwise a positional pick flagged
    ambiguous=True. Falls back to the plain global `find_task_id()` lookup
    when the project has no matching cards at all — same as the real method.
    """

    def __init__(self, url=None):
        self._existing: list[dict] = []
        self._next_id = 0

    def seed(self, title, project_id, content="", tid=None):
        tid = tid or f"SEED{len(self._existing) + 1}"
        self._existing.append(
            {"title": title, "id": tid, "project_id": project_id, "content": content}
        )
        return tid

    async def find_task_id(self, title, exclude=None):
        exclude = exclude or set()
        for row in self._existing:
            if row["title"] == title and row["id"] not in exclude:
                return row["id"]
        return None

    async def find_task_id_for_chat(self, title, chat_id, project_id, exclude=None):
        from app.ticktick.mcp_client import _chat_id_from_content

        exclude = exclude or set()
        same = [
            r for r in self._existing
            if r["title"] == title
            and r["id"] not in exclude
            and (project_id is None or r["project_id"] == project_id)
        ]
        if not same:
            tid = await self.find_task_id(title, exclude=exclude)
            return tid, False
        if len(same) == 1:
            return same[0]["id"], False
        if chat_id:
            matches = [r for r in same if _chat_id_from_content(r["content"]) == chat_id]
            if len(matches) == 1:
                return matches[0]["id"], False
        return same[0]["id"], True

    async def create_tasks(self, tasks, summary):
        for t in tasks:
            self._next_id += 1
            tid = f"NEW{self._next_id}"
            self._existing.append({
                "title": t["title"],
                "id": tid,
                "project_id": t.get("project_id"),
                "content": t.get("content", ""),
            })
        return f"Создано {len(tasks)}"


def _settings_stub(**overrides):
    base = dict(
        mongo_url="mongodb://fake",
        mongo_db="fakedb",
        ticktick_mcp_url="http://fake-mcp",
        default_project_id="inbox",
        # Non-empty by default so the chat-of-origin marker IS stamped on
        # freshly-created tasks (see `_origin_marker` in push_local_tasks.py) —
        # exercises the real disambiguation path most tests care about.
        webapp_url="https://app.example.com",
        bot_token="test-bot-token",
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


def test_chat_marker_binds_preexisting_duplicate_titles_to_correct_chat(monkeypatch):
    """The scenario the independent review flagged: two local docs from
    DIFFERENT chats produced the exact same task title, and BOTH were already
    pushed to TickTick by the main pipeline (which stamps a chat-of-origin
    deep-link marker on every task it creates — see app/pipeline/batch.py
    `_chat_link`), but the id write-back to Mongo failed for both, so they
    still show ticktickTaskId=None locally.

    Before this fix, reconciliation only had the title to go on: which doc
    got which id was pure positional luck (via the exclude-set), not
    necessarily the TickTick task that doc's chat actually produced. With the
    chat-of-origin marker now consulted, each doc must bind to the ONE
    TickTick task whose embedded chat_id actually matches — not just to SOME
    different id.
    """
    docs = [
        {
            "_id": "docA",
            "chatId": "chatA",
            "task": "Написать отчёт",
            "status": "open",
            "ticktickTaskId": None,
            "projectId": "proj1",
        },
        {
            "_id": "docB",
            "chatId": "chatB",
            "task": "Написать отчёт",
            "status": "open",
            "ticktickTaskId": None,
            "projectId": "proj1",
        },
    ]
    db = FakeDB(docs)

    mod = _load_module()
    monkeypatch.setattr(mod, "AsyncIOMotorClient", FakeMotorClient(db))

    tt_instance = FakeTT()
    id_for_chat_a = tt_instance.seed(
        "Написать отчёт", "proj1",
        content="[💬 Прочитать переписку](https://app.example.com/chat?c=chatA&t=tok1)",
    )
    id_for_chat_b = tt_instance.seed(
        "Написать отчёт", "proj1",
        content="[💬 Прочитать переписку](https://app.example.com/chat?c=chatB&t=tok2)",
    )
    monkeypatch.setattr(mod, "TickTickMCP", lambda url=None: tt_instance)
    monkeypatch.setattr(mod, "get_settings", lambda: _settings_stub())
    monkeypatch.setenv("MONGO_URL", "mongodb://fake")
    monkeypatch.setenv("MONGO_DB", "fakedb")
    monkeypatch.setenv("TICKTICK_MCP_URL", "http://fake-mcp")

    asyncio.run(mod.main())

    # Each doc must bind to the TickTick task ITS OWN chat produced — not just
    # to two different ids.
    assert db.tasks.docs["docA"]["ticktickTaskId"] == id_for_chat_a
    assert db.tasks.docs["docB"]["ticktickTaskId"] == id_for_chat_b
    # Both were already in TickTick — nothing new should have been created.
    assert tt_instance._next_id == 0


def test_ambiguous_duplicate_titles_without_chat_marker_logs_warning(monkeypatch, caplog):
    """Two pre-existing TickTick tasks share a title but carry NO
    chat-of-origin marker at all (e.g. created before WEBAPP_URL was
    configured) — there is no data to disambiguate with. Both docs must still
    end up with SOME id (no doc left stranded at None), but the script must
    log a warning flagging the guess instead of silently pretending the
    binding is correct."""
    docs = [
        {
            "_id": "docA",
            "chatId": "chatA",
            "task": "Позвонить клиенту",
            "status": "open",
            "ticktickTaskId": None,
            "projectId": "proj1",
        },
        {
            "_id": "docB",
            "chatId": "chatB",
            "task": "Позвонить клиенту",
            "status": "open",
            "ticktickTaskId": None,
            "projectId": "proj1",
        },
    ]
    db = FakeDB(docs)

    mod = _load_module()
    monkeypatch.setattr(mod, "AsyncIOMotorClient", FakeMotorClient(db))

    tt_instance = FakeTT()
    id_a = tt_instance.seed("Позвонить клиенту", "proj1", content="")
    id_b = tt_instance.seed("Позвонить клиенту", "proj1", content="")
    monkeypatch.setattr(mod, "TickTickMCP", lambda url=None: tt_instance)
    monkeypatch.setattr(mod, "get_settings", lambda: _settings_stub())
    monkeypatch.setenv("MONGO_URL", "mongodb://fake")
    monkeypatch.setenv("MONGO_DB", "fakedb")
    monkeypatch.setenv("TICKTICK_MCP_URL", "http://fake-mcp")

    with caplog.at_level("WARNING", logger="push_local_tasks_under_test"):
        asyncio.run(mod.main())

    bound_a = db.tasks.docs["docA"]["ticktickTaskId"]
    bound_b = db.tasks.docs["docB"]["ticktickTaskId"]
    # No doc is left stranded at None, and the two docs still land on
    # different TickTick ids (via the exclude set) — just not PROVABLY the
    # right ones, since neither candidate carries an origin marker.
    assert {bound_a, bound_b} == {id_a, id_b}
    assert bound_a != bound_b
    # The uncertainty must be surfaced, not swallowed.
    assert any("Ambiguous title match" in rec.message for rec in caplog.records)

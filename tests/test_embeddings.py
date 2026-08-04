"""app/embeddings.py::embed() — fail-soft policy stays intact (None on any
error, never raises into the pipeline), but every failure is now also
recorded to `embedding_failures` (app/repositories.py::record_embedding_failure)
so the failure RATE is visible from the outside — see the dedup-cap +
embedding-visibility fix (2026-08-04).

Follows the repo convention: a tiny in-memory fake for the one Mongo write
involved, driven with asyncio.run + monkeypatch (same shape as
tests/test_audit_log.py).
"""
import asyncio
from types import SimpleNamespace

import app.embeddings as embeddings
import app.repositories as repo


def _run(coro):
    return asyncio.run(coro)


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id="fake-id")


class FakeDB:
    def __init__(self):
        self.embedding_failures = FakeCollection()


def _settings(embed_model="bge-m3"):
    return SimpleNamespace(
        embed_model=embed_model, qwen_base_url="http://mini.local", qwen_api_key="ollama",
    )


class _BoomClient:
    """Fake OpenAI client whose embeddings.create always raises."""

    class embeddings:  # noqa: N801 — mirrors the real client's attribute shape
        @staticmethod
        async def create(model, input):  # noqa: A002 — matches openai's kwarg name
            raise RuntimeError("endpoint unreachable")


class _OkClient:
    """Fake OpenAI client whose embeddings.create succeeds."""

    class embeddings:  # noqa: N801
        @staticmethod
        async def create(model, input):  # noqa: A002
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in input]
            )


# ── repo.record_embedding_failure ──────────────────────────────────────────
def test_record_embedding_failure_writes_expected_fields(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)

    _run(repo.record_embedding_failure("ValueError: boom", 3))

    assert len(db.embedding_failures.docs) == 1
    doc = db.embedding_failures.docs[0]
    assert doc["error"] == "ValueError: boom"
    assert doc["textCount"] == 3
    assert "ts" in doc


# ── embed() fail-soft + visibility ──────────────────────────────────────────
def test_embed_failure_returns_none_and_records_row(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)
    monkeypatch.setattr(embeddings, "get_settings", lambda: _settings())
    monkeypatch.setattr(embeddings, "_get_client", lambda: _BoomClient())

    result = _run(embeddings.embed(["task one", "task two"]))

    assert result is None  # fail-soft policy unchanged
    assert len(db.embedding_failures.docs) == 1
    doc = db.embedding_failures.docs[0]
    assert doc["textCount"] == 2
    assert "RuntimeError" in doc["error"]
    assert "endpoint unreachable" in doc["error"]


def test_embed_success_does_not_record_a_failure(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)
    monkeypatch.setattr(embeddings, "get_settings", lambda: _settings())
    monkeypatch.setattr(embeddings, "_get_client", lambda: _OkClient())

    result = _run(embeddings.embed(["task"]))

    assert result == [[0.1, 0.2]]
    assert db.embedding_failures.docs == []


def test_embed_no_texts_short_circuits_without_recording(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)
    monkeypatch.setattr(embeddings, "get_settings", lambda: _settings())

    result = _run(embeddings.embed([]))

    assert result is None
    assert db.embedding_failures.docs == []


def test_embed_disabled_short_circuits_without_recording(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)
    monkeypatch.setattr(embeddings, "get_settings", lambda: _settings(embed_model=""))

    result = _run(embeddings.embed(["task"]))

    assert result is None
    assert db.embedding_failures.docs == []


def test_embed_diagnostic_write_failure_still_fails_soft(monkeypatch):
    """If the diagnostic write itself blows up (e.g. Mongo down), embed() must
    still return None rather than propagate — diagnostics can never turn a
    fail-soft embedding failure into a hard pipeline crash."""
    async def boom_record(error, text_count):
        raise RuntimeError("mongo unreachable")

    monkeypatch.setattr(repo, "record_embedding_failure", boom_record)
    monkeypatch.setattr(embeddings, "get_settings", lambda: _settings())
    monkeypatch.setattr(embeddings, "_get_client", lambda: _BoomClient())

    result = _run(embeddings.embed(["task"]))

    assert result is None

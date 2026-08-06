"""Tests for app/pipeline/calendar_events.py — pure helpers (title assembly,
event_key, normalize_times, verify_quote, build_description) plus the tick
(extraction + Mongo bookkeeping + creation-phase idempotency), in the same
style as tests/test_claims.py: Mongo faked at the get_db() boundary, no real
Anthropic call, no real calendar-mcp transport.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import calendar_mcp as calendar_mcp_mod
from app import signals as signals_mod
from app.llm import claude
from app.pipeline import calendar_events


def _run(coro):
    return asyncio.run(coro)


# ── fake Mongo boundary: db.signals / db.calendar_events ───────────────────
# Extends tests/test_claims.py's FakeCollection with the operators this
# stage's writes actually issue: $setOnInsert, $addToSet, $inc (on top of
# the existing $set/$unset/$gte/$ne/$in/$lt).


def _get_path(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _matches(doc: dict, filt: dict) -> bool:
    for key, cond in filt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        val = _get_path(doc, key)
        if isinstance(cond, dict):
            if "$gte" in cond and not (val is not None and val >= cond["$gte"]):
                return False
            if "$lt" in cond and not (val is not None and val < cond["$lt"]):
                return False
            if "$ne" in cond and val == cond["$ne"]:
                return False
            if "$in" in cond and val not in cond["$in"]:
                return False
            if "$exists" in cond:
                present = key in doc
                if present != cond["$exists"]:
                    return False
        elif val != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        self._docs.sort(key=lambda d: _get_path(d, field), reverse=(direction < 0))
        return self

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for d in self._docs:
            yield d


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])

    def find(self, filt):
        return FakeCursor([d for d in self.docs if _matches(d, filt)])

    async def find_one(self, filt):
        for d in self.docs:
            if _matches(d, filt):
                return d
        return None

    async def update_one(self, filt, update, upsert=False):
        for d in self.docs:
            if _matches(d, filt):
                self._apply(d, update, is_insert=False)
                return
        if upsert:
            new_doc: dict = {}
            for k, v in filt.items():
                if not isinstance(v, dict):
                    new_doc[k] = v
            self._apply(new_doc, update, is_insert=True)
            self.docs.append(new_doc)

    @staticmethod
    def _apply(d: dict, update: dict, is_insert: bool) -> None:
        if is_insert:
            for k, v in update.get("$setOnInsert", {}).items():
                d[k] = v
        for k, v in update.get("$set", {}).items():
            d[k] = v
        for k, v in update.get("$addToSet", {}).items():
            arr = d.setdefault(k, [])
            if v not in arr:
                arr.append(v)
        for k, v in update.get("$inc", {}).items():
            d[k] = d.get(k, 0) + v
        for k in update.get("$unset", {}):
            d.pop(k, None)


class FakeDB:
    def __init__(self, signals=None, calendar_events=None):
        self.signals = FakeCollection(signals)
        self.calendar_events = FakeCollection(calendar_events)


def _use_fake_db(monkeypatch, signals_docs=None, calendar_events_docs=None) -> FakeDB:
    db = FakeDB(signals_docs, calendar_events_docs)
    monkeypatch.setattr(calendar_events, "get_db", lambda: db)
    monkeypatch.setattr(signals_mod, "get_db", lambda: db)
    return db


def _signal_doc(source_id: str, ts_start: datetime, verdict: str = "auto", **overrides) -> dict:
    doc = {
        "source": "telegram",
        "source_id": source_id,
        "ts_start": ts_start,
        "ts_end": ts_start,
        "title": f"Signal {source_id}",
        "summary": "владелец: встретимся завтра в 15:00, ок?",
        "participants_raw": ["владелец"],
        "raw_ref": {"chat_id": "user_1", "date": "2026-08-04", "message_ids": [1]},
        "triage": {
            "score": 0.6, "category": "commitment", "verdict": verdict,
            "reason": "r", "rubric_version": "v0", "scored_at": ts_start,
        },
    }
    doc.update(overrides)
    return doc


class FakeSettings:
    default_timezone = "America/Los_Angeles"
    calendar_event_default_duration_minutes = 60
    calendar_event_max_past_days = 2
    calendar_event_max_future_days = 180
    calendar_events_max_per_signal = 3
    calendar_events_min_confidence = 0.6
    calendar_events_max_attempts = 3
    calendar_events_lookback_days = 3
    calendar_events_enabled = False
    calendar_mcp_url = ""
    calendar_target_calendar_id = ""
    calendar_mcp_account = ""


# WALL-CLOCK RULE — the same one tests/test_triage.py and tests/test_claims.py
# document at their own _recent(). run_calendar_events_tick reads the REAL
# clock TWICE: once for its lookback window (`utcnow() -
# calendar_events_lookback_days`) and once as the anchor normalize_times
# judges an event's start against (`calendar_event_max_past_days` /
# `_max_future_days`). A fixture pinned to a calendar date therefore rots on
# BOTH counts a few days after it is written — the signal drops out of the
# window and the event start slides into the "too far in the past" band. Tests
# that pass `since=`/`anchor` in explicitly (get_extractable_signals,
# normalize_times) or that feed create_pending_events a ready-made document
# never consult the real clock and may keep fixed dates.
def _recent(hours_ago: float = 1) -> datetime:
    """A signal ts_start that is always inside the lookback window."""
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _soon_iso(hours_ahead: float = 6) -> str:
    """An event start that is always in the near future relative to the tick's
    own anchor, in FakeSettings.default_timezone. Seconds are zeroed because
    event_key() keys on minute precision — callers that need the SAME key
    across two ticks must reuse one returned value, not call this twice."""
    local = datetime.now(ZoneInfo(FakeSettings.default_timezone)) + timedelta(hours=hours_ahead)
    return local.replace(second=0, microsecond=0).isoformat()


async def _fake_record_mutation(**kwargs):
    return "rid-1"


async def _fake_finalize_mutation(rid, **kwargs):
    return None


def _enabled_settings() -> FakeSettings:
    s = FakeSettings()
    s.calendar_events_enabled = True
    s.calendar_mcp_url = "https://calendar.example/mcp/secret"
    s.calendar_target_calendar_id = "cal-id"
    return s


# ── build_event_title — assembly, not summarization ─────────────────────


def test_build_event_title_assembles_from_slots():
    assert calendar_events.build_event_title("call", "Иван Петров") == "Созвон: Иван Петров"


def test_build_event_title_without_counterparty_and_caps_words_and_length():
    assert calendar_events.build_event_title("call", None) == "Созвон"
    assert calendar_events.build_event_title("call", "") == "Созвон"

    title = calendar_events.build_event_title(
        "meeting", "Иван Петрович Сидоров из отдела продаж и маркетинга"
    )
    assert title.startswith("Встреча: ")
    who_words = title.split(": ", 1)[1].split()
    assert len(who_words) <= 2
    assert len(title) <= 40


def test_build_event_title_signature_has_no_summary_param():
    """Guarantees this is ASSEMBLY from slots, not abstractive compression
    of source text — the function cannot even see a summary/quote."""
    params = list(inspect.signature(calendar_events.build_event_title).parameters)
    assert params == ["activity_type", "counterparty"]


# ── event_key ────────────────────────────────────────────────────────────


def test_event_key_deterministic_and_normalizes_counterparty():
    k1 = calendar_events.event_key("call", "Иван Петров", "2026-08-04T15:00")
    k2 = calendar_events.event_key("call", "Иван Петров", "2026-08-04T15:00")
    assert k1 == k2

    k_diff_start = calendar_events.event_key("call", "Иван Петров", "2026-08-05T15:00")
    assert k_diff_start != k1

    k_case_space = calendar_events.event_key("call", "  ИВАН   петров  ", "2026-08-04T15:00")
    assert k_case_space == k1


# ── normalize_times ──────────────────────────────────────────────────────


def test_normalize_times_all_day_end_is_start_plus_one_day():
    anchor = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    ev = {"start": "2026-08-10", "end": None, "all_day": True}

    result = calendar_events.normalize_times(ev, anchor, FakeSettings())

    assert result == ("2026-08-10", "2026-08-11", True)


def test_normalize_times_timed_default_duration_and_bad_end():
    anchor = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    settings = FakeSettings()

    no_end = {"start": "2026-08-10T15:00:00-07:00", "end": None, "all_day": False}
    start, end, all_day = calendar_events.normalize_times(no_end, anchor, settings)
    assert all_day is False
    assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == timedelta(minutes=60)

    bad_end = {
        "start": "2026-08-10T15:00:00-07:00",
        "end": "2026-08-10T14:00:00-07:00",  # before start
        "all_day": False,
    }
    start2, end2, _ = calendar_events.normalize_times(bad_end, anchor, settings)
    assert datetime.fromisoformat(end2) - datetime.fromisoformat(start2) == timedelta(minutes=60)


def test_normalize_times_naive_gets_default_tz_and_z_converts_with_colon_offset():
    anchor = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    settings = FakeSettings()

    naive = {"start": "2026-08-10T15:00:00", "end": None, "all_day": False}
    start, _, _ = calendar_events.normalize_times(naive, anchor, settings)
    # regression against pipeline/dedup.py::to_ticktick_due's "%z" style
    # ("+0300", no colon) — Google Calendar's API needs a colon offset.
    assert re.search(r"[+-]\d{2}:\d{2}$", start)

    zulu = {"start": "2026-08-10T22:00:00Z", "end": None, "all_day": False}
    start_z, _, _ = calendar_events.normalize_times(zulu, anchor, settings)
    assert re.search(r"[+-]\d{2}:\d{2}$", start_z)


def test_normalize_times_rejects_garbage_and_out_of_range():
    anchor = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    settings = FakeSettings()

    assert calendar_events.normalize_times({"start": "not-a-date", "all_day": False}, anchor, settings) is None
    assert calendar_events.normalize_times({"start": None, "all_day": False}, anchor, settings) is None
    # max_past_days=2 -> anything before 2026-08-02 12:00 UTC is rejected
    too_past = {"start": "2026-07-20T10:00:00-07:00", "all_day": False}
    assert calendar_events.normalize_times(too_past, anchor, settings) is None
    # max_future_days=180
    too_future = {"start": "2027-06-01T10:00:00-07:00", "all_day": False}
    assert calendar_events.normalize_times(too_future, anchor, settings) is None
    bad_year = {"start": "2031-08-10T10:00:00-07:00", "all_day": False}
    assert calendar_events.normalize_times(bad_year, anchor, settings) is None


# ── verify_quote ─────────────────────────────────────────────────────────


def test_verify_quote_exact_match_true_paraphrase_false():
    summary = "владелец: встретимся завтра в 15:00, ок?"
    assert calendar_events.verify_quote("встретимся завтра в 15:00", summary) is True
    assert calendar_events.verify_quote("давайте созвонимся в три", summary) is False
    assert calendar_events.verify_quote(None, summary) is False
    assert calendar_events.verify_quote("q", None) is False


# ── build_description ───────────────────────────────────────────────────


def test_build_description_blocks_and_marker():
    sig = {
        "title": "Чат с клиентом", "source": "telegram",
        "source_id": "user_1:2026-08-03", "raw_ref": {"date": "2026-08-03"},
    }
    key = "abc123"

    ev = {"evidence_quote": "встретимся завтра", "needs_clarification": False}
    desc = calendar_events.build_description(sig, ev, key, quote_verified=True)
    assert "«встретимся завтра»" in desc
    assert f"key={key}" in desc
    assert "telegram:user_1:2026-08-03" in desc

    desc_unverified = calendar_events.build_description(sig, ev, key, quote_verified=False)
    assert "«" not in desc_unverified

    ev_clarify = {"evidence_quote": None, "needs_clarification": True}
    desc_clarify = calendar_events.build_description(sig, ev_clarify, key, quote_verified=False)
    assert "⚠️ Уточнить время" in desc_clarify


# ── get_extractable_signals ─────────────────────────────────────────────


def test_get_extractable_signals_filters_verdict_extracted_and_window(monkeypatch):
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    auto = _signal_doc("c-auto", now - timedelta(hours=1), verdict="auto")
    dropped = _signal_doc("c-drop", now - timedelta(hours=1), verdict="drop")
    already = _signal_doc("c-done", now - timedelta(hours=1), verdict="decision", calendar_extracted=True)
    too_old = _signal_doc("c-old", now - timedelta(days=10), verdict="decision")
    _use_fake_db(monkeypatch, signals_docs=[auto, dropped, already, too_old])

    pending = _run(calendar_events.get_extractable_signals(since=now - timedelta(days=3)))

    assert sorted(d["source_id"] for d in pending) == ["c-auto"]


# ── run_calendar_events_tick — extraction + bookkeeping ─────────────────


def test_run_tick_writes_pending_event_and_marks_signal(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])
    start = _soon_iso()

    async def fake_extract(items, tz_name):
        return [{
            "index": 0,
            "events": [{
                "activity_type": "call", "counterparty": "клиент",
                "start": start, "end": None, "all_day": False,
                "evidence_quote": None, "confidence": 0.9, "needs_clarification": False,
            }],
        }]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)

    extracted, created = _run(calendar_events.run_calendar_events_tick(FakeSettings()))

    assert extracted == 1
    assert created == 0
    assert len(db.calendar_events.docs) == 1
    doc = db.calendar_events.docs[0]
    assert doc["status"] == "pending"
    assert doc["title"] == "Созвон: клиент"
    by_id = {d["source_id"]: d for d in db.signals.docs}
    assert by_id["user_1:2026-08-04"]["calendar_extracted"] is True


def test_run_tick_marks_signal_even_with_zero_events(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])

    async def fake_extract(items, tz_name):
        return [{"index": 0, "events": []}]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)

    extracted, created = _run(calendar_events.run_calendar_events_tick(FakeSettings()))

    assert extracted == 1
    assert created == 0
    assert db.calendar_events.docs == []
    by_id = {d["source_id"]: d for d in db.signals.docs}
    assert by_id["user_1:2026-08-04"]["calendar_extracted"] is True


def test_run_tick_llm_failure_leaves_signal_unmarked(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])
    seen: list[list] = []

    async def failing_extract(items, tz_name):
        seen.append(items)
        return []  # whole batch call failed, same contract as score_triage_batch

    monkeypatch.setattr(claude, "extract_calendar_events_batch", failing_extract)

    result = _run(calendar_events.run_calendar_events_tick(FakeSettings()))

    # The signal MUST have reached the extractor — otherwise every assertion
    # below is also satisfied by "the tick found nothing at all", which is
    # what this test silently degraded into once its fixture aged out of the
    # lookback window (see _recent()'s wall-clock rule above).
    assert [len(items) for items in seen] == [1]
    assert result == (0, 0)
    assert db.calendar_events.docs == []
    assert "calendar_extracted" not in db.signals.docs[0]


def test_run_tick_low_confidence_event_is_skipped(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])
    start = _soon_iso()

    async def fake_extract(items, tz_name):
        return [{
            "index": 0,
            "events": [{
                "activity_type": "call", "counterparty": "клиент",
                "start": start, "end": None, "all_day": False,
                "evidence_quote": None, "confidence": 0.2, "needs_clarification": False,
            }],
        }]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)

    extracted, created = _run(calendar_events.run_calendar_events_tick(FakeSettings()))

    assert extracted == 1
    assert created == 0
    assert len(db.calendar_events.docs) == 1
    doc = db.calendar_events.docs[0]
    assert doc["status"] == "skipped"
    assert doc["skip_reason"] == "low_confidence"


def test_run_tick_flag_disabled_never_touches_calendar_client(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])
    start = _soon_iso()

    async def fake_extract(items, tz_name):
        return [{
            "index": 0,
            "events": [{
                "activity_type": "meeting", "counterparty": None,
                "start": start, "end": None, "all_day": False,
                "evidence_quote": None, "confidence": 0.9, "needs_clarification": False,
            }],
        }]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)

    def must_not_resolve():
        raise AssertionError("resolve_calendar must not be called when creation is disabled")

    monkeypatch.setattr(calendar_events, "resolve_calendar", must_not_resolve)

    settings = FakeSettings()
    settings.calendar_events_enabled = False
    settings.calendar_mcp_url = "https://calendar.example/mcp/secret"
    settings.calendar_target_calendar_id = "cal-id"

    extracted, created = _run(calendar_events.run_calendar_events_tick(settings))

    assert extracted == 1
    assert created == 0
    assert db.calendar_events.docs[0]["status"] == "pending"


def test_run_tick_missing_calendar_id_leaves_pending(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])
    start = _soon_iso()

    async def fake_extract(items, tz_name):
        return [{
            "index": 0,
            "events": [{
                "activity_type": "meeting", "counterparty": None,
                "start": start, "end": None, "all_day": False,
                "evidence_quote": None, "confidence": 0.9, "needs_clarification": False,
            }],
        }]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)

    def must_not_resolve():
        raise AssertionError("resolve_calendar must not be called without a target calendar id")

    monkeypatch.setattr(calendar_events, "resolve_calendar", must_not_resolve)

    settings = FakeSettings()
    settings.calendar_events_enabled = True
    settings.calendar_mcp_url = "https://calendar.example/mcp/secret"
    settings.calendar_target_calendar_id = ""  # not configured

    extracted, created = _run(calendar_events.run_calendar_events_tick(settings))

    assert extracted == 1
    assert created == 0
    assert db.calendar_events.docs[0]["status"] == "pending"


# ── CalendarMCP.create_event guard (primary calendar refusal) ───────────


def test_create_event_refuses_primary_calendar():
    client = calendar_mcp_mod.CalendarMCP(url="https://x", token="", account="", calendar_id="primary")

    with pytest.raises(calendar_mcp_mod.CalendarMCPError):
        _run(
            client.create_event(
                summary="s", start="2026-08-04", end="2026-08-05",
                description="d", all_day=True, time_zone="UTC",
            )
        )


def test_create_event_signature_has_no_attendees_param():
    params = inspect.signature(calendar_mcp_mod.CalendarMCP.create_event).parameters
    assert "attendees" not in params


def test_create_event_args_never_include_attendees_and_always_send_updates_none(monkeypatch):
    captured: dict = {}

    async def fake_call(self, name, args):
        captured["args"] = args
        return {"id": "evt1", "htmlLink": "https://calendar.google.com/evt1"}

    monkeypatch.setattr(calendar_mcp_mod.CalendarMCP, "call", fake_call)
    client = calendar_mcp_mod.CalendarMCP(url="https://x", token="t", account="acc", calendar_id="cal-id")

    result = _run(
        client.create_event(
            summary="s", start="2026-08-04T10:00:00-07:00", end="2026-08-04T11:00:00-07:00",
            description="d", all_day=False, time_zone="America/Los_Angeles",
        )
    )

    assert "attendees" not in captured["args"]
    assert captured["args"]["sendUpdates"] == "none"
    assert result["id"] == "evt1"


# ── idempotency / dedup / self-healing / retries / audit fail-open ──────


def test_idempotent_rerun_of_same_signal_creates_the_event_exactly_once(monkeypatch):
    sig = _signal_doc("user_1:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig])
    # ONE start value reused by both ticks — event_key() is derived from it,
    # and the whole point of this test is that the second tick lands on the
    # SAME key.
    start = _soon_iso()

    async def fake_extract(items, tz_name):
        return [{
            "index": 0,
            "events": [{
                "activity_type": "call", "counterparty": "клиент",
                "start": start, "end": None, "all_day": False,
                "evidence_quote": None, "confidence": 0.9, "needs_clarification": False,
            }],
        }]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)
    monkeypatch.setattr(calendar_events, "record_mutation", _fake_record_mutation)
    monkeypatch.setattr(calendar_events, "finalize_mutation", _fake_finalize_mutation)

    create_calls = {"n": 0}

    class FakeClient:
        async def find_event_by_marker(self, *, marker, time_min, time_max):
            return None

        async def create_event(self, **kwargs):
            create_calls["n"] += 1
            return {"id": f"evt-{create_calls['n']}", "htmlLink": "https://x"}

    monkeypatch.setattr(calendar_events, "resolve_calendar", lambda: FakeClient())

    settings = _enabled_settings()

    extracted1, created1 = _run(calendar_events.run_calendar_events_tick(settings))
    assert extracted1 == 1
    assert created1 == 1
    assert create_calls["n"] == 1
    assert len(db.calendar_events.docs) == 1
    assert db.calendar_events.docs[0]["status"] == "created"

    # Simulate app/signals.py resetting calendar_extracted because new
    # messages arrived in the same (chat, day) bucket (see
    # _STALE_ON_CONTENT_CHANGE) — the signal is re-extracted.
    db.signals.docs[0].pop("calendar_extracted", None)

    extracted2, created2 = _run(calendar_events.run_calendar_events_tick(settings))
    assert extracted2 == 1
    assert created2 == 0  # nothing pending this time
    assert create_calls["n"] == 1  # create_event NOT called a second time
    assert len(db.calendar_events.docs) == 1  # still one row, not duplicated
    assert db.calendar_events.docs[0]["status"] == "created"


def test_dedup_across_chats_same_meeting_collapses_to_one_event(monkeypatch):
    sig_a = _signal_doc("user_1:2026-08-04", _recent())
    sig_b = _signal_doc("user_2:2026-08-04", _recent())
    db = _use_fake_db(monkeypatch, signals_docs=[sig_a, sig_b])

    same_event = {
        "activity_type": "meeting", "counterparty": "Иван Петров",
        "start": _soon_iso(hours_ahead=24), "end": None, "all_day": False,
        "evidence_quote": None, "confidence": 0.9, "needs_clarification": False,
    }

    async def fake_extract(items, tz_name):
        return [
            {"index": 0, "events": [dict(same_event)]},
            {"index": 1, "events": [dict(same_event)]},
        ]

    monkeypatch.setattr(claude, "extract_calendar_events_batch", fake_extract)
    monkeypatch.setattr(calendar_events, "record_mutation", _fake_record_mutation)
    monkeypatch.setattr(calendar_events, "finalize_mutation", _fake_finalize_mutation)

    create_calls = {"n": 0}

    class FakeClient:
        async def find_event_by_marker(self, *, marker, time_min, time_max):
            return None

        async def create_event(self, **kwargs):
            create_calls["n"] += 1
            return {"id": "evt-1", "htmlLink": "https://x"}

    monkeypatch.setattr(calendar_events, "resolve_calendar", lambda: FakeClient())

    extracted, created = _run(calendar_events.run_calendar_events_tick(_enabled_settings()))

    assert extracted == 2
    assert created == 1
    assert create_calls["n"] == 1
    assert len(db.calendar_events.docs) == 1
    assert set(db.calendar_events.docs[0]["signal_ids"]) == {
        "telegram:user_1:2026-08-04", "telegram:user_2:2026-08-04",
    }


def test_self_heal_via_marker_lookup_skips_create_event(monkeypatch):
    key = calendar_events.event_key("call", "клиент", "2026-08-04T18:00")
    doc = {
        "event_key": key, "status": "pending", "skip_reason": None,
        "title": "Созвон: клиент", "activity_type": "call", "counterparty": "клиент",
        "start": "2026-08-04T18:00:00-07:00", "end": "2026-08-04T19:00:00-07:00",
        "all_day": False, "time_zone": "America/Los_Angeles",
        "description": f"[tg-ai-assistant] key={key}", "evidence_quote": None,
        "quote_verified": False, "confidence": 0.9, "needs_clarification": False,
        "signal_ids": ["telegram:user_1:2026-08-04"], "chat_ids": ["user_1"],
        "created_at": datetime.now(timezone.utc), "attempts": 0,
        "google_event_id": None, "google_html_link": None, "calendar_id": None,
        "account": None, "last_error": None, "synced_at": None,
    }
    db = _use_fake_db(monkeypatch, calendar_events_docs=[doc])
    monkeypatch.setattr(calendar_events, "record_mutation", _fake_record_mutation)
    monkeypatch.setattr(calendar_events, "finalize_mutation", _fake_finalize_mutation)

    class FakeClient:
        async def find_event_by_marker(self, *, marker, time_min, time_max):
            assert marker == f"key={key}"
            return {"id": "evt-existing", "htmlLink": "https://x"}

        async def create_event(self, **kwargs):
            raise AssertionError("create_event must not run when self-healed via the marker")

    created = _run(calendar_events.create_pending_events(FakeClient(), _enabled_settings()))

    assert created == 1
    stored = db.calendar_events.docs[0]
    assert stored["status"] == "created"
    assert stored["google_event_id"] == "evt-existing"


def _pending_doc(key: str = "k1") -> dict:
    return {
        "event_key": key, "status": "pending", "skip_reason": None,
        "title": "t", "activity_type": "call", "counterparty": None,
        "start": "2026-08-04T18:00:00-07:00", "end": "2026-08-04T19:00:00-07:00",
        "all_day": False, "time_zone": "America/Los_Angeles",
        "description": "d", "evidence_quote": None, "quote_verified": False,
        "confidence": 0.9, "needs_clarification": False,
        "signal_ids": [], "chat_ids": [], "created_at": datetime.now(timezone.utc),
        "attempts": 0, "google_event_id": None, "google_html_link": None,
        "calendar_id": None, "account": None, "last_error": None, "synced_at": None,
    }


def test_create_event_failure_increments_attempts_then_fails(monkeypatch):
    db = _use_fake_db(monkeypatch, calendar_events_docs=[_pending_doc()])
    monkeypatch.setattr(calendar_events, "record_mutation", _fake_record_mutation)
    monkeypatch.setattr(calendar_events, "finalize_mutation", _fake_finalize_mutation)

    class FailingClient:
        async def find_event_by_marker(self, **kwargs):
            return None

        async def create_event(self, **kwargs):
            raise calendar_mcp_mod.CalendarMCPError("boom")

    settings = _enabled_settings()
    settings.calendar_events_max_attempts = 2

    created1 = _run(calendar_events.create_pending_events(FailingClient(), settings))
    assert created1 == 0
    assert db.calendar_events.docs[0]["attempts"] == 1
    assert db.calendar_events.docs[0]["status"] == "pending"

    created2 = _run(calendar_events.create_pending_events(FailingClient(), settings))
    assert created2 == 0
    assert db.calendar_events.docs[0]["attempts"] == 2
    assert db.calendar_events.docs[0]["status"] == "failed"


def test_audit_failure_does_not_block_creation(monkeypatch):
    db = _use_fake_db(monkeypatch, calendar_events_docs=[_pending_doc("k-audit")])

    async def broken_record_mutation(**kwargs):
        raise RuntimeError("audit down")

    async def broken_finalize_mutation(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(calendar_events, "record_mutation", broken_record_mutation)
    monkeypatch.setattr(calendar_events, "finalize_mutation", broken_finalize_mutation)

    class OkClient:
        async def find_event_by_marker(self, **kwargs):
            return None

        async def create_event(self, **kwargs):
            return {"id": "evt-1", "htmlLink": "https://x"}

    created = _run(calendar_events.create_pending_events(OkClient(), _enabled_settings()))

    assert created == 1
    assert db.calendar_events.docs[0]["status"] == "created"
    assert db.calendar_events.docs[0]["google_event_id"] == "evt-1"

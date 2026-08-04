"""verify_still_open: pure logic (index matching, fail-safe, both the
still_open drop and the merges_into fold) isolated from the network — only
the CLI-shim HTTP call is faked."""
import asyncio

from app.llm import claude


class FakeSettings:
    claude_cli_url = "http://shim.local/claude"
    claude_cli_token = "t"
    claude_cli_model = "opus"
    claude_cli_timeout = 30
    dedup_judge_model = "sonnet"


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_shim(monkeypatch, ok_payload=None, verdicts=None):
    async def fake_post(self, url, json=None, headers=None):
        body = ok_payload
        if body is None:
            import json as _json
            body = {"ok": True, "result": _json.dumps({"verdicts": verdicts or []})}
        return _FakeResponse(body)
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    monkeypatch.setattr(claude, "get_settings", lambda: FakeSettings())


def test_empty_candidates_short_circuits(monkeypatch):
    monkeypatch.setattr(claude, "get_settings", lambda: FakeSettings())
    result = _run(claude.verify_still_open("окно", []))
    assert result.keep == []
    assert result.merge_into == []


def test_keeps_everything_when_model_addresses_nothing(monkeypatch):
    _patch_shim(monkeypatch, verdicts=[])
    result = _run(claude.verify_still_open("окно", [{"task": "A"}, {"task": "B"}]))
    assert result.keep == [True, True]
    assert result.merge_into == [None, None]


def test_drops_only_the_flagged_index(monkeypatch):
    _patch_shim(monkeypatch, verdicts=[
        {"index": 1, "still_open": False, "merges_into": None, "reason": "уже сделал"},
    ])
    result = _run(claude.verify_still_open("окно", [{"task": "A"}, {"task": "B"}]))
    assert result.keep == [True, False]


def test_out_of_range_index_is_ignored(monkeypatch):
    _patch_shim(monkeypatch, verdicts=[
        {"index": 99, "still_open": False, "merges_into": None, "reason": "?"},
    ])
    result = _run(claude.verify_still_open("окно", [{"task": "A"}]))
    assert result.keep == [True]


def test_shim_error_response_is_fail_safe_keeps_all(monkeypatch):
    _patch_shim(monkeypatch, ok_payload={"ok": False, "error": "boom"})
    result = _run(claude.verify_still_open("окно", [{"task": "A"}, {"task": "B"}]))
    assert result.keep == [True, True]
    assert result.merge_into == [None, None]


def test_shim_exception_is_fail_safe_keeps_all(monkeypatch):
    async def fake_post_raises(self, url, json=None, headers=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post_raises)
    monkeypatch.setattr(claude, "get_settings", lambda: FakeSettings())

    result = _run(claude.verify_still_open("окно", [{"task": "A"}]))
    assert result.keep == [True]
    assert result.merge_into == [None]


def test_merges_into_recorded_for_the_earlier_candidate(monkeypatch):
    _patch_shim(monkeypatch, verdicts=[
        {"index": 0, "still_open": True, "merges_into": 1,
         "reason": "то же самое, индекс 1 полнее"},
        {"index": 1, "still_open": True, "merges_into": None, "reason": None},
    ])
    result = _run(claude.verify_still_open("окно", [
        {"task": "Надо позвонить"},
        {"task": "Позвонить Наде по поводу отчёта до пятницы"},
    ]))
    assert result.keep == [True, True]  # merging isn't dropping — caller folds it
    assert result.merge_into == [1, None]


def test_merges_into_self_is_ignored(monkeypatch):
    _patch_shim(monkeypatch, verdicts=[
        {"index": 0, "still_open": True, "merges_into": 0, "reason": "?"},
    ])
    result = _run(claude.verify_still_open("окно", [{"task": "A"}]))
    assert result.merge_into == [None]


def test_still_open_false_wins_over_merges_into_on_the_same_candidate(monkeypatch):
    # A candidate can't be BOTH "resolved" and "merge into another" — if the
    # model emits both, resolved-later wins and merges_into is never inspected.
    _patch_shim(monkeypatch, verdicts=[
        {"index": 0, "still_open": False, "merges_into": 1, "reason": "противоречиво"},
        {"index": 1, "still_open": True, "merges_into": None, "reason": None},
    ])
    result = _run(claude.verify_still_open("окно", [{"task": "A"}, {"task": "B"}]))
    assert result.keep == [False, True]
    assert result.merge_into == [None, None]

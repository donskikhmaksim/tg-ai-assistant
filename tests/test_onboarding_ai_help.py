"""Tests for the onboarding "Ask AI" route (app/web/server.py::api_onboarding_ask
and friends). This is the one Mini App route NOT gated by owner auth, so the
tests focus on its own guards: the ONBOARDING_AI_HELP_ENABLED kill switch, the
message-length cap, and the per-session/IP rate limit. The Claude call itself
(app/onboarding/ai_help.answer) is mocked throughout — this is about the HTTP
layer, not answer quality."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from aiohttp.base_protocol import BaseProtocol
from aiohttp.streams import StreamReader
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from app.web import server as server_mod


def _settings(**overrides):
    base = dict(
        onboarding_ai_help_enabled=True,
        onboarding_ai_model="haiku",
        onboarding_ai_max_message_chars=500,
        onboarding_ai_rate_limit_per_hour=20,
        onboarding_ai_global_hourly_cap=100,
        onboarding_railway_template_url="",
        onboarding_repo_url="",
        onboarding_assistant_setup_url="",
        onboarding_n8n_setup_url="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(coro):
    return asyncio.run(coro)


async def _ask(client, question=None, history=None, headers=None, raw_body=None):
    payload = raw_body if raw_body is not None else {"question": question}
    if raw_body is None and history is not None:
        payload["history"] = history
    return await client.post(
        "/api/onboarding/ask", json=payload, headers=headers or {}
    )


def _client(monkeypatch, settings, fake_answer=None):
    monkeypatch.setattr(server_mod, "get_settings", lambda: settings)
    if fake_answer is not None:
        monkeypatch.setattr(server_mod.ai_help, "answer", fake_answer)
    server_mod._onboarding_rate_state.clear()
    server_mod._onboarding_global_rate_state.clear()
    app = server_mod.build_app(bot=SimpleNamespace())
    return TestClient(TestServer(app))


def _mock_request(method="POST", path="/api/onboarding/ask", *, headers=None, json_body=None, remote="203.0.113.1"):
    """Build a real aiohttp Request (via make_mocked_request) with a JSON body
    readable through request.json(), then override `.remote` the same way
    aiohttp itself supports for a trusted proxy: `Request.clone(remote=...)`.
    This lets tests simulate distinct underlying TCP peers — something a
    live TestClient can't do, since every request from one TestClient shares
    the same loopback remote."""
    loop = asyncio.get_event_loop()
    protocol = BaseProtocol(loop=loop)
    payload = StreamReader(protocol, limit=2**16, loop=loop)
    body_bytes = json.dumps(json_body if json_body is not None else {}).encode()
    payload.feed_data(body_bytes)
    payload.feed_eof()
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    request = make_mocked_request(method, path, headers=req_headers, payload=payload)
    return request.clone(remote=remote)


# ── kill switch ──────────────────────────────────────────────────────────────

def test_disabled_flag_returns_404(monkeypatch):
    async def go():
        async with _client(monkeypatch, _settings(onboarding_ai_help_enabled=False)) as client:
            resp = await _ask(client, "как подключить TickTick?")
            assert resp.status == 404
            body = await resp.json()
            assert body["error"] == "disabled"

    _run(go())


def test_config_endpoint_reflects_disabled_flag(monkeypatch):
    async def go():
        async with _client(monkeypatch, _settings(onboarding_ai_help_enabled=False)) as client:
            resp = await client.get("/api/onboarding/config")
            assert resp.status == 200
            body = await resp.json()
            assert body["aiHelpEnabled"] is False

    _run(go())


def test_config_endpoint_reflects_enabled_flag(monkeypatch):
    async def go():
        async with _client(monkeypatch, _settings(onboarding_ai_help_enabled=True)) as client:
            resp = await client.get("/api/onboarding/config")
            body = await resp.json()
            assert body["aiHelpEnabled"] is True
            assert body["maxMessageChars"] == 500

    _run(go())


# ── message-length cap ───────────────────────────────────────────────────────

def test_question_too_long_rejected(monkeypatch):
    async def go():
        settings = _settings(onboarding_ai_max_message_chars=50)
        async with _client(monkeypatch, settings) as client:
            resp = await _ask(client, "x" * 51)
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "question_too_long"

    _run(go())


def test_question_at_cap_is_accepted(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_max_message_chars=50)
        async with _client(monkeypatch, settings, fake_answer) as client:
            resp = await _ask(client, "x" * 50)
            assert resp.status == 200

    _run(go())


def test_empty_question_rejected(monkeypatch):
    async def go():
        async with _client(monkeypatch, _settings()) as client:
            resp = await _ask(client, "   ")
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "question required"

    _run(go())


def test_bad_json_rejected(monkeypatch):
    async def go():
        app = server_mod.build_app(bot=SimpleNamespace())
        monkeypatch.setattr(server_mod, "get_settings", lambda: _settings())
        server_mod._onboarding_rate_state.clear()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/onboarding/ask",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    _run(go())


def test_oversized_history_turns_are_truncated_not_rejected(monkeypatch):
    """History turns longer than the cap are truncated (silently), not a hard
    400 — only the live `question` field is a hard reject."""
    seen = {}

    async def fake_answer(question, history=None, model=None):
        seen["history"] = history
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_max_message_chars=20)
        async with _client(monkeypatch, settings, fake_answer) as client:
            resp = await _ask(
                client,
                "short q",
                history=[{"role": "user", "text": "y" * 100}],
            )
            assert resp.status == 200

    _run(go())
    assert len(seen["history"][0]["text"]) == 20


# ── successful call ──────────────────────────────────────────────────────────

def test_successful_ask_returns_answer(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        assert question == "как подключить TickTick?"
        assert model == "haiku"
        return "Смотри DEPLOY.md, раздел про /connect."

    async def go():
        async with _client(monkeypatch, _settings(), fake_answer) as client:
            resp = await _ask(client, "как подключить TickTick?")
            assert resp.status == 200
            body = await resp.json()
            assert "DEPLOY.md" in body["answer"]

    _run(go())


def test_answer_none_degrades_to_fallback_message(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        return None

    async def go():
        async with _client(monkeypatch, _settings(), fake_answer) as client:
            resp = await _ask(client, "почему у меня ошибка X?")
            assert resp.status == 200
            body = await resp.json()
            assert body["answer"]  # non-empty fallback text, not a crash

    _run(go())


def test_answer_exception_degrades_to_fallback_message(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        raise RuntimeError("boom")

    async def go():
        async with _client(monkeypatch, _settings(), fake_answer) as client:
            resp = await _ask(client, "почему у меня ошибка X?")
            assert resp.status == 200
            body = await resp.json()
            assert body["answer"]

    _run(go())


# ── rate limiting ────────────────────────────────────────────────────────────

def test_rate_limit_blocks_after_cap(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=2)
        async with _client(monkeypatch, settings, fake_answer) as client:
            headers = {"X-Onboarding-Session": "sess-1"}
            r1 = await _ask(client, "q1", headers=headers)
            r2 = await _ask(client, "q2", headers=headers)
            r3 = await _ask(client, "q3", headers=headers)
            assert r1.status == 200
            assert r2.status == 200
            assert r3.status == 429
            body = await r3.json()
            assert body["error"] == "rate_limited"

    _run(go())


def test_rotated_session_and_xff_headers_no_longer_bypass_the_cap(monkeypatch):
    """Regression test for the CONFIRMED bypass an adversarial review found:
    the limiter used to key on the client-supplied X-Onboarding-Session
    header (falling back to a client-controllable X-Forwarded-For first
    hop), so an attacker sending a fresh random value on every request made
    each request look like a brand-new caller — PoC was 50 requests with 50
    random session ids, 0 rate-limited.

    A real aiohttp TestClient always connects from the same loopback address
    for every request it sends, so the three calls below share one real
    `request.remote` — exactly the "many spoofed identities, one underlying
    connection" shape of the PoC. Now that the limiter keys on
    `request.remote` instead of these headers, they must be counted together
    and hit the cap, no matter how many distinct session ids / XFF values are
    sent."""
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=2)
        async with _client(monkeypatch, settings, fake_answer) as client:
            r1 = await _ask(
                client,
                "q1",
                headers={"X-Onboarding-Session": "aaaaaaaa", "X-Forwarded-For": "1.1.1.1"},
            )
            r2 = await _ask(
                client,
                "q2",
                headers={"X-Onboarding-Session": "bbbbbbbb", "X-Forwarded-For": "2.2.2.2"},
            )
            r3 = await _ask(
                client,
                "q3",
                headers={"X-Onboarding-Session": "cccccccc", "X-Forwarded-For": "3.3.3.3"},
            )
            assert r1.status == 200
            assert r2.status == 200
            assert r3.status == 429  # capped after 2, despite 3 distinct spoofed identities
            body = await r3.json()
            assert body["error"] == "rate_limited"

    _run(go())


def test_rate_limit_keys_on_real_remote_ip_not_session_header(monkeypatch):
    """Same scenario as above, but exercised at the handler level with
    directly constructed requests (`request.clone(remote=...)`), so it also
    documents the exact mechanism: two requests with different
    X-Onboarding-Session / X-Forwarded-For headers but the SAME underlying
    `request.remote` are rate-limited together."""
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=1)
        monkeypatch.setattr(server_mod, "get_settings", lambda: settings)
        monkeypatch.setattr(server_mod.ai_help, "answer", fake_answer)
        server_mod._onboarding_rate_state.clear()
        server_mod._onboarding_global_rate_state.clear()

        req_a = _mock_request(
            json_body={"question": "q1"},
            headers={"X-Onboarding-Session": "session-a", "X-Forwarded-For": "9.9.9.9"},
            remote="198.51.100.7",
        )
        req_b = _mock_request(
            json_body={"question": "q2"},
            headers={"X-Onboarding-Session": "totally-different-session", "X-Forwarded-For": "8.8.8.8"},
            remote="198.51.100.7",  # same real peer as req_a
        )
        resp_a = await server_mod.api_onboarding_ask(req_a)
        resp_b = await server_mod.api_onboarding_ask(req_b)
        assert resp_a.status == 200
        assert resp_b.status == 429
        assert json.loads(resp_b.text)["error"] == "rate_limited"

    _run(go())


def test_rate_limit_isolates_distinct_real_remotes(monkeypatch):
    """Sanity check that keying on request.remote still isolates genuinely
    distinct callers (unlike a shared header, a distinct real peer address
    is a distinct caller)."""
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=1)
        monkeypatch.setattr(server_mod, "get_settings", lambda: settings)
        monkeypatch.setattr(server_mod.ai_help, "answer", fake_answer)
        server_mod._onboarding_rate_state.clear()
        server_mod._onboarding_global_rate_state.clear()

        req_a1 = _mock_request(json_body={"question": "q"}, remote="203.0.113.10")
        req_b1 = _mock_request(json_body={"question": "q"}, remote="203.0.113.20")
        req_a2 = _mock_request(json_body={"question": "q again"}, remote="203.0.113.10")
        assert (await server_mod.api_onboarding_ask(req_a1)).status == 200
        assert (await server_mod.api_onboarding_ask(req_b1)).status == 200
        assert (await server_mod.api_onboarding_ask(req_a2)).status == 429

    _run(go())


def test_rate_limit_uses_remote_ip_with_no_headers_sent(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=1)
        async with _client(monkeypatch, settings, fake_answer) as client:
            r1 = await _ask(client, "q1")
            r2 = await _ask(client, "q2")
            assert r1.status == 200
            assert r2.status == 429  # same test client -> same remote IP

    _run(go())


# ── global aggregate cap (Part 2) ────────────────────────────────────────────

def test_global_cap_blocks_regardless_of_per_ip_state(monkeypatch):
    """The aggregate cap must trip once hit, independent of per-IP state —
    protects the owner's spend even if per-IP keying were defeated by many
    distinct real callers (a botnet). Uses several distinct simulated
    `request.remote` values, each nowhere near its own (generous) per-IP cap,
    to prove the block comes from the global counter, not a per-IP one."""
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=1000, onboarding_ai_global_hourly_cap=2)
        monkeypatch.setattr(server_mod, "get_settings", lambda: settings)
        monkeypatch.setattr(server_mod.ai_help, "answer", fake_answer)
        server_mod._onboarding_rate_state.clear()
        server_mod._onboarding_global_rate_state.clear()

        remotes = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
        statuses = []
        bodies = []
        for i, remote in enumerate(remotes):
            req = _mock_request(json_body={"question": f"q{i}"}, remote=remote)
            resp = await server_mod.api_onboarding_ask(req)
            statuses.append(resp.status)
            bodies.append(json.loads(resp.text))

        assert statuses[:2] == [200, 200]
        assert statuses[2] == 429
        assert statuses[3] == 429
        # It's the GLOBAL cap, not a per-IP one — every remote here is brand
        # new and nowhere near its own 1000/hour allowance.
        assert bodies[2]["error"] == "global_limit_reached"
        assert bodies[3]["error"] == "global_limit_reached"

    _run(go())


def test_global_cap_is_independent_config_from_per_ip_cap(monkeypatch):
    """A single caller hitting its OWN per-IP cap gets `rate_limited`, not
    `global_limit_reached` — the two caps are distinct signals."""
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=1, onboarding_ai_global_hourly_cap=1000)
        monkeypatch.setattr(server_mod, "get_settings", lambda: settings)
        monkeypatch.setattr(server_mod.ai_help, "answer", fake_answer)
        server_mod._onboarding_rate_state.clear()
        server_mod._onboarding_global_rate_state.clear()

        req1 = _mock_request(json_body={"question": "q1"}, remote="192.0.2.50")
        req2 = _mock_request(json_body={"question": "q2"}, remote="192.0.2.50")
        assert (await server_mod.api_onboarding_ask(req1)).status == 200
        resp2 = await server_mod.api_onboarding_ask(req2)
        assert resp2.status == 429
        assert json.loads(resp2.text)["error"] == "rate_limited"

    _run(go())


# ── bounded memory (Part 3) ──────────────────────────────────────────────────

def test_rate_limit_state_dict_is_bounded_by_max_keys():
    """An attacker rotating the keying signal (now request.remote — real
    distinct source addresses, far costlier than the old free header, but
    still worth bounding) can't grow `_onboarding_rate_state` without limit:
    a hard max-key backstop evicts the least-recently-used entries."""
    server_mod._onboarding_rate_state.clear()
    total_keys = server_mod._ONBOARDING_RATE_STATE_MAX_KEYS + 500
    for i in range(total_keys):
        server_mod._onboarding_rate_limited(f"ip:203.0.113.{i}", limit=20)
    assert len(server_mod._onboarding_rate_state) <= server_mod._ONBOARDING_RATE_STATE_MAX_KEYS


def test_stale_rate_limit_keys_are_evicted_after_window_expires(monkeypatch):
    """Once a key's whole bucket has aged out of the 1-hour window, it should
    be dropped entirely (not just have its timestamps cleared and linger as
    an empty deque forever) — otherwise a slow rotation attack still grows
    the dict unboundedly over time even though each individual key stops
    counting toward anyone's rate limit."""
    server_mod._onboarding_rate_state.clear()
    fake_now = [1_000_000.0]
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: fake_now[0])

    server_mod._onboarding_rate_limited("ip:198.51.100.99", limit=20)
    assert "ip:198.51.100.99" in server_mod._onboarding_rate_state

    fake_now[0] += server_mod._ONBOARDING_RATE_WINDOW_SECONDS + 1
    # Any subsequent call (even for an unrelated key) sweeps stale entries.
    server_mod._onboarding_rate_limited("ip:203.0.113.1", limit=20)

    assert "ip:198.51.100.99" not in server_mod._onboarding_rate_state

"""Tests for the onboarding "Ask AI" route (app/web/server.py::api_onboarding_ask
and friends). This is the one Mini App route NOT gated by owner auth, so the
tests focus on its own guards: the ONBOARDING_AI_HELP_ENABLED kill switch, the
message-length cap, and the per-session/IP rate limit. The Claude call itself
(app/onboarding/ai_help.answer) is mocked throughout — this is about the HTTP
layer, not answer quality."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from app.web import server as server_mod


def _settings(**overrides):
    base = dict(
        onboarding_ai_help_enabled=True,
        onboarding_ai_model="haiku",
        onboarding_ai_max_message_chars=500,
        onboarding_ai_rate_limit_per_hour=20,
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
    app = server_mod.build_app(bot=SimpleNamespace())
    return TestClient(TestServer(app))


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


def test_rate_limit_is_isolated_per_session(monkeypatch):
    async def fake_answer(question, history=None, model=None):
        return "ok"

    async def go():
        settings = _settings(onboarding_ai_rate_limit_per_hour=1)
        async with _client(monkeypatch, settings, fake_answer) as client:
            a1 = await _ask(client, "q", headers={"X-Onboarding-Session": "a"})
            b1 = await _ask(client, "q", headers={"X-Onboarding-Session": "b"})
            a2 = await _ask(client, "q again", headers={"X-Onboarding-Session": "a"})
            assert a1.status == 200
            assert b1.status == 200
            assert a2.status == 429  # "a" already used its one slot

    _run(go())


def test_rate_limit_falls_back_to_ip_without_session_header(monkeypatch):
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

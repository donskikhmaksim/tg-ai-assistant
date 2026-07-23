"""Tests for the manifest-policy admin plane (Phase 1 — storage + Mini App
API only, see app/policy/ and app/web/server.py's policy routes).

Covers:
  - repositories.get_policy/save_policy CRUD against a tiny in-memory fake
    Mongo (repo convention: pure-logic + a fake collection, no real Mongo —
    see tests/test_audit_log.py).
  - the HTTP layer (GET/POST /api/policy) end to end against that same fake,
    via aiohttp's TestClient/TestServer (pattern from
    tests/test_onboarding_ai_help.py).
  - owner-auth gating: no initData, tampered initData, and a non-owner user
    are all rejected; only the bootstrapped owner succeeds.
  - the machine pull endpoint (GET /policy), bearer-token gated + ETag/304.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import urllib.parse
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

import app.repositories as repo
from app.web import server as server_mod

TOKEN = "123456:TEST-BOT-TOKEN"
OWNER_ID = 555
OTHER_ID = 999


def _run(coro):
    return asyncio.run(coro)


def _sign(fields: dict, token: str) -> str:
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": h})


def _init_data(uid: int, token: str = TOKEN) -> str:
    user = {"id": uid, "first_name": "T"}
    return _sign({"auth_date": "1700000000", "user": json.dumps(user)}, token)


# ─────────────────────────────────────────────────────────────────────────────
# Tiny in-memory fake for the `policy` collection (repo convention — see
# tests/test_audit_log.py's FakeCollection).
# ─────────────────────────────────────────────────────────────────────────────

class FakePolicyCollection:
    def __init__(self):
        self.docs: dict = {}

    async def find_one(self, flt, projection=None):
        _id = flt.get("_id")
        doc = self.docs.get(_id)
        if doc is None:
            return None
        if projection:
            return {k: v for k, v in doc.items() if k in projection or k == "_id"}
        return dict(doc)

    async def replace_one(self, flt, doc, upsert=False):
        _id = flt.get("_id")
        self.docs[_id] = dict(doc)
        return SimpleNamespace(matched_count=1, upserted_id=None)


class FakeDB:
    def __init__(self):
        self.policy = FakePolicyCollection()


def _use_fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(repo, "get_db", lambda: db)
    return db


def _settings(**overrides):
    base = dict(bot_token=TOKEN, policy_pull_token="pull-secret-123")
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(monkeypatch, settings=None, owner=OWNER_ID):
    monkeypatch.setattr(server_mod, "get_settings", lambda: settings or _settings())

    async def fake_get_bot_state(key):
        assert key == server_mod.OWNER_ID_KEY
        return owner

    monkeypatch.setattr(repo, "get_bot_state", fake_get_bot_state)
    app = server_mod.build_app(bot=SimpleNamespace())
    return TestClient(TestServer(app))


# ── repositories layer: CRUD ────────────────────────────────────────────────

def test_get_policy_default_when_never_saved(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        doc = await repo.get_policy()
        assert doc["version"] == 0
        assert doc["defaults"] == {}
        assert doc["tools"] == {}

    _run(go())


def test_save_policy_bumps_version_and_persists(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        saved = await repo.save_policy({"mutating": "off"}, {"ticktick-mcp.update_tasks": "hard_manifest"}, 42)
        assert saved["version"] == 1
        assert saved["updated_by"] == 42

        again = await repo.get_policy()
        assert again["version"] == 1
        assert again["defaults"] == {"mutating": "off"}
        assert again["tools"] == {"ticktick-mcp.update_tasks": "hard_manifest"}

    _run(go())


def test_save_policy_increments_version_on_each_save(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        first = await repo.save_policy({}, {}, 1)
        second = await repo.save_policy({}, {"ticktick-mcp.move_tasks": "off"}, 1)
        assert second["version"] == first["version"] + 1

    _run(go())


# ── HTTP layer: GET/POST /api/policy round-trip ─────────────────────────────

def test_get_policy_returns_catalog_with_recommended_tiers(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.get("/api/policy", headers={"X-Telegram-Init-Data": _init_data(OWNER_ID)})
            assert resp.status == 200
            body = await resp.json()
            assert body["version"] == 0
            tool = body["tools"]["ticktick-mcp.create_tasks"]
            assert tool["resolved"] == "hard_manifest"
            assert tool["override"] is None

    _run(go())


def test_post_override_then_get_reflects_it(monkeypatch):
    """The exact CRUD flow asked for: get default, post an override, get
    reflects the override."""
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"X-Telegram-Init-Data": _init_data(OWNER_ID)}

            before = await (await client.get("/api/policy", headers=headers)).json()
            assert before["tools"]["ticktick-mcp.update_tasks"]["resolved"] == "soft_guard"
            assert before["tools"]["ticktick-mcp.update_tasks"]["override"] is None

            post_resp = await client.post(
                "/api/policy",
                json={"tools": {"ticktick-mcp.update_tasks": "off"}},
                headers=headers,
            )
            assert post_resp.status == 200
            post_body = await post_resp.json()
            assert post_body["ok"] is True
            assert post_body["version"] == 1

            after = await (await client.get("/api/policy", headers=headers)).json()
            assert after["version"] == 1
            assert after["tools"]["ticktick-mcp.update_tasks"]["override"] == "off"
            assert after["tools"]["ticktick-mcp.update_tasks"]["resolved"] == "off"
            # An untouched tool keeps its catalog-recommended tier.
            assert after["tools"]["ticktick-mcp.create_tasks"]["resolved"] == "hard_manifest"

    _run(go())


def test_post_defaults_class_override_reflected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"X-Telegram-Init-Data": _init_data(OWNER_ID)}
            resp = await client.post("/api/policy", json={"defaults": {"mutating": "off"}}, headers=headers)
            assert resp.status == 200

            after = await (await client.get("/api/policy", headers=headers)).json()
            assert after["defaults"]["mutating"] == "off"
            # A mutating-class tool with no explicit tool-level override now
            # resolves via the new class default.
            assert after["tools"]["ticktick-mcp.update_tasks"]["resolved"] == "off"

    _run(go())


def test_post_clearing_override_reverts_to_catalog_default(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"X-Telegram-Init-Data": _init_data(OWNER_ID)}
            await client.post("/api/policy", json={"tools": {"ticktick-mcp.update_tasks": "off"}}, headers=headers)
            resp = await client.post("/api/policy", json={"tools": {"ticktick-mcp.update_tasks": None}}, headers=headers)
            assert resp.status == 200

            after = await (await client.get("/api/policy", headers=headers)).json()
            assert after["tools"]["ticktick-mcp.update_tasks"]["override"] is None
            assert after["tools"]["ticktick-mcp.update_tasks"]["resolved"] == "soft_guard"

    _run(go())


def test_post_unknown_tool_rejected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"X-Telegram-Init-Data": _init_data(OWNER_ID)}
            resp = await client.post(
                "/api/policy", json={"tools": {"nope.does_not_exist": "off"}}, headers=headers
            )
            assert resp.status == 400
            body = await resp.json()
            assert "unknown tool" in body["error"]

    _run(go())


def test_post_invalid_tier_rejected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"X-Telegram-Init-Data": _init_data(OWNER_ID)}
            resp = await client.post(
                "/api/policy",
                json={"tools": {"ticktick-mcp.update_tasks": "delete_everything"}},
                headers=headers,
            )
            assert resp.status == 400
            body = await resp.json()
            assert "unknown tier" in body["error"]

    _run(go())


def test_post_invalid_class_rejected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"X-Telegram-Init-Data": _init_data(OWNER_ID)}
            resp = await client.post(
                "/api/policy", json={"defaults": {"not_a_class": "off"}}, headers=headers
            )
            assert resp.status == 400

    _run(go())


# ── owner-auth gating ────────────────────────────────────────────────────────

def test_get_policy_without_init_data_rejected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.get("/api/policy")
            assert resp.status == 401

    _run(go())


def test_get_policy_tampered_init_data_rejected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            tampered = _init_data(OWNER_ID).replace(str(OWNER_ID), str(OTHER_ID))
            resp = await client.get("/api/policy", headers={"X-Telegram-Init-Data": tampered})
            assert resp.status == 401

    _run(go())


def test_get_policy_non_owner_rejected(monkeypatch):
    """A validly-SIGNED initData for a user who isn't the (already known)
    owner is forbidden, not merely unauthenticated."""
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch, owner=OWNER_ID) as client:
            resp = await client.get("/api/policy", headers={"X-Telegram-Init-Data": _init_data(OTHER_ID)})
            assert resp.status == 403

    _run(go())


def test_post_policy_non_owner_rejected(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch, owner=OWNER_ID) as client:
            resp = await client.post(
                "/api/policy",
                json={"tools": {"ticktick-mcp.update_tasks": "off"}},
                headers={"X-Telegram-Init-Data": _init_data(OTHER_ID)},
            )
            assert resp.status == 403

    _run(go())


def test_fresh_bot_bootstraps_any_signed_user_as_owner(monkeypatch):
    """Before any owner is known (owner_id unset), any validly-signed user may
    bootstrap — matches every other Mini App route's `_is_owner` behavior."""
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch, owner=None) as client:
            resp = await client.get("/api/policy", headers={"X-Telegram-Init-Data": _init_data(OTHER_ID)})
            assert resp.status == 200

    _run(go())


# ── machine pull endpoint (GET /policy) ─────────────────────────────────────

def test_policy_pull_requires_bearer_token(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.get("/policy")
            assert resp.status == 401

    _run(go())


def test_policy_pull_rejects_wrong_token(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.get("/policy", headers={"Authorization": "Bearer wrong"})
            assert resp.status == 401

    _run(go())


def test_policy_pull_disabled_when_token_unset(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch, settings=_settings(policy_pull_token="")) as client:
            resp = await client.get("/policy", headers={"Authorization": "Bearer anything"})
            assert resp.status == 401

    _run(go())


def test_policy_pull_returns_current_policy_with_etag(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            resp = await client.get("/policy", headers={"Authorization": "Bearer pull-secret-123"})
            assert resp.status == 200
            assert resp.headers["ETag"] == '"v0"'
            body = await resp.json()
            assert body["version"] == 0
            assert "ticktick-mcp.create_tasks" in body["catalog"]

    _run(go())


def test_policy_pull_304_when_etag_matches(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        async with _client(monkeypatch) as client:
            headers = {"Authorization": "Bearer pull-secret-123"}
            first = await client.get("/policy", headers=headers)
            etag = first.headers["ETag"]
            second = await client.get("/policy", headers={**headers, "If-None-Match": etag})
            assert second.status == 304

    _run(go())


def test_policy_pull_reflects_saved_overrides(monkeypatch):
    _use_fake_db(monkeypatch)

    async def go():
        await repo.save_policy({}, {"ticktick-mcp.update_tasks": "off"}, OWNER_ID)
        async with _client(monkeypatch) as client:
            resp = await client.get("/policy", headers={"Authorization": "Bearer pull-secret-123"})
            body = await resp.json()
            assert body["version"] == 1
            assert body["tools"]["ticktick-mcp.update_tasks"] == "off"

    _run(go())

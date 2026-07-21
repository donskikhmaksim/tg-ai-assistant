"""Settings-cabinet additions: per-chat/global extraction model + effort, an
editable base prompt override, and optional tier-1 (Qwen) endpoint."""
import asyncio

from app.config import get_settings
from app.llm import claude, qwen


# ── model alias resolution (API path) ───────────────────────────────────────

def test_model_alias_maps_to_api_id():
    assert claude.resolve_api_model("opus") == "claude-opus-4-8"
    assert claude.resolve_api_model("sonnet") == "claude-sonnet-5"
    assert claude.resolve_api_model("haiku") == "claude-haiku-4-5"


def test_model_alias_is_case_and_space_insensitive():
    assert claude.resolve_api_model("  Opus ") == "claude-opus-4-8"
    assert claude.resolve_api_model("SONNET") == "claude-sonnet-5"


def test_empty_or_unknown_model_falls_back_to_configured_default():
    default = get_settings().anthropic_model
    assert claude.resolve_api_model("") == default
    assert claude.resolve_api_model(None) == default
    assert claude.resolve_api_model("gpt-4") == default  # unknown → default


# ── base prompt compose (override vs default) ───────────────────────────────

def test_build_system_uses_default_when_no_override():
    assert claude._build_system().startswith(claude.SYSTEM_PROMPT)


def test_build_system_override_replaces_guidance_body():
    sys = claude._build_system(base_prompt="ТОЛЬКО деньги считай задачей")
    assert "ТОЛЬКО деньги считай задачей" in sys
    # The built-in body is swapped out, not appended alongside.
    assert claude.SYSTEM_PROMPT not in sys


def test_build_system_blank_override_falls_back_to_default():
    assert claude._build_system(base_prompt="   ").startswith(claude.SYSTEM_PROMPT)


def test_build_system_override_still_carries_context_and_rules():
    sys = claude._build_system(
        chat_context="ctx", extract_rules="rule", base_prompt="БАЗА"
    )
    assert "БАЗА" in sys and "ctx" in sys and "rule" in sys


# ── extraction result shape guard ───────────────────────────────────────────

def test_valid_result_passes_guard():
    good = {"new_tasks": [{"task": "позвонить"}], "status_updates": []}
    assert claude._is_valid_result(good) is True


def test_empty_lists_are_valid():
    assert claude._is_valid_result({"new_tasks": [], "status_updates": []}) is True


def test_wrong_shapes_fail_guard():
    assert claude._is_valid_result("not a dict") is False
    assert claude._is_valid_result({"new_tasks": "x", "status_updates": []}) is False
    assert claude._is_valid_result({"new_tasks": [], "status_updates": {}}) is False
    # a "task" that isn't a string → garbage from a broken custom prompt
    assert claude._is_valid_result({"new_tasks": [{"task": 5}], "status_updates": []}) is False


# ── Qwen tier-1: skip entirely when no endpoint ─────────────────────────────

def test_resolve_base_url_empty_is_disabled():
    assert qwen._resolve_base_url("") == ""
    assert qwen._resolve_base_url("http://host:11434/v1") == "http://host:11434/v1"


def test_has_task_fails_open_without_network_when_disabled():
    # Empty endpoint → tier-1 off → True WITHOUT building a client or hitting a
    # network (no client is ever cached).
    qwen._clients.clear()
    result = asyncio.run(qwen.has_task("любой текст", base_url=""))
    assert result is True
    assert qwen._clients == {}  # no client constructed → no localhost call


def test_healthcheck_skips_when_disabled():
    ok, detail = asyncio.run(qwen.healthcheck(base_url=""))
    assert ok is True and detail == ""

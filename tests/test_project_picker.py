"""Inline project/section create (mcp_client wrappers + id recovery) and the
per-user global default project/section preference in the batch resolver."""
import asyncio

import app.pipeline.batch as batch
from app.ticktick.mcp_client import TickTickMCP, _any_id


# ── id recovery from either echoed shape ────────────────────────────────────

def test_any_id_handles_both_shapes():
    assert _any_id("Created project\nID: 655cdfeb2c49d17e8d021f50") == "655cdfeb2c49d17e8d021f50"
    assert _any_id("Project created (id: 699d03848f0853b739baf1ca)") == "699d03848f0853b739baf1ca"
    assert _any_id("no id here") is None


# ── create_project ──────────────────────────────────────────────────────────

def test_create_project_recovers_echoed_id():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        assert name == "create_project" and args == {"name": "Fix&Roll"}
        return "Project 'Fix&Roll' created\n(id: 69f841179f1911020b96a62b)"

    tt.call = fake_call  # type: ignore[assignment]
    assert asyncio.run(tt.create_project("Fix&Roll")) == "69f841179f1911020b96a62b"


def test_create_project_falls_back_to_lookup_by_name():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        if name == "create_project":
            return "OK, done."  # no id echoed
        if name == "get_projects":
            return "Name: New One\nID: pid_new"
        raise AssertionError(name)

    tt.call = fake_call  # type: ignore[assignment]
    assert asyncio.run(tt.create_project("New One")) == "pid_new"


# ── create_project_column ───────────────────────────────────────────────────

def test_create_section_recovers_echoed_id():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        assert name == "create_project_column"
        assert args == {"project_id": "pid1", "name": "Tg"}
        return "Column created\nID: col_tg"

    tt.call = fake_call  # type: ignore[assignment]
    assert asyncio.run(tt.create_project_column("pid1", "Tg")) == "col_tg"


def test_create_section_falls_back_to_column_lookup():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        if name == "create_project_column":
            return "created"  # no id
        if name == "list_project_columns":
            return "- Tg  (id: col_tg)"
        raise AssertionError(name)

    tt.call = fake_call  # type: ignore[assignment]
    assert asyncio.run(tt.create_project_column("pid1", "Tg")) == "col_tg"


# ── global default project/section preference in _resolve_project ───────────

def _patch_repo(monkeypatch, *, binding=None, global_doc=None):
    async def fake_binding(_chat_id):
        return binding

    async def fake_global():
        return global_doc or {}

    monkeypatch.setattr(batch.repo, "get_project_binding", fake_binding)
    monkeypatch.setattr(batch.repo, "get_global_settings", fake_global)


def test_binding_wins_over_global_default(monkeypatch):
    _patch_repo(
        monkeypatch,
        binding={"ticktickProjectId": "bound", "projectName": "Bound", "ticktickSectionId": "sec"},
        global_doc={"default_project_id": "glob"},
    )
    pid, name, sec = asyncio.run(batch._resolve_project("user_1", None))
    assert (pid, name, sec) == ("bound", "Bound", "sec")


def test_global_default_project_and_section_preferred(monkeypatch):
    _patch_repo(
        monkeypatch,
        binding=None,
        global_doc={"default_project_id": "glob_pid", "default_section_id": "glob_sec"},
    )
    pid, _name, sec = asyncio.run(batch._resolve_project("user_1", None))
    assert pid == "glob_pid"
    assert sec == "glob_sec"  # global default section wins without any tt call


def test_no_binding_no_global_falls_through_to_env(monkeypatch):
    # Empty global + no connector (tt=None) + default_project by name can't be
    # resolved → stays local (None project id).
    _patch_repo(monkeypatch, binding=None, global_doc={})
    pid, _name, sec = asyncio.run(batch._resolve_project("user_1", None))
    assert pid is None and sec is None

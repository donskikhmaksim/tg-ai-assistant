"""Section-routing decision logic: which column a task category goes to, and
the invariant that real (open) tasks always fly while others are optional."""
from app.pipeline.batch import _section_for


def test_disabled_map_routes_nothing():
    smap = {"enabled": False, "open": {"id": "c1"}, "done": {"id": "c2"}}
    assert _section_for(smap, "open") is None
    assert _section_for(smap, "done") is None


def test_enabled_returns_configured_section():
    smap = {
        "enabled": True,
        "open": {"id": "col_open", "name": "Реальные"},
        "done": {"id": "col_done", "name": "Выполнено"},
        "cancelled": None,
        "rejected": {"id": "col_rej", "name": "На проверку"},
    }
    assert _section_for(smap, "open") == "col_open"
    assert _section_for(smap, "done") == "col_done"
    assert _section_for(smap, "cancelled") is None   # unset -> nowhere
    assert _section_for(smap, "rejected") == "col_rej"


def test_unset_category_is_none():
    smap = {"enabled": True, "open": {"id": "c1"}}
    assert _section_for(smap, "done") is None
    assert _section_for(smap, "cancelled") is None


def test_empty_map_safe():
    assert _section_for({}, "open") is None
    assert _section_for({"enabled": True}, "done") is None

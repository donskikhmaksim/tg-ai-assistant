"""Multi-project routing: settings sanitising, per-task route resolution, and
the ROUTING prompt block."""
from app.llm.claude import _build_user_prompt
from app.pipeline.batch import _route_for, _valid_routes


# ── _valid_routes ─────────────────────────────────────────────────────────
def test_valid_routes_happy_path():
    raw = [
        {"label": "работа", "hint": "Fix&Roll, клиенты", "project_id": "p1", "section_id": "s1"},
        {"label": "личное", "project_id": "p2"},
    ]
    out = _valid_routes(raw)
    assert out == [
        {"label": "работа", "hint": "Fix&Roll, клиенты", "project_id": "p1", "section_id": "s1"},
        {"label": "личное", "hint": None, "project_id": "p2", "section_id": None},
    ]


def test_valid_routes_drops_malformed():
    raw = [
        {"label": "", "project_id": "p1"},          # no label
        {"label": "x", "project_id": ""},           # no destination
        "not-a-dict",
        {"label": "ok", "project_id": "p3"},
    ]
    assert [r["label"] for r in _valid_routes(raw)] == ["ok"]


def test_valid_routes_dedups_labels_case_insensitive():
    raw = [
        {"label": "Работа", "project_id": "p1"},
        {"label": "работа", "project_id": "p2"},  # duplicate label → dropped
    ]
    out = _valid_routes(raw)
    assert len(out) == 1 and out[0]["project_id"] == "p1"


def test_valid_routes_non_list_is_empty():
    assert _valid_routes(None) == []
    assert _valid_routes("oops") == []
    assert _valid_routes({"label": "x"}) == []


# ── _route_for ────────────────────────────────────────────────────────────
ROUTES = [
    {"label": "работа", "hint": None, "project_id": "p1", "section_id": "s1"},
    {"label": "личное", "hint": None, "project_id": "p2", "section_id": None},
]


def test_route_for_matches_case_insensitive():
    assert _route_for({"route": "Работа"}, ROUTES)["project_id"] == "p1"
    assert _route_for({"route": "ЛИЧНОЕ"}, ROUTES)["project_id"] == "p2"


def test_route_for_null_or_unknown_is_default():
    assert _route_for({"route": None}, ROUTES) is None
    assert _route_for({}, ROUTES) is None
    assert _route_for({"route": "спорт"}, ROUTES) is None


def test_route_for_no_routes():
    assert _route_for({"route": "работа"}, []) is None


# ── prompt block ──────────────────────────────────────────────────────────
def test_prompt_has_routing_block_when_routes_given():
    p = _build_user_prompt(
        "window", "sum", [],
        routes=[{"label": "работа", "hint": "клиенты"}, {"label": "личное"}],
    )
    assert "# ROUTING" in p
    assert '"работа" — клиенты' in p
    assert '"личное"' in p


def test_prompt_no_routing_block_without_routes():
    assert "# ROUTING" not in _build_user_prompt("window", "sum", [])
    assert "# ROUTING" not in _build_user_prompt("window", "sum", [], routes=[])

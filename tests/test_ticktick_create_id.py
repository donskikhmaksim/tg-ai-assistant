"""The root fix: create_tasks now echoes the new id inline as `(id:<id>)`, and
the client reads it straight from the result line (no title search). These lock
the parse against the exact strings the ticktick-mcp server emits."""
from app.ticktick.mcp_client import _PAREN_ID_RE


def _id(text: str) -> str | None:
    m = _PAREN_ID_RE.search(text)
    return m.group(1).strip() if m else None


def test_created_id_single_task():
    out = "Создано 1:\n✓ «Позвонить маме» (id:6a5ec7948f08352c918086fd)"
    assert _id(out) == "6a5ec7948f08352c918086fd"


def test_created_id_with_subtasks():
    out = "Создано 1:\n✓ «Q3 Launch» + 3 подзадач (id:6a5ec7948f08352c91808700)"
    assert _id(out) == "6a5ec7948f08352c91808700"


def test_created_id_tree_path():
    # PATH A tree line has TWO parens — the id trailer is last; the first match
    # is the id, not the "(дерево, N всего)" note (that has no `id:`).
    out = "Создано 1:\n✓ «Эпик» + 2 подзадач (дерево, 3 всего) (id:6a5ec7948f08352c91808701)"
    assert _id(out) == "6a5ec7948f08352c91808701"


def test_created_id_absent_returns_none():
    # Older server build without the id trailer → parse yields None (client then
    # falls back to the title search).
    assert _id("Создано 1:\n✓ «Позвонить маме»") is None

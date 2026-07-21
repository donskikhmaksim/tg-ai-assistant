"""«Контроль» attribution decision: given chat type, who the action is on, and
the control_mode toggle, decide create-as-normal | create-as-control | skip.
Plus the title-marker rendering and the TickTick tag payload."""
from app.pipeline.batch import _control_decision, _control_title
from app.ticktick.mcp_client import TickTickMCP


def test_group_always_normal():
    # Groups are unaffected regardless of who / mode (from/to names handle them).
    assert _control_decision("group_123", "counterparty", "on") == "normal"
    assert _control_decision("group_123", "counterparty", "off") == "normal"
    assert _control_decision("group_123", "me", "on") == "normal"


def test_dm_action_on_owner_is_normal():
    # who="me" → owner's own to-do, unaffected by the toggle.
    assert _control_decision("user_42", "me", "on") == "normal"
    assert _control_decision("user_42", "me", "off") == "normal"


def test_dm_counterparty_on_is_control():
    assert _control_decision("user_42", "counterparty", "on") == "control"


def test_dm_counterparty_off_is_skip():
    assert _control_decision("user_42", "counterparty", "off") == "skip"


def test_missing_who_defaults_to_owner():
    # No/blank who is treated as the owner ("me") → normal, never skipped.
    assert _control_decision("user_42", None, "on") == "normal"
    assert _control_decision("user_42", "", "off") == "normal"


def test_unknown_chat_prefix_is_normal():
    # Anything that isn't a DM keeps current behavior.
    assert _control_decision("chan_9", "counterparty", "off") == "normal"


# ── title marker ────────────────────────────────────────────────────────────

def test_control_title_prefixes_marker():
    assert _control_title("Позвонить Наде", True, "👁 Контроль:") == "👁 Контроль: Позвонить Наде"


def test_control_title_custom_marker():
    assert _control_title("Позвонить Наде", True, "[КТРЛ]") == "[КТРЛ] Позвонить Наде"


def test_normal_title_unmarked():
    assert _control_title("Позвонить Наде", False, "👁 Контроль:") == "Позвонить Наде"


def test_empty_marker_leaves_title_bare():
    # Cleared marker → no prefix (the tag still carries the signal).
    assert _control_title("Позвонить Наде", True, "") == "Позвонить Наде"


# ── TickTick tag payload ────────────────────────────────────────────────────

def test_task_obj_includes_tags():
    obj = TickTickMCP._task_obj("t", "p", tags=["контроль"])
    assert obj["tags"] == ["контроль"]


def test_task_obj_omits_tags_when_absent():
    assert "tags" not in TickTickMCP._task_obj("t", "p")
    assert "tags" not in TickTickMCP._task_obj("t", "p", tags=[])

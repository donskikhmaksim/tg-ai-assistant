"""«Контроль» attribution decision: given chat type, who the action is on, and
the control_mode toggle, decide create-as-normal | create-as-control | skip."""
from app.pipeline.batch import _control_decision


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

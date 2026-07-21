from app.web.transcript import (
    NAME_PALETTE,
    group_messages,
    initials,
    sender_color,
)


def _msg(mid, direction="in", sender="Bob", text="hi"):
    return {"messageId": mid, "direction": direction, "senderName": sender, "text": text}


def test_sender_color_is_stable_and_in_palette():
    c1 = sender_color("Alice Smith")
    c2 = sender_color("  alice smith  ")  # case/space-insensitive
    assert c1 == c2
    assert c1 in NAME_PALETTE


def test_sender_color_handles_none():
    assert sender_color(None) in NAME_PALETTE


def test_initials():
    assert initials("Alice Smith") == "AS"
    assert initials("Bob") == "BO"
    assert initials("x") == "X"
    assert initials("") == "?"
    assert initials(None) == "?"


def test_group_consecutive_same_sender():
    msgs = [
        _msg(1, "in", "Bob"),
        _msg(2, "in", "Bob"),
        _msg(3, "out", "Me"),
        _msg(4, "in", "Bob"),
    ]
    groups = group_messages(msgs)
    assert [len(g["messages"]) for g in groups] == [2, 1, 1]
    assert groups[0]["direction"] == "in"
    assert groups[1]["direction"] == "out"


def test_group_breaks_on_sender_change_same_side():
    # Two different senders on the same side (group chat) must not merge.
    msgs = [_msg(1, "in", "Bob"), _msg(2, "in", "Ann")]
    groups = group_messages(msgs)
    assert [g["senderName"] for g in groups] == ["Bob", "Ann"]


def test_group_empty():
    assert group_messages([]) == []

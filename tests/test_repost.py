from app.telegram.repost import (
    FILTER_ALL,
    FILTER_MINE,
    FILTER_THEIRS,
    build_repost,
    can_native_forward,
    derive_owner_label,
    filter_messages,
    format_message,
)


def _msg(direction, text, sender=None, mid=1, biz="BQ"):
    return {
        "direction": direction,
        "text": text,
        "senderName": sender,
        "messageId": mid,
        "businessConnectionId": biz,
    }


DIALOGUE = [
    _msg("out", "привет", "Максим", 1),
    _msg("in", "здарова", "Пётр", 2),
    _msg("out", "как дела?", "Максим", 3),
    _msg("in", "норм", "Пётр", 4),
]


# --- filtering -------------------------------------------------------------

def test_filter_mine_keeps_only_outgoing():
    kept = filter_messages(DIALOGUE, FILTER_MINE)
    assert [m["messageId"] for m in kept] == [1, 3]
    assert all(m["direction"] == "out" for m in kept)


def test_filter_theirs_keeps_only_incoming():
    kept = filter_messages(DIALOGUE, FILTER_THEIRS)
    assert [m["messageId"] for m in kept] == [2, 4]
    assert all(m["direction"] == "in" for m in kept)


def test_filter_all_keeps_everything_in_order():
    kept = filter_messages(DIALOGUE, FILTER_ALL)
    assert [m["messageId"] for m in kept] == [1, 2, 3, 4]


def test_unknown_mode_falls_back_to_all():
    assert len(filter_messages(DIALOGUE, "bogus")) == len(DIALOGUE)


# --- formatting ------------------------------------------------------------

def test_format_message_bold_label_then_text_on_next_line():
    out = format_message(DIALOGUE[0])
    assert out == "<b>Максим:</b>\nпривет"


def test_format_escapes_html_in_name_and_text():
    m = _msg("in", "1 < 2 & 3 > 0", sender="A<b>Z", mid=9)
    out = format_message(m)
    assert "&lt;" in out and "&amp;" in out
    assert "<b>" in out  # the wrapping tag is preserved
    assert "A<b>Z" not in out  # the name's raw angle brackets are escaped


def test_format_falls_back_to_role_labels_when_no_sender():
    assert format_message(_msg("out", "hi", sender=None)).startswith("<b>Я:</b>")
    assert format_message(_msg("in", "hi", sender=None)).startswith("<b>Собеседник:</b>")


def test_build_repost_preserves_order_and_filters():
    chunks = build_repost(DIALOGUE, FILTER_THEIRS)
    assert len(chunks) == 1
    body = chunks[0]
    assert body.index("здарова") < body.index("норм")
    assert "привет" not in body  # owner's messages excluded


def test_build_repost_skips_empty_text():
    msgs = [_msg("out", "  ", "Максим", 1), _msg("in", "текст", "Пётр", 2)]
    chunks = build_repost(msgs, FILTER_ALL)
    assert len(chunks) == 1
    assert "Пётр" in chunks[0] and "Максим" not in chunks[0]


def test_build_repost_chunks_to_length_limit():
    big = [_msg("in", "x" * 400, "Пётр", i) for i in range(20)]
    chunks = build_repost(big, FILTER_ALL, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


def test_build_repost_hard_splits_single_oversized_block():
    chunks = build_repost([_msg("in", "y" * 5000, "Пётр", 1)], FILTER_ALL, limit=1000)
    assert len(chunks) >= 5
    assert all(len(c) <= 1000 for c in chunks)


def test_build_repost_empty_when_nothing_selected():
    assert build_repost(DIALOGUE, FILTER_MINE) and not build_repost([], FILTER_MINE)


# --- native forward feasibility -------------------------------------------

def test_business_messages_are_not_natively_forwardable():
    assert can_native_forward(DIALOGUE) is False


def test_plain_messages_are_forwardable():
    plain = [_msg("in", "hi", "Пётр", 1, biz=None)]
    assert can_native_forward(plain) is True


def test_empty_selection_not_forwardable():
    assert can_native_forward([]) is False


# --- owner label -----------------------------------------------------------

def test_derive_owner_label_uses_latest_own_sender():
    assert derive_owner_label(DIALOGUE) == "Максим"


def test_derive_owner_label_defaults_when_no_own_message():
    assert derive_owner_label([_msg("in", "hi", "Пётр", 1)]) == "Я"

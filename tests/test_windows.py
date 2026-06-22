from datetime import datetime, timedelta, timezone

from app.pipeline.windows import build_window, render_window

BASE = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)


def _doc(minutes_offset, direction="in", text="hi", mid=1, name="A"):
    return {
        "chatId": "user_1",
        "direction": direction,
        "senderName": name,
        "text": text,
        "messageId": mid,
        "date": BASE + timedelta(minutes=minutes_offset),
    }


def test_empty():
    assert build_window([], gap_hours=6) == []


def test_single_message():
    win = build_window([_doc(0)], gap_hours=6)
    assert len(win) == 1


def test_no_gap_keeps_all_six_chunks():
    # «я → через час он → через час я» — 6 кусков по ~1 ч, разрывов > 6ч нет
    docs = [_doc(i * 60, mid=i + 1) for i in range(6)]
    win = build_window(docs, gap_hours=6)
    assert len(win) == 6


def test_gap_cuts_window():
    # старый кусок, затем разрыв 8 часов, затем свежий разговор
    old = [_doc(0, mid=1), _doc(30, mid=2)]
    fresh = [_doc(8 * 60 + 30, mid=3), _doc(8 * 60 + 40, mid=4)]
    win = build_window(old + fresh, gap_hours=6)
    ids = [m.message_id for m in win]
    assert ids == [3, 4]  # старое отрезано паузой > 6ч


def test_render_includes_id_direction_and_text():
    win = build_window([_doc(0, direction="out", text="привет", mid=7, name="Я")], 6)
    rendered = render_window(win)
    assert "#7" in rendered
    assert "out" in rendered
    assert "привет" in rendered

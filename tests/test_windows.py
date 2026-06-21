from datetime import datetime, timedelta, timezone

from app.pipeline.windows import build_window, render_window

BASE = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _msg(minutes: int, mid: int, direction: str = "in", text: str = "hi"):
    return {
        "date": BASE + timedelta(minutes=minutes),
        "messageId": mid,
        "direction": direction,
        "senderName": "Bob",
        "text": text,
    }


def test_empty():
    assert build_window([], gap_hours=6, max_lookback_hours=48) == []


def test_contiguous_alternating_stays_one_window():
    # "me -> 1h -> them -> 1h -> me" — six ~1h hops, no gap > 6h => all in.
    msgs = [_msg(i * 60, i, "out" if i % 2 else "in") for i in range(6)]
    win = build_window(msgs, gap_hours=6, max_lookback_hours=48)
    assert len(win) == 6


def test_gap_splits_window():
    # old cluster, then a 10h gap, then a fresh message
    msgs = [_msg(0, 1), _msg(30, 2), _msg(30 + 10 * 60, 3)]
    win = build_window(msgs, gap_hours=6, max_lookback_hours=48)
    assert [m["messageId"] for m in win] == [3]


def test_max_lookback_caps_window():
    # messages every hour for 60h; only the last 48h are kept
    msgs = [_msg(i * 60, i) for i in range(61)]
    win = build_window(msgs, gap_hours=6, max_lookback_hours=48)
    newest = msgs[-1]["date"]
    assert all((newest - m["date"]).total_seconds() <= 48 * 3600 for m in win)
    assert win[-1]["messageId"] == 60


def test_render_includes_direction_and_id():
    out = render_window([_msg(0, 7, "out", "ping")])
    assert "(out)" in out and "#7" in out and "ping" in out

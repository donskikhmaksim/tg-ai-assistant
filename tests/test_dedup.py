from app.pipeline.dedup import to_ticktick_due


def test_date_becomes_iso_timestamp():
    assert to_ticktick_due("2026-06-28") == "2026-06-28T00:00:00+0000"


def test_none_passthrough():
    assert to_ticktick_due(None) is None
    assert to_ticktick_due("") is None


def test_full_timestamp_passthrough():
    assert to_ticktick_due("2026-06-28T09:30:00+0000") == "2026-06-28T09:30:00+0000"

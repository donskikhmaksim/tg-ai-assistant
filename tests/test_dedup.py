from app.pipeline.dedup import is_all_day_deadline, to_ticktick_due


def test_date_becomes_iso_timestamp():
    assert to_ticktick_due("2026-06-28") == "2026-06-28T00:00:00+0000"


def test_none_passthrough():
    assert to_ticktick_due(None) is None
    assert to_ticktick_due("") is None


def test_full_timestamp_passthrough():
    assert to_ticktick_due("2026-06-28T09:30:00+0000") == "2026-06-28T09:30:00+0000"


def test_date_only_is_all_day():
    assert is_all_day_deadline("2026-06-28") is True
    assert is_all_day_deadline("2026-06-28T17:00") is False
    assert is_all_day_deadline(None) is False


def test_time_defaults_to_utc():
    # No zone named and no explicit default → neutral UTC (never an owner zone).
    assert to_ticktick_due("2026-06-28T17:00") == "2026-06-28T17:00:00+0000"
    assert to_ticktick_due("2026-01-15T17:00") == "2026-01-15T17:00:00+0000"


def test_explicit_zone_is_dst_aware():
    # Passing a zone explicitly is honored, DST included.
    # LA: June → PDT (-0700), January → PST (-0800).
    assert to_ticktick_due("2026-06-28T17:00", "America/Los_Angeles") == "2026-06-28T17:00:00-0700"
    assert to_ticktick_due("2026-01-15T17:00", "America/Los_Angeles") == "2026-01-15T17:00:00-0800"


def test_named_zone_overrides_default():
    assert to_ticktick_due("2026-06-28T17:00", "Europe/Moscow") == "2026-06-28T17:00:00+0300"

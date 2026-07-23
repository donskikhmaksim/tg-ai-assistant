from app.pipeline.dedup import is_all_day_deadline, to_ticktick_due


def test_date_passes_through_verbatim():
    # An all-day deadline is a zone-independent calendar date: the literal
    # YYYY-MM-DD flows through with no offset attached (#36).
    assert to_ticktick_due("2026-06-28") == "2026-06-28"


def test_date_is_zone_independent():
    # The bare-date result must NOT depend on default_tz or a named zone — no
    # offset of any sign can shift the calendar day.
    for default_tz in ("UTC", "America/Los_Angeles", "Europe/Moscow"):
        assert to_ticktick_due("2026-06-28", default_tz=default_tz) == "2026-06-28"
        assert to_ticktick_due("2026-06-28", "Europe/Moscow", default_tz) == "2026-06-28"


def test_invalid_bare_date_dropped():
    assert to_ticktick_due("2026-13-40") is None


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

"""End-of-day group summary: text composition + skip/off decision (pure logic)."""
from app.pipeline import summary


# ── should_send_summary: opt-in AND activity ──────────────────────────────────

def test_off_never_sends_even_with_activity():
    assert summary.should_send_summary("off", 3, 2) is False


def test_on_with_no_activity_is_skipped():
    assert summary.should_send_summary("on", 0, 0) is False


def test_on_with_created_only_sends():
    assert summary.should_send_summary("on", 1, 0) is True


def test_on_with_closed_only_sends():
    assert summary.should_send_summary("on", 0, 1) is True


def test_blank_mode_treated_as_off():
    # An unresolved/empty toggle must not send.
    assert summary.should_send_summary("", 5, 5) is False


# ── compose_summary: empty day → None ─────────────────────────────────────────

def test_compose_empty_returns_none():
    assert summary.compose_summary("21.07.2026", [], 0) is None


# ── compose_summary: header counts + plural ──────────────────────────────────

def test_header_has_counts_and_date():
    text = summary.compose_summary("21.07.2026", ["Купить билеты"], 2)
    assert text is not None
    assert "📋 Итог дня (21.07.2026):" in text
    assert "создано 1 задача" in text  # 1 → singular
    assert "обновлено 2." in text


def test_plural_forms():
    assert summary._plural_tasks(1) == "задача"
    assert summary._plural_tasks(2) == "задачи"
    assert summary._plural_tasks(5) == "задач"
    assert summary._plural_tasks(11) == "задач"   # teens are genitive plural
    assert summary._plural_tasks(21) == "задача"
    assert summary._plural_tasks(0) == "задач"


# ── compose_summary: bulleted list + overflow ─────────────────────────────────

def test_lists_created_titles_as_bullets():
    text = summary.compose_summary("21.07.2026", ["Первая", "Вторая"], 0)
    assert "• Первая" in text
    assert "• Вторая" in text
    assert "…и ещё" not in text  # nothing truncated


def test_caps_list_and_shows_overflow():
    titles = [f"Задача {i}" for i in range(1, 9)]  # 8 created titles
    text = summary.compose_summary("21.07.2026", titles, 0, max_list=5)
    # Only the first 5 are bulleted, the rest collapse into "…и ещё 3".
    assert text.count("• ") == 5
    assert "• Задача 5" in text
    assert "• Задача 6" not in text
    assert "…и ещё 3" in text
    # Header still counts ALL created, not just the shown ones.
    assert "создано 8 задач" in text


def test_blank_titles_are_ignored_in_list():
    # A stored task with an empty title shouldn't produce an empty bullet.
    text = summary.compose_summary("21.07.2026", ["Настоящая", "  "], 0)
    assert "• Настоящая" in text
    assert text.count("• ") == 1


# ── group chat id → telegram id ───────────────────────────────────────────────

def test_group_chat_to_telegram_id():
    assert summary._group_chat_to_telegram_id("group_-1001234567890") == -1001234567890

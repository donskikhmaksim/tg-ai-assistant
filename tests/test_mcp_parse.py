import asyncio

from app.ticktick.mcp_client import (
    TickTickMCP,
    _chat_id_from_content,
    _parse_pairs,
    _parse_project_cards,
    _parse_projects,
)

# Real list_project_columns output shape: "- <name>  (id: <id>)".
COLUMNS = """Columns of project 69f841179f1911020b96a62b (2):
- Tg  (id: 6a3b552fb13e11a209c4e4c3)
- Not Sectioned  (id: 6a3b5525c189d1a209c4e495)"""

# Real get_projects output shape: "Name:" / "ID:" blocks.
PROJECTS = """Name: EPEX
ID: 655cdfeb2c49d17e8d021f50

Name: Family
ID: 699d03848f0853b739baf1ce"""


def test_parses_column_bullet_format():
    cols = _parse_pairs(COLUMNS)
    assert cols == [
        {"name": "Tg", "id": "6a3b552fb13e11a209c4e4c3"},
        {"name": "Not Sectioned", "id": "6a3b5525c189d1a209c4e495"},
    ]


def test_column_name_with_spaces_and_ampersand():
    cols = _parse_pairs("- CRM & IT  (id: 69f8de04d0fd514afc760375)")
    assert cols == [{"name": "CRM & IT", "id": "69f8de04d0fd514afc760375"}]


def test_projects_block_format_still_parses():
    assert _parse_projects(PROJECTS) == [
        {"name": "EPEX", "id": "655cdfeb2c49d17e8d021f50"},
        {"name": "Family", "id": "699d03848f0853b739baf1ce"},
    ]


# The ACTUAL live get_projects shape (2026-07): numbered blocks, extra metadata
# lines, and the id on its own as "(id: <id>)" — NOT "ID: <id>". The old parser
# only knew "ID:" so this parsed to [] and the Mini App project picker went blank.
PROJECTS_LIVE = """Found 3 projects:

Project 1:
Name: ⭐Personal
Color: #4CA1FF
View Mode: kanban
Kind: TASK
(id: 699d03848f0853b739baf1ca)

Project 2:
Name: 🧠Assistant
View Mode: kanban
Closed: Yes
Kind: TASK
(id: 699d03848f0853b739baf1d6)

Project 3:
Name: Тест
View Mode: list
Kind: TASK
(id: 69eac1bd6d2ed12a11aaf7c2)"""


def test_projects_live_paren_id_blocks():
    assert _parse_projects(PROJECTS_LIVE) == [
        {"name": "⭐Personal", "id": "699d03848f0853b739baf1ca"},
        {"name": "🧠Assistant", "id": "699d03848f0853b739baf1d6"},
        {"name": "Тест", "id": "69eac1bd6d2ed12a11aaf7c2"},
    ]


# Real get_project_tasks output (captured live from the deployed ticktick-mcp,
# 2026-08-04, project '⭐Personal', truncated to its first 2 of 216 tasks).
# REGRESSION: the parser used to look for a trailing "(id: <id> | project:
# <pid>)" marker that the deployed server has never emitted — it puts the id on
# its own "ID:" line instead. Every block silently failed to match, so
# get_project_tasks() always returned [] and the semantic-dedup candidate pool
# (app/pipeline/batch.py) was empty no matter how high dedup_project_task_cap
# was set. This fixture is the exact server text, not a hand-written guess.
PROJECT_TASKS_LIVE = """Found 216 tasks in project '⭐Personal':

Task 1:
ID: 6a64e0a48f08bf71b42143c8
Title: Оплатить Coinbase One Card — минимум $88 до 19.08
Project ID: 699d03848f0853b739baf1ca
Start Date: 2026-08-16T00:00:00.000+0000
Due Date: 2026-08-16T00:00:00.000+0000
Priority: Medium
Status: Active

Content:
От: Coinbase One Card <noreply@creditcard.coinbase.com>
Тема: Your latest statement is ready

Выписка от 25.07.2026:
• Срок оплаты: 19 августа 2026
• Минимальный платёж: $88.00
• Баланс выписки: $1,830.72

Действие: оплатить минимум $88 через приложение Coinbase (можно настроить автоплатёж).
Телефон поддержки: (888) 908-7930

Task 2:
ID: 6a5f60d08f08722c0a906094
Title: Проверить отменённый перевод Chase Auto $876.69 — до 13.08
Project ID: 699d03848f0853b739baf1ca
Start Date: 2026-08-12T21:00:00.000+0000
Due Date: 2026-08-13T00:00:00.000+0000
Priority: High
Status: Active

Content:
От: Chase (no.reply.alerts@chase.com)
Тема: Your transfer has been cancelled

Система отменила запланированный автоплатёж:
• Сумма: $876.69
• Откуда: Citibank (...9512)
• Куда: Chase Auto Account (...4038)
• Плановая дата: 14 августа 2026

Действия:
1. Проверить, почему отменён перевод (Chase app/сайт)
2. Если отмена ошибочна — восстановить платёж до 14.08
3. Убедиться, что платёж по автокредиту не пропущен

"""


def test_parses_real_project_tasks_blocks_not_empty():
    """The exact regression: this must NOT come back []."""
    cards = _parse_project_cards(PROJECT_TASKS_LIVE)
    assert len(cards) == 2


def test_parses_real_project_tasks_fields():
    cards = _parse_project_cards(PROJECT_TASKS_LIVE)
    assert cards[0]["id"] == "6a64e0a48f08bf71b42143c8"
    assert cards[0]["title"] == "Оплатить Coinbase One Card — минимум $88 до 19.08"
    assert cards[0]["due"] == "2026-08-16T00:00:00.000+0000"
    assert cards[0]["priority"] == "Medium"
    assert cards[0]["status"] == "Active"
    assert "Coinbase" in cards[0]["content"]

    assert cards[1]["id"] == "6a5f60d08f08722c0a906094"
    assert cards[1]["title"] == "Проверить отменённый перевод Chase Auto $876.69 — до 13.08"
    assert cards[1]["due"] == "2026-08-13T00:00:00.000+0000"


def test_json_array_fallback():
    js = '[{"id": "c1", "name": "Done"}, {"columnId": "c2", "title": "Doing"}]'
    assert _parse_pairs(js) == [
        {"name": "Done", "id": "c1"},
        {"name": "Doing", "id": "c2"},
    ]


SEARCH_OUT = (
    "Tasks matching 'Написать или надиктовать' (1):\n"
    "- [Inbox] Написать или надиктовать, в чём суть предложения по гаражным "
    "воротам  (id:6a5ec7948f08352c918086fd proj:inbox122587194)"
)


def test_find_task_id_recovers_id_from_search():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        assert name == "search_tasks"
        return SEARCH_OUT

    tt.call = fake_call  # type: ignore[assignment]
    tid = asyncio.run(
        tt.find_task_id("Написать или надиктовать, в чём суть предложения по гаражным воротам")
    )
    assert tid == "6a5ec7948f08352c918086fd"


def test_find_task_id_none_when_no_match():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        return "Tasks matching 'x' (0):"

    tt.call = fake_call  # type: ignore[assignment]
    assert asyncio.run(tt.find_task_id("nope")) is None


def test_find_task_id_exact_not_substring():
    """A shorter search term must NOT link to a longer near-duplicate's id."""
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        return "- [Inbox] Составить ТЗ по помещению, дедлайн пт  (id:LONG123 proj:p)"

    tt.call = fake_call  # type: ignore[assignment]
    assert asyncio.run(tt.find_task_id("Составить ТЗ")) is None
    # exact title still matches
    async def fake_exact(name, args):
        return "- [Inbox] Составить ТЗ  (id:EXACT9 proj:p)"
    tt.call = fake_exact  # type: ignore[assignment]
    assert asyncio.run(tt.find_task_id("Составить ТЗ")) == "EXACT9"


def test_find_task_id_exclude_skips_already_claimed_id():
    """Two different local docs can share the exact same title (see
    scripts/push_local_tasks.py). If TickTick already has two tasks with that
    title, `exclude` must make the second lookup skip the id the first doc
    already claimed instead of collapsing both docs onto the same TickTick id."""
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        return (
            "- [Inbox] Позвонить в банк  (id:FIRST1 proj:p)\n"
            "- [Inbox] Позвонить в банк  (id:SECOND2 proj:p)"
        )

    tt.call = fake_call  # type: ignore[assignment]
    first = asyncio.run(tt.find_task_id("Позвонить в банк"))
    assert first == "FIRST1"
    second = asyncio.run(tt.find_task_id("Позвонить в банк", exclude={first}))
    assert second == "SECOND2"
    # once both are excluded, nothing is left to bind to
    third = asyncio.run(tt.find_task_id("Позвонить в банк", exclude={first, second}))
    assert third is None


# ─────────────────────────────────────────────────────────────────────────────
# _chat_id_from_content / find_task_id_for_chat — chat-of-origin disambiguation
# (see scripts/push_local_tasks.py and app/pipeline/batch.py `_chat_link`).
# ─────────────────────────────────────────────────────────────────────────────


def test_chat_id_from_content_extracts_and_unquotes():
    content = "[💬 Прочитать переписку](https://x/chat?c=user_123%3A456&t=abc)"
    assert _chat_id_from_content(content) == "user_123:456"


def test_chat_id_from_content_none_when_no_marker():
    assert _chat_id_from_content("Просто текст без ссылки, без метки чата") is None
    assert _chat_id_from_content(None) is None
    assert _chat_id_from_content("") is None


_TWO_SAME_TITLE_WITH_MARKERS = (
    "Task 1:\n"
    "ID: TASKA\n"
    "Title: Позвонить в банк\n"
    "Project ID: proj1\n"
    "Status: Active\n"
    "\n"
    "Content:\n"
    "[💬 Прочитать переписку](https://x/chat?c=chatA&t=tok1)\n"
    "\n"
    "Task 2:\n"
    "ID: TASKB\n"
    "Title: Позвонить в банк\n"
    "Project ID: proj1\n"
    "Status: Active\n"
    "\n"
    "Content:\n"
    "[💬 Прочитать переписку](https://x/chat?c=chatB&t=tok2)\n"
)

_TWO_SAME_TITLE_NO_MARKERS = (
    "Task 1:\n"
    "ID: TASKA\n"
    "Title: Позвонить в банк\n"
    "Project ID: proj1\n"
    "Status: Active\n"
    "\n"
    "Task 2:\n"
    "ID: TASKB\n"
    "Title: Позвонить в банк\n"
    "Project ID: proj1\n"
    "Status: Active\n"
)


def test_find_task_id_for_chat_disambiguates_via_embedded_chat_marker():
    """Two TickTick tasks share the exact title but carry different embedded
    chat-of-origin markers — the doc's chatId must pick the RIGHT one, not
    whichever comes first positionally."""
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        assert name == "get_project_tasks"
        return _TWO_SAME_TITLE_WITH_MARKERS

    tt.call = fake_call  # type: ignore[assignment]
    tid, ambiguous = asyncio.run(
        tt.find_task_id_for_chat("Позвонить в банк", "chatB", "proj1")
    )
    assert tid == "TASKB"
    assert ambiguous is False

    tid2, ambiguous2 = asyncio.run(
        tt.find_task_id_for_chat("Позвонить в банк", "chatA", "proj1")
    )
    assert tid2 == "TASKA"
    assert ambiguous2 is False


def test_find_task_id_for_chat_ambiguous_when_no_markers_present():
    """Same-titled collision but NEITHER candidate carries a chat marker (e.g.
    created before WEBAPP_URL was configured) — there is no data to
    disambiguate with, so the result must be flagged ambiguous=True rather
    than silently pretending to know which is which."""
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        return _TWO_SAME_TITLE_NO_MARKERS

    tt.call = fake_call  # type: ignore[assignment]
    tid, ambiguous = asyncio.run(
        tt.find_task_id_for_chat("Позвонить в банк", "chatA", "proj1")
    )
    assert tid in {"TASKA", "TASKB"}  # still resolves SOMETHING (no doc left stranded)
    assert ambiguous is True


def test_find_task_id_for_chat_single_candidate_is_not_ambiguous():
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        return (
            "Task 1:\n"
            "ID: TASKA\n"
            "Title: Купить молоко\n"
            "Project ID: proj1\n"
            "Status: Active\n"
        )

    tt.call = fake_call  # type: ignore[assignment]
    tid, ambiguous = asyncio.run(
        tt.find_task_id_for_chat("Купить молоко", "chatA", "proj1")
    )
    assert tid == "TASKA"
    assert ambiguous is False


def test_find_task_id_for_chat_falls_back_to_global_search_without_project_hits():
    """No candidates in the given project's card dump (e.g. wrong/missing
    project_id) — falls back to the plain exact-title search, same as the
    pre-existing find_task_id() behaviour."""
    tt = TickTickMCP(url="http://x")

    async def fake_call(name, args):
        if name == "get_project_tasks":
            return "Found 0 tasks in project 'x':"
        if name == "search_tasks":
            return "- [Inbox] Купить молоко  (id:GLOBAL1 proj:p)"
        raise AssertionError(f"unexpected call: {name}")

    tt.call = fake_call  # type: ignore[assignment]
    tid, ambiguous = asyncio.run(
        tt.find_task_id_for_chat("Купить молоко", "chatA", "proj1")
    )
    assert tid == "GLOBAL1"
    assert ambiguous is False

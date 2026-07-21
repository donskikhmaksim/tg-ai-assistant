import asyncio

from app.ticktick.mcp_client import TickTickMCP, _parse_pairs, _parse_projects

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

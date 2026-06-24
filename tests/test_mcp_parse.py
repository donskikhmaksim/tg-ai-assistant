from app.ticktick.mcp_client import _parse_pairs, _parse_projects

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


def test_json_array_fallback():
    js = '[{"id": "c1", "name": "Done"}, {"columnId": "c2", "title": "Doing"}]'
    assert _parse_pairs(js) == [
        {"name": "Done", "id": "c1"},
        {"name": "Doing", "id": "c2"},
    ]

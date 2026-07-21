import asyncio

from app.pipeline.semantic_dedup import (
    band,
    best_match,
    cosine,
    decide_duplicate,
    merge_details,
)
from app.ticktick.mcp_client import _parse_project_cards, _parse_task_lines


# ── cosine ────────────────────────────────────────────────────────────────
def test_cosine_identical_is_one():
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 1.0


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_scale_invariant():
    assert abs(cosine([1.0, 1.0], [3.0, 3.0]) - 1.0) < 1e-9


def test_cosine_empty_is_zero():
    assert cosine([], []) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ── best_match ───────────────────────────────────────────────────────────
def _cand(name, vec):
    return {"title": name, "embedding": vec}


def test_best_match_none_below_threshold():
    q = [1.0, 0.0]
    cands = [_cand("orthogonal", [0.0, 1.0])]  # cosine 0.0
    assert best_match(q, cands, 0.86) is None


def test_best_match_picks_above_threshold():
    q = [1.0, 0.0, 0.0]
    cands = [_cand("same", [1.0, 0.0, 0.0])]  # cosine 1.0
    m = best_match(q, cands, 0.86)
    assert m is not None
    assert m["title"] == "same"
    assert abs(m["score"] - 1.0) < 1e-9


def test_best_match_returns_highest_scoring():
    q = [1.0, 0.0]
    cands = [
        _cand("weak", [0.9, 0.436]),   # ~0.9 cosine
        _cand("strong", [1.0, 0.02]),  # ~0.9998 cosine
    ]
    m = best_match(q, cands, 0.86)
    assert m is not None
    assert m["title"] == "strong"


def test_best_match_empty_candidates():
    assert best_match([1.0, 0.0], [], 0.86) is None


def test_best_match_skips_candidates_without_embedding():
    q = [1.0, 0.0]
    cands = [{"title": "no-vec"}, _cand("same", [1.0, 0.0])]
    m = best_match(q, cands, 0.86)
    assert m is not None and m["title"] == "same"


def test_best_match_threshold_boundary_inclusive():
    # A candidate scoring exactly at the threshold qualifies.
    q = [1.0, 0.0]
    cands = [_cand("exact", [1.0, 0.0])]
    assert best_match(q, cands, 1.0) is not None


def test_best_match_passes_through_identity_fields():
    q = [1.0, 0.0]
    cands = [{"title": "t", "embedding": [1.0, 0.0], "ticktickTaskId": "abc",
              "projectId": "p1", "dedupHash": "h1"}]
    m = best_match(q, cands, 0.5)
    assert m["ticktickTaskId"] == "abc"
    assert m["projectId"] == "p1"
    assert m["dedupHash"] == "h1"


# ── merge_details ────────────────────────────────────────────────────────
def test_merge_details_new_text_appended():
    assert merge_details("old context", "brand new detail") == "brand new detail"


def test_merge_details_empty_new_is_none():
    assert merge_details("old", "") is None
    assert merge_details("old", None) is None
    assert merge_details("old", "   ") is None


def test_merge_details_substring_is_none():
    assert merge_details("please call the supplier today", "call the supplier") is None


def test_merge_details_case_insensitive_substring():
    assert merge_details("Call The Supplier", "call the supplier") is None


def test_merge_details_no_existing_returns_new():
    assert merge_details(None, "first detail") == "first detail"
    assert merge_details("", "first detail") == "first detail"


# ── band classification ──────────────────────────────────────────────────
LOW, HIGH = 0.83, 0.93


def test_band_duplicate_at_and_above_high():
    assert band(0.93, LOW, HIGH) == "duplicate"
    assert band(0.987, LOW, HIGH) == "duplicate"  # "tap-to-pay" vs "tap to pay"


def test_band_distinct_at_and_below_low():
    assert band(0.83, LOW, HIGH) == "distinct"
    assert band(0.488, LOW, HIGH) == "distinct"  # genuinely distinct


def test_band_gray_in_between():
    assert band(0.857, LOW, HIGH) == "gray"  # real dup that a single 0.86 misses
    assert band(0.879, LOW, HIGH) == "gray"
    assert band(0.832, LOW, HIGH) == "gray"  # the observed false positive


# ── decide_duplicate (band + gray-zone judge) ────────────────────────────
def _run(coro):
    return asyncio.run(coro)


async def _judge_yes():
    return True


async def _judge_no():
    return False


async def _judge_none():
    return None


async def _judge_boom():
    raise RuntimeError("judge unavailable")


def test_decide_high_band_still_consults_judge():
    # A high cosine no longer auto-merges — the judge decides (URLs/numbers can be
    # ≥high yet distinct). Judge yes → duplicate; judge no → distinct.
    assert _run(decide_duplicate(0.95, LOW, HIGH, _judge_yes)) is True
    assert _run(decide_duplicate(0.95, LOW, HIGH, _judge_no)) is False


def test_decide_low_band_is_distinct_without_judge():
    # ≤ low → distinct; judge not consulted.
    assert _run(decide_duplicate(0.80, LOW, HIGH, _judge_boom)) is False


def test_decide_gray_calls_judge_yes():
    assert _run(decide_duplicate(0.857, LOW, HIGH, _judge_yes)) is True


def test_decide_gray_calls_judge_no():
    assert _run(decide_duplicate(0.832, LOW, HIGH, _judge_no)) is False


def test_decide_gray_judge_none_is_distinct():
    # Judge unavailable → SAFE default: create (distinct), never drop a task.
    assert _run(decide_duplicate(0.87, LOW, HIGH, _judge_none)) is False


def test_decide_gray_judge_error_is_distinct():
    assert _run(decide_duplicate(0.87, LOW, HIGH, _judge_boom)) is False


def test_decide_boundary_low_inclusive():
    # Exactly at low → distinct (no judge). Above low → judge decides.
    assert _run(decide_duplicate(0.83, LOW, HIGH, _judge_boom)) is False
    assert _run(decide_duplicate(0.93, LOW, HIGH, _judge_yes)) is True
    assert _run(decide_duplicate(0.93, LOW, HIGH, _judge_no)) is False


# ── project-task line parsing (ticktick client) ──────────────────────────
def test_parse_task_lines_search_format():
    text = (
        "Tasks in project (2):\n"
        "- [Inbox] Составить ТЗ  (id:6a5ec7948f08352c918086fd proj:inbox122587194)\n"
        "- [Inbox] Позвонить Наде  (id:6a5ec7948f08352c918086fe proj:inbox122587194)"
    )
    assert _parse_task_lines(text) == [
        {"title": "Составить ТЗ", "id": "6a5ec7948f08352c918086fd"},
        {"title": "Позвонить Наде", "id": "6a5ec7948f08352c918086fe"},
    ]


def test_parse_task_lines_plain_bullet():
    assert _parse_task_lines("- Buy milk  (id: abc123)") == [
        {"title": "Buy milk", "id": "abc123"}
    ]


def test_parse_task_lines_ignores_headers_and_blanks():
    assert _parse_task_lines("Found 0 tasks:\n\n") == []


# ── rich `Task N:` block parsing (get_project_tasks real format) ──────────
_REAL_BLOCKS = (
    "Found 2 tasks in project '⭐Personal':\n\n"
    "Task 1:\n"
    "Title: Проверить трафик CHP RV97124\n"
    "Start Date: 2026-08-10T07:00:00.000+0000\n"
    "Due Date: 2026-08-10T07:00:00.000+0000\n"
    "Priority: High\n"
    "Status: Active\n\n"
    "Content:\n"
    "Трафик выдан 7/3/26, CHP 215.\n"
    "Дедлайн: 18 августа 2026.\n"
    "(id: 6a4861848f0800bd4b30e74b | project: 699d03848f0853b739baf1ca)\n\n"
    "Task 2:\n"
    "Title: Напомнить Маше о возврате $2,000\n"
    "Due Date: none\n"
    "Priority: None\n"
    "Status: Active\n"
    "(id: 6a50ad507ebfd149c8ee1518 | project: 699d03848f0853b739baf1ca)\n"
)


def test_parse_project_cards_rich_block():
    cards = _parse_project_cards(_REAL_BLOCKS)
    assert len(cards) == 2
    a, b = cards
    assert a["id"] == "6a4861848f0800bd4b30e74b"
    assert a["title"] == "Проверить трафик CHP RV97124"
    assert a["due"] == "2026-08-10T07:00:00.000+0000"
    assert a["priority"] == "High"
    assert "Дедлайн: 18 августа 2026." in a["content"]
    # closing id line must NOT bleed into content
    assert "(id:" not in a["content"]
    assert b["id"] == "6a50ad507ebfd149c8ee1518"
    assert b["title"].startswith("Напомнить Маше")
    # "none"/"None" sentinels are dropped, not stored
    assert "due" not in b and "priority" not in b and "content" not in b


def test_parse_project_cards_falls_back_to_bullets():
    # No Task N: blocks → bullet (search_tasks) shape still parses.
    text = "- [Inbox] Buy milk  (id:abc123 proj:inbox1)"
    assert _parse_project_cards(text) == [{"title": "Buy milk", "id": "abc123"}]


def test_parse_project_cards_empty():
    assert _parse_project_cards("Found 0 tasks in project 'X':\n") == []

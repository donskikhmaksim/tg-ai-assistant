"""_apply_verify_result: pure fold/drop logic for verify_still_open's
verdicts — no repo/TickTick needed, plain dicts in, plain dicts out."""
from app.llm.claude import VerifyResult
from app.pipeline.batch import _apply_verify_result


def _c(task, details=None, source_message_ids=None):
    return {"task": task, "details": details, "source_message_ids": source_message_ids or []}


def test_no_op_when_nothing_flagged():
    candidates = [_c("A"), _c("B")]
    result = VerifyResult(2)
    out = _apply_verify_result(candidates, result)
    assert out == candidates


def test_drop_resolved_candidate():
    candidates = [_c("A"), _c("B")]
    result = VerifyResult(2)
    result.keep[1] = False
    out = _apply_verify_result(candidates, result)
    assert [c["task"] for c in out] == ["A"]


def test_merge_folds_dropped_title_into_kept_details_when_not_already_covered():
    # The vague early candidate's own TITLE carries the unique info (no
    # separate `details` field) — must still survive the merge.
    candidates = [
        _c("Надо позвонить", source_message_ids=[1]),
        _c("Позвонить Наде по поводу отчёта до пятницы", source_message_ids=[3]),
    ]
    result = VerifyResult(2)
    result.merge_into[0] = 1

    out = _apply_verify_result(candidates, result)

    assert [c["task"] for c in out] == ["Позвонить Наде по поводу отчёта до пятницы"]
    assert "Надо позвонить" in out[0]["details"]
    assert out[0]["source_message_ids"] == [1, 3]


def test_merge_does_not_duplicate_already_covered_info():
    candidates = [
        _c("Позвонить"),
        _c("Позвонить Наде по поводу отчёта", details="Позвонить"),
    ]
    result = VerifyResult(2)
    result.merge_into[0] = 1

    out = _apply_verify_result(candidates, result)

    assert len(out) == 1
    # "Позвонить" is already contained in the kept title+details — no
    # redundant re-append.
    assert out[0]["details"] == "Позвонить"


def test_merge_into_dropped_target_keeps_source_independent():
    # index 1 was itself resolved-later (keep=False) — merging INTO a
    # vanishing target would silently lose index 0 too, so it stays
    # independent instead.
    candidates = [_c("A"), _c("B")]
    result = VerifyResult(2)
    result.keep[1] = False
    result.merge_into[0] = 1

    out = _apply_verify_result(candidates, result)
    assert [c["task"] for c in out] == ["A"]


def test_merge_self_reference_is_ignored():
    candidates = [_c("A")]
    result = VerifyResult(1)
    result.merge_into[0] = 0  # degenerate — should never happen, but defend

    out = _apply_verify_result(candidates, result)
    assert [c["task"] for c in out] == ["A"]


def test_merge_chain_resolves_to_final_root():
    # 0 merges into 1, 1 merges into 2 — 0's unique info should still reach 2.
    candidates = [_c("самое раннее"), _c("среднее"), _c("самое полное")]
    result = VerifyResult(3)
    result.merge_into[0] = 1
    result.merge_into[1] = 2

    out = _apply_verify_result(candidates, result)

    assert [c["task"] for c in out] == ["самое полное"]
    assert "самое раннее" in out[0]["details"]

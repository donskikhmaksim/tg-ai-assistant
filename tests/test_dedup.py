from app.dedup import dedup_hash, normalize_task


def test_normalize_collapses_whitespace_and_case():
    assert normalize_task("  Прислать   Договор\n") == "прислать договор"


def test_same_task_same_hash_regardless_of_formatting():
    a = dedup_hash("user_1", "Прислать договор")
    b = dedup_hash("user_1", "  прислать   ДОГОВОР ")
    assert a == b


def test_different_chat_different_hash():
    assert dedup_hash("user_1", "Прислать договор") != dedup_hash(
        "user_2", "Прислать договор"
    )


def test_different_task_different_hash():
    assert dedup_hash("user_1", "Прислать договор") != dedup_hash(
        "user_1", "Позвонить клиенту"
    )

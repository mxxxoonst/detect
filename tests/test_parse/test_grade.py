"""测试 grade_parse: 分级解析路由."""

from pathlib import Path

from src.parse.grade import grade_parse


def _p(samples_dir: str, name: str) -> str:
    return str(Path(samples_dir) / name)


def test_grade_json_tier1(samples_dir):
    path = _p(samples_dir, "clean_users.json")
    grade = grade_parse(path, "json", "utf-8")
    assert grade.tier == 1
    assert grade.I == 1.0
    assert grade.fmt == "json"


def test_grade_csv_tier1(samples_dir):
    path = _p(samples_dir, "clean_users.csv")
    grade = grade_parse(path, "csv", "utf-8")
    assert grade.tier == 1
    assert grade.I == 1.0
    assert grade.fmt == "csv"


def test_grade_sqlite_tier1(samples_dir):
    path = _p(samples_dir, "clean_users.db")
    grade = grade_parse(path, "sqlite", "binary")
    assert grade.tier == 1
    assert grade.I == 1.0
    assert grade.fmt == "sqlite"


def test_grade_free_text(samples_dir):
    path = _p(samples_dir, "free_text_zh.txt")
    grade = grade_parse(path, "free_text", "utf-8")
    assert grade.tier == "free_text"
    assert grade.I is None


def test_grade_empty(samples_dir):
    path = _p(samples_dir, "empty.csv")
    grade = grade_parse(path, "empty", "utf-8")
    assert grade.tier == 3


def test_grade_binary(samples_dir):
    path = _p(samples_dir, "random.bin")
    grade = grade_parse(path, "binary_unknown", "binary")
    assert grade.tier == 3


def test_grade_json_tier2_incomplete(samples_dir):
    path = _p(samples_dir, "noisy_incomplete.json")
    grade = grade_parse(path, "json", "utf-8")
    # incomplete JSON → tier2 or tier3
    assert grade.tier in (2, 3)


def test_grade_csv_tier2_drift(samples_dir):
    path = _p(samples_dir, "noisy_column_drift.csv")
    grade = grade_parse(path, "free_text", "utf-8")
    assert grade.tier in ("free_text", 2)

"""测试 grade_parse: 分级解析路由."""

from pathlib import Path

import pytest

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


def test_grade_xlsx_tier1(samples_dir):
    pytest.importorskip("openpyxl")
    path = _p(samples_dir, "clean_users.xlsx")
    grade = grade_parse(path, "xlsx", "binary")
    assert grade.tier == 1
    assert grade.I == 1.0
    assert grade.fmt == "xlsx"


def test_grade_sql_dialect_tagged(samples_dir):
    """C0: SQL 文件 n_detail 始终带 dialect 标 (tier1 也有)。"""
    path = _p(samples_dir, "clean_schema.sql")
    grade = grade_parse(path, "sql", "utf-8")
    assert grade.n_detail is not None
    assert grade.n_detail["dialect"] == "sqlite"          # AUTOINCREMENT 标记
    assert grade.n_detail["dialect_status"] == "confident"


def test_grade_csv_header_collapse(make_temp_file):
    """表头塌成一列 + 数据正常分列 → tier2, n_detail.kind=header_col_mismatch (列名坍塌)。"""
    content = '"id,name,phone"\n1,Alice,13800000001\n2,Bob,13900000002\n3,Carol,13700000003\n'
    path = make_temp_file("header_collapse.csv", content)
    grade = grade_parse(path, "csv", "utf-8")
    assert grade.tier == 2
    assert grade.n_detail["kind"] == "header_col_mismatch"
    assert grade.n_detail["header_cols"] == 1


def test_detect_sql_dialect_statuses():
    """C0: 四种 dialect_status 的判定。"""
    from src.parse.sql_parser import _detect_sql_dialect

    mysql = _detect_sql_dialect("CREATE TABLE `t` (id INT AUTO_INCREMENT) ENGINE=InnoDB;")
    assert mysql["dialect"] == "mysql" and mysql["dialect_status"] == "confident"

    pg = _detect_sql_dialect("COPY public.users (id) FROM stdin;\n1\n\\.\n")
    assert pg["dialect"] == "postgres" and pg["dialect_status"] == "confident"

    ansi = _detect_sql_dialect("CREATE TABLE t (id INT); INSERT INTO t VALUES (1);")
    assert ansi["dialect"] == "ansi" and ansi["dialect_status"] == "unknown"

    amb = _detect_sql_dialect("SELECT `a`, [b] FROM t;")    # mysql 1 vs tsql 1
    assert amb["dialect_status"] == "ambiguous"

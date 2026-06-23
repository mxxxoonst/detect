"""Phase 3: I_strict 全格式贯通 + 跨格式一致性 (命门属性)。

命门 (docs/parser_strict_tolerant_design.md §0.3 / §1):
    strict_ok ⟺ I_strict==1 ⟺ deviations==0
本阶段 deviations 尚未形式化为独立字段 (Phase 4 抽 strict_ok/tolerant_parse 两入口),
故此处以可观测代理表达: 干净样本 tier1 ∧ I_strict==1; 含噪样本 I_strict<1。

覆盖六格式 (json/jsonl/csv/tsv/sql/xlsx) 的:
- 干净样本: tier==1 ∧ I_strict==1.0
- 含噪样本: tier==2 ∧ 0<=I_strict<1
- I_strict 在所有结构化格式都被回填 (非 None)
"""

import pytest

from src.parse.json_parser import parse_json, parse_jsonl
from src.parse.csv_parser import parse_csv, parse_tsv
from src.parse.sql_parser import parse_sql_text
from src.parse.xlsx_parser import parse_xlsx


# ── 干净样本: tier1 ⟺ I_strict==1 (六格式) ──────────────────────────────────
def test_clean_json_strict_one(make_temp_file):
    p = make_temp_file("c.json", '[{"id":1},{"id":2}]')
    g = parse_json(p, "utf-8")
    assert g.tier == 1 and g.I_strict == 1.0


def test_clean_jsonl_strict_one(make_temp_file):
    p = make_temp_file("c.jsonl", '{"a":1}\n{"a":2}\n')
    g = parse_jsonl(p, "utf-8")
    assert g.tier == 1 and g.I_strict == 1.0


def test_clean_csv_strict_one(make_temp_file):
    p = make_temp_file("c.csv", "id,name\n1,a\n2,b\n")
    g = parse_csv(p, "utf-8")
    assert g.tier == 1 and g.I_strict == 1.0


def test_clean_tsv_strict_one(make_temp_file):
    p = make_temp_file("c.tsv", "id\tname\n1\ta\n2\tb\n")
    g = parse_tsv(p, "utf-8")
    assert g.tier == 1 and g.I_strict == 1.0


def test_clean_sql_strict_one(make_temp_file):
    p = make_temp_file("c.sql", "CREATE TABLE t (id INT);\nINSERT INTO t VALUES (1);\n")
    g = parse_sql_text(p, "utf-8")
    assert g.tier == 1 and g.I_strict == 1.0


def test_clean_xlsx_strict_one(make_temp_file):
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "name"])
    ws.append([1, "a"])
    path = make_temp_file("c.xlsx", "")  # 先占位再覆写
    wb.save(path)
    g = parse_xlsx(path)
    assert g.tier == 1 and g.I_strict == 1.0


# ── 含噪样本: I_strict<1 (命门反向) ──────────────────────────────────────────
def test_noisy_json_strict_below_one(make_temp_file):
    p = make_temp_file("n.json", '[{"id":1},]')         # 尾逗号 → json5
    g = parse_json(p, "utf-8")
    assert g.tier == 2 and g.I_strict is not None and g.I_strict < 1.0


def test_noisy_jsonl_strict_below_one(make_temp_file):
    p = make_temp_file("n.jsonl", '{"a":1}\nBROKEN\n')
    g = parse_jsonl(p, "utf-8")
    assert g.tier == 2 and g.I_strict < 1.0


def test_noisy_csv_strict_below_one(make_temp_file):
    p = make_temp_file("n.csv", "id,name\n1,a\n2,b,c\n")  # 列漂移
    g = parse_csv(p, "utf-8")
    assert g.tier == 2 and g.I_strict < 1.0


def test_noisy_sql_strict_below_one(make_temp_file):
    p = make_temp_file("n.sql", "CREATE TABLE t (id INT);\nINSERT INTO t VALUES (1, 'oops")
    g = parse_sql_text(p, "utf-8")
    assert g.tier == 2 and g.I_strict < 1.0


# ── I_strict 全格式回填自检 ──────────────────────────────────────────────────
def test_i_strict_filled_all_formats(make_temp_file):
    """六格式结构化解析后 I_strict 必非 None (全格式贯通)。"""
    cases = [
        (parse_json, make_temp_file("a.json", '[{"x":1}]'), "utf-8"),
        (parse_jsonl, make_temp_file("a.jsonl", '{"x":1}\n'), "utf-8"),
        (parse_csv, make_temp_file("a.csv", "x,y\n1,2\n"), "utf-8"),
        (parse_tsv, make_temp_file("a.tsv", "x\ty\n1\t2\n"), "utf-8"),
        (parse_sql_text, make_temp_file("a.sql", "INSERT INTO t VALUES (1);\n"), "utf-8"),
    ]
    for fn, path, enc in cases:
        g = fn(path, enc)
        assert g.I_strict is not None, f"{fn.__name__} I_strict 未回填"

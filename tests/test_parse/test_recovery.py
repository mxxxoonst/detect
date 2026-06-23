"""Phase 4: strict_ok / tolerant_parse 两入口契约 + 共享内核一致性 + SQL 惰性流式。

覆盖 plan.md Phase 4 / design §0.3/§4/§5:
- 两入口契约: StrictVerdict / ParseResult 字段齐全
- 命门一致性: strict_ok(x).clean ⟺ tolerant_parse(x).report.deviations==0 ⟺ I_strict==1
- 干净/含噪样本两侧吻合 (六格式)
- SQL 惰性流式: iter_sql_file_statements 是生成器, parse_sql_text 不物化全部语句
"""

import types

import pytest

from src.parse.recovery import strict_ok, tolerant_parse
from src.parse.sql_strict import iter_sql_file_statements, scan_sql_statements


# ── 1. 两入口契约 ────────────────────────────────────────────────────────────
def test_strict_verdict_contract(make_temp_file):
    p = make_temp_file("c.json", '[{"id":1}]')
    v = strict_ok(p, "json", "utf-8")
    assert set(v.keys()) == {"clean", "reason", "n_unit"}
    assert v["clean"] is True
    assert v["reason"] == ""
    assert v["n_unit"] >= 1


def test_parse_result_contract(make_temp_file):
    p = make_temp_file("c.json", '[{"id":1}]')
    r = tolerant_parse(p, "json", "utf-8")
    assert set(r.keys()) == {"units", "raw_spans", "report"}
    rep = r["report"]
    assert set(rep.keys()) == {"C", "P", "L", "N", "I", "I_strict", "tier", "deviations"}
    assert rep["deviations"] == rep["P"] + rep["L"]


# ── 2. 命门一致性: clean ⟺ deviations==0 ⟺ I_strict==1 ───────────────────────
_CLEAN_CASES = [
    ("c.json", "json", '[{"id":1},{"id":2}]'),
    ("c.jsonl", "jsonl", '{"a":1}\n{"a":2}\n'),
    ("c.csv", "csv", "id,name\n1,a\n2,b\n"),
    ("c.tsv", "tsv", "id\tname\n1\ta\n"),
    ("c.sql", "sql", "CREATE TABLE t (id INT);\nINSERT INTO t VALUES (1);\n"),
]

_NOISY_CASES = [
    ("n.json", "json", '[{"id":1},]'),                 # 尾逗号
    ("n.jsonl", "jsonl", '{"a":1}\nBROKEN\n'),
    ("n.csv", "csv", "id,name\n1,a\n2,b,c\n"),          # 列漂移
    ("n.sql", "sql", "CREATE TABLE t (id INT);\nINSERT INTO t VALUES (1, 'oops"),
]


@pytest.mark.parametrize("name,fmt,content", _CLEAN_CASES)
def test_consistency_clean(make_temp_file, name, fmt, content):
    p = make_temp_file(name, content)
    v = strict_ok(p, fmt, "utf-8")
    r = tolerant_parse(p, fmt, "utf-8")
    # 命门三等价。
    assert v["clean"] is True
    assert r["report"]["deviations"] == 0
    assert r["report"]["I_strict"] == 1.0
    # clean ⟺ deviations==0
    assert v["clean"] == (r["report"]["deviations"] == 0)


@pytest.mark.parametrize("name,fmt,content", _NOISY_CASES)
def test_consistency_noisy(make_temp_file, name, fmt, content):
    p = make_temp_file(name, content)
    v = strict_ok(p, fmt, "utf-8")
    r = tolerant_parse(p, fmt, "utf-8")
    assert v["clean"] is False
    assert r["report"]["deviations"] > 0
    assert r["report"]["I_strict"] < 1.0
    # clean ⟺ deviations==0 (反向)
    assert v["clean"] == (r["report"]["deviations"] == 0)


def test_clean_xlsx_consistency(make_temp_file):
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.append(["id", "name"])
    path = make_temp_file("c.xlsx", "")
    wb.save(path)
    v = strict_ok(path, "xlsx", "binary")
    r = tolerant_parse(path, "xlsx", "binary")
    assert v["clean"] is True
    assert r["report"]["deviations"] == 0
    assert r["report"]["I_strict"] == 1.0


# ── 3. SQL 惰性流式: 不物化全部语句 ──────────────────────────────────────────
def test_iter_sql_is_generator(make_temp_file):
    p = make_temp_file("g.sql", "INSERT INTO t VALUES (1);\nINSERT INTO t VALUES (2);\n")
    it = iter_sql_file_statements(p, "utf-8")
    assert isinstance(it, types.GeneratorType), "应惰性生成器, 非物化 list"
    first = next(it)
    assert first.text.strip().startswith("INSERT")


def test_scan_sql_statements_lazy_not_materialized():
    """状态机消费惰性流: 喂一个无穷生成器, 取首句即停, 不应耗尽 (证明不物化全部)。"""
    consumed = {"n": 0}

    def _endless_chunks():
        # 不断产出合法语句; 若状态机物化全部会无限循环。
        while True:
            consumed["n"] += 1
            yield "INSERT INTO t VALUES (1);\n"

    gen = scan_sql_statements(_endless_chunks())
    first = next(gen)
    assert first.terminated is True
    # 只消费了有限个块即拿到首句 (惰性): 远小于「物化全部」所需。
    assert consumed["n"] < 100


def test_parse_sql_streaming_large(make_temp_file):
    """拼接较大 SQL 仍单遍惰性消费: C/P/L 计数正确, tier1。"""
    body = "".join(f"INSERT INTO t VALUES ({i}, 'v{i}');\n" for i in range(2000))
    p = make_temp_file("big.sql", body)
    from src.parse.sql_parser import parse_sql_text
    g = parse_sql_text(p, "utf-8")
    assert g.tier == 1
    assert g.I_strict == 1.0
    assert g.parsed["total_statements"] == 2000
    assert g.parsed["clean_statements"] == 2000

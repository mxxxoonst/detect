"""填值模式测试：模板定结构、LLM 只产值矩阵。

核心回归：模拟 LLM 掉字段（sch_00001 真实 bug——列清单 66 但 VALUES 仅 64），
验证模板逐字段兜底，最终结构仍 100% 完整（arity 恒等于字段数）。
"""

import json

import pytest

from src.synth.llm_render import (
    FakeFillLLMClient, _adaptive_llm_rows, _clean_example, _parse_value_matrix,
    build_value_prompt, make_fill_value_fn, render_llm_fill,
)
from src.synth.validate import validate_render


def _wide_unit(ncol, fmt="sql", pid="t_wide"):
    sk = {f"col_{i:02d}": {"depth": 1, "type": "str",
                           "samples": [f"ex{i}val"]} for i in range(ncol)}
    return {"id": f"sch_w{ncol}", "format": fmt, "partition_id": pid,
            "skeleton": sk, "record_count": 10}


def _named_unit():
    return {"id": "sch_n01", "format": "csv", "partition_id": "table",
            "skeleton": {
                "email":  {"depth": 1, "type": "str",
                           "samples": ["professorgoat@gmail.com…"]},  # 带截断尾标
                "age":    {"depth": 1, "type": "str", "samples": ["42"]},
                "country": {"depth": 1, "type": "str", "samples": ["AF", "A1"]},
            }, "record_count": 9}


# ── 示例清洗 / prompt ──────────────────────────────────────────────────────────

def test_clean_example_strips_truncation_and_skips_junk():
    assert _clean_example("professorgoat@gmail.com…") == "professorgoat@gmail.com"
    assert _clean_example("NULL") is None
    assert _clean_example(None) is None
    assert _clean_example("****@*.***") is None        # 全脱敏值不作示例


def test_build_value_prompt_has_fields_and_clean_examples():
    p = build_value_prompt(_named_unit(), n_rows=3)
    assert "array of exactly 3" in p
    assert "  email : str" in p
    assert "professorgoat@gmail.com" in p              # 示例已注入
    assert "…" not in p                                # 截断尾标已去


def test_build_value_prompt_omits_examples_when_too_wide():
    # 列极多 → 单字段示例预算 <6 → 省略示例，控 prompt token
    p = build_value_prompt(_wide_unit(600), n_rows=2)
    assert "字段过多，示例略" in p
    assert "ex0val" not in p          # 字段级样本值未注入（指令文案里的 e.g. 不算）


def test_adaptive_rows_shrinks_with_width():
    assert _adaptive_llm_rows(3, 3) == 3               # 窄表照常 3 行
    assert _adaptive_llm_rows(66, 3) == 3              # 128K 上下文+8192 输出，中宽表也够 3 行
    assert _adaptive_llm_rows(600, 3) < 3              # 极宽表仍减行防输出截断


# ── 值矩阵解析 ─────────────────────────────────────────────────────────────────

def test_parse_value_matrix_basic_and_tolerant():
    fields = ["a", "b"]
    t, n = _parse_value_matrix('[{"a":1,"b":2},{"a":3,"b":4}]', fields)
    assert n == 2 and t["a"] == [1, 3] and t["b"] == [2, 4]
    # 带围栏
    t2, _ = _parse_value_matrix('```json\n[{"a":9,"b":8}]\n```', fields)
    assert t2["a"] == [9]
    # 垃圾 → 空表（不崩）
    t3, n3 = _parse_value_matrix("not json at all", fields)
    assert t3 == {} and n3 == 0


def test_make_fill_value_fn_cursor_and_fallback():
    import random
    fn = make_fill_value_fn({"a": ["x", "y"]})
    rng = random.Random(0)
    assert fn("a", "str", None, rng) == "x"
    assert fn("a", "str", None, rng) == "y"
    assert fn("a", "str", None, rng) == "x"            # 游标循环
    assert isinstance(fn("b", "str", None, rng), str)  # 缺字段 → 模板兜底（不报错）


# ── 端到端：结构精确 + 掉值兜底（核心回归）──────────────────────────────────────

@pytest.mark.parametrize("fmt", ["csv", "sql"])
def test_fill_full_fidelity(fmt, tmp_path):
    unit = _wide_unit(20, fmt=fmt)
    doc, meta = render_llm_fill(unit, FakeFillLLMClient(), out_rows=3)
    res = validate_render(doc, unit, str(tmp_path))
    assert res["ok"] and res["jaccard"] == 1.0
    assert meta["n_missing"] == 0


def test_fill_absorbs_dropped_values(tmp_path):
    """LLM 掉末尾 2 个字段（=sch_00001 列 66/值 64 的偏差）→ 模板兜底 → 结构仍完整。"""
    unit = _wide_unit(66, fmt="sql", pid="t_amadb_user")
    doc, meta = render_llm_fill(unit, FakeFillLLMClient(drop_last=2), out_rows=3)
    res = validate_render(doc, unit, str(tmp_path))
    assert res["ok"], f"结构应完整，缺失={res['missing']}"
    assert res["recovered_path_count"] == 66           # 66 列一个不少
    assert meta["n_missing"] == 2                       # LLM 确实少给 2，但被吸收


def test_fill_sql_value_with_newline_roundtrips(tmp_path):
    """LLM 值含换行（中文自由文本）不得撑断单行 INSERT → 反解析仍恢复全列（sch_00001 真因）。"""
    from src.synth.llm_render import _parse_value_prompt

    class _NL:
        def complete(self, prompt):
            fields, _n = _parse_value_prompt(prompt)
            row = {f: ("第一行\n第二行" if i == 2 else f"v{i}")
                   for i, (f, t) in enumerate(fields)}
            return json.dumps([row], ensure_ascii=False)

    unit = _wide_unit(6, fmt="sql", pid="t_nl")
    doc, _meta = render_llm_fill(unit, _NL(), out_rows=2)
    assert "\n第二行" not in doc.split("VALUES", 1)[1]      # 值内换行已折叠
    res = validate_render(doc, unit, str(tmp_path))
    assert res["recovered_path_count"] == 6, res["missing"]
    assert res["ok"]


def test_fill_empty_llm_still_renders_structure(tmp_path):
    """LLM 返回空/垃圾 → 全字段模板兜底，结构依然 66 列（不崩、不缺结构）。"""
    class _Empty:
        def complete(self, prompt):
            return "garbage not json"
    unit = _wide_unit(66, fmt="sql")
    doc, meta = render_llm_fill(unit, _Empty(), out_rows=3,
                                fail_dump_dir=str(tmp_path / "failed"))
    res = validate_render(doc, unit, str(tmp_path))
    assert res["recovered_path_count"] == 66
    assert meta["filled_fields"] == 0
    assert (tmp_path / "failed" / "sch_w66.txt").exists()   # 原文已落盘

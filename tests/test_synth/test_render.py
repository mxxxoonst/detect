"""模板渲染 + 反解析保真校验测试（合成 unit，本地可跑，不依赖远程数据）。

覆盖：反折叠树形正确、五格式 render→parse 路径集 1:1 还原（jaccard=1）、
xlsx→csv 代理、值生成不回放截断/脱敏样本、确定可复现。
"""

import json

import pytest

from src.synth.render import _build_tree, render_unit, surface_format
from src.synth.validate import validate_render


def _json_unit():
    return {
        "id": "sch_j01", "source_file": "x.json", "format": "json",
        "partition_id": "uni_00",
        "skeleton": {
            "id":            {"depth": 1, "type": "int", "samples": []},
            "user.name":     {"depth": 2, "type": "str", "samples": []},
            "user.age":      {"depth": 2, "type": "int", "samples": []},
            "orders[].amt":  {"depth": 2, "type": "float", "samples": []},
            "orders[].oid":  {"depth": 2, "type": "str", "samples": []},
            "tags[]":        {"depth": 1, "type": "str", "samples": []},
        },
        "skeleton_count_B": 1, "record_count": 10,
    }


def _flat_unit(fmt, pid="table"):
    return {
        "id": f"sch_{fmt}01", "source_file": f"x.{fmt}", "format": fmt,
        "partition_id": pid,
        "skeleton": {
            "name":  {"depth": 1, "type": "str", "samples": []},
            "age":   {"depth": 1, "type": "int", "samples": []},
            "city":  {"depth": 1, "type": "str", "samples": []},
        },
        "record_count": 10,
    }


# ── 反折叠树 ──────────────────────────────────────────────────────────────────

def test_build_tree_inverts_folded_paths():
    tree = _build_tree(_json_unit()["skeleton"])
    ch = tree["children"]
    assert ch["id"]["kind"] == "leaf"
    assert ch["user"]["kind"] == "obj"
    assert set(ch["user"]["children"]) == {"name", "age"}
    assert ch["orders"]["kind"] == "list"
    assert set(ch["orders"]["elem"]["children"]) == {"amt", "oid"}
    assert ch["tags"]["kind"] == "list"
    assert ch["tags"]["elem"]["kind"] == "leaf"        # list-of-scalar


def test_render_json_reconstructs_nesting():
    text = render_unit(_json_unit())
    recs = json.loads(text)
    assert isinstance(recs, list) and recs
    r = recs[0]
    assert isinstance(r["user"], dict) and isinstance(r["user"]["name"], str)
    assert isinstance(r["orders"], list) and isinstance(r["orders"][0]["amt"], float)
    assert isinstance(r["tags"], list) and isinstance(r["tags"][0], str)


# ── 渲染→反解析：路径集 1:1 还原 ────────────────────────────────────────────────

@pytest.mark.parametrize("unit", [
    _json_unit(),
    {**_json_unit(), "id": "sch_jl1", "format": "jsonl"},
    _flat_unit("csv"),
    _flat_unit("tsv"),
    {**_flat_unit("sql", pid="users")},
    {**_flat_unit("xlsx", pid="Sheet1"), "id": "sch_xl1"},
])
def test_render_roundtrip_preserves_paths(unit, tmp_path):
    text = render_unit(unit)
    res = validate_render(text, unit, str(tmp_path))
    assert res["ok"], f"{unit['format']} missing={res['missing']} extra={res['extra']}"
    assert res["jaccard"] == 1.0


def test_xlsx_renders_to_csv_surface():
    unit = {**_flat_unit("xlsx", pid="Sheet1"), "id": "sch_xl2"}
    assert surface_format("xlsx") == "csv"
    text = render_unit(unit)
    # csv 代理：首行表头含列名，逗号分隔
    head = text.splitlines()[0]
    assert set(head.split(",")) == {"name", "age", "city"}


# ── 值生成：不回放截断/脱敏样本 ─────────────────────────────────────────────────

def test_value_faker_never_replays_dirty_samples():
    unit = _flat_unit("csv")
    unit["skeleton"]["note"] = {
        "depth": 1, "type": "str",
        "samples": ["verylongtruncatedheadvalue…", "****@*.***"],
    }
    text = render_unit(unit)
    assert "…" not in text            # 截断哨兵不得出现
    assert "****" not in text         # 脱敏星号串不得出现


# ── 确定可复现 ────────────────────────────────────────────────────────────────

def test_render_is_deterministic():
    unit = _json_unit()
    assert render_unit(unit) == render_unit(unit)
    assert render_unit(unit, seed=7) == render_unit(unit, seed=7)
    assert render_unit(unit, seed=7) != render_unit(unit, seed=8)

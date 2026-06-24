"""Q1 union-schema 兼容性合并 + JSONL-as-.json 路由测试。

验证签名爆炸消除：DataPart 的 test1/2/3.json 各收敛到 1 分片；真异质 mix 正确分 2；
JSONL-as-.json (test1) 不被静默丢成 0 分片。
"""

import json
from pathlib import Path

import pytest

from src.extract.schema_partition import partition_file
from src.extract.skeleton import (
    UnionSchemaClusterer,
    compatible,
    leaf_types,
    norm_type,
    record_leaf_types,
)
from src.parse.grade import Grade

DATAPART = Path(__file__).resolve().parents[2] / "test_data" / "DataPart"


def _grade(path: str, fmt: str = "json") -> Grade:
    return Grade(tier=1, I=1.0, fmt=fmt, encoding="utf-8", path=path)


def _part_count(path: str) -> int:
    parts, _ = partition_file(_grade(path))
    return len(parts)


# ── 归一化原语 ────────────────────────────────────────────────────────────────

class TestNormType:
    def test_null_is_wildcard(self):
        assert norm_type(None) is None

    def test_int_and_float_merge_to_num(self):
        assert norm_type(1) == "num"
        assert norm_type(1.5) == "num"

    def test_bool_before_int(self):
        assert norm_type(True) == "bool"

    def test_str_obj_arr(self):
        assert norm_type("x") == "str"
        assert norm_type({"a": 1}) == "obj"
        assert norm_type([1]) == "arr"


class TestLeafTypes:
    def test_empty_containers_skipped(self):
        out: dict = {}
        leaf_types({"a": {}, "b": [], "c": 1}, "", out)
        # 空 {} / 空 [] 不携带 schema 信息 → 只剩 c
        assert set(out.keys()) == {"c"}
        assert out["c"] == {"num"}

    def test_list_takes_union_not_first(self):
        out: dict = {}
        # list 元素类型不同 → 取并 (非只首元素)
        leaf_types({"xs": [1, "a"]}, "", out)
        assert out["xs[]"] == {"num", "str"}

    def test_null_not_recorded(self):
        out: dict = {}
        leaf_types({"a": None, "b": 1}, "", out)
        assert "a" not in out
        assert out["b"] == {"num"}


class TestCompatible:
    def test_disjoint_keys_compatible(self):
        # 键集不相交 → 无共享路径 → 兼容 (可选键)
        a = record_leaf_types({"x": 1})
        b = record_leaf_types({"y": "s"})
        assert compatible(a, b)

    def test_shared_path_type_conflict_incompatible(self):
        a = record_leaf_types({"id": 1})
        b = record_leaf_types({"id": "x"})
        # int→num vs str → 共享路径 isdisjoint → 不兼容
        assert not compatible(a, b)

    def test_int_float_no_conflict(self):
        a = record_leaf_types({"v": 1})
        b = record_leaf_types({"v": 2.5})
        assert compatible(a, b)


class TestClusterer:
    def test_optional_fields_single_cluster_with_presence_rate(self):
        c = UnionSchemaClusterer()
        for rec in [{"id": 1, "name": "A"},
                    {"id": 2, "name": "B", "phone": "x"},
                    {"id": 3, "name": "C"}]:
            c.add(rec)
        assert len(c.protos) == 1
        occ = c.occurrence(0)
        assert occ["id"] == 1.0
        assert occ["name"] == 1.0
        assert occ["phone"] == pytest.approx(1 / 3, abs=1e-3)

    def test_real_conflict_two_clusters(self):
        c = UnionSchemaClusterer()
        c.add({"id": "u1", "email": "a@x"})
        c.add({"id": 100, "amount": 9.9})
        assert len(c.protos) == 2


# ── DataPart 收敛验证 (本地小样本, ijson 已装) ──────────────────────────────────

@pytest.mark.skipif(not DATAPART.exists(), reason="DataPart 样本不存在")
class TestDataPartConvergence:
    def test_test1_single_partition(self):
        # test1.json 实为 JSONL-as-.json (逐行独立对象) → 探测转 JSONL → 1 分片(不丢)
        assert _part_count(str(DATAPART / "test1.json")) == 1

    def test_test2_single_partition(self):
        # test2.json 为拼接多行对象 (`},\n{`) + 尾截断 → 流式增量恢复 → union-schema 1 分片
        assert _part_count(str(DATAPART / "test2.json")) == 1

    def test_test3_single_partition(self):
        # test3.json: 4734 条同质 Mongo 导出, 旧精确签名炸成 1253 片 → union-schema 1 片
        assert _part_count(str(DATAPART / "test3.json")) == 1

    def test_test3_occurrence_has_optional_fields(self):
        parts, _ = partition_file(_grade(str(DATAPART / "test3.json")))
        occ = parts[0]["occurrence"]
        # 同质导出含可选字段 → 应有 presence-rate < 1.0 的字段 (非全占位 1.0)
        assert any(v < 1.0 for v in occ.values())


# ── 真异质 mix (合成) → 正确分 2 ──────────────────────────────────────────────

class TestHeterogeneousMix:
    def test_users_orders_conflict_splits(self, tmp_path):
        mix = [
            {"id": "u-1", "name": "Alice", "email": "a@x.com"},
            {"id": "u-2", "name": "Bob", "email": "b@x.com"},
            {"id": 1001, "amount": 9.9, "status": "paid"},
            {"id": 1002, "amount": 3.5, "status": "pending"},
        ]
        p = tmp_path / "mix.json"
        p.write_text(json.dumps(mix), encoding="utf-8")
        parts, stats = partition_file(_grade(str(p)))
        assert len(parts) == 2
        assert stats["method"] == "union_schema"

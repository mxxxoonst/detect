"""Q2 CSV schema 级去重测试（保留重数）。

验证：split-dump 家族折叠成预期 distinct schema 数、无列名编号文件（含空 cell null
多态）折叠成 1、真 singleton 不被误并、cluster_size 频率权重正确、quote 感知切列。
测试数据由 generate.gen_csv_dedup_samples 现场生成到 tmp（可复现，带 seed）。
"""

import importlib.util
import random
from pathlib import Path

import pytest

from src.extract.schema_dedup import (
    _cell_class,
    _v_compatible,
    csv_fingerprint,
    dedup_csv_schemas,
)

GEN_PY = Path(__file__).resolve().parents[2] / "test_data" / "generate.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_dedup", GEN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def csv_samples(tmp_path):
    """现场生成 Q2 CSV 去重样本到 tmp（seed 固定 → 可复现）。"""
    random.seed(42)
    gen = _load_generator()
    out = tmp_path / "csv_dedup"
    gen.gen_csv_dedup_samples(out)
    return out


def _units(csv_dir: Path):
    return [
        {"id": f"sch_{i:05d}", "source_file": str(p), "format": "csv"}
        for i, p in enumerate(sorted(csv_dir.glob("*.csv")))
    ]


# ── 列宏类 / 兼容性原语 ──────────────────────────────────────────────────────

class TestCellClass:
    def test_empty_is_wildcard(self):
        assert _cell_class("") is None
        assert _cell_class("   ") is None

    def test_pure_number_is_num(self):
        assert _cell_class("12345") == "num"
        assert _cell_class("12,345") == "num"

    def test_letters_are_str(self):
        assert _cell_class("AT&T") == "str"
        assert _cell_class("a@x.com") == "str"


class TestVCompatible:
    def test_empty_set_does_not_block(self):
        # 某列一方为空集（采样全空）→ 不挡合并（列层 null 多态）
        a = ("V", 3, (frozenset({"num"}), frozenset(), frozenset({"str"})))
        b = ("V", 3, (frozenset({"num"}), frozenset({"str"}), frozenset({"str"})))
        assert _v_compatible(a, b)

    def test_conflict_blocks(self):
        a = ("V", 2, (frozenset({"num"}), frozenset({"str"})))
        b = ("V", 2, (frozenset({"str"}), frozenset({"str"})))
        assert not _v_compatible(a, b)

    def test_diff_col_count_incompatible(self):
        a = ("V", 2, (frozenset({"num"}), frozenset({"str"})))
        b = ("V", 3, (frozenset({"num"}), frozenset({"str"}), frozenset({"num"})))
        assert not _v_compatible(a, b)


# ── 端到端去重 ────────────────────────────────────────────────────────────────

class TestDedupEndToEnd:
    def test_distinct_schema_count(self, csv_samples):
        rep = dedup_csv_schemas(_units(csv_samples))
        # 17 文件 → 6 distinct schema（carriers×5, devices×3, 编号×6, 3 singleton）
        assert rep["total_csv_units"] == 17
        assert rep["distinct_schemas"] == 6

    def test_carriers_family_folds(self, csv_samples):
        rep = dedup_csv_schemas(_units(csv_samples))
        clu = self._cluster_with_file(rep, "ad_line_carriers.csv")
        assert clu["cluster_size"] == 5
        assert clu["has_header"] is True

    def test_devices_family_folds(self, csv_samples):
        rep = dedup_csv_schemas(_units(csv_samples))
        clu = self._cluster_with_file(rep, "ad_line_devices.csv")
        assert clu["cluster_size"] == 3

    def test_numbered_headerless_with_null_polymorphism_folds(self, csv_samples):
        rep = dedup_csv_schemas(_units(csv_samples))
        clu = self._cluster_with_file(rep, "1.csv")
        # 6 个编号文件含空 cell（列层 null 多态），空当通配 → 全并成 1
        assert clu["cluster_size"] == 6
        assert clu["has_header"] is False

    def test_singletons_not_merged(self, csv_samples):
        rep = dedup_csv_schemas(_units(csv_samples))
        for fname in ("report_two_col.csv", "ad_groups_five_col.csv",
                      "lonely_headerless.csv"):
            clu = self._cluster_with_file(rep, fname)
            assert clu["cluster_size"] == 1

    def test_cluster_size_sums_to_total(self, csv_samples):
        rep = dedup_csv_schemas(_units(csv_samples))
        assert sum(c["cluster_size"] for c in rep["clusters"]) == rep["total_csv_units"]

    def test_non_csv_units_skipped(self, csv_samples):
        units = _units(csv_samples) + [
            {"id": "sch_99999", "source_file": "x.json", "format": "json"}
        ]
        rep = dedup_csv_schemas(units)
        assert rep["total_csv_units"] == 17  # json 被跳过

    @staticmethod
    def _cluster_with_file(rep, basename: str):
        for c in rep["clusters"]:
            if any(Path(f).name == basename for f in c["member_files"]):
                return c
        raise AssertionError(f"no cluster contains {basename}")


# ── quote 感知切列 ───────────────────────────────────────────────────────────

class TestQuoteAware:
    def test_quoted_separator_not_split(self, tmp_path):
        # 引号内逗号不应被切列：4 列而非 6 列
        p = tmp_path / "q.csv"
        p.write_text('330000,330003,2,"12,14,26"\n330001,330004,3,"1,2"\n',
                     encoding="utf-8")
        fp, has_hdr, _ = csv_fingerprint(str(p), ",", "utf-8")
        assert fp[0] == "V"
        assert fp[1] == 4  # 众数列数 = 4（quote 感知）

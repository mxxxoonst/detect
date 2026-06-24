"""DataPart 真实小样本（tracked）端到端覆盖。

Amart_CSC_VOC_20220506.csv（干净 tier1 CSV，29 列含 PII）与 test1/2/3.json
（union-schema 收敛对象）走 partition → build_schema_unit 的真实路径。
"""

from pathlib import Path

import pytest

from src.extract.schema_dedup import csv_fingerprint, dedup_csv_schemas
from src.extract.schema_partition import partition_file, _sniff_sep
from src.extract.schema_unit import build_schema_unit, reset_unit_counter
from src.parse.grade import Grade

DATAPART = Path(__file__).resolve().parents[2] / "test_data" / "DataPart"
AMART = DATAPART / "Amart_CSC_VOC_20220506.csv"

pytestmark = pytest.mark.skipif(not DATAPART.exists(), reason="DataPart 样本不存在")


def _grade(path: Path, fmt: str) -> Grade:
    return Grade(tier=1, I=1.0, fmt=fmt, encoding="utf-8", path=str(path))


class TestAmartCsv:
    def test_single_partition(self):
        parts, stats = partition_file(_grade(AMART, "csv"))
        assert len(parts) == 1
        assert stats["method"] == "single"

    def test_schema_unit_has_pii_fields(self):
        reset_unit_counter()
        parts, _ = partition_file(_grade(AMART, "csv"))
        unit = build_schema_unit(parts[0])
        # 表头含 EMAIL / MOBILE / FIRST_NAME → 应有 PII 种子（high_conf）
        pii_keys = {
            info["key_name"]
            for info in unit["fields"].values()
            if info["pii_seed"] and info["pii_seed"][0] == "high_conf"
        }
        assert pii_keys  # 至少命中一个高置信 PII 字段

    def test_fingerprint_has_header(self):
        sep = _sniff_sep(str(AMART), "utf-8")
        fp, has_hdr, _ = csv_fingerprint(str(AMART), sep, "utf-8")
        assert has_hdr is True
        assert fp[0] == "H"
        assert "email" in fp[1]


class TestDataPartJsonConvergence:
    @pytest.mark.parametrize("fname", ["test1.json", "test2.json", "test3.json"])
    def test_each_converges_to_one_partition(self, fname):
        parts, stats = partition_file(_grade(DATAPART / fname, "json"))
        assert len(parts) == 1
        assert stats["method"] == "union_schema"

    def test_build_schema_unit_fills_occurrence(self):
        reset_unit_counter()
        parts, _ = partition_file(_grade(DATAPART / "test3.json", "json"))
        unit = build_schema_unit(parts[0])
        # occurrence 真值落到 fields（含可选字段 < 1.0）
        occs = [info["occurrence"] for info in unit["fields"].values()]
        assert any(o < 1.0 for o in occs)
        # required 与 occurrence>=0.9 一致
        for info in unit["fields"].values():
            assert info["required"] == (info["occurrence"] >= 0.9)


class TestDedupIncludesAmart:
    def test_amart_is_own_cluster(self):
        units = [{"id": "sch_00001", "source_file": str(AMART), "format": "csv"}]
        rep = dedup_csv_schemas(units)
        assert rep["distinct_schemas"] == 1
        assert rep["clusters"][0]["has_header"] is True
        assert rep["clusters"][0]["cluster_size"] == 1

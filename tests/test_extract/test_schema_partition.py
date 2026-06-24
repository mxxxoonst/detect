"""schema_partition 测试：文件内 Schema 分片。"""

import pytest

from src.extract.schema_partition import partition_file
from src.parse.grade import Grade


def _make_grade(path: str, fmt: str, enc: str = "utf-8") -> Grade:
    """构造最小 Grade 对象供测试使用。"""
    return Grade(tier=1, I=1.0, fmt=fmt, encoding=enc, path=path)


def _consume(partition) -> list:
    """将 partition 的 record_iter 全部消费为 list。"""
    return list(partition["record_iter"])


# ── xlsx: 每 sheet 一个 partition ─────────────────────────────────────────────

class TestXlsx:
    def _make_xlsx(self, tmp_path) -> str:
        openpyxl = pytest.importorskip("openpyxl")
        p = tmp_path / "t.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "people"
        ws.append(["name", "phone"])
        ws.append(["Alice", "13800000001"])
        ws.append(["Bob", "13900000002"])
        wb.save(str(p))
        return str(p)

    def test_one_partition_per_sheet(self, tmp_path):
        grade = _make_grade(self._make_xlsx(tmp_path), "xlsx", "binary")
        parts, stats = partition_file(grade)
        assert stats["format"] == "xlsx"
        assert len(parts) == 1
        assert parts[0]["partition_id"] == "people"

    def test_rows_zip_headers(self, tmp_path):
        grade = _make_grade(self._make_xlsx(tmp_path), "xlsx", "binary")
        parts, _ = partition_file(grade)
        rows = _consume(parts[0])
        assert rows[0]["name"] == "Alice"
        assert rows[0]["phone"] == "13800000001"


# ── JSON: 显式包装 key ────────────────────────────────────────────────────────

class TestJsonExplicitKey:
    def test_produces_two_partitions(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, stats = partition_file(grade)

        assert len(parts) == 2
        ids = {p["partition_id"] for p in parts}
        assert ids == {"users", "orders"}

    def test_stats_method_is_explicit_key(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        _, stats = partition_file(grade)

        assert stats["method"] == "explicit_key"
        assert stats["partition_count"] == 2

    def test_users_partition_records(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        users_part = next(p for p in parts if p["partition_id"] == "users")
        records = _consume(users_part)

        assert len(records) == 3
        assert records[0]["name"] == "Alice"

    def test_orders_partition_records(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        orders_part = next(p for p in parts if p["partition_id"] == "orders")
        records = _consume(orders_part)

        assert len(records) == 3
        assert records[0]["order_id"] == "OD001"

    def test_partition_field_paths_initialized_empty(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        # field_paths 仍由 build_schema_unit 回填；occurrence 已由 union-schema 聚类填真值。
        for p in parts:
            assert p["field_paths"] == set()

    def test_partition_occurrence_populated(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        # 全字段在每条记录都出现 → presence-rate == 1.0（union-schema 真值，非占位）。
        users_part = next(p for p in parts if p["partition_id"] == "users")
        assert users_part["occurrence"]  # 非空
        assert all(v == 1.0 for v in users_part["occurrence"].values())

    def test_format_is_json(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["format"] == "json"


# ── JSON: 骨架聚类兜底 ────────────────────────────────────────────────────────

class TestJsonUnionSchema:
    """union-schema 兼容性合并（Q1）：可选/缺键不分片；共享路径真冲突才分。"""

    def test_disjoint_keys_merge_into_one(self, fixtures_dir):
        # array_mixed: users(id/name/email) 与 products(product_id/price/category)
        # 键集不相交、无共享路径冲突 → union-schema 合并为 1（消除签名爆炸的核心）。
        grade = _make_grade(str(fixtures_dir / "array_mixed.json"), "json")
        parts, stats = partition_file(grade)

        assert len(parts) == 1
        assert stats["method"] == "union_schema"

    def test_partition_ids_start_with_uni(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "array_mixed.json"), "json")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["partition_id"].startswith("uni_")

    def test_shared_path_conflict_splits_into_two(self, fixtures_dir):
        # array_conflict: 两族都有 'id'，但一族 id 为 str、另一族为 num → 真冲突 → 分 2。
        grade = _make_grade(str(fixtures_dir / "array_conflict.json"), "json")
        parts, stats = partition_file(grade)

        assert len(parts) == 2
        assert stats["method"] == "union_schema"
        key_sets = []
        for p in parts:
            records = _consume(p)
            key_sets.append(set().union(*[set(r.keys()) for r in records]))
        # 一族含 email（users）、另一族含 amount（orders）
        assert any("email" in ks for ks in key_sets)
        assert any("amount" in ks for ks in key_sets)

    def test_noisy_flag_is_false(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "array_mixed.json"), "json")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["noisy"] is False


# ── JSONL ─────────────────────────────────────────────────────────────────────

class TestJsonl:
    def test_single_schema_single_partition(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "users.jsonl"), "jsonl")
        parts, stats = partition_file(grade)

        assert len(parts) == 1
        assert stats["method"] == "union_schema"

    def test_all_records_loaded(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "users.jsonl"), "jsonl")
        parts, _ = partition_file(grade)

        records = _consume(parts[0])
        assert len(records) == 5
        assert all("name" in r for r in records)


# ── CSV 稳定列 ────────────────────────────────────────────────────────────────

class TestCsvStable:
    def test_single_partition(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "stable.csv"), "csv")
        parts, stats = partition_file(grade)

        assert len(parts) == 1
        assert stats["method"] == "single"
        assert parts[0]["partition_id"] == "table"

    def test_not_noisy(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "stable.csv"), "csv")
        parts, _ = partition_file(grade)

        assert parts[0]["noisy"] is False

    def test_records_have_all_columns(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "stable.csv"), "csv")
        parts, _ = partition_file(grade)

        records = _consume(parts[0])
        assert len(records) == 5
        assert set(records[0].keys()) == {"id", "name", "phone", "email", "address"}

    def test_stats_partition_count(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "stable.csv"), "csv")
        _, stats = partition_file(grade)

        assert stats["partition_count"] == 1
        assert stats["format"] == "csv"


# ── CSV 列不稳定（噪声）────────────────────────────────────────────────────────

class TestCsvNoisy:
    def test_single_partition_still_created(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "noisy_cols.csv"), "csv")
        parts, _ = partition_file(grade)

        assert len(parts) == 1

    def test_noisy_flag_set(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "noisy_cols.csv"), "csv")
        parts, _ = partition_file(grade)

        assert parts[0]["noisy"] is True


# ── SQL 文本 ──────────────────────────────────────────────────────────────────

class TestSql:
    def test_two_tables_two_partitions(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "two_tables.sql"), "sql")
        parts, stats = partition_file(grade)

        assert len(parts) == 2
        assert stats["method"] == "table_name"

    def test_partition_ids_are_table_names(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "two_tables.sql"), "sql")
        parts, _ = partition_file(grade)

        ids = {p["partition_id"] for p in parts}
        assert ids == {"users", "orders"}

    def test_users_records_have_correct_fields(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "two_tables.sql"), "sql")
        parts, _ = partition_file(grade)

        users_part = next(p for p in parts if p["partition_id"] == "users")
        records = _consume(users_part)

        assert len(records) == 3
        assert "name" in records[0]
        assert "phone" in records[0]

    def test_orders_records_have_correct_fields(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "two_tables.sql"), "sql")
        parts, _ = partition_file(grade)

        orders_part = next(p for p in parts if p["partition_id"] == "orders")
        records = _consume(orders_part)

        assert len(records) == 3
        assert "order_id" in records[0]
        assert "amount" in records[0]


# ── PartitionStats 完整性 ─────────────────────────────────────────────────────

class TestPartitionStats:
    def test_stats_fields_complete(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        _, stats = partition_file(grade)

        required_keys = {"source_file", "format", "partition_count", "partition_ids", "method"}
        assert required_keys.issubset(stats.keys())

    def test_partition_ids_match_parts(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "two_tables.sql"), "sql")
        parts, stats = partition_file(grade)

        assert set(stats["partition_ids"]) == {p["partition_id"] for p in parts}

    def test_unknown_format_returns_empty(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "stable.csv"), "log")
        parts, stats = partition_file(grade)

        assert len(parts) == 0
        assert stats["method"] == "unknown"

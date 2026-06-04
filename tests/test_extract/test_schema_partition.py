"""schema_partition 测试：文件内 Schema 分片。"""


from src.extract.schema_partition import partition_file
from src.parse.grade import Grade


def _make_grade(path: str, fmt: str, enc: str = "utf-8") -> Grade:
    """构造最小 Grade 对象供测试使用。"""
    return Grade(tier=1, I=1.0, fmt=fmt, encoding=enc, path=path)


def _consume(partition) -> list:
    """将 partition 的 record_iter 全部消费为 list。"""
    return list(partition["record_iter"])


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

    def test_partition_fields_initialized_empty(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["field_paths"] == set()
            assert p["occurrence"] == {}

    def test_format_is_json(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "explicit_keys.json"), "json")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["format"] == "json"


# ── JSON: 骨架聚类兜底 ────────────────────────────────────────────────────────

class TestJsonSkeletonCluster:
    def test_two_skeletons_produce_two_partitions(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "array_mixed.json"), "json")
        parts, stats = partition_file(grade)

        assert len(parts) == 2
        assert stats["method"] == "skeleton_cluster"

    def test_partition_ids_start_with_sig(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "array_mixed.json"), "json")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["partition_id"].startswith("sig_")
            assert len(p["partition_id"]) == 12  # "sig_" + 8 hex chars

    def test_records_grouped_by_schema(self, fixtures_dir):
        grade = _make_grade(str(fixtures_dir / "array_mixed.json"), "json")
        parts, _ = partition_file(grade)

        all_keys = set()
        for p in parts:
            records = _consume(p)
            for rec in records:
                all_keys.update(rec.keys())

        assert "name" in all_keys
        assert "price" in all_keys

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
        assert stats["method"] == "skeleton_cluster"

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


# ── SQLite ────────────────────────────────────────────────────────────────────

class TestSqlite:
    def test_three_tables_three_partitions(self, three_table_db):
        grade = _make_grade(three_table_db, "sqlite")
        parts, stats = partition_file(grade)

        assert len(parts) == 3
        assert stats["method"] == "table_name"

    def test_partition_ids_are_table_names(self, three_table_db):
        grade = _make_grade(three_table_db, "sqlite")
        parts, _ = partition_file(grade)

        ids = {p["partition_id"] for p in parts}
        assert ids == {"users", "orders", "products"}

    def test_row_data_is_correct(self, three_table_db):
        grade = _make_grade(three_table_db, "sqlite")
        parts, _ = partition_file(grade)

        users_part = next(p for p in parts if p["partition_id"] == "users")
        records = _consume(users_part)

        assert len(records) == 3
        names = [r["name"] for r in records]
        assert "Alice" in names

    def test_products_partition_has_price(self, three_table_db):
        grade = _make_grade(three_table_db, "sqlite")
        parts, _ = partition_file(grade)

        prod_part = next(p for p in parts if p["partition_id"] == "products")
        records = _consume(prod_part)

        assert len(records) == 3
        assert "price" in records[0]

    def test_format_is_sqlite(self, three_table_db):
        grade = _make_grade(three_table_db, "sqlite")
        parts, _ = partition_file(grade)

        for p in parts:
            assert p["format"] == "sqlite"


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

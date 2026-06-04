"""build_schema_unit 测试：SchemaPartition → SchemaUnit。"""

import pytest

from src.extract.schema_types import SchemaPartition
from src.extract.schema_unit import build_schema_unit, reset_unit_counter


def _make_partition(partition_id, records, fmt="json", source_file="test.json", noisy=False)->SchemaPartition:
    """构造最小 SchemaPartition 供测试使用。"""
    return SchemaPartition(
        source_file=source_file,
        format= fmt,
        partition_id=partition_id,
        field_paths=set(),
        occurrence={},
        noisy=noisy,
        record_iter=iter(records))


@pytest.fixture(autouse=True)
def reset_counter():
    """每个测试前重置全局计数器，保证 ID 可预测。"""
    reset_unit_counter()
    yield


_SIMPLE_RECORDS = [
    {"id": 1, "name": "Alice", "phone": "13812345678"},
    {"id": 2, "name": "Bob",   "phone": "13987654321"},
    {"id": 3, "name": "Carol", "phone": "13700000001"},
]


# ── Unit ID ───────────────────────────────────────────────────────────────────

class TestUnitId:
    def test_first_unit_id_format(self):
        part= _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        assert unit["id"] == "sch_00001"

    def test_second_unit_id_increments(self):
        part1 = _make_partition("users", list(_SIMPLE_RECORDS))
        part2 = _make_partition("orders", [{"order_id": "OD001", "amount": 99.9}])
        u1 = build_schema_unit(part1)
        u2 = build_schema_unit(part2)
        assert u1["id"] == "sch_00001"
        assert u2["id"] == "sch_00002"

    def test_unit_id_preserved_in_output(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        assert unit["source_file"] == "test.json"
        assert unit["format"] == "json"
        assert unit["partition_id"] == "users"


# ── field_id ──────────────────────────────────────────────────────────────────

class TestFieldId:
    def test_single_field_id_format(self):
        part = _make_partition("u", [{"name": "Alice"}])
        unit = build_schema_unit(part)
        field = unit["fields"]["name"]
        assert field["field_id"] == "f_00001_01"

    def test_field_ids_sequential_within_unit(self):
        # id, name, phone 按字典序排列，对应 01, 02, 03
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        fids = sorted(f["field_id"] for f in unit["fields"].values())
        assert fids[0] == "f_00001_01"
        assert fids[1] == "f_00001_02"
        assert fids[2] == "f_00001_03"

    def test_field_id_uses_unit_seq(self):
        # 第二个 unit 的 field_id 前缀变为 00002
        build_schema_unit(_make_partition("skip", [{"x": 1}]))
        part = _make_partition("users", [{"name": "Alice"}])
        unit = build_schema_unit(part)
        field = unit["fields"]["name"]
        assert field["field_id"].startswith("f_00002_")


# ── record_count ──────────────────────────────────────────────────────────────

class TestRecordCount:
    def test_record_count_matches_input(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        assert unit["record_count"] == 3

    def test_single_record_count(self):
        part = _make_partition("u", [{"id": 1}])
        unit = build_schema_unit(part)
        assert unit["record_count"] == 1


# ── 空 partition ──────────────────────────────────────────────────────────────

class TestEmptyPartition:
    def test_empty_iter_returns_minimal_unit(self):
        part = _make_partition("empty", [])
        unit = build_schema_unit(part)
        assert unit["skeleton"] == []
        assert unit["fields"] == {}
        assert unit["record_count"] == 0

    def test_empty_unit_still_has_id(self):
        part = _make_partition("empty", [])
        unit = build_schema_unit(part)
        assert unit["id"] == "sch_00001"

    def test_empty_unit_preserves_format(self):
        part = _make_partition("empty", [], fmt="csv")
        unit = build_schema_unit(part)
        assert unit["format"] == "csv"

    def test_empty_unit_skeleton_count_is_zero(self):
        part = _make_partition("empty", [])
        unit = build_schema_unit(part)
        assert unit["skeleton_count_B"] == 0


# ── PII 检测 ──────────────────────────────────────────────────────────────────

class TestPiiDetection:
    def test_phone_field_is_high_conf(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        pii = unit["fields"]["phone"]["pii_seed"]
        assert pii is not None
        assert pii[0] == "high_conf"

    def test_phone_pii_type(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        pii = unit["fields"]["phone"]["pii_seed"]
        assert pii[1] == "phone_number"

    def test_email_field_pii(self):
        records = [{"email": "alice@example.com", "name": "Alice"}]
        part = _make_partition("u", records)
        unit = build_schema_unit(part)
        pii = unit["fields"]["email"]["pii_seed"]
        assert pii == ("high_conf", "email")

    def test_name_field_is_person_name(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        pii = unit["fields"]["name"]["pii_seed"]
        assert pii == ("high_conf", "person_name")

    def test_non_pii_field_is_none(self):
        # 'id' 不在 PII 关键词列表中
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        pii = unit["fields"]["id"]["pii_seed"]
        assert pii is None

    def test_password_field_is_credential(self):
        records = [{"password": "hashed_value_123"}]
        part = _make_partition("u", records)
        unit = build_schema_unit(part)
        pii = unit["fields"]["password"]["pii_seed"]
        assert pii[0] == "high_conf"
        assert pii[1] == "credential"


# ── occurrence / required ─────────────────────────────────────────────────────

class TestOccurrence:
    def test_all_fields_occurrence_is_one(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        for fi in unit["fields"].values():
            assert fi["occurrence"] == 1.0

    def test_required_true_when_occurrence_at_one(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        for fi in unit["fields"].values():
            assert fi["required"] is True

    def test_key_name_matches_field_name(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        # 平坦结构: key_name 等于路径（字段名）
        for path, fi in unit["fields"].items():
            assert fi["key_name"] == path


# ── skeleton ──────────────────────────────────────────────────────────────────

class TestSkeleton:
    def test_skeleton_nonempty_for_records(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        assert len(unit["skeleton"]) > 0

    def test_skeleton_is_list_of_pairs(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        for item in unit["skeleton"]:
            assert len(item) == 2  # (path, dtype)

    def test_skeleton_count_b_is_one_for_uniform_records(self):
        # 所有记录骨架相同时 B=1
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        assert unit["skeleton_count_B"] == 1

    def test_skeleton_count_b_reflects_mixed_shapes(self):
        # 两种不同结构
        mixed = [
            {"id": 1, "name": "Alice"},
            {"product_id": "P1", "price": 9.9},
        ]
        part = _make_partition("mixed", mixed)
        unit = build_schema_unit(part)
        assert unit["skeleton_count_B"] == 2

    def test_skeleton_counts_dict_populated(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        assert len(unit["skeleton_counts"]) >= 1


# ── 拓扑 ──────────────────────────────────────────────────────────────────────

class TestTopology:
    def test_topology_nonempty_when_not_noisy(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS), noisy=False)
        unit = build_schema_unit(part)
        assert unit["topology"] != {}

    def test_topology_empty_when_noisy(self):
        part = _make_partition("noisy", list(_SIMPLE_RECORDS), noisy=True)
        unit = build_schema_unit(part)
        assert unit["topology"] == {}

    def test_topology_has_depth_for_flat_fields(self):
        part = _make_partition("users", [{"name": "Alice", "age": 30}])
        unit = build_schema_unit(part)
        # 平坦字段 depth=1
        assert unit["topology"]["name"]["depth"] == 1
        assert unit["topology"]["age"]["depth"] == 1

    def test_topology_siblings_correct(self):
        part = _make_partition("u", [{"a": 1, "b": 2}])
        unit = build_schema_unit(part)
        # "a" 的兄弟是 "b"，反之亦然
        assert "b" in unit["topology"]["a"]["siblings"]
        assert "a" in unit["topology"]["b"]["siblings"]


# ── field_paths 回填 ──────────────────────────────────────────────────────────

class TestFieldPathsBackfill:
    def test_partition_field_paths_backfilled(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        assert part["field_paths"] == set()
        build_schema_unit(part)
        assert part["field_paths"] == {"id", "name", "phone"}

    def test_partition_occurrence_backfilled(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        build_schema_unit(part)
        assert set(part["occurrence"].keys()) == {"id", "name", "phone"}
        for v in part["occurrence"].values():
            assert v == 1.0


# ── value_profile ─────────────────────────────────────────────────────────────

class TestValueProfile:
    def test_str_field_has_len_dist(self):
        records = [{"username": "alice123"}, {"username": "bob456"}]
        part = _make_partition("u", records)
        unit = build_schema_unit(part)
        vp = unit["fields"]["username"]["value_profile"]
        assert "len_dist" in vp

    def test_int_field_profile_has_sample_count(self):
        part = _make_partition("users", list(_SIMPLE_RECORDS))
        unit = build_schema_unit(part)
        vp = unit["fields"]["id"]["value_profile"]
        assert "sample_count" in vp
        assert vp["sample_count"] == 3

    def test_value_profile_does_not_contain_raw_value(self):
        # 画像只存摘要，不含原始字符串
        records = [{"secret_token": "super_secret_abc123"}]
        part = _make_partition("u", records)
        unit = build_schema_unit(part)
        vp = unit["fields"]["secret_token"]["value_profile"]
        assert "super_secret_abc123" not in str(vp)

    def test_empty_field_has_empty_profile(self):
        # 只含 None 值的字段
        records = [{"note": None}]
        part = _make_partition("u", records)
        unit = build_schema_unit(part)
        # value_profile 为空 dict 或只含 sample_count
        vp = unit["fields"]["note"]["value_profile"]
        assert isinstance(vp, dict)


# ── 折叠路径对齐：skeleton / fields / topology 共用模板路径 ─────────────────────
#
# 重构后五类信息共用"折叠模板路径"（list 下标折叠成 []），skeleton 与 fields
# 按 path 1:1 对齐。两套字段主干方案：
#   - mode="template"（B，默认）：裁剪到 most_common 签名主干
#   - mode="fold"（A）：全部折叠 leaf 路径并集（保留数组元素级异构）

# 同构复杂样本：所有记录共享同一签名，A/B 主干一致。
# orders 在三条记录里长度 3 / 1 / 2（验证扇出折叠、画像聚合）。
_COMPLEX_RECORDS = [
    {
        "id": 1,
        "user": {"name": "Alice", "phone": "13812345678"},
        "orders": [
            {"oid": "A1", "amt": 9.9},
            {"oid": "A2", "amt": 15.0},
            {"oid": "A3", "amt": 3.5},
        ],
        "tags": ["vip", "new"],
        "meta": {"geo": {"lat": 31.23, "lng": 121.47}},
    },
    {
        "id": 2,
        "user": {"name": "Bob", "phone": "13987654321"},
        "orders": [
            {"oid": "B1", "amt": 100.0},
        ],
        "tags": ["churned"],
        "meta": {"geo": {"lat": 39.90, "lng": 116.40}},
    },
    {
        "id": 3,
        "user": {"name": "Carol", "phone": "13700000001"},
        "orders": [
            {"oid": "C1", "amt": 42.0},
            {"oid": "C2", "amt": 7.7},
        ],
        "tags": ["vip"],
        "meta": {"geo": {"lat": 22.54, "lng": 114.06}},
    },
]

# 同构样本的 8 条折叠模板叶子路径
_EXPECTED_LEAVES = {
    "id", "user.name", "user.phone", "orders[].oid", "orders[].amt",
    "tags[]", "meta.geo.lat", "meta.geo.lng",
}


def _print_report(unit):
    """-s 运行时打印 skeleton / fields 并排对照。"""
    bar = "=" * 72
    print("\n" + bar)
    print("skeleton（折叠模板路径）")
    print(bar)
    for entry in unit["skeleton"]:
        path, dtype = entry[0], entry[1]
        extra = f"  {entry[2]}" if len(entry) > 2 else ""
        print(f"  {path:<24} : {dtype}{extra}")

    print("\n" + bar)
    print("fields（折叠模板路径，与 skeleton 1:1）")
    print(bar)
    for fp in sorted(unit["fields"]):
        info = unit["fields"][fp]
        vp = info["value_profile"]
        print(
            f"  {fp:<24} key_name={info['key_name']:<8} "
            f"occ={info['occurrence']:<4} pii={info['pii_seed']} "
            f"vp.type={vp.get('type')}"
        )


class TestFoldedPathAlignment:
    """skeleton / fields / topology 共用折叠模板路径，按 path 对齐（默认 B 方案）。"""

    def test_print_report(self, capsys):
        with capsys.disabled():
            _print_report(build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS)))

    def test_skeleton_and_fields_paths_match(self):
        # 同构样本：skeleton 路径集 == fields 路径集 == 8 条折叠叶子
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        skeleton_paths = {entry[0] for entry in unit["skeleton"]}
        assert skeleton_paths == set(unit["fields"].keys()) == _EXPECTED_LEAVES

    def test_array_field_no_fanout(self):
        # 折叠后无下标扇出：orders[].amt 一条，没有 orders[0].amt
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        assert "orders[].amt" in unit["fields"]
        assert "orders[0].amt" not in unit["fields"]
        assert "orders[2].amt" not in unit["fields"]

    def test_list_of_scalar_folds_to_bracket(self):
        # list-of-scalar：统一为 tags[]（不再是裸 tags），key_name=tags
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        assert "tags[]" in unit["fields"]
        assert "tags" not in unit["fields"]
        assert unit["fields"]["tags[]"]["key_name"] == "tags"

    def test_container_nodes_not_fields(self):
        # 纯容器节点不进 fields
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        for container in ("user", "meta", "meta.geo", "orders"):
            assert container not in unit["fields"]

    def test_value_profile_aggregated_across_indices(self):
        # orders[].amt 聚合了所有下标/记录的 6 个值（3+1+2），画像不再碎片化
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        vp = unit["fields"]["orders[].amt"]["value_profile"]
        assert vp.get("sample_count") == 6

    def test_pii_on_folded_path(self):
        # user.name / user.phone 命中 PII
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        assert unit["fields"]["user.name"]["pii_seed"][0] == "high_conf"
        assert unit["fields"]["user.phone"]["pii_seed"][1] == "phone_number"

    def test_topology_folded_depth_by_dot_only(self):
        # depth 仅按 . 深度（[] 不计）：orders[].amt=2, tags[]=1, meta.geo.lat=3
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        topo = unit["topology"]
        assert topo["id"]["depth"] == 1
        assert topo["tags[]"]["depth"] == 1
        assert topo["orders[].amt"]["depth"] == 2
        assert topo["meta.geo.lat"]["depth"] == 3

    def test_topology_parent_and_siblings_folded(self):
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        topo = unit["topology"]
        assert topo["orders[].amt"]["parent"] == "orders[]"
        assert topo["orders[].amt"]["siblings"] == ["orders[].oid"]
        assert topo["id"]["parent"] is None

    def test_topology_includes_container_nodes(self):
        # topology 在叶子主干之外补全中间容器节点（backbone 无路径的 dict/list 容器）
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        topo = unit["topology"]
        for container in ("user", "orders[]", "meta", "meta.geo"):
            assert container in topo
            assert container not in unit["fields"]   # 容器不进 fields

    def test_topology_superset_of_fields(self):
        # 叶子字段全部在 topology 中（topology = fields ∪ 容器节点）
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        assert set(unit["fields"].keys()) <= set(unit["topology"].keys())

    def test_topology_meta_container_attrs(self):
        # meta 容器：depth=1、parent=None、siblings 为其它顶层节点
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        meta = unit["topology"]["meta"]
        assert meta["depth"] == 1
        assert meta["parent"] is None
        assert meta["siblings"] == ["id", "orders[]", "tags[]", "user"]

    def test_topology_nested_container_attrs(self):
        # meta.geo 容器：depth=2、parent=meta、含 lat/lng 叶子；orders[] 顶层容器
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        topo = unit["topology"]
        assert topo["meta.geo"]["depth"] == 2
        assert topo["meta.geo"]["parent"] == "meta"
        assert topo["meta.geo"]["siblings"] == []
        assert topo["orders[]"]["depth"] == 1
        assert topo["orders[]"]["parent"] is None

    def test_skeleton_count_b_preserved(self):
        # 同构样本 B=1（逐记录签名相同），折叠不破坏 B 计数
        unit = build_schema_unit(_make_partition("complex", _COMPLEX_RECORDS))
        assert unit["skeleton_count_B"] == 1


# 异构样本：元素级字段差异 + 跨记录可选字段
_HETERO_RECORDS = [
    {"items": [{"a": 1}, {"b": 2}]},   # 同一记录内元素异构：a 在 [0]，b 在 [1]
    {"items": [{"a": 3}]},             # 缺 b
]


class TestFoldVsTemplateMode:
    """A(fold) 找回元素级异构 / 少数派字段；B(template) 裁剪到主导签名。"""

    def test_fold_recovers_heterogeneous_element_fields(self):
        # A 方案：items[].a 和 items[].b 都在（union 不丢）
        unit = build_schema_unit(_make_partition("h", _HETERO_RECORDS), mode="fold")
        assert "items[].a" in unit["fields"]
        assert "items[].b" in unit["fields"]

    def test_template_drops_non_backbone_fields(self):
        # B 方案（默认）：主导签名只含首元素 {a} → items[].a 在，items[].b 被丢
        unit = build_schema_unit(_make_partition("h", _HETERO_RECORDS), mode="template")
        assert "items[].a" in unit["fields"]
        assert "items[].b" not in unit["fields"]

    def test_fold_multi_type_marker(self):
        # A 方案 dtype 多型标记：x 既是 int 又是 str
        recs = [{"x": 1}, {"x": 2}, {"x": "hello"}]
        unit = build_schema_unit(_make_partition("m", recs), mode="fold")
        entry = next(e for e in unit["skeleton"] if e[0] == "x")
        assert len(entry) == 3                       # (path, dtype, meta)
        assert entry[1] == "int"                     # 最高频
        assert entry[2]["multi_type"] == ["int", "str"]
        assert entry[2]["dominant_ratio"] == round(2 / 3, 4)

    def test_null_not_counted_in_dtype(self):
        # null 不计入 dtype：x 全是 int + 一个 null → 单型 int，无多型标记
        recs = [{"x": 1}, {"x": None}, {"x": 3}]
        unit = build_schema_unit(_make_partition("n", recs), mode="fold")
        entry = next(e for e in unit["skeleton"] if e[0] == "x")
        assert entry[1] == "int"
        assert len(entry) == 2                        # 无多型标记

    def test_all_null_path_fallback(self):
        # 全 null 路径 dtype 兜底为 "null"
        recs = [{"note": None}, {"note": None}]
        unit = build_schema_unit(_make_partition("z", recs), mode="fold")
        entry = next(e for e in unit["skeleton"] if e[0] == "note")
        assert entry[1] == "null"

    def test_b_topology_cropped_to_backbone(self):
        # B 方案 topology 主干裁剪：少数派叶子 items[].b 不在 topology；
        # 但容器节点 items[] 会被补全（叶子 items[].a 的祖先）。
        unit = build_schema_unit(_make_partition("h", _HETERO_RECORDS), mode="template")
        assert "items[].b" not in unit["topology"]
        assert "items[]" in unit["topology"]          # 容器节点补全
        assert "items[]" not in unit["fields"]
        # topology = fields(叶子) ∪ 容器节点 → fields ⊆ topology
        assert set(unit["fields"].keys()) <= set(unit["topology"].keys())

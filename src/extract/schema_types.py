"""共享数据结构（TypedDict）。三处统一 import，避免循环依赖。

用 TypedDict 而非 dataclass：这些结构以 dict 形态在管线中流动并序列化成 JSON，
TypedDict 在保持 `obj["key"]` 运行态不变的前提下提供静态类型契约（IDE 补全 + mypy
查 key 拼写）。VocabTable 是任意键的倒排索引，保留为类型别名。
"""

from typing import Any, Dict, Iterator, List, Optional, Tuple, TypedDict


# ── SchemaPartition ───────────────────────────────────────────────────────────
# schema_partition.partition_file() 产出，build_schema_unit() 输入。
class SchemaPartition(TypedDict):
    source_file: str                    # 文件绝对路径
    format: str                         # json / jsonl / csv / tsv / sql / sqlite
    partition_id: str                   # 同文件多 schema 区分，如 "users" / "sig_a3f2c1b0"
    record_iter: Iterator[dict]         # ⚠ 惰性迭代器，只可消费一次
    noisy: bool                         # CSV 列数不稳定时为 True，据此跳过拓扑
    field_paths: set                    # 已知字段路径集合（build_schema_unit 回填）
    occurrence: Dict[str, float]        # 字段出现率；当前占位 1.0（build_schema_unit 回填）


# ── PartitionStats ────────────────────────────────────────────────────────────
# schema_partition.partition_file() 的分片副产物，供调参和统计。
class PartitionStats(TypedDict):
    source_file: str
    format: str
    partition_count: int
    partition_ids: List[str]
    method: str                         # explicit_key / skeleton_cluster / single / table_name


# ── FieldInfo ─────────────────────────────────────────────────────────────────
# SchemaUnit.fields 的值类型。
class FieldInfo(TypedDict):
    field_id: str                       # f_{su_seq:05d}_{field_seq:02d}
    key_name: str                       # 折叠路径末段字段名
    occurrence: float                   # 出现率（当前占位 1.0）
    required: bool                      # occurrence >= 0.9
    value_profile: Dict[str, Any]       # ⚠ 非原值，profile_value/aggregate_profiles 输出
    pii_seed: Optional[Tuple[str, Optional[str]]]   # (level, pii_type) 或 None


# ── SchemaUnit ────────────────────────────────────────────────────────────────
# build_schema_unit() 产出，build_vocab_table() 输入。
class SchemaUnit(TypedDict):
    id: str                             # 全局唯一，格式 "sch_{N:05d}"
    source_file: str
    format: str
    partition_id: str
    skeleton: List[tuple]               # [(path, dtype) | (path, dtype, meta), ...] 折叠模板路径
    skeleton_count_B: int               # 唯一骨架签名数 B
    skeleton_counts: Dict[str, int]     # {sig: count}（供全局聚合）
    topology: Dict[str, dict]           # {path: {depth, parent, siblings}}（裁剪到主干）
    fields: Dict[str, FieldInfo]        # {折叠路径: FieldInfo}
    record_count: int                   # 实际采样记录数


# ── VocabTable ────────────────────────────────────────────────────────────────
# build_vocab_table() 产出（任意键倒排索引，非定长记录，保留为类型别名）。
# VocabTable[semantic_class][key_variant] = [schema_unit_id, ...]
VocabTable = Dict[str, Dict[str, List[str]]]


# ── KeyEntry ──────────────────────────────────────────────────────────────────
# build_vocab_table() 内部使用：收集的字段判别线索。
class KeyEntry(TypedDict):
    key_name: str
    path: str
    schema_unit_id: str
    field_id: str
    value_profile: Dict[str, Any]
    pii_seed: Optional[Tuple[str, Optional[str]]]

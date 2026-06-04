"""阶段2编排器。

- extract_all()       : 新主入口 —— schema_partition(分片) → build_schema_unit(构建) → vocab_table(词汇表)
- extract_five_infos(): 兼容接口 —— extract_all() 的薄包装，把 SchemaUnit 拍平为
                        旧的全局扁平五类信息 dict（无溯源；新代码勿用）
"""

from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from src.parse.grade import Grade
from src.extract.schema_types import SchemaUnit, VocabTable


# ════════════════════════════════════════════════════════════════════════════
# 新主入口：Schema 单元化
# ════════════════════════════════════════════════════════════════════════════

def extract_all(
    tier1_grades: List[Grade],
    mode: str = "template",
) -> Tuple[List[SchemaUnit], VocabTable, Dict[str, Any]]:
    """Schema 单元化提取主入口。

    执行顺序：schema_partition（分片） → build_schema_unit（构建） → vocab_table（词汇表）

    Args:
        tier1_grades: tier1 种子。
        mode: 字段主干方案，"template"（B，默认）或 "fold"（A）。

    Returns:
        schema_units  : 每个文件/表对应的 SchemaUnit 列表
        vocab_table   : 全局同义倒排表
        global_view   : 全局聚合统计
    """
    from src.extract.schema_partition import partition_file
    from src.extract.schema_unit import build_schema_unit
    from src.extract.vocab_table import build_vocab_table

    all_partitions = []
    partition_stats_list = []

    for grade in tier1_grades:
        parts, stats = partition_file(grade)
        all_partitions.extend(parts)
        partition_stats_list.append(stats)

    schema_units: List[SchemaUnit] = [
        build_schema_unit(p, mode=mode) for p in all_partitions
    ]

    vocab_table, uncertain = build_vocab_table(schema_units)

    global_view = _aggregate_global_view(schema_units)
    global_view["partition_stats"] = partition_stats_list
    global_view["uncertain_vocab"] = uncertain

    return schema_units, vocab_table, global_view


def _aggregate_global_view(schema_units: List[SchemaUnit]) -> Dict[str, Any]:
    """将所有 SchemaUnit 聚合为全局视图。"""
    all_skeletons: Counter = Counter()
    all_field_names: set = set()
    pii_seeds_count = 0
    total_records = 0

    for unit in schema_units:
        total_records += unit.get("record_count", 0)
        for sig, cnt in unit.get("skeleton_counts", {}).items():
            all_skeletons[sig] += cnt
        all_field_names.update(unit.get("fields", {}).keys())
        for path, info in unit.get("fields", {}).items():
            if info.get("pii_seed"):
                pii_seeds_count += 1

    shape_B = len(all_skeletons)
    naming_A = len(all_field_names)

    return {
        "shape_templates_B":    shape_B,
        "naming_templates_A":   naming_A,
        "AB_ratio":             round(naming_A / max(shape_B, 1), 3),
        "pii_seeds_count":      pii_seeds_count,
        "total_records_sampled": total_records,
        "top_skeletons":        dict(all_skeletons.most_common(20)),
    }


# ════════════════════════════════════════════════════════════════════════════
# 兼容接口：旧的全局扁平五类信息
# ════════════════════════════════════════════════════════════════════════════

def extract_five_infos(
    tier1_grades: List[Grade],
    mode: str = "template",
) -> Dict[str, Any]:
    """【兼容保留】返回旧版全局扁平五类信息 dict。

    现已改为 ``extract_all()`` 的薄包装：跑新管线后把每个 SchemaUnit 的
    （折叠路径）信息合并成一份全局扁平视图。与旧实现的差异：

    - 路径用**折叠模板路径**（`orders[].amt`），非旧的 `[i]` 实例路径；
    - `value_profiles` / `topology` 跨分片合并，同名路径**后者覆盖**
      （value_profile 的聚合不可后期无损合并，这里取最后出现的分片）；
    - 不再独立重新读盘，复用 ``extract_all`` 的分片采样。

    ⚠ 无溯源（看不出字段来自哪个文件/表）；新代码请直接用 ``extract_all()``。
    """
    schema_units, _vocab_table, global_view = extract_all(tier1_grades, mode=mode)

    field_vocab: Dict[str, set] = defaultdict(set)
    value_profiles: Dict[str, Any] = {}
    topology: Dict[str, Any] = {}
    pii_seeds: Dict[str, Any] = {}
    skeletons: Counter = Counter()

    for unit in schema_units:
        skeletons.update(unit.get("skeleton_counts", {}))
        topology.update(unit.get("topology", {}))
        for path, info in unit.get("fields", {}).items():
            field_vocab[info["key_name"]].add(path)
            if info.get("value_profile"):
                value_profiles[path] = info["value_profile"]
            if info.get("pii_seed"):
                pii_seeds[path] = info["pii_seed"]

    naming_A = len(field_vocab)                       # 唯一字段名数（与旧 vocab_stats 一致）
    shape_B = global_view["shape_templates_B"]

    return {
        "skeletons":            dict(skeletons.most_common(200)),
        "shape_templates_B":    shape_B,
        "field_vocab":          {k: sorted(v)[:20] for k, v in field_vocab.items()},
        "naming_templates_A":   naming_A,
        "AB_ratio":             round(naming_A / max(shape_B, 1), 3),
        "value_profiles":       value_profiles,
        "topology":             topology,
        "pii_seeds":            pii_seeds,
        "total_records_sampled": global_view["total_records_sampled"],
    }

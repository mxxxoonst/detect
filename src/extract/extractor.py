"""阶段2编排器：流式 / 断点续跑入口 + 全局视图聚合。

- stream_schema_units()   : Pass1，逐 tier1 Grade 分片 + 构建 SchemaUnit，回调即时落盘。
- finalize_ir_from_units(): IR 路径 Pass2，流式聚合 global_view（跳过 vocab_table）。
- finalize_from_units()   : 非 IR 路径 Pass2，额外构建 vocab_table。
"""

from collections import Counter
from typing import Any, Dict, Tuple

from src.extract.schema_types import VocabTable
from src.utils.logger import get_logger

log = get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════════
# 全局视图聚合（供 finalize_* 复用）
# ════════════════════════════════════════════════════════════════════════════

def _norm_skel_type(t: Any) -> Any:
    """聚合用类型归一: int/float → 'num'（与 schema_dedup 同口径），其余原样。"""
    return "num" if t in ("int", "float") else t


def _aggregate_global_view(schema_units) -> Dict[str, Any]:
    """将所有 SchemaUnit 聚合为全局视图（接受 list 或惰性迭代器，流式增量计数）。"""
    all_skeletons: Counter = Counter()
    all_field_names: set = set()
    pii_seeds_count = 0
    total_records = 0
    unit_count = 0

    for unit in schema_units:
        unit_count += 1
        total_records += unit.get("record_count", 0)
        counts = unit.get("skeleton_counts")
        sk = unit.get("skeleton")
        fields = unit.get("fields")
        if counts:                                   # 冗长形态：逐签名计数
            for sig, cnt in counts.items():
                all_skeletons[sig] += cnt
        elif isinstance(sk, dict):                   # 紧凑 IR：path + 归一 type 签名(与去重口径一致)
            # 已去重 → 每个代表是一类 distinct schema；按 cluster_size 加权还原真实重数
            # (你看到的 590 现在落到单一代表上，而非 590 条同质 unit)。
            sig = "|".join(f"{p}:{_norm_skel_type(m.get('type'))}"
                           for p, m in sorted(sk.items()))
            all_skeletons[sig] += unit.get("cluster_size", 1)
        if fields:                                   # 冗长形态：字段名 + PII 计数
            all_field_names.update(fields.keys())
            for path, info in fields.items():
                if info.get("pii_seed"):
                    pii_seeds_count += 1
        elif isinstance(sk, dict):                   # 紧凑 IR：字段名取 skeleton 键
            all_field_names.update(sk.keys())

    shape_B = len(all_skeletons)
    naming_A = len(all_field_names)

    return {
        "shape_templates_B":    shape_B,
        "naming_templates_A":   naming_A,
        "AB_ratio":             round(naming_A / max(shape_B, 1), 3),
        "pii_seeds_count":      pii_seeds_count,
        "total_records_sampled": total_records,
        "schema_unit_count":    unit_count,
        "top_skeletons":        dict(all_skeletons.most_common(20)),
    }


# ════════════════════════════════════════════════════════════════════════════
# 流式 / 断点续跑入口（落盘消费，内存恒定 O(单文件)）
# ════════════════════════════════════════════════════════════════════════════

def stream_schema_units(
    grades_iter,
    mode: str,
    append_unit,
    done_source_files=frozenset(),
    sample_mode: str = "off",
    compact: bool = False,
) -> Tuple[int, int, int, int]:
    """Pass1：逐 tier1 Grade 分片 + 构建 SchemaUnit，经 ``append_unit`` 回调即时落盘。

    内存恒定（一次只持有单文件的分片与 unit）；``done_source_files`` 命中则跳过（续跑）。

    Args:
        grades_iter:        惰性产出 tier1 Grade 的迭代器。
        mode:               字段主干方案 template/fold。
        append_unit:        回调 ``append_unit(unit)`` 负责把单个 SchemaUnit 落盘。
        done_source_files:  已处理的源文件集合（续跑跳过）。
        sample_mode:        信息三样本保留，"off"（默认）/ "raw" / "masked"。

    Returns:
        (processed_files, partition_count, written_units, skipped_files)
    """
    from src.extract.schema_partition import partition_file
    from src.extract.schema_unit import build_schema_unit

    processed = pc = written = skipped = 0
    for grade in grades_iter:
        if grade.path in done_source_files:
            skipped += 1
            continue
        processed += 1
        parts, _stats = partition_file(grade)
        pc += len(parts)
        for p in parts:
            unit = build_schema_unit(p, mode=mode, sample_mode=sample_mode,
                                     compact=compact)
            # 空分片(0 记录: 如纯表头 CSV)不构成 IR 单元，不落盘
            if unit.get("record_count", 0) == 0:
                continue
            append_unit(unit)
            written += 1
        if processed % 200 == 0:
            log.info("  阶段2 进度: 已处理 %d 文件, 累计写出 %d unit", processed, written)

    return processed, pc, written, skipped


def finalize_ir_from_units(units_iter_factory) -> Dict[str, Any]:
    """建 IR 路径（§4.7）：只聚合 global_view，**跳过全局 vocab_table**。

    vocab_table 是语料分析产物（跨表同义倒排），非「单元 IR」；构建 IR 数据集时
    SchemaUnit 本身即投影单元（结构 skeleton + 拓扑 topology + 值证据=值样本，
    occurrence 已由 Q1 union-schema 真值填充）。此路径省掉词表聚类，单遍流式、内存恒定。

    值证据走 build_schema_unit 的 sample_mode（建 IR 推荐 "masked"），由上游 CLI
    传入；本函数只做不依赖原值的全局聚合。
    """
    return _aggregate_global_view(units_iter_factory())


def finalize_from_units(units_iter_factory) -> Tuple[VocabTable, Dict[str, Any]]:
    """Pass2：两遍流式读 schema_units.jsonl，聚合 global_view + 构建 vocab_table。

    ``units_iter_factory()`` 每次调用返回一个**新的** SchemaUnit 迭代器（如
    ``lambda: iter_jsonl(units_path)``）。两遍分别喂全局聚合与词表聚类，
    全局聚合内存恒定；词表聚类内存为 O(字段条目数)（跨单元同义对齐的固有代价）。
    """
    from src.extract.vocab_table import build_vocab_table

    global_view = _aggregate_global_view(units_iter_factory())
    vocab_table, uncertain = build_vocab_table(units_iter_factory())
    global_view["uncertain_vocab"] = uncertain
    return vocab_table, global_view

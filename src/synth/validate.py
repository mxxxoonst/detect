"""渲染保真校验：渲染文本 → 写临时文件 → grade_parse → partition_file →
build_schema_unit(compact) → 取回 skeleton 路径集，与源 unit 路径集比对。

这是「模板 vs LLM 效果」的**客观结构指标**：渲染出来的文档被真实解析器读回后，
原 schema 的字段路径是否被忠实还原（jaccard / missing / extra）。注意只比**路径集**
不比类型——SQL/CSV 经解析后值都成字符串、类型丢失是预期现象，不应算结构失真。

全程合成临时文件、本地可跑，不碰远程数据；走的就是真实阶段1+阶段2 的解析与提取路径。
"""

from pathlib import Path
from typing import Any, Dict, Set

from src.extract.schema_partition import partition_file
from src.extract.schema_unit import build_schema_unit
from src.parse.grade import grade_parse
from src.synth.render import surface_format
from src.utils.logger import get_logger

log = get_logger(__name__)

_PARSEABLE = {"json", "jsonl", "csv", "tsv", "sql", "xlsx"}
_EXT = {"json": "json", "jsonl": "jsonl", "csv": "csv", "tsv": "tsv", "sql": "sql"}


def _norm_paths(skeleton: Any) -> Set[str]:
    """取 skeleton 的折叠路径集（紧凑 IR 是 dict；冗长形态退化为 keys）。"""
    if isinstance(skeleton, dict):
        return set(skeleton.keys())
    return set()


def validate_render(
    text: str,
    unit: Dict[str, Any],
    tmp_dir: str,
    encoding: str = "utf-8",
) -> Dict[str, Any]:
    """把渲染文本反解析，度量结构保真。

    Returns dict:
        unit_id / surface_format / tier / n_partitions_recovered /
        expected_path_count / recovered_path_count / missing / extra / jaccard / ok
    其中 ``ok`` = 期望路径**全部**被还原（missing 为空）。
    """
    sf = surface_format(unit.get("format", ""))
    expected = _norm_paths(unit.get("skeleton"))
    result: Dict[str, Any] = {
        "unit_id":               unit.get("id"),
        "surface_format":        sf,
        "tier":                  None,
        "n_partitions_recovered": 0,
        "expected_path_count":   len(expected),
        "recovered_path_count":  0,
        "missing":               sorted(expected)[:20],
        "extra":                 [],
        "jaccard":               0.0,
        "ok":                    False,
    }
    if not text.strip():
        log.debug("validate_render %s: 空渲染文本", unit.get("id"))
        return result

    ext = _EXT.get(sf, "txt")
    fp = Path(tmp_dir) / f"{unit.get('id', 'unit')}.{ext}"
    try:
        fp.write_text(text, encoding=encoding, errors="replace")
    except OSError as e:
        log.warning("validate_render %s: 临时文件写入失败 %s", unit.get("id"), e)
        return result

    grade = grade_parse(str(fp), sf, encoding)
    result["tier"] = grade.tier
    if not grade.fmt:
        grade.fmt = sf
    if grade.fmt not in _PARSEABLE:
        return result

    recovered: Set[str] = set()
    n_parts = 0
    try:
        parts, _stats = partition_file(grade)
        for p in parts:
            u = build_schema_unit(p, mode="template", sample_mode="off", compact=True)
            if u.get("record_count", 0) == 0:
                continue
            n_parts += 1
            recovered |= _norm_paths(u.get("skeleton"))
    except Exception as e:                       # 反解析任何环节出错都算保真失败，不崩
        log.debug("validate_render %s: 反解析失败 %s", unit.get("id"), e)

    inter = expected & recovered
    union = expected | recovered
    result.update({
        "n_partitions_recovered": n_parts,
        "recovered_path_count":   len(recovered),
        "missing":                sorted(expected - recovered)[:20],
        "extra":                  sorted(recovered - expected)[:20],
        "jaccard":                round(len(inter) / max(len(union), 1), 3),
        "ok":                     bool(expected) and expected <= recovered,
    })
    return result

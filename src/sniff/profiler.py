"""语料库嗅探画像: 遍历目录产出交叉表。"""

from collections import Counter
from typing import Any, Dict, List, Tuple

from src.constants import LOW_CONF_THRESHOLD
from src.sniff.sniffer import sniff_file
from src.utils.file_utils import walk_files, extension


def profile_corpus(root: str, files: list | None = None) -> Dict[str, Any]:
    """遍历目录, 对全部文件做嗅探, 产出交叉表和分布。

    Args:
        root: 语料库根路径 (用于日志/调试)
        files: 可选的文件列表, 提供时直接使用而非 walk_files

    Returns:
        {
            "cross_table":      {(ext, real_format): count},
            "format_dist":      {real_format: count},
            "low_confidence":   [(path, fmt, conf), ...],  最多 200 条
            "total_files":      int,
        }
    """
    cross_table: Counter = Counter()
    format_dist: Counter = Counter()
    low_confidence: List[Tuple[str, str, float]] = []

    it: list = files if files is not None else list(walk_files(root))
    for path in it:
        fmt, enc, conf = sniff_file(path)
        ext = extension(path)
        cross_table[(ext, fmt)] += 1
        format_dist[fmt] += 1
        if conf < LOW_CONF_THRESHOLD:
            low_confidence.append((path, fmt, conf))

    # 限制低置信样本数
    low_confidence = low_confidence[:200]

    # 将 (ext, fmt) tuple key 转为 "ext|fmt" 字符串 key, 方便 JSON 序列化
    cross_table_str = {f"{ext}|{fmt}": cnt for (ext, fmt), cnt in cross_table.items()}

    return {
        "cross_table": cross_table_str,
        "format_dist": dict(format_dist),
        "low_confidence": low_confidence,
        "total_files": sum(format_dist.values()),
    }

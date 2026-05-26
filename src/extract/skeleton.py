"""信息一: 结构骨架 — 抹 value 留 key 树, 统计形状模板."""

from collections import Counter
from typing import Any, Dict, List


def structure_signature(record: Any) -> str:
    """计算单条 record 的结构骨架签名。

    规则:
      - dict: 保留 key 名, 递归 value
      - list:  保留元素类型, 取第一个元素递归
      - 标量: 替换为类型标记 <str>, <int>, <float>, <bool>, <null>
      - 嵌套: 生成 JSON 形式的签名串, 可供 hash/count

    Returns:
        类似 '{"id":"<int>","name":"<str>","scores":["<float>"]}' 的字符串
    """
    return _signature(record)


def _signature(value: Any) -> str:
    if value is None:
        return "<null>"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float>"
    if isinstance(value, str):
        return "<str>"
    if isinstance(value, list):
        if not value:
            return "[]"
        elem = _signature(value[0])
        return f"[{elem}]"
    if isinstance(value, dict):
        items = ",".join(
            f'"{k}":{_signature(v)}' for k, v in sorted(value.items())
        )
        return "{" + items + "}"
    return f"<{type(value).__name__}>"


def collect_skeletons(records: List[Dict]) -> Counter:
    """收集一批 records 的骨架签名计数。"""
    counter: Counter = Counter()
    for rec in records:
        sig = structure_signature(rec)
        counter[sig] += 1
    return counter

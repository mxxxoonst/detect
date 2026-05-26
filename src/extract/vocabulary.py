"""信息二: field_vocab — 所有字段名及出现路径。"""

from collections import defaultdict
from typing import Any, Dict, List, Set


def build_vocabulary(records: List[Dict]) -> Dict[str, Set[str]]:
    """构建 field → {paths} 映射。

    每条 path 以 '.' 分隔嵌套层级, 列表以 '[i]' 表示。
    例如: 'users[0].name', 'users[0].phone'
    """
    vocab: Dict[str, Set[str]] = defaultdict(set)
    for rec in records:
        _walk_vocab(rec, "", vocab)
    return dict(vocab)


def _walk_vocab(node: Any, prefix: str, vocab: Dict[str, Set[str]]):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            vocab[key].add(path)
            _walk_vocab(value, path, vocab)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            path = f"{prefix}[{i}]"
            _walk_vocab(item, path, vocab)


def vocab_stats(vocab: Dict[str, Set[str]]) -> Dict:
    """统计 vocab: 总名词数 (A), 高频词 Top-N."""
    return {
        "total_fields_A": len(vocab),
        "top_fields": sorted(
            [(k, len(v)) for k, v in vocab.items()],
            key=lambda x: -x[1],
        )[:50],
    }

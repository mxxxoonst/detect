"""信息四: 拓扑 — 每个字段的 depth, parent, siblings."""

from typing import Any, Dict, List


def build_topology(records: List[Dict]) -> Dict[str, Dict]:
    """为所有 record 中的字段路径构建拓扑信息。

    Returns:
        {
            "field_path": {
                "depth": int,
                "parent": str | None,
                "siblings": [str, ...]  (同父下的兄弟字段)
            }
        }
    """
    topology: Dict[str, Dict] = {}
    for rec in records:
        _walk_topology(rec, "", topology)
    return topology


def _walk_topology(node: Any, prefix: str, topology: Dict[str, Dict]):
    if isinstance(node, dict):
        keys = list(node.keys())
        for key in keys:
            path = f"{prefix}.{key}" if prefix else key
            parent = prefix if prefix else None
            siblings = [f"{prefix}.{k}" if prefix else k for k in keys if k != key]
            depth = path.count(".") + 1
            topology[path] = {
                "depth": depth,
                "parent": parent,
                "siblings": siblings,
            }
            _walk_topology(node[key], path, topology)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            path = f"{prefix}[{i}]"
            if not path.startswith("[") and "[" in path:
                depth = path.count(".") + 1
            else:
                depth = 0
            topology[path] = {
                "depth": depth,
                "parent": prefix if prefix else None,
                "siblings": [],
            }
            _walk_topology(item, path, topology)

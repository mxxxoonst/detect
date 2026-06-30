"""信息一: 结构骨架 — 抹 value 留 key 树, 统计形状模板.

除了精确签名 (structure_signature, 仍供 skeleton_count_B / skeleton_counts 计数),
本模块还提供 **union-schema 归一化** 能力 (norm_type / leaf_types / compatible),
用于 JSON/JSONL 记录级的兼容性合并聚类 —— 消除「可选键 / null 多态 / 空容器」
三类伪签名差异造成的分片爆炸 (见 docs/json_csv_schema_handling.md §2.1)。
"""

from collections import Counter
from typing import Any, Dict, List, Set


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
    """
    对一条 record 做深度优先递归，抹掉值、保留结构：

  - dict → {"key":签名}，按 key
  排序（sorted(value.items())）保证同结构不同顺序得到同一签名
  - list → 只取首元素递归：[元素签名]（空列表为 []）
  - 标量 → 类型标记 <int>/<str>/<float>/<bool>/<null>

  以一条嵌套 record 为例：

  {
    "id": 1,
    "user":   {"name": "Alice", "age": 30},
    "tags":   ["a", "b"],
    "orders": [{"oid": "X", "amt": 9.9}]
  }

  生成的签名串（注意 key 已排序、list 只看首元素）：

  {"id":<int>,"orders":[{"amt":<float>,"oid":<str>}],"tags":[<str>],"user":{"a
  ge":<int>,"name":<str>}}

    """
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


# ── union-schema 归一化（兼容性合并 / 模式包络）─────────────────────────────────
# 把精确签名脆点逐一消解（docs §2.1.2）：
#   null      → 不携带类型（可空，通配）
#   缺键      → 不算冲突（可选）
#   空 {}/[]  → 通配（跳过）
#   int/float → 统一 num
#   list      → 对所有元素取并（非只首元素）
# 两条记录归同一分片 ⟺ 在共享叶路径上无类型冲突。


def norm_type(value: Any) -> str | None:
    """标量/容器 → 归一化类型标记；None → 返回 None（通配，不携带类型）。

    bool 先于 int 判定（Python 中 bool 是 int 子类）；int/float 合并为 ``num``。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "obj"
    if isinstance(value, list):
        return "arr"
    return "other"


def leaf_types(value: Any, prefix: str, out: Dict[str, Set[str]]) -> None:
    """展平成 {叶路径: set(类型)}；跳过空容器；list 对所有元素取并。

    - dict（非空）→ 按 key 排序递归 ``prefix.k``
    - list（非空）→ 每个元素递归到 ``prefix[]``（取并，非只首元素）
    - 空 {} / 空 []  → 直接返回（不携带 schema 信息，即「空容器通配」）
    - 标量 → 在 prefix 处累加 norm_type（null 不落，即「可空通配」）
    """
    if isinstance(value, dict):
        if not value:
            return
        for k in sorted(value):
            leaf_types(value[k], f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(value, list):
        if not value:
            return
        for elem in value:
            leaf_types(elem, f"{prefix}[]", out)
    else:
        t = norm_type(value)
        if t is not None:
            out.setdefault(prefix, set()).add(t)


def record_leaf_types(record: Any) -> Dict[str, Set[str]]:
    """单条记录 → {叶路径: set(归一化类型)}。"""
    out: Dict[str, Set[str]] = {}
    leaf_types(record, "", out)
    return out


def compatible(proto: Dict[str, Set[str]], rec_paths: Dict[str, Set[str]]) -> bool:
    """记录是否与原型兼容：不存在「共享叶路径且类型集互斥」。

    缺键 / 空容器 / null 都不进入 ``rec_paths`` → 天然不算冲突（可选 / 通配）。
    """
    for path, types in rec_paths.items():
        proto_types = proto.get(path)
        if proto_types is not None and proto_types.isdisjoint(types):
            return False
    return True


class UnionSchemaClusterer:
    """单遍贪心聚类：每条记录并入第一个兼容原型（扩张并集 + 累加每字段出现计数），
    否则开新原型。真异质实体（共享路径真冲突）仍被正确分开。

    流式友好：``add(record)`` 逐条喂入，只在内存维护 O(原型数) 的并集 schema 与
    每字段出现计数 —— 同质文件原型数应为个位数。原始记录不在此累积（采样落地由
    调用方按 SAMPLE_PER_FILE 控制）。
    """

    def __init__(self) -> None:
        self.protos: List[Dict[str, Set[str]]] = []       # 每原型: {叶路径: 类型并集}
        self.field_counts: List[Counter] = []             # 每原型: {叶路径: 出现记录数}
        self.sizes: List[int] = []                        # 每原型: 记录数

    def add(self, record: Any) -> int:
        """喂一条记录，返回其归入的原型索引（新原型则为新索引）。"""
        rec_paths = record_leaf_types(record)
        for i, proto in enumerate(self.protos):
            if compatible(proto, rec_paths):
                for p, ts in rec_paths.items():
                    proto.setdefault(p, set()).update(ts)
                    self.field_counts[i][p] += 1
                self.sizes[i] += 1
                return i
        # 新原型
        self.protos.append({p: set(ts) for p, ts in rec_paths.items()})
        fc: Counter = Counter()
        for p in rec_paths:
            fc[p] += 1
        self.field_counts.append(fc)
        self.sizes.append(1)
        return len(self.protos) - 1

    def occurrence(self, idx: int) -> Dict[str, float]:
        """原型 idx 的每字段 presence-rate（出现记录数 / 该原型记录总数）。"""
        size = self.sizes[idx]
        if size <= 0:
            return {}
        return {p: round(c / size, 4) for p, c in self.field_counts[idx].items()}

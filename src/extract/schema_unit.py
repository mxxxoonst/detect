"""build_schema_unit：SchemaPartition → SchemaUnit。

消费 partition.record_iter（一次性），**一遍遍历**桶内记录，抽取五类信息，
组装并返回 SchemaUnit。

路径约定：list 下标统一折叠成 `[]`（模板路径），五类信息共用同一套路径。

两套字段主干方案（mode 参数，默认 "template"）：
  - "template"（B，默认）：字段主干 = most_common 签名展开的模板叶子路径（子集）；
                           skeleton/topology/fields 均裁剪到该主干，允许丢失非主干字段。
  - "fold"（A）：字段主干 = 全部折叠 leaf 路径并集（含数组元素级异构、少数派可选字段）。

两套共用同一遍遍历，仅在"主干路径集"的选取上分叉（B 的路径集 ⊆ A 的路径集）。
occurrence 暂为占位符 1.0（真值随 optional_field_grouping 一并落地，见 todo_list.md）。
"""

import itertools
import json
import re
from collections import Counter, defaultdict
from itertools import islice
from typing import Any, Dict, List, Optional, Tuple

from src.constants import SAMPLE_PER_FILE
from src.extract.schema_types import SchemaPartition, SchemaUnit,FieldInfo
from src.extract.skeleton import structure_signature
from src.extract.value_profile import profile_value, aggregate_profiles
from src.extract.pii_seed import key_name_implies_pii, infer_pii_type, is_free_text_field
from src.utils.logger import get_logger

log = get_logger(__name__)

# 模块级全局计数器，单次 pipeline 运行内自增，不跨 run 持久化
_UNIT_COUNTER = itertools.count(1)


def reset_unit_counter() -> None:
    """重置计数器（仅供测试使用）。"""
    global _UNIT_COUNTER
    _UNIT_COUNTER = itertools.count(1)


def set_unit_counter(next_value: int) -> None:
    """将 ID 计数器重设到指定起点（断点续跑：从已写 unit 的 max+1 继续，避免 ID 冲突）。"""
    global _UNIT_COUNTER
    _UNIT_COUNTER = itertools.count(next_value)


def build_schema_unit(
    partition: SchemaPartition, mode: str = "template", sample_mode: str = "off",
    compact: bool = False,
) -> SchemaUnit:
    """消费 partition['record_iter']，一遍遍历组装五类信息，返回 SchemaUnit。

    Args:
        partition: schema_partition 产出的分片。
        mode: "template"（B，默认）裁剪到 most_common 签名主干；"fold"（A）取全路径并集。
        sample_mode: 信息三样本保留方案，"off"（默认，守 PII 红线，不留原值）/
                     "raw"（留原始样本）/ "masked"（留脱敏样本）。
        compact: 紧凑 IR 形态（--ir）。skeleton 收敛为
                 ``{path: {depth, type, samples}}`` 的单一骨架字典——拓扑(depth)、
                 类型、样本三合一，**丢弃** value_profile 统计画像 / 独立 topology /
                 fields / skeleton_counts 冗余块。

    ⚠ record_iter 消费后不可重播。
    """
    # 1. 消费迭代器（一次性）
    records: List[Dict] = list(islice(partition["record_iter"], SAMPLE_PER_FILE))

    # 2. 分配全局唯一 ID
    su_seq = next(_UNIT_COUNTER)
    unit_id = f"sch_{su_seq:05d}"

    # 空 partition 快速返回（上层 stream_schema_units 据 record_count==0 跳过不落 IR）
    if not records:
        log.debug("空分片(采到 0 条记录) %s [%s]，不落 IR",
                  partition["source_file"], partition["partition_id"])
        return {
            "id":               unit_id,
            "source_file":      partition["source_file"],
            "format":           partition["format"],
            "partition_id":     partition["partition_id"],
            "skeleton":         [],
            "skeleton_count_B": 0,
            "skeleton_counts":  {},
            "topology":         {},
            "fields":           {},
            "record_count":     0,
        }

    # 3. 单遍遍历：同时产出三样
    #    sig_counter     : 逐记录签名计数 → 保 B / skeleton_counts / AB_ratio（不依赖折叠）
    #    template_values : 折叠路径 → 该路径所有叶子值（元素级聚合，供 value 画像）
    #    dtype_seen      : 折叠路径 → 类型计数（null 不计入，供 skeleton dtype 多型判定）
    sig_counter: Counter = Counter()
    template_values: Dict[str, list] = defaultdict(list)
    dtype_seen: Dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        sig_counter[structure_signature(rec)] += 1
        _walk_fold(rec, "", template_values, dtype_seen)

    # 4. 信息一：骨架 B 计数（始终来自逐记录签名，与字段主干方案无关）
    skeleton_count_B = len(sig_counter)
    skeleton_counts = dict(sig_counter.most_common(50))

    # 5. 字段主干路径集 + skeleton 路径列表（随 mode 分叉）
    backbone, skeleton = _backbone_and_skeleton(
        mode, template_values, dtype_seen, sig_counter
    )

    # 5b. 紧凑 IR（--ir）：单一 skeleton 字典 {path: {depth, type, samples}}，
    #     拓扑(depth 由点路径深度给出，层级隐含在 path 键里)/类型/样本三合一，
    #     不建 value_profile / 独立 topology / fields / skeleton_counts。
    if compact:
        skel = _compact_skeleton(backbone, skeleton, template_values, sample_mode,
                                 partition["format"])
        partition["field_paths"] = set(skel.keys())
        log.debug("build_schema_unit %s [%s] compact: 采样 %d 条, 字段 %d 个",
                  unit_id, partition["partition_id"], len(records), len(skel))
        return {
            "id":               unit_id,
            "source_file":      partition["source_file"],
            "format":           partition["format"],
            "partition_id":     partition["partition_id"],
            "skeleton":         skel,
            "skeleton_count_B": skeleton_count_B,
            "record_count":     len(records),
        }

    # 6. 信息四：拓扑（裁剪到主干；noisy 标记时跳过）
    noisy = partition.get("noisy", False)
    topology: Dict = {} if noisy else _build_topology_folded(backbone)

    # union-schema 聚类回填的每字段 presence-rate（叶路径粒度，由 schema_partition
    # 的 UnionSchemaClusterer 算出）。JSON/JSONL 走此真值；其它格式为空 → 兜底 1.0。
    occ_map: Dict[str, float] = partition.get("occurrence") or {}

    # 7. 信息二/三/五：在主干路径上组装 field_info（不依赖 build_vocabulary）
    fields: Dict[str, FieldInfo] = {}
    field_seq = 0
    for path in sorted(backbone):
        field_seq += 1
        field_id = f"f_{su_seq:05d}_{field_seq:02d}"

        key_name = _extract_key_name(path)           # 折叠后路径的终端字段名
        values = template_values.get(path, [])

        # 信息三：value 画像（同模板的所有下标值已聚合，画像不再碎片化）。
        # 默认 sample_mode="off" 不留原值（守 PII 红线）；raw/masked 时按 pattern 去重留样。
        vp = (
            aggregate_profiles(
                [profile_value(v) for v in values], values, sample_mode
            )
            if values else {}
        )

        # occurrence：union-schema 聚类的真值 presence-rate；缺则占位 1.0。
        occ = occ_map.get(path, 1.0)

        # 信息五：PII 种子
        pii = _pii_for_field(key_name, values)

        fields[path] = {
            "field_id":      field_id,
            "key_name":      key_name,
            "occurrence":    occ,
            "required":      occ >= 0.9,
            "value_profile": vp,
            "pii_seed":      pii,
        }

    # 回填 partition 的 field_paths 和 occurrence（保留聚类真值，缺则补 1.0）
    partition["field_paths"] = set(fields.keys())
    partition["occurrence"] = {p: fields[p]["occurrence"] for p in fields}

    log.debug("build_schema_unit %s [%s] mode=%s: 采样 %d 条, B=%d, 字段 %d 个",
              unit_id, partition["partition_id"], mode,
              len(records), skeleton_count_B, len(fields))

    return {
        "id":               unit_id,
        "source_file":      partition["source_file"],
        "format":           partition["format"],
        "partition_id":     partition["partition_id"],
        "skeleton":         skeleton,
        "skeleton_count_B": skeleton_count_B,
        "skeleton_counts":  skeleton_counts,
        "topology":         topology,
        "fields":           fields,
        "record_count":     len(records),
    }


# ── 紧凑 IR skeleton（--ir）─────────────────────────────────────────────────────

def _compact_skeleton(
    backbone: set,
    skeleton: List[Tuple],
    template_values: Dict[str, list],
    sample_mode: str,
    fmt: str,
) -> Dict[str, Dict]:
    """组装紧凑骨架 ``{path: {depth, type, samples}}``。

    - depth：JSON/JSONL 取折叠点路径深度（``path.count(".")+1``，层级隐含在 path 键里，
      如 ``meta.geo.lat`` / ``orders[].amt``）；CSV/TSV 是平表，字段恒为 depth 1
      （列名若含 ``.`` 不应被误判成嵌套）。
    - type：该路径的主类型（多型取 skeleton 的 dominant dtype）。
    - samples：按 pattern 去重的样本值（≤SAMPLE_MAX），受 sample_mode 闸门约束
      （"off" 不留原值，守 PII 红线）。
    """
    tabular = fmt in ("csv", "tsv")
    type_of = {entry[0]: entry[1] for entry in skeleton}
    skel: Dict[str, Dict] = {}
    for path in sorted(backbone):
        values = template_values.get(path, [])
        samples: list = []
        if sample_mode != "off" and values:
            vp = aggregate_profiles(
                [profile_value(v) for v in values], values, sample_mode
            )
            samples = vp.get("samples", [])
        skel[path] = {
            "depth":   1 if tabular else path.count(".") + 1,
            "type":    type_of.get(path, "null"),
            "samples": samples,
        }
    return skel


# ── 单遍折叠遍历 ────────────────────────────────────────────────────────────────

def _type_tag(value: Any) -> str:
    """与 structure_signature 一致的标量类型标记（不含 null，调用方已过滤）。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _walk_fold(
    node: Any, prefix: str, values: Dict[str, list], dtypes: Dict[str, Counter]
) -> None:
    """折叠遍历：list 下标折叠成 []，仅叶子（标量/标量列表元素）落值与类型。

    - dict → 点路径递归 f"{prefix}.{k}"
    - list → 路径加 [] 标记，对【每个】元素递归（保留异构元素字段）
    - 标量 → 在 prefix 处落值；非 null 时记类型计数
    容器节点（dict/list 本身）不作为字段，只有叶子进入 values/dtypes。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            _walk_fold(v, path, values, dtypes)
    elif isinstance(node, list):
        list_path = f"{prefix}[]"
        for item in node:
            if isinstance(item, (dict, list)):
                _walk_fold(item, list_path, values, dtypes)
            else:                                    # list-of-scalar → e.g. tags[]
                values[list_path].append(item)
                if item is not None:
                    dtypes[list_path][_type_tag(item)] += 1
    else:                                            # 标量叶子
        if prefix:
            values[prefix].append(node)
            if node is not None:
                dtypes[prefix][_type_tag(node)] += 1


# ── 字段主干 + skeleton 派生（随 mode 分叉）────────────────────────────────────

def _backbone_and_skeleton(
    mode: str,
    template_values: Dict[str, list],
    dtype_seen: Dict[str, Counter],
    sig_counter: Counter,
) -> Tuple[set, List[Tuple]]:
    """返回 (主干路径集, skeleton 路径列表)。

    - mode="fold"（A）：主干 = 全部折叠路径并集；skeleton dtype 取最高频 + 多型标记。
    - mode="template"（B，默认）：主干 = most_common 签名叶子路径；skeleton 沿用单型签名。
    """
    if mode == "fold":
        backbone = set(template_values.keys())
        skeleton = _skeleton_from_dtype(backbone, dtype_seen)
        return backbone, skeleton

    # 默认 B：模板 schema 路径
    skeleton = _most_common_skeleton_as_path_list(sig_counter)
    backbone = {entry[0] for entry in skeleton}
    return backbone, skeleton


def _skeleton_from_dtype(paths: set, dtype_seen: Dict[str, Counter]) -> List[Tuple]:
    """A 方案：由折叠并集 + 类型计数派生 skeleton。

    每个 entry：
      (path, dtype)                                              # 单型
      (path, dtype, {"multi_type": [...], "dominant_ratio": r})  # 多型（决策 1）
    全 null 路径 dtype 兜底为 "null"，不加多型标记。
    """
    result: List[Tuple] = []
    for path in sorted(paths):
        dt = dtype_seen.get(path)
        if not dt:                                   # 全 null
            result.append((path, "null"))
            continue
        dominant, cnt = dt.most_common(1)[0]
        if len(dt) > 1:
            total = sum(dt.values())
            result.append((
                path,
                dominant,
                {"multi_type": sorted(dt), "dominant_ratio": round(cnt / total, 4)},
            ))
        else:
            result.append((path, dominant))
    return result


def _most_common_skeleton_as_path_list(sig_counter: Counter) -> List[Tuple[str, str]]:
    """B 方案：将最常见骨架签名解析为 [(path, dtype)] 列表。

    签名格式（来自 skeleton.structure_signature）：
      '{"id":"<int>","name":"<str>","scores":["<float>"]}'
    解析为：[("id","int"), ("name","str"), ("scores[]","float")]
    """
    if not sig_counter:
        return []
    most_common_sig = sig_counter.most_common(1)[0][0]
    try:
        # 骨架签名使用非 JSON 的裸类型标记（<int> 等），需先加引号使其合法
        normalized = re.sub(r"<([^>]+)>", r'"<\1>"', most_common_sig)
        sig_obj = json.loads(normalized)
    except Exception as e:
        log.debug("骨架签名解析失败(B 方案返回空主干): %s | sig=%.120s", e, most_common_sig)
        return []
    result: List[Tuple[str, str]] = []
    _walk_sig(sig_obj, "", result)
    return result


def _walk_sig(node: Any, prefix: str, result: list) -> None:
    """解析签名对象为路径列表。

    - dict → 路径用 . 拼接：f"{prefix}.{k}"
    - list → 在前缀加 [] 标记，对首元素递归
    - 标量标记（"<int>"）→ 落地一个 (prefix, dtype)

    压平示例：
      [("id","int"), ("orders[].amt","float"), ("orders[].oid","str"),
       ("tags[]","str"), ("user.age","int"), ("user.name","str")]
    """
    if isinstance(node, dict):
        for k, v in sorted(node.items()):
            path = f"{prefix}.{k}" if prefix else k
            _walk_sig(v, path, result)
    elif isinstance(node, list):
        if node:
            _walk_sig(node[0], f"{prefix}[]", result)
    elif isinstance(node, str) and node.startswith("<") and node.endswith(">"):
        dtype = node[1:-1]  # "<int>" → "int"
        if prefix:
            result.append((prefix, dtype))


# ── 折叠拓扑 ────────────────────────────────────────────────────────────────────

def _parent_path(path: str) -> Optional[str]:
    """折叠路径的父级：去掉最后一个 . 分隔段（[] 随段保留）。

    'orders[].amt' → 'orders[]'；'meta.geo.lat' → 'meta.geo'；
    'id' / 'tags[]'（无 .）→ None（顶层）。
    """
    return path.rsplit(".", 1)[0] if "." in path else None


def _expand_with_ancestors(paths: set) -> set:
    """把每个叶子路径的所有折叠祖先容器节点补全进集合。

    'meta.geo.lat' → 补 'meta.geo'、'meta'；'orders[].amt' → 补 'orders[]'；
    'user.name' → 补 'user'。顶层叶子（'id'/'tags[]'）无祖先，仅保留自身。
    """
    expanded = set()
    for p in paths:
        cur: Optional[str] = p
        while cur is not None:
            expanded.add(cur)
            cur = _parent_path(cur)
    return expanded


def _build_topology_folded(paths: set) -> Dict[str, Dict]:
    """在折叠路径主干上构建拓扑，**含中间容器节点**。

    backbone（paths）仅含叶子路径；这里先用 _expand_with_ancestors 把每个叶子的
    所有折叠祖先容器节点（如 meta.geo.lat → meta、meta.geo；orders[].amt → orders[]）
    补全，再在补全后的全节点集上统一计算拓扑——容器节点与叶子一视同仁，同样获得
    depth / parent / siblings。

    depth 仅按 . 深度（[] 不计层级）：depth = path.count(".") + 1。
    parent / siblings 基于折叠路径，siblings 同父去重（含同父的容器与叶子节点）。
    """
    nodes = _expand_with_ancestors(paths)
    parent_of: Dict[str, Optional[str]] = {p: _parent_path(p) for p in nodes}
    children: Dict[Optional[str], list] = defaultdict(list)
    for p, par in parent_of.items():
        children[par].append(p)

    topo: Dict[str, Dict] = {}
    for p in nodes:
        par = parent_of[p]
        topo[p] = {
            "depth":    p.count(".") + 1,
            "parent":   par,
            "siblings": sorted(s for s in children[par] if s != p),
        }
    return topo


# ── 字段名 / PII ────────────────────────────────────────────────────────────────

def _extract_key_name(path: str) -> str:
    """从路径末段提取字段名，去掉列表标记。
    'user.name'     → 'name'
    'orders[].amt'  → 'amt'
    'tags[]'        → 'tags'
    """
    last = path.split(".")[-1]
    bracket = last.find("[")
    return last[:bracket] if bracket >= 0 else last


def _pii_for_field(
    field_name: str, values: list
) -> Optional[Tuple[str, Optional[str]]]:
    """判断字段是否为 PII 种子，返回 (level, pii_type) 或 None。"""
    if key_name_implies_pii(field_name):
        return "high_conf", infer_pii_type(field_name)
    str_values = [v for v in values if isinstance(v, str)]
    if is_free_text_field(str_values):
        return "needs_llm", None
    return None

"""vocab_table（build_vocab_table）：全局词汇表构建。

跨所有 SchemaUnit 做 key 语义对齐，产出跨表同义倒排表 VocabTable。

三证据联合聚类：
  B (value 画像相似) + C (PII 类型一致) → 粗聚类
  A (字符串相似度)                       → 簇内校正 / 冲突检测
"""

import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Dict, List,  Tuple

from src.extract.schema_types import KeyEntry, SchemaUnit, VocabTable
from src.utils.logger import get_logger

log = get_logger(__name__)

# 阈值常量
_PROFILE_SIM_THRESHOLD = 0.7   # value 画像相似度 ≥ 此值视为同义
_STR_SPLIT_THRESHOLD   = 0.45  # 字符串相似度 < 此值时标记冲突（uncertain）


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def build_vocab_table(
    schema_units: List[SchemaUnit],
) -> Tuple[VocabTable, List[Dict]]:
    """跨所有 SchemaUnit 构建同义倒排表。

    Returns:
        vocab_table   : VocabTable
        uncertain_list: 多证据冲突的存疑项，留待后续强模型处理
    """
    entries = _collect_key_entries(schema_units)
    if not entries:
        log.info("build_vocab_table: 无字段 entry, 返回空表")
        return {}, []

    clusters = _initial_clusters_by_bc(entries)
    vocab_table, uncertain_list = _build_inverted_index(clusters, entries)
    log.info("build_vocab_table: %d 字段 entry → %d 聚类 → %d 语义类, %d uncertain",
             len(entries), len(clusters), len(vocab_table), len(uncertain_list))
    return vocab_table, uncertain_list


# ── Step 1 ────────────────────────────────────────────────────────────────────

def _collect_key_entries(schema_units: List[SchemaUnit]) -> List[KeyEntry]:
    """遍历所有 SchemaUnit.fields，收集 KeyEntry 列表。"""
    entries: List[KeyEntry] = []
    for unit in schema_units:
        for path, info in unit.get("fields", {}).items():
            entries.append({
                "key_name":       info["key_name"],
                "path":           path,
                "schema_unit_id": unit["id"],
                "field_id":       info["field_id"],
                "value_profile":  info.get("value_profile", {}),
                "pii_seed":       info.get("pii_seed"),
            })
    return entries


# ── Step 2 ────────────────────────────────────────────────────────────────────

def _initial_clusters_by_bc(entries: List[KeyEntry]) -> List[List[int]]:
    """B + C 粗聚类，返回 clusters（每簇为 entry 下标列表）。

    C: 相同 high_conf PII 类型 → Union
    B: value 画像相似度 ≥ _PROFILE_SIM_THRESHOLD → Union（仅跨簇时）
    """
    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # C: 相同 high_conf PII 类型归一簇
    pii_groups: Dict[str, list] = defaultdict(list)
    for i, e in enumerate(entries):
        pii = e.get("pii_seed")
        if pii and pii[0] == "high_conf":
            pii_type = pii[1]
            if pii_type:
                pii_groups[pii_type].append(i)

    for idxs in pii_groups.values():
        for j in range(1, len(idxs)):
            union(idxs[0], idxs[j])

    # B: 同一 SchemaUnit 内不做跨字段合并（防止误聚）
    # 只对不同 schema_unit_id 之间的字段做画像相似判断
    su_by_idx = {i: e["schema_unit_id"] for i, e in enumerate(entries)}

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if su_by_idx[i] == su_by_idx[j]:
                continue  # 同表字段不因画像相似而合并
            sim = profile_similarity(
                entries[i]["value_profile"],
                entries[j]["value_profile"],
            )
            if sim >= _PROFILE_SIM_THRESHOLD:
                union(i, j)

    groups: Dict[int, list] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    return list(groups.values())


# ── Step 3 ────────────────────────────────────────────────────────────────────

def _build_inverted_index(
    clusters: List[List[int]],
    entries: List[KeyEntry],
) -> Tuple[VocabTable, List[Dict]]:
    """根据聚类结果建倒排表，检测字符串冲突标记 uncertain。"""
    vocab_table: VocabTable = {}
    uncertain_list: List[Dict] = []

    for cluster_idxs in clusters:
        cluster_entries = [entries[i] for i in cluster_idxs]
        semantic_class = _assign_semantic_class(cluster_entries)

        # A: 检测簇内字符串冲突（标记 uncertain，不拆分）
        if _has_string_conflict(cluster_entries):
            uncertain_list.append({
                "semantic_class":  semantic_class,
                "key_names":       list({e["key_name"] for e in cluster_entries}),
                "candidates":      [semantic_class, "<UNKNOWN>"],
                "schema_unit_ids": list({e["schema_unit_id"] for e in cluster_entries}),
            })

        if semantic_class not in vocab_table:
            vocab_table[semantic_class] = {}

        for e in cluster_entries:
            kn = e["key_name"]
            if kn not in vocab_table[semantic_class]:
                vocab_table[semantic_class][kn] = []
            su_id = e["schema_unit_id"]
            if su_id not in vocab_table[semantic_class][kn]:
                vocab_table[semantic_class][kn].append(su_id)

    return vocab_table, uncertain_list


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _assign_semantic_class(cluster_entries: List[KeyEntry]) -> str:
    """优先用 high_conf PII 类型命名；无 PII 则用最高频 key_name。"""
    pii_types = [
        e["pii_seed"][1]
        for e in cluster_entries
        if e.get("pii_seed")
        and e["pii_seed"][0] == "high_conf"
        and e["pii_seed"][1]
    ]
    if pii_types:
        most_common = Counter(pii_types).most_common(1)[0][0]
        return f"<{most_common.upper()}>"

    key_names = [e["key_name"] for e in cluster_entries]
    return Counter(key_names).most_common(1)[0][0] if key_names else "<unknown>"


def _has_string_conflict(cluster_entries: List[KeyEntry]) -> bool:
    """簇内存在字符串相似度 < 阈值的 key 对时返回 True。"""
    unique_names = list({e["key_name"] for e in cluster_entries})
    for i in range(len(unique_names)):
        for j in range(i + 1, len(unique_names)):
            if _string_similarity(unique_names[i], unique_names[j]) < _STR_SPLIT_THRESHOLD:
                return True
    return False


def _string_similarity(s1: str, s2: str) -> float:
    """基于 SequenceMatcher 的字符串相似度，忽略大小写和常见分隔符。"""
    def normalize(s: str) -> str:
        return re.sub(r"[_\-\s]+", "", s.lower())

    n1, n2 = normalize(s1), normalize(s2)
    if not n1 or not n2:
        return 0.0
    return SequenceMatcher(None, n1, n2).ratio()


def profile_similarity(p1: Dict, p2: Dict) -> float:
    """value 画像相似度占位实现。

    当前逻辑：
      - type 不同 → 0.0
      - type 相同且有 len_dist → 0.5 + 0.5 × min/max 均值比
      - type 相同无 len_dist   → 0.8
    后续随 profile_value 完善时替换为真实度量。
    """
    t1 = p1.get("type", "")
    t2 = p2.get("type", "")
    if not t1 or not t2 or t1 != t2:
        return 0.0

    ld1 = p1.get("len_dist", {})
    ld2 = p2.get("len_dist", {})
    if ld1 and ld2:
        m1 = ld1.get("mean", 0)
        m2 = ld2.get("mean", 0)
        if m1 > 0 and m2 > 0:
            return 0.5 + 0.5 * min(m1, m2) / max(m1, m2)

    return 0.8  # 类型相同但无长度信息，保守认为相似

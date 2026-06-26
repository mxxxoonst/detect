"""Q2：CSV schema 级去重（保留重数）—— 跨文件语料级后处理。

真实语料里大量 CSV 文件**共享同一 schema**（split-dump ``part-0001..``、无列名编号
文件 ``1.csv``..``N.csv``）。研究原子是 **distinct schema 而非文件**：按文件计数会
拉高 CSV 占比、失真分布。本模块对 CSV/TSV SchemaUnit 做语料级去重：

    每文件算 schema 指纹 → 跨文件夹全局聚类 → 每簇留 1 个代表 + cluster_size 频率权重。

dedup-with-multiplicity（同 LM 预训练去重）：训练吃 distinct schema（别让编码器过拟合
最高频布局），保留 cluster_size 用于分布标定 / Tier2 频率权重。

指纹（复用 value_profile 的同一套类型系统，不造第三套分类器）：
  - **有列名**：归一化表头元组（小写 / strip / strip 引号，保留顺序）。
  - **无列名**：``(众数列数, 各列非空宏类签名元组)``；**空 cell 当通配**（§2.2.4 的
    null 多态在列层重演——不做会把同族文件拆散）。

与现有流式架构一致：作用在 SchemaUnit 的 ``source_file`` 上，逐文件**流式** quote 感知
读头部若干行算指纹（``csv.reader``，禁 ``split(sep)``），不在内存累全量原始记录。
聚类内存 O(distinct schema 数)。

⚠ §3.3.2（表头探测）/ §3.3.3（数值类型归并阈值）的硬化**依赖真实语料标定，不在本次
范围**（标 TODO）；本模块只做能在小样本上验证的逻辑。
"""

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from src.extract.schema_types import CsvDedupReport, CsvSchemaCluster, SchemaUnit
from src.extract.value_profile import _macro_class
from src.utils.jsonl import iter_jsonl
from src.utils.logger import get_logger

log = get_logger(__name__)

# 算指纹时读的最大行数（quote 感知；只读头部，不整文件 load）。
_FINGERPRINT_ROWS = 60


# ── 列宏类（复用 value_profile 的 _macro_class 单一真源）──────────────────────

def _cell_class(cell: str) -> Optional[str]:
    """单元格 → 列层宏类标记；空 cell 返回 None（通配，不携带 schema 信息）。

    复用 value_profile._macro_class 对**主导字符**的判定，避免造第三套分类器：
    取非空白字符的多数宏桶；纯数字 → "num"，含 letter → "str"，其余按多数桶。

    ⚠ §3.3.3 更细的类型格（email⊏string、phone⊏numeric）依赖真实语料标定，
    TODO：远程全量调阈值后再细化；当前只用 value_profile 的粗宏桶。
    """
    s = cell.strip()
    if not s:
        return None
    macro: Counter = Counter()
    for ch in s:
        if ch.isspace():
            continue
        macro[_macro_class(ch)] += 1
    if not macro:
        return None
    if macro.get("letter", 0) > 0:
        return "str"
    if set(macro) <= {"number"}:
        return "num"
    if set(macro) <= {"number", "punct"}:
        # 12,345 / 1.5 / 330-003 等纯数字+标点 → 数值类
        return "num"
    return macro.most_common(1)[0][0]


# ── 表头探测（quote 感知；§3.3.2 硬化为 TODO）────────────────────────────────

def _is_pure_num(s: str) -> bool:
    s = s.strip().strip('"')
    if not s:
        return False
    return all(_macro_class(ch) in ("number", "punct") for ch in s) and any(
        _macro_class(ch) == "number" for ch in s
    )


def _has_header(rows: List[List[str]]) -> bool:
    """启发式：row0 无纯数字列、但数据行同位置出现纯数字 ⟹ 有表头。

    ⚠ §3.3.2：此单一数字判据两个方向都会错（漏判全字符串表头 / 误判首行全文本数据）。
    TODO：远程全量上推广到 date/bool/float 多数表决 + 词法线索 + 置信度路由弃权。
    本次只做可在小样本验证的版本。
    """
    if len(rows) < 2:
        return False
    r0, r1 = rows[0], rows[1]
    if len(r0) != len(r1):
        return False
    r0_anynum = any(_is_pure_num(c) for c in r0)
    r1_anynum = any(_is_pure_num(c) for c in r1)
    return (not r0_anynum) and r1_anynum


# ── 指纹（流式 quote 感知读文件头部）──────────────────────────────────────────

# 有列名指纹: ("H", (归一化表头元组,))
# 无列名指纹: ("V", 众数列数, (每列非空宏类 frozenset,))
HeaderFp = Tuple[str, Tuple[str, ...]]
ValueFp = Tuple[str, int, Tuple[FrozenSet[str], ...]]
Fingerprint = Tuple


def _read_rows(path: str, sep: str, encoding: str, limit: int) -> List[List[str]]:
    """quote 感知读前 limit 行（csv.reader，尊重引号内分隔符 / 跨行字段）。"""
    rows: List[List[str]] = []
    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
            cleaned = (line.replace("\x00", "") for line in f)
            for row in csv.reader(cleaned, delimiter=sep):
                if row and any(c.strip() for c in row):
                    rows.append(row)
                if len(rows) >= limit:
                    break
    except OSError as e:
        log.debug("CSV 指纹读取失败 %s: %s", path, e)
    return rows


def csv_fingerprint(
    path: str, sep: str, encoding: str = "utf-8"
) -> Tuple[Optional[Fingerprint], bool, str]:
    """对单个 CSV 文件算 schema 指纹（流式 quote 感知）。

    Returns:
        (fingerprint, has_header, human_desc)；读不到内容时 fingerprint=None。
    """
    rows = _read_rows(path, sep, encoding, _FINGERPRINT_ROWS)
    if not rows:
        return None, False, "empty"

    if _has_header(rows):
        header = tuple(c.strip().strip('"').lower() for c in rows[0])
        fp: Fingerprint = ("H", header)
        desc = "HEADER " + ",".join(header[:8]) + ("..." if len(header) > 8 else "")
        return fp, True, desc

    # 无列名：众数列数 + 每列非空宏类集（空 cell 当通配，不参与签名）
    ncols = Counter(len(r) for r in rows).most_common(1)[0][0]
    colsets: List[FrozenSet[str]] = []
    for j in range(ncols):
        s: Set[str] = set()
        for r in rows:
            if len(r) == ncols and j < len(r):
                c = _cell_class(r[j])
                if c is not None:
                    s.add(c)
        colsets.append(frozenset(s))
    fp = ("V", ncols, tuple(colsets))
    valsig = "".join("".join(sorted(s)) if s else "*" for s in colsets)
    desc = f"NOHEADER cols={ncols} valsig={valsig[:48]}{'...' if len(valsig) > 48 else ''}"
    return fp, False, desc


def _v_compatible(a: ValueFp, b: ValueFp) -> bool:
    """两个无列名指纹是否兼容：同列数、每列非空宏类集不冲突（允许一方为空集）。

    空集 = 该列采样里全空（null 多态在列层重演，§2.2.4）——不挡合并。
    """
    if a[0] != "V" or b[0] != "V" or a[1] != b[1]:
        return False
    for sa, sb in zip(a[2], b[2]):
        if sa and sb and sa.isdisjoint(sb):
            return False
    return True


# ── 主入口：对 CSV/TSV SchemaUnit 做语料级去重 ────────────────────────────────

def _sep_for_unit(unit: SchemaUnit) -> str:
    """SchemaUnit 的分隔符：tsv → \\t，其余沿用 schema_partition 的嗅探。"""
    if unit.get("format") == "tsv":
        return "\t"
    from src.extract.schema_partition import _sniff_sep
    enc = _unit_encoding(unit)
    return _sniff_sep(unit["source_file"], enc)


def _unit_encoding(unit: SchemaUnit) -> str:
    """SchemaUnit 未持有 encoding（不在 TypedDict 内）→ 兜底 utf-8（errors=replace 读）。"""
    return "utf-8"


def dedup_csv_schemas(units_iter) -> CsvDedupReport:
    """对一批 SchemaUnit 中的 CSV/TSV 单元做 schema 级去重。

    两遍逻辑但内存 O(distinct schema 数)：逐 unit 流式算指纹（quote 感知读文件头部，
    不累原始记录）→ 精确指纹分桶 → 兼容的无列名桶再合并（空 cell 通配）。
    非 CSV/TSV unit 直接跳过。

    Args:
        units_iter: 可迭代的 SchemaUnit（list 或 iter_jsonl 惰性流）。

    Returns:
        CsvDedupReport（distinct schema 数 + 每簇代表 + cluster_size 频率权重）。
    """
    # exact[fp] = [(unit_id, source_file), ...]
    exact: Dict[Fingerprint, List[Tuple[str, str]]] = {}
    desc_of: Dict[Fingerprint, str] = {}
    total = 0

    for unit in units_iter:
        if unit.get("format") not in ("csv", "tsv"):
            continue
        total += 1
        path = unit["source_file"]
        sep = _sep_for_unit(unit)
        fp, _hdr, desc = csv_fingerprint(path, sep, _unit_encoding(unit))
        if fp is None:
            fp = ("EMPTY", path)          # 空文件各成一簇，不误并
            desc = "empty"
        exact.setdefault(fp, []).append((unit["id"], path))
        desc_of.setdefault(fp, desc)

    # 兼容合并：无列名（"V"）桶按 _v_compatible 贪心并入第一个兼容簇；其余精确即一簇。
    # merged[i] = {"fp":..., "desc":..., "members":[...], "files":[...]}
    merged: List[Dict] = []
    for fp, items in exact.items():
        placed = False
        if fp[0] == "V":
            for m in merged:
                if m["fp"][0] == "V" and _v_compatible(m["fp"], fp):  # type: ignore[arg-type]
                    # 取每列宏类集的并作为新代表指纹
                    newcols = tuple(
                        frozenset(a | b) for a, b in zip(m["fp"][2], fp[2])
                    )
                    m["fp"] = ("V", fp[1], newcols)
                    m["members"].extend(uid for uid, _ in items)
                    m["files"].extend(f for _, f in items)
                    placed = True
                    break
        if not placed:
            merged.append({
                "fp":      fp,
                "desc":    desc_of[fp],
                "members": [uid for uid, _ in items],
                "files":   [f for _, f in items],
            })

    # 按簇大小降序输出
    merged.sort(key=lambda m: -len(m["members"]))
    clusters: List[CsvSchemaCluster] = []
    for i, m in enumerate(merged):
        clusters.append({
            "cluster_id":          f"csv_clu_{i:03d}",
            "representative":      m["members"][0],
            "representative_file": m["files"][0],
            "has_header":          m["fp"][0] == "H",
            "fingerprint":         m["desc"],
            "cluster_size":        len(m["members"]),
            "members":             m["members"],
            "member_files":        m["files"],
        })

    report: CsvDedupReport = {
        "total_csv_units":  total,
        "exact_buckets":    len(exact),
        "distinct_schemas": len(merged),
        "clusters":         clusters,
    }
    log.info("CSV schema 去重: %d 个 CSV unit → 精确指纹桶 %d → distinct schema %d",
             total, len(exact), len(merged))
    return report


# ── 非 CSV（JSON/JSONL/SQL/XLSX…）按 skeleton 形状签名去重 ─────────────────────

# 数值类型归一: int/float/num 同视为 "num"（避免采样把同列推成 int vs float 而拆散）。
_NUM_TYPES = {"int", "float", "num"}


def _norm_type(t: Optional[str]) -> Optional[str]:
    """类型归一: int/float/num → 'num'; null/None → None（**通配**，不阻断合并）;
    其余（str/bool/…）原样保留——保留「数值 vs 字符串」这类真实区分。"""
    if t in _NUM_TYPES:
        return "num"
    if t in (None, "null"):
        return None
    return t


def _skeleton_typed(
    unit: SchemaUnit,
) -> Optional[Tuple[Tuple[str, ...], Tuple[Optional[str], ...]]]:
    """抽 ``(path 元组, 归一 type 向量)``，按 path 排序对齐。空/缺骨架返回 None。

    - 紧凑 IR：skeleton 是 ``{path: {depth,type,samples}}`` 字典。
    - 冗长形态：skeleton 是 ``[(path, dtype[, meta]), ...]`` 列表。
    不读源文件——结构已在 unit 里。
    """
    sk = unit.get("skeleton")
    items: List[Tuple[str, Optional[str]]] = []
    if isinstance(sk, dict):
        items = [(p, m.get("type")) for p, m in sk.items()]
    elif isinstance(sk, list):
        items = [(e[0], e[1] if len(e) >= 2 else None)
                 for e in sk if isinstance(e, (list, tuple)) and len(e) >= 1]
    if not items:
        return None
    items.sort(key=lambda x: x[0])
    paths = tuple(p for p, _ in items)
    types = tuple(_norm_type(t) for _, t in items)
    return paths, types


def _types_compatible(a: Tuple, b: Tuple) -> bool:
    """逐位兼容: 任一为通配(None) 或两者相等 → 兼容; 两个具体且不等(如 str vs num) → 冲突。"""
    return all(x is None or y is None or x == y for x, y in zip(a, b))


def _merge_type_vec(a: Tuple, b: Tuple) -> Tuple:
    """合并两个兼容 type 向量: 通配位用对方的具体类型填实。"""
    return tuple(x if x is not None else y for x, y in zip(a, b))


def dedup_json_schemas(units_iter) -> Dict:
    """对**非 CSV/TSV** 单元做 schema 去重（保留重数）：**path 语义相同 + 归一 type 兼容**才合并。

    JSON/JSONL 大量同 schema 文件（分卷导出 ``part-*``、同表多文件）、以及一个 SQL
    dump 里列布局相同的众多表 → 折叠为代表 + ``cluster_size``。

    合并规则（type 归一折中，不丢弃 type）：
      1. **path 集必须完全相同**（字段语义身份）；
      2. 在此前提下，逐列归一 type 兼容才并：int/float→num（消除采样噪声）、
         null→通配（全空采样不阻断），但 **str vs num 等真实冲突仍分簇**。
    贪心聚类（同 CSV 的 V 兼容合并），不读源文件，内存 O(distinct schema 数)。

    Returns:
        {total_units, distinct_schemas, clusters:[{representative, cluster_size, types, ...}]}。
    """
    # groups[(fmt, paths)] = [cluster, ...]; cluster = {types, members:[(id,file)]}
    groups: Dict[Tuple, List[Dict]] = {}
    uniq: List[Dict] = []                             # 无骨架 → 各自成簇，不误并
    total = 0
    for unit in units_iter:
        if unit.get("format") in ("csv", "tsv"):
            continue
        total += 1
        fmt = unit.get("format", "?")
        member = (unit["id"], unit["source_file"])
        typed = _skeleton_typed(unit)
        if typed is None:
            uniq.append({"fmt": fmt, "paths": (), "types": (), "members": [member]})
            continue
        paths, types = typed
        bucket = groups.setdefault((fmt, paths), [])
        for c in bucket:                              # 同 path 组内按 type 兼容贪心并入
            if _types_compatible(c["types"], types):
                c["types"] = _merge_type_vec(c["types"], types)
                c["members"].append(member)
                break
        else:
            bucket.append({"fmt": fmt, "paths": paths, "types": types, "members": [member]})

    all_clusters = uniq + [c for bucket in groups.values() for c in bucket]
    all_clusters.sort(key=lambda c: -len(c["members"]))
    clusters: List[Dict] = []
    for i, c in enumerate(all_clusters):
        paths = c["paths"]
        if paths:
            head = ",".join(paths[:8])
            desc = f"{c['fmt']} {len(paths)}f {head}{'...' if len(paths) > 8 else ''}"
        else:
            desc = f"{c['fmt']} (no skeleton)"
        clusters.append({
            "cluster_id":          f"sch_clu_{i:03d}",
            "representative":      c["members"][0][0],
            "representative_file": c["members"][0][1],
            "format":              c["fmt"],
            "fingerprint":         desc,
            "types":               [t or "*" for t in c["types"]],
            "cluster_size":        len(c["members"]),
            "members":             [m for m, _ in c["members"]],
            "member_files":        [f for _, f in c["members"]],
        })
    log.info("非 CSV schema 去重(path+归一type): %d 个 unit → distinct schema %d",
             total, len(all_clusters))
    return {"total_units": total, "distinct_schemas": len(all_clusters), "clusters": clusters}


def representative_sizes(*reports) -> Dict[str, int]:
    """汇总多份去重报告的 ``{代表 unit_id: cluster_size}``（CSV + 非 CSV 并集）。"""
    out: Dict[str, int] = {}
    for rep in reports:
        for c in rep.get("clusters", []):
            out[c["representative"]] = c["cluster_size"]
    return out


def rewrite_units_deduped(units_path, rep_size: Dict[str, int]) -> Tuple[int, int]:
    """把 schema_units.jsonl 重写为去重后的 IR（dedup-with-multiplicity 落盘）。

    ``rep_size`` 是**所有格式**每簇代表 → cluster_size 的并集（由 representative_sizes
    汇总 CSV + 非 CSV 报告得到，含 size==1 的 singleton）。每条 unit：

        id ∈ rep_size  → 保留并打 cluster_size（=1 也打，表示该 schema 仅此一文件）；
        id ∉ rep_size  → 同 schema 的非代表重复 → 丢弃。

    流式读 → 写临时文件 → 原子替换，内存 O(distinct schema 数)。

    Returns:
        (kept, dropped)。
    """
    units_path = Path(units_path)
    tmp = units_path.with_name(units_path.name + ".tmp")
    kept = dropped = 0
    with open(tmp, "w", encoding="utf-8", errors="replace") as out:
        for u in iter_jsonl(units_path):
            rid = u.get("id")
            if rid not in rep_size:                   # 非代表的同 schema 重复 → 丢弃
                dropped += 1
                continue
            u["cluster_size"] = rep_size[rid]         # 代表打重数权重
            out.write(json.dumps(u, ensure_ascii=False, default=str))
            out.write("\n")
            kept += 1
    tmp.replace(units_path)
    log.info("schema 去重落盘: 保留 %d 代表 unit, 丢弃 %d 个同 schema 重复", kept, dropped)
    return kept, dropped

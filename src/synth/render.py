"""模板实例渲染：紧凑 IR SchemaUnit → 对应 format 的合成文档（结构精确、确定可复现）。

核心两步：
  1. 反折叠：把折叠路径（``a.b``、``orders[].amt``、``tags[]``）还原成嵌套模板树
     （``schema_unit._walk_fold`` 的逆）。
  2. 按 type 现场造值——**不回放 skeleton.samples**。samples 是跨记录抽、按 pattern 去重、
     可能脱敏/截断（尾带 ``…``）的形态证据；回放会注入 artifact，``--keep-samples`` 不带
     mask 时更会把真实 PII 二次落进合成语料。samples 仅作长度/字符类提示。

format 路由：json→顶层数组、jsonl→逐行对象、csv/tsv→表头+数据行、sql→CREATE+INSERT、
xlsx→**csv 代理**（每 sheet 已是独立平表 unit，渲成 CSV 零结构损失，且把 openpyxl 踢出循环）。

值生成可插拔（``value_fn``）：默认随机 token；LLM 渲染的离线桩传入「拟真」provider，
未来 free-text 叶子的 LLM 填值也走同一钩子。
"""

import csv
import hashlib
import io
import json
import random
from typing import Any, Callable, Dict, List, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

# 每个 list 路径渲染的元素个数（够暴露 `[]` 折叠即可，不追求体量）
_LIST_K = 2
# 默认每文档记录数（结构信号足矣；CSV 需 ≥2 行供列稳定性判定）
_DEFAULT_N = 3
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

# xlsx 无独立表面：每 sheet 是平表，渲成 csv 代理
_SURFACE = {"xlsx": "csv"}

# 渲染叶值的回调签名：(field_name, type, samples, rng) -> 标量值
ValueFn = Callable[[str, Optional[str], Optional[list], random.Random], Any]


def surface_format(fmt: str) -> str:
    """渲染表面格式：xlsx→csv，其余原样。校验/落盘扩展名据此决定。"""
    return _SURFACE.get(fmt, fmt)


# ── 公开入口 ────────────────────────────────────────────────────────────────────

def render_unit(
    unit: Dict[str, Any],
    n_records: Optional[int] = None,
    seed: Optional[int] = None,
    value_fn: Optional[ValueFn] = None,
) -> str:
    """把一个紧凑 IR SchemaUnit 渲染成其（表面）format 的文本文档。

    Args:
        unit:      含 ``format`` / ``skeleton{path:{type,samples,...}}`` / ``partition_id`` 的单元。
        n_records: 记录数，缺省 _DEFAULT_N（tabular 自动 ≥2）。
        seed:      随机种子，缺省由 unit id 派生 → 确定可复现。
        value_fn:  叶值生成回调，缺省随机 token（不回放样本）。

    Returns:
        渲染文本；无 skeleton 或不可结构化渲染的 format 返回 ""。
    """
    fmt = surface_format(unit.get("format", "json"))
    skeleton = unit.get("skeleton") or {}
    if not skeleton or not isinstance(skeleton, dict):
        log.debug("render_unit %s: 空/非紧凑 skeleton，跳过", unit.get("id"))
        return ""

    seed = seed if seed is not None else _seed_of(unit.get("id", ""))
    n = n_records if n_records is not None else _DEFAULT_N
    #value 生成回调函数
    vfn = value_fn or _default_value

    if fmt in ("csv", "tsv"):
        return _render_tabular(skeleton, max(n, 2), fmt, seed, vfn)
    if fmt == "sql":
        return _render_sql(unit, skeleton, n, seed, vfn)
    if fmt == "jsonl":
        return _render_jsonl(skeleton, n, seed, vfn)
    if fmt == "json":
        return _render_json(skeleton, n, seed, vfn)
    log.debug("render_unit %s: format=%s 无结构化渲染器，跳过", unit.get("id"), fmt)
    return ""


# ── 反折叠：路径集 → 嵌套模板树 ──────────────────────────────────────────────────

def _split_segment(seg: str) -> tuple[str, int]:
    """拆一个点路径段为 (名字, list 深度)。``orders[]``→('orders',1)；``m[][]``→('m',2)。"""
    depth = 0
    while seg.endswith("[]"):
        seg = seg[:-2]
        depth += 1
    return seg, depth


def _build_tree(skeleton: Dict[str, Dict]) -> Dict[str, Any]:
    """把折叠路径集还原成模板树。节点三类：

    - ``{"kind":"obj","children":{name:node}}``
    - ``{"kind":"list","elem":node}``
    - ``{"kind":"leaf","type":t,"samples":[...]}``
    """
    root: Dict[str, Any] = {"kind": "obj", "children": {}}
    for path, meta in skeleton.items():
        comps = [_split_segment(s) for s in path.split(".")]
        _insert(root, comps, meta.get("type", "str"), meta.get("samples") or [])
    return root


def _insert(node: Dict, comps: List[tuple], t: str, samples: list) -> None:
    """沿点路径段插入叶子，按 list 深度包 list 包装层。"""
    for i, (name, ld) in enumerate(comps):
        last = i == len(comps) - 1
        existing = node["children"].get(name)
        if existing is None:
            inner: Dict[str, Any] = (
                {"kind": "leaf", "type": t, "samples": samples}
                if last else {"kind": "obj", "children": {}}
            )
            wrapped = inner
            for _ in range(ld):
                wrapped = {"kind": "list", "elem": wrapped}
            node["children"][name] = wrapped
            existing = wrapped
        # 穿过 ld 层 list 包装到内层节点
        inner = existing
        for _ in range(ld):
            if inner.get("kind") == "list":
                inner = inner["elem"]
        if last:
            return
        if inner.get("kind") == "obj":      # 继续下钻；类型冲突则就此打住（容错）
            node = inner
        else:
            return


def _gen_value(node: Dict, rng: random.Random, vfn: ValueFn, field_name: str = "") -> Any:
    """按模板树递归造一条实例值。"""
    kind = node["kind"]
    if kind == "leaf":
        return vfn(field_name, node.get("type"), node.get("samples"), rng)
    if kind == "obj":
        return {name: _gen_value(child, rng, vfn, name)
                for name, child in node["children"].items()}
    # list
    return [_gen_value(node["elem"], rng, vfn, field_name) for _ in range(_LIST_K)]


# ── 默认值生成（随机；不回放样本）──────────────────────────────────────────────

def _default_value(field_name: str, t: Optional[str], samples: Optional[list],
                   rng: random.Random) -> Any:
    """按类型造随机值。samples 仅用于长度提示（绝不回放）。"""
    t = (t or "str").lower()
    if t in ("int", "num"):
        return rng.randint(0, 99999)
    if t == "float":
        return round(rng.uniform(0, 9999), 2)
    if t == "bool":
        return rng.choice([True, False])
    if t in ("null", "none"):
        return None
    hint = _len_hint(samples)
    length = hint if hint is not None else rng.randint(4, 12)
    return "".join(rng.choice(_ALPHABET) for _ in range(length))


def _len_hint(samples: Optional[list]) -> Optional[int]:
    """从 sample 取长度提示：跳过截断（含 ``…``）/脱敏（多 ``*``）的脏样本。"""
    for s in samples or []:
        if isinstance(s, str) and "…" not in s and not _is_masked(s):
            return max(1, min(len(s), 24))
    return None


def _is_masked(s: str) -> bool:
    if not s:
        return False
    return s.count("*") >= 0.6 * len(s)


# ── JSON / JSONL ────────────────────────────────────────────────────────────────

def _render_json(skeleton: Dict, n: int, seed: int, vfn: ValueFn) -> str:
    rng = random.Random(seed)
    tree = _build_tree(skeleton)
    records = [_gen_value(tree, rng, vfn) for _ in range(n)]
    return json.dumps(records, ensure_ascii=False, indent=2)


def _render_jsonl(skeleton: Dict, n: int, seed: int, vfn: ValueFn) -> str:
    rng = random.Random(seed)
    tree = _build_tree(skeleton)
    lines = [json.dumps(_gen_value(tree, rng, vfn), ensure_ascii=False) for _ in range(n)]
    return "\n".join(lines) + "\n"


# ── CSV / TSV（含 xlsx→csv 代理）────────────────────────────────────────────────

def _render_tabular(skeleton: Dict, n: int, fmt: str, seed: int, vfn: ValueFn) -> str:
    rng = random.Random(seed)
    cols = list(skeleton.keys())
    delim = "\t" if fmt == "tsv" else ","
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="\n")
    w.writerow(cols)
    for _ in range(n):
        w.writerow([_csv_cell(vfn(c, skeleton[c].get("type"),
                                  skeleton[c].get("samples"), rng)) for c in cols])
    return buf.getvalue()


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# ── SQL ─────────────────────────────────────────────────────────────────────────

def _render_sql(unit: Dict, skeleton: Dict, n: int, seed: int, vfn: ValueFn) -> str:
    """CREATE TABLE（列各占一行，配合 _partition_sql 的多行解析）+ N 条 INSERT。"""
    rng = random.Random(seed)
    table = _safe_ident(unit.get("partition_id") or "t")
    cols = list(skeleton.keys())
    coldefs = ",\n  ".join(f"`{c}` {_sql_type(skeleton[c].get('type'))}" for c in cols)
    lines = [f"CREATE TABLE `{table}` (\n  {coldefs}\n);"]
    collist = ", ".join(f"`{c}`" for c in cols)
    for _ in range(n):
        vals = ", ".join(
            _sql_lit(vfn(c, skeleton[c].get("type"), skeleton[c].get("samples"), rng))
            for c in cols
        )
        lines.append(f"INSERT INTO `{table}` ({collist}) VALUES ({vals});")
    return "\n".join(lines) + "\n"


def _sql_type(t: Optional[str]) -> str:
    return {
        "int": "INT", "num": "INT", "float": "DOUBLE",
        "bool": "BOOLEAN", "null": "TEXT", "none": "TEXT",
    }.get((t or "str").lower(), "VARCHAR(255)")


def _sql_lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    # 折叠值内换行 → 空格：保证单行 INSERT，否则会撑成多物理行，把逐行的
    # _partition_sql 打爆（读不到闭合 `)` → 0 元组 → 0 记录的误报，见 sch_00001）。
    s = str(v).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return "'" + s.replace("'", "''") + "'"


def _safe_ident(name: str) -> str:
    out = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(name))
    return out or "t"


# ── 杂项 ────────────────────────────────────────────────────────────────────────

def _seed_of(s: str) -> int:
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)

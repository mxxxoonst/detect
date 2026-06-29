"""LLM 渲染：把紧凑 IR skeleton 描述给 LLM，要求产出**结构精确匹配**的 format 文档
（realistic values），带结构校验闸门——反解析路径集不符则重试一次、再不符回退模板。

为何需要闸门：LLM 爱加/改/删字段、"自作主张修正"，会**静默污染干净锚点**。校验闸门把
结构保真这件事变成硬约束（模板渲染天然不需要）。

Client 可插拔（协议 ``LLMClient.complete(prompt)->str``）：
  - FakeLLMClient    : 离线确定性桩。仅解析 prompt 里的 schema 规格 → 调模板渲染器
                       + 「拟真」值 provider。让本地测试/无 API 时跑通整条管线与对比，
                       并直观体现 LLM 路线「值更像真的」（结构仍走模板保证精确）。
  - OpenAICompatClient: env 配置的 OpenAI 兼容端点（vLLM/Qwen/DeepSeek/本地 LLM 皆可），
                       openai 惰性 import（非声明依赖，仅远程配齐时启用）。
"""

import json
import os
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from src.synth.render import _build_tree, _gen_value, surface_format
from src.utils.logger import get_logger

log = get_logger(__name__)

# prompt 里 schema 规格行：``  path : type``
_SPEC_LINE = re.compile(r"^\s+(\S+)\s*:\s*(\w+)\s*$")
_FENCE = re.compile(r"^\s*```[\w-]*\s*\n?|\n?\s*```\s*$")

# 校验回调：(text, unit) -> 含 "ok"/"missing"/"extra" 的结果 dict
Validator = Callable[[str, Dict[str, Any]], Dict[str, Any]]


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str: ...


# ── prompt 构造 ─────────────────────────────────────────────────────────────────

def build_prompt(unit: Dict[str, Any], n_records: int = 3) -> str:
    """把 unit 的（表面 format）schema 描述成「人/机皆可读」的生成指令。

    刻意把 format/table/records/fields 写成规整块——既是给真实 LLM 的清晰约束，
    也是 FakeLLMClient 离线复原 schema 的解析锚点。
    """
    sf = surface_format(unit.get("format", "json"))
    skeleton = unit.get("skeleton") or {}
    spec = "\n".join(f"  {path} : {meta.get('type', 'str')}"
                     for path, meta in skeleton.items())
    table = unit.get("partition_id") or "t"
    table_line = f"table: {table}\n" if sf == "sql" else ""
    return (
        f"You are a data generator. Produce exactly {n_records} records as a single "
        f"valid {sf.upper()} document.\n"
        "Match this schema EXACTLY: same field paths, same types, no extra or missing "
        "fields. Dotted paths denote object nesting; `[]` denotes a list. Use realistic, "
        "coherent values. Output ONLY the raw document — no markdown fences, no prose.\n\n"
        f"format: {sf}\n"
        f"{table_line}"
        f"records: {n_records}\n"
        "fields:\n"
        f"{spec}\n"
    )


def strip_fences(text: str) -> str:
    """剥掉 LLM 常见的 ```lang ... ``` 围栏，留裸文档。"""
    t = text.strip()
    if t.startswith("```"):
        t = _FENCE.sub("", t)
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


# ── 渲染主流程（带校验闸门）──────────────────────────────────────────────────────

def render_llm(
    unit: Dict[str, Any],
    client: LLMClient,
    n_records: int = 3,
    validator: Optional[Validator] = None,
) -> Tuple[str, Dict[str, Any]]:
    """调 LLM 渲染，可选反解析校验：不达标重试一次、再不达标回退模板渲染。

    Returns:
        (text, meta)；meta 含 attempts / used_fallback / surface_format。
    """
    from src.synth.render import render_unit            # 延迟 import，避免循环

    prompt = build_prompt(unit, n_records)
    sf = surface_format(unit.get("format", "json"))
    meta: Dict[str, Any] = {"attempts": 0, "used_fallback": False, "surface_format": sf}

    text = strip_fences(client.complete(prompt))
    meta["attempts"] = 1
    if validator is None:
        return text, meta

    res = validator(text, unit)
    if res.get("ok"):
        return text, meta

    # 重试一次：把缺/多字段反馈给模型纠偏
    fix = (f"{prompt}\nYour previous output was structurally wrong. "
           f"Missing fields: {res.get('missing')}. Extra fields: {res.get('extra')}. "
           "Fix it: include every listed path exactly once, drop extras.")
    text2 = strip_fences(client.complete(fix))
    meta["attempts"] = 2
    if validator(text2, unit).get("ok"):
        return text2, meta

    log.debug("render_llm %s: LLM 两次结构不达标，回退模板渲染", unit.get("id"))
    meta["used_fallback"] = True
    return render_unit(unit, n_records=n_records), meta


# ── 离线确定性桩：解析 prompt → 模板渲染 + 拟真值 ────────────────────────────────

class FakeLLMClient:
    """无 API 时的离线桩：仅靠 prompt 复原 schema，走模板渲染器 + 拟真值 provider。

    结构由模板保证精确（故校验必过），值比随机 token「像真的」，用于本地跑通对比管线、
    直观展示两条路线的值差异。真实效果对比仍需在远程换上真实 client。
    """

    def complete(self, prompt: str) -> str:
        from src.synth.render import _render_json, _render_jsonl, _render_tabular, \
            _render_sql, _seed_of

        fmt, table, n, skeleton = _parse_prompt(prompt)
        if not skeleton:
            return ""
        seed = _seed_of(prompt[:64])
        if fmt in ("csv", "tsv"):
            return _render_tabular(skeleton, max(n, 2), fmt, seed, _realistic_value)
        if fmt == "sql":
            return _render_sql({"partition_id": table}, skeleton, n, seed, _realistic_value)
        if fmt == "jsonl":
            return _render_jsonl(skeleton, n, seed, _realistic_value)
        return _render_json(skeleton, n, seed, _realistic_value)


def _parse_prompt(prompt: str) -> Tuple[str, str, int, Dict[str, Dict]]:
    """从 build_prompt 的规格块复原 (fmt, table, n, skeleton)。"""
    fmt, table, n = "json", "t", 3
    skeleton: Dict[str, Dict] = {}
    in_fields = False
    for line in prompt.splitlines():
        s = line.strip()
        if s.startswith("format:"):
            fmt = s.split(":", 1)[1].strip()
        elif s.startswith("table:"):
            table = s.split(":", 1)[1].strip()
        elif s.startswith("records:"):
            try:
                n = int(s.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif s == "fields:":
            in_fields = True
        elif in_fields:
            m = _SPEC_LINE.match(line)
            if m:
                skeleton[m.group(1)] = {"type": m.group(2), "samples": []}
    return fmt, table, n, skeleton


# 拟真值池：按字段名命中常见 PII/业务字段，未命中回落随机词
_POOL: Dict[str, List[str]] = {
    "name": ["Alice Chen", "Bob Li", "Carol Wang", "David Zhao"],
    "first_name": ["Alice", "Bob", "Carol", "David"],
    "last_name": ["Chen", "Li", "Wang", "Zhao"],
    "email": ["alice@example.com", "bob@test.org", "carol@mail.cn", "d.zhao@corp.com"],
    "phone": ["13800138000", "13912345678", "15011112222"],
    "mobile": ["13800138000", "13912345678"],
    "city": ["Beijing", "Shanghai", "Hangzhou", "Shenzhen"],
    "country": ["China", "USA", "Japan", "Germany"],
    "address": ["No.1 Main St", "88 Park Ave", "12 Lake Rd"],
    "gender": ["M", "F"],
    "status": ["active", "inactive", "pending"],
    "company": ["Acme Inc", "Globex", "Initech"],
    "title": ["Engineer", "Manager", "Analyst"],
    "url": ["https://example.com/a", "https://test.org/b"],
    "ip": ["192.168.1.10", "10.0.0.5", "172.16.0.1"],
    "date": ["2024-01-15", "2023-11-02", "2025-06-20"],
    "created_at": ["2024-01-15 09:30:00", "2023-11-02 14:20:00"],
}
_WORDS = ["lorem", "ipsum", "dolor", "amet", "data", "node", "alpha", "beta"]


def _realistic_value(field_name: str, t: Optional[str], samples: Optional[list],
                     rng: random.Random) -> Any:
    """拟真值：先按字段名命中池，否则按类型造像样的值。"""
    t = (t or "str").lower()
    key = field_name.lower()
    if t in ("str", "null", "none"):
        for pat, pool in _POOL.items():
            if pat in key:
                return rng.choice(pool)
        if t in ("null", "none"):
            return None
        return rng.choice(_WORDS) + str(rng.randint(1, 99))
    if t in ("int", "num"):
        if "age" in key:
            return rng.randint(18, 75)
        if "year" in key:
            return rng.randint(1990, 2025)
        if "id" in key:
            return rng.randint(1000, 999999)
        return rng.randint(0, 9999)
    if t == "float":
        if "price" in key or "amount" in key:
            return round(rng.uniform(1, 9999), 2)
        return round(rng.uniform(0, 100), 2)
    if t == "bool":
        return rng.choice([True, False])
    return rng.choice(_WORDS)


# ── 真实端点适配器（env 配置，惰性 import openai）────────────────────────────────

class OpenAICompatClient:
    """OpenAI 兼容端点。env: SYNTH_LLM_MODEL / SYNTH_LLM_BASE_URL / SYNTH_LLM_API_KEY。"""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, temperature: float = 0.0,
                 max_tokens: int = 8192):   # 输出上限；服务端 max_model_len=131072 留足余量
        self.model = "qwen-72b-gptq"
                #(model or os.environ.get("SYNTH_LLM_MODEL", "gpt-4o-mini"))
        self.base_url = "http://172.17.66.200:19520/v1"
                #base_url or os.environ.get("SYNTH_LLM_BASE_URL")
        self.api_key = "EMPTY"
        #api_key or os.environ.get("SYNTH_LLM_API_KEY", "EMPTY")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = None

    def complete(self, prompt: str) -> str:
        if self._client is None:
            from openai import OpenAI                    # 惰性，非声明依赖
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""


# ════════════════════════════════════════════════════════════════════════════
# 填值模式：模板定结构（arity 由代码保证），LLM 只产「值矩阵」
# ════════════════════════════════════════════════════════════════════════════
# 根治宽/SQL 漂移：LLM 不再写 format 结构，只给值；模板遍历全部字段逐格出值，缺字段
# 由 _default_value 兜底 → 列/值数恒等于字段数，掉值/漂移机制上消失（见 sch_00001：
# 列清单 66 但 VALUES 仅 64 的偏差，在此被吸收）。
# 内网环境：samples 原值可作示例入 prompt（去 64 字符截断尾标 …），输出可含真实形态值。

_EXAMPLE_BUDGET = 2400      # 样本示例总字符预算（控 prompt 长度，利于模型理解宽表）
_MAX_EXAMPLE_LEN = 48       # 单字段示例上限（再长无助格式判别）
_OUT_CHAR_BUDGET = 16000    # 估算输出字符预算（配 max_tokens=8192，约 ≤177 列仍给满 3 行；更宽则自适应减行防 JSON 截断）


def _clean_example(s: Any) -> Optional[str]:
    """样本 → prompt 示例：去截断尾标 `…`、跳过 None/NULL/全脱敏值。"""
    if s is None:
        return None
    if not isinstance(s, str):
        return str(s)
    t = s.rstrip("…").strip()
    if not t or t.upper() == "NULL":
        return None
    if t.count("*") >= 0.6 * len(t):          # 历史 masked 值对“真实示例”无意义
        return None
    return t


def _field_example(meta: Dict[str, Any], maxlen: int) -> Optional[str]:
    for s in (meta.get("samples") or []):
        c = _clean_example(s)
        if c:
            return c[:maxlen]
    return None


def _adaptive_llm_rows(ncol: int, cap: int) -> int:
    """按列数自适应让 LLM 产几行：列越多行越少，防输出 JSON 触 max_tokens 被截断。"""
    return max(1, min(cap, _OUT_CHAR_BUDGET // max(ncol * 30, 1)))


def build_value_prompt(unit: Dict[str, Any], n_rows: int = 3,
                       example_budget: int = _EXAMPLE_BUDGET) -> str:
    """要 LLM 只产「值矩阵」（JSON array of objects，键=字段名），每字段附 1 个 e.g. 示例。

    示例取自 skeleton.samples（去截断尾标）；示例长度按列数自适应，列过多则不附示例
    （控 prompt token，利于模型理解）。
    """
    skeleton = unit.get("skeleton") or {}
    fields = list(skeleton.keys())
    ncol = max(len(fields), 1)
    per = min(_MAX_EXAMPLE_LEN, example_budget // ncol)
    show = per >= 6
    lines = []
    for f in fields:
        meta = skeleton[f]
        ex = _field_example(meta, per) if show else None
        lines.append(f"  {f} : {meta.get('type', 'str')}"
                     + (f"   e.g. {ex}" if ex else ""))
    spec = "\n".join(lines)
    omit = "" if show else "  (字段过多，示例略)\n"
    return (
        f"You are a data value generator. Output ONLY a JSON array of exactly {n_rows} "
        "objects — no markdown fences, no prose, no SQL.\n"
        f"Every object MUST contain ALL {len(fields)} keys listed below (use the field "
        "names verbatim as JSON keys). Generate realistic, coherent values; where an "
        "`e.g.` example is shown, match its format/units/length but produce NEW values. "
        "Keep each row internally consistent (e.g. country code matches country name).\n\n"
        "fields:\n"
        f"{spec}\n"
        f"{omit}"
        f'Return exactly: [{{ "<field>": <value>, ... }} repeated {n_rows} times]'
    )


def _loads_lenient(s: str) -> Any:
    """``strict=False`` 容许字符串内裸控制符（TAB/换行/\\x01——来自源数据二进制/HTML/
    自由文本列），否则 LLM 原样带出会被严格解析器拒为 'Invalid control character'。"""
    return json.loads(s, strict=False)


def _first_balanced_array(text: str) -> Optional[str]:
    """引号/转义感知地截取**首个配平** ``[...]``，绕开夹带散文 / 重复'corrected'数组
    （旧贪婪正则 `\\[.*\\]` 会吞到最后一个 `]`）。无闭合（截断）返回 None。"""
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            esc = (c == "\\") and not esc
            if c == '"' and not esc:
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _scan_objects(text: str) -> List[str]:
    """引号/转义感知地切出所有顶层配平 ``{...}`` 子串。整体解析失败时逐对象兜底：
    截断只丢未闭合的尾对象、单对象括号/分隔符错位只丢该对象，其余行的值照收。"""
    objs: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = esc = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                esc = (c == "\\") and not esc
                if c == '"' and not esc:
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    objs.append(text[i:j + 1])
                    i = j + 1
                    break
            j += 1
        else:                       # 扫到结尾仍未闭合（截断的尾对象）→ 收工
            break
    return objs


def _parse_value_matrix(raw: str, fields: List[str]) -> Tuple[Dict[str, list], int]:
    """LLM 文本 → ({field: [值,...]}, 行数)。容错三层 + 字段名 strip 匹配。

    解析:① 整体容错解析(strict=False);② 失败→截首个配平数组;③ 仍失败→逐对象兜底
    (截断/单对象坏只丢该行)。匹配:精确键优先,回退 ``strip()`` 容忍定宽 padding 字段名
    (源 CSV 表头如 ``'ACC_AUTH        '``，LLM 回显时会 trim)。
    """
    text = strip_fences(raw)
    data: Any = None
    try:
        data = _loads_lenient(text)
    except Exception:
        arr = _first_balanced_array(text)
        if arr:
            try:
                data = _loads_lenient(arr)
            except Exception:
                data = None
    if isinstance(data, dict):
        data = [data]
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    if not rows:                                    # 逐对象兜底
        for chunk in _scan_objects(text):
            try:
                o = _loads_lenient(chunk)
            except Exception:
                continue
            if isinstance(o, dict):
                rows.append(o)

    # 字段名归一化：精确优先，strip 后回退（容忍源表头 padding 与 LLM trim 的差异）
    fset = set(fields)
    canon = {f.strip(): f for f in fields}          # 'ACC_AUTH   '→ stripped 键映回规范名
    table: Dict[str, list] = defaultdict(list)
    for r in rows:
        for k, v in r.items():
            if v is None:
                continue
            cf = k if k in fset else (canon.get(k.strip()) if isinstance(k, str) else None)
            if cf is not None:
                table[cf].append(v)
    return dict(table), len(rows)


def make_fill_value_fn(table: Dict[str, list]) -> Callable:
    """值来源 value_fn：从 LLM 值表按字段取值（per-field 游标循环）；缺字段回落模板默认。"""
    from src.synth.render import _default_value
    cursor: Dict[str, int] = defaultdict(int)

    def fn(field_name, typ, samples, rng):
        vals = table.get(field_name)
        if not vals:
            return _default_value(field_name, typ, samples, rng)   # 模板兜底，结构不缺
        v = vals[cursor[field_name] % len(vals)]
        cursor[field_name] += 1
        return v
    return fn


def render_llm_fill(unit: Dict[str, Any], client: LLMClient, out_rows: int = 3,
                    fail_dump_dir: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """填值模式渲染：结构走模板（arity 恒等于字段数），值取自 LLM 值矩阵。

    无需结构校验闸门/回退——掉值/漂移由模板逐字段兜底吸收。meta 记录填充率。
    一个值都没解析出时把 LLM 原文落 ``fail_dump_dir`` 便于排查。
    """
    from src.synth.render import render_unit

    fields = list((unit.get("skeleton") or {}).keys())
    llm_rows = _adaptive_llm_rows(len(fields), out_rows)
    prompt = build_value_prompt(unit, llm_rows)
    raw = client.complete(prompt)
    table, n_rows = _parse_value_matrix(raw, fields)

    if not table and fail_dump_dir:
        Path(fail_dump_dir).mkdir(parents=True, exist_ok=True)
        (Path(fail_dump_dir) / f"{unit.get('id', 'unit')}.txt").write_text(
            raw, encoding="utf-8", errors="replace")

    doc = render_unit(unit, n_records=out_rows, value_fn=make_fill_value_fn(table))
    missing = [f for f in fields if f not in table]
    meta = {"mode": "fill", "n_fields": len(fields), "filled_fields": len(table),
            "n_missing": len(missing), "missing_fields": missing[:20],
            "llm_rows": n_rows, "llm_rows_requested": llm_rows}
    if missing:
        log.debug("render_llm_fill %s: %d/%d 字段由模板兜底", unit.get("id"),
                  len(missing), len(fields))
    return doc, meta


class FakeFillLLMClient:
    """填值模式离线桩：解析 value-prompt 的字段 → 返回 JSON 值矩阵（拟真值）。

    ``drop_last>0`` 时故意少给末尾 N 个字段，模拟 LLM 掉值，验证模板兜底补满结构。
    """

    def __init__(self, drop_last: int = 0):
        self.drop_last = drop_last

    def complete(self, prompt: str) -> str:
        from src.synth.render import _seed_of
        fields, n = _parse_value_prompt(prompt)
        keep = fields[:len(fields) - self.drop_last] if self.drop_last else fields
        rng = random.Random(_seed_of(prompt[:80]))
        rows = [{f: _realistic_value(f, t, None, rng) for f, t in keep} for _ in range(n)]
        return json.dumps(rows, ensure_ascii=False)


def _parse_value_prompt(prompt: str) -> Tuple[List[Tuple[str, str]], int]:
    """从 build_value_prompt 复原 ([(field, type)...], 行数)。"""
    n = 3
    m = re.search(r"array of exactly (\d+)", prompt)
    if m:
        n = int(m.group(1))
    fields: List[Tuple[str, str]] = []
    in_fields = False
    for line in prompt.splitlines():
        s = line.strip()
        if s == "fields:":
            in_fields = True
            continue
        if in_fields:
            mm = re.match(r"^\s+(\S+)\s*:\s*(\w+)", line)
            if mm:
                fields.append((mm.group(1), mm.group(2)))
            elif s.startswith("Return"):
                break
    return fields, n

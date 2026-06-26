"""信息三: value 画像 — 统计摘要, 默认绝不存原始值.

对每个字段路径, 收集:
  - 长度分布: min/max/mean/median/std
  - 字符宏类分布 (char_dist): 基于 unicodedata.category 的 7 个互斥穷尽宏桶
    number / letter / mark / punct / symbol / space / other —— 之和恒为 1, 无漏网。
  - 脚本直方图 (scripts): 仅对 letter 部分统计, 轻量覆盖 8 个脚本 + Other 残差
    (Han/Hiragana/Katakana/Hangul/Latin/Cyrillic/Arabic/Greek)。
  - 模式模板 (pattern): 如 "D{11}"。与 char_dist 同源于一个分类函数, 字母再按
    粗脚本细分 (Han→C, Latin→L, 其它脚本→X), token 集有界、跨语言可比对。
  - 样本值 (samples): **默认关闭**, 守"禁止持久化 PII 原值"红线。仅当
    sample_mode 显式为 "raw"/"masked" 时, 按 pattern 去重最多取 5 个 (优先覆盖
    不同 pattern 类别); "masked" 保留分隔符与长度、内容字符打码, 不落原始字符。

⚠ 完备性 (MECE) 与语言精度分层解决: 完备性交给 Unicode 划分 + other 残差 (与语言
无关、可校验); 语言/脚本精度交给开放词表式 scripts 直方图 (只枚举关心的 8 个脚本,
其余诚实归 Other)。两条轴共用同一个 _macro_class / _script_of, 不存在两套分类器漂移。
"""

import bisect
import statistics
import unicodedata
from collections import Counter
from typing import Any, Dict, List, Optional

# 每字段路径保留的样本上限 (优先覆盖不同 pattern 类别)
SAMPLE_MAX = 5

# 单个样本值最大字符数: 样本是「值形态证据」非全量载荷, 超长值 (嵌入 JSON/base64/
# 长 free-text) 截断头部, 既控 IR 体积又收窄 PII 暴露面。email/phone/UUID/日期均 <64 不受影响。
SAMPLE_VALUE_MAXLEN = 64


# ──── 字符宏类: Unicode general category 首字母 → 宏桶 (互斥且穷尽) ────
_CAT_MACRO = {
    "L": "letter",   # Lu Ll Lt Lm Lo  —— 所有文字 (拉丁/汉字/假名/谚文…)
    "N": "number",   # Nd Nl No         —— 含全角数字/罗马数字/分数
    "M": "mark",     # Mn Mc Me         —— 组合附加符 (分解形 é 的重音)
    "P": "punct",    # Pc Pd Ps Pe Pi Pf Po
    "S": "symbol",   # Sm Sc Sk So      —— 含 Emoji / 货币符
    "Z": "space",    # Zs Zl Zp
    "C": "other",    # Cc Cf Cs Co Cn   —— 控制 / 未分配 (显式残差桶)
}
# char_dist 固定键顺序 (7 桶之和恒为 1, 便于下游校验)
_MACRO_KEYS = ("number", "letter", "mark", "punct", "symbol", "space", "other")

# pattern token: 宏桶 → 单字符 token (letter 不在此表, 走脚本细分见 _letter_token)
_MACRO_TOKEN = {
    "number": "D",
    "mark":   "M",
    "punct":  "P",
    "symbol": "S",
    "space":  " ",
    "other":  "?",
}


# ──── 脚本范围表 (轻量覆盖 8 个脚本, 其余 letter 归 Other) ────
# (start, end_inclusive, script)；只枚举关心的脚本, 其余码点诚实归 Other 而非丢弃。
_SCRIPT_RANGES = sorted([
    (0x0041, 0x005A, "Latin"), (0x0061, 0x007A, "Latin"),
    (0x00C0, 0x024F, "Latin"), (0x1E00, 0x1EFF, "Latin"),
    (0xFF21, 0xFF3A, "Latin"), (0xFF41, 0xFF5A, "Latin"),
    (0x0370, 0x03FF, "Greek"), (0x1F00, 0x1FFF, "Greek"),
    (0x0400, 0x052F, "Cyrillic"),
    (0x0600, 0x06FF, "Arabic"), (0x0750, 0x077F, "Arabic"),
    (0x08A0, 0x08FF, "Arabic"), (0xFB50, 0xFDFF, "Arabic"),
    (0xFE70, 0xFEFF, "Arabic"),
    (0x3040, 0x309F, "Hiragana"),
    (0x30A0, 0x30FF, "Katakana"), (0x31F0, 0x31FF, "Katakana"),
    (0xFF66, 0xFF9D, "Katakana"),
    (0x1100, 0x11FF, "Hangul"), (0x3130, 0x318F, "Hangul"),
    (0xAC00, 0xD7AF, "Hangul"),
    (0x3400, 0x4DBF, "Han"), (0x4E00, 0x9FFF, "Han"),
    (0xF900, 0xFAFF, "Han"), (0x20000, 0x2A6DF, "Han"),
    (0x2A700, 0x2EBEF, "Han"),
])
_SCRIPT_STARTS = [r[0] for r in _SCRIPT_RANGES]


def _macro_class(ch: str) -> str:
    """字符 → 7 宏桶之一 (唯一真源: isspace 优先, 其余按 Unicode category 首字母)。

    isspace 优先保证 \\t/\\n 等被归入 space (它们的 category 是 Cc), 与直觉一致。
    任意码点必落且只落一个桶 → char_dist 之和恒为 1。
    """
    if ch.isspace():
        return "space"
    return _CAT_MACRO.get(unicodedata.category(ch)[0], "other")


def _script_of(ch: str) -> str:
    """letter 字符 → 脚本名 (8 脚本之一或 Other)。bisect 命中区间表。"""
    cp = ord(ch)
    i = bisect.bisect_right(_SCRIPT_STARTS, cp) - 1
    if i >= 0:
        start, end, name = _SCRIPT_RANGES[i]
        if start <= cp <= end:
            return name
    return "Other"


def _letter_token(script: str) -> str:
    """letter 的 pattern token: 保留中文/拉丁的判别, 其余脚本统一 X (token 集有界)。"""
    if script == "Han":
        return "C"
    if script == "Latin":
        return "L"
    return "X"


def profile_value(value: Any) -> Dict:
    """计算单个 value 的画像 (绝不存原值, 即时丢弃输入)。"""
    if value is None:
        return {"type": "null", "len": 0}

    profile: Dict = {"type": type(value).__name__}

    if isinstance(value, (int, float, bool)):
        profile["value_range_hint"] = _numeric_hint(value)
        return profile

    if isinstance(value, str):
        profile.update(_str_profile(value))
        return profile

    # 复合类型: 只记录结构
    if isinstance(value, list):
        profile["len"] = len(value)
        if value:
            profile["elem_type"] = type(value[0]).__name__
    elif isinstance(value, dict):
        profile["keys"] = list(value.keys())[:20]

    return profile


def _str_profile(s: str) -> Dict:
    """字符串画像 —— **单趟**遍历同时产出 char_dist / scripts / pattern。

    入口先 NFC 归一, 把分解形 (e + 组合重音) 折回预组合字符, 计数更稳定。
    """
    s = unicodedata.normalize("NFC", s)
    length = len(s)

    macro: Counter = Counter()
    scripts: Counter = Counter()
    out: List[str] = []          # pattern 的游程压缩输出
    prev_tok: Optional[str] = None
    run = 0

    for ch in s:
        m = _macro_class(ch)
        macro[m] += 1
        if m == "letter":
            sc = _script_of(ch)
            scripts[sc] += 1
            tok = _letter_token(sc)
        else:
            tok = _MACRO_TOKEN[m]
        # 同 token 游程压缩: 'aaa' → 'L{3}'
        if tok == prev_tok:
            run += 1
        else:
            if prev_tok is not None:
                out.append(prev_tok if run == 1 else f"{prev_tok}{{{run}}}")
            prev_tok, run = tok, 1
    if prev_tok is not None:
        out.append(prev_tok if run == 1 else f"{prev_tok}{{{run}}}")

    char_dist = {
        f"{k}_pct": round(macro.get(k, 0) / max(length, 1), 3) for k in _MACRO_KEYS
    }
    result: Dict = {"len": length, "char_dist": char_dist, "pattern": "".join(out)}
    if scripts:
        total = sum(scripts.values())
        result["scripts"] = {
            k: round(v / total, 3) for k, v in scripts.most_common()
        }
    return result


def _make_pattern(s: str) -> str:
    """单字符串 → 字符类模式模板 (薄包装, 复用 _str_profile 的同源 pattern)。"""
    return _str_profile(s)["pattern"]


def _numeric_hint(value: int | float | bool) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        if 1900 <= value <= 2100:
            return "year_like"
        if 1 <= value <= 150:
            return "age_like"
        if value > 100000:
            return "large_int"
        return "int"
    if isinstance(value, float):
        return "float"
    return "unknown"


# ──── 样本保留 (默认关闭; raw / masked 二选一) ────

def _sample_key(p: Dict) -> str:
    """样本去重键: 优先 pattern (覆盖不同结构类别), 标量回退到 type。"""
    return p.get("pattern") if "pattern" in p else p.get("type", "?")


def _mask(value: Any) -> Any:
    """脱敏: 保留分隔符/空白与长度, 内容字符 (字母/数字/符号/标记) → '*'。

    'user@x.com' → '****@*.***'；'2024-01-02' → '****-**-**'；'张三' → '**'。
    非字符串标量保留位数结构、字母数字打码 ('<bool>' 单独标注)。
    """
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return "".join("*" if c.isalnum() else c for c in repr(value))
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    s = unicodedata.normalize("NFC", value)
    return "".join(
        ch if _macro_class(ch) in ("punct", "space") else "*" for ch in s
    )


def _truncate(value: Any) -> Any:
    """超长字符串样本截断头部 + `…` 标记; 非字符串原样返回。

    样本只作值形态证据, 头部已足以表征 pattern; 截断同时控体积、收窄 PII 暴露面。
    """
    if isinstance(value, str) and len(value) > SAMPLE_VALUE_MAXLEN:
        return value[:SAMPLE_VALUE_MAXLEN] + "…"
    return value


def _select_samples(raw_values: List[Any], profiles: List[Dict], mode: str) -> List[Any]:
    """按 pattern 去重挑样本: 每个不同 pattern 留首个代表, 最多 SAMPLE_MAX 个。

    优先覆盖不同 pattern 类别 (类别 > 5 时任取 5 种); null 不取样。
    mode="raw" 落原值, "masked" 落脱敏值; 两者均经 _truncate 限长 (raw/masked 同样会
    产生超长串——masked 的 `*` 串 1:1 保长亦需截断)。
    """
    picked: Dict[str, Any] = {}
    for v, p in zip(raw_values, profiles):
        if v is None:
            continue
        k = _sample_key(p)
        if k in picked:
            continue
        picked[k] = v
        if len(picked) >= SAMPLE_MAX:
            break
    if mode == "masked":
        return [_truncate(_mask(v)) for v in picked.values()]
    return [_truncate(v) for v in picked.values()]


def aggregate_profiles(
    profiles: List[Dict],
    raw_values: Optional[List[Any]] = None,
    sample_mode: str = "off",
) -> Dict:
    """聚合多条 records 的同一字段画像。

    Args:
        profiles:    profile_value() 逐值输出。
        raw_values:  与 profiles 等长的原始值 (仅 sample_mode!="off" 时需要)。
        sample_mode: "off"(默认, 不留样) / "raw"(留原值) / "masked"(留脱敏值)。
    """
    lengths = [p.get("len", 0) for p in profiles if "len" in p]
    patterns: Counter = Counter()
    for p in profiles:
        if "pattern" in p:
            patterns[p["pattern"]] += 1

    result: Dict = {"sample_count": len(profiles)}
    if lengths:
        result["len_dist"] = {
            "min":    min(lengths),
            "max":    max(lengths),
            "mean":   round(sum(lengths) / len(lengths), 1),
            "median": round(statistics.median(lengths), 1),
            "std":    round(statistics.pstdev(lengths), 1) if len(lengths) > 1 else 0.0,
        }
    if patterns:
        result["top_patterns"] = patterns.most_common(10)
        result["unique_patterns"] = len(patterns)

    # char_dist 宏桶均值 (固定键, key-agnostic 求平均)
    char_dists = [p["char_dist"] for p in profiles if "char_dist" in p]
    if char_dists:
        result["avg_char_dist"] = {
            key: round(sum(d[key] for d in char_dists) / len(char_dists), 3)
            for key in char_dists[0]
        }

    # scripts 直方图均值 (开放词表, 取并集键, 缺失计 0)
    script_dists = [p["scripts"] for p in profiles if "scripts" in p]
    if script_dists:
        keys = set().union(*script_dists)
        result["avg_scripts"] = {
            k: round(sum(d.get(k, 0) for d in script_dists) / len(script_dists), 3)
            for k in sorted(keys)
        }

    # 样本值: 默认关闭, 守 PII 红线
    if sample_mode != "off" and raw_values is not None:
        samples = _select_samples(raw_values, profiles, sample_mode)
        if samples:
            result["samples"] = samples

    return result

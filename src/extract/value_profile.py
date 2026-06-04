"""信息三: value 画像 — 统计摘要, 绝不存原始值.

对每个字段路径, 收集:
  - 长度分布: min/max/mean/median/std
  - 字符类别分布: digit%, alpha%, CJK%, punct%, space%
  - 模式模板: 如 "DDD-DDDD-DDDD" (D=digit, L=letter, C=CJK, S=symbol)
  - 空值率 / 唯一值率
"""

import re
from collections import Counter
from typing import Any, Dict, List


# ──── 字符分类 ────
DIGIT_RE = re.compile(r"\d")
ALPHA_RE = re.compile(r"[a-zA-Z]")
CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
PUNCT_RE = re.compile(r"[，。！？、；：""'《》（）—…,.!?;:\"()_/@#$%^&*+=<>{}|`-]")
SPACE_RE = re.compile(r"\s")


def profile_value(value: Any) -> Dict:
    """计算单个 value 的画像 (不存原值)."""
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
    """字符串画像."""
    length = len(s)
    digits = len(DIGIT_RE.findall(s))
    alphas = len(ALPHA_RE.findall(s))
    cjks = len(CJK_RE.findall(s))
    puncts = len(PUNCT_RE.findall(s))
    spaces = len(SPACE_RE.findall(s))

    return {
        "len": length,
        "char_dist": {
            "digit_pct": round(digits / max(length, 1), 3),
            "alpha_pct": round(alphas / max(length, 1), 3),
            "cjk_pct": round(cjks / max(length, 1), 3),
            "punct_pct": round(puncts / max(length, 1), 3),
            "space_pct": round(spaces / max(length, 1), 3),
        },
        "pattern": _make_pattern(s),
    }


def _make_pattern(s: str) -> str:
    """将字符串转换为字符类模式模板.

    D=digit, L=letter(alpha), C=CJK, S=symbol/punct, ' '=space
    连续相同类型压缩: '13812345678' → 'D{11}'
    """
    result = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isdigit():
            cls = "D"
        elif ch.isalpha() and ch.isascii():
            cls = "L"
        elif "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            cls = "C"
        elif ch.isspace():
            cls = " "
        else:
            cls = "S"
        count = 1
        while i + count < len(s):
            ch2 = s[i + count]
            cls2 = _char_class(ch2)
            if cls2 != cls:
                break
            count += 1
        if count == 1:
            result.append(cls)
        else:
            result.append(f"{cls}{{{count}}}")
        i += count
    return "".join(result)


def _char_class(ch: str) -> str:
    if ch.isdigit():
        return "D"
    if ch.isalpha() and ch.isascii():
        return "L"
    if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
        return "C"
    if ch.isspace():
        return " "
    return "S"


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


def aggregate_profiles(profiles: List[Dict]) -> Dict:
    """聚合多条 records 的同一字段画像."""
    lengths = [p.get("len", 0) for p in profiles if "len" in p]
    patterns = Counter()
    for p in profiles:
        if "pattern" in p:
            patterns[p["pattern"]] += 1

    result: Dict = {"sample_count": len(profiles)}
    if lengths:
        result["len_dist"] = {
            "min": min(lengths),
            "max": max(lengths),
            "mean": round(sum(lengths) / len(lengths), 1),
        }
    if patterns:
        result["top_patterns"] = patterns.most_common(10)
        result["unique_patterns"] = len(patterns)

    # 汇总字符分布均值
    char_dists = [p["char_dist"] for p in profiles if "char_dist" in p]
    if char_dists:
        avg_dist = {}
        for key in char_dists[0]:
            avg_dist[key] = round(
                sum(d[key] for d in char_dists) / len(char_dists), 3
            )
        result["avg_char_dist"] = avg_dist

    return result

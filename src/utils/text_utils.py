"""文本处理工具: 括号平衡、非空行提取、句式检测等."""

import json
import re
from statistics import stdev
from typing import List, Dict


def first_n_nonempty_lines(text: str, n: int) -> List[str]:
    """提取前 n 条非空行 (strip 后非空)."""
    result = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            result.append(stripped)
        if len(result) >= n:
            break
    return result


def balanced_brackets(text: str) -> bool:
    """检查花括号 {} 和方括号 [] 是否配对."""
    stack = []
    pairs = {"{": "}", "[": "]"}
    for ch in text:
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in ("}", "]"):
            if not stack or stack.pop() != ch:
                return False
    return len(stack) == 0


def avg_line_len(lines: List[str]) -> float:
    """平均行长."""
    if not lines:
        return 0.0
    return sum(len(line) for line in lines) / len(lines)


def has_sentence_punctuation(lines: List[str]) -> bool:
    """检测是否含自然语言标点 (句号、逗号、问号等)."""
    punct = re.compile(r"[。，？！、；：,.!?;:]")
    hits = sum(1 for line in lines if punct.search(line))
    return hits / max(len(lines), 1) > 0.3


def no_strong_structure_signal(scores: Dict[str, float]) -> bool:
    """检查投票分数中是否有强结构信号 (>0.5)."""
    return all(v < 0.5 for v in scores.values())


def column_stability(lines: List[str], sep: str) -> tuple:
    """返回 (第一行列数, 列数标准差, 列数列表)."""
    col_counts = [line.count(sep) + 1 for line in lines]
    if not col_counts:
        return 0, 0.0, []
    avg = sum(col_counts) / len(col_counts)
    sd = stdev(col_counts) if len(col_counts) > 1 else 0.0
    return col_counts[0], sd, col_counts


def first_line_looks_like_header(line: str) -> bool:
    """试探首行是否更像表头 (含常见非数值字符、字母下划线为主)."""
    # 简单启发式: 首行字段以字母/下划线/中文为主, 不是纯数字
    if not line:
        return False
    header_chars = re.findall(r"[\w一-鿿]", line)
    return len(header_chars) / max(len(line), 1) > 0.5


def try_json_loads(line: str):
    """尝试 JSON parse, 成功返回 parsed, 失败返回 None."""
    try:
        return json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None


# ──── 阶段1/2 用 ────

def regex_search(pattern: str, text: str, flags: int = 0) -> bool:
    """返回 pattern 是否在 text 中匹配."""
    return bool(re.search(pattern, text, flags))

"""文本处理工具: 括号平衡、非空行提取、句式检测等."""

import json
import re
from collections import Counter
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


def has_sentence_punctuation(lines: List[str]) -> bool:
    """检测是否含自然语言标点 (句号、逗号、问号等)."""
    punct = re.compile(r"[。，？！、；：,.!?;:]")
    hits = sum(1 for line in lines if punct.search(line))
    return hits / max(len(lines), 1) > 0.3


def no_strong_structure_signal(scores: Dict[str, float]) -> bool:
    """检查投票分数中是否有强结构信号 (>0.5)."""
    return all(v < 0.5 for v in scores.values())


def column_profile(lines: List[str], sep: str) -> tuple:
    """返回 (众数列数, 命中众数的行占比, 行数)。

    用"众数列数 + 命中比例"(而非首行列数 + 标准差), 对少数因 value 内嵌分隔符
    而漂移的行更鲁棒 (无表头 CSV 也适用)。
    """
    col_counts = [line.count(sep) + 1 for line in lines]
    if not col_counts:
        return 0, 0.0, 0
    modal_cols, modal_n = Counter(col_counts).most_common(1)[0]
    return modal_cols, modal_n / len(col_counts), len(col_counts)


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

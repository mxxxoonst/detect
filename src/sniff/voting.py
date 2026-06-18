"""多候选加权投票: 解开 txt 的真实格式。"""

import re
from typing import Dict, List

from src.constants import (
    SQL_KEYWORD_PATTERN, SQL_STMT_START_PATTERN,
    LOG_PATTERN, LOG_TS_PREFIX_PATTERN, LOG_LEVEL_PATTERN,
)
from src.utils.text_utils import (
    balanced_brackets,
    column_profile,
    first_line_looks_like_header,
    has_sentence_punctuation,
    no_strong_structure_signal,
    try_json_loads,
)


def vote_format(lines: List[str], full_text: str) -> Dict[str, float]:
    """对文本行做加权投票, 返回 {format: score}.

    Args:
        lines: 前 N 条非空行
        full_text: 解码后的完整头部文本
    """
    scores: Dict[str, float] = {
        "json": 0.0, "jsonl": 0.0, "csv": 0.0, "tsv": 0.0,
        "sql": 0.0, "log": 0.0, "free_text": 0.0,
    }
    n = max(len(lines), 1)

    # ── JSON: 全文 strip 后以 { 或 [ 开头并能闭合 ──
    stripped = full_text.strip()
    if stripped and stripped[0] in "{[":
        scores["json"] += 0.6
        if balanced_brackets(full_text):
            scores["json"] += 0.3

    # ── JSONL: 每行独立能 parse 成 JSON 对象 ──
    parsable = sum(1 for line in lines if try_json_loads(line) is not None)
    if parsable / n > 0.8:
        scores["jsonl"] += 0.9

    # ── CSV / TSV: 分隔符列数跨行稳定 ──
    _vote_delimited(lines, scores, n)

    # ── SQL: txt 里夹 SQL 语句 ──
    # 强信号"行锚定": 关键词须起一行(任意行)且文件含 ; 结尾语句 → 0.95;
    # 区别于 CSV value 里埋的 'insert into' (不在行首 → 不触发强信号)。
    strong_stmt_lines = sum(
        1 for ln in lines if re.match(SQL_STMT_START_PATTERN, ln, re.IGNORECASE)
    )
    semicolon_lines = sum(1 for ln in lines if ln.endswith(";"))
    if strong_stmt_lines >= 1 and semicolon_lines >= 1:
        scores["sql"] += 0.95
    elif strong_stmt_lines >= 1 or re.search(SQL_KEYWORD_PATTERN, full_text, re.IGNORECASE):
        scores["sql"] += 0.7

    # ── 日志行: 时间戳/级别模式 ──
    # 强信号(行首时间戳 + 级别 双命中) → 0.95，压过 CSV/JSON 的 0.9；否则弱信号 → 0.85
    strong_log = sum(
        1 for line in lines
        if re.search(LOG_TS_PREFIX_PATTERN, line)
        and re.search(LOG_LEVEL_PATTERN, line, re.IGNORECASE)
    )
    weak_log = sum(1 for line in lines if re.search(LOG_PATTERN, line, re.IGNORECASE))
    if strong_log / n > 0.6:
        scores["log"] += 0.95
    elif weak_log / n > 0.6:
        scores["log"] += 0.85

    # ── 自由文本: 长句、标点密度低、无强结构信号 ──
    avg_len = sum(len(line) for line in lines) / n
    if avg_len > 40 and has_sentence_punctuation(lines) and no_strong_structure_signal(scores):
        scores["free_text"] += 0.5

    return scores


def _vote_delimited(lines: List[str], scores: Dict[str, float], n: int):
    """对 CSV/TSV/管道/冒号分隔符做投票 (众数列稳定性, 容忍少数漂移行)."""
    for sep, fmt in [("\t", "tsv"), (",", "csv"), (";", "csv"), ("|", "csv"), (":", "csv")]:
        modal_cols, modal_frac, _ = column_profile(lines, sep)
        # 众数列数≥2 且多数行命中众数 → 分隔结构 (无表头 CSV、个别 value 内嵌分隔符均不掉票)
        if modal_cols >= 2 and modal_frac >= 0.7:
            scores[fmt] += 0.8
            if first_line_looks_like_header(lines[0]):
                scores[fmt] += 0.1

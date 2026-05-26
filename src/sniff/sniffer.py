"""单文件内容嗅探: 返回 (real_format, encoding, confidence)."""

import re
from typing import Tuple

from src.constants import SNIFF_HEAD_BYTES, SNIFF_LINES, ACCEPT_THRESHOLD, SQLITE_MAGIC
from src.utils.encoding import detect_encoding, safe_decode
from src.utils.file_utils import read_head_bytes, read_first_bytes, is_binary, is_sqlite_magic, extension
from src.utils.text_utils import first_n_nonempty_lines, regex_search
from src.sniff.voting import vote_format


def sniff_file(path: str) -> Tuple[str, str, float]:
    """嗅探单个文件的真实格式。

    Returns:
        (real_format, encoding, confidence)
        real_format: 'json'|'jsonl'|'csv'|'tsv'|'sql'|'sqlite'|
                     'log'|'free_text'|'db_nonsqlite'|'binary_unknown'|'empty'
    """
    ext = extension(path)

    # ── 二进制候选: .db / 无扩展名二进制 ──
    raw16 = read_first_bytes(path, 16)
    if is_sqlite_magic(raw16):
        return ("sqlite", "binary", 1.0)

    # 读 1KB 判断二进制
    raw1k = read_first_bytes(path, 1024)
    if is_binary(raw1k):
        if ext == "db":
            return ("db_nonsqlite", "binary", 0.8)
        return ("binary_unknown", "binary", 0.6)

    # ── 文本类: 探编码 ──
    head = read_head_bytes(path, SNIFF_HEAD_BYTES)
    enc = detect_encoding(head)
    text = safe_decode(head, enc)
    lines = first_n_nonempty_lines(text, SNIFF_LINES)

    if not lines:
        return ("empty", enc, 1.0)

    # ── .sql 扩展名: 优先确认 SQL 文本 ──
    if ext == "sql" and regex_search(
        r"\b(create\s+table|insert\s+into|drop\s+table)\b", text, re.IGNORECASE
    ):
        return ("sql", enc, 0.95)

    # ── 多候选加权投票 ──
    scores = vote_format(lines, text)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    conf = scores[best]

    if conf < ACCEPT_THRESHOLD:
        return ("free_text", enc, conf)

    return (best, enc, conf)

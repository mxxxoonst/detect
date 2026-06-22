"""单文件内容嗅探: 返回 (real_format, encoding, confidence)。

设计取舍: 真实语料中 json/csv/jsonl/sql/tsv/xlsx 等结构化文件 ~99% 后缀与内容一致
(噪声留给阶段1 容错解析暴露, 不在嗅探期纠格式), 故对这些扩展名直接信任;
仅对 .txt / .log / 无扩展名等无强先验的文件做内容投票, 路由到对应解析器。
"""

from typing import Tuple

from src.constants import SNIFF_HEAD_BYTES, SNIFF_LINES, ACCEPT_THRESHOLD
from src.utils.encoding import detect_encoding, safe_decode
from src.utils.file_utils import (
    read_head_bytes, read_first_bytes, is_binary, detect_bom, extension,
)
from src.utils.text_utils import first_n_nonempty_lines
from src.sniff.voting import vote_format
from src.utils.logger import get_logger

log = get_logger(__name__)

# 结构化扩展名 → 直接信任的真实格式 (后缀即判据, 不做内容投票)
_TRUSTED_EXT = {
    "json": "json",
    "jsonl": "jsonl",
    "ndjson": "jsonl",
    "csv": "csv",
    "tsv": "tsv",
    "sql": "sql",
    "xlsx": "xlsx",
}


def sniff_file(path: str) -> Tuple[str, str, float]:
    """嗅探单个文件的真实格式。

    0 字节文件由调用方(main 的 parse 阶段 / profile_corpus)在进入前按文件大小跳过,
    这里不再处理 empty。

    Returns:
        (real_format, encoding, confidence)
        real_format: 'json'|'jsonl'|'csv'|'tsv'|'sql'|'xlsx'|
                     'log'|'free_text'|'binary_unknown'
    """
    # ── 结构化扩展名: 直接信任 (xlsx 为二进制, 无需探编码) ──
    fmt = _TRUSTED_EXT.get(extension(path))
    if fmt is not None:
        if fmt == "xlsx":
            return ("xlsx", "binary", 1.0)
        enc = _detect_text_encoding(path)
        log.debug("扩展名信任 %s → %s (enc=%s)", path, fmt, enc)
        return (fmt, enc, 1.0)

    # ── .txt / .log / 无扩展名等: 内容投票路由 ──
    return _sniff_by_content(path)


def _detect_text_encoding(path: str) -> str:
    """优先 BOM, 否则 chardet 探编码 (UTF-16/32 含 \\x00, 须靠 BOM 识别)。"""
    bom_enc = detect_bom(read_first_bytes(path, 16))
    if bom_enc:
        return bom_enc
    return detect_encoding(read_head_bytes(path, SNIFF_HEAD_BYTES))


def _sniff_by_content(path: str) -> Tuple[str, str, float]:
    """无强先验扩展名: 二进制判定 + 加权投票, 路由到 json/csv/sql/log/free_text。"""
    bom_enc = detect_bom(read_first_bytes(path, 16))
    # BOM 文本的 \x00 是编码产物, 非二进制信号 → 有 BOM 时跳过 is_binary
    if not bom_enc and is_binary(read_first_bytes(path, 1024)):
        log.debug("未知二进制: %s", path)
        return ("binary_unknown", "binary", 0.6)

    head = read_head_bytes(path, SNIFF_HEAD_BYTES)
    enc = bom_enc or detect_encoding(head)
    text = safe_decode(head, enc)
    lines = first_n_nonempty_lines(text, SNIFF_LINES)
    # 纯空白文件(无非空行): vote_format 全 0 分 → 落 free_text(低置信), 不再单列 empty
    scores = vote_format(lines, text)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    conf = scores[best]
    log.debug("投票路由 %s: best=%s conf=%.2f enc=%s scores=%s",
              path, best, conf, enc, {k: round(v, 2) for k, v in scores.items() if v > 0})

    if conf < ACCEPT_THRESHOLD:
        log.debug("置信度 %.2f < %.2f, 归为 free_text: %s", conf, ACCEPT_THRESHOLD, path)
        return ("free_text", enc, conf)

    return (best, enc, conf)

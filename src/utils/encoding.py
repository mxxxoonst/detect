"""编码探测与安全解码。"""

import chardet


def detect_encoding(raw: bytes) -> str:
    """探测 bytes 的编码, 返回 encoding name 字符串, 默认 'utf-8'."""
    result = chardet.detect(raw)
    enc = result.get("encoding")
    return enc if enc else "utf-8"


def safe_decode(raw: bytes, encoding: str) -> str:
    """安全解码, 非法字节用 U+FFFD 替换."""
    return raw.decode(encoding, errors="replace")

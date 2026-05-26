"""文件 I/O 工具: 读头、二进制检测、遍历. 全部流式, 不整文件 load."""

import os
import string
from src.constants import SNIFF_HEAD_BYTES, SQLITE_MAGIC, MAX_BINARY_RATIO


def read_head_bytes(path: str, n: int = SNIFF_HEAD_BYTES) -> bytes:
    """读文件头部 n 字节."""
    with open(path, "rb") as f:
        return f.read(n)


def read_first_bytes(path: str, n: int) -> bytes:
    """读文件前 n 字节 (通用)."""
    with open(path, "rb") as f:
        return f.read(n)


def is_binary(raw: bytes) -> bool:
    """检测控制字符/NULL 字节比例 → 二进制文件.

    注意: UTF-8 中文等多字节字符的 high bytes (>=0x80) 是合法文本,
    不计入二进制信号。仅统计控制字符 (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F) 和 DEL(0x7F).
    """
    if not raw:
        return False
    # NULL bytes 是强力二进制信号
    null_count = raw.count(b"\x00")
    if null_count > max(len(raw) / 256, 1):
        return True
    # 控制字符集 (不含 \t=0x09, \n=0x0A, \r=0x0D)
    control_count = sum(
        1 for b in raw
        if b <= 0x08 or b in (0x0B, 0x0C) or (0x0E <= b <= 0x1F) or b == 0x7F
    )
    return (control_count / len(raw)) > MAX_BINARY_RATIO


def is_sqlite_magic(raw16: bytes) -> bool:
    """检查是否为 SQLite 数据库魔数."""
    return raw16[: len(SQLITE_MAGIC)] == SQLITE_MAGIC


def walk_files(root: str):
    """遍历目录下所有文件, yield 绝对路径."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def count_lines(path: str, encoding: str) -> int:
    """流式统计文本文件行数."""
    count = 0
    with open(path, "r", encoding=encoding, errors="replace") as f:
        for _ in f:
            count += 1
    return count


def file_size(path: str) -> int:
    """文件字节数."""
    return os.path.getsize(path)


def extension(path: str) -> str:
    """返回小写扩展名, 不含点, 无扩展名返回空串."""
    ext = os.path.splitext(path)[1].lower()
    return ext.lstrip(".") if ext else ""

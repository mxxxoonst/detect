"""JSONL 流式读写工具: 阶段间落盘 + 断点续跑的载体。

- append_jsonl: 逐对象追加一行并 flush，崩溃时已写行不丢。
- iter_jsonl:   逐行流式读，容忍崩溃残留的截断尾行（跳过并告警）。
"""

import json
from pathlib import Path
from typing import Any, Iterator

from src.utils.logger import get_logger

log = get_logger(__name__)


def append_jsonl(path: str | Path, obj: Any) -> None:
    """追加一个对象为一行 JSON 并立即 flush（崩溃安全的最小单位）。

    ``errors="replace"``：样本值可能含**孤立代理项**（如 JSON 源里裸写 `\\ude08`，
    json.loads 会产出一个无配对的 U+DE08，编码到 UTF-8 时 `surrogates not allowed`
    报错）。用 replace 把它降级为 `?` 而非让整条流水线崩溃退出。
    """
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str))
        f.write("\n")
        f.flush()


def iter_jsonl(path: str | Path) -> Iterator[Any]:
    """逐行流式产出对象；文件不存在时产出空。

    末行若因进程中断而截断（非法 JSON），跳过并记 WARNING，不影响前序完整行。
    """
    p = Path(path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("%s 第 %d 行解析失败(可能为崩溃截断尾行), 跳过: %s", p, lineno, e)


def count_lines(path: str | Path) -> int:
    """统计 JSONL 有效对象行数（用于 partition/unit 计数）。"""
    return sum(1 for _ in iter_jsonl(path))

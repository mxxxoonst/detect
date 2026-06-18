"""JSON / JSONL 解析器: strict (ijson 流式) + tolerant (json5 容错)."""

import ijson
import json
import json5
from pathlib import Path

from src.parse.grade import Grade
from src.utils.encoding import safe_decode
from src.utils.file_utils import count_lines, read_head_bytes
from src.utils.logger import get_logger

log = get_logger(__name__)


def parse_json(path: str, encoding: str) -> Grade:
    """两级 ijson 流式策略解析 JSON 数组，兜底 json5 处理非数组/语法噪声。

    Level 1: ijson.items 流式读完所有元素，无崩溃 → tier1, I=1.0
    Level 2: ijson 中途崩溃但已读出部分元素 → 按行数比例估算 I(x)
    兜底:    good==0（非数组 / 首条即损坏）→ json5 容错
    """
    raw = read_head_bytes(path)
    text = safe_decode(raw, encoding)

    good, crashed, bytes_good, err_msg = _ijson_count_items(path)

    if good > 0:
        if not crashed:
            # ── Level 1: 全量解析成功 ──────────────────────────────
            return Grade(
                tier=1, I=1.0, fmt="json", encoding=encoding,
                parsed={"type": "json", "path": path, "encoding": encoding},
                note=f"ijson strict OK, {good} items",
            )

        # ── Level 2: 崩溃前读出了部分记录 ──────────────────────────
        # estimated_total = 文件总行数 / (good_items 占的行数 / good_items)
        #                 = 文件总行数 × good_items / lines_consumed
        file_size = Path(path).stat().st_size
        total_lines = count_lines(path, encoding)

        # 用字节比例近似 good_items 占的行数
        # 注意: ijson 有读取缓冲，bytes_good 可能接近 file_size（末尾截断场景）
        # 因此 estimated_total 下界取 good+1：崩溃说明至少还有 1 条损坏记录
        lines_consumed = max(1, round(total_lines * bytes_good / max(file_size, 1)))
        avg_lines_per_item = lines_consumed / good
        estimated_total = max(good + 1, round(total_lines / avg_lines_per_item))
        I = min(good / estimated_total, 1.0)
        tier = 1 if I >= 0.99 else 2

        return Grade(
            tier=tier, I=I, fmt="json", encoding=encoding,
            parsed={
                "type": "json", "path": path, "encoding": encoding,
                "good_items": good, "estimated_total": estimated_total,
            },
            n_form="partial_array",
            n_detail={"kind": "partial_array", "reason": err_msg[:200], "offset": bytes_good},
            note=f"ijson partial: {good}/{estimated_total} items, I={I:.3f}",
        )

    # ── 兜底: good==0，顶层非数组或首条即损坏 → json5 容错 ────────
    return _json_tolerant(path, encoding, text, "no items from ijson streaming")


def _ijson_count_items(path: str) -> tuple:
    """用 ijson.items 流式读顶层数组，统计成功元素数和崩溃前消耗字节。

    Returns:
        (good_count, crashed, bytes_at_last_good_item, err_msg)
        - good_count: 成功 yield 的元素数
        - crashed:    True 表示 ijson 中途抛异常
        - bytes_at_last_good_item: 最后一个成功元素 yield 后已读字节数
          （用于按比例估算 good_items 在文件中占的行数）
    """
    class _PosTracker:
        """透传 read()，同时累计已读字节数，供 ijson 消费。"""
        def __init__(self, f):
            self._f = f
            self.pos = 0

        def read(self, n=-1):
            data = self._f.read(n)
            self.pos += len(data)
            return data

    good = 0
    last_good_pos = 0

    try:
        with open(path, "rb") as f:
            tracker = _PosTracker(f)
            try:
                for _item in ijson.items(tracker, "item"):
                    good += 1
                    last_good_pos = tracker.pos
                return good, False, last_good_pos, ""   # 无崩溃
            except Exception as e:
                log.debug("ijson 流式中途崩溃 %s: 已读 %d 项, %d 字节 (%s)",
                          path, good, last_good_pos, e)
                return good, True, last_good_pos, f"{type(e).__name__}: {e}"
    except OSError as e:
        log.warning("JSON 文件打开失败 %s: %s", path, e)
        return 0, True, 0, f"{type(e).__name__}: {e}"


def parse_jsonl(path: str, encoding: str) -> Grade:
    """解析 JSONL: 每行一个 JSON 对象。"""
    try:
        good = 0
        bad = 0
        bad_samples = []
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for lineno, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    good += 1
                except json.JSONDecodeError as e:
                    bad += 1
                    if len(bad_samples) < 5:
                        # 只存结构化诊断(错误串+行号+长度)，不落原始行内容(守 PII 红线)
                        bad_samples.append({"lineno": lineno, "err": str(e)[:200], "len": len(line)})
        total = good + bad
        if total == 0:
            return Grade(tier=3, I=0.0, fmt="jsonl", encoding=encoding)

        I = good / total if total > 0 else 0.0
        if bad:
            log.debug("JSONL %s: %d 好行 / %d 坏行 (I=%.3f)", path, good, bad, I)
        if I == 1.0:
            return Grade(tier=1, I=1.0, fmt="jsonl", encoding=encoding,
                         parsed={"type": "jsonl", "units": good})
        return Grade(tier=2, I=I, fmt="jsonl", encoding=encoding,
                     n_form="jsonl_parse_error",
                     n_detail={"kind": "jsonl_parse_error", "bad_count": bad, "samples": bad_samples},
                     parsed={"type": "jsonl", "units": good, "bad_lines": bad})
    except Exception as e:
        log.warning("JSONL 解析失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, fmt="jsonl", encoding=encoding, error=str(e))


def _json_tolerant(path: str, encoding: str, text: str, strict_error: str) -> Grade:
    """json5 容错解析，用于顶层非数组或 json5 语法噪声的文件。

    I(x) 修正:
      - head 覆盖整个文件 (小文件) → json5 已恢复全部内容 → I = 1.0
      - head 仅覆盖部分 (大文件) → 按平均对象字节数外推 estimated_total
    """
    try:
        data = json5.loads(text)

        if isinstance(data, list):
            recovered = len(data)
        elif isinstance(data, dict):
            recovered = 1
        else:
            recovered = 0

        # head 与整个文件的覆盖比较
        fsize = Path(path).stat().st_size
        head_bytes = len(text.encode("utf-8", errors="replace"))

        if head_bytes >= fsize * 0.99:
            # head 就是完整文件，json5 已恢复所有内容
            I = 1.0 if recovered > 0 else 0.0
        else:
            # 大文件：按已读部分的平均对象大小推算总量
            avg_obj_bytes = max(head_bytes / max(recovered, 1), 1)
            total_units = max(recovered, int(fsize / avg_obj_bytes))
            I = min(recovered / total_units, 1.0)

        tier = 1 if I >= 0.99 else 2
        return Grade(
            tier=tier, I=I, fmt="json", encoding=encoding,
            parsed={"type": "json", "tolerant": True, "units": recovered},
            n_form=_classify_json_error(strict_error),
            note=f"json5 tolerant recovery, I={I:.3f}",
        )
    except Exception as e2:
        log.warning("JSON json5 容错也失败 %s: strict=%s; tolerant=%s",
                    path, strict_error, e2)
        return Grade(tier=3, I=0.0, fmt="json", encoding=encoding,
                     error=f"strict: {strict_error}; tolerant: {e2}")


def _classify_json_error(error_msg: str) -> str:
    """将 JSON 解析错误分类."""
    msg = error_msg.lower()
    if "trailing comma" in msg or "expecting property name" in msg:
        return "trailing_comma"
    if "single quote" in msg or "expecting value" in msg:
        return "single_quotes"
    if "comment" in msg or "expecting value" in msg:
        return "comments"
    if "unterminated string" in msg:
        return "unclosed_string"
    if "expecting" in msg:
        return "incomplete"
    return "other"

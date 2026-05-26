"""JSON / JSONL 解析器: strict (ijson 流式) + tolerant (json5 容错)."""

import ijson
import json
import json5

from src.parse.grade import Grade
from src.utils.encoding import safe_decode
from src.utils.file_utils import count_lines, read_head_bytes
from src.constants import SNIFF_HEAD_BYTES


def parse_json(path: str, encoding: str) -> Grade:
    """流式严格解析 JSON, 失败回退 json5 容错."""
    raw = read_head_bytes(path)
    text = safe_decode(raw, encoding)

    # strict parse via ijson (streaming)
    try:
        # ijson 需要 bytes 输入做流式解析
        records = []
        with open(path, "rb") as f:
            parser = ijson.parse(f)
            for _prefix, _event, value in parser:
                # 仅收集顶层对象/数组元素做计数
                pass
        # ijson 跑通 = strict OK
        # 重新读文件统计 record 数
        total_units = _estimate_json_units(path, encoding, text)
        return Grade(tier=1, I=1.0, fmt="json", encoding=encoding,
                     parsed={"type": "json", "path": path, "encoding": encoding},
                     note=f"strict parse OK, ~{total_units} units")
    except Exception as e:
        # tolerant fallback with json5
        return _json_tolerant(path, encoding, text, str(e))


def parse_jsonl(path: str, encoding: str) -> Grade:
    """解析 JSONL: 每行一个 JSON 对象."""
    try:
        good = 0
        bad = 0
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                    good += 1
                except json.JSONDecodeError:
                    bad += 1
        total = good + bad
        if total == 0:
            return Grade(tier=3, I=0.0, fmt="jsonl", encoding=encoding)

        I = good / total if total > 0 else 0.0
        if I == 1.0:
            return Grade(tier=1, I=1.0, fmt="jsonl", encoding=encoding,
                         parsed={"type": "jsonl", "units": good})
        return Grade(tier=2, I=I, fmt="jsonl", encoding=encoding,
                     n_form="jsonl_parse_error",
                     parsed={"type": "jsonl", "units": good, "bad_lines": bad})
    except Exception as e:
        return Grade(tier=3, I=0.0, fmt="jsonl", encoding=encoding, error=str(e))


def _json_tolerant(path: str, encoding: str, text: str, strict_error: str) -> Grade:
    """json5 容错解析."""
    try:
        data = json5.loads(text)
        total_lines = count_lines(path, encoding)
        total_units = max(total_lines // 2, 1)
        # 成功恢复, 计算 I(x)
        if isinstance(data, list):
            recovered = len(data)
        elif isinstance(data, dict):
            recovered = 1
        else:
            recovered = 0
        I = min(recovered / total_units, 1.0) if total_units > 0 else 0.0
        tier = 1 if I >= 0.99 else 2
        return Grade(tier=tier, I=I, fmt="json", encoding=encoding,
                     parsed={"type": "json", "tolerant": True, "units": recovered},
                     n_form=_classify_json_error(strict_error),
                     note=f"json5 tolerant recovery, I={I:.3f}")
    except Exception as e2:
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


def _estimate_json_units(path: str, encoding: str, head_text: str) -> int:
    """估算 JSON 文件中的结构单元数.

    方法 A: 按文件大小线性估算.
    如果 JSON 顶层是数组, 估算元素数; 否则按 1 个单元计.
    """
    stripped = head_text.strip()
    if stripped.startswith("["):
        # 数组: 按文件大小/平均元素大小估算
        file_size = __import__("os").path.getsize(path)
        avg_item_size = max(len(stripped) // 10, 1)
        return max(file_size // avg_item_size, 1)
    return 1

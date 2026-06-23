"""JSON / JSONL 解析器: strict (ijson 流式) + tolerant (json5 容错)."""

import ijson
import json
from pathlib import Path

from src.parse.grade import Grade
from src.parse.json_recovery import looks_like_jsonl_path, tolerant_load_text
from src.utils.encoding import safe_decode
from src.utils.file_utils import count_lines, read_head_bytes
from src.utils.logger import get_logger

log = get_logger(__name__)


def parse_json(path: str, encoding: str) -> Grade:
    """JSON 严格内核 (ijson 流式) + 容错叠加 (json5 / partial-array / JSONL 探测)。

    单元粒度 = 顶层数组元素。N = C + P + L:
      C = ijson 严格全程零异常消费到 EOF 的元素 (干净)。
      P = json5 救回、可信的对象 (容错修复)。
      L = ijson 崩溃点之后无法消费的残片 (落通道二)。

    度量与 tier:
      I_strict = C/N (种子门)；I = (C+P)/N (官方退化曲线)。
      tier1 ⟺ I_strict==1 ⟺ ijson 全程零偏离消费到 EOF (零 P 零 L)。
      容错路径 (json5 / partial) **封顶 tier2** —— 修过的文件绝不漏进种子库。

    Level 1: ijson.items 流式读完所有元素且无崩溃 → strict OK → tier1, I_strict=1。
    Level 2: ijson 中途崩溃但已读出部分元素 → partial-array, 崩溃点后估为 L。
    兜底:    good==0 → 先探测 JSONL-as-.json (逐行独立对象), 否则走 json5 容错。
    """
    raw = read_head_bytes(path)
    text = safe_decode(raw, encoding)

    good, crashed, bytes_good, err_msg = _ijson_count_items(path)

    if good > 0:
        if not crashed:
            # ── Level 1: 严格内核全程零异常消费到 EOF → 干净种子 ────
            return Grade(
                tier=1, I=1.0, I_strict=1.0, fmt="json", encoding=encoding,
                parsed={"type": "json", "path": path, "encoding": encoding, "units": good},
                note=f"ijson strict OK, {good} items",
            )

        # ── Level 2: 崩溃前读出了部分记录 → partial-array (封顶 tier2) ──
        # 崩溃点之后无法消费的记录纳入 L 计数 (修「早期崩溃后续不计」)。
        # C = good (崩溃前严格消费成功)；崩溃说明至少 1 条损坏 → L >= 1。
        file_size = Path(path).stat().st_size
        total_lines = count_lines(path, encoding)

        # 用字节比例近似 good 占的行数, 外推总记录数 N (下界 good+1)。
        lines_consumed = max(1, round(total_lines * bytes_good / max(file_size, 1)))
        avg_lines_per_item = lines_consumed / good
        estimated_total = max(good + 1, round(total_lines / avg_lines_per_item))

        C = good
        N = estimated_total
        L = max(N - C, 1)            # 崩溃点之后 (含损坏的那条) 计 L
        P = 0                        # ijson partial 不做 json5 二次救援, 故 P=0
        I_strict = C / N
        I = (C + P) / N
        # 容错路径封顶 tier2: 即便 I 很高也不给 tier1 (堵 tier1 泄漏)。
        tier = 2 if I > 0.0 else 3

        return Grade(
            tier=tier, I=I, I_strict=I_strict, fmt="json", encoding=encoding,
            parsed={
                "type": "json", "path": path, "encoding": encoding,
                "good_items": C, "estimated_total": N,
                "clean_items": C, "repaired_items": P, "lost_items": L,
            },
            n_form="partial_array",
            n_detail={"kind": "partial_array", "reason": err_msg[:200], "offset": bytes_good,
                      "c_count": C, "p_count": P, "l_count": L, "n_total": N},
            note=f"ijson partial: {C}/{N} items, I_strict={I_strict:.3f} I={I:.3f}",
        )

    # ── 兜底: good==0，顶层非数组或首条即损坏 ────────────────────
    # 先探测 JSONL-as-.json (逐行独立对象), 消除静默零产出 (共享 json_recovery 单一真源)。
    if looks_like_jsonl_path(path, encoding):
        log.debug("JSON %s: 顶层数组零记录, 探测为逐行独立对象 → 转 JSONL 迭代", path)
        grade = parse_jsonl(path, encoding)
        grade.fmt = "json"          # 后缀仍是 .json, 仅标注以 JSONL 语义恢复
        if grade.parsed is not None:
            grade.parsed["jsonl_as_json"] = True
        if grade.note:
            grade.note = "JSONL-as-.json: " + grade.note
        else:
            grade.note = "JSONL-as-.json recovered"
        return grade

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

        # JSONL: 每行一单元, 无中间「修复」态 → C=好行, L=坏行, P=0。
        # I_strict = 好行/总行 (bad==0 ⟺ I_strict==1 ⟺ tier1)。
        I = good / total if total > 0 else 0.0
        I_strict = I
        if bad:
            log.debug("JSONL %s: %d 好行 / %d 坏行 (I_strict=%.3f)", path, good, bad, I_strict)
        if bad == 0:
            return Grade(tier=1, I=1.0, I_strict=1.0, fmt="jsonl", encoding=encoding,
                         parsed={"type": "jsonl", "units": good})
        return Grade(tier=2, I=I, I_strict=I_strict, fmt="jsonl", encoding=encoding,
                     n_form="jsonl_parse_error",
                     n_detail={"kind": "jsonl_parse_error", "bad_count": bad, "samples": bad_samples,
                               "c_count": good, "p_count": 0, "l_count": bad, "n_total": total},
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
        data = tolerant_load_text(text)   # 共享 json_recovery 单一真源 (与 extract 同源)

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

        if recovered == 0:
            return Grade(tier=3, I=0.0, I_strict=0.0, fmt="json", encoding=encoding,
                         n_form=_classify_json_error(strict_error),
                         note="json5 recovered nothing")

        # 容错路径封顶 tier2: json5 修过 ⟹ 严格侧零通过 ⟹ I_strict=0, 绝不给 tier1。
        # 恢复内容全部计 P (可信修复), 故 I=(C+P)/N 中 C=0。
        I_strict = 0.0
        tier = 2
        return Grade(
            tier=tier, I=I, I_strict=I_strict, fmt="json", encoding=encoding,
            parsed={"type": "json", "tolerant": True, "units": recovered},
            n_form=_classify_json_error(strict_error),
            note=f"json5 tolerant recovery, I_strict={I_strict:.3f} I={I:.3f}",
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

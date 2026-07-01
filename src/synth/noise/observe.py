"""Stage 4:容错解析(观测线)—— 带噪 CSV → per-char 观测特征 + seg2 RAW 段。

产出(**观测,可错**,作输入特征,不是标签):
    tentative_field  : 观测列序号(-1 非值/坏区)
    tentative_record : 观测行序号(-1 非值/坏区)
    is_raw           : 1 = 落在不可恢复区(seg2)
    raw_spans        : [(start, end)] 坏区字符区间

原则:同一套 `scan_csv_cells`,喂**干净**得 oracle 归组(Stage 1),喂**带噪**得观测归组(此处)。
列漂移/合并 → 观测归组与 oracle 不同(这正是模型要学的可错输入);未闭合引号 → 从该处到 EOF
列切分坍塌 → 标 seg2。
"""

from typing import Dict, List, Optional

from src.synth.noise.csv_align import scan_csv_cells
from src.utils.logger import get_logger

log = get_logger(__name__)


def observe_csv(noised_text: str, delim: str = ",", quote: str = '"') -> Dict:
    """带噪 CSV → 观测特征 + seg2 RAW 段。"""
    n = len(noised_text)
    tentative_field = [-1] * n
    tentative_record = [-1] * n
    is_raw = [0] * n

    # 观测归组:同 scan,但喂带噪文本 → 可错(列漂移处与 oracle 不一致)
    for row, col, s, e in scan_csv_cells(noised_text, delim, quote):
        for k in range(s, e):
            tentative_field[k] = col
            tentative_record[k] = row

    # seg2:未闭合引号 → 从最后一个未匹配引号到 EOF 观测坍塌 → 不可恢复
    raw_spans: List[tuple] = []
    open_pos = _unbalanced_quote_open(noised_text, quote)
    if open_pos is not None:
        for k in range(open_pos, n):
            is_raw[k] = 1
            tentative_field[k] = -1
            tentative_record[k] = -1
        raw_spans.append((open_pos, n))
        log.debug("observe_csv: 未闭合引号 → seg2 [%d, %d)", open_pos, n)

    return {
        "tentative_field": tentative_field,
        "tentative_record": tentative_record,
        "is_raw": is_raw,
        "raw_spans": raw_spans,
    }


def _unbalanced_quote_open(text: str, quote: str = '"') -> Optional[int]:
    """返回最后一个未匹配开引号的位置;引号全平衡则 None。"""
    in_q = False
    open_pos: Optional[int] = None
    for i, c in enumerate(text):
        if c == quote:
            if not in_q:
                in_q, open_pos = True, i
            else:
                in_q, open_pos = False, None
    return open_pos if in_q else None

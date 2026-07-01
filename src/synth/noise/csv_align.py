"""Stage 1:对**干净** CSV 做精确 span 解析,得 CleanAlign(标签线,非观测线)。

CleanAlign: 干净文本每个**值字符**的下标 → (row_id, col_id, type_tag)。
- row_id: 0=表头, 1..=数据行;col_id=列序号(= 字段槽 field_id)。
- 结构字符(分隔符/换行)**不入** CleanAlign(它们不归属任何字段)。

干净合成文件必然严格可解,故这里走精确扫描(引号平衡);与容错解析器(观测线)分属两条。
"""

from typing import Dict, Iterator, Tuple

from src.utils.logger import get_logger

log = get_logger(__name__)

INSERTED = -1  # EditMap 中标记"注入字符"(无干净来源)

CleanAlign = Dict[int, Tuple[int, int, str]]


def scan_csv_cells(
    text: str, delim: str = ",", quote: str = '"'
) -> Iterator[Tuple[int, int, int, int]]:
    """按字符扫描干净 CSV,yield 每个 cell 的 (row, col, start, end) 字符区间。

    start/end 为 cell 内容在 `text` 中的字符下标区间 [start, end)(含引号本体)。
    quote-aware:引号内的 delim/换行不切分。干净文件假设引号平衡。
    """
    n = len(text)
    i = 0
    row = col = 0
    cell_start = 0
    in_quote = False
    while i < n:
        c = text[i]
        if c == quote:
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote and c == delim:
            yield (row, col, cell_start, i)
            col += 1
            i += 1
            cell_start = i
            continue
        if not in_quote and c == "\n":
            end = i
            if end > cell_start and text[end - 1] == "\r":
                end -= 1
            yield (row, col, cell_start, end)
            row += 1
            col = 0
            i += 1
            cell_start = i
            continue
        i += 1
    # 收尾:无末尾换行时的最后一个 cell,或行以 delim 结尾的空尾列
    if cell_start < n or (n > 0 and text[n - 1] == delim):
        end = n
        if end > cell_start and text[end - 1] == "\r":
            end -= 1
        yield (row, col, cell_start, end)


def classify_type(v: str) -> str:
    """极简值类型分类(type_tag)。空/NULL→null,整数→int,浮点→float,布尔→bool,否则 str。"""
    s = v.strip().strip('"').strip()
    if s == "" or s.upper() == "NULL":
        return "null"
    if s.lower() in ("true", "false"):
        return "bool"
    try:
        int(s)
        return "int"
    except ValueError:
        pass
    try:
        float(s)
        return "float"
    except ValueError:
        pass
    return "str"


def build_clean_align_csv(text: str, delim: str = ",") -> CleanAlign:
    """干净 CSV → {值字符下标: (row, col, type_tag)}。结构字符不入表。"""
    align: CleanAlign = {}
    for row, col, s, e in scan_csv_cells(text, delim):
        t = classify_type(text[s:e])
        for k in range(s, e):
            align[k] = (row, col, t)
    log.debug("build_clean_align_csv: %d 值字符, %d cells", len(align), _cell_count(text, delim))
    return align


def _cell_count(text: str, delim: str) -> int:
    return sum(1 for _ in scan_csv_cells(text, delim))

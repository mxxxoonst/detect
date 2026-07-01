"""Stage 2:CSV 注噪算子。每算子 = clean_text → (noised_text, EditMap, NoiseMeta)。

EditMap: 带噪字符下标 → 干净字符下标(或 INSERTED=-1 表示注入字符)。
算子**只改文本 + 产 EditMap**,不碰 CleanAlign(对齐在 inject.compose 里合成)。

不交叉注噪:每次只调一个算子(见 inject.inject_one)。当前覆盖三个结构效应:
    A_unrecoverable = 截断        (form)
    B_boundary      = 未转义分隔符 (form)  —— 注入假分隔符
    C_misbind       = 删分隔符     (structure) —— 列漂移/字段合并
"""

import random
from typing import Dict, Optional, Tuple

from src.synth.noise.csv_align import INSERTED, scan_csv_cells

EditMap = Dict[int, int]
NoiseResult = Optional[Tuple[str, EditMap, dict]]


def op_truncation(text: str, rng: random.Random, cut: Optional[int] = None) -> NoiseResult:
    """效应 A:在后半随机字节处截断,尾部丢失 → 末行成不可恢复残块。"""
    n = len(text)
    if n < 4:
        return None
    if cut is None:
        cut = rng.randrange(max(int(n * 0.5), 1), n)
    noised = text[:cut]
    edit_map: EditMap = {i: i for i in range(cut)}
    meta = {"layer": "form", "op": "truncation", "effect": "A_unrecoverable",
            "span": (cut, n), "extra": {}}
    return noised, edit_map, meta


def op_unescaped_delimiter(
    text: str, rng: random.Random, delim: str = ",",
    target: Optional[Tuple[int, int]] = None,
) -> NoiseResult:
    """效应 B:向某数据 cell 内部注入裸分隔符 → 制造"假边界"。

    oracle 里该逗号无干净来源(INSERTED),两侧仍属同一 (row,col) → 教模型
    "这个分隔符不是真边界、两边同字段"。
    """
    cells = [(r, c, s, e) for (r, c, s, e) in scan_csv_cells(text, delim)
             if r >= 1 and e - s >= 2]
    if not cells:
        return None
    if target is not None:
        cand = [x for x in cells if (x[0], x[1]) == target]
        if not cand:
            return None
        cell = cand[0]
    else:
        cell = cells[rng.randrange(len(cells))]
    _r, _c, s, e = cell
    pos = (s + e) // 2  # cell 内部插入
    noised = text[:pos] + delim + text[pos:]
    edit_map: EditMap = {i: i for i in range(pos)}
    edit_map[pos] = INSERTED
    for i in range(pos + 1, len(noised)):
        edit_map[i] = i - 1
    meta = {"layer": "form", "op": "unescaped_delimiter", "effect": "B_boundary",
            "span": (pos, pos + 1), "extra": {"cell": (cell[0], cell[1])}}
    return noised, edit_map, meta


def op_unclosed_quote(
    text: str, rng: random.Random, quote: str = '"',
    target: Optional[Tuple[int, int]] = None,
) -> NoiseResult:
    """效应 A(CSV 版):向某数据 cell 头注入未闭合引号 → 从此到 EOF 观测坍塌成不可恢复区。

    比截断更能在 CSV 上制造 seg2:注入的 `"` 无匹配闭合,容错解析从该处失去列切分。
    """
    cells = [(r, c, s, e) for (r, c, s, e) in scan_csv_cells(text, ",")
             if r >= 1 and e - s >= 1]
    if not cells:
        return None
    if target is not None:
        cand = [x for x in cells if (x[0], x[1]) == target]
        if not cand:
            return None
        cell = cand[0]
    else:
        cell = cells[rng.randrange(len(cells))]
    pos = cell[2]  # cell 头
    noised = text[:pos] + quote + text[pos:]
    edit_map: EditMap = {i: i for i in range(pos)}
    edit_map[pos] = INSERTED
    for i in range(pos + 1, len(noised)):
        edit_map[i] = i - 1
    meta = {"layer": "form", "op": "unclosed_quote", "effect": "A_unrecoverable",
            "span": (pos, pos + 1), "extra": {"cell": (cell[0], cell[1])}}
    return noised, edit_map, meta


def op_delete_delimiter(
    text: str, rng: random.Random, delim: str = ",",
    target: Optional[int] = None,
) -> NoiseResult:
    """效应 C:删除某数据行的一个真分隔符 → 相邻两列合并、列漂移。

    oracle 里两段字符各自保留原列 → 观测看成一个 cell,但 field-binding 标签
    仍能把两段分回原列;被删处应是边界(缺失边界)。
    """
    seps = [e for (r, _c, _s, e) in scan_csv_cells(text, delim)
            if r >= 1 and e < len(text) and text[e] == delim]
    if not seps:
        return None
    d = target if target is not None else seps[rng.randrange(len(seps))]
    noised = text[:d] + text[d + 1:]
    edit_map: EditMap = {i: i for i in range(d)}
    for i in range(d, len(noised)):
        edit_map[i] = i + 1
    meta = {"layer": "structure", "op": "delete_delimiter", "effect": "C_misbind",
            "span": (d, d), "extra": {}}
    return noised, edit_map, meta

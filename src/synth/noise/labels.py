"""Stage 3:从 OracleChar 派生 4 头的 per-char 训练标签(带噪坐标系)。

产出(长度均 = len(noised_text)):
    field_id        : 值字符 → 列序号(字段槽);非值字符 = IGNORE(-100)
    field_boundary  : 1 = 该字符是某 cell 的首字符(字段边界起点)
    record_boundary : 1 = 该字符是某行首 cell 的首字符(记录边界起点)
    damaged         : 1 = 注入字符(假分隔符等)

对应头:field_id→field-binding;field/record_boundary→boundary;damaged→boundary 的
damaged 通道;re-anchor 走 seg2(观测侧,Stage 4,暂不在此)。一致性头用配对视图,无逐字符标签。
"""

from typing import Dict, List

from src.synth.noise.inject import OracleChar

IGNORE = -100  # 交叉熵忽略位(非值字符的 field_id)


def labels_from_align(noised_text: str, oracle: OracleChar) -> Dict[str, List[int]]:
    """OracleChar → 4 组 per-char 标签。"""
    n = len(noised_text)
    field_id = [IGNORE] * n
    field_boundary = [0] * n
    record_boundary = [0] * n
    damaged = [0] * n

    prev_val = None  # 上一个"值字符"的 (row, col);跳过 STRUCT/INSERTED
    for i in range(n):
        o = oracle.get(i)
        if o == "INSERTED":
            damaged[i] = 1
            continue  # 注入字符:非真边界、非值 → 不更新 prev_val
        if isinstance(o, tuple):
            row, col, _t = o
            field_id[i] = col
            if prev_val is None or prev_val[0] != row or prev_val[1] != col:
                field_boundary[i] = 1
                if prev_val is None or prev_val[0] != row:
                    record_boundary[i] = 1
            prev_val = (row, col)
        # "STRUCT"(真分隔符/换行)/ None:非值字符,保持默认,不更新 prev_val

    return {
        "field_id": field_id,
        "field_boundary": field_boundary,
        "record_boundary": record_boundary,
        "damaged": damaged,
    }


def reanchor_labels(raw_spans, oracle: OracleChar) -> List[int]:
    """seg2 每个 RAW 段的 re-anchor 标签 = 该段字符在 oracle 里的众数记录行。

    坏块内容不可解析,但其字符经 edit_map 仍回溯到干净记录 → 众数行即"该塞回哪条记录";
    段内无值字符(纯结构)则记 -1(table 级)。
    """
    out: List[int] = []
    for s, e in raw_spans:
        rows = [oracle[i][0] for i in range(s, e)
                if isinstance(oracle.get(i), tuple)]
        if rows:
            out.append(max(set(rows), key=rows.count))
        else:
            out.append(-1)
    return out

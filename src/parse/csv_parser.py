"""CSV / TSV 解析器: strict(列一致) + tolerant(skip bad lines)."""

import csv
from collections import Counter
from statistics import stdev

from src.parse.grade import Grade
from src.utils.logger import get_logger

log = get_logger(__name__)


def parse_csv(path: str, encoding: str) -> Grade:
    """解析 CSV: 嗅探分隔符, strict 要求列数一致, 失败则 tolerant skip."""
    sep = _sniff_delimiter(path, encoding)
    return _parse_delimited(path, encoding, sep, "csv")


def parse_tsv(path: str, encoding: str) -> Grade:
    """解析 TSV: 固定 \t 分隔符."""
    return _parse_delimited(path, encoding, "\t", "tsv")


def _read_rows(path: str, encoding: str, sep: str):
    """quote-aware 流式读非空行, 返回 (raw_lens, core_lens, headers, read_error)。

    用 csv.reader (RFC 4180: 尊重引号内分隔符/换行/`""` 转义), 禁 split(sep)。
    - raw_lens: 每行原始列数。
    - core_lens: 每行去掉**尾部连续空单元**后的列数 (末个非空单元下标+1)。
      用于把"行尾多余逗号"(trailing comma → 空的幻影尾列) 与真实结构漂移区分开。
    read_error: reader 中途抛异常时为该异常 (部分行已读), 否则 None。
    """
    raw_lens: list[int] = []
    core_lens: list[int] = []
    headers = None
    read_error = None
    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=sep)
            for row in reader:
                if not row or all(cell.strip() == "" for cell in row):
                    continue
                raw_lens.append(len(row))
                core_lens.append(_core_len(row))
                if headers is None:
                    headers = row
    except Exception as e:
        read_error = e
    return raw_lens, core_lens, headers, read_error


def _core_len(row: list) -> int:
    """去掉尾部连续空单元后的列数 (末个非空单元下标+1; 全空→0)。"""
    i = len(row)
    while i > 0 and row[i - 1].strip() == "":
        i -= 1
    return i


def _row_clean(raw: int, core: int, width: int) -> bool:
    """行是否与 schema 等宽 (trailing-comma 容忍)。

    干净 ⟺ 原始列数等于 schema 宽度 (raw==width),
        或仅**多出空的尾列** (raw>width ∧ core<=width, 即超宽部分全空 = 行尾逗号)。
    raw<width (缺尾字段) 或 raw>width 且尾列非空 (内嵌分隔符把真实值推过宽度) → 不干净。
    """
    return raw == width or (raw > width and core <= width)


def _parse_delimited(path: str, encoding: str, sep: str, fmt: str) -> Grade:
    """通用分隔符解析: strict 谓词 (列全等) + tolerant (列漂移/skip), 回填 I_strict。

    单元粒度 = 数据行。N = 总行数。
      C = 列数等于众数 (modal) 且 reader 无错的行 (严格自洽)。
      P = 偏离众数但仍成行的行 (容错重对齐救回)。
      L = reader 直接丢的行 (读中断后未读到的部分)。
    I_strict = 列全等 ? 1.0 : C/N (modal_consistent/total)；I = (C+P)/N。
    """
    raw_lens, core_lens, headers, read_error = _read_rows(path, encoding, sep)
    total_rows = len(raw_lens)

    if total_rows == 0:
        if read_error is not None:
            log.warning("%s 解析失败(零行) %s: %s", fmt.upper(), path, read_error)
            return Grade(tier=3, I=0.0, I_strict=0.0, fmt=fmt, encoding=encoding,
                         error=str(read_error))
        return Grade(tier=3, I=0.0, I_strict=0.0, fmt=fmt, encoding=encoding, note="empty file")

    # schema 宽度 = 众数列数 (数据共识, 不被坍塌表头带偏); 行清洁判定带 trailing-comma 容忍。
    modal = Counter(raw_lens).most_common(1)[0][0]
    width = modal
    clean_flags = [_row_clean(r, c, width) for r, c in zip(raw_lens, core_lens)]
    trailing_comma_rows = sum(
        1 for r, c, ok in zip(raw_lens, core_lens, clean_flags) if ok and r > width
    )

    # ── strict 谓词: reader 无错 ∧ 每行等宽(容忍空尾列) → tier1 (I_strict==1) ──
    if read_error is None and width >= 1 and all(clean_flags):
        return Grade(tier=1, I=1.0, I_strict=1.0, fmt=fmt, encoding=encoding,
                     parsed={"type": fmt, "headers": headers, "rows": total_rows,
                             "columns": width, "trailing_comma_rows": trailing_comma_rows})

    # ── tolerant: 真实列漂移 / reader 中断 → 计 C/P/L ──
    C = sum(clean_flags)                                # 等宽(容忍空尾列) → clean
    P = total_rows - C                                  # 真实偏离(缺字段/内嵌分隔符) → repaired
    # reader 中断: 已读 total_rows 行成行, 未读到的部分为 L (无从知数, 至少 1)。
    L = 1 if read_error is not None else 0
    N = total_rows + L

    I_strict = C / N
    I = (C + P) / N
    tier = 2 if I > 0.0 else 3

    drift = _column_drift(raw_lens)
    header_cols = raw_lens[0]
    modal_frac = C / total_rows
    if read_error is not None:
        kind = "read_interrupted"
    elif header_cols != modal and modal_frac >= 0.7:
        # 列名坍塌: 表头列数 != 数据众数列数, 且数据行多数自洽 (value 正常分列)
        kind = "header_col_mismatch"
    else:
        kind = "col_drift"

    n_detail = {"kind": kind, "drift": round(drift, 4),
                "modal_cols": modal, "header_cols": header_cols,
                "col_hist": _col_hist(raw_lens),
                "c_count": C, "p_count": P, "l_count": L, "n_total": N}
    if read_error is not None:
        n_detail["reason"] = str(read_error)[:200]
        log.debug("%s 解析中断但已读 %d 行 %s: %s", fmt.upper(), total_rows, path, read_error)

    return Grade(tier=tier, I=I, I_strict=I_strict, fmt=fmt, encoding=encoding,
                 n_struct=drift, n_detail=n_detail,
                 parsed={"type": fmt, "headers": headers,
                         "good_rows": total_rows, "total_rows": N})


def _sniff_delimiter(path: str, encoding: str) -> str:
    """嗅探 CSV 分隔符: 在候选中选列数最稳定者。

    **quote-aware**: 用 csv.reader 数列 (尊重引号内分隔符), 禁 line.count(sep)——
    否则引号内的分隔符会被误计 (如 `a,"b,c"` naive 数成 3 列实为 2 列)。
    每个候选只读头部 20 行, 流式不整文件 load。
    """
    candidates = [",", ";", "|", ":", "\t"]
    best_sep = ","
    best_stability = float("inf")
    for sep in candidates:
        col_counts = []
        try:
            with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
                reader = csv.reader(f, delimiter=sep)
                for row in reader:
                    if not row or all(cell.strip() == "" for cell in row):
                        continue
                    col_counts.append(len(row))
                    if len(col_counts) >= 20:
                        break
        except Exception as e:
            log.debug("分隔符 %r 嗅探失败 %s: %s", sep, path, e)
            continue
        if col_counts and len(col_counts) >= 2 and col_counts[0] >= 2:
            sd = stdev(col_counts) if len(col_counts) > 1 else 0.0
            if sd < best_stability:
                best_stability = sd
                best_sep = sep
    return best_sep


def _col_hist(col_counts: list) -> dict:
    """列数分布直方图 {列数: 行数}，作为列漂移的结构化噪声签名（不含原始值）。"""
    return {str(k): v for k, v in Counter(col_counts).most_common()}


def _column_drift(col_counts: list) -> float:
    """列漂移度量: 列数的变异系数."""
    if not col_counts:
        return 0.0
    mean = sum(col_counts) / len(col_counts)
    if mean == 0:
        return 0.0
    variance = sum((c - mean) ** 2 for c in col_counts) / len(col_counts)
    return (variance ** 0.5) / mean

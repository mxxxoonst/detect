"""CSV / TSV 解析器: strict(列一致) + tolerant(skip bad lines)."""

import csv
from statistics import stdev

from src.parse.grade import Grade


def parse_csv(path: str, encoding: str) -> Grade:
    """解析 CSV: 嗅探分隔符, strict 要求列数一致, 失败则 tolerant skip."""
    sep = _sniff_delimiter(path, encoding)
    return _parse_delimited(path, encoding, sep, "csv")


def parse_tsv(path: str, encoding: str) -> Grade:
    """解析 TSV: 固定 \t 分隔符."""
    return _parse_delimited(path, encoding, "\t", "tsv")


def _parse_delimited(path: str, encoding: str, sep: str, fmt: str) -> Grade:
    """通用分隔符文件解析."""
    total_rows = 0
    good_rows = 0
    col_counts = []
    headers = None

    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=sep)
            for row in reader:
                if not row or all(cell.strip() == "" for cell in row):
                    continue
                total_rows += 1
                col_counts.append(len(row))
                if headers is None:
                    headers = row
                good_rows += 1
    except Exception as e:
        if total_rows == 0:
            return Grade(tier=3, I=0.0, fmt=fmt, encoding=encoding, error=str(e))
        # 部分成功
        I = good_rows / max(total_rows, 1)
        return Grade(tier=2, I=I, fmt=fmt, encoding=encoding,
                     n_struct=_column_drift(col_counts) if col_counts else 0.0,
                     parsed={"type": fmt, "headers": headers, "good_rows": good_rows})

    if total_rows == 0:
        return Grade(tier=3, I=0.0, fmt=fmt, encoding=encoding, note="empty file")

    # strict: 列数全部一致 → tier1
    ncols = col_counts[0] if col_counts else 0
    if ncols >= 1 and all(c == ncols for c in col_counts):
        return Grade(tier=1, I=1.0, fmt=fmt, encoding=encoding,
                     parsed={"type": fmt, "headers": headers, "rows": good_rows, "columns": ncols})

    # tolerant: 列数有漂移
    I = good_rows / total_rows
    drift = _column_drift(col_counts)
    return Grade(tier=2, I=I, fmt=fmt, encoding=encoding,
                 n_struct=drift,
                 parsed={"type": fmt, "headers": headers, "good_rows": good_rows, "total_rows": total_rows})


def _sniff_delimiter(path: str, encoding: str) -> str:
    """嗅探 CSV 分隔符: 在 , ; | 中选列数最稳定的."""
    candidates = [",", ";", "|"]
    best_sep = ","
    best_stability = float("inf")
    lines_read = 0
    for sep in candidates:
        col_counts = []
        try:
            with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    col_counts.append(line.count(sep) + 1)
                    lines_read += 1
                    if len(col_counts) >= 20:
                        break
        except Exception:
            continue
        if col_counts and len(col_counts) >= 2 and col_counts[0] >= 2:
            sd = stdev(col_counts) if len(col_counts) > 1 else 0.0
            if sd < best_stability:
                best_stability = sd
                best_sep = sep
    return best_sep


def _column_drift(col_counts: list) -> float:
    """列漂移度量: 列数的变异系数."""
    if not col_counts:
        return 0.0
    mean = sum(col_counts) / len(col_counts)
    if mean == 0:
        return 0.0
    variance = sum((c - mean) ** 2 for c in col_counts) / len(col_counts)
    return (variance ** 0.5) / mean

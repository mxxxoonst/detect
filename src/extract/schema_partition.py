"""schema_partition（partition_file）：文件内 Schema 分片。

把单个 tier1 Grade 切成若干 SchemaPartition：
  JSON/JSONL : 显式包装 key 优先，兜底骨架签名聚类
  CSV/TSV    : 整文件单 partition，检测列稳定性
  SQL 文本   : 按 CREATE TABLE / INSERT INTO 的表名分片
  xlsx       : openpyxl 只读，每 sheet 一个 partition
"""

import csv
import json
import re
from collections import defaultdict
from itertools import islice
from statistics import stdev
from typing import Dict, Iterator, List, Optional, Tuple

import ijson

from src.constants import SAMPLE_PER_FILE, SNIFF_HEAD_BYTES
from src.extract.schema_types import PartitionStats, SchemaPartition
from src.extract.skeleton import UnionSchemaClusterer
from src.parse.grade import Grade
from src.parse.json_recovery import (
    looks_like_jsonl_path,
    stream_concatenated_json_records,
    tolerant_json_records,
    tolerant_load_text,
)
from src.utils.logger import get_logger

log = get_logger(__name__)

_INSERT_HEADER_RE = re.compile(
    r"INSERT\s+INTO\s+[`'\"]?(\w+)[`'\"]?\s*(?:\(([^)]+)\))?", re.IGNORECASE
)
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`'\"]?(\w+)[`'\"]?", re.IGNORECASE
)
_SQL_KEYWORDS = {
    "PRIMARY", "KEY", "UNIQUE", "CONSTRAINT", "FOREIGN",
    "INDEX", "FULLTEXT", "CHECK", "PARTITION", "SPATIAL",
}


# ── 公开接口 ──────────────────────────────────────────────────────────────────

def partition_file(grade: Grade) -> Tuple[List[SchemaPartition], PartitionStats]:
    """按文件格式路由到对应分片策略，返回 (partitions, stats)。"""
    fmt = grade.fmt

    if fmt == "json":
        parts, method = _partition_json(grade.path, grade.encoding)
    elif fmt == "jsonl":
        parts, method = _partition_jsonl(grade.path, grade.encoding)
    elif fmt in ("csv", "tsv"):
        parts, method = _partition_csv(grade.path, grade.encoding, fmt)
    elif fmt == "sql":
        parts, method = _partition_sql(grade.path, grade.encoding)
    elif fmt == "xlsx":
        parts, method = _partition_xlsx(grade)
    else:
        parts, method = [], "unknown"

    stats: PartitionStats = {
        "source_file":     grade.path,
        "format":          fmt,
        "partition_count": len(parts),
        "partition_ids":   [p["partition_id"] for p in parts],
        "method":          method,
    }
    log.debug("partition_file %s: fmt=%s method=%s → %d 分片",
              grade.path, fmt, method, len(parts))
    return parts, stats


# ── JSON ──────────────────────────────────────────────────────────────────────

def _partition_json(path: str, encoding: str) -> Tuple[List[SchemaPartition], str]:
    """优先检测显式包装 key，兜底 union-schema 兼容性合并聚类。"""
    explicit = _detect_explicit_keys(path, encoding)
    if explicit:
        parts = []
        for key_name, records in explicit.items():
            if not records:
                continue
            occ = _occurrence_from_records(records)
            parts.append(_make_partition(path, "json", key_name, iter(records), occ))
        if parts:
            return parts, "explicit_key"

    buckets, occ_map = _cluster_by_skeleton_json(path, encoding)
    parts = [
        _make_partition(path, "json", sig_id, iter(records), occ_map.get(sig_id))
        for sig_id, records in buckets.items()
    ]
    return parts, "union_schema"


def _detect_explicit_keys(path: str, encoding: str) -> Dict[str, list] | None:
    """检测顶层是否为 {key→list[dict]} 包装结构。只读 head，大文件不全量 load。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(SNIFF_HEAD_BYTES)
        text = raw.decode(encoding, errors="replace")
    except Exception as e:
        log.debug("显式 key 探测读取失败, 回退骨架聚类 %s: %s", path, e)
        return None

    try:
        # 共享 json_recovery 单一真源 (json 严格 → json5 容错)，与 parse 阶段同源。
        # head 截断时也可能失败 → 回退骨架聚类。
        data = tolerant_load_text(text)
    except Exception as e:
        log.debug("显式 key 探测 json/json5 均失败, 回退骨架聚类 %s: %s", path, e)
        return None

    if not isinstance(data, dict):
        return None

    result = {}
    for key, val in data.items():
        if isinstance(val, list) and val and isinstance(val[0], dict):
            result[key] = [r for r in val if isinstance(r, dict)]

    return result if result else None


def _cluster_by_skeleton_json(
    path: str, encoding: str = "utf-8"
) -> Tuple[Dict[str, list], Dict[str, Dict[str, float]]]:
    """ijson 流式读顶层数组，union-schema 兼容性合并聚类（消除签名爆炸）。

    单遍贪心：每条记录并入第一个兼容原型（扩张并集 + 累加每字段出现计数），否则开
    新原型。归一化只放过「可选键 / null 多态 / 空容器」伪差异，真冲突仍分开。
    内存恒定：聚类器只持 O(原型数) 的并集 schema + 计数；每桶采样落地受
    SAMPLE_PER_FILE 限制。返回 (buckets, occurrence_map)，桶 id 为 ``uni_00`` 序号。

    Returns:
        buckets:        {partition_id: [采样记录,...]}
        occurrence_map: {partition_id: {叶路径: presence-rate}}
    """
    clusterer = UnionSchemaClusterer()
    samples: Dict[int, list] = defaultdict(list)

    for rec in _stream_json_records(path, encoding):
        if not isinstance(rec, dict):
            continue
        idx = clusterer.add(rec)
        if len(samples[idx]) < SAMPLE_PER_FILE:
            samples[idx].append(rec)

    buckets: Dict[str, list] = {}
    occ_map: Dict[str, Dict[str, float]] = {}
    for idx in range(len(clusterer.protos)):
        pid = f"uni_{idx:02d}"
        buckets[pid] = samples.get(idx, [])
        occ_map[pid] = clusterer.occurrence(idx)
    return buckets, occ_map


def _occurrence_from_records(records: list) -> Dict[str, float]:
    """对一组已知同质记录（显式 key 分片）算每叶路径的 presence-rate。"""
    clusterer = UnionSchemaClusterer()
    for rec in records:
        if isinstance(rec, dict):
            clusterer.add(rec)
    if not clusterer.protos:
        return {}
    # 显式 key 下视为单原型；若内部仍有真冲突分出多原型，取第一个（最大族）的近似
    return clusterer.occurrence(0)


def _stream_json_records(path: str, encoding: str = "utf-8") -> Iterator[dict]:
    """流式迭代顶层数组记录，宽容度与 parse 阶段对齐。

    1. 快路径：ijson 二进制流式（UTF-8），干净大文件内存恒定。
    2. 兜底：按探测编码读文本，json 严格 → json5 容错。
       覆盖 GBK 等非 UTF-8 编码 + 注释/单引号/尾逗号等 JSON5 语法，
       与 json_parser._json_tolerant 一致——避免"只靠容错进 tier1"的
       JSON 文件在分片阶段零产出（见 gbk_users / noisy_trailing_comma）。
    """
    yielded = 0
    try:
        with open(path, "rb") as f:
            for item in ijson.items(f, "item"):
                yielded += 1
                yield item
        if yielded:
            return
    except Exception as e:
        log.debug("ijson 流式读失败, 回退容错整体加载 %s: %s", path, e)
        if yielded:
            # 已流式产出部分记录，再走整文件兜底会重复 → 保留已得部分
            log.debug("ijson 崩溃前已产出 %d 条, 跳过兜底以免重复 %s", yielded, path)
            return

    # ── 顶层数组零产出: 先探 JSONL-as-.json (与 parse 阶段 parse_json 同源), 是则逐行迭代 ──
    if looks_like_jsonl_path(path, encoding):
        log.debug("JSON 分片 %s: 顶层数组零记录, 探测为逐行独立对象 → JSONL 迭代", path)
        try:
            with open(path, "r", encoding=encoding, errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    yield rec
        except OSError as e:
            log.warning("JSONL-as-.json 分片读取失败 %s: %s", path, e)
        return

    # ── 流式增量恢复: 拼接的多个顶层值 / 缺外括号的数组体 / pretty-print 多行对象 ──
    # (test2.json 型: `},\n{` 分隔、尾部截断)。逐值 raw_decode, 不整文件 load。
    concat_yielded = 0
    for rec in stream_concatenated_json_records(path, encoding):
        concat_yielded += 1
        yield rec
    if concat_yielded:
        log.debug("JSON 分片 %s: 流式增量恢复出 %d 条 (拼接顶层值)", path, concat_yielded)
        return

    # ── 末路兜底: json 严格 → json5 容错 (GBK / 注释 / 单引号等语法噪声, 小文件) ──
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            text = f.read()
    except Exception as e:
        log.warning("JSON 文本读取失败 %s: %s", path, e)
        return

    yield from tolerant_json_records(text)


# ── JSONL ─────────────────────────────────────────────────────────────────────

def _partition_jsonl(path: str, encoding: str) -> Tuple[List[SchemaPartition], str]:
    """JSONL 无显式包装 key，逐行流式 union-schema 兼容性合并聚类。"""
    clusterer = UnionSchemaClusterer()
    samples: Dict[int, list] = defaultdict(list)
    bad = 0

    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if not isinstance(rec, dict):
                    bad += 1
                    continue
                idx = clusterer.add(rec)
                if len(samples[idx]) < SAMPLE_PER_FILE:
                    samples[idx].append(rec)
    except Exception as e:
        log.warning("JSONL 分片读取失败 %s: %s", path, e)

    if bad:
        log.debug("JSONL 分片 %s: 跳过 %d 个坏行/非 dict 行", path, bad)

    parts = []
    for idx in range(len(clusterer.protos)):
        pid = f"uni_{idx:02d}"
        parts.append(
            _make_partition(path, "jsonl", pid, iter(samples.get(idx, [])),
                            clusterer.occurrence(idx))
        )
    return parts, "union_schema"


# ── CSV / TSV ─────────────────────────────────────────────────────────────────

def _partition_csv(
    path: str, encoding: str, fmt: str
) -> Tuple[List[SchemaPartition], str]:
    """整文件单 partition，含列稳定性检测。"""
    sep = "\t" if fmt == "tsv" else _sniff_sep(path, encoding)
    is_noisy = _check_col_stability(path, encoding, sep)
    if is_noisy:
        log.debug("%s 列不稳定(noisy), 下游将跳过拓扑: %s", fmt.upper(), path)

    def _iter_records():
        # 流式：逐行剥 NUL 喂给 csv.DictReader（多行引号字段由 csv 跨行重组，
        # 不受逐行影响）；islice 到 SAMPLE_PER_FILE，不再整文件 read 进内存。
        try:
            with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
                cleaned_lines = (line.replace("\x00", "") for line in f)
                reader = csv.DictReader(cleaned_lines, delimiter=sep)
                clean_rows = (
                    {
                        (k.lstrip("﻿") if k else k): v
                        for k, v in row.items()
                        if k is not None
                    }
                    for row in reader
                )
                valid_rows = (r for r in clean_rows if len(r) >= 2)
                for clean in islice(valid_rows, SAMPLE_PER_FILE):
                    yield clean
        except Exception as e:
            log.warning("CSV/TSV 流式分桶失败 %s: %s", path, e)
            return

    part = _make_partition(path, fmt, "table", _iter_records())
    part["noisy"] = is_noisy
    return [part], "single"


def _check_col_stability(path: str, encoding: str, sep: str) -> bool:
    """前 20 行列数方差 > 0.5 或首行列数 < 2 则视为不稳定。"""
    col_counts = []
    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                stripped = line.strip()
                if stripped:
                    col_counts.append(stripped.count(sep) + 1)
    except Exception as e:
        log.debug("列稳定性检测失败(默认 not noisy) %s: %s", path, e)
        return False

    if len(col_counts) < 2:
        return False
    return stdev(col_counts) > 0.5 or col_counts[0] < 2


def _sniff_sep(path: str, encoding: str) -> str:
    """在 , ; | 中选列数 > 1 且跨行最稳定的分隔符。"""
    best_sep, best_score = ",", float("inf")
    for sep in (",", ";", "|"):
        col_counts = []
        try:
            with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
                for i, line in enumerate(f):
                    if i >= 20:
                        break
                    stripped = line.strip()
                    if stripped:
                        col_counts.append(stripped.count(sep) + 1)
        except Exception:
            continue
        if col_counts and col_counts[0] >= 2:
            sd = stdev(col_counts) if len(col_counts) > 1 else 0.0
            if sd < best_score:
                best_score, best_sep = sd, sep
    return best_sep


# ── SQL 文本 ──────────────────────────────────────────────────────────────────

def _partition_sql(path: str, encoding: str) -> Tuple[List[SchemaPartition], str]:
    """流式扫描 SQL，按表名分桶；与 parsers/sql_parser.py 策略一致，不落地中间 CSV。"""
    table_schemas: Dict[str, list] = {}
    table_buckets: Dict[str, list] = defaultdict(list)
    current_table: str | None = None
    current_cols: list = []

    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            f_iter = iter(f)
            for line in f_iter:
                line_strip = line.strip()
                if not line_strip or line_strip.startswith(("--", "/*", "#")):
                    continue
                line_upper = line_strip.upper()

                if line_upper.startswith("CREATE TABLE"):
                    m = _CREATE_TABLE_RE.match(line_strip)
                    if m:
                        tname = m.group(1)
                        cols = _parse_create_columns(f_iter)
                        if cols:
                            table_schemas[tname] = cols

                elif line_upper.startswith("INSERT INTO"):
                    m = _INSERT_HEADER_RE.match(line_strip)
                    if m:
                        table: str = m.group(1)
                        current_table = table
                        cols_str = m.group(2) or ""
                        current_cols = (
                            [c.strip().strip("`'\"") for c in cols_str.split(",")]
                            if cols_str
                            else table_schemas.get(table, [])
                        )
                        val_idx = line_upper.find("VALUES")
                        if val_idx != -1:
                            values_part = line_strip[val_idx + 6:].strip()
                            if values_part:
                                _collect_sql_rows_into(
                                    values_part, current_cols,
                                    table_buckets[table],
                                )

                elif current_table and line_strip.startswith("("):
                    _collect_sql_rows_into(
                        line_strip, current_cols, table_buckets[current_table]
                    )

                if line_strip.endswith(";"):
                    current_table = None
                    current_cols = []
    except Exception as e:
        log.warning("SQL 分片扫描失败 %s: %s", path, e)

    parts = [
        _make_partition(path, "sql", tname, iter(records[:SAMPLE_PER_FILE]))
        for tname, records in table_buckets.items()
        if records
    ]
    return parts, "table_name"


def _parse_create_columns(f_iter: Iterator[str]) -> list:
    """消耗 f_iter 直到遇到结束符，提取列名（过滤 SQL 保留字）。"""
    cols = []
    for line in f_iter:
        s = line.strip()
        if s.startswith(")") or (s.endswith(";") and not s.startswith("`")):
            break
        if not s or s.startswith(("--", "/*", "#")):
            continue
        m = re.match(r"^\s*[`'\"]?(\w+)[`'\"]?", s)
        if m:
            col = m.group(1)
            if col.upper() not in _SQL_KEYWORDS:
                cols.append(col)
    return cols


def _collect_sql_rows_into(values_str: str, cols: list, bucket: list) -> None:
    """解析 VALUES 元组追加到 bucket；与 extractor.py._collect_sql_rows 逻辑一致。"""
    content = values_str.strip().rstrip(";")
    for match in re.findall(r"\((.*?)\)(?:,|$)", content, re.DOTALL):
        try:
            csv.field_size_limit(2147483647)
            for row in csv.reader(
                [match], delimiter=",", quotechar="'", skipinitialspace=True
            ):
                if len(bucket) >= SAMPLE_PER_FILE:
                    return
                if cols and len(row) == len(cols):
                    bucket.append(dict(zip(cols, row)))
                elif row:
                    bucket.append({f"col_{i}": v for i, v in enumerate(row)})
        except csv.Error:
            continue


# ── xlsx ──────────────────────────────────────────────────────────────────────

def _partition_xlsx(grade: Grade) -> Tuple[List[SchemaPartition], str]:
    """openpyxl 只读，每 sheet 一个 partition；首行作表头，行 zip 成 dict 采样。"""
    try:
        import openpyxl
    except ImportError as e:
        log.warning("openpyxl 未安装, xlsx 分片跳过 %s: %s", grade.path, e)
        return [], "sheet_name"

    parts = []
    try:
        wb = openpyxl.load_workbook(grade.path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = _xlsx_sheet_rows(ws)
            if rows:
                parts.append(_make_partition(grade.path, "xlsx", ws.title, iter(rows)))
        wb.close()
    except Exception as e:
        log.warning("xlsx 打开/读 sheet 失败 %s: %s", grade.path, e)

    return parts, "sheet_name"


def _xlsx_sheet_rows(ws) -> list:
    """首行作表头，后续行 zip 成 dict，跳过全空行，采样到 SAMPLE_PER_FILE。"""
    rows: list = []
    headers = None
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            headers = [
                str(c) if c is not None else f"col_{j}"
                for j, c in enumerate(row)
            ]
            continue
        if headers is None:
            continue
        rec = {
            (headers[j] if j < len(headers) else f"col_{j}"): val
            for j, val in enumerate(row)
        }
        if any(v is not None and str(v).strip() != "" for v in rec.values()):
            rows.append(rec)
        if len(rows) >= SAMPLE_PER_FILE:
            break
    return rows


# ── 通用工厂 ──────────────────────────────────────────────────────────────────

def _make_partition(
    source_file: str,
    fmt: str,
    partition_id: str,
    record_iter: Iterator[dict],
    occurrence: Optional[Dict[str, float]] = None,
) -> SchemaPartition:
    """构造 SchemaPartition dict。

    ``occurrence``：JSON/JSONL union-schema 聚类已算出的每字段 presence-rate（叶路径
    粒度），由 build_schema_unit 消费回填到 SchemaUnit.fields[path].occurrence/required；
    其它格式留空 dict（build_schema_unit 兜底占位 1.0）。``field_paths`` 仍由
    build_schema_unit 回填。
    """
    return {
        "source_file":  source_file,
        "format":       fmt,
        "partition_id": partition_id,
        "field_paths":  set(),
        "occurrence":   dict(occurrence) if occurrence else {},
        "record_iter":  record_iter,
        "noisy":        False,
    }

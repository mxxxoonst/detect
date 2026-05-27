"""阶段2编排器: extract_five_infos — 仅对 tier1 种子做五类信息提取."""

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List

from src.constants import SAMPLE_PER_FILE
from src.parse.grade import Grade
from src.extract.skeleton import collect_skeletons
from src.extract.vocabulary import build_vocabulary, vocab_stats
from src.extract.value_profile import profile_value, aggregate_profiles
from src.extract.topology import build_topology
from src.extract.pii_seed import detect_pii_seeds

# SQL INSERT 解析用的预编译正则
_INSERT_HEADER_RE = re.compile(
    r"INSERT\s+INTO\s+[`'\"]?(\w+)[`'\"]?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)


def extract_five_infos(tier1_grades: List[Grade]) -> Dict[str, Any]:
    """从 tier1 种子中提取五类信息。

    Returns:
        {
            "skeletons":       Counter,        # 骨架签名 → 计数
            "shape_templates_B": int,           # 唯一形状数
            "field_vocab":     {field: {paths}},
            "naming_templates_A": int,          # 唯一字段名数
            "AB_ratio":        float,           # A/B
            "value_profiles":  {path: aggregate},
            "topology":        {path: {depth, parent, siblings}},
            "pii_seeds":       {path: (level, type)},
        }
    """
    all_records: List[Dict] = []
    all_skeletons: Counter = Counter()

    for grade in tier1_grades:
        records = _iterate_records(grade)
        sampled = _sample(records, SAMPLE_PER_FILE)
        all_records.extend(sampled)
        skeletons = collect_skeletons(sampled)
        all_skeletons.update(skeletons)

    if not all_records:
        return _empty_result()

    # 信息一: 骨架
    shape_templates_B = len(all_skeletons)

    # 信息二: 词汇表
    vocab = build_vocabulary(all_records)
    vstats = vocab_stats(vocab)
    naming_templates_A = vstats["total_fields_A"]

    # 信息三: value 画像
    value_profiles = _build_value_profiles(all_records, vocab)

    # 信息四: 拓扑
    topology = build_topology(all_records)

    # 信息五: PII 种子
    pii_seeds = detect_pii_seeds(all_records, vocab)

    # A/B 比值
    AB_ratio = naming_templates_A / max(shape_templates_B, 1)

    return {
        "skeletons": dict(all_skeletons.most_common(200)),
        "shape_templates_B": shape_templates_B,
        "field_vocab": {k: list(v)[:20] for k, v in vocab.items()},
        "naming_templates_A": naming_templates_A,
        "AB_ratio": round(AB_ratio, 3),
        "value_profiles": value_profiles,
        "topology": topology,
        "pii_seeds": pii_seeds,
        "total_records_sampled": len(all_records),
    }


def _iterate_records(grade: Grade):
    """根据 fmt 迭代 record."""
    fmt = grade.fmt
    parsed = grade.parsed

    if parsed is None:
        return []

    if fmt == "json":
        return _iter_json(grade.path, grade.encoding)

    if fmt == "jsonl":
        return _iter_jsonl(grade.path, grade.encoding)

    if fmt in ("csv", "tsv"):
        return _iter_csv(grade.path, grade.encoding, grade.fmt)

    if fmt == "sqlite":
        return _iter_sqlite(grade.parsed)

    if fmt == "sql":
        return _iter_sql(grade.path, grade.encoding)

    return []


# ──── JSON ────────────────────────────────────────────────────────────────────

def _iter_json(path: str, encoding: str) -> List[Dict]:
    """流式读 JSON：优先 ijson 迭代顶层数组，回退整体加载处理单对象/包装结构。"""
    import ijson
    import json

    records: List[Dict] = []

    # 第一步：ijson 流式迭代顶层数组 [{...}, {...}, ...]
    # 大文件场景下避免整体 load 导致 OOM
    try:
        with open(path, "rb") as f:
            for item in ijson.items(f, "item"):
                if isinstance(item, dict):
                    records.append(item)
                if len(records) >= SAMPLE_PER_FILE:
                    break
        if records:
            return records
    except Exception:
        pass

    # 第二步：整体加载 —— 顶层单 dict，或带包装 key 的结构 {"data": [...]}
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            for item in data[:SAMPLE_PER_FILE]:
                if isinstance(item, dict):
                    records.append(item)
        elif isinstance(data, dict):
            # 检查有无值为对象列表的包装 key，如 {"users": [{...}]}
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    for item in v[:SAMPLE_PER_FILE]:
                        records.append(item)
                    break
            if not records:
                records.append(data)
    except Exception:
        pass

    return records


# ──── JSONL ───────────────────────────────────────────────────────────────────

def _iter_jsonl(path: str, encoding: str) -> List[Dict]:
    """读 JSONL：逐行解析，遇错跳过，达采样上限即停。"""
    import json

    records: List[Dict] = []
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        records.append(obj)
                except json.JSONDecodeError:
                    continue
                if len(records) >= SAMPLE_PER_FILE:
                    break
    except Exception:
        pass
    return records


# ──── CSV / TSV ───────────────────────────────────────────────────────────────

def _read_csv_content(path: str, encoding: str) -> str:
    """读取 CSV 全文，处理 BOM（utf-8-sig）和 NUL 字节（SQL dump 常见）。"""
    # utf-8 类型优先尝试 utf-8-sig，去除 Windows BOM
    primary = "utf-8-sig" if encoding.lower().replace("-", "") in ("utf8",) else encoding
    for enc in (primary, encoding, "utf-8-sig"):
        try:
            with open(path, "r", encoding=enc, errors="replace", newline="") as f:
                content = f.read()
            if "\x00" in content:
                content = content.replace("\x00", "")
            return content
        except Exception:
            continue
    return ""


def _iter_csv(path: str, encoding: str, fmt: str) -> List[Dict]:
    """读 CSV/TSV：处理 BOM、NUL 字节，自动嗅探分隔符，达采样上限即停。"""
    import csv
    import io

    sep = "\t" if fmt == "tsv" else _sniff_csv_sep(path, encoding)

    content = _read_csv_content(path, encoding)
    if not content:
        return []

    records: List[Dict] = []
    try:
        reader = csv.DictReader(io.StringIO(content), delimiter=sep)
        for row in reader:
            # 清理 BOM 残留于首字段名（utf-8-sig 偶发未完全消除）
            clean_row = {
                (k.lstrip("﻿") if k else k): v
                for k, v in row.items()
                if k is not None
            }
            if len(clean_row) >= 2:
                records.append(clean_row)
            if len(records) >= SAMPLE_PER_FILE:
                break
    except Exception:
        pass

    return records


def _sniff_csv_sep(path: str, encoding: str) -> str:
    """在 , ; | 中选列数 > 1 且跨行最稳定的分隔符。"""
    from statistics import stdev

    candidates = [",", ";", "|"]
    best_sep = ","
    best_score = float("inf")
    for sep in candidates:
        col_counts = []
        try:
            with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
                for i, line in enumerate(f):
                    if i >= 20:
                        break
                    line = line.strip()
                    if line:
                        col_counts.append(line.count(sep) + 1)
        except Exception:
            continue
        if col_counts and len(col_counts) >= 2 and col_counts[0] >= 2:
            sd = stdev(col_counts) if len(col_counts) > 1 else 0.0
            if sd < best_score:
                best_score = sd
                best_sep = sep
    return best_sep


# ──── SQLite ──────────────────────────────────────────────────────────────────

def _iter_sqlite(parsed: Dict) -> List[Dict]:
    """从 SQLite parsed 信息中提取表 schema 作为 records（仅元数据级）。"""
    tables = parsed.get("tables", []) if parsed else []
    return [
        {"table_name": t.get("name", ""), "schema": t.get("sql", "")}
        for t in tables
    ]


# ──── SQL 文本 ────────────────────────────────────────────────────────────────

def _iter_sql(path: str, encoding: str) -> List[Dict]:
    """从 SQL 文本的 INSERT INTO VALUES 语句中提取行级数据。

    流式逐行扫描：识别 INSERT 列声明 → 解析 VALUES 元组 → 组装 dict record。
    与 parsers/sql_parser.py 的提取逻辑一致，但不落地中间 CSV 文件。
    """
    records: List[Dict] = []
    current_cols: List[str] = []

    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line in f:
                if len(records) >= SAMPLE_PER_FILE:
                    break

                line_strip = line.strip()
                if not line_strip or line_strip.startswith(("--", "/*", "#")):
                    continue

                line_upper = line_strip.upper()

                if line_upper.startswith("INSERT INTO"):
                    m = _INSERT_HEADER_RE.match(line_strip)
                    if m:
                        cols_str = m.group(2) or ""
                        if cols_str:
                            current_cols = [
                                c.strip().strip("`'\"") for c in cols_str.split(",")
                            ]
                        val_idx = line_upper.find("VALUES")
                        if val_idx != -1:
                            values_part = line_strip[val_idx + 6:].strip()
                            if values_part:
                                _collect_sql_rows(values_part, current_cols, records)

                # VALUES 跨行写法：续行以 ( 开头
                elif current_cols and line_strip.startswith("("):
                    _collect_sql_rows(line_strip, current_cols, records)

                # 语句结束符：重置列声明
                if line_strip.endswith(";"):
                    current_cols = []
    except Exception:
        pass

    return records


def _collect_sql_rows(values_str: str, cols: List[str], records: list) -> None:
    """解析 SQL VALUES 部分的元组，追加到 records。

    用 csv.reader(quotechar="'") 处理 SQL 字符串字面量中的逗号，
    与 parsers/sql_parser.py._parse_values_part() 策略一致。
    """
    import csv

    content = values_str.strip().rstrip(";")
    # re.findall 提取每个 (...) 内容；re.DOTALL 允许值中含换行
    tuple_matches = re.findall(r"\((.*?)\)(?:,|$)", content, re.DOTALL)

    for match in tuple_matches:
        try:
            csv.field_size_limit(2147483647)
            reader = csv.reader(
                [match], delimiter=",", quotechar="'", skipinitialspace=True
            )
            for row in reader:
                if cols and len(row) == len(cols):
                    records.append(dict(zip(cols, row)))
                elif row:
                    # 无列名声明时用位置占位符
                    records.append({f"col_{i}": v for i, v in enumerate(row)})
        except csv.Error:
            continue


# ──── 辅助函数 ────────────────────────────────────────────────────────────────

def _sample(items: list, n: int) -> list:
    """取前 n 条（简单截断，不做随机抽样以保持可复现）。"""
    return items[:n]


def _build_value_profiles(records: List[Dict], vocab: Dict[str, set]) -> Dict[str, Any]:
    """为每个字段路径构建聚合 value 画像。"""
    path_values: Dict[str, list] = defaultdict(list)
    for rec in records:
        _collect_path_values(rec, "", path_values)

    return {
        path: aggregate_profiles([profile_value(v) for v in values])
        for path, values in path_values.items()
    }


def _collect_path_values(node: Any, prefix: str, path_values: Dict[str, list]):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            path_values[path].append(value)
            _collect_path_values(value, path, path_values)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            path = f"{prefix}[{i}]"
            if isinstance(item, (dict, list)):
                _collect_path_values(item, path, path_values)


def _empty_result() -> Dict[str, Any]:
    return {
        "skeletons": {},
        "shape_templates_B": 0,
        "field_vocab": {},
        "naming_templates_A": 0,
        "AB_ratio": 0.0,
        "value_profiles": {},
        "topology": {},
        "pii_seeds": {},
        "total_records_sampled": 0,
    }

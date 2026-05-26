"""阶段2编排器: extract_five_infos — 仅对 tier1 种子做五类信息提取."""

from collections import Counter, defaultdict
from typing import Any, Dict, List

from src.constants import SAMPLE_PER_FILE
from src.parse.grade import Grade
from src.extract.skeleton import collect_skeletons
from src.extract.vocabulary import build_vocabulary, vocab_stats
from src.extract.value_profile import profile_value, aggregate_profiles
from src.extract.topology import build_topology
from src.extract.pii_seed import detect_pii_seeds


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
        return []  # SQL 文本暂不提取 record 级信息

    return []


def _iter_json(path: str, encoding: str) -> List[Dict]:
    """读 JSON 文件, 返回顶层 records."""
    import json
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []
    except Exception:
        return []


def _iter_jsonl(path: str, encoding: str) -> List[Dict]:
    """读 JSONL 文件."""
    import json
    records = []
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass
    return records


def _iter_csv(path: str, encoding: str, fmt: str) -> List[Dict]:
    """读 CSV/TSV 文件, 自动嗅探分隔符."""
    import csv

    if fmt == "tsv":
        sep = "\t"
    else:
        sep = _sniff_csv_sep(path, encoding)

    records = []
    try:
        with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
            reader = csv.DictReader(f, delimiter=sep)
            for row in reader:
                # 过滤因分隔符错误产生的单列畸形行
                if len(row) >= 2:
                    records.append(dict(row))
    except Exception:
        pass
    return records


def _sniff_csv_sep(path: str, encoding: str) -> str:
    """在 , ; | 中选列数 > 1 且最稳定的分隔符."""
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


def _iter_sqlite(parsed: Dict) -> List[Dict]:
    """从 SQLite parsed 信息中提取表 schema 作为 records."""
    tables = parsed.get("tables", []) if parsed else []
    records = []
    for t in tables:
        records.append({
            "table_name": t.get("name", ""),
            "schema": t.get("sql", ""),
        })
    return records


def _sample(items: list, n: int) -> list:
    """取前 n 条 (简单截断, 不做随机抽样以保持可复现)."""
    return items[:n]


def _build_value_profiles(records: List[Dict], vocab: Dict[str, set]) -> Dict[str, Any]:
    """为每个字段路径构建聚合 value 画像."""
    # 按 path 收集 values
    path_values: Dict[str, list] = defaultdict(list)
    for rec in records:
        _collect_path_values(rec, "", path_values)

    profiles = {}
    for path, values in path_values.items():
        per_value = [profile_value(v) for v in values]
        profiles[path] = aggregate_profiles(per_value)
    return profiles


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

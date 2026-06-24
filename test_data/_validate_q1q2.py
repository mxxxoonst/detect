"""可复现验证脚本：对**生产代码路径**（src/extract）跑 Q1 union-schema 合并与 Q2
CSV schema 去重，输出对比表。区别于同目录 _validate_part.py（脚手架替身，用旧精确
签名作对照）——本脚本直接调 schema_partition.partition_file / schema_dedup.dedup_csv_schemas，
是生产逻辑的实测。

用法（本地小样本，ijson 已装）：
    uv run python test_data/_validate_q1q2.py
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.extract.schema_partition import partition_file          # noqa: E402
from src.extract.schema_dedup import dedup_csv_schemas           # noqa: E402
from src.parse.grade import Grade                                # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def _grade(path, fmt="json"):
    return Grade(tier=1, I=1.0, fmt=fmt, encoding="utf-8", path=path)


def q1():
    print("=" * 70)
    print("Q1  JSON union-schema 合并（生产路径 partition_file）")
    print("=" * 70)
    print(f"  {'文件':16s} {'分片':>6s}  {'采样':>6s}  {'字段':>5s}  {'可选(<1.0)':>10s}")
    for fn in sorted(glob.glob(os.path.join(ROOT, "DataPart", "*.json"))):
        parts, stats = partition_file(_grade(fn))
        sizes = [len(list(p["record_iter"])) for p in parts]
        occ = parts[0]["occurrence"] if parts else {}
        opt = sum(1 for v in occ.values() if v < 1.0)
        print(f"  {os.path.basename(fn):16s} {len(parts):6d}  "
              f"{(max(sizes) if sizes else 0):6d}  {len(occ):5d}  {opt:10d}")


def q2():
    print()
    print("=" * 70)
    print("Q2  CSV schema 去重（生产路径 dedup_csv_schemas）")
    print("=" * 70)
    # 优先用合成可复现样本；若 csv_tests 真实语料存在也一并跑
    for label, pattern in [
        ("csv_dedup_samples（合成可复现）", os.path.join(ROOT, "csv_dedup_samples", "*.csv")),
        ("csv_tests（真实语料，若存在）", os.path.join(ROOT, "..", "csv_tests", "*.csv")),
    ]:
        files = sorted(glob.glob(pattern))
        if not files:
            continue
        units = [
            {"id": f"sch_{i:05d}", "source_file": p, "format": "csv"}
            for i, p in enumerate(files)
        ]
        rep = dedup_csv_schemas(units)
        print(f"\n  [{label}]")
        print(f"  文件 {rep['total_csv_units']}  →  精确指纹桶 {rep['exact_buckets']}  "
              f"→  distinct schema {rep['distinct_schemas']}")
        for c in rep["clusters"]:
            rep_name = os.path.basename(c["representative_file"])
            extra = f"  (+{c['cluster_size'] - 1} 重)" if c["cluster_size"] > 1 else ""
            print(f"    x{c['cluster_size']:2d} hdr={str(c['has_header']):5s} "
                  f"代表={rep_name}{extra}")


if __name__ == "__main__":
    q1()
    q2()

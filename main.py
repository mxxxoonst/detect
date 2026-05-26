"""PII Detect — 数据预处理和信息提取流水线 CLI.

用法:
  uv run python main.py sniff   <corpus_root>        # 阶段0: 内容嗅探
  uv run python main.py parse   <corpus_root>        # 阶段1: 容错分级解析
  uv run python main.py extract <corpus_root>        # 阶段2: 五类信息提取
  uv run python main.py pipeline <corpus_root>        # 三个阶段串联
"""

import argparse
import json
import os
import sys
import time

from src.sniff.profiler import profile_corpus
from src.sniff.sniffer import sniff_file
from src.parse.grade import grade_parse
from src.extract.extractor import extract_five_infos
from src.utils.file_utils import walk_files


def cmd_sniff(args):
    """阶段0: 产出交叉表."""
    print(f"[阶段0] 内容嗅探: {args.root}")
    start = time.time()
    result = profile_corpus(args.root)
    elapsed = time.time() - start

    _print_sniff_report(result, elapsed)
    _save_output(result, "sniff_report.json", args.output_dir)


def cmd_parse(args):
    """阶段1: 容错分级解析."""
    print(f"[阶段1] 容错分级解析: {args.root}")
    start = time.time()

    tier1, tier2, tier3, noise, free_text = [], [], [], [], []
    errors = []

    files = list(walk_files(args.root))
    total = len(files)
    for i, path in enumerate(files):
        fmt, enc, conf = sniff_file(path)
        grade = grade_parse(path, fmt, enc)

        if grade.tier == 1:
            tier1.append(_grade_summary(grade))
        elif grade.tier == 2:
            tier2.append(_grade_summary(grade))
        elif grade.tier == 3:
            tier3.append(_grade_summary(grade))
        elif grade.tier == "noise_sample":
            noise.append(_grade_summary(grade))
        elif grade.tier == "free_text":
            free_text.append(_grade_summary(grade))

        if grade.error:
            errors.append({"path": path, "error": grade.error})

        if (i + 1) % 100 == 0:
            print(f"  进度: {i+1}/{total}")

    elapsed = time.time() - start
    report = {
        "tier1_clean_seeds": len(tier1),
        "tier2_noisy": len(tier2),
        "tier3_unparseable": len(tier3),
        "noise_sample_log": len(noise),
        "free_text": len(free_text),
        "total": total,
        "elapsed_s": round(elapsed, 1),
        "tier1_files": tier1[:200],
        "tier2_files": tier2[:200],
        "noise_files": noise[:200],
        "free_text_files": free_text[:200],
        "errors": errors[:100],
    }

    _print_parse_report(report)
    _save_output(report, "parse_report.json", args.output_dir)


def cmd_extract(args):
    """阶段2: 五类信息提取."""
    print(f"[阶段2] 五类信息提取: {args.root}")
    start = time.time()

    # 先收集 tier1 种子
    tier1_grades = []
    for path in walk_files(args.root):
        fmt, enc, conf = sniff_file(path)
        grade = grade_parse(path, fmt, enc)
        if grade.tier == 1:
            tier1_grades.append(grade)

    print(f"  tier1 种子数: {len(tier1_grades)}")
    result = extract_five_infos(tier1_grades)
    elapsed = time.time() - start
    result["elapsed_s"] = round(elapsed, 1)

    _print_extract_report(result)
    _save_output(result, "extract_report.json", args.output_dir)


def cmd_pipeline(args):
    """三阶段全流水线."""
    print(f"[流水线] 全阶段执行: {args.root}")
    print("=" * 60)

    # 阶段0
    print("\n── 阶段0: 内容嗅探 ──")
    t0 = time.time()
    sniff_result = profile_corpus(args.root)
    _print_sniff_report(sniff_result, time.time() - t0)

    # 阶段1
    print("\n── 阶段1: 容错分级解析 ──")
    t1 = time.time()
    tier1_grades, tier2_grades = [], []
    noise, free_text_grades = [], []
    for path in walk_files(args.root):
        fmt, enc, conf = sniff_file(path)
        grade = grade_parse(path, fmt, enc)
        if grade.tier == 1:
            tier1_grades.append(grade)
        elif grade.tier == 2:
            tier2_grades.append(grade)
        elif grade.tier == "noise_sample":
            noise.append(grade)
        elif grade.tier == "free_text":
            free_text_grades.append(grade)

    print(f"  tier1: {len(tier1_grades)} | tier2: {len(tier2_grades)} | "
          f"noise: {len(noise)} | free_text: {len(free_text_grades)} | "
          f"耗时: {time.time()-t1:.1f}s")

    # 阶段2
    print("\n── 阶段2: 五类信息提取 ──")
    t2 = time.time()
    if tier1_grades:
        extract_result = extract_five_infos(tier1_grades)
        _print_extract_report(extract_result)
    else:
        extract_result = {"note": "无 tier1 种子, 跳过提取"}
        print("  无 tier1 种子可提取")
    extract_result["elapsed_s"] = round(time.time() - t2, 1)

    # 汇总
    total_elapsed = time.time() - t0
    pipeline_result = {
        "phase0": {
            "cross_table": sniff_result["cross_table"],
            "format_dist": sniff_result["format_dist"],
            "low_confidence_count": len(sniff_result["low_confidence"]),
        },
        "phase1": {
            "tier1_count": len(tier1_grades),
            "tier2_count": len(tier2_grades),
            "noise_count": len(noise),
            "free_text_count": len(free_text_grades),
        },
        "phase2": extract_result,
        "total_elapsed_s": round(total_elapsed, 1),
    }

    _save_output(pipeline_result, "pipeline_report.json", args.output_dir)
    print(f"\n全流水线完成, 总耗时: {total_elapsed:.1f}s")


# ════════════════════════════════════════════════════════════════
# 输出辅助
# ════════════════════════════════════════════════════════════════


def _print_sniff_report(result: dict, elapsed: float):
    fmt_dist = result["format_dist"]
    print(f"  文件总数: {result['total_files']}")
    print(f"  格式分布:")
    for fmt, cnt in sorted(fmt_dist.items(), key=lambda x: -x[1]):
        pct = cnt / max(result["total_files"], 1) * 100
        print(f"    {fmt:20s}: {cnt:5d}  ({pct:5.1f}%)")
    print(f"  低置信样本: {len(result['low_confidence'])}")
    print(f"  耗时: {elapsed:.1f}s")


def _print_parse_report(report: dict):
    print(f"  文件总数: {report['total']}")
    print(f"  tier1 (干净种子):    {report['tier1_clean_seeds']}")
    print(f"  tier2 (可恢复噪声):  {report['tier2_noisy']}")
    print(f"  tier3 (不可解析):    {report['tier3_unparseable']}")
    print(f"  noise_sample (日志): {report['noise_sample_log']}")
    print(f"  free_text:           {report['free_text']}")
    print(f"  耗时: {report['elapsed_s']}s")


def _print_extract_report(result: dict):
    print(f"  Records sampled:     {result.get('total_records_sampled', 0)}")
    print(f"  形状模板数 (B):      {result.get('shape_templates_B', 0)}")
    print(f"  命名模板数 (A):      {result.get('naming_templates_A', 0)}")
    print(f"  A/B 比值:            {result.get('AB_ratio', 0)}")
    print(f"  PII 种子:            {len(result.get('pii_seeds', {}))}")
    print(f"  耗时: {result.get('elapsed_s', 0)}s")


def _save_output(data, filename: str, output_dir: str):
    """保存 JSON 结果到文件."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"  输出: {path}")


def _grade_summary(grade):
    return {
        "path": grade.path,
        "fmt": grade.fmt,
        "tier": grade.tier,
        "I": grade.I,
        "encoding": grade.encoding,
        "error": grade.error,
        "n_form": grade.n_form,
        "n_struct": grade.n_struct,
        "note": grade.note,
    }


# ════════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="PII Detect — 数据预处理和信息提取流水线"
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # sniff
    p_sniff = sub.add_parser("sniff", help="阶段0: 内容嗅探 → 交叉表")
    p_sniff.add_argument("root", help="语料库根目录")
    p_sniff.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")

    # parse
    p_parse = sub.add_parser("parse", help="阶段1: 容错分级解析")
    p_parse.add_argument("root", help="语料库根目录")
    p_parse.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")

    # extract
    p_extract = sub.add_parser("extract", help="阶段2: 五类信息提取")
    p_extract.add_argument("root", help="语料库根目录")
    p_extract.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")

    # pipeline
    p_pipe = sub.add_parser("pipeline", help="三阶段全流水线")
    p_pipe.add_argument("root", help="语料库根目录")
    p_pipe.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")

    args = parser.parse_args()

    if args.command == "sniff":
        cmd_sniff(args)
    elif args.command == "parse":
        cmd_parse(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "pipeline":
        cmd_pipeline(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

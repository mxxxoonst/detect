"""PII Detect — 数据预处理和信息提取流水线 CLI.

用法:
  uv run python main.py sniff   <corpus_root>        # 阶段0: 内容嗅探
  uv run python main.py parse   <corpus_root>        # 阶段1: 容错分级解析
  uv run python main.py extract <corpus_root>        # 阶段2: 五类信息提取
  uv run python main.py pipeline <corpus_root>        # 三个阶段串联
"""

import argparse
import json
import time
from pathlib import Path

from src.sniff.profiler import profile_corpus
from src.sniff.sniffer import sniff_file
from src.parse.grade import grade_parse
from src.extract.extractor import extract_all
from src.utils.file_utils import walk_files
from src.utils.logger import setup_logger


def cmd_sniff(args):
    """阶段0: 产出交叉表."""
    log = setup_logger("sniff", file=args.output_file)
    paths = resolve_input(args.root, log)

    if not paths:
        log.error("未找到可嗅探的文件, 跳过阶段0")
        return

    log.info("[阶段0] 内容嗅探: %s (文件数: %d)", args.root, len(paths))
    start = time.time()
    result = profile_corpus(args.root, files=paths)
    elapsed = time.time() - start

    _print_sniff_report(result, elapsed)
    _save_output(result, "sniff_report.json", args.output_dir)
    log.info("阶段0 完成")


def resolve_input(root: str, log) -> list:
    """宽限输入解析: 支持文件/目录, 目录递归查找文件.

    - 如果是文件, 直接返回 [path]
    - 如果是目录, 递归遍历所有文件
    - 如果目录为空(无任何文件), 向上递归父目录继续查找
    - 如果仍无文件, 逐层检查祖父目录, 直到根目录
    - 若全程无文件, 返回空列表并记录警告

    Returns:
        找到的文件路径列表
    """
    p = Path(root).resolve()

    # 单文件: 直接返回
    if p.is_file():
        log.debug("输入为单文件: %s", p)
        return [str(p)]

    if not p.is_dir():
        log.error("输入路径不存在: %s", p)
        return []

    # 目录: 递归查找所有文件
    files = list(walk_files(p))
    if files:
        log.debug("目录 %s 下找到 %d 个文件", p, len(files))
        return files

    # 目录为空: 向上查找父目录, 直到文件系统根
    log.warning("目录 %s 下无文件, 向上查找父目录 ...", p)
    current = p
    while current != current.parent:  # 未到文件系统根
        parent = current.parent
        parent_files = list(walk_files(parent))
        if parent_files:
            log.info("在父目录 %s 中找到 %d 个文件", parent, len(parent_files))
            return parent_files
        log.warning("父目录 %s 也无文件, 继续向上 ...", parent)
        current = parent

    log.error("全程未找到任何文件")
    return []


def cmd_parse(args):
    """阶段1: 容错分级解析."""
    log = setup_logger("parse", file=args.output_file)
    files = resolve_input(args.root, log)

    if not files:
        log.error("未找到可解析的文件, 跳过阶段1")
        return

    log.info("[阶段1] 容错分级解析: %s (文件数: %d)", args.root, len(files))
    start = time.time()

    tier1, tier2, tier3, noise, free_text = [], [], [], [], []
    errors = []

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
            log.info("  进度: %d/%d", i+1, total)

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
    log = setup_logger("extract", file=args.output_file)
    files = resolve_input(args.root, log)

    if not files:
        log.error("未找到可提取的文件, 跳过阶段2")
        return

    log.info("[阶段2] 五类信息提取: %s (文件数: %d)", args.root, len(files))
    start = time.time()

    # 先收集 tier1 种子
    tier1_grades = []
    for path in files:
        fmt, enc, conf = sniff_file(path)
        grade = grade_parse(path, fmt, enc)
        if grade.tier == 1:
            tier1_grades.append(grade)

    log.info("  tier1 种子数: %d", len(tier1_grades))
    field_mode = getattr(args, "field_mode", "template")
    log.info("  字段主干方案: %s", field_mode)
    schema_units, vocab_table, global_view = extract_all(tier1_grades, mode=field_mode)
    elapsed = time.time() - start
    global_view["elapsed_s"] = round(elapsed, 1)

    _print_extract_report(global_view)
    _save_output(global_view, "extract_report.json", args.output_dir)
    _save_output(schema_units, "schema_units.json", args.output_dir)
    _save_output(
        {"vocab_table": vocab_table, "uncertain": global_view.get("uncertain_vocab", [])},
        "vocab_table.json",
        args.output_dir,
    )


def cmd_pipeline(args):
    """三阶段全流水线."""
    log = setup_logger("pipeline", file=args.output_file)

    # 宽限输入解析
    sniff_files = resolve_input(args.root, log)
    if not sniff_files:
        log.error("未找到可处理的文件, 流水线终止")
        return

    log.info("[流水线] 全阶段执行: %s (文件数: %d)", args.root, len(sniff_files))
    print("=" * 60)

    # 阶段0
    print("\n── 阶段0: 内容嗅探 ──")
    t0 = time.time()
    sniff_result = profile_corpus(args.root, files=sniff_files)
    _print_sniff_report(sniff_result, time.time() - t0)
    log.info("阶段0 完成")

    # 阶段1
    print("\n── 阶段1: 容错分级解析 ──")
    t1 = time.time()
    tier1_grades, tier2_grades = [], []
    noise, free_text_grades = [], []
    for path in sniff_files:
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

    log.info(
        "  tier1: %d | tier2: %d | noise: %d | free_text: %d | 耗时: %.1fs",
        len(tier1_grades), len(tier2_grades),
        len(noise), len(free_text_grades), time.time()-t1,
    )

    # 阶段2
    print("\n── 阶段2: 五类信息提取 ──")
    t2 = time.time()
    if tier1_grades:
        field_mode = getattr(args, "field_mode", "template")
        log.info("  字段主干方案: %s", field_mode)
        schema_units, vocab_table, extract_result = extract_all(tier1_grades, mode=field_mode)
        _print_extract_report(extract_result)
    else:
        schema_units, vocab_table = [], {}
        extract_result = {"note": "无 tier1 种子, 跳过提取"}
        log.warning("无 tier1 种子可提取")
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
        "phase2": {
            "global_view": extract_result,
            "schema_unit_count": len(schema_units),
            "vocab_class_count": len(vocab_table),
        },
        "total_elapsed_s": round(total_elapsed, 1),
    }

    _save_output(schema_units, "schema_units.json", args.output_dir)
    _save_output(
        {"vocab_table": vocab_table, "uncertain": extract_result.get("uncertain_vocab", [])},
        "vocab_table.json",
        args.output_dir,
    )

    _save_output(pipeline_result, "pipeline_report.json", args.output_dir)
    log.info("全流水线完成, 总耗时: %.1fs", total_elapsed)


# ════════════════════════════════════════════════════════════════
# 输出辅助
# ════════════════════════════════════════════════════════════════


def _print_sniff_report(result: dict, elapsed: float):
    fmt_dist = result["format_dist"]
    print(f"  文件总数: {result['total_files']}")
    print("  格式分布:")
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
    print(f"  PII 种子字段数:      {result.get('pii_seeds_count', 0)}")
    print(f"  不确定词汇对:        {len(result.get('uncertain_vocab', []))}")
    if result.get("partition_stats"):
        print(f"  分片数:              {sum(s.get('partition_count', 0) for s in result['partition_stats'])}")
    print(f"  耗时: {result.get('elapsed_s', 0)}s")


def _save_output(data, filename: str, output_dir: str):
    """保存 JSON 结果到文件."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
    p_sniff.add_argument("root", help="语料库根目录/文件")
    p_sniff.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")
    p_sniff.add_argument("-f", "--output-file", default=None, help="日志文件路径 (默认: <output-dir>/sniff.log)")

    # parse
    p_parse = sub.add_parser("parse", help="阶段1: 容错分级解析")
    p_parse.add_argument("root", help="语料库根目录/文件")
    p_parse.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")
    p_parse.add_argument("-f", "--output-file", default=None, help="日志文件路径 (默认: <output-dir>/parse.log)")

    # extract
    p_extract = sub.add_parser("extract", help="阶段2: 五类信息提取")
    p_extract.add_argument("root", help="语料库根目录/文件")
    p_extract.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")
    p_extract.add_argument("-f", "--output-file", default=None, help="日志文件路径 (默认: <output-dir>/extract.log)")
    p_extract.add_argument("--field-mode", choices=["template", "fold"], default="template",
                           help="字段主干方案: template(B, 默认, 裁剪到主导签名) / fold(A, 全路径并集)")

    # pipeline
    p_pipe = sub.add_parser("pipeline", help="三阶段全流水线")
    p_pipe.add_argument("root", help="语料库根目录/文件")
    p_pipe.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")
    p_pipe.add_argument("-f", "--output-file", default=None, help="日志文件路径 (默认: <output-dir>/pipeline.log)")
    p_pipe.add_argument("--field-mode", choices=["template", "fold"], default="template",
                        help="字段主干方案: template(B, 默认) / fold(A, 全路径并集)")

    args = parser.parse_args()

    # 默认日志文件: 未显式指定时使用 <output-dir>/<command>.log
    if args.command and args.output_file is None:
        args.output_file = str(Path(args.output_dir) / f"{args.command}.log")

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

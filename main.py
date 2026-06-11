"""PII Detect — 数据预处理和信息提取流水线 CLI.

用法:
  uv run python main.py sniff   <corpus_root>        # 阶段0: 内容嗅探
  uv run python main.py parse   <corpus_root>        # 阶段1: 容错分级解析
  uv run python main.py extract <corpus_root>        # 阶段2: 五类信息提取
  uv run python main.py pipeline <corpus_root>        # 三个阶段串联
"""

import argparse
import json
import logging
import time
from collections import Counter, defaultdict
from pathlib import Path

from src.constants import LOW_CONF_THRESHOLD
from src.sniff.profiler import profile_corpus
from src.sniff.sniffer import sniff_file
from src.parse.grade import grade_parse, grade_from_summary
from src.extract.extractor import stream_schema_units, finalize_from_units
from src.extract.schema_unit import set_unit_counter
from src.utils.file_utils import walk_files, extension
from src.utils.jsonl import append_jsonl, iter_jsonl, count_lines
from src.utils.logger import setup_logger, get_logger

# 面向命令编排层的 logger，挂在 pii_detect 根命名空间下，
# 与各子模块的 get_logger(__name__) 共享同一组 handler（终端 + 日志文件）。
log = get_logger("main")


def _configure_logging(args) -> None:
    """按 --verbose 配置 pii_detect 根 logger 的级别与日志文件 handler。

    所有子模块 get_logger(__name__) 向上 propagate 到 pii_detect，
    因此只需在此处统一挂 file handler 即可收齐全流水线日志。
    """
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    setup_logger("pii_detect", level=level, file=args.output_file)


def cmd_sniff(args):
    """阶段0: 产出交叉表."""
    _configure_logging(args)
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


_TIER_KEY = {
    1: "tier1", 2: "tier2", 3: "tier3",
    "noise_sample": "noise", "free_text": "free_text",
}


def _tier_key(tier) -> str:
    return _TIER_KEY.get(tier, "other")


def _stream_grades(files: list, grades_path: Path, restart: bool) -> dict:
    """阶段1 流式: 逐文件 sniff+grade，**即时追加** grades.jsonl（内存恒定 O(1)）。

    断点续跑：扫描已写行得到 done 集，跳过已处理文件；崩溃残留的截断尾行由 iter_jsonl 容错跳过。
    返回 tier 计数 / 每类样本(capped 200) / 错误(capped 100) 等供报表用。
    """
    grades_path = Path(grades_path)
    grades_path.parent.mkdir(parents=True, exist_ok=True)
    if restart and grades_path.exists():
        grades_path.unlink()
        log.info("--restart: 已清空 %s", grades_path.name)

    counts: Counter = Counter()
    samples: dict = defaultdict(list)
    errors: list = []
    done: set = set()
    for d in iter_jsonl(grades_path):          # 续跑：重建计数/样本/done 集
        done.add(d.get("path"))
        tk = _tier_key(d.get("tier"))
        counts[tk] += 1
        if len(samples[tk]) < 200:
            samples[tk].append(d)
        if d.get("error") and len(errors) < 100:
            errors.append({"path": d.get("path"), "error": d.get("error")})
    if done:
        log.info("续跑: grades.jsonl 已有 %d 文件, 本次跳过", len(done))

    total = len(files)
    new = 0
    for path in files:
        if path in done:
            continue
        fmt, enc, conf = sniff_file(path)
        grade = grade_parse(path, fmt, enc)
        line = _grade_summary(grade)
        line["conf"] = round(conf, 4)
        append_jsonl(grades_path, line)
        new += 1
        tk = _tier_key(grade.tier)
        counts[tk] += 1
        if len(samples[tk]) < 200:
            samples[tk].append(line)
        if grade.error and len(errors) < 100:
            errors.append({"path": path, "error": grade.error})
        if grade.tier == 3:
            log.warning("tier3 不可解析: %s (fmt=%s, error=%s)", path, grade.fmt, grade.error)
        if new % 500 == 0:
            log.info("  阶段1 进度: 新处理 %d / 共 %d (已跳过 %d)", new, total, len(done))

    log.info("阶段1 完成: 新处理 %d, 跳过 %d, 累计 %d", new, len(done), new + len(done))
    return {"counts": counts, "samples": samples, "errors": errors, "total": total}


def cmd_parse(args):
    """阶段1: 容错分级解析（流式落盘 grades.jsonl，可断点续跑）。"""
    _configure_logging(args)
    files = resolve_input(args.root, log)

    if not files:
        log.error("未找到可解析的文件, 跳过阶段1")
        return

    log.info("[阶段1] 容错分级解析: %s (文件数: %d)", args.root, len(files))
    start = time.time()

    grades_path = Path(args.output_dir) / "grades.jsonl"
    stats = _stream_grades(files, grades_path, args.restart)
    c = stats["counts"]

    report = {
        "tier1_clean_seeds": c["tier1"],
        "tier2_noisy": c["tier2"],
        "tier3_unparseable": c["tier3"],
        "noise_sample_log": c["noise"],
        "free_text": c["free_text"],
        "total": stats["total"],
        "elapsed_s": round(time.time() - start, 1),
        "tier1_files": stats["samples"]["tier1"][:200],
        "tier2_files": stats["samples"]["tier2"][:200],
        "noise_files": stats["samples"]["noise"][:200],
        "free_text_files": stats["samples"]["free_text"][:200],
        "errors": stats["errors"][:100],
        "grades_jsonl": str(grades_path),
    }

    _print_parse_report(report)
    _save_output(report, "parse_report.json", args.output_dir)


def _unit_seq(uid) -> int:
    """从 'sch_00042' 取序号 42（续跑时确定 ID 计数器续接点）。"""
    if not uid:
        return 0
    try:
        return int(str(uid).split("_")[-1])
    except ValueError:
        return 0


def _sample_mode_from_args(args) -> str:
    """从 CLI 标志推导信息三样本保留方案 (默认 off, 守 PII 红线)。

    --keep-samples 关 → "off"；开 + --mask-samples → "masked"；开且不打码 → "raw"。
    """
    if not getattr(args, "keep_samples", False):
        return "off"
    return "masked" if getattr(args, "mask_samples", False) else "raw"


def _run_extract_stream(grades_path: Path, output_dir: str, field_mode: str, restart: bool,
                        sample_mode: str = "off"):
    """阶段2 流式: 读 grades.jsonl(tier1) → 逐文件分片+构建 → 追加 schema_units.jsonl，
    再两遍流式聚合 global_view + vocab_table。内存恒定 O(单文件)，可断点续跑。
    """
    units_path = Path(output_dir) / "schema_units.jsonl"
    units_path.parent.mkdir(parents=True, exist_ok=True)
    if restart and units_path.exists():
        units_path.unlink()
        log.info("--restart: 已清空 %s", units_path.name)

    # 续跑：已处理源文件集 + ID 计数器续接（避免 sch_NNNNN 冲突）
    done_src: set = set()
    max_seq = 0
    for u in iter_jsonl(units_path):
        done_src.add(u.get("source_file"))
        max_seq = max(max_seq, _unit_seq(u.get("id")))
    if done_src:
        set_unit_counter(max_seq + 1)
        log.info("续跑: schema_units.jsonl 已有 %d 源文件(max id seq=%d), 跳过并续接 ID",
                 len(done_src), max_seq)

    log.info("  字段主干方案: %s | 样本保留: %s", field_mode, sample_mode)
    grades_iter = (
        grade_from_summary(d) for d in iter_jsonl(grades_path) if d.get("tier") == 1
    )
    processed, pc, written, skipped = stream_schema_units(
        grades_iter, field_mode, lambda u: append_jsonl(units_path, u), done_src,
        sample_mode=sample_mode,
    )
    log.info("  阶段2 Pass1 完成: 处理 %d 文件(跳过 %d), 新写出 %d unit", processed, skipped, written)

    # Pass2: 两遍流式读 units → 聚合 + 词表
    vocab_table, global_view = finalize_from_units(lambda: iter_jsonl(units_path))
    global_view["partition_total"] = count_lines(units_path)
    return vocab_table, global_view, units_path


def cmd_extract(args):
    """阶段2: 五类信息提取（流式落盘 schema_units.jsonl，可断点续跑）。"""
    _configure_logging(args)
    files = resolve_input(args.root, log)

    if not files:
        log.error("未找到可提取的文件, 跳过阶段2")
        return

    log.info("[阶段2] 五类信息提取: %s (文件数: %d)", args.root, len(files))
    start = time.time()

    grades_path = Path(args.output_dir) / "grades.jsonl"
    _stream_grades(files, grades_path, args.restart)      # 阶段1：确保 grades.jsonl 就绪（可续跑）

    field_mode = getattr(args, "field_mode", "template")
    vocab_table, global_view, units_path = _run_extract_stream(
        grades_path, args.output_dir, field_mode, args.restart,
        sample_mode=_sample_mode_from_args(args),
    )
    global_view["elapsed_s"] = round(time.time() - start, 1)

    _print_extract_report(global_view)
    _save_output(global_view, "extract_report.json", args.output_dir)
    _save_output(
        {"vocab_table": vocab_table, "uncertain": global_view.get("uncertain_vocab", [])},
        "vocab_table.json",
        args.output_dir,
    )
    log.info("  schema_units 已流式写入: %s", units_path)


def _phase0_from_grades(grades_path: Path) -> dict:
    """从 grades.jsonl 派生阶段0 交叉表/分布/低置信（复用阶段1 已写的 fmt/conf，省去重复嗅探）。"""
    cross: Counter = Counter()
    dist: Counter = Counter()
    low: list = []
    for d in iter_jsonl(grades_path):
        fmt = d.get("fmt", "?")
        ext = extension(d.get("path", ""))
        cross[f"{ext}|{fmt}"] += 1
        dist[fmt] += 1
        conf = d.get("conf")
        if conf is not None and conf < LOW_CONF_THRESHOLD and len(low) < 200:
            low.append((d.get("path"), fmt, conf))
    return {
        "cross_table": dict(cross),
        "format_dist": dict(dist),
        "low_confidence": low,
        "total_files": sum(dist.values()),
    }


def cmd_pipeline(args):
    """三阶段全流水线（全程流式落盘，可断点续跑）。

    阶段0+1 合并为一遍 sniff+grade 落盘 grades.jsonl，phase0 交叉表从 grades.jsonl 派生
    （省去原先 profile_corpus 的额外整轮嗅探）；阶段2 流式消费并落盘 schema_units.jsonl。
    """
    _configure_logging(args)

    files = resolve_input(args.root, log)
    if not files:
        log.error("未找到可处理的文件, 流水线终止")
        return

    log.info("[流水线] 全阶段执行: %s (文件数: %d)", args.root, len(files))
    log.info("=" * 60)
    t0 = time.time()
    grades_path = Path(args.output_dir) / "grades.jsonl"

    # 阶段0+1：一遍 sniff+grade 流式落盘
    log.info("── 阶段0+1: 嗅探 + 分级解析（流式落盘 grades.jsonl）──")
    stats = _stream_grades(files, grades_path, args.restart)
    c = stats["counts"]
    phase0 = _phase0_from_grades(grades_path)
    _print_sniff_report(phase0, time.time() - t0)
    log.info("  tier1: %d | tier2: %d | noise: %d | free_text: %d",
             c["tier1"], c["tier2"], c["noise"], c["free_text"])

    # 阶段2：流式提取
    log.info("── 阶段2: 五类信息提取（流式 schema_units.jsonl）──")
    field_mode = getattr(args, "field_mode", "template")
    if c["tier1"]:
        vocab_table, global_view, units_path = _run_extract_stream(
            grades_path, args.output_dir, field_mode, args.restart,
            sample_mode=_sample_mode_from_args(args),
        )
        _print_extract_report(global_view)
    else:
        vocab_table = {}
        global_view = {"note": "无 tier1 种子, 跳过提取", "uncertain_vocab": []}
        units_path = Path(args.output_dir) / "schema_units.jsonl"
        log.warning("无 tier1 种子可提取")

    total_elapsed = time.time() - t0
    global_view["elapsed_s"] = round(total_elapsed, 1)

    pipeline_result = {
        "phase0": {
            "cross_table": phase0["cross_table"],
            "format_dist": phase0["format_dist"],
            "low_confidence_count": len(phase0["low_confidence"]),
        },
        "phase1": {
            "tier1_count": c["tier1"],
            "tier2_count": c["tier2"],
            "noise_count": c["noise"],
            "free_text_count": c["free_text"],
        },
        "phase2": {
            "global_view": global_view,
            "schema_unit_count": global_view.get("schema_unit_count", 0),
            "vocab_class_count": len(vocab_table),
        },
        "total_elapsed_s": round(total_elapsed, 1),
    }

    _save_output(
        {"vocab_table": vocab_table, "uncertain": global_view.get("uncertain_vocab", [])},
        "vocab_table.json",
        args.output_dir,
    )
    _save_output(pipeline_result, "pipeline_report.json", args.output_dir)
    log.info("全流水线完成, 总耗时: %.1fs", total_elapsed)
    log.info("  schema_units 已流式写入: %s", units_path)


# ════════════════════════════════════════════════════════════════
# 输出辅助
# ════════════════════════════════════════════════════════════════


def _print_sniff_report(result: dict, elapsed: float):
    fmt_dist = result["format_dist"]
    log.info("  文件总数: %d", result["total_files"])
    log.info("  格式分布:")
    for fmt, cnt in sorted(fmt_dist.items(), key=lambda x: -x[1]):
        pct = cnt / max(result["total_files"], 1) * 100
        log.info("    %-20s: %5d  (%5.1f%%)", fmt, cnt, pct)
    log.info("  低置信样本: %d", len(result["low_confidence"]))
    log.info("  耗时: %.1fs", elapsed)


def _print_parse_report(report: dict):
    log.info("  文件总数: %d", report["total"])
    log.info("  tier1 (干净种子):    %d", report["tier1_clean_seeds"])
    log.info("  tier2 (可恢复噪声):  %d", report["tier2_noisy"])
    log.info("  tier3 (不可解析):    %d", report["tier3_unparseable"])
    log.info("  noise_sample (日志): %d", report["noise_sample_log"])
    log.info("  free_text:           %d", report["free_text"])
    log.info("  耗时: %ss", report["elapsed_s"])


def _print_extract_report(result: dict):
    log.info("  Records sampled:     %d", result.get("total_records_sampled", 0))
    log.info("  形状模板数 (B):      %d", result.get("shape_templates_B", 0))
    log.info("  命名模板数 (A):      %d", result.get("naming_templates_A", 0))
    log.info("  A/B 比值:            %s", result.get("AB_ratio", 0))
    log.info("  PII 种子字段数:      %d", result.get("pii_seeds_count", 0))
    log.info("  不确定词汇对:        %d", len(result.get("uncertain_vocab", [])))
    log.info("  SchemaUnit 数:       %d", result.get("schema_unit_count", 0))
    if result.get("partition_total") is not None:
        log.info("  分片数:              %d", result["partition_total"])
    log.info("  耗时: %ss", result.get("elapsed_s", 0))


def _save_output(data, filename: str, output_dir: str):
    """保存 JSON 结果到文件."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log.info("  输出: %s", path)


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


def _add_sample_args(p):
    """给 extract/pipeline 子命令挂样本保留标志 (默认关闭, 守 PII 红线)。"""
    p.add_argument("--keep-samples", action="store_true",
                   help="信息三保留样本值 (⚠ 落盘原值, 默认关闭以守 PII 红线); 每字段≤5个、按 pattern 去重")
    p.add_argument("--mask-samples", action="store_true",
                   help="配合 --keep-samples: 样本脱敏 (保留分隔符与长度, 内容字符打码), 不落原始字符")


def main():
    parser = argparse.ArgumentParser(
        description="PII Detect — 数据预处理和信息提取流水线"
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # 所有子命令共享的通用参数（输入/输出/日志/详细程度）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("root", help="语料库根目录/文件")
    common.add_argument("-o", "--output-dir", default="output", help="输出目录 (默认: output)")
    common.add_argument("-f", "--output-file", default=None, help="日志文件路径 (默认: <output-dir>/<command>.log)")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="开启 DEBUG 级日志 (含逐文件吞错、骨架解析等细节)")
    common.add_argument("--restart", action="store_true",
                        help="清空已有 grades.jsonl / schema_units.jsonl 重新开始 (默认续跑)")

    # sniff
    sub.add_parser("sniff", parents=[common], help="阶段0: 内容嗅探 → 交叉表")

    # parse
    sub.add_parser("parse", parents=[common], help="阶段1: 容错分级解析")

    # extract
    p_extract = sub.add_parser("extract", parents=[common], help="阶段2: 五类信息提取")
    p_extract.add_argument("--field-mode", choices=["template", "fold"], default="template",
                           help="字段主干方案: template(B, 默认, 裁剪到主导签名) / fold(A, 全路径并集)")
    _add_sample_args(p_extract)

    # pipeline
    p_pipe = sub.add_parser("pipeline", parents=[common], help="三阶段全流水线")
    p_pipe.add_argument("--field-mode", choices=["template", "fold"], default="template",
                        help="字段主干方案: template(B, 默认) / fold(A, 全路径并集)")
    _add_sample_args(p_pipe)

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

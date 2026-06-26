"""对比驱动：同一批 SchemaUnit 上跑「模板渲染」与「LLM 渲染」，各自反解析校验结构保真，
落盘双份渲染文本 + 一份 compare_report.json（含逐单元指标与汇总）。

用法（unit 文件来自阶段2 落盘的 schema_units.jsonl）::

    uv run python -m src.synth.compare --units output/schema_units.jsonl \
        -o output/synth_compare -n 20 --llm fake
    # 远程接真实端点：--llm openai（配 SYNTH_LLM_* env）

默认 --llm fake（离线确定性桩，无需 API）；--no-llm 只跑模板。
"""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.synth.llm_render import (
    FakeFillLLMClient, FakeLLMClient, OpenAICompatClient, render_llm, render_llm_fill,
)
from src.synth.render import render_unit, surface_format
from src.synth.validate import validate_render
from src.utils.jsonl import iter_jsonl
from src.utils.logger import get_logger, setup_logger

log = get_logger(__name__)

_EXT = {"json": "json", "jsonl": "jsonl", "csv": "csv", "tsv": "tsv", "sql": "sql"}


def _take(units: Iterable[Dict], n: Optional[int]) -> List[Dict]:
    out = []
    for u in units:
        if isinstance(u.get("skeleton"), dict) and u["skeleton"]:   # 仅紧凑 IR 单元
            out.append(u)
        if n is not None and len(out) >= n:
            break
    return out


def _write(out_dir: Path, sub: str, unit_id: str, sf: str, text: str) -> None:
    d = out_dir / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{unit_id}.{_EXT.get(sf, 'txt')}").write_text(text, encoding="utf-8",
                                                        errors="replace")


def _summary(rows: List[Dict], key: str) -> Dict[str, Any]:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"count": 0}
    ok = sum(1 for v in vals if v.get("ok"))
    jac = sum(v.get("jaccard", 0.0) for v in vals) / len(vals)
    fb = sum(1 for v in vals if v.get("used_fallback"))
    s = {"count": len(vals), "ok": ok, "ok_rate": round(ok / len(vals), 3),
         "mean_jaccard": round(jac, 3)}
    if key == "llm":
        s["fallback"] = fb
        fr = [v["filled_fields"] / v["n_fields"] for v in vals
              if v.get("n_fields")]
        if fr:
            s["mean_fill_rate"] = round(sum(fr) / len(fr), 3)
    return s


def render_compare(units_path: str, out_dir: str, n: Optional[int] = 20,
                   llm: str = "fake", fill: bool = False,
                   tmp_dir: Optional[str] = None) -> Dict[str, Any]:
    """主流程：读 unit → 双路渲染 + 校验 → 落盘 + 汇总报告。

    ``fill=True`` 时 LLM 走「填值模式」（模板定结构、LLM 只产值矩阵），否则整篇渲染。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tmp_dir) if tmp_dir else out / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    client = None
    if llm == "fake":
        client = FakeFillLLMClient() if fill else FakeLLMClient()
    elif llm == "openai":
        client = OpenAICompatClient()

    units = _take(iter_jsonl(units_path), n)
    log.info("对比渲染开始: %d 个紧凑 IR 单元, llm=%s, mode=%s",
             len(units), llm, "fill" if fill else "whole")

    rows: List[Dict] = []
    for unit in units:
        uid = unit.get("id", "unit")
        sf = surface_format(unit.get("format", "json"))

        t_text = render_unit(unit)
        t_val = validate_render(t_text, unit, str(tmp))
        _write(out, "template", uid, sf, t_text)

        row: Dict[str, Any] = {"unit_id": uid, "format": unit.get("format"),
                               "surface_format": sf,
                               "expected_paths": t_val["expected_path_count"],
                               "template": t_val}

        if client is not None:
            if fill:
                l_text, l_meta = render_llm_fill(
                    unit, client, fail_dump_dir=str(out / "llm_failed"))
            else:
                l_text, l_meta = render_llm(
                    unit, client,
                    validator=lambda txt, u: validate_render(txt, u, str(tmp)))
            l_val = validate_render(l_text, unit, str(tmp))
            for k in ("used_fallback", "attempts", "mode", "filled_fields",
                      "n_fields", "n_missing"):
                if k in l_meta:
                    l_val[k] = l_meta[k]
            _write(out, "llm", uid, sf, l_text)
            row["llm"] = l_val

        rows.append(row)
        log.debug("  %s [%s] template.ok=%s%s", uid, sf, t_val["ok"],
                  f" llm.ok={row['llm']['ok']}" if "llm" in row else "")

    report = {
        "units": len(rows),
        "llm_mode": llm,
        "summary": {
            "template": _summary(rows, "template"),
            **({"llm": _summary(rows, "llm")} if client is not None else {}),
        },
        "rows": rows,
    }
    (out / "compare_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("对比渲染完成: 模板 %s%s → %s",
             report["summary"]["template"],
             f" | LLM {report['summary']['llm']}" if client is not None else "",
             out / "compare_report.json")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="模板 vs LLM 渲染对比（结构保真度量）")
    ap.add_argument("--units", required=True, help="schema_units.jsonl 路径")
    ap.add_argument("-o", "--output-dir", default="output/synth_compare")
    ap.add_argument("-n", type=int, default=20, help="取前 N 个单元（默认 20；0=全部）")
    ap.add_argument("--llm", choices=["fake", "openai"], default="fake",
                    help="LLM client：fake=离线桩(默认) / openai=兼容端点")
    ap.add_argument("--no-llm", action="store_true", help="只跑模板渲染")
    ap.add_argument("--fill", action="store_true",
                    help="LLM 走填值模式（模板定结构、LLM 只产值矩阵，根治宽/SQL 漂移）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logger("pii_detect", level=logging.DEBUG if args.verbose else logging.INFO)
    render_compare(
        units_path=args.units,
        out_dir=args.output_dir,
        n=None if args.n == 0 else args.n,
        llm="none" if args.no_llm else args.llm,
        fill=args.fill,
    )


if __name__ == "__main__":
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    os.environ.pop("all_proxy", None)
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("ALL_PROXY", None)
    main()

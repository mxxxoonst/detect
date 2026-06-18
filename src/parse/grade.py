"""容错分级解析: Grade 数据类 + 路由分发."""

from dataclasses import dataclass
from typing import Any, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class Grade:
    """阶段1 解析分拣结果。

    tier:  1=干净种子 / 2=可恢复噪声 / 3=不可解析
           'noise_sample'=log类弱结构 / 'free_text'=自由文本
    I:     可恢复性 0~1, 非结构化类型为 None
    """
    tier: int | str
    I: Optional[float]
    parsed: Any = None
    fmt: str = ""
    error: Optional[str] = None
    n_form: Optional[str] = None       # JSON 格式错误分类
    n_struct: Optional[float] = None   # CSV 列漂移度量
    n_detail: Optional[dict] = None    # tier2 失败细节(错误串/偏移/坏行长度/列分布)，供噪声分布聚类；结构化、不落原始内容
    note: Optional[str] = None
    encoding: str = "utf-8"
    path: str = ""


def grade_from_summary(d: dict) -> Grade:
    """从 grades.jsonl 的一行摘要重建最小 Grade（供阶段2 流式消费）。

    只重建下游 partition_file 实际需要的字段（path/fmt/encoding/tier/I 等），
    不含 parsed（阶段2 从磁盘重读原文件，无需 parsed 负载）。
    """
    return Grade(
        tier=d.get("tier"),
        I=d.get("I"),
        fmt=d.get("fmt", ""),
        encoding=d.get("encoding", "utf-8"),
        path=d.get("path", ""),
        n_form=d.get("n_form"),
        n_struct=d.get("n_struct"),
        n_detail=d.get("n_detail"),
        note=d.get("note"),
        error=d.get("error"),
    )


def grade_parse(path: str, real_format: str, enc: str) -> Grade:
    """按真实范式路由到对应解析器。

    Args:
        path: 文件路径
        real_format: 阶段0 嗅探出的真实格式
        enc: 阶段0 探测出的编码
    """
    # 延迟 import, 避免循环依赖
    from src.parse.json_parser import parse_json, parse_jsonl
    from src.parse.csv_parser import parse_csv, parse_tsv
    from src.parse.sql_parser import parse_sql_text
    from src.parse.xlsx_parser import parse_xlsx

    fmt = real_format

    grade = _route(fmt, path, enc,
                   parse_json, parse_jsonl, parse_csv, parse_tsv,
                   parse_sql_text, parse_xlsx)
    log.debug("grade_parse %s: fmt=%s → tier=%s I=%s error=%s",
              path, fmt, grade.tier, grade.I, grade.error)
    return grade


def _route(fmt, path, enc, parse_json, parse_jsonl, parse_csv, parse_tsv,
           parse_sql_text, parse_xlsx) -> Grade:
    """按格式分发到具体解析器，统一回填 path。"""
    if fmt == "xlsx":
        grade = parse_xlsx(path)
        grade.path = path
        return grade

    if fmt in ("json", "jsonl"):
        grade = parse_jsonl(path, enc) if fmt == "jsonl" else parse_json(path, enc)
        grade.path = path
        return grade

    if fmt == "csv":
        grade = parse_csv(path, enc)
        grade.path = path
        return grade

    if fmt == "tsv":
        grade = parse_tsv(path, enc)
        grade.path = path
        return grade

    if fmt == "sql":
        grade = parse_sql_text(path, enc)
        grade.path = path
        return grade

    if fmt == "log":
        return Grade(tier="noise_sample", I=None, fmt="log", encoding=enc, path=path,
                     note="日志为弱结构, 供注噪参照/真实测试集")

    if fmt == "free_text":
        return Grade(tier="free_text", I=None, fmt="free_text", encoding=enc, path=path,
                     note="自由文本, 进 PII 自举(阶段2)/真实测试集, 不做结构解析")

    # binary_unknown / empty / 其他不可解析
    return Grade(tier=3, I=0.0, fmt=fmt, encoding=enc, path=path,
                 note=f"格式 '{fmt}' 不可解析")

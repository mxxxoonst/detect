"""parse 阶段「严格谓词 / 容错转换器」两入口 + 共享内核 (反漂移)。

承接 docs/parser_strict_tolerant_design.md §0.3 / §4 / §5:

- **严格 = canonical_core(recovery=OFF)**: 种子门 oracle。`strict_ok(path,fmt,enc) -> StrictVerdict`。
- **容错 = canonical_core(recovery=ON, 仪表化)**: IR 产生器。`tolerant_parse(path,fmt,enc) -> ParseResult`。

二者**共享同一内核** —— 即各格式的 `parse_X()`: 它一遍状态机/流式扫描同时产出
严格门计数 `C` 与容错救回计数 `P/L`, 封装在同一个 `Grade` 里。两入口都从这同一个
`Grade` 派生, 因此命门恒成立:

    strict_ok(x).clean ⟺ I_strict==1 ⟺ tolerant_parse(x).report.deviations == 0

(退化曲线起点 I(x_clean)=1 的健全性命门, design §0.3。)

`deviations = P + L` (= 形式层总破坏 = N - C); `deviations==0 ⟺ C==N ⟺ I_strict==1`。

PII 红线: 本模块只产计数/path/I/I_strict, 不持有任何原值; `raw_spans` 仅产 anchor_path
(byte_range 留待训练期注噪算子, 见 design §5), 不落原文。
"""

from typing import Optional, TypedDict

from src.parse.grade import Grade, grade_parse
from src.utils.logger import get_logger

log = get_logger(__name__)


# ── 数据契约 (TypedDict, 见 CLAUDE.md: 结构以 dict 流动) ──────────────────────
class StrictVerdict(TypedDict):
    """严格谓词输出 (种子门)。clean ⟺ I_strict==1 ⟺ tier1。"""
    clean: bool
    reason: str          # 不干净时的形式层破坏标记 (n_form / "deviations>0" 等); 干净为 ""
    n_unit: int          # 单元总数 N (估算/精确, 视格式)


class RecoveryReport(TypedDict):
    """容错报告 (C/P/L/I/I_strict/tier + 偏离数)。"""
    C: int
    P: int
    L: int
    N: int
    I: Optional[float]
    I_strict: Optional[float]
    tier: object         # int | str (沿用 Grade.tier 取值域)
    deviations: int      # P + L = N - C; ==0 ⟺ 严格零偏离 ⟺ clean


class ParseResult(TypedDict):
    """容错转换器输出 (通道一 units 占位 + 通道二 raw_spans 锚点 + 报告)。

    units / raw_spans 在 parse 阶段为占位 (parse 不组装 SchemaUnit, 那是 extract 阶段);
    本入口的实质产物是 report (C/P/L/I/I_strict/tier/deviations), 供训练期 tier 标定。
    """
    units: list           # 通道一 (parse 阶段空, extract 阶段填)
    raw_spans: list       # 通道二 [RAW] 锚点 (parse 阶段空)
    report: RecoveryReport


# ── C/P/L 还原: 从 Grade 派生 (共享内核的免费副产品) ─────────────────────────
def _cpl_from_grade(g: Grade) -> tuple:
    """从 Grade 还原 (C, P, L, N)。

    各 parser 已把 C/P/L 落进 n_detail (c_count/p_count/l_count/n_total); 干净 tier1
    无 n_detail, 此时 C==N (I_strict==1, 零 P 零 L)。非结构化 (log/free_text) C/P/L 无意义, 归 0。
    """
    nd = g.n_detail or {}
    if "c_count" in nd:
        C = int(nd.get("c_count", 0))
        P = int(nd.get("p_count", 0))
        L = int(nd.get("l_count", 0))
        N = int(nd.get("n_total", C + P + L))
        return C, P, L, max(N, C + P + L)

    # 无 C/P/L 明细: 干净 tier1 (I_strict==1) ⟹ 全 C; 其余按 I_strict 还原。
    units = _unit_count(g)
    if g.I_strict is not None and g.I_strict >= 1.0:
        return units, 0, 0, units
    if g.tier == 3:
        return 0, 0, units, units
    # 兜底: 用 I_strict 估 C (理论上结构化格式都会落 n_detail, 极少走到这)。
    C = round((g.I_strict or 0.0) * units)
    return C, max(units - C, 0), 0, units


def _unit_count(g: Grade) -> int:
    """从 parsed 摘要取单元总数 N (各格式键名不一, 取第一个命中的)。"""
    p = g.parsed or {}
    for k in ("total_statements", "rows", "good_rows", "total_rows", "units"):
        if k in p and isinstance(p[k], int):
            return p[k]
    return 1 if g.tier in (1, 2) else 0


def _deviations(g: Grade) -> int:
    """形式层总破坏 = P + L = N - C。==0 ⟺ I_strict==1 ⟺ clean。"""
    C, P, L, _ = _cpl_from_grade(g)
    return P + L


# ── 两入口 ──────────────────────────────────────────────────────────────────
def strict_ok(path: str, fmt: str, enc: str) -> StrictVerdict:
    """严格谓词 (种子门 oracle)。clean ⟺ 容错零偏离 ⟺ I_strict==1 ⟺ tier1。

    高精度 / fail-closed: 拿不准判不干净 (见 design §0.1)。
    """
    g = grade_parse(path, fmt, enc)
    C, P, L, N = _cpl_from_grade(g)
    dev = P + L
    clean = (g.I_strict is not None and g.I_strict >= 1.0 and dev == 0 and g.tier == 1)
    reason = ""
    if not clean:
        reason = g.n_form or g.error or f"deviations={dev}"
    return StrictVerdict(clean=clean, reason=reason, n_unit=N)


def tolerant_parse(path: str, fmt: str, enc: str) -> ParseResult:
    """容错转换器 (IR 产生器)。与 strict_ok 共享同一 Grade 内核, 派生 C/P/L/deviations。

    命门: tolerant_parse(x).report.deviations == 0 ⟺ strict_ok(x).clean (逐字节同源)。
    """
    g = grade_parse(path, fmt, enc)
    C, P, L, N = _cpl_from_grade(g)
    report = RecoveryReport(
        C=C, P=P, L=L, N=N,
        I=g.I, I_strict=g.I_strict, tier=g.tier,
        deviations=P + L,
    )
    return ParseResult(units=[], raw_spans=[], report=report)

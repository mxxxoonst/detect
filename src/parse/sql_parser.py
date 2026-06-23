"""SQL 文本解析器: 严格内核 (scan_sql 状态机) + 容错抽取 (regex 降级)。

角色解耦 (见 docs/parser_strict_tolerant_design.md §2.4):
- **严格内核** = `sql_strict.scan_sql_file`: 引号/注释/括号/`$$`/`--` 感知的超集分句器,
  全语句平衡、扫到 EOF 无截断、每条可归类 ⟹ strict_ok (I_strict==1 ⟺ tier1)。
- **容错叠加** = 现有 regex (`CREATE_INSERT_RE`) **降级**为抽取器, 跑在 scan_sql 切出的语句
  边界上: 损坏语句里仍能抽出 表名+列清单 → schema 进通道一 (P); 连头都抽不出 → L。
- **度量** I 改为**语句级可恢复性** (C+P)/N, 不再是 CREATE/INSERT 关键词纯度;
  引号/括号闭合由状态机精确判定, 取代裸奇偶 `_check_unclosed_quotes`。
- **方言探测** `_detect_sql_dialect` 保留为弱元数据, **不参与**严格判定。

PII 红线: 流式扫到 EOF, 抽出表名/列名等结构事实后即丢弃中间文本; grades.jsonl 不落 SQL 原文。
"""

import re
from typing import NamedTuple, Optional

from src.parse.grade import Grade
from src.parse.sql_strict import iter_sql_file_statements
from src.utils.logger import get_logger

log = get_logger(__name__)


class _ScanCounts(NamedTuple):
    """惰性累加得到的 scan 层计数 (替代物化 ScanResult, 口径与 sql_strict.summarize 一致)。"""
    n_total: int
    n_balanced: int
    n_truncated: int
    n_unbalanced: int
    n_form: Optional[str]


# 容错抽取器: 在 scan_sql 切出的语句边界上抽 schema 头部 (降级自原 tier 判定主力)。
CREATE_INSERT_RE = re.compile(
    r'\b(CREATE\s+TABLE|CREATE\s+INDEX|INSERT\s+INTO|DROP\s+TABLE|ALTER\s+TABLE)\b',
    re.IGNORECASE
)

# 方言探测仍只看头部 (弱元数据, 不参与严格判定), 故只读前 64KB 喂 _detect_sql_dialect。
_DIALECT_HEAD = 65536


def parse_sql_text(path: str, encoding: str) -> Grade:
    """严格内核 (scan_sql) 切句 + 容错 regex 抽 schema, 产语句级 I / I_strict / tier。

    **惰性消费** `iter_sql_file_statements`: 单遍累加 C/P/L + has_create/insert + scan 计数,
    **不 `list()` 物化全部语句** (GB SQL dump 内存恒定, 见 docs §4 / plan Phase 4)。
    """
    # ── 容错叠加 + 严格内核合一: 惰性逐条消费状态机切出的语句边界, 单遍累加 ──
    # 单元粒度 = 语句。N = 语句总数。
    #   C = 平衡且正常收尾、且能抽出 schema 头的语句 (严格内核直接消费成功)。
    #   P = 不可信但 regex 仍救回 schema 的语句 (损坏但抽出 表名/列 → 进通道一)。
    #   L = 截断/未闭合, 或连 schema 头都抽不出的语句 (落通道二)。
    N = 0
    C = 0
    P = 0
    L = 0
    n_truncated = 0
    n_unbalanced = 0
    has_create = False
    has_insert = False
    try:
        for st in iter_sql_file_statements(path, encoding):
            N += 1
            if st.truncated:
                n_truncated += 1
            if not st.balanced:
                n_unbalanced += 1
            extractable = bool(CREATE_INSERT_RE.search(st.text))
            if extractable:
                up = st.text.upper()
                if "CREATE TABLE" in up:
                    has_create = True
                if "INSERT INTO" in up:
                    has_insert = True
            clean = st.balanced and st.terminated and not st.truncated
            if clean and extractable:
                C += 1
            elif extractable:
                # 损坏 (未闭合/截断/无正常收尾) 但 regex 仍抽出 schema → 可信恢复, 计 P。
                P += 1
            else:
                # 截断尾句, 或非 DDL/DML (纯注释/SELECT/未知) 抽不出头 → L。
                L += 1
    except Exception as e:
        log.warning("SQL scan 失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, error=str(e))

    if N == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, note="empty file")

    # scan 层汇总 (与 sql_strict.summarize 口径一致, 惰性累加得来, 不物化语句)。
    n_balanced = N - n_unbalanced
    if n_truncated > 0:
        scan_n_form = "truncated"
    elif n_unbalanced > 0:
        scan_n_form = "unbalanced"
    else:
        scan_n_form = None
    scan = _ScanCounts(n_total=N, n_balanced=n_balanced,
                       n_truncated=n_truncated, n_unbalanced=n_unbalanced,
                       n_form=scan_n_form)

    # 方言探测: 弱元数据, 仅扫头部, 不参与严格判定。
    dialect_info = _detect_dialect_head(path, encoding)

    # I_strict = C/N (种子门); I = (C+P)/N (官方退化曲线)。
    I_strict = C / N if N > 0 else 0.0
    I = (C + P) / N if N > 0 else 0.0

    # tier 由 I_strict / I 共同决定 (不再由单一容错路径铸造)。
    if I_strict == 1.0:
        tier = 1
    elif I > 0.0:
        tier = 2
    else:
        tier = 3

    # 若整文件抽不出任何 CREATE/INSERT (C==0 ∧ P==0), 沿用旧语义记 tier3。
    if C == 0 and P == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding,
                     I_strict=0.0,
                     n_detail=_merge_detail(dialect_info, scan, C, P, L),
                     note="no CREATE/INSERT statements found")

    n_detail = _merge_detail(dialect_info, scan, C, P, L)
    note = None
    if tier != 1:
        # 形式层破坏摘要 (结构化, 不落原文)。
        n_detail.update({"kind": "sql_incomplete"})
        if scan.n_truncated > 0:
            note = f"{scan.n_truncated} truncated/unbalanced statements"
            log.debug("SQL %s: C=%d P=%d L=%d N=%d I_strict=%.3f I=%.3f, %d truncated",
                      path, C, P, L, N, I_strict, I, scan.n_truncated)

    return Grade(tier=tier, I=I, I_strict=I_strict, fmt="sql", encoding=encoding,
                 n_form=scan.n_form,
                 parsed={
                     "type": "sql",
                     "total_statements": N,
                     "clean_statements": C,
                     "repaired_statements": P,
                     "lost_statements": L,
                     "has_create": has_create,
                     "has_insert": has_insert,
                 },
                 n_detail=n_detail,
                 note=note)


def _merge_detail(dialect_info: dict, scan, C: int, P: int, L: int) -> dict:
    """合并方言元数据 + scan 计数细节 (结构化, 不含 SQL 原文)。"""
    d = dict(dialect_info)
    d.update({
        "n_total": scan.n_total,
        "c_count": C,
        "p_count": P,
        "l_count": L,
        "n_balanced": scan.n_balanced,
        "n_truncated": scan.n_truncated,
        "n_unbalanced": scan.n_unbalanced,
    })
    return d


def _detect_dialect_head(path: str, encoding: str) -> dict:
    """只读头部喂方言探测 (弱元数据, 不参与严格判定)。读失败回退 ansi/unknown。"""
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            head = f.read(_DIALECT_HEAD)
    except Exception as e:
        log.debug("SQL 方言探测读头失败 %s: %s", path, e)
        return {"dialect": "ansi", "dialect_status": "unknown", "dialect_scores": {}}
    return _detect_sql_dialect(head)


# ── C0: 方言探测 (标记投票, 仅作弱元数据; 不参与解析/分句) ──────────────────────
# 每条 (正则, 权重); 命中即加权。强唯一标记 2~3 分, 易混标记 1 分。仅扫 64KB 头。
_DIALECT_MARKERS = {
    "mysql": [
        (r"`", 1),                                   # 反引号标识符
        (r"\bAUTO_INCREMENT\b", 2),
        (r"\bENGINE\s*=", 2),
        (r"/\*!", 2),                                # 版本注释 /*!40101
        (r"\bDEFAULT\s+CHARSET\b", 2),
        (r"\bLOCK\s+TABLES\b", 1),
    ],
    "postgres": [
        (r"\$\$|\$[A-Za-z_]\w*\$", 2),               # dollar-quote $$...$$ / $tag$
        (r"\bCOPY\b[^\n;]*\bFROM\s+stdin\b", 3),     # pg_dump 数据块 (强)
        (r"^\\\.\s*$", 2),                           # COPY 结束符 \. 独行
        (r"\bOWNER\s+TO\b", 2),
        (r"\bpg_catalog\b", 2),
        (r"::\w", 1),                                # 类型转换 'x'::text
        (r"\bnextval\s*\(", 1),
        (r"\bSET\s+search_path\b", 2),
    ],
    "tsql": [
        (r"\[[A-Za-z_]\w*\]", 1),                    # [标识符]
        (r"^\s*GO\s*$", 2),                          # 独行 GO 批分隔
        (r"\bIDENTITY\s*\(", 2),
        (r"\bNVARCHAR\b", 1),
        (r"\bSET\s+(?:ANSI_NULLS|QUOTED_IDENTIFIER)\b", 2),
        (r"\bUSE\s+\[", 1),
    ],
    "sqlite": [
        (r"\bPRAGMA\b", 2),
        (r"\bAUTOINCREMENT\b", 2),                   # 无下划线, 区别 MySQL AUTO_INCREMENT
        (r"\bsqlite_sequence\b", 2),
        (r"\bWITHOUT\s+ROWID\b", 2),
        (r"\bBEGIN\s+TRANSACTION\b", 1),
    ],
    "oracle": [
        (r"\bVARCHAR2\b", 2),
        (r"\bNUMBER\s*\(", 1),
        (r"\bNVL\s*\(", 1),
        (r"\bSYSDATE\b", 1),
        (r"\bFROM\s+DUAL\b", 2),
        (r"\bCREATE\s+OR\s+REPLACE\b", 1),
    ],
}


def _detect_sql_dialect(text: str) -> dict:
    """标记投票判 SQL 方言, 返回 {dialect, dialect_status, dialect_scores}。

    dialect_status 供人工排查"无法确认"者:
      confident — 唯一方言领先 ≥2 分, 可信
      ambiguous — 存在竞争方言且分差 <2 → 多方言混判, 看 dialect_scores 人工裁定
      weak      — 仅 1 个弱标记 (top<2), 证据不足
      unknown   — 无任何方言标记 (通用 ANSI / 难判), dialect 记 'ansi'
    """
    scores = {}
    for dialect, markers in _DIALECT_MARKERS.items():
        s = 0
        for pat, w in markers:
            if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
                s += w
        scores[dialect] = s

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0
    hits = {k: v for k, v in scores.items() if v > 0}

    if top_score == 0:
        return {"dialect": "ansi", "dialect_status": "unknown", "dialect_scores": {}}
    if second_score > 0 and (top_score - second_score) < 2:
        status = "ambiguous"
    elif top_score < 2:
        status = "weak"
    else:
        status = "confident"
    return {"dialect": top_name, "dialect_status": status, "dialect_scores": hits}

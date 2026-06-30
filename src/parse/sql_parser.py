"""SQL 文本解析器: 严格内核 (scan_sql 状态机) + 容错抽取 (regex 降级)。

角色解耦 (见 docs/parser_strict_tolerant_design.md §2.4):
- **严格内核** = `sql_strict.iter_sql_file_statements`: 引号/注释/括号/`$$`/`--` 感知的超集分句器,
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


# 研究范围内的"信息单元": 只有 CREATE TABLE / INSERT INTO 产出 IR (建表结构 + 样本行)。
# I_strict / I 的分母 N 只统计这两类语句; 注释 / SET / LOCK / DROP / ALTER / SELECT 等
# 脚手架语句出范围 → 既不进分子也不进分母 (见 parse_sql_text 的单遍累加)。
IN_SCOPE_RE = re.compile(
    r'\b(CREATE\s+TABLE|INSERT\s+INTO)\b',
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
    # 单元粒度 = **研究范围内的语句** (CREATE TABLE / INSERT INTO)。N = 范围内语句数。
    #   C = 范围内、平衡且正常收尾 (严格内核直接消费成功)。
    #   P = 范围内、未闭合但未截断 → regex 仍可抽 表名/列 → 进通道一 (可信恢复)。
    #   L = 范围内、截断 (尾部丢失, schema/行可能不全) → 落通道二。
    # 脚手架语句 (注释/SET/LOCK/DROP/ALTER/SELECT 等, 不匹配 IN_SCOPE_RE) **出范围**:
    #   既不进 N 也不进 C/P/L, 只在 scan 层计 n_total/n_truncated/n_unbalanced 作文件健康元数据。
    #   修复: 旧逻辑把脚手架计入 L 稀释 I_strict, 令干净 mysqldump (~10 条 SET/注释) 普遍掉 tier2。
    N = 0
    C = 0
    P = 0
    L = 0
    n_total = 0          # scan 层全部语句 (含脚手架), 仅作文件健康元数据
    n_truncated = 0
    n_unbalanced = 0
    n_ignored = 0        # 出范围 (脚手架) 语句数
    has_create = False
    has_insert = False
    try:
        for st in iter_sql_file_statements(path, encoding):
            n_total += 1
            if st.truncated:
                n_truncated += 1
            if not st.balanced:
                n_unbalanced += 1
            if not IN_SCOPE_RE.search(st.text):
                n_ignored += 1                       # 出范围: 不进 N / C / P / L
                continue
            N += 1
            up = st.text.upper()
            if "CREATE TABLE" in up:
                has_create = True
            if "INSERT INTO" in up:
                has_insert = True
            if st.balanced and st.terminated and not st.truncated:
                C += 1
            elif st.truncated:
                L += 1                               # 尾部截断 → 通道二
            else:
                P += 1                               # 未闭合但完整 → regex 抽头 → 通道一
    except Exception as e:
        log.warning("SQL scan 失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, error=str(e))

    if n_total == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, note="empty file")

    # scan 层汇总 (over 全部语句, 文件健康元数据; 不参与 I/tier 计算)。
    n_balanced = n_total - n_unbalanced
    if n_truncated > 0:
        scan_n_form = "truncated"
    elif n_unbalanced > 0:
        scan_n_form = "unbalanced"
    else:
        scan_n_form = None
    scan = _ScanCounts(n_total=n_total, n_balanced=n_balanced,
                       n_truncated=n_truncated, n_unbalanced=n_unbalanced,
                       n_form=scan_n_form)

    # 方言探测: 弱元数据, 仅扫头部, 不参与严格判定。
    dialect_info = _detect_dialect_head(path, encoding)

    # 范围内零语句 (整文件无 CREATE TABLE / INSERT INTO) → tier3。
    if N == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding,
                     I_strict=0.0,
                     n_detail=_merge_detail(dialect_info, scan, C, P, L, n_ignored),
                     note="no CREATE/INSERT statements found")

    # I_strict = C/N (种子门, N=范围内语句); I = (C+P)/N (官方退化曲线)。
    I_strict = C / N
    I = (C + P) / N

    # tier 由 I_strict / I 共同决定。范围内全截断 (C=0,P=0) → I=0 → tier3。
    if I_strict == 1.0:
        tier = 1
    elif I > 0.0:
        tier = 2
    else:
        tier = 3

    n_detail = _merge_detail(dialect_info, scan, C, P, L, n_ignored)
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


def _merge_detail(dialect_info: dict, scan, C: int, P: int, L: int, n_ignored: int = 0) -> dict:
    """合并方言元数据 + scan 计数细节 (结构化, 不含 SQL 原文)。

    n_total/n_balanced/n_truncated/n_unbalanced = scan 层 (全部语句, 文件健康);
    n_scope/c/p/l = 范围内 (CREATE TABLE / INSERT INTO) 的 C/P/L 会计;
    n_ignored = 出范围脚手架语句数。I_strict/I 只由 n_scope 口径算。
    """
    d = dict(dialect_info)
    d.update({
        "n_total": scan.n_total,
        "n_scope": C + P + L,
        "c_count": C,
        "p_count": P,
        "l_count": L,
        "n_ignored": n_ignored,
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

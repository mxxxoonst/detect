"""SQL 文本解析器: regex 抽取 CREATE/INSERT 头部, 不做 AST."""

import re

from src.parse.grade import Grade
from src.utils.logger import get_logger

log = get_logger(__name__)


CREATE_INSERT_RE = re.compile(
    r'\b(CREATE\s+TABLE|CREATE\s+INDEX|INSERT\s+INTO|DROP\s+TABLE|ALTER\s+TABLE)\b',
    re.IGNORECASE
)
STATEMENT_DELIMITERS = re.compile(r';\s*\n|;\s*$')


def parse_sql_text(path: str, encoding: str) -> Grade:
    """Regex 抽取 SQL 文件中的 CREATE/INSERT 头部."""
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            text = f.read(65536)  # 只读前 64KB, SQL schema 一般不大
    except Exception as e:
        log.warning("SQL 读头失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, error=str(e))

    if not text.strip():
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, note="empty file")

    # C0: text 非空 → 一律探测方言, tier1/2/3 都带 dialect 标, 供方言分布 survey
    dialect_info = _detect_sql_dialect(text)

    # 统计所有语句分隔符位置, 估算 statement 数量
    statements = STATEMENT_DELIMITERS.split(text)
    # 过滤空语句
    statements = [s.strip() for s in statements if s.strip()]

    total = len(statements)
    if total == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding,
                     n_detail=dialect_info, note="no statements found")

    # 统计含有 CREATE TABLE/INSERT 等关键操作的语句
    complete = sum(1 for s in statements if CREATE_INSERT_RE.search(s))

    if complete == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding,
                     n_detail=dialect_info, note="no CREATE/INSERT statements found")

    I = complete / total if total > 0 else 0.0
    tier = 1 if I == 1.0 else 2

    # 检查是否有不完整的 SQL (引号不闭合)
    # 注: 引号奇偶计数是方言朴素的粗判 (不识别 '' 转义 / $$ dollar-quote / E'..'),
    #     会高估未闭合; 方言无关的精确判定随任务 C(超集分句器)落地。
    incomplete_info = None
    n_detail = dict(dialect_info)   # 始终带方言标; tier2 再叠加失败细节
    if tier == 2:
        unclosed = _check_unclosed_quotes(statements)
        n_detail.update({"kind": "sql_incomplete", "complete": complete,
                         "total": total, "unclosed_quotes": unclosed})
        if unclosed > 0:
            incomplete_info = f"{unclosed} statements with unclosed quotes"
            log.debug("SQL %s: %d/%d 完整语句, %d 条引号不配对",
                      path, complete, total, unclosed)

    return Grade(tier=tier, I=I, fmt="sql", encoding=encoding,
                 parsed={
                     "type": "sql",
                     "total_statements": total,
                     "complete_statements": complete,
                     "has_create": any("CREATE TABLE" in s.upper() for s in statements),
                     "has_insert": any("INSERT INTO" in s.upper() for s in statements),
                 },
                 n_detail=n_detail,
                 note=incomplete_info)


def _check_unclosed_quotes(statements: list) -> int:
    """检查有多少条语句的引号不配对."""
    count = 0
    for stmt in statements:
        single = stmt.count("'")
        double = stmt.count('"')
        if single % 2 != 0 or double % 2 != 0:
            count += 1
    return count


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

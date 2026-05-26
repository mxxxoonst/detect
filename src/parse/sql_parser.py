"""SQL 文本解析器: regex 抽取 CREATE/INSERT 头部, 不做 AST."""

import re

from src.parse.grade import Grade
from src.constants import SQL_KEYWORD_PATTERN


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
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, error=str(e))

    if not text.strip():
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, note="empty file")

    # 统计所有语句分隔符位置, 估算 statement 数量
    statements = STATEMENT_DELIMITERS.split(text)
    # 过滤空语句
    statements = [s.strip() for s in statements if s.strip()]

    total = len(statements)
    if total == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding, note="no statements found")

    # 统计含有 CREATE TABLE/INSERT 等关键操作的语句
    complete = sum(1 for s in statements if CREATE_INSERT_RE.search(s))

    if complete == 0:
        return Grade(tier=3, I=0.0, fmt="sql", encoding=encoding,
                     note="no CREATE/INSERT statements found")

    I = complete / total if total > 0 else 0.0
    tier = 1 if I == 1.0 else 2

    # 检查是否有不完整的 SQL (引号不闭合)
    incomplete_info = None
    if I < 1.0:
        unclosed = _check_unclosed_quotes(statements)
        if unclosed > 0:
            incomplete_info = f"{unclosed} statements with unclosed quotes"

    return Grade(tier=tier, I=I, fmt="sql", encoding=encoding,
                 parsed={
                     "type": "sql",
                     "total_statements": total,
                     "complete_statements": complete,
                     "has_create": any("CREATE TABLE" in s.upper() for s in statements),
                     "has_insert": any("INSERT INTO" in s.upper() for s in statements),
                 },
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

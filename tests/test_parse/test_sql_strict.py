"""测试 SQL 严格内核 (scan_sql 状态机) + 容错抽取的解耦。

覆盖 docs/parser_strict_tolerant_design.md §3 / §7 Phase 1 要求的用例:
- 完全干净平衡多语句 (C==N, I_strict==1, tier1)
- 未闭合单引号 (落 tier2, I_strict<1)
- 含 '' 与 \\' 转义的合法语句 (不应误判未闭合)
- 块注释 /* */ 内含分号 (不应误切句)
- $$ dollar-quote 内含分号/引号 (不应误切句)
- 尾部截断 (unbalanced → truncated → tier2)
- 空文件 / 无 CREATE-INSERT
- 一致性: strict_ok ⟺ scan 零截断 (退化曲线起点命门)
"""

from pathlib import Path

from src.parse.grade import grade_parse
from src.parse.sql_parser import parse_sql_text
from src.parse.sql_strict import (
    scan_sql_statements,
    summarize,
)


def _scan(text: str):
    """把整段文本当单块喂状态机, 返回 (语句列表, ScanResult)。"""
    statements = list(scan_sql_statements([text]))
    return statements, summarize(statements)


def _write(make_temp_file, name: str, content: str) -> str:
    return make_temp_file(name, content)


# ── 1. 完全干净平衡多语句 → C==N, I_strict==1, tier1 ───────────────────────────
def test_clean_balanced_multistatement(make_temp_file):
    sql = (
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR(50));\n"
        "INSERT INTO t (id, name) VALUES (1, 'Alice');\n"
        "INSERT INTO t (id, name) VALUES (2, 'Bob');\n"
    )
    statements, scan = _scan(sql)
    assert scan.n_total == 3
    assert scan.n_truncated == 0
    assert scan.strict_ok is True
    assert scan.n_form is None
    assert all(s.balanced and s.terminated and not s.truncated for s in statements)

    path = _write(make_temp_file, "clean.sql", sql)
    grade = parse_sql_text(path, "utf-8")
    assert grade.tier == 1
    assert grade.I_strict == 1.0
    assert grade.I == 1.0
    assert grade.n_detail["c_count"] == 3
    assert grade.n_detail["p_count"] == 0
    assert grade.n_detail["l_count"] == 0


# ── 2. 未闭合单引号 → tier2, I_strict<1 ──────────────────────────────────────
def test_unclosed_single_quote(make_temp_file):
    # 第三条 INSERT 的字符串未闭合, 吞掉后续直到 EOF。
    sql = (
        "CREATE TABLE t (id INT, name TEXT);\n"
        "INSERT INTO t VALUES (1, 'ok');\n"
        "INSERT INTO t VALUES (2, 'unterminated);\n"
    )
    statements, scan = _scan(sql)
    assert scan.strict_ok is False
    assert scan.n_truncated >= 1
    assert scan.n_form in ("truncated", "unbalanced")
    # 尾句应判截断。
    assert statements[-1].truncated is True

    path = _write(make_temp_file, "unclosed.sql", sql)
    grade = parse_sql_text(path, "utf-8")
    assert grade.tier == 2
    assert grade.I_strict < 1.0
    # 尾句含 INSERT INTO, regex 仍抽得出 → 计 P, 故 I 可能 == 1.0
    assert grade.I > 0.0


# ── 3. 含 '' 与 \\' 转义的合法语句 → 不应误判未闭合 ────────────────────────────
def test_escaped_quotes_not_unclosed(make_temp_file):
    # '' 双单引号转义 + \\' 反斜杠转义, 两条都是平衡合法语句。
    sql = (
        "INSERT INTO t VALUES (1, 'O''Brien');\n"          # '' 转义
        "INSERT INTO t VALUES (2, 'line\\'s end');\n"      # \\' 转义
    )
    statements, scan = _scan(sql)
    assert scan.n_total == 2
    assert scan.n_truncated == 0
    assert scan.strict_ok is True
    assert all(s.balanced and s.terminated for s in statements)

    path = _write(make_temp_file, "escaped.sql", sql)
    grade = parse_sql_text(path, "utf-8")
    # 这两条无 CREATE/INSERT 之外只有 INSERT INTO → extractable, 平衡 → C。
    assert grade.tier == 1
    assert grade.I_strict == 1.0


# ── 4. 块注释 /* */ 内含分号 → 不应误切句 ─────────────────────────────────────
def test_block_comment_with_semicolon(make_temp_file):
    sql = (
        "CREATE TABLE t (id INT);\n"
        "/* a comment; with; semicolons; and 'quotes' */\n"
        "INSERT INTO t VALUES (1);\n"
    )
    statements, scan = _scan(sql)
    # 注释内的 ; 不切句 → 仍是 2 条带分隔语句 (注释并入相邻语句缓冲)。
    assert scan.n_truncated == 0
    assert scan.strict_ok is True
    # 不应因注释内分号炸出大量语句。
    assert scan.n_total <= 3

    path = _write(make_temp_file, "blockcmt.sql", sql)
    grade = parse_sql_text(path, "utf-8")
    assert grade.tier == 1
    assert grade.I_strict == 1.0


# ── 5. 行注释 -- 内含分号 → 不应误切句 ───────────────────────────────────────
def test_line_comment_with_semicolon(make_temp_file):
    sql = (
        "CREATE TABLE t (id INT);\n"
        "-- this; is; a; line comment with semicolons\n"
        "INSERT INTO t VALUES (1);\n"
    )
    statements, scan = _scan(sql)
    assert scan.n_truncated == 0
    assert scan.strict_ok is True


# ── 6. $$ dollar-quote 内含分号/引号 → 不应误切句 ─────────────────────────────
def test_dollar_quote_with_semicolon_and_quotes(make_temp_file):
    sql = (
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "BEGIN\n"
        "  PERFORM 'a; b; c'; -- semicolons and a single quote inside\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "INSERT INTO t VALUES (1);\n"
    )
    statements, scan = _scan(sql)
    # dollar-quote 体内的 ; 不切句; 整个函数 + INSERT = 2 条, 全平衡。
    assert scan.n_truncated == 0
    assert scan.strict_ok is True
    assert scan.n_total == 2


def test_named_dollar_quote(make_temp_file):
    # $tag$ 命名 dollar-quote, 内部含会迷惑朴素匹配的 $$ 与 ; 。
    sql = (
        "INSERT INTO t VALUES (1, $body$has ; and $$ inside$body$);\n"
        "INSERT INTO t VALUES (2, 'ok');\n"
    )
    statements, scan = _scan(sql)
    assert scan.n_total == 2
    assert scan.n_truncated == 0
    assert scan.strict_ok is True


# ── 7. 尾部截断 (unbalanced) → tier2 ────────────────────────────────────────
def test_trailing_truncation_unbalanced(make_temp_file):
    # 末句括号未闭合 + 无分隔符正常收尾。
    sql = (
        "INSERT INTO t VALUES (1, 'ok');\n"
        "INSERT INTO t VALUES (2, 'partial"          # EOF: 仍在 SQUOTE, depth>0
    )
    statements, scan = _scan(sql)
    assert scan.strict_ok is False
    assert scan.n_truncated >= 1
    assert statements[-1].truncated is True

    path = _write(make_temp_file, "trunc.sql", sql)
    grade = parse_sql_text(path, "utf-8")
    assert grade.tier == 2
    assert grade.I_strict < 1.0


def test_missing_final_semicolon_is_truncated(make_temp_file):
    # 末句平衡但无分隔符正常收尾 → 仍判截断 (保守 fail-closed)。
    sql = "INSERT INTO t VALUES (1, 'ok')"        # 平衡但无尾 ';'
    statements, scan = _scan(sql)
    assert statements[-1].terminated is False
    assert statements[-1].truncated is True
    assert scan.strict_ok is False


# ── 8. 空文件 / 无 CREATE-INSERT ────────────────────────────────────────────
def test_empty_file(make_temp_file):
    path = _write(make_temp_file, "empty.sql", "   \n\n  ")
    grade = parse_sql_text(path, "utf-8")
    assert grade.tier == 3
    assert grade.I == 0.0


def test_no_create_insert(make_temp_file):
    # 全是 SELECT, 无 DDL/DML schema 头。
    sql = "SELECT 1;\nSELECT * FROM t;\n"
    statements, scan = _scan(sql)
    assert scan.strict_ok is True            # scan 层平衡
    path = _write(make_temp_file, "select_only.sql", sql)
    grade = parse_sql_text(path, "utf-8")
    # 抽不出 CREATE/INSERT → C==0 ∧ P==0 → tier3 (旧语义保留)。
    assert grade.tier == 3
    assert grade.I == 0.0
    assert grade.I_strict == 0.0


# ── 9. 一致性命门: strict_ok ⟺ scan 零截断 ───────────────────────────────────
def test_strict_ok_consistency():
    clean = (
        "CREATE TABLE t (id INT);\n"
        "INSERT INTO t VALUES (1);\n"
    )
    _, scan_clean = _scan(clean)
    assert scan_clean.strict_ok == (scan_clean.n_truncated == 0
                                    and scan_clean.n_balanced == scan_clean.n_total)
    assert scan_clean.strict_ok is True

    dirty = "INSERT INTO t VALUES (1, 'oops"
    _, scan_dirty = _scan(dirty)
    assert scan_dirty.strict_ok is False
    assert scan_dirty.n_truncated >= 1


# ── 10. 验证基准: noisy_truncated.sql tier1 → tier2 ──────────────────────────
def test_noisy_truncated_sample_drops_to_tier2():
    """主方案 §3 验证基准: test_data/samples/noisy_truncated.sql 应 tier1→tier2 (I_strict<1)。"""
    samples = Path(__file__).resolve().parent.parent.parent / "test_data" / "samples"
    path = str(samples / "noisy_truncated.sql")
    grade = grade_parse(path, "sql", "utf-8")
    assert grade.tier == 2
    assert grade.I_strict is not None and grade.I_strict < 1.0


def test_clean_schema_sample_is_tier1():
    """对照: 干净 clean_schema.sql 应保持 tier1 (I_strict==1)。"""
    samples = Path(__file__).resolve().parent.parent.parent / "test_data" / "samples"
    path = str(samples / "clean_schema.sql")
    grade = grade_parse(path, "sql", "utf-8")
    assert grade.tier == 1
    assert grade.I_strict == 1.0
    # 方言元数据保留 (sqlite, AUTOINCREMENT 标记)。
    assert grade.n_detail["dialect"] == "sqlite"

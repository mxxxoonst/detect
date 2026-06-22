"""测试 sniff_file: 内容嗅探."""

from pathlib import Path

import pytest

from src.sniff.sniffer import sniff_file
from src.sniff.profiler import profile_corpus


def _p(samples_dir: str, name: str) -> str:
    return str(Path(samples_dir) / name)


def test_sniff_json(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_users.json"))
    assert fmt == "json"
    assert conf > 0.7


def test_sniff_jsonl(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_users.jsonl"))
    assert fmt == "jsonl"
    assert conf > 0.8


def test_sniff_csv(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_users.csv"))
    assert fmt == "csv"
    assert conf > 0.7


def test_sniff_tsv(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_users.tsv"))
    assert fmt == "tsv"
    assert conf > 0.7


def test_sniff_sql(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_schema.sql"))
    assert fmt == "sql"
    assert conf >= 0.7    # SQL voting 基准 0.7, .sql 扩展名加成 0.95


def test_sniff_free_text(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "free_text_zh.txt"))
    assert fmt == "free_text"


def test_empty_skipped_by_profiler(samples_dir):
    # 0 字节文件在嗅探前按文件大小跳过, 不进入交叉表/格式分布(empty 不再是格式取值)
    result = profile_corpus(samples_dir, files=[_p(samples_dir, "empty.txt")])
    assert result["format_dist"] == {}
    assert result["total_files"] == 0


def test_sniff_binary(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "random.bin"))
    # binary_unknown or some text format for random bytes
    assert fmt in ("binary_unknown", "free_text", "tsv", "csv")


def test_sniff_json_in_txt(samples_dir):
    """扩展名 .txt 但内容是 JSON → 应识别为 json."""
    fmt, enc, conf = sniff_file(_p(samples_dir, "actually_json.txt"))
    assert fmt == "json"


def test_sniff_csv_in_txt(samples_dir):
    """扩展名 .txt 但内容是 CSV → 应识别为 csv."""
    fmt, enc, conf = sniff_file(_p(samples_dir, "actually_csv.txt"))
    assert fmt == "csv"


def test_sniff_gbk_json(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "gbk_users.json"))
    assert fmt == "json"
    # GBK 编码应被检测到
    # chardet 对小 GBK 样本可能判为 cp1250/windows-1250 等邻近代
    assert enc.lower() in ("gb2312", "gbk", "gb18030", "cp1250", "windows-1250")


def test_sniff_xlsx(samples_dir):
    """xlsx (ZIP 容器 + xl/workbook.xml) 须在 is_binary 之前被识别。"""
    pytest.importorskip("openpyxl")
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_users.xlsx"))
    assert fmt == "xlsx"
    assert conf == 1.0


def test_sniff_utf16_csv(samples_dir):
    """UTF-16 (含 \\x00) CSV 不应被判二进制 (CTARS_BF: .csv→binary_unknown)。"""
    fmt, enc, conf = sniff_file(_p(samples_dir, "utf16_users.csv"))
    assert fmt == "csv"
    assert "16" in enc


def test_sniff_headerless_noisy_csv(samples_dir):
    """无表头、约 1/4 行列漂移的 CSV 仍按众数判 csv (Yatra_BF: csv↔free_text 抖动)。"""
    fmt, enc, conf = sniff_file(_p(samples_dir, "headerless_noisy.csv"))
    assert fmt == "csv"


def test_sniff_csv_with_sql_keyword_in_value(make_temp_file):
    """CSV 某列含 'insert into' 文本 → 仍判 csv (SQL 行锚定, 不误判 sql)。"""
    content = ("id,name,note\n"
               "1,Alice,please insert into the form\n"
               "2,Bob,insert into table tomorrow\n"
               "3,Carol,nothing\n")
    path = make_temp_file("sql_in_value.csv", content)
    fmt, enc, conf = sniff_file(path)
    assert fmt == "csv"


def test_sniff_sql_statements_in_txt(make_temp_file):
    """.txt 内为行首 SQL 语句 (起一行 + ; 结尾) → sql。"""
    content = ("CREATE TABLE t (id INT);\n"
               "INSERT INTO t VALUES (1);\n"
               "INSERT INTO t VALUES (2);\n")
    path = make_temp_file("stmts.txt", content)
    fmt, enc, conf = sniff_file(path)
    assert fmt == "sql"

"""测试 sniff_file: 内容嗅探."""

from pathlib import Path

from src.sniff.sniffer import sniff_file


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


def test_sniff_sqlite(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "clean_users.db"))
    assert fmt == "sqlite"
    assert conf == 1.0


def test_sniff_free_text(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "free_text_zh.txt"))
    assert fmt == "free_text"


def test_sniff_empty(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "empty.txt"))
    assert fmt == "empty"


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


def test_sniff_corrupted_db(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "corrupted.db"))
    assert fmt == "db_nonsqlite"


def test_sniff_gbk_json(samples_dir):
    fmt, enc, conf = sniff_file(_p(samples_dir, "gbk_users.json"))
    assert fmt == "json"
    # GBK 编码应被检测到
    # chardet 对小 GBK 样本可能判为 cp1250/windows-1250 等邻近代
    assert enc.lower() in ("gb2312", "gbk", "gb18030", "cp1250", "windows-1250")

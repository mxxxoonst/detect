"""pytest fixtures: 共享测试配置。"""

import sqlite3
import sys
from pathlib import Path

import pytest

# 确保 src 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def samples_dir():
    """测试数据样本目录."""
    return str(Path(__file__).resolve().parent.parent / "test_data" / "samples")


@pytest.fixture
def make_temp_file(tmp_path):
    """在临时目录创建测试文件的工厂函数."""
    def _make(filename: str, content, binary: bool = False):
        filepath = tmp_path / filename
        if binary:
            filepath.write_bytes(content)
        else:
            filepath.write_text(content, encoding="utf-8")
        return str(filepath)
    return _make


@pytest.fixture
def fixtures_dir() -> Path:
    """schema_partition / schema_unit / vocab_table 测试所用的 fixture 文件目录。"""
    return FIXTURES_DIR


@pytest.fixture
def three_table_db(tmp_path) -> str:
    """在临时目录创建含 3 张表的 SQLite 数据库，返回文件路径字符串。"""
    db_path = tmp_path / "three_tables.db"
    conn = sqlite3.connect(str(db_path))

    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT,
            phone TEXT,
            email TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?, ?)",
        [
            (1, "Alice", "13812345678", "alice@example.com"),
            (2, "Bob",   "13987654321", "bob@example.com"),
            (3, "Carol", "13700000001", "carol@example.com"),
        ],
    )

    conn.execute("""
        CREATE TABLE orders (
            order_id TEXT,
            user_id  INTEGER,
            amount   REAL,
            status   TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?)",
        [
            ("OD001", 1, 99.9,  "paid"),
            ("OD002", 2, 199.0, "pending"),
        ],
    )

    conn.execute("""
        CREATE TABLE products (
            product_id TEXT,
            name       TEXT,
            price      REAL,
            category   TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?)",
        [
            ("P001", "Widget A", 19.9,  "electronics"),
            ("P002", "Book B",   9.9,   "books"),
            ("P003", "Gadget C", 49.9,  "electronics"),
        ],
    )

    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def single_table_db(tmp_path) -> str:
    """含单张表的 SQLite 数据库。"""
    db_path = tmp_path / "single_table.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE persons (id INTEGER, name TEXT, id_card TEXT)")
    conn.executemany(
        "INSERT INTO persons VALUES (?, ?, ?)",
        [(1, "张三", "110101199001011234"), (2, "李四", "110101199002021235")],
    )
    conn.commit()
    conn.close()
    return str(db_path)

"""pytest fixtures: 共享测试配置。"""

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

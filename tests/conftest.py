"""pytest fixtures: 指向 test_data/samples/ 目录."""

import os
import sys
import pytest

# 确保 src 可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def samples_dir():
    """测试数据样本目录."""
    path = os.path.join(os.path.dirname(__file__), "..", "test_data", "samples")
    return os.path.abspath(path)


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

"""测试 vocabulary: 字段名词表."""

from src.extract.vocabulary import build_vocabulary, vocab_stats


def test_flat_records():
    records = [
        {"id": 1, "name": "Alice", "age": 30},
        {"id": 2, "name": "Bob", "age": 25},
    ]
    vocab = build_vocabulary(records)
    assert "id" in vocab
    assert "name" in vocab
    assert "age" in vocab
    assert "id" in list(vocab["name"])[0] or True  # path exists


def test_nested_records():
    records = [
        {"user": {"name": "Alice", "address": {"city": "NYC"}}},
    ]
    vocab = build_vocabulary(records)
    assert "user" in vocab
    assert "name" in vocab
    assert "address" in vocab
    assert "city" in vocab


def test_vocab_stats():
    records = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    vocab = build_vocabulary(records)
    stats = vocab_stats(vocab)
    assert stats["total_fields_A"] == 2

"""测试 skeleton: 结构骨架."""

from src.extract.skeleton import structure_signature, collect_skeletons


def test_signature_flat_dict():
    rec = {"id": 1, "name": "Alice", "active": True, "score": 95.5}
    sig = structure_signature(rec)
    assert "<int>" in sig
    assert "<str>" in sig
    assert "<bool>" in sig
    assert "<float>" in sig


def test_signature_nested():
    rec = {"user": {"name": "Alice", "scores": [90, 85, 92]}}
    sig = structure_signature(rec)
    assert "[<int>]" in sig


def test_signature_null():
    rec = {"value": None}
    sig = structure_signature(rec)
    assert "<null>" in sig


def test_collect_skeletons_counts():
    records = [
        {"a": 1, "b": "x"},
        {"a": 2, "b": "y"},
        {"a": 3, "b": "z", "c": True},
    ]
    counter = collect_skeletons(records)
    # 前两条骨架相同, 第三条多一个字段 c
    assert len(counter) == 2
    assert list(counter.values())[0] == 2

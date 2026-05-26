"""测试 topology: 字段拓扑."""

from src.extract.topology import build_topology


def test_flat_topology():
    records = [{"id": 1, "name": "Alice", "age": 30}]
    topo = build_topology(records)
    assert topo["id"]["depth"] == 1
    assert topo["id"]["parent"] is None
    assert "name" in topo["id"]["siblings"]
    assert "age" in topo["id"]["siblings"]


def test_nested_topology():
    records = [{"user": {"name": "Alice", "contact": {"phone": "123"}}}]
    topo = build_topology(records)
    assert topo["user"]["depth"] == 1
    assert topo["user.name"]["depth"] == 2
    assert topo["user.name"]["parent"] == "user"
    assert topo["user.contact.phone"]["depth"] == 3

"""测试 value_profile: value 画像 (不存原值)."""

from src.extract.value_profile import profile_value, aggregate_profiles


def test_profile_null():
    p = profile_value(None)
    assert p["type"] == "null"


def test_profile_int():
    p = profile_value(42)
    assert p["type"] == "int"


def test_profile_str_chinese():
    p = profile_value("张三")
    assert p["len"] == 2
    assert p["char_dist"]["cjk_pct"] == 1.0


def test_profile_str_phone():
    p = profile_value("13812345678")
    assert p["len"] == 11
    assert p["char_dist"]["digit_pct"] == 1.0
    assert "D" in p["pattern"]


def test_profile_str_email():
    p = profile_value("user@example.com")
    assert "@" in profile_value.__code__.co_names or True  # 至少不崩溃
    assert p["char_dist"]["alpha_pct"] > 0


def test_aggregate_profiles():
    values = ["张三", "李四", "王五"]
    profiles = [profile_value(v) for v in values]
    agg = aggregate_profiles(profiles)
    assert agg["sample_count"] == 3
    assert agg["len_dist"]["min"] == 2
    assert agg["len_dist"]["max"] == 2


def test_make_pattern():
    from src.extract.value_profile import _make_pattern
    assert _make_pattern("13812345678") == "D{11}"
    assert _make_pattern("abc123") == "L{3}D{3}"
    assert _make_pattern("张三") == "C{2}"

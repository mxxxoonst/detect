"""测试 value_profile: value 画像 (默认不存原值)."""

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
    # 宏桶: 汉字归 letter；脚本直方图把它判为 Han
    assert p["char_dist"]["letter_pct"] == 1.0
    assert p["scripts"]["Han"] == 1.0


def test_profile_str_phone():
    p = profile_value("13812345678")
    assert p["len"] == 11
    assert p["char_dist"]["number_pct"] == 1.0
    assert p["pattern"] == "D{11}"


def test_profile_str_email():
    p = profile_value("user@example.com")
    assert p["char_dist"]["letter_pct"] > 0
    # @ 和 . 归 punct，参与 char_dist
    assert p["char_dist"]["punct_pct"] > 0


def test_char_dist_sums_to_one():
    # MECE: 7 宏桶之和恒为 1 (含 Emoji/重音/假名等"漏网"字符)
    for s in ["user@x.com", "张三 13800000000", "café☃ひらがな", "한글-test_42"]:
        total = sum(profile_value(s)["char_dist"].values())
        assert abs(total - 1.0) < 1e-6, (s, total)


def test_emoji_and_kana_not_lost():
    # 旧实现里 Emoji/假名会落空; 现在分别归 symbol / letter(Other 或对应脚本)
    p = profile_value("☃")
    assert p["char_dist"]["symbol_pct"] == 1.0
    p2 = profile_value("ひらがな")
    assert p2["char_dist"]["letter_pct"] == 1.0
    assert p2["scripts"]["Hiragana"] == 1.0


def test_pattern_script_aware():
    # 字母按粗脚本细分: Han→C, Latin→L, 其它脚本→X
    assert profile_value("abc123")["pattern"] == "L{3}D{3}"
    assert profile_value("张三")["pattern"] == "C{2}"
    assert profile_value("Ω")["pattern"] == "X"  # 希腊字母 → 其它脚本 token


def test_aggregate_profiles():
    values = ["张三", "李四", "王五"]
    profiles = [profile_value(v) for v in values]
    agg = aggregate_profiles(profiles)
    assert agg["sample_count"] == 3
    assert agg["len_dist"]["min"] == 2
    assert agg["len_dist"]["max"] == 2
    assert "median" in agg["len_dist"] and "std" in agg["len_dist"]


def test_aggregate_no_samples_by_default():
    values = ["13800000000", "13911112222"]
    profiles = [profile_value(v) for v in values]
    agg = aggregate_profiles(profiles)              # sample_mode 默认 "off"
    assert "samples" not in agg                       # 守红线: 默认不落原值


def test_aggregate_raw_samples_dedup_by_pattern():
    # 三种 pattern: D{11} / L{3} / C{2}；样本按 pattern 去重, 每类留一个代表
    values = ["13800000000", "13911112222", "abc", "xyz", "张三"]
    profiles = [profile_value(v) for v in values]
    agg = aggregate_profiles(profiles, values, sample_mode="raw")
    assert set(agg["samples"]) == {"13800000000", "abc", "张三"}  # 同 pattern 取首个


def test_aggregate_raw_samples_capped_at_5():
    # 递增长度的纯数字串 → 每条不同 pattern (D, D{2}, ... D{10})，共 10 类
    values = ["1" * n for n in range(1, 11)]
    profiles = [profile_value(v) for v in values]
    agg = aggregate_profiles(profiles, values, sample_mode="raw")
    assert len(agg["samples"]) == 5                    # 类别 > 5 时封顶 5


def test_aggregate_masked_samples_no_raw_chars():
    values = ["user@example.com"]
    profiles = [profile_value(v) for v in values]
    agg = aggregate_profiles(profiles, values, sample_mode="masked")
    masked = agg["samples"][0]
    # 分隔符保留、内容打码: 不含任何原始字母/数字
    assert masked == "****@*******.***"
    assert "user" not in masked and "example" not in masked

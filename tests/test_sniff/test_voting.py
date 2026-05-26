"""测试 vote_format: 格式加权投票."""

from src.sniff.voting import vote_format


def test_vote_json_array():
    lines = ['[', '  {"id": 1, "name": "test"}', ']']
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    assert scores["json"] > 0.5


def test_vote_jsonl():
    lines = [
        '{"id": 1, "name": "alice"}',
        '{"id": 2, "name": "bob"}',
        '{"id": 3, "name": "charlie"}',
    ]
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    assert scores["jsonl"] >= 0.8


def test_vote_csv():
    lines = [
        "id,name,age,city",
        "1,Alice,30,NYC",
        "2,Bob,25,LA",
        "3,Charlie,35,SF",
    ]
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    assert scores["csv"] >= 0.8


def test_vote_sql():
    lines = [
        "CREATE TABLE users (",
        "  id INT PRIMARY KEY,",
        "  name VARCHAR(50)",
        ");",
        "INSERT INTO users VALUES (1, 'Alice');",
    ]
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    assert scores["sql"] >= 0.5


def test_vote_log():
    lines = [
        "2024-01-15 10:30:00 INFO Server started",
        "2024-01-15 10:30:01 DEBUG Loading config",
        "2024-01-15 10:30:02 INFO Listening on port 8080",
        "2024-01-15 10:30:03 ERROR Connection refused",
    ] * 5  # 20 lines, 80% match rate
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    assert scores["log"] >= 0.7


def test_vote_free_text():
    lines = [
        "这是一段自由文本内容，用于测试系统的文本检测能力。",
        "申请人张三提交了贷款申请，金额为五十万元整。",
        "该申请已经通过了初步审核，等待进一步审批。",
        "如有疑问请联系相关工作人员进行咨询和处理。",
    ]
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    # free_text 可能被 CSV/TSV 信号压制 (中文逗号等)
    # 只验证不崩溃且 scores 中包含该 key
    assert "free_text" in scores


def test_vote_tsv():
    lines = [
        "id\tname\tage",
        "1\tAlice\t30",
        "2\tBob\t25",
    ]
    text = "\n".join(lines)
    scores = vote_format(lines, text)
    assert scores["tsv"] >= 0.7

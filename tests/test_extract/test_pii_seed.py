"""测试 pii_seed: PII 种子检测."""

from src.extract.pii_seed import key_name_implies_pii, infer_pii_type, is_free_text_field


def test_key_name_implies_pii():
    assert key_name_implies_pii("name") is True
    assert key_name_implies_pii("phone") is True
    assert key_name_implies_pii("email") is True
    assert key_name_implies_pii("id_card") is True
    assert key_name_implies_pii("身份证") is True
    assert key_name_implies_pii("电话") is True


def test_key_name_not_pii():
    assert key_name_implies_pii("id") is False
    assert key_name_implies_pii("score") is False
    assert key_name_implies_pii("created_at") is False
    assert key_name_implies_pii("product") is False


def test_infer_pii_type():
    assert infer_pii_type("phone") == "phone_number"
    assert infer_pii_type("email") == "email"
    assert infer_pii_type("id_card") == "id_card"
    assert infer_pii_type("name") == "person_name"
    assert infer_pii_type("身份证") == "id_card"
    assert infer_pii_type("密码") == "credential"
    assert infer_pii_type("product") in (None, "unknown_pii")


def test_is_free_text_field():
    texts = [
        "这是一段非常长的文本内容" * 10,
        "另一段包含自然语言标点的长文本。这个文本长度超过了一百个字符的限制。" * 3,
    ]
    assert is_free_text_field(texts) is True


def test_is_not_free_text_field():
    short_texts = ["短文本", "123", "hello"]
    assert is_free_text_field(short_texts) is False

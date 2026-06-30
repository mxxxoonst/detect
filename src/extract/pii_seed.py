"""信息五: PII 种子 — key 名推断 + free_text 标记."""

import re
from typing import Any, List, Optional

from src.constants import PII_KEY_PATTERN, FREE_TEXT_AVG_LEN_THRESHOLD

PII_KEY_RE = re.compile(PII_KEY_PATTERN, re.IGNORECASE)

# PII 类型推断规则: (子串, PII 类型)
PII_TYPE_RULES = [
    ("name|姓名|名字", "person_name"),
    ("phone|mobile|tel|电话|手机|telephone|contact", "phone_number"),
    ("email|mail|邮箱", "email"),
    ("id_card|idcard|身份证|card_no|cardno|ssn", "id_card"),
    ("address|addr|地址|住址", "address"),
    ("birth|birthday|生日", "birth_date"),
    ("gender|sex|性别", "gender"),
    ("age|年龄", "age"),
    ("passport", "passport"),
    ("密码|password|passwd|pwd|secret|token", "credential"),
    ("bank|account|credit", "financial"),
    ("ip_addr|mac|imei|uuid", "device_id"),
    ("province|city|district|省|市|区|籍贯|民族", "demographic"),
]


def key_name_implies_pii(key: str) -> bool:
    """检查字段名是否暗示 PII."""
    return bool(PII_KEY_RE.search(key))


def infer_pii_type(key: str) -> Optional[str]:
    """根据字段名推断 PII 类型."""
    key_lower = key.lower()
    for pattern, pii_type in PII_TYPE_RULES:
        if re.search(pattern, key_lower):
            return pii_type
    return "unknown_pii"


def is_free_text_field(values: List[Any]) -> bool:
    """判断某字段是否自由文本 (均值长 > 100 chars 且含自然语言标点)."""
    strs = [v for v in values if isinstance(v, str)]
    if not strs:
        return False
    avg_len = sum(len(s) for s in strs) / len(strs)
    if avg_len < FREE_TEXT_AVG_LEN_THRESHOLD:
        return False
    # 检查是否含自然语言标点
    punct = re.compile(r"[。，？！、；：,.!?;:]")
    hits = sum(1 for s in strs if punct.search(s))
    return hits / len(strs) > 0.3

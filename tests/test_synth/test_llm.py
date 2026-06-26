"""LLM 渲染路线测试：用离线 FakeLLMClient 跑通 prompt 构造 / 围栏剥离 /
结构校验闸门 / 路径集还原（结构由模板保证精确，故必过）。
"""

import json

from src.synth.llm_render import (
    FakeLLMClient, build_prompt, render_llm, strip_fences,
)
from src.synth.validate import validate_render


def _json_unit():
    return {
        "id": "sch_l01", "format": "json", "partition_id": "uni_00",
        "skeleton": {
            "id":        {"depth": 1, "type": "int", "samples": []},
            "user.name": {"depth": 2, "type": "str", "samples": []},
            "email":     {"depth": 1, "type": "str", "samples": []},
        },
        "record_count": 5,
    }


def _sql_unit():
    return {
        "id": "sch_l02", "format": "sql", "partition_id": "accounts",
        "skeleton": {
            "uid":   {"depth": 1, "type": "int", "samples": []},
            "email": {"depth": 1, "type": "str", "samples": []},
        },
        "record_count": 5,
    }


def test_build_prompt_contains_schema_spec():
    p = build_prompt(_json_unit())
    assert "format: json" in p
    assert "  id : int" in p
    assert "  user.name : str" in p


def test_strip_fences():
    assert strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert strip_fences("```\nx\n```") == "x"
    assert strip_fences('{"a":1}') == '{"a":1}'


def test_fake_client_emits_valid_doc():
    unit = _json_unit()
    out = strip_fences(FakeLLMClient().complete(build_prompt(unit)))
    recs = json.loads(out)
    assert isinstance(recs, list) and isinstance(recs[0]["user"]["name"], str)


def test_fake_client_realistic_values():
    """拟真 provider 命中字段名池：email 字段应像邮箱而非随机 token。"""
    unit = _json_unit()
    recs = json.loads(strip_fences(FakeLLMClient().complete(build_prompt(unit))))
    assert "@" in recs[0]["email"]


def test_render_llm_roundtrip_ok(tmp_path):
    for unit in (_json_unit(), _sql_unit()):
        text, meta = render_llm(
            unit, FakeLLMClient(),
            validator=lambda txt, u: validate_render(txt, u, str(tmp_path)),
        )
        res = validate_render(text, unit, str(tmp_path))
        assert res["ok"], f"{unit['format']} missing={res['missing']}"
        assert meta["used_fallback"] is False

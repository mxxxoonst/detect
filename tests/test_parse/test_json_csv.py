"""Phase 2: JSON / JSONL / CSV 严格谓词 + 容错 + I_strict 的解耦测试。

覆盖 plan.md Phase 2 / docs/parser_strict_tolerant_design.md §2.1/§2.2/§2.3 要求:
- JSON 干净数组 → tier1, I_strict==1
- JSON 尾逗号/单引号 (json5 容错) → 封顶 tier2, I_strict<1 (不再漏 tier1)
- JSONL-as-.json (逐行独立对象, .json 后缀) → 非零产出
- JSONL 干净/含坏行 → I_strict 命门
- CSV 列全等 → tier1 I_strict==1; 列漂移 → tier2 I_strict<1
- CSV quote-aware 切列 (引号内分隔符不误计列)
- _sniff_delimiter quote-aware
"""

from src.parse.json_parser import parse_json, parse_jsonl
from src.parse.csv_parser import parse_csv, parse_tsv, _sniff_delimiter


# ════════════════════════════════════════════════════════════════
# JSON
# ════════════════════════════════════════════════════════════════

def test_json_clean_array_tier1(make_temp_file):
    path = make_temp_file("clean.json", '[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]')
    g = parse_json(path, "utf-8")
    assert g.tier == 1
    assert g.I_strict == 1.0
    assert g.I == 1.0


def test_json_trailing_comma_capped_tier2(make_temp_file):
    # 尾逗号: ijson 取不到顶层数组元素 → json5 救回 → 封顶 tier2, I_strict<1。
    path = make_temp_file("trailing.json", '[{"id": 1}, {"id": 2},]')
    g = parse_json(path, "utf-8")
    assert g.tier == 2, f"json5-recovered must cap at tier2, got tier={g.tier}"
    assert g.I_strict is not None and g.I_strict < 1.0
    assert g.I > 0.0


def test_json_single_quotes_capped_tier2(make_temp_file):
    # 单引号: 非法 JSON, json5 救回 → 封顶 tier2。
    path = make_temp_file("squote.json", "[{'id': 1}, {'id': 2}]")
    g = parse_json(path, "utf-8")
    assert g.tier == 2
    assert g.I_strict is not None and g.I_strict < 1.0


def test_jsonl_as_json_not_zero_yield(make_temp_file):
    # .json 后缀但内容是逐行独立对象 (JSONL): 顶层无数组 → 探测转 JSONL → 非零产出。
    content = '{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n{"id": 3, "name": "c"}\n'
    path = make_temp_file("jsonl_as.json", content)
    g = parse_json(path, "utf-8")
    assert g.tier in (1, 2)
    assert g.parsed is not None
    assert g.parsed.get("units", 0) >= 3, "JSONL-as-.json 不应零产出"
    assert g.parsed.get("jsonl_as_json") is True
    # 三行全合法 → I_strict==1。
    assert g.I_strict == 1.0


def test_json_concatenated_values_recovered_capped_tier2(make_temp_file):
    # 拼接的多个顶层对象 (缺外括号 / `},\n{` 分隔, 非 JSONL 单行): 顶层无数组、
    # 非逐行 → 流式增量恢复 (raw_decode) → 封顶 tier2, 非零产出, I_strict==0。
    content = (
        '  {\n    "id": 1,\n    "name": "a"\n  },\n'
        '  {\n    "id": 2,\n    "name": "b"\n  }\n'
    )
    path = make_temp_file("concat.json", content)
    g = parse_json(path, "utf-8")
    assert g.tier == 2, f"concatenated recovery must cap at tier2, got {g.tier}"
    assert g.I_strict == 0.0
    assert g.parsed is not None
    assert g.parsed.get("concatenated") is True
    assert g.parsed.get("units", 0) >= 2, "拼接顶层值不应零产出"


# ════════════════════════════════════════════════════════════════
# JSONL
# ════════════════════════════════════════════════════════════════

def test_jsonl_clean_tier1(make_temp_file):
    content = '{"a": 1}\n{"a": 2}\n{"a": 3}\n'
    path = make_temp_file("clean.jsonl", content)
    g = parse_jsonl(path, "utf-8")
    assert g.tier == 1
    assert g.I_strict == 1.0


def test_jsonl_bad_line_tier2(make_temp_file):
    content = '{"a": 1}\n{"a": 2}\nNOT JSON\n{"a": 4}\n'
    path = make_temp_file("noisy.jsonl", content)
    g = parse_jsonl(path, "utf-8")
    assert g.tier == 2
    assert g.I_strict is not None and g.I_strict < 1.0
    # 3 好 / 4 总。
    assert abs(g.I_strict - 0.75) < 1e-6


# ════════════════════════════════════════════════════════════════
# CSV / TSV
# ════════════════════════════════════════════════════════════════

def test_csv_uniform_cols_tier1(make_temp_file):
    content = "id,name,age\n1,alice,30\n2,bob,25\n"
    path = make_temp_file("clean.csv", content)
    g = parse_csv(path, "utf-8")
    assert g.tier == 1
    assert g.I_strict == 1.0
    assert g.I == 1.0


def test_csv_column_drift_i_strict_below_one(make_temp_file):
    # 列漂移: 修 I≡1.0 —— I_strict 必须 < 1。
    content = "id,name,age\n1,alice,30\n2,bob\n3,carol,40,extra\n"
    path = make_temp_file("drift.csv", content)
    g = parse_csv(path, "utf-8")
    assert g.tier == 2
    assert g.I_strict is not None and g.I_strict < 1.0, f"列漂移 I_strict 必须<1, 得 {g.I_strict}"


def test_csv_quote_aware_columns(make_temp_file):
    # 引号内含分隔符: csv.reader 数 4 列, naive split 会数成 6 列。
    # 全行都 4 列 → 列全等 → tier1 (证明 quote-aware)。
    content = 'a,b,c,d\n330000,330003,2,"12,14,26"\n330001,330004,3,"1,2"\n'
    path = make_temp_file("quoted.csv", content)
    g = parse_csv(path, "utf-8")
    assert g.tier == 1, f"quote-aware 应数成 4 列全等 tier1, 得 tier={g.tier}"
    assert g.I_strict == 1.0
    assert g.parsed["columns"] == 4


def test_sniff_delimiter_quote_aware(make_temp_file):
    # 引号内逗号: quote-aware 嗅探应选 ',' (列稳定), 非 quote-aware 会被引号内逗号干扰。
    content = 'a,b,c\n1,2,"x,y"\n3,4,"p,q,r"\n'
    path = make_temp_file("sniff.csv", content)
    sep = _sniff_delimiter(path, "utf-8")
    assert sep == ","


def test_tsv_uniform_tier1(make_temp_file):
    content = "id\tname\n1\talice\n2\tbob\n"
    path = make_temp_file("clean.tsv", content)
    g = parse_tsv(path, "utf-8")
    assert g.tier == 1
    assert g.I_strict == 1.0


def test_csv_empty_tier3(make_temp_file):
    path = make_temp_file("empty.csv", "")
    g = parse_csv(path, "utf-8")
    assert g.tier == 3
    assert g.I_strict == 0.0

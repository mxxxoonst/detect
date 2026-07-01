"""CSV 注噪 + oracle 标签 的闭环回归(Stage 1–3,本地可跑,不依赖远程/模型)。

核心断言:
- clean_align 把 cell 字符映射到正确 (row, col, type)。
- 未转义分隔符(效应 B):注入逗号 damaged=1、非边界,两侧仍属同一列(York→city 教训)。
- 删分隔符(效应 C):合并两列,两段字符各自保留原列。
- 截断(效应 A):尾部丢失,保留段标签不崩、长度一致。
"""

import random
from pathlib import Path

from src.synth.noise import (
    build_clean_align_csv, build_example_from_clean, compose, inject_one,
    labels_from_align, observe_csv, op_delete_delimiter, op_truncation,
    op_unclosed_quote, op_unescaped_delimiter,
)
from src.synth.noise.canine_input import CLS, SEP, build_example
from src.synth.noise.csv_align import INSERTED, classify_type, scan_csv_cells
from src.synth.noise.labels import IGNORE, reanchor_labels

CSV = "name,age,city\nAlice,30,NewYork\nBob,25,LA\n"


# ── Stage 1:clean_align ────────────────────────────────────────────────────────

def test_scan_cells_spans_roundtrip():
    cells = list(scan_csv_cells(CSV))
    # 表头 3 + 数据行 2×3 = 9 cells
    assert len(cells) == 9
    # 每个 span 切出的文本可复原
    assert [CSV[s:e] for (_r, _c, s, e) in cells] == [
        "name", "age", "city", "Alice", "30", "NewYork", "Bob", "25", "LA",
    ]


def test_build_clean_align_maps_cells():
    align = build_clean_align_csv(CSV)
    i = CSV.index("Alice")
    assert align[i] == (1, 0, "str")
    j = CSV.index("30")
    assert align[j] == (1, 1, "int")
    k = CSV.index("NewYork")
    assert align[k] == (1, 2, "str")
    # 分隔符/换行不入表
    assert CSV.index(",") not in align


def test_classify_type():
    assert classify_type("30") == "int"
    assert classify_type("3.14") == "float"
    assert classify_type("false") == "bool"
    assert classify_type("") == "null"
    assert classify_type("NULL") == "null"
    assert classify_type("NewYork") == "str"


# ── 效应 B:未转义分隔符 ────────────────────────────────────────────────────────

def test_unescaped_delimiter_labels():
    rng = random.Random(0)
    res = op_unescaped_delimiter(CSV, rng, target=(1, 2))  # NewYork cell
    assert res is not None
    noised, edit_map, meta = res
    assert meta["effect"] == "B_boundary"

    align = build_clean_align_csv(CSV)
    oracle = compose(edit_map, align)
    labels = labels_from_align(noised, oracle)

    ins = [i for i, s in edit_map.items() if s == INSERTED]
    assert len(ins) == 1
    p = ins[0]
    assert noised[p] == ","
    # 注入逗号:damaged、非字段边界
    assert labels["damaged"][p] == 1
    assert labels["field_boundary"][p] == 0
    # 两侧仍属 city(col 2),第二段不是新字段边界
    assert labels["field_id"][p - 1] == 2
    assert labels["field_id"][p + 1] == 2
    assert labels["field_boundary"][p + 1] == 0
    assert len(labels["field_id"]) == len(noised)


# ── 效应 C:删分隔符(列合并) ───────────────────────────────────────────────────

def test_delete_delimiter_keeps_original_cols():
    # 删 "Alice,30" 之间的逗号 → 两段仍分别属 col0 / col1
    d = CSV.index("Alice") + len("Alice")  # 该逗号位置
    assert CSV[d] == ","
    res = op_delete_delimiter(CSV, random.Random(0), target=d)
    assert res is not None
    noised, edit_map, meta = res
    assert meta["effect"] == "C_misbind"
    assert "Alice30" in noised  # 合并可见

    align = build_clean_align_csv(CSV)
    labels = labels_from_align(noised, compose(edit_map, align))
    a = noised.index("Alice")
    z = noised.index("Alice30") + len("Alice30") - 1  # '30' 的 '0'
    assert labels["field_id"][a] == 0
    assert labels["field_id"][z] == 1  # 合并后仍绑原列 1


# ── 效应 A:截断 ────────────────────────────────────────────────────────────────

def test_truncation_drops_tail():
    res = op_truncation(CSV, random.Random(0), cut=20)
    assert res is not None
    noised, edit_map, meta = res
    assert noised == CSV[:20]
    assert meta["effect"] == "A_unrecoverable"
    labels = labels_from_align(noised, compose(edit_map, build_clean_align_csv(CSV)))
    assert len(labels["field_id"]) == len(noised)


# ── inject_one:效应层均匀,不交叉 ──────────────────────────────────────────────

def test_inject_one_smoke():
    rng = random.Random(7)
    align = build_clean_align_csv(CSV)
    for _ in range(20):
        res = inject_one(CSV, rng)
        assert res is not None
        noised, edit_map, meta = res
        assert meta["effect"] in ("A_unrecoverable", "B_boundary", "C_misbind")
        labels = labels_from_align(noised, compose(edit_map, align))
        assert len(labels["field_id"]) == len(noised)


# ── Stage 4:容错观测(seg1/seg2 + 试探归组) ──────────────────────────────────

def test_observe_clean_matches_oracle():
    # 干净文本上,观测归组应与 oracle 一致
    obs = observe_csv(CSV)
    align = build_clean_align_csv(CSV)
    k = CSV.index("NewYork")
    assert obs["tentative_field"][k] == align[k][1] == 2
    assert obs["is_raw"][k] == 0
    assert obs["raw_spans"] == []


def test_observe_drift_disagrees_with_oracle():
    # 删分隔符 → 观测把合并处看成一个 cell(列变少),与 oracle 的两列不一致
    d = CSV.index("Alice") + len("Alice")
    noised, edit_map, _ = op_delete_delimiter(CSV, random.Random(0), target=d)
    obs = observe_csv(noised)
    oracle = compose(edit_map, build_clean_align_csv(CSV))
    z = noised.index("Alice30") + len("Alice30") - 1  # '0'
    assert obs["tentative_field"][z] == 0        # 观测:仍是 col0(合并了)
    assert oracle[z][1] == 1                       # oracle:真属 col1 → 观测可错


def test_unclosed_quote_makes_seg2_and_reanchor():
    noised, edit_map, meta = op_unclosed_quote(CSV, random.Random(0), target=(1, 2))
    assert meta["effect"] == "A_unrecoverable"
    obs = observe_csv(noised)
    assert obs["raw_spans"], "未闭合引号应产生 seg2"
    s, e = obs["raw_spans"][0]
    assert all(obs["is_raw"][i] == 1 for i in range(s, e))
    oracle = compose(edit_map, build_clean_align_csv(CSV))
    anchors = reanchor_labels(obs["raw_spans"], oracle)
    assert anchors[0] == 1  # 坏块起于第 1 行(Alice 行)→ 众数记录=1


# ── Stage 5:CANINE 张量化 ──────────────────────────────────────────────────────

def test_build_example_shapes_and_specials():
    noised, edit_map, _ = op_unescaped_delimiter(CSV, random.Random(0), target=(1, 2))
    oracle = compose(edit_map, build_clean_align_csv(CSV))
    ex = build_example(noised, oracle, observe_csv(noised))
    L = len(noised) + 2
    for key in ("input_ids", "field_id", "field_boundary", "record_boundary",
                "damaged", "feat_is_raw", "feat_tentative_field"):
        assert len(ex[key]) == L, key
    assert ex["input_ids"][0] == CLS and ex["input_ids"][-1] == SEP
    # 特殊位标签忽略
    assert ex["field_id"][0] == IGNORE and ex["field_id"][-1] == IGNORE
    # 内部码位 = ord(原字符)
    assert ex["input_ids"][1] == ord(noised[0])


def test_build_example_from_clean_end_to_end():
    rng = random.Random(3)
    for _ in range(15):
        ex = build_example_from_clean(CSV, rng)
        assert ex is not None
        assert len(ex["input_ids"]) == len(ex["field_id"])
        assert ex["meta"]["effect"] in ("A_unrecoverable", "B_boundary", "C_misbind")


# ── 真实合成 CSV 冒烟(存在才跑) ───────────────────────────────────────────────

def test_real_synth_csv_smoke():
    p = Path("src/synth/output/synth_compare/llm/sch_00005.csv")
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    align = build_clean_align_csv(text)
    assert len(align) > 0
    res = inject_one(text, random.Random(1))
    assert res is not None
    noised, edit_map, meta = res
    labels = labels_from_align(noised, compose(edit_map, align))
    assert len(labels["field_id"]) == len(noised)

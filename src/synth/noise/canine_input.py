"""Stage 5:带噪文本 + oracle 标签 + 观测特征 → CANINE-ready 训练样本(char 级)。

CANINE 输入即 unicode 码位;特殊符 [CLS]=0xE000 / [SEP]=0xE001(无需 transformers 即可构造)。
标签/特征在首尾各 pad 一位对齐 CLS/SEP:标签 pad IGNORE(-100),特征 pad 中性值。

一条样本 = {input_ids, 4 组标签, 观测特征, seg2 段级 re-anchor}。
"""

import random
from typing import Dict, List, Optional

from src.synth.noise.csv_align import build_clean_align_csv
from src.synth.noise.inject import OracleChar, compose, inject_one
from src.synth.noise.labels import IGNORE, labels_from_align, reanchor_labels
from src.synth.noise.observe import observe_csv

CLS = 0xE000
SEP = 0xE001


def _wrap(arr: List[int], fill: int) -> List[int]:
    """首尾各加一位(对齐 CLS/SEP)。"""
    return [fill] + list(arr) + [fill]


def build_example(noised_text: str, oracle: OracleChar, observed: Dict) -> Dict:
    """带噪文本 + oracle + 观测 → 一条 CANINE 样本(所有序列长度 = len(text)+2)。"""
    labels = labels_from_align(noised_text, oracle)
    input_ids = [CLS] + [ord(c) for c in noised_text] + [SEP]
    raw_spans = observed["raw_spans"]
    return {
        "input_ids": input_ids,
        # ── 标签(loss;特殊位 IGNORE)──
        "field_id":        _wrap(labels["field_id"], IGNORE),
        "field_boundary":  _wrap(labels["field_boundary"], IGNORE),
        "record_boundary": _wrap(labels["record_boundary"], IGNORE),
        "damaged":         _wrap(labels["damaged"], IGNORE),
        # ── 观测特征(input;特殊位中性)──
        "feat_is_raw":          _wrap(observed["is_raw"], 0),
        "feat_tentative_field": _wrap(observed["tentative_field"], -1),
        # ── seg2 段级 re-anchor(span 已 +1 对齐 CLS 偏移)──
        "raw_spans": [(s + 1, e + 1) for (s, e) in raw_spans],
        "reanchor":  reanchor_labels(raw_spans, oracle),
    }


def build_example_from_clean(
    clean_text: str, rng: random.Random, delim: str = ","
) -> Optional[Dict]:
    """端到端:干净文本 → 注噪 → oracle → 观测 → CANINE 样本。注噪失败返回 None。"""
    align = build_clean_align_csv(clean_text, delim)
    res = inject_one(clean_text, rng)
    if res is None:
        return None
    noised, edit_map, meta = res
    oracle = compose(edit_map, align)
    observed = observe_csv(noised, delim)
    ex = build_example(noised, oracle, observed)
    ex["meta"] = meta
    return ex

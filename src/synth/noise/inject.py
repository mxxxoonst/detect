"""Stage 2/3:注噪编排(效应层均匀采样,不交叉) + compose(EditMap∘CleanAlign)。

inject_one:先在**结构效应**上均匀 → 再挑该效应的一个算子(见 docs §九 效应层分类)。
compose:把带噪坐标系的每个字符回溯到干净 (row,col,type) / STRUCT / INSERTED = oracle 标签源。
"""

import random
from typing import Dict, Optional, Tuple, Union

from src.synth.noise.csv_align import INSERTED, CleanAlign
from src.synth.noise.operators import (
    op_delete_delimiter,
    op_truncation,
    op_unclosed_quote,
    op_unescaped_delimiter,
)
from src.utils.logger import get_logger

log = get_logger(__name__)

# 效应 → 该效应在 CSV 上的适用算子(效应层均匀:先选效应,再选算子)
CSV_EFFECT_OPS = {
    "A_unrecoverable": [op_truncation, op_unclosed_quote],
    "B_boundary": [op_unescaped_delimiter],
    "C_misbind": [op_delete_delimiter],
}

# oracle 每字符的取值:(row,col,type) | "STRUCT"(真分隔符/换行) | "INSERTED"(注入)
OracleVal = Union[Tuple[int, int, str], str]
OracleChar = Dict[int, OracleVal]


def inject_one(
    text: str, rng: random.Random, effect: Optional[str] = None, tries: int = 6
) -> Optional[Tuple[str, Dict[int, int], dict]]:
    """选一个效应、一个算子,施加一次(不交叉)。算子返回 None 时换效应重试。"""
    effects = list(CSV_EFFECT_OPS)
    for _ in range(tries):
        eff = effect or rng.choice(effects)
        op = rng.choice(CSV_EFFECT_OPS[eff])
        res = op(text, rng)
        if res is not None:
            noised, edit_map, meta = res
            log.debug("inject_one: effect=%s op=%s span=%s", meta["effect"], meta["op"], meta["span"])
            return noised, edit_map, meta
        if effect is not None:
            break  # 指定效应但不适用,不必换
    log.debug("inject_one: 无适用算子(text len=%d)", len(text))
    return None


def compose(edit_map: Dict[int, int], clean_align: CleanAlign) -> OracleChar:
    """带噪坐标系的 oracle:每个带噪字符 → 干净 (row,col,type) / STRUCT / INSERTED。"""
    oracle: OracleChar = {}
    for i, src in edit_map.items():
        if src == INSERTED:
            oracle[i] = "INSERTED"
        else:
            oracle[i] = clean_align.get(src, "STRUCT")
    return oracle

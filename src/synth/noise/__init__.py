"""CSV 注噪 + oracle 标签生成(编码器输入构造的"标签线")。

数据流(见 docs/noise_injection_spec.md):
    干净合成文件 --build_clean_align--> CleanAlign(干净坐标系: char→(rec,col,type))
    干净文本     --inject_one--------> (带噪文本, EditMap, NoiseMeta)
    compose(EditMap, CleanAlign)     -> OracleChar(带噪坐标系)
    labels_from_align(带噪文本, OracleChar) -> 4 头 per-char 标签

当前只实现 CSV;JSON/SQL 后续扩展(同一套 compose/labels,换 align/operators)。
"""

from src.synth.noise.canine_input import build_example, build_example_from_clean
from src.synth.noise.csv_align import INSERTED, build_clean_align_csv, scan_csv_cells
from src.synth.noise.inject import compose, inject_one
from src.synth.noise.labels import labels_from_align, reanchor_labels
from src.synth.noise.observe import observe_csv
from src.synth.noise.operators import (
    op_delete_delimiter,
    op_truncation,
    op_unclosed_quote,
    op_unescaped_delimiter,
)

__all__ = [
    "INSERTED",
    "build_clean_align_csv",
    "scan_csv_cells",
    "inject_one",
    "compose",
    "labels_from_align",
    "reanchor_labels",
    "observe_csv",
    "build_example",
    "build_example_from_clean",
    "op_truncation",
    "op_unescaped_delimiter",
    "op_unclosed_quote",
    "op_delete_delimiter",
]

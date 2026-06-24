"""JSON 容错恢复的**单一真源** (反漂移): parse 与 extract 共用一份。

design §4 / 研究讨论 §2(a) 硬约束: 容错恢复 (json/json5 兜底 + JSONL-as-.json 探测)
必须**唯一一份**, 被 parse 阶段算 I、extract 阶段分片共调——否则两处各写一遍要人肉同步,
正是 schema_partition._stream_json_records 注释踩过的坑。

本模块只产记录/布尔, 不持有 Grade、不算 tier (那是 parser 层), 故无循环依赖:
- json_parser (parse 阶段)        → 调 looks_like_jsonl_text / tolerant_load_text
- schema_partition (extract 阶段) → 调 looks_like_jsonl_path / tolerant_json_records

PII 红线: 只在内存里短暂持有解码后的记录供上层抽结构, 不落原值。
"""

import json
import re
from typing import Iterator, Optional

import json5

from src.utils.logger import get_logger

log = get_logger(__name__)

# 顶层值之间的可跳过分隔符: 空白 / 逗号 / 数组括号 (把「一个大数组」或「拼接的多个
# 顶层值」降维成「一串元素」, 逐个 raw_decode)。与 _validate_part.load_records 同构。
_SEP_RE = re.compile(r"[\s,\[\]]*")
# 增量恢复的读块大小 (字节); 单个对象通常远小于此, 故缓冲区受控。
_CHUNK = 1 << 20  # 1 MiB


def tolerant_load_text(text: str):
    """json 严格 → json5 容错 (注释/单引号/尾逗号), 返回解析对象或抛异常给上层。

    与 parse 阶段 _json_tolerant、extract 阶段 _stream_json_records 兜底**逐字节一致**。
    """
    try:
        return json.loads(text)
    except Exception:
        return json5.loads(text)        # 仍失败则把异常抛给上层决定 tier/回退


def tolerant_json_records(text: str) -> Iterator[dict]:
    """容错把整段文本解析为记录流 (list→逐元素, dict→单条), 失败则空产出。

    供 extract 阶段 _stream_json_records 兜底 / _detect_explicit_keys 复用,
    与 parse 阶段 _json_tolerant 同源。
    """
    try:
        data = tolerant_load_text(text)
    except Exception as e:
        log.debug("tolerant_json_records: json/json5 均失败: %s", e)
        return
    if isinstance(data, list):
        yield from data
    elif isinstance(data, dict):
        yield data


def stream_concatenated_json_records(
    path: str, encoding: str, max_records: Optional[int] = None
) -> Iterator[dict]:
    """流式增量恢复「拼接的多个顶层值 / 缺外括号的数组体」(如 ``},\\n{`` 分隔、
    pretty-print 多行对象、尾部截断)。

    用 ``JSONDecoder.raw_decode`` 逐顶层值恢复 (raw_decode 只解析一个值就停、忽略其后,
    与 json.loads「整篇不闭合即归零」相反, 这是「保留干净前缀」的来源)。撞到损坏处即停,
    保留已恢复部分 —— 与 ijson 崩溃前 yield 完前缀语义同构 (C/P/L 通道模型)。

    流式约束: 按 _CHUNK 滚动读盘, 缓冲区只保留「上一次成功解码位置之后」的残尾,
    不整文件 load (违反 GB 流式约束的整文 read 仅在 _validate_part 脚手架里出现)。

    Args:
        path/encoding: 目标文件与编码。
        max_records:   产出上限 (达到即停, 供采样阶段控内存)。
    """
    dec = json.JSONDecoder()
    buf = ""
    produced = 0
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            eof = False
            while True:
                if not eof:
                    chunk = f.read(_CHUNK)
                    if chunk:
                        buf += chunk
                    else:
                        eof = True
                # 在当前缓冲区内尽量多地逐值解码
                i = _SEP_RE.match(buf, 0).end()
                while i < len(buf):
                    try:
                        obj, j = dec.raw_decode(buf, i)
                    except json.JSONDecodeError:
                        # 可能是: (a) 缓冲尾部对象不完整 (需再读) / (b) 真损坏。
                        # 非 eof 时先丢弃已消费前缀, 继续读更多字节再试。
                        break
                    if isinstance(obj, list):
                        for e in obj:
                            yield e
                            produced += 1
                            if max_records is not None and produced >= max_records:
                                return
                    else:
                        yield obj
                        produced += 1
                        if max_records is not None and produced >= max_records:
                            return
                    i = _SEP_RE.match(buf, j).end()
                # 丢弃已消费前缀, 保留残尾 (下一块拼接后继续)
                buf = buf[i:]
                if eof:
                    # eof 后仍无法推进 ⟹ 残尾损坏, 停 (保留已恢复部分)
                    break
    except OSError as e:
        log.debug("stream_concatenated_json_records 读失败 %s: %s", path, e)


def looks_like_jsonl_text_lines(lines: Iterator[str], probe_lines: int = 5) -> bool:
    """从行迭代器探测「逐行独立 JSON 对象」(JSONL-as-.json) 的共享核。

    取前若干非空行逐行 json.loads, 成功且 >=2 行成立即判 JSONL; 撞到数组包裹符 `[`/`]` 即否。
    """
    parsed_lines = 0
    for raw_line in lines:
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        if line in ("[", "]"):
            return False
        try:
            json.loads(line)
            parsed_lines += 1
        except (json.JSONDecodeError, ValueError):
            return False
        if parsed_lines >= probe_lines:
            break
    return parsed_lines >= 2


def looks_like_jsonl_path(path: str, encoding: str, probe_lines: int = 5) -> bool:
    """流式从文件头部探测 JSONL-as-.json (只读头部若干行, 不整文件 load)。"""
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            return looks_like_jsonl_text_lines(f, probe_lines)
    except OSError as e:
        log.debug("JSONL-as-.json 探测读失败 %s: %s", path, e)
        return False

"""xlsx 二进制解析: **strict-only** (无容错中间态)。

二进制格式形式化为 strict-only (见 docs/parser_strict_tolerant_design.md §2 xlsx 行):
openpyxl(read_only) 打开成功 + sheet 可枚举 + 表头可读 → I_strict=1.0 / tier1;
打开失败 / 损坏 → tier3。**strict == tolerant** (二进制无 json5/列漂移这类可恢复中间态),
故命门 `strict_ok ⟺ I_strict==1 ⟺ deviations==0` 在 xlsx 上退化为「能否打开读表头」的二元判定。

行数据在阶段2 (schema_partition._partition_xlsx) 才采样, 阶段1 只读首行表头保持轻量。
openpyxl 为可选依赖, 缺失时降级为 tier3 而非崩溃。
"""

from src.parse.grade import Grade
from src.utils.logger import get_logger

log = get_logger(__name__)


def parse_xlsx(path: str) -> Grade:
    """读 xlsx 各 sheet 的表头信息 (read_only 流式, 不整文件 load)。"""
    try:
        import openpyxl
    except ImportError as e:
        log.error("openpyxl 未安装, 无法解析 xlsx %s: %s", path, e)
        return Grade(tier=3, I=0.0, I_strict=0.0, fmt="xlsx", error=f"openpyxl missing: {e}")

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        log.warning("xlsx 打开失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, I_strict=0.0, fmt="xlsx", error=str(e))

    try:
        sheets = []
        for ws in wb.worksheets:
            headers = None
            for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
                headers = [str(c) if c is not None else "" for c in row]
                break
            sheets.append({"name": ws.title, "headers": headers})
        wb.close()
    except Exception as e:
        log.warning("xlsx 读 sheet 失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, I_strict=0.0, fmt="xlsx", error=str(e))

    if not sheets:
        return Grade(tier=3, I=0.0, I_strict=0.0, fmt="xlsx", note="no sheets found")

    log.debug("xlsx %s: 读到 %d 个 sheet", path, len(sheets))
    # strict-only: 打开+sheet+表头可读 ⟹ I_strict=1.0/tier1 (二进制无容错中间态, strict==tolerant)。
    return Grade(tier=1, I=1.0, I_strict=1.0, fmt="xlsx",
                 parsed={"type": "xlsx", "sheets": sheets, "sheet_count": len(sheets)},
                 note=f"{len(sheets)} sheets readable")

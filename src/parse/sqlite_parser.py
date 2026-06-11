"""SQLite 二进制解析: sqlite3.connect 读 schema."""

import sqlite3

from src.parse.grade import Grade
from src.utils.logger import get_logger

log = get_logger(__name__)


def parse_sqlite(path: str) -> Grade:
    """读 SQLite .db 文件的 schema 信息."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        )
        tables = cursor.fetchall()
        conn.close()

        if tables:
            schema = [{"name": t[0], "sql": t[1]} for t in tables]
            log.debug("SQLite %s: 读到 %d 张表", path, len(tables))
            return Grade(tier=1, I=1.0, fmt="sqlite",
                         parsed={"type": "sqlite", "tables": schema, "table_count": len(tables)},
                         note=f"{len(tables)} tables readable")
        log.warning("SQLite %s: sqlite_master 无表", path)
        return Grade(tier=3, I=0.0, fmt="sqlite",
                     note="no tables found in sqlite_master")
    except sqlite3.Error as e:
        log.warning("SQLite 打开/读 schema 失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, fmt="sqlite", error=str(e))
    except Exception as e:
        log.error("SQLite 非预期失败 %s: %s", path, e)
        return Grade(tier=3, I=0.0, fmt="sqlite", error=str(e))

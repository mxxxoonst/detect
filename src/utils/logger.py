"""日志模块: 统一使用 Python logging 库."""

import logging
import sys
from pathlib import Path


def setup_logger(
    name: str = "pii_detect",
    level: int = logging.INFO,
    stream: bool = True,
    file: str | None = None,
) -> logging.Logger:
    """配置并返回 logger.

    Args:
        name: logger 名称, 默认 "pii_detect"
        level: 日志级别, 默认 INFO
        stream: 是否输出到 stdout/stderr, 默认 True
        file: 可选的日志文件路径

    Returns:
        配置好的 logging.Logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 按 handler 类型分别幂等：允许在已有 stream handler 的 logger 上
    # 后续补挂 file handler（main.py 先 import 触发模块级单例，再带 file 重配根 logger）
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )
    has_file = any(isinstance(h, logging.FileHandler) for h in logger.handlers)

    if stream and not has_stream:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)

    if file and not has_file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


# 模块级默认 logger, 供各子模块直接 import 使用
logger = setup_logger()


def get_logger(name: str) -> logging.Logger:
    """获取命名子 logger, 继承默认配置."""
    return logging.getLogger(f"pii_detect.{name}")

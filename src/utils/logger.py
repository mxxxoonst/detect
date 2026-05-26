"""日志模块: 统一使用 Python logging 库."""

import logging
import os
import sys


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

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if stream:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)

    if file:
        _dir = os.path.dirname(file)
        if _dir:
            os.makedirs(_dir, exist_ok=True)
        file_handler = logging.FileHandler(file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


# 模块级默认 logger, 供各子模块直接 import 使用
logger = setup_logger()


def get_logger(name: str) -> logging.Logger:
    """获取命名子 logger, 继承默认配置."""
    return logging.getLogger(f"pii_detect.{name}")

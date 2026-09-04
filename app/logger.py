"""滚动日志：写 ~/.ctrltrans/logs/app.log，出错也不影响主流程。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app.config import DATA_DIR

_LOG_FORMAT = "%(asctime)s [%(levelname)s] pid=%(process)d %(name)s: %(message)s"


def setup_logger(verbose_console: bool = False) -> logging.Logger:
    logger = logging.getLogger("ctrltrans")
    if logger.handlers:  # 防重复初始化
        return logger
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_LOG_FORMAT)

    try:
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "app.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except OSError:
        pass  # 日志目录不可写时仅用控制台

    if verbose_console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        logger.addHandler(sh)
    return logger

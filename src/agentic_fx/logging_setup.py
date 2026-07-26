"""技術ログ (severity 軸)。activity ログとは完全分離 — 設計書 §13。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_technical_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    target = (log_dir / "agentic.log").resolve()
    logger = logging.getLogger("agentic_fx")
    logger.setLevel(level.upper())
    logger.propagate = False
    # 同一パスなら既存 handler を再利用、異なるパスなら close して差し替える
    for h in list(logger.handlers):
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == target:
            return logger
        logger.removeHandler(h)
        h.close()
    handler = RotatingFileHandler(target, maxBytes=10 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    return logger

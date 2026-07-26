"""技術ログ (severity 軸)。activity ログとは完全分離 — 設計書 §13。

タイムスタンプは activity ログ (UTC) と突き合わせられるよう UTC に統一する
(logging.Formatter は既定で time.localtime を使うため、converter を
time.gmtime に差し替える。ローカル時刻との混同を防ぐため書式に "UTC" を含める)。
"""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s UTC %(levelname)s %(name)s: %(message)s"


def _make_formatter() -> logging.Formatter:
    formatter = logging.Formatter(_FORMAT)
    formatter.converter = time.gmtime
    return formatter


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
    handler.setFormatter(_make_formatter())
    logger.addHandler(handler)
    return logger

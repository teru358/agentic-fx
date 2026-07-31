import logging
import time

from agentic_fx.logging_setup import setup_technical_logging


def test_writes_to_file_only(tmp_path):
    logger = setup_technical_logging(tmp_path, level="INFO")
    logger.info("hello technical")
    logger.debug("should be filtered")
    text = (tmp_path / "agentic.log").read_text(encoding="utf-8")
    assert "hello technical" in text
    assert "should be filtered" not in text
    assert not any(isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)
                   for h in logger.handlers)


def test_idempotent_setup(tmp_path):
    l1 = setup_technical_logging(tmp_path)
    l2 = setup_technical_logging(tmp_path)
    assert l1 is l2
    assert len(l2.handlers) == 1


def test_reinit_with_different_dir_switches_file(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    setup_technical_logging(dir_a).info("to-a")
    logger = setup_technical_logging(dir_b)
    logger.info("to-b")
    assert len(logger.handlers) == 1
    assert "to-b" not in (dir_a / "agentic.log").read_text(encoding="utf-8")
    assert "to-b" in (dir_b / "agentic.log").read_text(encoding="utf-8")


def test_technical_log_timestamps_are_utc(tmp_path):
    """技術ログは activity ログ (UTC) と突き合わせられるよう、UTC で統一する。
    ローカルタイムゾーンに依存しない検証: converter が time.gmtime であること
    (= localtime を使っていないこと) と、フォーマットに UTC 表記が含まれること。
    """
    logger = setup_technical_logging(tmp_path, level="INFO")
    formatter = logger.handlers[0].formatter
    assert formatter.converter is time.gmtime
    fmt_text = (formatter._fmt or "") + (formatter.datefmt or "")
    assert "UTC" in fmt_text

    logger.info("utc-timestamp-check")
    text = (tmp_path / "agentic.log").read_text(encoding="utf-8")
    assert "UTC" in text


def test_daemon_adds_stderr_handler(tmp_path):
    logger = setup_technical_logging(tmp_path, daemon=True)
    kinds = [type(h).__name__ for h in logger.handlers]
    assert "RotatingFileHandler" in kinds
    assert "StreamHandler" in kinds
    # daemon=False に戻すと stderr handler は外れる
    logger2 = setup_technical_logging(tmp_path, daemon=False)
    assert [type(h).__name__ for h in logger2.handlers] == [
        "RotatingFileHandler"]

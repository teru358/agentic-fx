import logging

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

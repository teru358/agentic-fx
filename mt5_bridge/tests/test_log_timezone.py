from __future__ import annotations

import logging
import logging.config
import time

import server


def test_configured_log_formatters_render_a_fixed_timestamp_in_utc():
    loggers = [logging.getLogger("uvicorn.error"), logging.getLogger("uvicorn.access")]
    previous_state = [
        (logger, logger.handlers[:], logger.level, logger.propagate)
        for logger in loggers
    ]
    try:
        logging.config.dictConfig(server._build_log_config())
        default_formatter = loggers[0].handlers[0].formatter
        access_formatter = loggers[1].handlers[0].formatter

        record = logging.LogRecord(
            name="bridge.test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="sample", args=(), exc_info=None,
        )
        record.created = 1_700_000_000
        outputs = [
            server._file_handler.formatter.format(record),
            server._stream_handler.formatter.format(record),
            default_formatter.format(record),
        ]

        access_record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:1234", "GET", "/health", "1.1", 200),
            exc_info=None,
        )
        access_record.created = 1_700_000_000
        outputs.append(access_formatter.format(access_record))

        for output in outputs:
            assert "2023-11-14 22:13:20 UTC" in output
        assert all(formatter.converter is time.gmtime for formatter in (
            server._file_handler.formatter,
            server._stream_handler.formatter,
            default_formatter,
            access_formatter,
        ))
    finally:
        for logger, handlers, level, propagate in previous_state:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate

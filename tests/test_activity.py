from agentic_fx.activity import ActivityLog, Category


def test_write_format(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category.TRADE, "order_opened", "USDJPY long 0.10lot", ref_id="42")
    line = (tmp_path / "activity.log").read_text(encoding="utf-8").strip()
    parts = line.split("\t")
    assert parts[1] == "TRADE"
    assert parts[2] == "order_opened"
    assert parts[3] == "USDJPY long 0.10lot"
    assert parts[4] == "42"


def test_tail_with_category_filter(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    for i in range(5):
        log.write(Category.NEWS, "fetched", f"n{i}")
    log.write(Category.TRADE, "order_opened", "t1")
    assert len(log.tail(3)) == 3
    trades = log.tail(10, category=Category.TRADE)
    assert len(trades) == 1 and "t1" in trades[0]


def test_tail_empty_file(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    assert log.tail(5) == []


def test_summary_newlines_sanitized(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category.SYSTEM, "boot", "line1\nline2")
    assert len((tmp_path / "activity.log").read_text().strip().splitlines()) == 1

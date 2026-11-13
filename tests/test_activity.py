import logging

import pytest

from agentic_fx.activity import ActivityLog, Category


def test_write_failure_calls_callback_without_propagating(tmp_path, monkeypatch):
    calls = []
    log = ActivityLog(tmp_path / "activity.log", on_write_failure=calls.append)

    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(log._path), "open", fail_open)
    log.write(Category.SYSTEM, "event", "summary")

    assert len(calls) == 1
    assert isinstance(calls[0], OSError)


def test_write_failure_callback_error_is_also_suppressed(tmp_path, monkeypatch):
    def callback(_exc):
        raise RuntimeError("callback failed")

    log = ActivityLog(tmp_path / "activity.log", on_write_failure=callback)

    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(log._path), "open", fail_open)
    log.write(Category.SYSTEM, "event", "summary")


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


def test_write_does_not_raise_on_io_error(tmp_path, caplog):
    """N2 一次修正のピン: write は例外を送出しない契約。

    activity は可観測性の記録であり、資金保護経路 (SL/TP 監視) の
    隔離ハンドラのあらゆる場所から呼ばれるため、呼び出し側で毎回
    try に包む方針はモグラ叩きになる (再レビュー N1 → N2 がその実証)。
    書き込み不能でも記録漏れとして技術ログに warning を残すだけで、
    呼び出し元へは決して伝播させない。

    ``target`` をファイルではなくディレクトリにすることで、権限に依存
    せず (root 実行でも) ``open("a")`` が確実に失敗する状況を作る。"""
    target = tmp_path / "adir"
    target.mkdir()
    log = ActivityLog(target)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.activity"):
        log.write(Category.TRADE, "order_opened", "USDJPY long 0.10lot")
    assert "activity write failed" in caplog.text
    assert "order_opened" in caplog.text


def test_write_does_not_raise_on_none_summary_or_str_category(tmp_path, caplog):
    """I3 のピン: 整形 3 行 (ts / clean / line の組み立て) が try の外に
    あると、summary=None (``None.split()`` で AttributeError) や、
    category が ``Category`` ではなく素の str のケース
    (``"TRADE".value`` は存在せず AttributeError) で「決して送出しない」
    契約に反して例外がそのまま伝播する (レビュアー実測)。整形も try の
    中に入れ、ログ用の category 表示も ``.value`` に依存しない形にする。"""
    log = ActivityLog(tmp_path / "activity.log")
    with caplog.at_level(logging.WARNING, logger="agentic_fx.activity"):
        log.write("TRADE", "some_event", None)  # category が str, summary が None
    assert "activity write failed" in caplog.text
    assert "some_event" in caplog.text


def test_write_required_after_a_torn_line_starts_a_new_line(tmp_path):
    path = tmp_path / "activity.log"
    path.write_bytes(b"2026-01-01T00:00:00+00:00\tsystem\tpartial\tbody")      # 改行なしの部分行
    ActivityLog(path).write_required(Category.SYSTEM, "next_event", "ok", "r1")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("partial\tbody") and len(lines) == 2
    fields = lines[1].split("\t")
    assert fields[2:] == ["next_event", "ok", "r1"]


def test_write_required_adds_no_blank_line_after_a_complete_line(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write_required(Category.SYSTEM, "one", "a")
    log.write_required(Category.SYSTEM, "two", "b")
    assert len((tmp_path / "activity.log").read_text(encoding="utf-8").splitlines()) == 2


def test_write_required_fsyncs_the_directory_only_when_it_creates_the_file(tmp_path, monkeypatch):
    from agentic_fx import activity
    synced = []
    monkeypatch.setattr(activity, "_fsync_dir", synced.append)
    log = ActivityLog(tmp_path / "activity.log")
    log.write_required(Category.SYSTEM, "one", "a")
    assert synced == [tmp_path, tmp_path.parent]
    log.write_required(Category.SYSTEM, "two", "b")
    assert synced == [tmp_path, tmp_path.parent]


def test_write_required_fsyncs_the_directory_when_the_file_vanishes_after_a_check(
        tmp_path, monkeypatch):
    from agentic_fx import activity
    synced = []
    monkeypatch.setattr(activity, "_fsync_dir", synced.append)
    path = tmp_path / "activity.log"
    path.write_text("old\n", encoding="utf-8")
    log = ActivityLog(path)
    path.unlink()
    log.write_required(Category.SYSTEM, "one", "a")
    assert synced == [tmp_path, tmp_path.parent]


def test_write_required_raises_when_a_write_makes_no_progress(tmp_path, monkeypatch):
    from agentic_fx import activity
    monkeypatch.setattr(activity.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        ActivityLog(tmp_path / "activity.log").write_required(Category.SYSTEM, "one", "a")


def test_write_required_raises_when_the_directory_fsync_fails(tmp_path, monkeypatch):
    from agentic_fx import activity

    def boom(path):
        raise OSError(5, "io")

    monkeypatch.setattr(activity, "_fsync_dir", boom)
    with pytest.raises(OSError):
        ActivityLog(tmp_path / "activity.log").write_required(Category.SYSTEM, "one", "a")


def _race_after_newline_decision(monkeypatch, path, other):
    original = ActivityLog._needs_newline

    def racing(fd):
        decided = original(fd)
        with open(path, "ab") as f:
            f.write(other)
        return decided

    monkeypatch.setattr(ActivityLog, "_needs_newline", staticmethod(racing))


def test_a_line_appended_by_another_writer_survives_write_required(tmp_path, monkeypatch):
    path = tmp_path / "activity.log"
    path.write_bytes(b"partial")
    other = b"OTHER\tfull\tline\n"
    _race_after_newline_decision(monkeypatch, path, other)
    ActivityLog(path).write_required(Category.SYSTEM, "mine", "ok", "r1")
    lines = path.read_bytes().split(b"\n")
    assert lines[0] == b"partial" + other.rstrip(b"\n")
    assert lines[1] == b""
    assert lines[2].split(b"\t")[2:] == [b"mine", b"ok", b"r1"]
    assert lines[3:] == [b""]


def test_a_line_appended_by_another_writer_survives_end_partial_line(tmp_path, monkeypatch):
    path = tmp_path / "activity.log"
    path.write_bytes(b"partial")
    other = b"OTHER\tfull\tline\n"
    _race_after_newline_decision(monkeypatch, path, other)
    ActivityLog(path).end_partial_line()
    assert path.read_bytes() == b"partial" + other + b"\n"

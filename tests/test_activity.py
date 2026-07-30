import logging

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

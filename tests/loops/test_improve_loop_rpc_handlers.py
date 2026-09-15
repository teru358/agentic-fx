"""ImproveLoop._build_rpc_handlers — run_backtest/analyze_corr の親側
実装 (設計書 §3.4、Task 7-D の non-committing 版を握る。10.9 節 Step 11)。

D-4 是正 (検収 欠落10本のうち4本): プラン L18334-18459 の逐語。**未申告の
適応**: プランの `_SAVE_KWARGS` は naive `datetime` (`datetime(2020, 1, 1)`)
を使うが、本プロジェクトは naive datetime を明示的に禁止する
(`tests/integration/test_improve_forbidden_regression.py::_RW7_SAVE_KWARGS`
が同じ save_kwargs 形を tz-aware で使っている実例に倣い、ここでも
tz-aware に揃える)。
"""
from __future__ import annotations

import math
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.backtest.holdout import NoHistoryError

from agentic_fx.backtest.timeframes import TF_MINUTES
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect_readonly
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
# round2 #9 是正 (2026-08-29、設計書 §6.1 裁定注記): analyze_for_agent は
# 内部で in_sample_until (≈2026-05-22) から遡る既定90日窓 (since≈
# 2026-02-21) を強制するようになった。旧 2026-01-01 はこの窓の外側
# (=insufficient_data になる) だったため、窓の内側かつ boundary より
# 確実に前の日付へ更新する (200本の1hバー=約8.3日分なので boundary は
# 跨がない)。
_BEFORE_BOUNDARY = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)


def _sine(n, *, phase=0):
    """三角関数の決定的疑似価格列 (`tests/backtest/test_analysis.py::_sine`
    と同型)。"""
    return [100 + math.sin((i + phase) / 5.0) for i in range(n)]


def _seed_series(conn, symbol, values, *, start, timeframe="1h"):
    """`tests/backtest/test_analysis.py::_series` と同型 — timeframe 幅
    刻みの 1m バーとして投入する (分析面は 1m 行を読み取り時リサンプル)。"""
    step = timedelta(minutes=TF_MINUTES[timeframe])
    rows = [(symbol, "1m", (start + i * step).isoformat(),
             v, v + 0.05, v - 0.05, v, 1.0, 0.01)
            for i, v in enumerate(values)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")

_SAVE_KWARGS = dict(
    scope="in_sample", plugin_ref="plugins/_staging/x/myst",
    content_hash="cand-hash", kind="strategy", pair="USDJPY",
    timeframe="1h", source="dukascopy", base_interval="1m", params={},
    period=(datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc)),
    metrics={"pf": 1.3, "trades": 40, "avg_r": 0.1, "evaluable": True}, settings_hash="s",
    core_commit="c", initial_balance=10000.0, now=_NOW)


def _patch_strategy_lookup(monkeypatch):
    """candidate meta 解決 (`plugin_loader._discover_one`) と
    `strategy_adapter.build_intent_source` を fake する — 本節が検査するのは
    RPC handler と台帳/persist の結線であり、実バックテスト実行ではない。"""
    from agentic_fx.plugin.version_store import content_hash_bytes

    def discover(path, name):
        path.mkdir(parents=True, exist_ok=True)
        files = {
            "plugin.py": b"def evaluate(*args): return {}\n",
            "config.yaml": b"kind: strategy\n",
            "test_plugin.py": b"def test_candidate(): pass\n",
        }
        for filename, data in files.items():
            target = path / filename
            if not target.exists():
                target.write_bytes(data)
        return SimpleNamespace(
            name=name, kind="strategy", timeframe="1h",
            content_hash=content_hash_bytes(
                (path / "plugin.py").read_bytes(),
                (path / "config.yaml").read_bytes()),
            pairs=("USDJPY",),
            # [indicator-consumption-wiring] T5b 逸脱是正: `run_backtest_
            # handler` (Step 5-4c) が `resolve_indicator_deps(meta, ...)`
            # を呼ぶようになり、`meta.indicators` を読む。この fake meta は
            # 依存なし strategy を表す (config.yaml に `indicators:` が無い)
            # ので空 tuple。
            indicators=())

    monkeypatch.setattr("agentic_fx.plugin.loader._discover_one", discover)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_adapter.build_intent_source",
        lambda meta, **kw: SimpleNamespace(close=lambda: None))


def _patch_run_in_sample(monkeypatch, save_kwargs=_SAVE_KWARGS):
    def _fake_run_in_sample(*a, record_fn=None, **kw):
        record_fn(dict(save_kwargs))
        return dict(save_kwargs["metrics"])
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        _fake_run_in_sample)


def test_run_backtest_creates_readonly_tmp_snapshot(loop_min, tmp_path, monkeypatch):
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})

    archive_tmp = Path(result.save_kwargs["archive_tmp"])
    assert archive_tmp.name.startswith(
        f".tmp-{result.save_kwargs['artifact_hash']}-")
    assert {p.name for p in archive_tmp.iterdir()} == {
        "plugin.py", "config.yaml", "test_plugin.py", "meta.json"}
    assert archive_tmp.stat().st_mode & 0o777 == 0o500
    assert all(p.stat().st_mode & 0o777 == 0o400 for p in archive_tmp.iterdir())
    meta = json.loads((archive_tmp / "meta.json").read_text())
    assert meta["artifact_hash"] == result.save_kwargs["artifact_hash"]


def test_run_backtest_skips_snapshot_when_ledger_not_open(
        loop_min, tmp_path, monkeypatch):
    """/code-review 2 周目 CR4 (2026-09-11): timeout 後に完了した handler
    (受理境界の外) は snapshot を書かない — commit 後の plugins/_archive に
    孤立 tmp が残り、次の起動まで誰も拾わない。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()
    result = loop_min._build_rpc_handlers(
        ledger, staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert "error" not in result
    assert "archive_tmp" not in result.save_kwargs
    assert not (loop_min._root / "plugins" / "_archive").exists()
    assert "archive_skipped_late" in (tmp_path / "activity.log").read_text()


def test_run_backtest_skips_snapshot_when_rpc_abandoned_before_write(
        loop_min, tmp_path, monkeypatch):
    """codex 2 周目 Important (2026-09-11): `ledger.state()` を読んだ
    直後に dispatcher の RPC timeout が予約を release しても、その競合窓は
    `state() != "OPEN"` 単独では捉えられない — handler 自身が動いている
    worker thread に `RPC_ABANDONED_ATTR` が立っていないかも見る。ここでは
    `run_in_sample` (backtest 本体) の内側で abandoned を模擬的に立てて、
    OPEN のままでも snapshot が作られないことを確認する。"""
    import threading as _threading

    from agentic_fx.runners.worker_runner import RPC_ABANDONED_ATTR

    _patch_strategy_lookup(monkeypatch)

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        setattr(_threading.current_thread(), RPC_ABANDONED_ATTR, True)
        record_fn(dict(_SAVE_KWARGS))
        return dict(_SAVE_KWARGS["metrics"])

    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        _fake_run_in_sample)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    assert ledger.state() == "OPEN"
    try:
        result = loop_min._build_rpc_handlers(
            ledger, staging_dir=staging.parent)["run_backtest"](
                {"name": "myst", "pair": "USDJPY"})
    finally:
        # このマーカーは実 thread 属性として残る (monkeypatch のロールバック
        # 対象外) — 同一プロセスの後続テストへ漏れないよう必ず外す。
        if hasattr(_threading.current_thread(), RPC_ABANDONED_ATTR):
            delattr(_threading.current_thread(), RPC_ABANDONED_ATTR)
    assert "error" not in result
    assert "archive_tmp" not in result.save_kwargs
    assert not (loop_min._root / "plugins" / "_archive").exists()
    assert "archive_skipped_late" in (tmp_path / "activity.log").read_text()


def test_run_backtest_removes_tmp_when_rpc_abandoned_after_write(
        loop_min, tmp_path, monkeypatch):
    """post-write 経路: snapshot 書き込みが (meta.json まで) 完了した
    *後* に abandoned が立った場合でも、書いた tmp を残さず消し、
    `archive_tmp`/`artifact_hash` を台帳へ渡さない (post_write)。"""
    import threading as _threading

    from agentic_fx.runners.worker_runner import RPC_ABANDONED_ATTR

    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)

    from agentic_fx.plugin import version_store as _version_store
    original_write_ro_file = _version_store._write_ro_file
    calls = {"n": 0}

    def _fake_write_ro_file(path, data):
        original_write_ro_file(path, data)
        calls["n"] += 1
        if path.name == "meta.json":
            # meta.json は最後に書かれるファイル — その書き込み完了直後に
            # abandoned を立てる (post-write の競合窓を模擬する)。
            setattr(_threading.current_thread(), RPC_ABANDONED_ATTR, True)

    monkeypatch.setattr(
        "agentic_fx.plugin.version_store._write_ro_file", _fake_write_ro_file)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    try:
        result = loop_min._build_rpc_handlers(
            ledger, staging_dir=staging.parent)["run_backtest"](
                {"name": "myst", "pair": "USDJPY"})
    finally:
        if hasattr(_threading.current_thread(), RPC_ABANDONED_ATTR):
            delattr(_threading.current_thread(), RPC_ABANDONED_ATTR)
    assert calls["n"] == 4  # plugin.py / config.yaml / test_plugin.py / meta.json
    assert "error" not in result
    assert "archive_tmp" not in result.save_kwargs
    assert "artifact_hash" not in result.save_kwargs
    archive_root = loop_min._root / "plugins" / "_archive" / staging.parent.name
    if archive_root.exists():
        assert list(archive_root.iterdir()) == []
    assert ("archive_skipped_late" in (tmp_path / "activity.log").read_text()
            and "reason=post_write" in (tmp_path / "activity.log").read_text())


def test_run_backtest_rejects_changed_content_before_execution(
        loop_min, tmp_path, monkeypatch):
    candidate = tmp_path / "staging" / "myst"
    candidate.mkdir(parents=True)
    for filename in ("plugin.py", "config.yaml", "test_plugin.py"):
        (candidate / filename).write_text("changed")
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: SimpleNamespace(
            name=name, kind="strategy", timeframe="1h",
            content_hash="stale", pairs=("USDJPY",)))
    called = threading.Event()
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        lambda *a, **kw: called.set())
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=candidate.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert result == {"error": "loader_rejected: content changed during backtest"}
    assert not called.is_set()


def test_snapshot_write_failure_returns_backtest_and_logs_activity(
        loop_min, tmp_path, monkeypatch):
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.plugin.version_store._write_ro_file",
        lambda *a, **kw: (_ for _ in ()).throw(PermissionError("denied")))
    candidate = tmp_path / "staging" / "myst"
    candidate.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=candidate.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert result["metrics"] == {"pf": 1.3, "trades": 40, "avg_r": 0.1, "evaluable": True}
    assert "archive_tmp" not in result.save_kwargs
    assert "archive_failed" in (tmp_path / "activity.log").read_text()


def test_snapshot_a_b_a_creates_three_distinct_tmp_dirs(
        loop_min, tmp_path, monkeypatch):
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    candidate = tmp_path / "staging" / "myst"
    candidate.mkdir(parents=True)
    handler = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=candidate.parent)["run_backtest"]
    hashes = []
    for body in (b"A", b"B", b"A"):
        (candidate / "plugin.py").write_bytes(body)
        result = handler({"name": "myst", "pair": "USDJPY"})
        hashes.append(result.save_kwargs["artifact_hash"])
    tmp_dirs = list((tmp_path / "plugins" / "_archive" / "staging").iterdir())
    assert hashes[0] == hashes[2] != hashes[1]
    assert len(tmp_dirs) == 3


def test_run_backtest_handler_returns_json_safe_limited_reply(
        loop_full, tmp_path, monkeypatch):
    """実際の save_kwargs 形でも RPC 応答は限定され JSON 化できる。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    handlers = loop_full._build_rpc_handlers(ledger, staging_dir=staging_dir)
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=handlers["run_backtest"],
        analyze_corr_handler=handlers["analyze_corr"])}

    out = tools["run_backtest"].func(name="myst", pair="USDJPY")

    json.dumps(out)
    # [indicator-consumption-wiring] T5b 逸脱是正: Step 5-4c の
    # `_backtest_reply_from_save_kwargs` が成功応答へ `started: True` を
    # 足すようになった (F4 の裏 — 成功応答にも `started` キーが載る)。
    assert out == {
        "scope": "in_sample",
        "pair": "USDJPY",
        "timeframe": "1h",
        "source": "dukascopy",
        "metrics": {"pf": 1.3, "trades": 40, "avg_r": 0.1, "evaluable": True},
        "trial_count": 1,
        "started": True,
    }
    assert "period" not in out and "now" not in out
    assert "content_hash" not in out


def test_run_backtest_handler_source_follows_backtest_settings(
        loop_no_seam, tmp_path, monkeypatch):
    loop, _ = loop_no_seam
    loop._settings = loop._settings.model_copy(update={
        "backtest": loop._settings.backtest.model_copy(
            update={"eval_source": "mt5"})})
    _patch_strategy_lookup(monkeypatch)
    seen_sources = []

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_adapter.build_intent_source",
        lambda meta, **kw: (seen_sources.append(("intent", kw["dataset"].source))
                            or SimpleNamespace(close=lambda: None)))

    def _fake_run_in_sample(*args, record_fn=None, **kwargs):
        seen_sources.append(("in_sample", kwargs["dataset"].source))
        record_fn({**_SAVE_KWARGS, "source": kwargs["dataset"].source})
        return dict(_SAVE_KWARGS["metrics"])

    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        _fake_run_in_sample)
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)

    result = loop._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging_dir)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})

    assert seen_sources == [("intent", "mt5"), ("in_sample", "mt5")]
    assert result["source"] == "mt5"


def test_run_backtest_handler_result_feeds_persist_ledger_rows(
        loop_min, conn, tmp_path, monkeypatch):
    """handler → ledger.record(result_summary=save_kwargs 込み) →
    10.10 節 `_persist_ledger_rows` が `backtest_runs` へ実際に行を書く、
    までの結線を確認する。

    未申告の適応 (D-4 是正時に検出、D-3 と同型): `tests/loops/conftest.py`
    の `loop_min`/`loop_full` は write/readonly 両 factory が同一 `conn`
    オブジェクトを返す。`run_backtest_handler` は readonly conn を
    `finally: conn.close()` するため (production の
    `_build_rpc_handlers` 自体の挙動、変更しない)、そのままだと
    handler 呼び出し後に共有 `conn` が閉じてテストが DB を読めなくなる。
    `_db_readonly_conn_factory` をこのテストだけ新規接続を返す形に
    差し替える (D-3 で `_rw7_build_improve_loop` に施したのと同じ是正)。"""
    monkeypatch.setattr(
        loop_min, "_db_readonly_conn_factory",
        lambda: connect_readonly(tmp_path / "t.db"))
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    handlers = loop_min._build_rpc_handlers(ledger, staging_dir=staging_dir)
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=handlers["run_backtest"],
        analyze_corr_handler=handlers["analyze_corr"])}
    tools["run_backtest"].func(name="myst", pair="USDJPY")
    ledger.freeze()

    before = conn.execute(
        "SELECT COUNT(*) c FROM backtest_runs").fetchone()["c"]
    loop_min._persist_ledger_rows(
        conn, ledger_entries=ledger.entries(), now=_NOW,
        mission_outcome="approval")
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) c FROM backtest_runs").fetchone()["c"]
    assert after == before + 1
    # A6 (v3 設計): RPC ledger 経路で保存された行の base_interval が実際に
    # dataset (既定 1m) と一致することを直接検査する (段階 2 レビュー是正
    # — TypeError にならないことだけでは列の値までは pin できない)。
    row = conn.execute(
        "SELECT base_interval FROM backtest_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["base_interval"] == "1m"


def test_persist_ledger_rows_fails_closed_when_handler_omits_save_kwargs(
        loop_min, conn):
    """10.10 節の契約 (申し送り): handler が save_kwargs を result_summary
    へ混ぜ込み忘れると `_persist_ledger_rows` が `KeyError` で落ちる —
    台帳データが永続化されないまま静かに完走しない、fail-closed の pin。"""
    entries = [{"kind": "run_backtest",
               "result_summary": {"metrics": {"pf": 1.0}}}]  # save_kwargs 欠落
    with pytest.raises(KeyError):
        loop_min._persist_ledger_rows(
            conn, ledger_entries=entries, now=_NOW,
            mission_outcome="approval")


def test_persist_ledger_error_activity_includes_mission_id(loop_min, conn, tmp_path):
    """並列 mission の ledger skip 行にも発生元 mission を残す。"""
    loop_min._persist_ledger_rows(
        conn,
        ledger_entries=[{
            "kind": "run_backtest",
            "result_summary": {"error": "backtest_failed"},
            "trial_count": 1,
        }],
        now=_NOW,
        mission_id=424242,
        mission_outcome="approval",
    )

    activity_text = (tmp_path / "activity.log").read_text()
    assert "ledger_entry_skipped_error" in activity_text
    assert "mission=424242" in activity_text


def test_analyze_corr_handler_does_not_persist_before_tx2(
        loop_min, conn, tmp_path, monkeypatch):
    """analyze_corr handler は `persist=False` で呼ぶ — RPC 呼出し時点では
    `analysis_runs` へ書かない (10.10 節の Tx-2 でのみ書く、7-D
    `test_analyze_for_agent_persist_false_does_not_write_analysis_runs` と
    同じ pin を `_build_rpc_handlers` 経由で確認する)。

    未申告の適応 (D-4 是正時に検出、上記テストと同型の理由): 共有 conn の
    close 罠を避けるため readonly factory を差し替える。

    D-14 是正 (検収 R2): 元の fixture は空 DB だったため
    `analyze_for_agent` が実データに到達する前に `insufficient_data` へ
    倒れ、`persist` の値によらず行数が変化しない — `persist=True` への
    変異が SURVIVED する空洞だった (10.9 M10)。`tests/backtest/
    test_analysis.py::_seed_two_series` と同型のデータ (holdout boundary
    より確実に前の 2 通貨ペア系列) を投入し、`analyze_corr` が実際に
    corr_matrix を計算する経路まで到達させる。

    実測した kill の経路 (readonly conn を使う本番相当の fixture の
    帰結): `persist=True` 変異下では readonly conn への書込みが
    `sqlite3.OperationalError: attempt to write a readonly database` で
    失敗し、`analyze_corr_handler` の外側 except がこれを握って
    `{"error": "analyze_failed"}` を返す — 下記の `assert "error" not in
    result` がこれを検出する (row-count assert `after == before` はこの
    変異下でも変わらず通り、killer ではない)。"""
    monkeypatch.setattr(
        loop_min, "_db_readonly_conn_factory",
        lambda: connect_readonly(tmp_path / "t.db"))
    # settings.pairs=["USDJPY"]/watch_symbols=[] のままでは corr_matrix の
    # candidates が 1 件のみで trial_count が常に 0 (insufficient_data
    # 確定) になる — EURUSD を watch_symbols に足して 2 候補にする。
    monkeypatch.setattr(loop_min, "_settings", loop_min._settings.model_copy(
        update={"datafeed": loop_min._settings.datafeed.model_copy(
            update={"watch_symbols": ["EURUSD"]})}))
    _seed_series(conn, "USDJPY", _sine(200, phase=0), start=_BEFORE_BOUNDARY)
    _seed_series(conn, "EURUSD", _sine(200, phase=1), start=_BEFORE_BOUNDARY)
    conn.commit()

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    handlers = loop_min._build_rpc_handlers(ledger, staging_dir=Path("/tmp/x"))
    before = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    result = handlers["analyze_corr"]({"kind": "corr_matrix", "timeframe": "1h"})
    # fixture 自体が空洞化していないことの前提確認: 実際に相関が計算できて
    # いる (insufficient_data に倒れていない)。
    assert "error" not in result, (
        f"analyze_corr が insufficient_data 等に倒れた (fixture が空洞): "
        f"{result!r}")
    after = conn.execute(
        "SELECT COUNT(*) c FROM analysis_runs").fetchone()["c"]
    assert after == before
    # /code-review 2 周目 CR3 (2026-09-11): 台帳は痩せない — 親 wrapper が
    # 記録する private (.save_kwargs) は遮断 7 前の全量 (in_sample_until を
    # 含む)。無いと analysis_runs.params_json から境界が落ちる。
    private = getattr(result, "save_kwargs", None)
    assert private is not None
    assert "in_sample_until" in private.get("params", private)


def test_run_backtest_handler_rejects_non_strategy_candidate(
        loop_min, tmp_path, monkeypatch):
    """C25 是正 (束D検収, verified-local-round1.md §11 #12):
    `run_backtest_handler` の `if meta is None or meta.kind != "strategy":
    raise ValueError(...)` を踏むテストが 0 件だった
    (`grep -rn "requires a strategy" tests/` = 0 件)。全 fixture が
    `_patch_strategy_lookup` (常に `kind="strategy"` の meta を返す) を
    使うため未踏だった。ここでは `kind="indicator"` の meta を返す fixture
    で `ValueError` (親 try/except の**外側**、fail closed) を要求する。"""
    from types import SimpleNamespace

    fake_meta = SimpleNamespace(
        name="myst", kind="indicator", timeframe="1h",
        content_hash="cand-hash", pairs=("USDJPY",))
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: fake_meta)
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    handlers = loop_min._build_rpc_handlers(ledger, staging_dir=staging_dir)

    with pytest.raises(RuntimeError, match="requires a strategy") as exc_info:
        handlers["run_backtest"]({"name": "myst", "pair": "USDJPY"})
    assert "run_plugin_tests" in str(exc_info.value)
    assert "設計 §6" in str(exc_info.value)


def test_run_backtest_handler_returns_error_dict_and_closes_conn_on_failure(
        loop_min, tmp_path, monkeypatch):
    """L-B27 是正 (束D検収, verified-local-round1.md §11 #8):
    `{"error": "backtest_failed"}` を**返すこと**を assert するテストが
    0 件だった (既存の grep ヒットは全て「error が出ていないこと」の
    否定 assert)。`holdout.run_in_sample` を例外送出に monkeypatch し、
    戻り値と `intent_source.close()`/`conn.close()` (spy) を assert する。"""
    _patch_strategy_lookup(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("simulated backtest failure")
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample", _boom)

    closed = {"intent_source": False, "conn": False}
    real_readonly_factory = loop_min._db_readonly_conn_factory

    class _ConnSpy:
        def __init__(self, real_conn):
            self._real = real_conn

        def close(self):
            closed["conn"] = True
            self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(
        loop_min, "_db_readonly_conn_factory",
        lambda: _ConnSpy(real_readonly_factory()))

    class _IntentSourceSpy:
        def close(self):
            closed["intent_source"] = True

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_adapter.build_intent_source",
        lambda meta, **kw: _IntentSourceSpy())

    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    handlers = loop_min._build_rpc_handlers(ledger, staging_dir=staging_dir)

    result = handlers["run_backtest"]({"name": "myst", "pair": "USDJPY"})

    assert result == {"error": "backtest_failed"}
    assert closed["intent_source"] is True
    assert closed["conn"] is True
    archive_root = tmp_path / "plugins" / "_archive"
    assert not archive_root.exists() or not any(archive_root.rglob(".tmp-*"))


def test_run_backtest_handler_reports_missing_history_with_available_pairs(
        loop_min, tmp_path, monkeypatch):
    _patch_strategy_lookup(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        lambda *a, **kw: (_ for _ in ()).throw(NoHistoryError(
            "no 1m history for symbol='EURUSD' source='dukascopy' "
            "(cannot determine in-sample start)")))
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)
    candidate = staging_dir / "myst"
    (candidate / "plugin.py").write_bytes(b"plugin")
    (candidate / "config.yaml").write_bytes(b"config")
    (candidate / "test_plugin.py").write_bytes(b"test")
    from agentic_fx.plugin.version_store import content_hash_bytes
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: SimpleNamespace(
            name="myst", kind="strategy", timeframe="1h",
            content_hash=content_hash_bytes(b"plugin", b"config"),
            pairs=("EURUSD",), indicators=()))
    handlers = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}), staging_dir=staging_dir)

    result = handlers["run_backtest"]({"name": "myst", "pair": "EURUSD"})

    # ohlcv_history が空のとき、hint は「設定 pair」を available と偽らず
    # (m47 の EURUSD 死路再誘導の防止)、履歴未投入である事実を伝える。
    assert result["error"] == "no_history_for_symbol"
    assert result["message"] == (
        "no 1m history for symbol='EURUSD' source='dukascopy' "
        "(cannot determine in-sample start)")
    assert "no pair has local backtest history" in result["hint"]
    assert "USDJPY" not in result["hint"]


def test_run_backtest_handler_maps_real_holdout_missing_history_contract(
        loop_no_seam, tmp_path, monkeypatch):
    """実 DB の空履歴から出る holdout の文言を handler 契約まで pin する。"""
    loop, _ = loop_no_seam
    _patch_strategy_lookup(monkeypatch)
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)
    handlers = loop._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging_dir)

    result = handlers["run_backtest"]({"name": "myst", "pair": "EURUSD"})

    assert result["error"] == "no_history_for_symbol"
    assert result["hint"] == (
        "No 1m history for EURUSD, and no pair has local backtest "
        "history yet — run_backtest cannot succeed until history data is "
        "imported. Report this as a discovery instead of retrying other "
        "pairs.")


def test_run_backtest_handler_missing_history_hint_lists_pairs_with_data(
        loop_no_seam, tmp_path, monkeypatch):
    """1m 履歴が実在する symbol だけを hint に載せる (settings.pairs では
    なく DB の実データで判定する) ことを pin する。hint 構築は handler が
    readonly conn を close した後の再接続なので、write/readonly が同一
    conn の loop_min seam では検証できない — 本番同型の loop_no_seam
    (毎回新規接続) を使う。"""
    loop, db_path = loop_no_seam
    loop._settings = loop._settings.model_copy(update={
        "backtest": loop._settings.backtest.model_copy(
            update={"eval_source": "mt5"})})
    _patch_strategy_lookup(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        lambda *a, **kw: (_ for _ in ()).throw(NoHistoryError(
            "no 1m history for symbol='EURUSD' source='mt5' "
            "(cannot determine in-sample start)")))
    seed_conn = loop._db_write_conn_factory()
    seed_conn.execute(
        "INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high,"
        " low, close, volume, source) VALUES ('USDJPY', '1m',"
        " '2026-01-01T00:00:00+00:00', 1, 1, 1, 1, 0, 'mt5')")
    seed_conn.execute(
        "INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high,"
        " low, close, volume, source) VALUES ('EURUSD', '1m',"
        " '2026-01-01T00:00:00+00:00', 1, 1, 1, 1, 0, 'dukascopy')")
    seed_conn.commit()
    seed_conn.close()
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)
    handlers = loop._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}), staging_dir=staging_dir)

    result = handlers["run_backtest"]({"name": "myst", "pair": "EURUSD"})

    assert result["error"] == "no_history_for_symbol"
    assert "Pairs with local backtest history: USDJPY." in result["hint"]
    available = result["hint"].split("history: ", 1)[1]
    assert "EURUSD" not in available


def test_run_backtest_handler_missing_history_hint_uses_dataset_base_interval(
        loop_no_seam, tmp_path, monkeypatch):
    """段 0 pin: hint 構築の symbol 探索 SQL は ``dataset.base_interval`` を
    使うべきで、literal "1m" に固定戻しすると 5m 基底運用で実在する
    symbol を取りこぼす (hint が「no pair has local backtest history」に
    後退する)。"""
    loop, db_path = loop_no_seam
    loop._settings = loop._settings.model_copy(update={
        "backtest": loop._settings.backtest.model_copy(
            update={"eval_source": "mt5", "base_interval": "5m"})})
    _patch_strategy_lookup(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        lambda *a, **kw: (_ for _ in ()).throw(NoHistoryError(
            "no 5m history for symbol='EURUSD' source='mt5' "
            "(cannot determine in-sample start)")))
    seed_conn = loop._db_write_conn_factory()
    seed_conn.execute(
        "INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high,"
        " low, close, volume, source) VALUES ('USDJPY', '5m',"
        " '2026-01-01T00:00:00+00:00', 1, 1, 1, 1, 0, 'mt5')")
    seed_conn.commit()
    seed_conn.close()
    staging_dir = tmp_path / "staging"
    (staging_dir / "myst").mkdir(parents=True)
    handlers = loop._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}), staging_dir=staging_dir)

    result = handlers["run_backtest"]({"name": "myst", "pair": "EURUSD"})

    assert result["error"] == "no_history_for_symbol"
    assert "No 5m history for EURUSD" in result["hint"]
    assert "Pairs with local backtest history: USDJPY." in result["hint"]


def test_run_backtest_handler_reports_pair_not_declared(
        loop_min, tmp_path, monkeypatch):
    _patch_strategy_lookup(monkeypatch)
    staging_dir = tmp_path / "staging"
    candidate = staging_dir / "myst"
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_bytes(b"plugin")
    (candidate / "config.yaml").write_bytes(b"config")
    (candidate / "test_plugin.py").write_bytes(b"test")
    from agentic_fx.plugin.version_store import content_hash_bytes
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: SimpleNamespace(
            name="myst", kind="strategy", timeframe="1h",
            content_hash=content_hash_bytes(b"plugin", b"config"),
            pairs=("EURUSD",), indicators=()))
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError(
            "pair 'USDJPY' is not in plugin 'myst''s declared pairs ('EURUSD',)")))
    handlers = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}), staging_dir=staging_dir)

    result = handlers["run_backtest"]({"name": "myst", "pair": "USDJPY"})

    assert result == {
        "error": "pair_not_declared_by_plugin",
        "message": "pair 'USDJPY' is not in plugin 'myst''s declared pairs "
                   "('EURUSD',)",
        "hint": "Request one of the plugin's declared pairs: EURUSD.",
    }


def test_analyze_corr_handler_returns_error_dict_on_failure(
        loop_min, tmp_path, monkeypatch):
    """L-B27 是正 (束D検収, verified-local-round1.md §11 #8):
    `analyze_corr_handler` の `{"error": "analyze_failed"}` を**返すこと**
    を assert するテストが 0 件だった。`analyze_for_agent` を例外送出に
    monkeypatch する。"""
    def _boom(*a, **kw):
        raise RuntimeError("simulated analyze failure")
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.analyze_for_agent", _boom, raising=False)
    monkeypatch.setattr(
        "agentic_fx.backtest.analysis.analyze_for_agent", _boom)

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    handlers = loop_min._build_rpc_handlers(ledger, staging_dir=tmp_path)

    result = handlers["analyze_corr"]({"kind": "corr_matrix", "timeframe": "1h"})

    assert result == {"error": "analyze_failed"}


def test_snapshot_meta_json_metrics_match_save_kwargs(
        loop_min, tmp_path, monkeypatch):
    """ローカル 1 周目 #8 (2026-09-10): meta.json の metrics が
    save_kwargs["metrics"] と同一であること。既存 snapshot テストは
    `meta["artifact_hash"] == save_kwargs["artifact_hash"]` の自己一致しか
    見ないため、metadata の metrics を空 dict にしても緑のままだった。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    meta = json.loads(
        (Path(result.save_kwargs["archive_tmp"]) / "meta.json").read_text())
    assert meta["metrics"] == result.save_kwargs["metrics"]
    assert meta["metrics"] == {"pf": 1.3, "trades": 40, "avg_r": 0.1, "evaluable": True}
    assert meta["name"] == "myst"
    assert meta["pair"] == "USDJPY"


def test_snapshot_artifact_hash_covers_test_plugin_bytes(
        loop_min, tmp_path, monkeypatch):
    """ローカル 1 周目 #9 (2026-09-10): artifact_hash が 3 ファイル
    (plugin.py / config.yaml / test_plugin.py) の bytes から計算されること。
    テストが実装と独立に hash を再計算していなかったため、3 本目を落として
    content_hash 相当 (2 ファイル) に退化させても緑のままだった。"""
    from agentic_fx.plugin.version_store import artifact_hash_bytes
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    handler = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"]
    first = handler({"name": "myst", "pair": "USDJPY"})
    expected = artifact_hash_bytes(
        (staging / "plugin.py").read_bytes(),
        (staging / "config.yaml").read_bytes(),
        (staging / "test_plugin.py").read_bytes())
    assert first.save_kwargs["artifact_hash"] == expected
    # test_plugin.py だけを変える (content_hash は plugin.py+config.yaml 由来
    # なので不変) → artifact_hash は変わらなければならない
    (staging / "test_plugin.py").write_bytes(b"def test_candidate(): assert 1\n")
    second = handler({"name": "myst", "pair": "USDJPY"})
    assert second.save_kwargs["artifact_hash"] != expected


def test_snapshot_fsyncs_tmp_dir_and_parent(loop_min, tmp_path, monkeypatch):
    """ローカル 1 周目 #10 (2026-09-10): `_fsync_dir(archive_tmp)` と
    `_fsync_dir(archive_tmp.parent)` が両方呼ばれること。fsync は観測不能な
    次元で spy が無く、両方削除しても緑のままだった。spy は
    `_build_rpc_handlers` を呼ぶ **前** に張る (handler が関数内 import で
    名前を束縛するため)。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)
    synced: list[Path] = []
    monkeypatch.setattr("agentic_fx.plugin.version_store._fsync_dir",
                        lambda d: synced.append(Path(d)))
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(   # spy を張った後に構築する
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    archive_tmp = Path(result.save_kwargs["archive_tmp"])
    assert synced == [archive_tmp, archive_tmp.parent]


def test_unreadable_candidate_files_are_loader_rejected_before_execution(
        loop_min, tmp_path, monkeypatch):
    """ローカル 1 周目 #11 (2026-09-10): 候補 3 ファイルのいずれかが読めない
    (`except OSError`) 経路も loader_rejected を返し backtest を起動しない
    こと。既存は content hash 不一致だけを通すので OSError 節は一度も
    踏まれず、返す文字列を書き換えても緑のままだった。"""
    called = threading.Event()
    monkeypatch.setattr(
        "agentic_fx.loops.improve_loop.holdout.run_in_sample",
        lambda *a, **kw: called.set())
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: SimpleNamespace(
            name=name, kind="strategy", timeframe="1h",
            content_hash="whatever", pairs=("USDJPY",)))
    candidate = tmp_path / "staging" / "myst"
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_bytes(b"x")
    (candidate / "config.yaml").write_bytes(b"y")
    # test_plugin.py を作らない → read_bytes が FileNotFoundError (OSError)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=candidate.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert result == {
        "error": "loader_rejected: content changed during backtest"}
    assert not called.is_set()


# ---- [profitability-floor] T2 Step 2-1 (2026-09-13): submission_blocked ----

def test_f7_1_submission_blocked_absent_when_in_sample_passes_floor(
        loop_min, tmp_path, monkeypatch):
    """F7-1: in_sample 段が合格するときはキー自体が無い。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)  # 既定 metrics: pf=1.3, avg_r=0.1 (合格)
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert "submission_blocked" not in result


def test_f7_1_submission_blocked_present_when_in_sample_fails_floor(
        loop_min, tmp_path, monkeypatch):
    """F7-1: in_sample 段が不合格のときだけ `submission_blocked` が現れる
    (`reason=unprofitable`)。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(
        monkeypatch,
        save_kwargs={**_SAVE_KWARGS,
                    "metrics": {"trades": 40, "pf": 0.3, "avg_r": -0.2,
                               "evaluable": True}})
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert result["submission_blocked"]["reason"] == "unprofitable"
    assert "pf=0.3" in result["submission_blocked"]["detail"]


def test_f7_2_submission_blocked_uses_same_settings_as_t1_min_pf(
        loop_min, tmp_path, monkeypatch):
    """F7-2: T1 と同じ判定関数 (`_check_profitability_floor`) を呼ぶ —
    `min_pf` を変えると判定が追従する (ハードコードではない)。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(
        monkeypatch,
        save_kwargs={**_SAVE_KWARGS,
                    "metrics": {"trades": 40, "pf": 1.3, "avg_r": 0.1,
                               "evaluable": True}})
    loop_min._settings = loop_min._settings.model_copy(deep=True)
    loop_min._settings.improve.gate.min_pf = 2.0  # pf=1.3 を不合格にする
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert result["submission_blocked"]["reason"] == "unprofitable"


def test_f7_4_submission_blocked_not_in_save_kwargs_or_ledger(
        loop_min, tmp_path, monkeypatch):
    """F7-4: `submission_blocked` は公開面 (agent へ返る dict) にのみ現れ、
    `save_kwargs` (台帳/Tx-2 が読む非公開の元データ) には混ざらない。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(
        monkeypatch,
        save_kwargs={**_SAVE_KWARGS,
                    "metrics": {"trades": 40, "pf": 0.3, "avg_r": -0.2,
                               "evaluable": True}})
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})
    assert "submission_blocked" in result
    assert "submission_blocked" not in result.save_kwargs


def test_f9_1_run_backtest_handler_calls_shared_floor_helper_in_sample_only(
        loop_min, tmp_path, monkeypatch):
    """F9-1 (親 `run_backtest_handler` 側の 3 番目の呼び出し元、2026-09-13
    F 番号 gap 充足): `_check_profitability_floor` が `scope="in_sample"`
    と実 metrics dict で呼ばれる (holdout は一切参照しない — T2 Step
    2-1 の契約)。"""
    import agentic_fx.loops.improve_loop as improve_loop_module

    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)

    calls = []
    real_fn = improve_loop_module._check_profitability_floor

    def _spy(per_pair, *, settings, scope):
        calls.append((scope, per_pair))
        return real_fn(per_pair, settings=settings, scope=scope)

    monkeypatch.setattr(
        improve_loop_module, "_check_profitability_floor", _spy)

    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})

    assert len(calls) == 1
    assert calls[0][0] == "in_sample"
    assert calls[0][1] == {"USDJPY": {"pf": 1.3, "trades": 40,
                                      "avg_r": 0.1, "evaluable": True}}


def test_f9_2_run_backtest_handler_helper_not_duplicated_inline(
        loop_min, tmp_path, monkeypatch):
    """F9-2: `run_backtest_handler` が helper を経由せず同等ロジックを
    複製していたら、差し替えは効かず `submission_blocked` は付かない —
    実際は経由しているので差し替えどおり付く。"""
    import agentic_fx.loops.improve_loop as improve_loop_module

    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(monkeypatch)  # pf=1.3 (既定で PASS するはず)

    monkeypatch.setattr(
        improve_loop_module, "_check_profitability_floor",
        lambda per_pair, *, settings, scope: ("unprofitable", "forced-by-spy"))

    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})

    assert result["submission_blocked"]["reason"] == "unprofitable"
    assert "forced-by-spy" in result["submission_blocked"]["detail"]


def test_f5_6_submission_blocked_has_no_holdout_language_or_period_endpoints(
        loop_min, tmp_path, monkeypatch):
    """F5-6 (2026-09-13、F 番号 gap 充足、遮断 8): `submission_blocked` に
    `"holdout"` の語・holdout の数値・期間端点が無い (既存 pin
    `tests/tools/test_improve_rpc_tools.py::
    test_run_backtest_records_to_ledger_and_returns_handler_result` の
    `"period_start" not in json.dumps(out)` を、submission_blocked が
    付く経路にも拡張する)。"""
    _patch_strategy_lookup(monkeypatch)
    _patch_run_in_sample(
        monkeypatch,
        save_kwargs={**_SAVE_KWARGS,
                    "metrics": {"trades": 40, "pf": 0.3, "avg_r": -0.2,
                               "evaluable": True}})
    staging = tmp_path / "staging" / "myst"
    staging.mkdir(parents=True)
    result = loop_min._build_rpc_handlers(
        ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        staging_dir=staging.parent)["run_backtest"](
            {"name": "myst", "pair": "USDJPY"})

    dumped = json.dumps(dict(result))
    assert "submission_blocked" in dumped
    assert "holdout" not in dumped
    assert "period_start" not in dumped
    assert "period_end" not in dumped

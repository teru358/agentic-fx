"""人間 CLI — history import/compare/coverage、backtest run、analyze corr
(プラン 6 Task 11)。

Task 4-10 で実装済みの importer / runner / metrics / analysis 関数を配線
するだけの薄いアダプタ。発注系ロジックはここに書かない。

DB 解決契約 (レビュー裁定 codex M5): root は ``Path.cwd()`` (entry.py が渡す)、
DB は ``root / "data" / "agentic.db"`` (init 済みを要求 — 無ければ
``ensure_initialized`` が案内して ``SystemExit(2)``)。CLI が触るのは
この実 DB のみ — in-memory 再生 DB は ``run_replay`` 内部に閉じる。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog
from agentic_fx.backtest.analysis import coverage_report, corr_matrix
from agentic_fx.backtest.importer import import_dukascopy
from agentic_fx.backtest.metrics import compute_metrics
from agentic_fx.backtest.mt5_import import compare_sources, import_mt5
from agentic_fx.backtest.runner import run_replay
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, Origin, TradeIntent
from agentic_fx.loops.verify_backend import VerifyBackendGateError
from agentic_fx.plugin import approval as plugin_approval
from agentic_fx.plugin import loader as plugin_loader
from agentic_fx.plugin import sandbox as plugin_sandbox
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.service import ensure_initialized
from agentic_fx.store import backtest_runs, ohlcv
from agentic_fx.store.db import connect, init_db

# analyze corr で --to 未指定時の in_sample_until (aware far-future 定数 —
# 上書き節 D 逐語)。人間の探索は期間自由なので実質「上限なし」を表す。
_NO_LIMIT = datetime(9999, 12, 28, tzinfo=timezone.utc)


def _parse_date(v: str) -> datetime:
    """``YYYY-MM-DD`` (時刻付き ISO も可) を aware UTC に変換する。

    時刻付き ISO でも tzinfo は無視し (naive として解釈)、UTC を付与する
    (上書き節 B 逐語)。
    """
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)


def register_subparsers(sub: "argparse._SubParsersAction") -> None:
    """entry.py の ``sub`` (top-level subparsers) に history/backtest/analyze
    を登録する。"""
    history = sub.add_parser("history", help="履歴データ操作")
    history_sub = history.add_subparsers(dest="history_command", required=True)

    imp = history_sub.add_parser("import", help="一括取り込み (進捗を件数で表示)")
    imp.add_argument("--source", choices=["dukascopy", "mt5"], required=True)
    imp.add_argument("--symbol", required=True)
    imp.add_argument("--from", dest="from_", type=_parse_date, required=True)
    imp.add_argument("--to", dest="to", type=_parse_date, required=True)
    imp.add_argument("--max-requests", type=int, default=None,
                     help="dukascopy: 1 回で投げるリクエスト上限 (既定 500)。"
                          "相手は無料公開サービスなので 1 秒間隔で投げる — "
                          "長期取得は範囲を分けるか、この値を明示的に上げること")

    comp = history_sub.add_parser("compare", help="source 間の close 差照合")
    comp.add_argument("--symbol", required=True)

    cov = history_sub.add_parser("coverage", help="カバレッジレポート")
    cov.add_argument("--symbol", required=True)
    cov.add_argument("--timeframe", required=True)
    cov.add_argument("--source", required=True, choices=sorted(ohlcv.IMPORT_SOURCES))
    cov.add_argument("--from", dest="from_", type=_parse_date, required=True)
    cov.add_argument("--to", dest="to", type=_parse_date, required=True)

    backtest = sub.add_parser("backtest", help="バックテスト")
    backtest_sub = backtest.add_subparsers(dest="backtest_command",
                                           required=True)
    run = backtest_sub.add_parser(
        "run", help="人間用バックテスト実行 (自由期間・scope=human_custom)")
    run.add_argument("--symbol", required=True)
    run.add_argument("--source", required=True, choices=sorted(ohlcv.IMPORT_SOURCES))
    run.add_argument("--from", dest="from_", type=_parse_date, required=True)
    run.add_argument("--to", dest="to", type=_parse_date, required=True)
    # opus R2 M6: --proposal-file (既存の JSONL 提案列経路) と --plugin
    # (プラン 7 — strategy plugin を IntentSource として評価する経路) は
    # 相互排他 (どちらか一方が必須)。既存の proposal 経路の動作・保存契約
    # (kind="proposals") は変えない。
    source_group = run.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--proposal-file")
    source_group.add_argument("--plugin", help="評価する strategy plugin の名前 "
                              "(<cwd>/plugins/<name>/)")
    run.add_argument("--timeframe", default="1h")

    analyze = sub.add_parser("analyze", help="履歴分析")
    analyze_sub = analyze.add_subparsers(dest="analyze_command",
                                         required=True)
    corr = analyze_sub.add_parser("corr", help="2 ペアの相関 (人間の自由探索)")
    corr.add_argument("--a", required=True)
    corr.add_argument("--b", required=True)
    corr.add_argument("--timeframe", required=True)
    corr.add_argument("--source", required=True, choices=sorted(ohlcv.IMPORT_SOURCES))
    corr.add_argument("--from", dest="from_", type=_parse_date, default=None)
    corr.add_argument("--to", dest="to", type=_parse_date, default=None)

    # plugin 承認フロー (プラン 7 Task 6): submit は pending 行を作るだけ、
    # bless は検証 + 即時承認 (人間 CLI からのみ — 改善ループには非露出)。
    plugin = sub.add_parser("plugin", help="plugin 承認フロー")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)
    submit = plugin_sub.add_parser(
        "submit", help="plugin を検証し承認申請 (pending) 行を作る")
    submit.add_argument("name")
    submit.add_argument(
        "--from", dest="from_kind", choices=["_human"], default=None,
        help="'_human' なら plugins/_human/<name> の候補を submit する "
             "(P1 candidate_origin=human、プラン10 Task11)")
    bless_parser = plugin_sub.add_parser(
        "bless", help="plugin を検証し即時承認する (人間 CLI 専用)")
    bless_parser.add_argument("name")
    bless_parser.add_argument(
        "--from", dest="from_kind", choices=["_human"], default=None,
        help="'_human' 必須 (裁定3: 指定なしは常に拒否 — materialize を案内)")
    materialize_parser = plugin_sub.add_parser(
        "materialize", help="live plain plugin を plugins/_human/<name> へ"
                            "読み取り専用コピーする (0700/0600)")
    materialize_parser.add_argument("name")
    retire_parser = plugin_sub.add_parser(
        "retire", help="legacy plain live plugin を plugins/_retired/ へ退避する"
                       " (flock、未完ジャーナルは拒否)")
    retire_parser.add_argument("name")

    improve = sub.add_parser("improve", help="改善ループ操作 (CLI 専用)")
    improve_sub = improve.add_subparsers(dest="improve_command", required=True)
    verify_backend_parser = improve_sub.add_parser(
        "verify-backend",
        help="scheduler/backlog に触れない one-shot backend 検証 "
            "(llama_swap_verified=false のまま実行できる唯一の経路)")
    verify_backend_parser.add_argument(
        "--backend", choices=("local", "claude", "codex"), required=True)
    verify_backend_parser.add_argument(
        "--provider", choices=("chatgpt", "llama_swap"), default=None,
        help="--backend codex のときのみ意味を持つ (runner.codex.provider "
            "の一時上書き)")


# ---- history import ------------------------------------------------------


def _make_dukascopy_progress():
    """時間単位の進捗コールバック — 件数を間引いて表示する。"""
    state = {"n": 0}

    def _cb(_current: datetime) -> None:
        state["n"] += 1
        if state["n"] % 100 == 0:
            print(f"import: {state['n']} 時間分処理済み")

    return _cb


def _history_import(conn, settings, args: argparse.Namespace) -> int:
    if args.source == "dukascopy":
        kw = {} if args.max_requests is None else {
            "max_requests": args.max_requests}
        result = import_dukascopy(conn, args.symbol, args.from_, args.to,
                                  progress=_make_dukascopy_progress(), **kw)
    else:  # mt5
        bridge_url = settings.datafeed.mt5.bridge_url
        if bridge_url is None:
            print("datafeed.mt5.bridge_url が未設定です", file=sys.stderr)
            return 1
        result = import_mt5(conn, args.symbol, args.from_, args.to,
                            base_url=bridge_url)
    print(f"inserted={result.inserted} unchanged={result.unchanged} "
         f"conflicted={result.conflicted}")
    return 0


def _history_compare(conn, settings, args: argparse.Namespace) -> int:
    result = compare_sources(conn, args.symbol, settings)
    print(result)
    return 0


def _history_coverage(conn, args: argparse.Namespace) -> int:
    result = coverage_report(conn, args.symbol, timeframe=args.timeframe,
                             source=args.source, start=args.from_,
                             end=args.to)
    print(result)
    return 0


# ---- backtest run ----------------------------------------------------------


def _load_proposals(path: Path) -> list[tuple[datetime, dict]]:
    """proposal-file (JSONL) を全行 parse し、ts 昇順に整列して返す。

    ts が欠落・parse 不能・naive なら ``ValueError`` (fail closed、部分実行
    しない — 上書き節 F 逐語)。

    Fix Round 1 F3 (sonnet I-4 = codex Important): ts だけでなく intent 本体
    の形状も **副作用 (run_replay 実行) の前に全行** 検証する。検証は
    ``runner.py`` の実呼び出し (``TradeIntent.from_llm_dict(pending_proposal,
    origin=Origin.SCHEDULER)``) と同じ形で行い、1 行でも不正なら
    ``ValueError`` (fail closed、何も実行しない)。許容 action は ``open``
    のみ (brief 「OPEN_INTENT 形」の裁定 — hold/close/cancel は proposal-file
    としては不正)。
    """
    proposals: list[tuple[datetime, dict]] = []
    text = path.read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {lineno}: invalid JSON ({e})") from e
        if not isinstance(obj, dict) or "ts" not in obj:
            raise ValueError(f"line {lineno}: missing 'ts'")
        ts_raw = obj["ts"]
        try:
            ts = datetime.fromisoformat(ts_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"line {lineno}: unparsable ts {ts_raw!r} ({e})") from e
        if ts.tzinfo is None:
            raise ValueError(f"line {lineno}: ts must be timezone-aware")
        ts = ts.astimezone(timezone.utc)
        intent = {k: v for k, v in obj.items() if k != "ts"}
        if intent.get("action") != "open":
            raise ValueError(
                f"line {lineno}: only action='open' is supported in "
                f"proposal-file, got {intent.get('action')!r}")
        # IntentParseError は ValueError のサブクラスなのでそのまま伝播させる
        # (ここで捕まえて包み直す必要はない — 呼び出し元は ValueError を見る)。
        TradeIntent.from_llm_dict(intent, origin=Origin.SCHEDULER)
        proposals.append((ts, intent))
    proposals.sort(key=lambda p: p[0])
    return proposals


class _ProposalIntentSource:
    """JSONL 提案列を ``IntentSource`` (Bar -> dict | None) に変換する薄い
    アダプタ (プラン 7 Task 5 で ``--plugin`` 経路が追加されたが、置き換え
    ではなく相互排他で共存する — コントローラ裁定)。

    ``closed_bar.ts >= ts`` の最古の未消費提案を 1 件返す。1 呼び出し
    (1 バー) で 1 件のみ消費し、残りは次バー以降に持ち越す。この「1 バー
    1 件」契約は ``run_replay`` が 1 closed bar につき本アダプタを最大 1 回
    しか呼ばない前提 (runner.py の評価バケット確定処理) に依存する —
    同一 ``Bar`` で 2 回呼ばれると 2 件消費してしまう (codex Minor 裁定)。
    """

    def __init__(self, proposals: list[tuple[datetime, dict]]) -> None:
        self._proposals = proposals
        self._idx = 0

    def __call__(self, closed_bar: Bar) -> dict | None:
        if self._idx >= len(self._proposals):
            return None
        ts, intent = self._proposals[self._idx]
        if closed_bar.ts >= ts:
            self._idx += 1
            return intent
        return None


def _empty_history_guard(conn, args: argparse.Namespace) -> bool:
    """F4 (最終レビュー opus I-4): 対象 source/期間に 1m 履歴が 0 行なら
    run_replay を呼ばずに fail closed する。run_replay 自体は空バー窓
    (市場クローズ期間等) の再生を前提にした既存テストと衝突しないよう
    変更しない (裁定) — CLI 側で先に検査する。0 行のまま黙って完走すると
    正常系と見分けのつかない human_custom 行が残る (--source のタイプミス
    等を検出できない)。真偽値で「履歴あり (続行可)」を返す — 無ければ
    ここで診断メッセージを出す。
    """
    n_bars = conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE symbol=? AND interval='1m' "
        "AND source=? AND bar_time >= ? AND bar_time < ?",
        (args.symbol, args.source, args.from_.isoformat(),
         args.to.isoformat())).fetchone()[0]
    if n_bars == 0:
        print(f"エラー: symbol={args.symbol} source={args.source} の指定期間に "
             "1m 履歴が 0 件です (source のタイプミスや未インポート期間の "
             "可能性があります)", file=sys.stderr)
        return False
    return True


def _backtest_run_proposal(conn, settings, args: argparse.Namespace) -> int:
    proposal_path = Path(args.proposal_file).resolve()
    try:
        proposals = _load_proposals(proposal_path)
    except ValueError as e:
        print(f"提案ファイルの読み込みに失敗しました: {e}", file=sys.stderr)
        return 1

    intent_source = _ProposalIntentSource(proposals)
    content_hash = hashlib.sha256(proposal_path.read_bytes()).hexdigest()

    if not _empty_history_guard(conn, args):
        return 1

    result = run_replay(settings, symbol=args.symbol, source=args.source,
                        start=args.from_, end=args.to,
                        intent_source=intent_source,
                        eval_timeframe=args.timeframe, history_conn=conn)
    metrics = compute_metrics(result)
    run_id = backtest_runs.save_human_run(
        conn, plugin_ref=str(proposal_path), content_hash=content_hash,
        kind="proposals", pair=args.symbol, timeframe=args.timeframe,
        source=args.source, period=(args.from_, args.to), metrics=metrics,
        settings_hash=backtest_runs.settings_snapshot_hash(settings),
        core_commit=backtest_runs.core_commit(),
        initial_balance=settings.backtest.initial_balance,
        now=datetime.now(timezone.utc))
    print(f"backtest run id={run_id}")
    print(metrics)
    return 0


def _backtest_run_plugin(conn, settings, args: argparse.Namespace,
                         root: Path) -> int:
    """`--plugin <name>` 経路 (プラン 7 Task 5)。discover + check_source は
    通す (承認 (approval_requests) は要求しない — 手元評価は承認前が自然)。
    plugins_dir 規約はアプリ全体で確立済みの ``root / "plugins"``
    (= 実行時の ``Path.cwd()``、service.py の承認済み plugin ロードと同じ)。
    """
    plugins_dir = root / "plugins"
    metas = plugin_loader.discover(plugins_dir) if plugins_dir.is_dir() else []
    meta = next((m for m in metas if m.name == args.plugin), None)
    if meta is None:
        print(f"エラー: plugin '{args.plugin}' が {plugins_dir} に見つかりません "
             "(discover で検出できる 3 ファイル構成か確認してください)",
             file=sys.stderr)
        return 1
    if meta.kind != "strategy":
        print(f"エラー: plugin '{args.plugin}' の kind は {meta.kind!r} です "
             "(backtest run --plugin は kind=strategy のみ対応)",
             file=sys.stderr)
        return 1
    if args.symbol not in meta.pairs:
        print(f"エラー: plugin '{args.plugin}' は symbol={args.symbol!r} を "
             f"宣言していません (pairs={meta.pairs})", file=sys.stderr)
        return 1
    try:
        plugin_sandbox.check_source(meta.path / "plugin.py")
    except plugin_sandbox.SandboxError as e:
        print(f"エラー: plugin '{args.plugin}' は sandbox 検査を通りません: {e}",
             file=sys.stderr)
        return 1

    if not _empty_history_guard(conn, args):
        return 1

    intent_source = strategy_adapter.build_intent_source(
        meta, conn=conn, pair=args.symbol, source=args.source,
        settings=settings)
    try:
        # strategy_adapter の「貫通」契約 (SandboxError を hold へ読み替え
        # ない・run_replay を止める) は変えない — run_replay は
        # SandboxError をそのまま送出させる。ここで catch するのは
        # 「人間向け CLI は生 traceback を出さない」という CLI 境界の
        # 確立方針を --plugin 経路にも適用するため (最終レビュー F2、
        # `plugin submit`/`bless` の既存 catch と同じ変換規律)。fail
        # closed の実体 (human_custom 行を残さない・rc≠0) は維持する。
        result = run_replay(settings, symbol=args.symbol, source=args.source,
                            start=args.from_, end=args.to,
                            intent_source=intent_source,
                            eval_timeframe=args.timeframe, history_conn=conn)
    except plugin_sandbox.SandboxError as e:
        print(f"エラー: plugin '{args.plugin}' の評価がサンドボックスエラーで"
             f"停止しました (backtest_runs 行は残しません): "
             f"{safe_error_text(e)}", file=sys.stderr)
        return 1
    finally:
        # 上書き節 3: run_replay が例外で終わってもサンドボックスプロセス
        # をリークさせない。
        intent_source.close()

    if intent_source.eval_count == 0:
        print("警告: plugin の評価が 1 回も発火しませんでした "
             "(宣言 timeframe・期間・データ範囲を確認してください)",
             file=sys.stderr)

    metrics = compute_metrics(result)
    run_id = backtest_runs.save_human_run(
        conn, plugin_ref=f"plugins/{meta.name}", content_hash=meta.content_hash,
        kind="strategy", pair=args.symbol, timeframe=args.timeframe,
        source=args.source, period=(args.from_, args.to), metrics=metrics,
        settings_hash=backtest_runs.settings_snapshot_hash(settings),
        core_commit=backtest_runs.core_commit(),
        initial_balance=settings.backtest.initial_balance,
        now=datetime.now(timezone.utc))
    print(f"backtest run id={run_id}")
    print(metrics)
    return 0


def _backtest_run(conn, settings, args: argparse.Namespace, root: Path) -> int:
    if args.plugin is not None:
        return _backtest_run_plugin(conn, settings, args, root)
    return _backtest_run_proposal(conn, settings, args)


# ---- analyze corr -----------------------------------------------------------


def _analyze_corr(conn, args: argparse.Namespace) -> int:
    in_sample_until = args.to if args.to is not None else _NO_LIMIT
    result = corr_matrix(conn, [args.a, args.b], timeframe=args.timeframe,
                         source=args.source, in_sample_until=in_sample_until,
                         since=args.from_)
    print(result[(args.a, args.b)])
    return 0


# ---- plugin submit / bless (プラン 7 Task 6) --------------------------------


def _find_plugin_meta(root: Path, name: str):
    """discover して ``name`` に一致する ``PluginMeta`` を探す。
    ``(plugins_dir, meta)`` を返す (``meta`` は見つからなければ ``None`` —
    呼び出し元がエラーメッセージに ``plugins_dir`` を使うため一緒に返す)。
    plugins_dir 規約はアプリ全体で確立済みの ``root / "plugins"``
    (`--plugin` 経路・service.py の承認済み plugin ロードと同じ)。"""
    plugins_dir = root / "plugins"
    metas = plugin_loader.discover(plugins_dir) if plugins_dir.is_dir() else []
    return plugins_dir, next((m for m in metas if m.name == name), None)


# 裁定3 (11d/11e): `afx plugin bless <name>` (--from なし) は常に拒否する。
_BLESS_NO_FROM_ERROR = (
    "afx plugin bless <name> は廃止されました。"
    "'afx plugin materialize <name>' で候補を書き出し、編集してから "
    "'afx plugin bless --from _human <name>' を実行してください。")


def _plugin_submit(conn, settings, args: argparse.Namespace, root: Path) -> int:
    if getattr(args, "from_kind", None) == "_human":
        # 11d/11e: --from _human は switch.submit_candidate (P1) を通す
        # (candidate_origin="human" — plugins/_human/<name> を候補にする)
        plugins_dir = root / "plugins"
        try:
            approval_id = plugin_switch.submit_candidate(
                conn, name=args.name, staging_dir=plugins_dir / "_human",
                candidate_origin="human", mission_id=None, backlog_id=None,
                settings=settings, now=datetime.now(timezone.utc))
        except (ValueError, plugin_sandbox.SandboxError,
                plugin_switch.CandidateMissingError) as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1
        print(f"承認申請 id={approval_id} (pending)")
        return 0

    plugins_dir, meta = _find_plugin_meta(root, args.name)
    if meta is None:
        print(f"エラー: plugin '{args.name}' が {plugins_dir} に見つかりません "
             "(discover で検出できる 3 ファイル構成か確認してください)",
             file=sys.stderr)
        return 1
    try:
        approval_id = plugin_approval.submit_plugin(
            conn, meta, settings=settings, now=datetime.now(timezone.utc))
    except (ValueError, plugin_sandbox.SandboxError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"approval id={approval_id}")
    return 0


def _plugin_bless(conn, settings, args: argparse.Namespace, root: Path) -> int:
    # 裁定3: --from _human 無しは常に拒否 (materialize を案内)。
    # 検収 m9 是正: 他の全エラーと同じく stderr へ (旧稿は stdout へ print
    # していて統一が取れていなかった)。
    if getattr(args, "from_kind", None) != "_human":
        print(_BLESS_NO_FROM_ERROR, file=sys.stderr)
        return 1
    plugins_dir = root / "plugins"
    human_dir = plugins_dir / "_human" / args.name
    try:
        approval_id = plugin_switch.bless_candidate(
            conn, name=args.name, human_dir=human_dir, settings=settings,
            now=datetime.now(timezone.utc), decided_by="human_cli")
    except (ValueError, plugin_sandbox.SandboxError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"approval id={approval_id}")
    return 0


def _plugin_materialize(conn, settings, args: argparse.Namespace, root: Path) -> int:
    plugins_dir = root / "plugins"
    try:
        dest = plugin_switch.materialize_plugin(plugins_dir, args.name)
    except (FileExistsError, FileNotFoundError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"materialize: {dest}")
    return 0


def _plugin_retire(conn, settings, args: argparse.Namespace, root: Path) -> int:
    plugins_dir = root / "plugins"
    activity = ActivityLog(root / "logs" / "activity.log")
    try:
        plugin_switch.retire_plugin(
            conn, plugins_dir, args.name, now=datetime.now(timezone.utc),
            activity=activity)
    except (plugin_switch.UnresolvedJournalError, ValueError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"plugin {args.name!r} を retire しました")
    return 0


def _improve_verify_backend(conn, settings, args: argparse.Namespace,
                            root: Path) -> int:
    from agentic_fx.core.contracts import SystemClock
    from agentic_fx.loops.verify_backend import verify_backend
    result = verify_backend(root, settings, backend=args.backend,
                           provider=args.provider, clock=SystemClock())
    if not result.ok:
        print(f"エラー: {result.detail}", file=sys.stderr)
        return 1
    print(result.detail)
    print(f"fingerprint: {result.fingerprint}")
    # I1 是正 (codex 1周目 verified-codex-round1.md cli.py:533 脚):
    # `--provider` を省略しても実効 provider が llama_swap なら案内を出す
    # 必要がある。`args.provider` (常に None のことがある) ではなく
    # `result.provider` (verify_backend が解決した実効値) を見る。
    if result.backend == "codex" and result.provider == "llama_swap":
        print("合格後、人間が config/settings.yaml の improve.llama_swap_verified "
             "を true に設定してください (この CLI は書き換えません)。")
    return 0


# ---- dispatch ---------------------------------------------------------------


def dispatch(args: argparse.Namespace, root: Path) -> int:
    ensure_initialized(root)
    settings = load_settings(root / "config" / "settings.yaml")

    # Fix Round 1 F6 (sonnet I-3): 統一エラー境界。人間向け CLI なので生の
    # traceback を出さない — 診断メッセージを stderr に出して rc=1 とする。
    # SystemExit (ensure_initialized 由来) は Exception ではないのでここを
    # 経由せず素通りする。DB 接続・初期化も catch 対象に含める (codex I-2)。
    try:
        conn = connect(root / "data" / "agentic.db")
        init_db(conn)
        try:
            if args.command == "history":
                if args.history_command == "import":
                    return _history_import(conn, settings, args)
                if args.history_command == "compare":
                    return _history_compare(conn, settings, args)
                return _history_coverage(conn, args)
            if args.command == "backtest":
                return _backtest_run(conn, settings, args, root)
            if args.command == "plugin":
                if args.plugin_command == "submit":
                    return _plugin_submit(conn, settings, args, root)
                if args.plugin_command == "materialize":
                    return _plugin_materialize(conn, settings, args, root)
                if args.plugin_command == "retire":
                    return _plugin_retire(conn, settings, args, root)
                if args.plugin_command == "bless":
                    return _plugin_bless(conn, settings, args, root)
                raise ValueError(f"unknown plugin subcommand: {args.plugin_command!r}")
            if args.command == "improve":
                return _improve_verify_backend(conn, settings, args, root)
            return _analyze_corr(conn, args)
        finally:
            conn.close()
    except (ValueError, KeyError, OSError, sqlite3.Error,
           VerifyBackendGateError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

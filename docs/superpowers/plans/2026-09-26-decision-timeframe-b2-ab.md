# [decision-timeframe-config] B-2 実装プラン 段 a + 段 b (v1.1) — 共通規約と段 a

設計書: `docs/superpowers/specs/2026-09-26-decision-timeframe-b2-design.md` v1.0 (2026-09-26 ユーザー承認)。
対象コードは main `43c6042` の現物。本プランは 2 ファイルに分ける:

- **本ファイル**: 共通の Global Constraints / プラン規約 / File Structure / AC 対応 / 依存図 / 呼び出し元の全数表 + **段 a (Ta1〜Ta3)** + 段 a 末の全体 green
- **段 b**: `docs/superpowers/plans/2026-09-26-decision-timeframe-b2-ab-stage-b.md` (Tb1〜Tb3 + 段 b 末の全体 green + 未実測の申告 + 変更履歴)

段 c (signal 購読) / 段 d (context 足と期限) は別プラン。**段 a を main に入れてから段 b に着手する。**

## Global Constraints

- **設計を変えない。** 設計書 v1.0 が正。設計書に無い判断が要るときは実装を止めて申告する ([[plan-code-defects-not-implementer-defects]])。本プランで設計書と食い違った箇所は「spec との食い違い」節 (本ファイル末尾) に「要 spec 修正」の印を付けて列挙した。実装はプラン記述に従い、そこに無い逸脱は申告義務がある。
- **資金保護の実行順序を変えない。** `Scheduler.tick` の `_force_close_day` (`scheduler.py:232`)・`_process_limit_fills`・`_process_exits` (`:239`) より前に cursor・session・受理判定のコードを置かない。新コードは `finally:` 節 (`:240-254`) の `_run_hooks` の後か、`finally` の後の受理判定ブロック (`:256-260`) にだけ置く (AC-B2-24)。
- **drawdown kill switch・Risk Gate・executor の判定に触らない。** B-2 は scheduler の起動判定・supervisor の受理結果・起動時検査・永続 cursor だけを変える。
- **tick 冒頭の `now` 1 回採取を守る。** `_scheduler_tick_once` (`service.py:1258-1319`) が採った `now` を scheduler 内の全判定 (due・session・cursor の `updated_at`・activity の summary) にそのまま使う。scheduler 内で `clock.now()` / `datetime.now()` を新たに読まない。
- **transaction 境界 (A2-3 spec §3.1 と同じ規律)。** DB 書込みは core_lock 保持中に `conn_core` でだけ行う。scheduler の書込みは `Scheduler.tick` (core_lock 内、`service.py:1289-1310`) から、TradeLoop の書込みは prepare の `with self._core_lock:` (`trade_loop.py:163-215`) から。SQL 例外後に `conn_core` へ未 rollback の transaction を残さない (IV-11)。
- **実 DB (`data/agentic.db`)・実 `config/settings.yaml`・`plugins/`・`policy/` を読み書きしない。** テストは `tmp_path` の SQLite だけ ([[tests-touching-real-repo-resources]])。`config/settings.yaml.example` に足すキーは個人の `settings.yaml` に実装 task が書かない。
- **外向き通信なし。** bridge / yfinance / LLM を叩かない。service 層テストは既存の `_no_real_network()` と `FakeRunner` を使う。
- **本体コードに互換層を入れない。** `on_trade_mission` の戻り値は `SubmitResult` だけを受ける。bool / `None` を黙って受ける分岐は書かない (テスト側の fake を新契約に書き換える)。
- **コードコメントに工程ラベル・レビュアー名・タスク番号 (Ta1 等) を入れない** ([[no-review-labels-in-code-comments]])。commit 前に追加行を `git -C <worktree> diff -U0 | grep -nE '^\+.*(Ta[0-9]|Tb[0-9]|AC-B2|IV-[0-9])'` と、メモリ [[no-review-labels-in-code-comments]] に列挙された工程ラベル・名前の grep で 0 件を確認する。
- **未コミットの差分の上で `git checkout` / `git restore` を使わない。** 逆変異の復元は `cp` 退避で行う ([[no-git-checkout-over-uncommitted-subagent-work]])。
- **`git -C <path>` で操作し `cd` しない。** 並列 task は worktree 分離 ([[subagent-inherits-session-cwd]])。
- **出力を `| head` / `| grep` で途中終了させない。** pytest の結果行は最後まで読む。
- **逐語転写は機械 diff する** ([[transcription-must-be-machine-diffed]])。本文のコードブロックを転写したら `diff` で 0 を確認してから次へ。

## プラン規約

- 各 task は「a テストを置く → **red の逐語確認** → b 実装転写 → c **green** → d **逆変異** → commit」の順。red/green は結果行を逐語で貼る。
- 逆変異は適用可能な形 (対象ファイル / 置換前 / 置換後 / red になるべきテスト名) で書く。**リストは下限** ([[mutation-testing]])。
- エスカレーション: 転写したテストが red にならない / 想定と違う理由で red / green にならない、のどれかが起きたら実装を止め、観測した逐語と疑う原因を報告する (自分で assert を緩めない、テストの期待値を書き換えない)。
- **逸脱の申告義務**: プランの文面と違うことを 1 行でもしたら、報告に「逸脱: 何を / なぜ」を列挙する。申告の無い逸脱は検収で差し戻す。
- 実装の受け手は worktree (`git worktree add` を scratchpad 配下) で作業し、各 task 末に commit する。main への統合は段単位で指揮者が行う。
- 行番号はすべて `43c6042` 時点。着手時に `grep -n` で再確認し、ずれていたら現物の行で当てる (内容の一致で当てる)。
- **「記述の具体化」の範囲**: spec との食い違い表で「記述の具体化のみ」と分類してよいのは、spec の文言を変えず挙動も変えない (「何が起きるか」が spec の記述どおりに成立する) 場合に限る。挙動が spec の記述と食い違う (例: spec は「稼働中も検出する」と言っているのに実装が「起動時にしか検出しない」) 場合は具体化ではなく差異であり、実装を先に進めず指揮者へ申告して裁定を仰ぐ (着手前検証で見つかった実例: 稼働中の cron_cursor 再読込みを「記述の具体化」と誤分類し、実装から抜け落ちていた)。

## File Structure

| ファイル | 変更 | task |
|---|---|---|
| `src/agentic_fx/core/mission_ceiling.py` | **新規**: Cw / Cd / watchdog 上限の算出 (純関数) と固定待ちの定数 | Ta1 |
| `src/agentic_fx/runners/worker_runner.py` | 固定待ち 3 か所 (`:617`・`:630`・`:710`) を `mission_ceiling` の定数参照に置換 (値は不変) | Ta1 |
| `src/agentic_fx/config.py` | `WorkerSettings.dispatch_ceiling_sec` 追加 / `DatafeedSettings` に派生専用足の判断足拒否 | Ta1 |
| `config/settings.yaml.example` | `worker.dispatch_ceiling_sec` のコメント行 | Ta1 |
| `src/agentic_fx/service.py` | `_check_mission_ceilings` 新設と build_app からの呼び出し / `_default_dispatch_ceiling_sec` を新式へ (Ta1)。`on_trade_mission`・`_SupervisorAsk.ask_once` を `SubmitResult` 化 (Ta3) | Ta1・Ta3 |
| `src/agentic_fx/core/supervisor.py` | `SubmitResult`・`_phase`・`clock_fn`、future 解決前の idle 化 | Ta2 |
| `src/agentic_fx/core/scheduler.py` | `_due_cron_watermarks`、`cron_mission_deferred` / `cron_mission_coalesced` | Ta2 |
| `src/agentic_fx/backtest/runner.py` | `:419` の fake を `SubmitResult` 契約へ | Ta3 |
| `tests/core/test_mission_ceiling.py` | **新規** (AC-B2-03 の算出部) | Ta1 |
| `tests/test_mission_ceiling_startup.py` | **新規** (AC-B2-03 の起動時検査・配線) | Ta1 |
| `tests/test_config.py` | **追記** (AC-B2-04) | Ta1 |
| `tests/test_stop_sequence.py` | `_minimal_app` stub の拡張 + `:57` の期待値を新式へ | Ta1 |
| `tests/test_service_app.py:1409-1441` | `test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling` の pin 値を新式へ (`:452-463・506-517・635-647` の spy 書換えは Ta3) | Ta1 |
| `tests/runners/fixtures/fake_worker_hanging_rpc.py` | **新規** (RPC 投げっぱなし fake 子、実 subprocess) | Ta1 |
| `tests/runners/test_worker_runner.py` | **追記** (`test_dispatcher_join_dominates_ceiling_when_rpc_hangs`、characterization test) | Ta1 |
| `tests/core/test_supervisor.py` | **追記** (AC-B2-03 の fake clock / queued / 解決順) + 既存 `try_submit` 21 か所の書換え | Ta2 |
| `tests/core/test_scheduler_decision_timeframe.py` | **新規** (AC-B2-01・02・24a、段 b でも追記) | Ta2・Ta3 |
| `tests/core/test_scheduler.py` | `Env._trade` と bool 契約サイトの書換え | Ta2 |
| `tests/test_service_app.py` | spy 3 本・直接 `try_submit` 3 か所・fake 2 か所の書換え + 追記 (service 1 tick の deferred) | Ta3 |
| `tests/test_wiring.py` / `tests/backtest/test_base_interval_convergence.py` / `tests/backtest/test_runner.py` / `tests/core/test_e2e_paper_cycle.py` | `on_trade_mission` fake を新契約へ | Ta3 |

## 受入条件 (設計書 §5) と task の対応

| AC | task | テスト名 (予定) |
|---|---|---|
| AC-B2-03 (算出) | Ta1 | `test_worker_ceiling_breakdown_matches_example_defaults` / `test_worker_ceiling_adds_cli_terminate_grace_only_for_claude` / `test_dispatch_and_watchdog_ceiling_derive_from_worker_ceiling` / `test_watchdog_ceiling_prefers_explicit_setting` / `test_reflection_batch_size_matches_ceiling_constant` / `test_dispatch_ceiling_formula` (書換え) |
| AC-B2-03 (起動時検査) | Ta1 | `test_startup_rejects_decision_timeframe_not_longer_than_worker_ceiling` / `test_startup_allows_15m_and_writes_one_dispatch_warning` / `test_startup_1h_writes_no_dispatch_warning` / `test_startup_rejects_explicit_dispatch_ceiling_below_cd` / `test_build_app_runs_mission_ceiling_check` |
| AC-B2-04 | Ta1 | `test_derived_only_decision_timeframe_is_rejected` (+ 既存 `test_decision_timeframe_contract` の 1h・15m) |
| AC-B2-03 (RPC 投げっぱなし・余白の性質差) | Ta1 | `tests/runners/test_worker_runner.py::test_dispatcher_join_dominates_ceiling_when_rpc_hangs` (characterization test、mission_ceiling の新設前から成立する挙動の回帰ピン) |
| AC-B2-03 (supervisor 解放時刻) / IV-12 | Ta2 | `test_try_submit_reports_running_until_fake_worker_releases_slot` / `test_try_submit_reports_queued_before_dispatch_starts` / `test_slot_is_idle_before_future_callbacks_run` / `test_shutdown_rejects_with_reason_shutdown` |
| AC-B2-01 | Ta2 | `test_busy_defers_once_with_elapsed_and_accepts_latest_closed_bar` |
| AC-B2-02 | Ta2 | `test_busy_across_two_bars_coalesces_to_latest_counting_db_bars` / `test_coalesced_is_not_written_when_intermediate_bar_is_missing` |
| AC-B2-24 (a) | Ta3 | `test_cursor_advance_exception_leaves_day_close_and_sl_already_done` |
| AC-B2-21 (段 a 分: 1h の発火リズム不変) | Ta2・Ta3 | 既存 `tests/core/test_scheduler.py` の cron 系 (`test_hourly_trade_mission` ほか) を assert 無改変で green |
| service 配線 | Ta3 | `test_service_tick_records_deferred_with_supervisor_phase` / `test_supervisor_ask_returns_busy_message_on_rejection` |

段 b の AC (05〜12, 21 の (a)(b), 22〜25) は段 b ファイルの対応表を見る。

## task 依存図

```text
Ta1 (mission_ceiling + 起動時検査 + watchdog 統一 + 派生足拒否) ─┐
Ta2 (SubmitResult + deferred/coalesced、scheduler/supervisor)   ─┴→ Ta3 (service/backtest 配線 + 全呼び出し元の書換え + AC-B2-24a) → 段 a 全体 green
```

- Ta1 と Ta2 は触るファイルが重ならない (Ta1: `mission_ceiling.py`・`worker_runner.py`・`config.py`・`service.py` の `_validate_startup` 周辺と `:1392-1396`・example・`test_stop_sequence.py`。Ta2: `supervisor.py`・`scheduler.py`・`test_supervisor.py`・`test_scheduler.py`・新規テスト)。worktree 分離で並列可。
- Ta3 は Ta1・Ta2 の統合後に 1 レーン直列 (service.py の別行域と全テストの書換えを持つ)。

## 呼び出し元の全数 (grep で数えた、`43c6042`)

数え方: `grep -rn 'on_trade_mission' src tests` = **52 行・16 ファイル**、`grep -rn 'try_submit' src tests` = **51 行・6 ファイル** (設計書 §10 の数と一致)。コメント・docstring・テスト名だけの行は「文字列一致のみ」として書換え不要に分類した。各行の扱いは下表。

### `on_trade_mission` (52 行)

| 場所 | 行 | 種別 | 段 a の扱い |
|---|---|---|---|
| `src/agentic_fx/core/scheduler.py` | 52 | 型注釈 | `Callable[..., SubmitResult]` へ (Ta2) |
| 同 | 68 | 代入 | 不変 |
| 同 | 258 | 呼び出し | `result = self.on_trade_mission(reason)`、`.accepted` を読む (Ta2) |
| 同 | 309, 312 | docstring | 文字列一致のみ |
| `src/agentic_fx/service.py` | 1077-1081 | 定義 | `-> SubmitResult`、`is not None` を外す (Ta3) |
| 同 | 1177 | kwarg 渡し | 不変 |
| 同 | 1602 | コメント | 文字列一致のみ |
| `src/agentic_fx/backtest/runner.py` | 419 | fake (実行時に呼ばれない: backtest の `ohlcv_cache` は空で cron が due にならない。推測、段 a 全体 green で確定) | `lambda reason, **_: SubmitResult.rejected("shutdown")` (Ta3) |
| `tests/core/test_scheduler.py` | 101 | kwarg 渡し | 不変 |
| 同 | 107-110 | `Env._trade` (呼ばれる) | `def _trade(self, reason, **_kwargs)` + `return SubmitResult.accepted_with(None)` (Ta2) |
| 同 | 167 | docstring | 文字列一致のみ |
| 同 | 189 | `env._trade = lambda reason: False` (呼ばれる) | `lambda reason, **_: SubmitResult.rejected("running")` (Ta2) |
| 同 | 190 | 代入 | 不変 |
| 同 | 196 | `lambda reason: env.trade_reasons.append(reason) or True` (呼ばれる) | `lambda reason, **_: env.trade_reasons.append(reason) or SubmitResult.accepted_with(None)` (Ta2) |
| 同 | 404 | `on_trade_mission=lambda: None` (構築だけ、呼ばれない) | 不変 (`TypeError` 検査の kwargs 一式を作るだけ) |
| 同 | 1236, 2935, 2936, 2938, 2954, 2963, 2964 | docstring/コメント/テスト名 | 文字列一致のみ (2935-2938 の「bool」説明文は Ta2 で「SubmitResult」に直す、assert は不変) |
| 同 | 2941-2948 (`boom`) | 例外を投げる fake (呼ばれる) | `def boom(reason, **_kwargs)` (段 b で kw が増えても `TypeError` に化けないため。assert は不変) (Ta2) |
| 同 | 2968 | `lambda reason: False` (呼ばれる) | `lambda reason, **_: SubmitResult.rejected("running")` (Ta2) |
| `tests/test_service_app.py` | 436, 437, 448, 478, 479, 480, 502, 611, 631, 653, 1631, 3991 | テスト名/docstring/コメント | 文字列一致のみ |
| 同 | 3092 | `lambda reason: fired.append(reason) or True` | `lambda reason, **_: fired.append(reason) or SubmitResult.accepted_with(None)` (Ta3) |
| 同 | 3993-3994 | `lambda reason: mission_calls.append(reason) or True` | `lambda reason, **_: mission_calls.append(reason) or SubmitResult.accepted_with(None)` (Ta3) |
| `tests/test_wiring.py` | 82 | fake (このファイルでは cron が due にならず呼ばれない: `:106` が `trade_calls == []` を assert) | `lambda reason, **_: trade_calls.append(reason) or SubmitResult.rejected("running")` (Ta3) |
| `tests/backtest/test_base_interval_convergence.py` | 849, 857, 912 | fake (呼ばれない、推測) | `lambda reason, **_: SubmitResult.rejected("shutdown")` (Ta3) |
| `tests/backtest/test_runner.py` | 670 | 同上 | 同上 (Ta3) |
| `tests/core/test_e2e_paper_cycle.py` | 57 | 同上 | 同上 (Ta3) |
| `tests/test_closed_bars_wiring.py` | 242 | 本物を横流し | 不変 |
| `tests/test_e2e_worker_isolation.py` | 166 | 文字列 | 文字列一致のみ |
| `tests/test_e2e_phase1.py` | 76 / `tests/test_e2e_plugin_signal.py` | 218 | コメント | 文字列一致のみ |
| `tests/store/test_rag_lock.py` | 173 | コメント | 文字列一致のみ |
| `tests/loops/test_trade_loop.py` | 198 | `test_non_trade_missions...` (部分一致) | 対象外 |
| `tests/core/test_executor_category.py` | 37, 276 | `route_non_trade_mission` (部分一致) | 対象外 |

**fake の引数規約**: 段 a で書き換える fake は全部 `(reason, **_kwargs)` を受ける形にする。段 b (Tb3) で scheduler が `decision_bars=` をキーワードで渡し始めても fake を二度書き換えないための**テスト側の書式の統一**であり、段 b の機能を先に入れるものではない (段 a の scheduler は `self.on_trade_mission(reason)` の 1 引数で呼ぶ)。

### `try_submit` (51 行)

| 場所 | 行 | 段 a の扱い |
|---|---|---|
| `src/agentic_fx/core/supervisor.py` | 4 (docstring) / 61 (定義) | 61 を `-> SubmitResult` へ (Ta2) |
| `src/agentic_fx/service.py` | 657 | `result = ...; if not result.accepted: <busy 文言>; future = result.future` (Ta3) |
| 同 | 1081 | `return supervisor.try_submit("trade", trigger=trigger)` (Ta3) |
| 同 | 1079, 1602 | コメント (1079 は Ta3 で書き直す) |
| `tests/core/test_supervisor.py` | 27-28, 40-41, 44-45, 55-58, 76, 102-103, 107-108, 119, 130, 132-133, 156-157, 196 (コード 16 行、`:172` は戻り値を使っていないため無改変) + 23, 33, 51, 84, 148, 155 (名前/docstring 6 行) | Ta2 Step 2-a の書換え表 |
| `tests/test_service_app.py` | spy 3 本 (`:455-461`・`:509-515`・`:638-644`、それぞれ `:463`・`:517`・`:647` で patch) | `r = original_try_submit(...)`; `if r.accepted: captured.append(r.future)`; `return r` (Ta3)。設計書 §10 は `:452-463` の 1 本だけを挙げているが同型が 3 本ある |
| 同 | 589-590, 1597-1598 | `future = app.supervisor.try_submit(...).future` + `assert future is not None` は不変 (Ta3) |
| 同 | 1673 | `rejected = app.supervisor.try_submit(...)`; `assert rejected.accepted is False and rejected.reason == "shutdown"` (Ta3) |
| 同 | 452, 506, 635, 1627, 1631, 1637, 1668 | コメント/テスト名 | 文字列一致のみ |
| `tests/test_e2e_worker_isolation.py` | 166 | 文字列一致のみ |
| `tests/core/test_scheduler.py` | 2965 | docstring (Ta2 で文言だけ直す) |

---

## Ta1: Cw / Cd の算出・起動時検査・watchdog 上限の統一・派生専用足の拒否

**担当**: 設計書 §3.3 (Cw/Cd・起動時検査・watchdog 統一)、§7 (`worker.dispatch_ceiling_sec`・派生専用足)、AC-B2-03 (算出と起動時検査)、AC-B2-04。

**数値の扱い (§9-1 実測を取り込み済み)**: `tmp/design-b2/measurements.md` #1 (実 `WorkerRunner` + SIGTERM を無視する fake 子の実時間計測) で、Cw = 30+330+10+5+5+5+20 = 405 (claude 415)、Cd = 4×Cw+10 = 1630/1670 の**式と値は変更不要**と確認済み。watchdog 1690/1730 もそのまま。実測で分かった性質差: SIGKILL 後 wait・その再実行・`reader.join` の計 15 秒は正常系ではほぼ消費されない安全マージン (実測 0.001 秒台、`_ensure_dead` の 2 回目は no-op で「wait が 2 回起きる」経路は本環境で再現できず未検証)、一方 `dispatcher.join` (`rpc_timeout_sec`+5) は RAG RPC が張り付くと mission timeout と独立に実測でも支配的になる (シナリオ C)。どちらも式に残す (上界として正しい)。この性質差の回帰として、AC-B2-03 に「RPC を投げっぱなしにする fake 子」のケースを 1 本足す — `tests/runners/test_worker_runner.py::test_dispatcher_join_dominates_ceiling_when_rpc_hangs` (下記 Step 1-a)。`MissionSupervisor._dispatch` の trade→reflection 直列で busy が合計時間保持されることも同実測で確認済み (Cd の前提)。

### Step 1-a: テストを置いて red を確認する

- [ ] `tests/core/test_mission_ceiling.py` を新規作成:

```python
"""判断 mission の上限 Cw (worker 1 回) / Cd (dispatch 全体) の算出。"""
import inspect
from pathlib import Path

from agentic_fx.config import RunnerChoice, load_settings
from agentic_fx.core import mission_ceiling
from agentic_fx.loops.reflection_cycle import ReflectionCycle

EXAMPLE = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _with(settings, *, backend=None, dispatch_ceiling_sec=None):
    s = settings
    if backend is not None:
        s = s.model_copy(update={"runner": s.runner.model_copy(update={
            "trade": RunnerChoice(backend=backend, model="m")})})
    if dispatch_ceiling_sec is not None:
        s = s.model_copy(update={"worker": s.worker.model_copy(update={
            "dispatch_ceiling_sec": dispatch_ceiling_sec})})
    return s


def test_worker_ceiling_breakdown_matches_example_defaults():
    cw = mission_ceiling.worker_ceiling(EXAMPLE)
    assert (cw.startup, cw.deadline, cw.terminate, cw.kill_wait,
            cw.kill_wait_retry, cw.reader_join, cw.dispatcher_join,
            cw.cli_terminate) == (30.0, 330.0, 10.0, 5.0, 5.0, 5.0, 20.0, 0.0)
    assert cw.total == 405.0


def test_worker_ceiling_adds_cli_terminate_grace_only_for_claude():
    cw = mission_ceiling.worker_ceiling(_with(EXAMPLE, backend="claude"))
    assert cw.cli_terminate == 10.0
    assert cw.total == 415.0


def test_dispatch_and_watchdog_ceiling_derive_from_worker_ceiling():
    assert mission_ceiling.dispatch_ceiling_sec(EXAMPLE) == 1630.0
    assert mission_ceiling.watchdog_ceiling_sec(EXAMPLE) == 1690.0
    claude = _with(EXAMPLE, backend="claude")
    assert mission_ceiling.dispatch_ceiling_sec(claude) == 1670.0
    assert mission_ceiling.watchdog_ceiling_sec(claude) == 1730.0


def test_watchdog_ceiling_prefers_explicit_setting():
    s = _with(EXAMPLE, dispatch_ceiling_sec=2000.0)
    assert mission_ceiling.watchdog_ceiling_sec(s) == 2000.0
    assert mission_ceiling.dispatch_ceiling_sec(s) == 1630.0


def test_reflection_batch_size_matches_ceiling_constant():
    default = inspect.signature(ReflectionCycle.run_pending).parameters[
        "max_items"].default
    assert default == mission_ceiling.REFLECTIONS_PER_DISPATCH == 3
```

- [ ] `tests/test_mission_ceiling_startup.py` を新規作成:

```python
"""起動時検査: Cw >= 判断足幅は拒否、Cd >= 判断足幅は WARNING 1 回。"""
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import RunnerChoice, load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _check_mission_ceilings, build_app
from tests.test_service_app import NOW, _init

EXAMPLE = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")


def _with(settings, *, decision=None, backend=None, dispatch_ceiling_sec=None):
    s = settings
    if decision is not None:
        s = s.model_copy(update={"datafeed": s.datafeed.model_copy(update={
            "decision_timeframes": [decision], "primary_intervals": [decision]})})
    if backend is not None:
        s = s.model_copy(update={"runner": s.runner.model_copy(update={
            "trade": RunnerChoice(backend=backend, model="m")})})
    if dispatch_ceiling_sec is not None:
        s = s.model_copy(update={"worker": s.worker.model_copy(update={
            "dispatch_ceiling_sec": dispatch_ceiling_sec})})
    return s


def _events(path):
    if not path.exists():
        return []
    return [line.split("\t") for line in
            path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(("backend", "cw"), [("local", 405), ("claude", 415)])
def test_startup_rejects_decision_timeframe_not_longer_than_worker_ceiling(
        tmp_path, backend, cw):
    activity = ActivityLog(tmp_path / "a.log")
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(_with(EXAMPLE, decision="5m", backend=backend),
                                activity)
    msg = str(exc.value)
    assert "判断足 5m (300 秒)" in msg
    assert f"Cw {cw} 秒" in msg
    assert "1 確定足に 1 回の判断が成立しません" in msg
    assert "llama_swap.timeout_sec" in msg


def test_startup_rejects_when_only_total_ceiling_exceeds_width(tmp_path):
    """`cw.deadline` (280 秒) 単独では 5m (300 秒) 未満だが、`cw.total`
    (355 秒、起動待ち・SIGTERM 猶予・SIGKILL 後 wait・join を含む) は
    足幅以上になる設定。`deadline_budget` だけで比較する実装はこれを
    見逃して起動を許してしまうので、`cw.total` で比較しているかを直接
    検査する。既定値の 5m だけでは `cw.deadline` (330 秒) も足幅以上に
    なり両方の実装が起動を拒否するため区別できない — deadline < 足幅
    <= total になる設定をこの 1 本で足して区別する。"""
    settings = _with(EXAMPLE, decision="5m")
    settings = settings.model_copy(update={"llama_swap": settings.llama_swap.model_copy(
        update={"timeout_sec": 250.0})})
    activity = ActivityLog(tmp_path / "a.log")
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(settings, activity)
    assert "Cw 355 秒" in str(exc.value)


@pytest.mark.parametrize(("backend", "cd"), [("local", 1630), ("claude", 1670)])
def test_startup_allows_15m_and_writes_one_dispatch_warning(tmp_path, backend, cd):
    path = tmp_path / "a.log"
    _check_mission_ceilings(_with(EXAMPLE, decision="15m", backend=backend),
                            ActivityLog(path))
    events = _events(path)
    assert [e[2] for e in events] == ["mission_ceiling_dispatch_warning"]
    assert "判断足 15m (900 秒)" in events[0][3]
    assert f"Cd {cd} 秒" in events[0][3]


def test_startup_1h_writes_no_dispatch_warning(tmp_path):
    path = tmp_path / "a.log"
    _check_mission_ceilings(EXAMPLE, ActivityLog(path))
    assert _events(path) == []


def test_startup_rejects_explicit_dispatch_ceiling_below_cd(tmp_path):
    activity = ActivityLog(tmp_path / "a.log")
    with pytest.raises(RuntimeError) as exc:
        _check_mission_ceilings(_with(EXAMPLE, dispatch_ceiling_sec=1629.0),
                                activity)
    assert "worker.dispatch_ceiling_sec=1629" in str(exc.value)
    assert "Cd 1630 秒" in str(exc.value)
    _check_mission_ceilings(_with(EXAMPLE, dispatch_ceiling_sec=1630.0), activity)


def test_build_app_runs_mission_ceiling_check(tmp_path):
    import agentic_fx.service as service_mod

    _init(tmp_path)
    seen = []
    real = service_mod._check_mission_ceilings

    def spy(settings, activity):
        seen.append(settings.datafeed.decision_timeframe)
        return real(settings, activity)

    with patch("agentic_fx.service._check_mission_ceilings", spy):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert seen == ["1h"]
    finally:
        app.close()
```

- [ ] `tests/test_config.py` の `test_decision_timeframe_contract` の直後に追記:

```python
@pytest.mark.parametrize("decision", ["4h", "1d"])
def test_derived_only_decision_timeframe_is_rejected(decision):
    import yaml

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["datafeed"].pop("primary_intervals")
    raw["schedule"].pop("trade_interval_min")
    raw["datafeed"]["decision_timeframes"] = [decision]
    # `intervals` に判断足自身を含めておく — 含めないと既存の
    # `_check_intervals` (primary_intervals は intervals の部分集合) が
    # 先に落ち、この validator の「派生専用」メッセージまで到達しない。
    if decision not in raw["datafeed"]["intervals"]:
        raw["datafeed"]["intervals"] = [*raw["datafeed"]["intervals"], decision]
    with pytest.raises(ValidationError, match="派生専用") as exc:
        Settings.model_validate(raw)
    assert "[1h]" in str(exc.value)
```

- [ ] `tests/test_stop_sequence.py` の `_minimal_app` (`:19-36`) の stub と `test_dispatch_ceiling_formula` (`:48-57`) を書き換える (assert の対象値だけが新式に変わる。関数名・docstring の意図は不変):

```python
def _minimal_app(*, backend="local", dispatch_ceiling_sec=None):
    class Worker:
        worker_grace_sec = 3.0
        worker_terminate_grace_sec = 2.0
        worker_startup_timeout_sec = 4.0
        rpc_timeout_sec = 1.0
    Worker.dispatch_ceiling_sec = dispatch_ceiling_sec
    class Llama: timeout_sec = 42.0
    class Trade: pass
    Trade.backend = backend
    class Runner:
        trade = Trade()
        cli_terminate_grace_sec = 7.0
    class Settings: worker = Worker(); llama_swap = Llama(); runner = Runner()
    class Supervisor:
        heartbeat = time.monotonic()
        busy_since = None
        def is_alive(self): return True
        def fail_pending(self, *, exc): self.failed = exc
    app = type("AppStub", (), {})()
    app.settings = Settings()
    app.supervisor = Supervisor()
    app.activity = _Activity()
    app.notifier = _Notifier()
    app.fatal_reason = None
    app.watchdog_heartbeat = time.monotonic()
    app.mission_watch = type("Watch", (), {"breached": lambda self, grace_sec: None})()
    return app


def test_dispatch_ceiling_formula():
    """ceiling は Cd (trade 1 回 + reflection 最大 3 回、worker 1 回の上限 Cw
    の 4 倍 + 余白 10) + watchdog 余裕 60。明示設定があればそれを使う。

    Cw = 起動待ち 4 + deadline (42+3) + SIGTERM 猶予 2 + kill wait 5 + 再実行 5
         + reader.join 5 + dispatcher.join (1+5) = 72
    """
    assert _default_dispatch_ceiling_sec(_minimal_app()) == 72.0 * 4 + 10.0 + 60.0
    assert _default_dispatch_ceiling_sec(_minimal_app(backend="claude")) == \
        (72.0 + 7.0) * 4 + 10.0 + 60.0
    assert _default_dispatch_ceiling_sec(
        _minimal_app(dispatch_ceiling_sec=500.0)) == 500.0
```

- [ ] `tests/runners/fixtures/fake_worker_hanging_rpc.py` を新規作成 (実 subprocess で spawn する fake 子。ready 応答直後に `tool_rpc` を投げ、以後 SIGTERM を無視して居座る。`tmp/design-b2/measurements.md` #1 シナリオ C の実測スクリプトをそのまま転写):

```python
import json
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)

sys.stdin.buffer.readline()  # handshake
out = sys.stdout.buffer


def send(frame):
    out.write((json.dumps(frame) + "\n").encode())
    out.flush()


send({"type": "ready", "seq": 1, "ok": True})
# 直後に slow RPC を投げ、以後は result を送らず居座る
send({"type": "tool_rpc", "seq": 2, "rpc_id": "r1", "name": "slow", "args": {}})
while True:
    time.sleep(0.05)
```

- [ ] `tests/runners/test_worker_runner.py` の末尾に追記 (AC-B2-03 の「別途」節、`dispatcher.join(rpc_timeout_sec+5)` が `_escalate_kill`/`_ensure_dead` 完了後も Cw を支配し得ることの回帰。spec §3.3 の余白の性質差の注記に対応):

```python
def test_dispatcher_join_dominates_ceiling_when_rpc_hangs(tmp_path):
    """RPC ハンドラがハングし続ける実 subprocess に対し、`_escalate_kill`/
    `_ensure_dead` (deadline+terminate_grace ≈ 1.2 秒) が完了した後も、
    `finally` の `dispatcher.join(rpc_timeout_sec+5)` が満了するまで
    `_run_with_child` が返らないこと (= Cw を実際に押し上げる経路である
    こと) を実時間 (短縮値) で検証する。
    """
    import subprocess

    fixture = (Path(__file__).resolve().parent / "fixtures" /
               "fake_worker_hanging_rpc.py")
    settings = SETTINGS.model_copy(update={"worker": SETTINGS.worker.model_copy(
        update={"worker_startup_timeout_sec": 3.0, "worker_grace_sec": 0.5,
                "worker_terminate_grace_sec": 0.2, "rpc_timeout_sec": 3.0})})
    clock = FixedClock(NOW)
    runner = WorkerRunner(
        root=_root(tmp_path), settings=settings, clock=clock,
        rag=_rag(tmp_path), rpc_handlers={"slow": lambda args: time.sleep(100)})
    mission = Mission(prompt="x", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=0.5)
    proc = subprocess.Popen(
        [sys.executable, str(fixture)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True)

    t0 = time.monotonic()
    result = runner._run_with_child(proc, mission, settings.worker)
    elapsed = time.monotonic() - t0

    assert result.status == "timeout"
    # mission timeout 経路 (deadline+terminate_grace ≈ 1.2 秒) だけなら
    # 2 秒未満で返るはずだが、dispatcher.join がハンドラの返り待ちで
    # rpc_timeout_sec (3.0 秒) 分を追加で消費する。
    assert elapsed >= 3.0
    assert elapsed < 3.0 + 5.0 + 1.0  # rpc_timeout_sec + join マージン + 余裕
```

  **この 1 本は `mission_ceiling` の新設と無関係に、現物の `WorkerRunner._run_with_child` に対する characterization test** (Cw の式・起動時検査を作る前から成立する挙動の回帰ピン) であり、下記 Step 1-a の「red を確認する」対象には含めない — 最初から green のはずで、実装後もそのまま green を維持する。

- [ ] **既存 pin の見落とし是正 (実測で判明、2026-09-26 着手前検証)**: `tests/test_service_app.py:1409-1441` の `test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling` は旧式 `(42.0+3.0+2.0)*4+60.0` を **pin しており、他の書換え対象と一緒にここで直さないと転写だけでは通らない**。spec との食い違い #3 は「`test_stop_sequence.py:57` だけが式を pin する」としていたが、これはもう 1 本の pin を見落としていた (spec v1.1 で訂正済み)。`test_stop_sequence.py` の書換えと同じタイミングで、ここに書き換える:

```python
    assert recorded.get("timeout") == pytest.approx(112.0 * 4 + 10.0 + 60.0)
```

  (`_seam_app` の設定 (`llama_swap.timeout_sec=42.0`・`worker_grace_sec=3.0`・`worker_terminate_grace_sec=2.0`、`worker_startup_timeout_sec`=既定 30・`rpc_timeout_sec`=既定 15・backend=local) から `Cw=30+(42+3)+2+5+5+5+(15+5)=112`、`Cd=112×4+10=458`、新 pin 値は `112.0*4+10.0+60.0=518.0`)。docstring の「`llama_swap.timeout_sec + ... + 10.0` から計算」も新式の説明に直す。

- [ ] red を確認する。**collection error があると `Interrupted` になり後続の red が観測できないため 2 回に分ける** (実測で判明、2026-09-26 着手前検証):
      1. `uv run pytest -q tests/core/test_mission_ceiling.py tests/test_mission_ceiling_startup.py`
      2. `uv run pytest -q "tests/test_config.py::test_derived_only_decision_timeframe_is_rejected" tests/test_stop_sequence.py::test_dispatch_ceiling_formula`
      3. `uv run pytest -q tests/test_service_app.py -k "dispatch_ceiling or join_budget or shutdown_timeout"` (3 本ヒット: `test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling`・`test_watchdog_ceiling_and_join_budget_come_from_the_same_value`・`test_run_service_records_shutdown_timeout_when_commit_core_is_stuck`。上記で書き換えた `:1409-1441` を含め、旧式を pin している既存テストをここで洗い出す — Step 1-b の実装前は現物の旧式が `248.0` を返すため、書き換え済みの `:1409-1441` だけが `assert 248.0 == 518.0 ± ...` で `1 failed, 2 passed` になる)
      期待: 前 2 ファイルは `ImportError: cannot import name 'mission_ceiling' from 'agentic_fx.core'` / `ImportError: cannot import name '_check_mission_ceilings' from 'agentic_fx.service'` の collection error (`from agentic_fx.core import mission_ceiling` という import 文のため `ModuleNotFoundError` ではなく `ImportError` になる。意味は同じ)。`test_derived_only_decision_timeframe_is_rejected[4h]` は `Failed: DID NOT RAISE ValidationError`。**`[1d]` は raw の `intervals` に `1d` が含まれていないため、既存の `_check_intervals` が先に `primary_intervals must be a subset of intervals` で落ち、期待した「派生専用」の正規表現に一致しない** (実測で判明、2026-09-26 着手前検証) — テストの raw に `intervals` へ `1d` を足して `_check_intervals` を素通りさせ、新設 validator の `派生専用` メッセージだけで落ちるようにする。`test_dispatch_ceiling_formula` は `assert 248.0 == 358.0` 系。

### Step 1-b: 実装を転写する

- [ ] `src/agentic_fx/core/mission_ceiling.py` を新規作成:

```python
"""判断 mission がスロットを占有し得る上限。

worker 1 回の上限 Cw は、子の起動待ち・実行 deadline・SIGTERM 猶予・
SIGKILL 後の wait (finally での再実行分を含む)・reader/dispatcher の join・
(backend=claude なら) CLI の回収猶予を直列に足した保守的な上界。
dispatch 全体の上限 Cd は trade 1 回と reflection 最大 3 回を同じ Cw で
見積もり、lock/SQL 等の未計測分に固定の余白を足す。watchdog と停止時の
join 予算はこの Cd に検出遅延の余裕を足した 1 本の値を共有する。
終了を保証するのは worker の deadline と SIGKILL であり、tool の予算ではない。
"""
from __future__ import annotations

from dataclasses import dataclass

# WorkerRunner の打ち切り経路の固定待ち (runners/worker_runner.py が参照する)。
KILL_WAIT_SEC = 5.0
READER_JOIN_SEC = 5.0
DISPATCHER_JOIN_MARGIN_SEC = 5.0
# MissionSupervisor._dispatch は trade の後に ReflectionCycle.run_pending()
# (既定 max_items=3) を同じ dispatch 内で呼ぶ。
REFLECTIONS_PER_DISPATCH = 3
DISPATCH_MARGIN_SEC = 10.0
WATCHDOG_MARGIN_SEC = 60.0


@dataclass(frozen=True)
class WorkerCeiling:
    startup: float
    deadline: float
    terminate: float
    kill_wait: float
    kill_wait_retry: float
    reader_join: float
    dispatcher_join: float
    cli_terminate: float

    @property
    def total(self) -> float:
        return (self.startup + self.deadline + self.terminate + self.kill_wait
                + self.kill_wait_retry + self.reader_join
                + self.dispatcher_join + self.cli_terminate)

    def breakdown_text(self) -> str:
        return (f"起動待ち {self.startup:g} + 実行 deadline {self.deadline:g}"
                f" + SIGTERM 猶予 {self.terminate:g}"
                f" + SIGKILL 後 wait {self.kill_wait:g}"
                f" + wait 再実行 {self.kill_wait_retry:g}"
                f" + reader.join {self.reader_join:g}"
                f" + dispatcher.join {self.dispatcher_join:g}"
                f" + CLI 回収 {self.cli_terminate:g}")


def worker_ceiling(settings) -> WorkerCeiling:
    w = settings.worker
    cli = (settings.runner.cli_terminate_grace_sec
           if settings.runner.trade.backend == "claude" else 0.0)
    return WorkerCeiling(
        startup=float(w.worker_startup_timeout_sec),
        deadline=float(settings.llama_swap.timeout_sec + w.worker_grace_sec),
        terminate=float(w.worker_terminate_grace_sec),
        kill_wait=KILL_WAIT_SEC,
        kill_wait_retry=KILL_WAIT_SEC,
        reader_join=READER_JOIN_SEC,
        dispatcher_join=float(w.rpc_timeout_sec + DISPATCHER_JOIN_MARGIN_SEC),
        cli_terminate=float(cli))


def dispatch_ceiling_sec(settings) -> float:
    cw = worker_ceiling(settings).total
    return cw * (1 + REFLECTIONS_PER_DISPATCH) + DISPATCH_MARGIN_SEC


def watchdog_ceiling_sec(settings) -> float:
    explicit = getattr(settings.worker, "dispatch_ceiling_sec", None)
    if explicit is not None:
        return float(explicit)
    return dispatch_ceiling_sec(settings) + WATCHDOG_MARGIN_SEC
```

- [ ] `src/agentic_fx/runners/worker_runner.py`: import 節 (`:26` の直後) に `from agentic_fx.core.mission_ceiling import (DISPATCHER_JOIN_MARGIN_SEC, KILL_WAIT_SEC, READER_JOIN_SEC)` を足し、`:617` `reader.join(timeout=5.0)` → `reader.join(timeout=READER_JOIN_SEC)`、`:630` `dispatcher.join(timeout=dispatcher_timeout_sec + 5.0)` → `dispatcher.join(timeout=dispatcher_timeout_sec + DISPATCHER_JOIN_MARGIN_SEC)`、`:710` `proc.wait(timeout=5)` → `proc.wait(timeout=KILL_WAIT_SEC)`。値は 3 つとも 5 秒のまま (挙動不変)。
- [ ] `src/agentic_fx/config.py` の `WorkerSettings` (`:455-481`) の `snapshot_max_age_sec` の直後に追記:

```python
    # watchdog と停止時 join 予算が共有する dispatch 上限 (秒)。未設定なら
    # Cd (trade 1 + reflection 最大 3 の worker 上限 + 余白) + 60 を自動導出する。
    # 明示する場合は Cd 未満だと起動拒否 (正常な dispatch の途中で watchdog が
    # サービスを止め得るため)。
    dispatch_ceiling_sec: float | None = Field(default=None, gt=0)
```

- [ ] 同 `DatafeedSettings._one_decision_timeframe` (`:193-203`) の直後に追記:

```python
    @model_validator(mode="after")
    def _decision_timeframe_is_stored(self) -> "DatafeedSettings":
        # 4h 以上は registry が 1h 保存へ正規化する派生専用の足で、判断足の
        # 確定 watermark が永久に空になり cron が発火しない。換算はしない。
        from agentic_fx.datafeed.sources import INTERVAL_MIN
        timeframe = self.decision_timeframes[0]
        if INTERVAL_MIN.get(timeframe, 0) >= 240:
            raise ValueError(
                f"decision_timeframes=[{timeframe}] は派生専用の足です "
                "(保存は 1h に正規化されるため判断足の cron が発火しません)。"
                "判断足には [1h] か [15m] を指定してください")
        return self
```

- [ ] `config/settings.yaml.example` の `worker:` 節、`snapshot_max_age_sec` 行 (`:155`) の直後に 1 行追記 (キーは書かずコメントだけ):

```yaml
  # dispatch_ceiling_sec:         # 未設定 = 自動導出 (worker 1 回の上限 Cw×4+10 に 60 を足す。既定 local 1690 / claude 1730)。明示するなら Cd 未満は起動拒否
```

- [ ] `src/agentic_fx/service.py`: import 節に `from agentic_fx.core import mission_ceiling` を足す。`_warn_strategy_timeframe_mismatches` (`:627-637`) の直後に新設:

```python
def _check_mission_ceilings(settings, activity) -> None:
    """判断足幅と mission 上限を突き合わせる。

    worker 1 回の上限 Cw が判断足幅以上なら 1 確定足に 1 回の判断が構造的に
    成立しないので起動を拒否する。dispatch 全体の上限 Cd が足幅以上でも
    起動は妨げず WARNING を 1 回書く (実際の見送り・合流は稼働中の
    cron_mission_deferred / cron_mission_coalesced で観測する)。
    """
    timeframe = settings.datafeed.decision_timeframe
    width_sec = settings.datafeed.decision_timeframe_width.total_seconds()
    cw = mission_ceiling.worker_ceiling(settings)
    if cw.total >= width_sec:
        raise RuntimeError(
            f"判断足 {timeframe} ({width_sec:.0f} 秒) に対し worker 1 回の上限 "
            f"Cw {cw.total:.0f} 秒 ({cw.breakdown_text()}) が足幅以上です。"
            "1 確定足に 1 回の判断が成立しません。判断足を 15m 以上にするか "
            "llama_swap.timeout_sec を下げてください")
    cd = mission_ceiling.dispatch_ceiling_sec(settings)
    explicit = settings.worker.dispatch_ceiling_sec
    if explicit is not None and explicit < cd:
        raise RuntimeError(
            f"worker.dispatch_ceiling_sec={explicit:g} は dispatch 全体の上限 "
            f"Cd {cd:.0f} 秒未満です。正常な dispatch の途中で watchdog が"
            f"サービスを止め得るため、{cd:.0f} 以上にするか未設定 (自動導出) に"
            "してください")
    if cd >= width_sec:
        activity.write(
            Category.SYSTEM, "mission_ceiling_dispatch_warning",
            f"判断足 {timeframe} ({width_sec:.0f} 秒) に対し dispatch 全体の上限 "
            f"Cd {cd:.0f} 秒 (trade 1 + reflection 最大 "
            f"{mission_ceiling.REFLECTIONS_PER_DISPATCH}、Cw {cw.total:.0f} 秒) が"
            "足幅以上です。起動は続けます。見送り・合流は cron_mission_deferred / "
            "cron_mission_coalesced で確認できます")
```

- [ ] 同 `build_app` の `_validate_startup(settings)` (`:930`) の直後に `_check_mission_ceilings(settings, activity)` を 1 行追加。
- [ ] 同 `_default_dispatch_ceiling_sec` (`:1392-1396`) の本体を置換:

```python
def _default_dispatch_ceiling_sec(app: App) -> float:
    return mission_ceiling.watchdog_ceiling_sec(app.settings)
```

  `run_service` の `:1541` (watchdog) と `:1650` (supervisor join 予算) はこの関数の戻り値を同じローカル変数で共有しているので配線は変えない。`_watchdog_check` は引き続き `time.monotonic() - busy_since` で比べる (supervisor の `clock_fn` 既定も `time.monotonic`、Ta2)。

### Step 1-c: green

- [ ] Step 1-a の 4 対象 + `test_dispatcher_join_dominates_ceiling_when_rpc_hangs` (最初から green の characterization test) + `uv run pytest -q tests/test_config.py tests/test_stop_sequence.py tests/runners` が pass。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): 対象 4 件で `15 passed`。`tests/test_config.py tests/test_stop_sequence.py tests/runners` + service_app の 2 本で `476 passed`** (この実測は本改訂で追加した 2 本を含まない — `test_startup_rejects_when_only_total_ceiling_exceeds_width` は「対象 4 件」の一つ `tests/test_mission_ceiling_startup.py` に入るので再実行時は `16 passed`、`test_dispatcher_join_dominates_ceiling_when_rpc_hangs` は `tests/runners` に入るので `477 passed` になる)。`[1d]` のケースも green になる — 新設の `_decision_timeframe_is_stored` が既存 `_check_intervals` より**前**に定義されるため pydantic の after-validator が定義順に走り、先に「派生専用」で落ちる (この定義順への依存はテスト側で `intervals` に判断足を足して吸収したので壊れない、上記 Minor 参照)。** `tests/test_service_app.py::test_watchdog_ceiling_and_join_budget_come_from_the_same_value` と `test_run_service_records_shutdown_timeout_when_commit_core_is_stuck` (`_default_dispatch_ceiling_sec` を patch) は**無改変で** green のこと (設計書 §10 はこの 2 本の書換えを挙げているが、式を pin していないので書換え不要。spec との食い違い #3)。`tests/test_service_app.py:1409-1441` は Step 1-a で書換え済みのため新式 (518.0) で green になっていること。

### Step 1-d: 逆変異

| # | 対象 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| Ta1-M1 | `mission_ceiling.py` | `kill_wait_retry=KILL_WAIT_SEC,` | `kill_wait_retry=0.0,` | `test_worker_ceiling_breakdown_matches_example_defaults` / `test_dispatch_and_watchdog_ceiling_derive_from_worker_ceiling` / `test_dispatch_ceiling_formula` |
| Ta1-M2 | 同 | `startup=float(w.worker_startup_timeout_sec),` | `startup=0.0,` | 同上 + `test_startup_rejects_decision_timeframe_not_longer_than_worker_ceiling` (文面の Cw 値) |
| Ta1-M3 | 同 | `if settings.runner.trade.backend == "claude" else 0.0)` | `if False else 0.0)` | `test_worker_ceiling_adds_cli_terminate_grace_only_for_claude` / claude 側 parametrize |
| Ta1-M4 | `service.py` `_check_mission_ceilings` | `if cw.total >= width_sec:` | `if cw.deadline >= width_sec:` (`deadline_budget` 330 だけで比べる) | `test_startup_rejects_when_only_total_ceiling_exceeds_width` (5m・`llama_swap.timeout_sec=250` で `cw.deadline=280<300` だが `cw.total=355>=300` — **実測で判明: 既定値の 5m parametrize (`test_startup_rejects_decision_timeframe_not_longer_than_worker_ceiling`) だけでは `cw.deadline=330` も 300 以上なのでこの変異を殺せない**。上記の追加ケースで殺す) |
| Ta1-M5 | 同 | `if cd >= width_sec:` + `activity.write(...)` | `if cd >= width_sec: raise RuntimeError("Cd")` | `test_startup_allows_15m_and_writes_one_dispatch_warning` |
| Ta1-M6 | `service.py` `_default_dispatch_ceiling_sec` | 新本体 | 旧式 `(timeout_sec + worker_grace_sec + worker_terminate_grace_sec) * 4 + 60.0` | `test_dispatch_ceiling_formula` |
| Ta1-M7 | `service.py` build_app | `_check_mission_ceilings(settings, activity)` | (行削除) | `test_build_app_runs_mission_ceiling_check` |
| Ta1-M8 | `config.py` | `if INTERVAL_MIN.get(timeframe, 0) >= 240:` | `if INTERVAL_MIN.get(timeframe, 0) > 1440:` | `test_derived_only_decision_timeframe_is_rejected[4h]` と `[1d]` |
| Ta1-M9 | 同 | `>= 240` | `>= 60` (1h も拒否) | 既存 `test_decision_timeframe_contract[None-1h-None]` |
| Ta1-M10 | `mission_ceiling.py` | `REFLECTIONS_PER_DISPATCH = 3` | `REFLECTIONS_PER_DISPATCH = 2` | `test_reflection_batch_size_matches_ceiling_constant` / Cd 系 |
| Ta1-M11 | `mission_ceiling.py` | `if explicit is not None:` | `if False:` | `test_watchdog_ceiling_prefers_explicit_setting` / `test_dispatch_ceiling_formula` (3 本目) |

**実測 (2026-09-26、worktree `tmp/wt/b2-verify`)**: M1 殺す (10 本 red) / **M4 は追加した `test_startup_rejects_when_only_total_ceiling_exceeds_width` で殺す (既定値の 5m parametrize だけでは生存する)** / M8 殺す (`[4h]`・`[1d]` 両方 red)。M2・M3・M5〜M7・M9〜M11 は表のとおり実施予定 (未個別実測、実装時に確認)。

- [ ] commit: `feat(decision-timeframe): mission 上限 Cw/Cd の起動時検査と watchdog 上限の統一、派生専用足の判断足を拒否`

---

## Ta2: `SubmitResult` と busy 見送り・合流の可観測性

**担当**: 設計書 §3.3 (構造化 SubmitResult・過負荷の可観測性)、IV-1・IV-8・IV-12、AC-B2-01・02、AC-B2-03 の supervisor 解放時刻。

**設計の具体化 (設計書に無い細部、申告済み)**:
1. `SubmitResult` は設計書の 4 項目 (`accepted`/`reason`/`busy_since`/`future`) に `checked_at` (拒否を判定した瞬間の `clock_fn()`、同じ lock 内で読む) を足す。経過秒数 `busy_elapsed_sec = checked_at - busy_since` を supervisor と同じ時計で出すため (scheduler の `now` は wall clock、`busy_since` は monotonic で直接引けない)。spec との食い違い #5。
2. `_run` は **future を解決する前に** `self._lock` 下で `_phase="idle"`・`busy_since=None` に戻す。現物 (`supervisor.py:131-141`) は `future.set_result` の後に `finally` で `_busy` を戻すため、`future.result()` が返った直後の `try_submit` が拒否され得る (既存 `test_supervisor.py:202-204` は偶然通っている)。
3. 見送り・合流を pair 単位で書くため、`_trade_mission_due` が「前進した pair だけ」の `_due_cron_watermarks` を持つ。段 b の provenance (Tb3) も同じ属性を使う。
4. activity の時刻表記は `YYYY-MM-DDTHH:MM` (UTC)。設計書の例 (`bar=12:30`) は日付を省いているが、閉場をまたぐ行と区別するため日付を付ける。

### Step 2-a: テストを置いて red を確認する

- [ ] `tests/core/test_supervisor.py` の import を `from agentic_fx.core.supervisor import MissionSupervisor, SubmitResult` にし、既存テストを次の規則で書き換える (assert の意味は不変、観測点を `SubmitResult` に移すだけ):

| 行 | 置換前 | 置換後 |
|---|---|---|
| 27-28 | `future = sup.try_submit(...)` / `assert future is not None` | `submitted = sup.try_submit(...)` / `assert submitted.accepted is True` / `future = submitted.future` |
| 40-41 | `f1 = sup.try_submit(...)` / `assert f1 is not None` | `r1 = sup.try_submit(...)` / `assert r1.accepted is True` / `f1 = r1.future` |
| 44-45 | `f2 = ...` / `assert f2 is None` | `r2 = ...` / `assert r2.accepted is False and r2.reason == "running"` |
| 55, 57-58 | `f1 = ...` / `f2 = ...` / `assert f2 is not None` | `f1 = sup.try_submit(...).future` / `r2 = ...` / `assert r2.accepted is True` / `f2 = r2.future` |
| 76, 119, 196 | `x = sup.try_submit(...)` | `x = sup.try_submit(...).future` |
| 172 | `sup.try_submit("trade", trigger="cron")` (戻り値を代入せず呼び出すだけ) | **無改変** (戻り値を使っていないので `SubmitResult` 化の影響を受けない、実測で判明。誤って `.future` を付けると存在しない変数への代入になる) |
| 102-103 | 40-41 と同じ | 同上 |
| 107-108 | 44-45 と同じ | 同上 (`reason == "running"`) |
| 130 | `f1 = sup.try_submit(...)` | `f1 = sup.try_submit(...).future` |
| 132-133 | `f2 = ...` / `assert f2 is None` | `r2 = ...` / `assert r2.accepted is False and r2.reason == "running"` |
| 156-157 | `result = ...` / `assert result is None, ...` | `result = ...` / `assert result.accepted is False and result.reason == "shutdown", ...` |
| 23, 33, 51, 84, 148, 155 | テスト名・docstring | 148/155 の「None を返す」を「reason="shutdown" で拒否する」に直す。名前は不変 |

- [ ] 同ファイル末尾に追記:

```python
class _FakeMonotonic:
    def __init__(self, t: float) -> None:
        self._t = t
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t


def test_try_submit_reports_running_until_fake_worker_releases_slot():
    """fake worker は注入クロックが dispatch 開始から Cw (405 秒) 経つまで
    戻らない。その間 try_submit は running + busy_since で拒否し、解放後に
    受理する。実時間は待たない。"""
    clock = _FakeMonotonic(1000.0)
    cw = 405.0
    started = threading.Event()

    def trade_fn(trigger):
        started.set()
        deadline = time.monotonic() + 5.0
        while clock() < 1000.0 + cw:
            assert time.monotonic() < deadline, "fake clock が進まなかった"
            time.sleep(0.001)
        return {"trigger": trigger}

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=clock)
    sup.start()
    first = sup.try_submit("trade", trigger="cron")
    assert first.accepted is True and first.reason is None
    assert first.busy_since is None and first.future is not None
    assert started.wait(5.0)
    for t in (1000.0, 1200.0, 1404.9):
        clock.set(t)
        r = sup.try_submit("trade", trigger="cron")
        assert (r.accepted, r.reason, r.busy_since, r.future) == (
            False, "running", 1000.0, None)
        assert r.busy_elapsed_sec == pytest.approx(t - 1000.0)
    clock.set(1000.0 + cw)
    first.future.result(timeout=5.0)
    again = sup.try_submit("trade", trigger="cron")
    assert again.accepted is True
    again.future.result(timeout=5.0)
    sup.shutdown(drain_exc=RuntimeError("test"))


def test_try_submit_reports_queued_before_dispatch_starts():
    """受理済みだが dispatch 前 (supervisor スレッド未起動) は queued で拒否し、
    busy_since は None (dispatch していないので経過時間は無い)。"""
    sup = MissionSupervisor(trade_fn=lambda t: t, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=lambda: 50.0)
    assert sup.try_submit("trade", trigger="cron").accepted is True
    r = sup.try_submit("trade", trigger="cron")
    assert (r.accepted, r.reason, r.busy_since, r.checked_at) == (
        False, "queued", None, 50.0)
    assert r.busy_elapsed_sec is None
    sup.fail_pending(exc=RuntimeError("drain"))
    assert sup.busy_since is None
    assert sup.try_submit("trade", trigger="cron").accepted is True


def test_slot_is_idle_before_future_callbacks_run():
    """future の完了 callback (supervisor スレッドで同期実行) の時点で
    スロットは既に空いている — result() が返ったら次を必ず受理できる。"""
    seen = []
    registered = threading.Event()

    def trade_fn(trigger):
        # callback の登録より先に完了させない (登録後に完了させると
        # callback は必ず supervisor スレッドの future 解決の中で走る)
        assert registered.wait(5.0)
        return trigger

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q)
    sup.start()
    first = sup.try_submit("trade", trigger="cron")
    done = threading.Event()

    def on_done(_future):
        r = sup.try_submit("ask", question="next")
        seen.append((r.accepted, r.reason, sup.busy_since))
        done.set()

    first.future.add_done_callback(on_done)
    registered.set()
    assert done.wait(5.0)
    assert seen == [(True, None, None)]
    sup.shutdown(drain_exc=RuntimeError("test"))


def test_shutdown_rejects_with_reason_shutdown_even_when_idle():
    sup = MissionSupervisor(trade_fn=lambda t: t, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=lambda: 7.0)
    sup.shutdown(drain_exc=RuntimeError("stop"))
    r = sup.try_submit("trade", trigger="cron")
    assert (r.accepted, r.reason, r.busy_since, r.future) == (
        False, "shutdown", None, None)
```

  既存 `test_busy_since_is_set_during_dispatch_and_cleared_after` (`:184-204`) は無改変 (観測点は `busy_since` のまま、`:204` は解決順の是正で決定的に成立する)。

- [ ] `tests/core/test_scheduler.py` を書き換える: import に `from agentic_fx.core.supervisor import SubmitResult` を足し、`Env._trade` (`:107-110`) を

```python
    def _trade(self, reason, **_kwargs):
        self.trade_calls += 1
        self.trade_reasons.append(reason)
        return SubmitResult.accepted_with(None)
```

  に、`:189` を `env._trade = lambda reason, **_: SubmitResult.rejected("running")`、`:196` を `env.sched.on_trade_mission = lambda reason, **_: env.trade_reasons.append(reason) or SubmitResult.accepted_with(None)`、`:2941` を `def boom(reason, **_kwargs):`、`:2968` を `env.sched.on_trade_mission = lambda reason, **_: SubmitResult.rejected("running")  # busy を模す` にする。`:2935-2938`・`:2964-2966` の docstring の「bool / True / False」は「SubmitResult.accepted」に直す (assert は不変)。

- [ ] `tests/core/test_scheduler_decision_timeframe.py` を新規作成 (段 b でも追記するので helper を先に置く):

```python
"""判断足 cron の見送り・合流・資金保護順序 (B-2)。"""
from datetime import datetime, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.core.supervisor import SubmitResult
from agentic_fx.store import orders
from tests.core.test_scheduler import SETTINGS, WED, Env, _seed_decision_bar

THU = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)   # 木曜


def _settings(interval="15m", pairs=("USDJPY",)):
    datafeed = SETTINGS.datafeed.model_copy(update={
        "decision_timeframes": [interval], "primary_intervals": [interval]})
    return SETTINGS.model_copy(update={"datafeed": datafeed, "pairs": list(pairs)})


def _at(base, hour, minute, second=0, *, day=None):
    return base.replace(day=day or base.day, hour=hour, minute=minute,
                        second=second)


def _seed_bar(env, bar_time, *, interval="15m", pair="USDJPY"):
    env.conn.execute(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?)",
        (pair, interval, bar_time.isoformat(), 148.0, 148.1, 147.9, 148.0,
         1.0, "yfinance"))
    env.conn.commit()


class _SlotFake:
    """supervisor の単一スロットを tick 時刻で模す (実時間は使わない)。
    busy_until より前の呼び出しは running で拒否する。"""

    def __init__(self):
        self.now = None
        self.busy_from = None
        self.busy_until = None
        self.accepted_at = []
        self.accepted_kwargs = []

    def __call__(self, reason, **kwargs):
        if self.busy_until is not None and self.now < self.busy_until:
            return SubmitResult.rejected(
                "running", busy_since=self.busy_from.timestamp(),
                checked_at=self.now.timestamp())
        self.busy_from = self.now
        self.accepted_at.append(self.now)
        self.accepted_kwargs.append(dict(kwargs))
        return SubmitResult.accepted_with(None)


def _env(tmp_path, base, *, interval="15m", pairs=("USDJPY",)):
    env = Env(tmp_path, base=base, seed_cron_bar=False)
    env.sched.settings = _settings(interval, pairs)
    slot = _SlotFake()
    env.sched.on_trade_mission = slot
    return env, slot


def _tick(env, slot, now):
    slot.now = now
    return env.sched.tick(now)


def _activity(env, event):
    path = env.tmp_path / "a.log"
    if not path.exists():
        return []
    rows = [line.split("\t") for line in
            path.read_text(encoding="utf-8").splitlines()]
    return [row[3] for row in rows if row[2] == event]


def test_busy_defers_once_with_elapsed_and_accepts_latest_closed_bar(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    assert slot.accepted_at == [_at(THU, 12, 30, 31)]
    slot.busy_until = _at(THU, 12, 47)
    # 12:30 足の確定は bar_time + 足幅 15m + grace 30 秒 = 12:45:30
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 30))
    _tick(env, slot, _at(THU, 12, 46, 30))
    assert _activity(env, "cron_mission_deferred") == [
        "USDJPY 15m bar=2026-09-24T12:30 busy (running 15.0 min)"]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 15)
    # 12:45 足 (確定 13:00:30) が DB に先に在っても 12:47:30 では採らない
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 12, 47, 30))
    assert slot.accepted_at == [_at(THU, 12, 30, 31), _at(THU, 12, 47, 30)]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 30)
    assert len(_activity(env, "cron_mission_deferred")) == 1


def test_busy_across_two_bars_coalesces_to_latest_counting_db_bars(tmp_path):
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    slot.busy_until = _at(THU, 13, 2)
    _seed_bar(env, _at(THU, 12, 30))
    _tick(env, slot, _at(THU, 12, 45, 30))
    _seed_bar(env, _at(THU, 12, 45))
    _tick(env, slot, _at(THU, 13, 0, 30))
    _tick(env, slot, _at(THU, 13, 2, 30))
    assert slot.accepted_at == [_at(THU, 12, 30, 31), _at(THU, 13, 2, 30)]
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)
    assert _activity(env, "cron_mission_coalesced") == [
        "USDJPY 15m accepted=2026-09-24T12:45 skipped=1 (2026-09-24T12:30)"]
    assert [s.split(" busy")[0] for s in _activity(env, "cron_mission_deferred")] == [
        "USDJPY 15m bar=2026-09-24T12:30", "USDJPY 15m bar=2026-09-24T12:45"]


def test_coalesced_is_not_written_when_intermediate_bar_is_missing(tmp_path):
    """足幅からの格子計算ではなく DB に実在する閉じた足だけを数える。"""
    env, slot = _env(tmp_path, THU)
    _seed_bar(env, _at(THU, 12, 15))
    _tick(env, slot, _at(THU, 12, 30, 31))
    slot.busy_until = _at(THU, 13, 2)
    _seed_bar(env, _at(THU, 12, 45))           # 12:30 足は欠けている
    _tick(env, slot, _at(THU, 13, 0, 30))
    _tick(env, slot, _at(THU, 13, 2, 30))
    assert slot.accepted_at[-1] == _at(THU, 13, 2, 30)
    assert env.sched._cron_watermarks[("USDJPY", "15m")] == _at(THU, 12, 45)
    assert _activity(env, "cron_mission_coalesced") == []
```

  (`orders`・`Bar`・`WED`・`_seed_decision_bar`・`pytest` の import は Ta3 の追記で使う)

- [ ] red を確認する: `uv run pytest -q tests/core/test_supervisor.py tests/core/test_scheduler_decision_timeframe.py tests/core/test_scheduler.py`
      期待: `ImportError: cannot import name 'SubmitResult' from 'agentic_fx.core.supervisor'` の collection error (3 ファイル)。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): 期待どおり 3 ファイルとも collection error。** import だけ先に通した状態 (空の `SubmitResult` stub を一時に置く) での個別 red は取らない — collection error で足りる。

### Step 2-b: 実装を転写する

- [ ] `src/agentic_fx/core/supervisor.py`: import に `from dataclasses import dataclass` と `from typing import Callable, Literal` を足し、`class MissionSupervisor` の前に追加:

```python
@dataclass(frozen=True)
class SubmitResult:
    """`try_submit` の結果。accepted / reason / busy_since / checked_at は
    `MissionSupervisor._lock` の中で 1 回に読んだ値の組。

    reason: "queued" = 受理済みで dispatch 前 / "running" = dispatch 中
    (busy_since あり) / "shutdown" = 停止中で新規を受けない。
    """
    accepted: bool
    reason: Literal["queued", "running", "shutdown"] | None
    busy_since: float | None
    checked_at: float | None
    future: Future | None

    @property
    def busy_elapsed_sec(self) -> float | None:
        if self.busy_since is None or self.checked_at is None:
            return None
        return self.checked_at - self.busy_since

    @classmethod
    def accepted_with(cls, future: Future | None) -> "SubmitResult":
        return cls(True, None, None, None, future)

    @classmethod
    def rejected(cls, reason: Literal["queued", "running", "shutdown"], *,
                 busy_since: float | None = None,
                 checked_at: float | None = None) -> "SubmitResult":
        return cls(False, reason, busy_since, checked_at, None)
```

- [ ] `MissionSupervisor.__init__` に引数 `clock_fn: Callable[[], float] = time.monotonic` (最後のキーワード) を足し、本体の `self._busy = False` を削除して次を足す:

```python
        self._clock_fn = clock_fn
        # "idle" = 空き / "queued" = 受理済み・dispatch 前 / "running" =
        # dispatch 中。遷移は self._lock の中だけで行い、busy_since と組で
        # 読み書きする (running なのに busy_since が None の区間を作らない)。
        self._phase: Literal["idle", "queued", "running"] = "idle"
```

- [ ] `try_submit` (`:61-71`) を置換:

```python
    def try_submit(self, kind: str, **kwargs) -> SubmitResult:
        with self._lock:
            # C2 fix: shutdown 後は新規受付を停止する
            if self._stop_event.is_set():
                return SubmitResult.rejected("shutdown")
            if self._phase != "idle":
                return SubmitResult.rejected(
                    self._phase, busy_since=self.busy_since,
                    checked_at=self._clock_fn())
            self._phase = "queued"
            future: Future = Future()
            self._queue.put((kind, kwargs, future))
            return SubmitResult.accepted_with(future)
```

- [ ] `fail_pending` 内の古いコメント `# m2 fix: None センチネルの場合でも _busy を必ず解放する` (`_busy` は本 task で `_phase` に置き換わり存在しなくなる) を `# None センチネルの場合でも _phase/busy_since を必ず解放する` に直す (実測で放置されていたことが判明、2026-09-26 着手前検証)。
- [ ] `fail_pending` の `finally` (`:93-95`) を置換:

```python
        finally:
            with self._lock:
                self._phase = "idle"
                self.busy_since = None
```

- [ ] `_run` の `self.busy_since = time.monotonic()` (`:128`) から `pump.join(timeout=1.0)` (`:142`) までを置換:

```python
            with self._lock:
                self._phase = "running"
                self.busy_since = self._clock_fn()
            result = None
            error: Exception | None = None
            try:
                result = self._dispatch(kind, kwargs)
            except Exception as e:  # noqa: BLE001 — supervisor スレッドを殺さない
                _log.exception("mission job %r failed", kind)
                error = e
            finally:
                pump_stop.set()
                # future を解決する前にスロットを空ける: result() が返った
                # 時点 (および完了 callback の中) で次の try_submit が必ず
                # 受理される。
                with self._lock:
                    self._phase = "idle"
                    self.busy_since = None
            if not future.cancelled():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(result)
            pump.join(timeout=1.0)
```

- [ ] `src/agentic_fx/core/scheduler.py`:
  - import: `from datetime import datetime, timedelta, timezone` / `from agentic_fx.core.supervisor import SubmitResult`
  - `:52` の型を `on_trade_mission: Callable[..., SubmitResult],` に。
  - `__init__` の `self._warned_regressed_cron_watermarks` (`:107`) の直後に:

```python
        # その tick に「前進した」(W > L) pair だけの確定足。見送り・合流の
        # 記録と (受理時の) provenance に使う。
        self._due_cron_watermarks: dict[tuple[str, str], datetime] = {}
        # cron_mission_deferred を書いた (pair, interval, bar_time)。
        # 同じ watermark の見送りは 1 回だけ書く。受理で空に戻す。
        self._cron_deferred_logged: set[tuple[str, str, datetime]] = set()
```

  - 受理ブロック (`:256-260`) を置換:

```python
        reason = None if self._stopping() else self._trade_mission_due(now)
        if reason is not None:
            result = self.on_trade_mission(reason)
            if reason == "cron":
                if result.accepted:
                    previous = dict(self._cron_watermarks)
                    self._advance_cron_watermarks(self._pending_cron_watermarks)
                    self._cron_deferred_logged.clear()
                    self._record_cron_coalesced(previous)
                else:
                    self._record_cron_deferred(result)
        return pending
```

  - `_trade_mission_due` の `:333-347` を置換 (`signal_due_fn` 以降は不変):

```python
        self._due_cron_watermarks = {}
        if self.state_fn() != "ready":
            return None
        latest = self._latest_cron_watermarks(now)
        self._pending_cron_watermarks = latest
        due: dict[tuple[str, str], datetime] = {}
        for key, watermark in latest.items():
            cursor = self._cron_watermarks.get(key)
            if cursor is None or watermark > cursor:
                due[key] = watermark
            elif watermark < cursor and key not in self._warned_regressed_cron_watermarks:
                self._warned_regressed_cron_watermarks.add(key)
                _log.warning("cron watermark regressed for %s %s: %s < %s",
                             key[0], key[1], watermark.isoformat(), cursor.isoformat())
        self._due_cron_watermarks = due
        if due:
            return "cron"
```

  - `_latest_cron_watermarks` の source 導出 (`:364-365`) を `source = self._storage_source()` にし、次の 4 つを `_advance_cron_watermarks` の直後に追加:

```python
    def _storage_source(self) -> str:
        # primary から導出した storage source。別 source の残存行を cron の
        # 根拠にしない。
        return ("mt5-live" if self.settings.datafeed.primary == "mt5"
                else self.settings.datafeed.primary)

    def _record_cron_deferred(self, result: SubmitResult) -> None:
        for (pair, interval), bar_time in sorted(self._due_cron_watermarks.items()):
            marker = (pair, interval, bar_time)
            if marker in self._cron_deferred_logged:
                continue
            self._cron_deferred_logged.add(marker)
            self.activity.write(
                Category.SYSTEM, "cron_mission_deferred",
                f"{pair} {interval} bar={_fmt_bar(bar_time)} "
                f"busy ({_busy_text(result)})")

    def _record_cron_coalesced(self, previous: dict) -> None:
        source = self._storage_source()
        for (pair, interval), bar_time in sorted(self._due_cron_watermarks.items()):
            prev = previous.get((pair, interval))
            if prev is None:
                continue
            try:
                rows = ohlcv.load_cache_bars(self.conn, pair, interval,
                                             source=source, since=prev,
                                             until=bar_time)
            except Exception as e:  # noqa: BLE001 — 観測の失敗で受理済みの判断を巻き戻さない
                _log.warning("coalesced count failed for %s %s: %s",
                             pair, interval, safe_error_text(e))
                continue
            skipped = [row.ts for row in rows if prev < row.ts < bar_time]
            if skipped:
                self.activity.write(
                    Category.SYSTEM, "cron_mission_coalesced",
                    f"{pair} {interval} accepted={_fmt_bar(bar_time)} "
                    f"skipped={len(skipped)} "
                    f"({', '.join(_fmt_bar(t) for t in skipped)})")
```

  - モジュール末尾 (`period_key_of` の後) に:

```python
def _fmt_bar(bar_time: datetime) -> str:
    return bar_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _busy_text(result: SubmitResult) -> str:
    if result.reason == "running" and result.busy_elapsed_sec is not None:
        return f"running {result.busy_elapsed_sec / 60:.1f} min"
    return str(result.reason)
```

### Step 2-c: green

- [ ] `uv run pytest -q tests/core/test_supervisor.py tests/core/test_scheduler_decision_timeframe.py tests/core/test_scheduler.py tests/core/test_scheduler_signal.py tests/core/test_scheduler_tick_order.py tests/core/test_scheduler_cache_maintenance.py tests/core/test_scheduler_improve_occurrence.py` が pass。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): 指定 7 ファイルで `161 passed`。** Ta3 前なので `tests/test_service_app.py` の try_submit 系は red のまま (Ta3 で直す)。

### Step 2-d: 逆変異

| # | 対象 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| Ta2-M1 | `supervisor.py` `_run` | idle 化 → future 解決 の順 | future 解決 (`set_result`/`set_exception`) を `finally` の**前** (try 内) に**移す** — **`finally` 末尾の future 解決ブロックは削除する** (削除し忘れて両方に解決コードが残ると `set_result` が 2 回呼ばれ `InvalidStateError` になり、supervisor スレッドが死んで別の壊れ方 (3 本 red) になる。実測で判明、2026-09-26 着手前検証 — 「解決順序だけを戻す」変異に限る) | `test_slot_is_idle_before_future_callbacks_run` (callback 内の `try_submit` が running で拒否) |
| Ta2-M2 | 同 | `self.busy_since = self._clock_fn()` | `self.busy_since = time.monotonic()` | `test_try_submit_reports_running_until_fake_worker_releases_slot` (`busy_since == 1000.0` が偽) |
| Ta2-M3 | 同 `try_submit` | `self._phase, busy_since=self.busy_since,` | `self._phase, busy_since=None,` | 同上 |
| Ta2-M4 | 同 `try_submit` | `self._phase = "queued"` | (行削除: running になるまで idle のまま) | `test_try_submit_reports_queued_before_dispatch_starts` |
| Ta2-M5 | 同 `fail_pending` の `finally` | `self._phase = "idle"` | (行削除: drain 後も queued のまま) | `test_try_submit_reports_queued_before_dispatch_starts` (drain 後の受理) |
| Ta2-M6 | 同 `try_submit` | `return SubmitResult.rejected("shutdown")` | `return SubmitResult.rejected("running")` | `test_shutdown_rejects_with_reason_shutdown_even_when_idle` / 既存 `test_shutdown_rejects_new_submissions` |
| Ta2-M7 | `scheduler.py` `_record_cron_deferred` | `if marker in self._cron_deferred_logged: continue` | (2 行削除: 毎 tick 書く) | `test_busy_defers_once_with_elapsed_and_accepts_latest_closed_bar` |
| Ta2-M8 | 同 | `f"busy ({_busy_text(result)})"` | `"busy"` | 同上 |
| Ta2-M9 | 同 受理ブロック | `if result.accepted:` | `if True:` (受理前に cursor を進める) | 同上 (`_cron_watermarks == 12:15` が偽) |
| Ta2-M10 | 同 `_record_cron_coalesced` | `skipped = [row.ts for row in rows if prev < row.ts < bar_time]` | `skipped = [prev + timedelta(minutes=15) * i for i in range(1, int((bar_time - prev) / timedelta(minutes=15)))]` (足幅からの格子計算) | `test_coalesced_is_not_written_when_intermediate_bar_is_missing` |
| Ta2-M11 | 同 | `if skipped:` | `if True:` | `test_coalesced_is_not_written_when_intermediate_bar_is_missing` |
| Ta2-M12 | `store/ohlcv.py:187` (既存の確定判定) | `cutoff = ... - width - grace` | `cutoff = ... - grace` (次の足の開始を足し忘れる) | `test_busy_defers_once_with_elapsed_and_accepts_latest_closed_bar` (12:47:30 に 12:45 足を採る) |

**実測 (2026-09-26、worktree `tmp/wt/b2-verify`)**: M1 は表の変異どおりに作ると `set_result` の二重呼び出しで supervisor スレッドが死に別の壊れ方 (3 本 red) になったため、「解決順序だけを戻す」変異 (finally 後の解決ブロックは削除) に絞ったところ、意図どおり `test_slot_is_idle_before_future_callbacks_run` だけが red。M7 (dedup 削除) は `test_busy_defers_once_with_elapsed_and_accepts_latest_closed_bar` が red。M10 (格子計算) は `test_coalesced_is_not_written_when_intermediate_bar_is_missing` が red。M2〜M6・M8・M9・M11・M12 は表のとおり実施予定 (未個別実測)。

- [ ] commit: `feat(decision-timeframe): try_submit を SubmitResult (queued/running/shutdown + busy_since) にし、cron の見送り・合流を activity に 1 回ずつ記録`

---

## Ta3: service / backtest の配線、全呼び出し元の書換え、資金保護順序の回帰 (AC-B2-24 (a))

**担当**: 設計書 §3.3 (ask 経路・cron 経路)、§10 の呼び出し元書換え、AC-B2-24 (a)。

### Step 3-a: テストを置いて red を確認する

- [ ] `tests/core/test_scheduler_decision_timeframe.py` に追記:

```python
def test_cursor_advance_exception_leaves_day_close_and_sl_already_done(
        tmp_path, monkeypatch):
    """cron 受理後の cursor 前進で例外が出ても、同じ tick の資金保護
    (day 強制決済・OPEN の SL 監視) は例外より前に 1 回実行済み。"""
    env = Env(tmp_path, seed_cron_bar=False)
    day_oid = env.place_limit()
    swing_oid = orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
    env.sched.tick(WED.replace(minute=1))
    assert orders.get(env.conn, day_oid)["status"] == "open"
    assert orders.get(env.conn, swing_oid)["status"] == "open"

    near_close = WED.replace(hour=20, minute=57)
    _seed_decision_bar(env, WED.replace(hour=19))
    env.bars["USDJPY"] = Bar("USDJPY", "1m", near_close, 147.70, 147.75,
                             147.50, 147.55, 100)

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected cursor advance failure")

    monkeypatch.setattr(env.sched, "_advance_cron_watermarks", boom)
    with pytest.raises(RuntimeError, match="injected cursor advance failure"):
        env.sched.tick(near_close)
    assert env.trade_reasons == ["cron"]
    day = orders.get(env.conn, day_oid)
    assert (day["status"], day["close_reason"]) == ("closed", "day_rollover")
    swing = orders.get(env.conn, swing_oid)
    assert (swing["status"], swing["close_reason"]) == ("closed", "sl")
```

- [ ] `tests/test_service_app.py` に追記 (`test_on_trade_mission_runs_loop_and_reflection` の直前):

```python
def test_service_tick_records_deferred_with_supervisor_phase(tmp_path):
    """本物の MissionSupervisor (スレッド未起動 = 受理後 queued のまま) で、
    次の確定足の cron が queued 理由で見送られ activity に 1 行出る。"""
    from agentic_fx.core.accounting import record_snapshot

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                        equity=1_000_000)
        _seed_decision_bar(app.conn_core)
        with _no_real_network():
            with app.core_lock:
                app.scheduler.tick(NOW)
            _seed_decision_bar(app.conn_core, NOW - timedelta(hours=1))
            with app.core_lock:
                app.scheduler.tick(NOW + timedelta(minutes=1))
        lines = [line.split("\t") for line in
                 (tmp_path / "logs" / "activity.log").read_text(
                     encoding="utf-8").splitlines()]
        deferred = [row[3] for row in lines if row[2] == "cron_mission_deferred"]
        assert deferred == ["USDJPY 1h bar=2026-07-22T11:00 busy (queued)"]
    finally:
        app.supervisor.shutdown(drain_exc=RuntimeError("test shutdown"))
        app.close()


def test_supervisor_ask_returns_busy_message_on_rejection():
    from concurrent.futures import Future

    from agentic_fx.core.supervisor import SubmitResult
    from agentic_fx.service import _SupervisorAsk

    class _Sup:
        def __init__(self, result):
            self.result = result

        def try_submit(self, kind, **kwargs):
            return self.result

    busy = _SupervisorAsk(_Sup(SubmitResult.rejected("running")), 1.0)
    assert "Mission 実行中" in busy.ask_once("q")
    done: Future = Future()
    done.set_result("answer")
    ok = _SupervisorAsk(_Sup(SubmitResult.accepted_with(done)), 1.0)
    assert ok.ask_once("q") == "answer"
```

- [ ] 同ファイルの既存サイトを「呼び出し元の全数」表のとおり書き換える: spy 3 本 (`:456-461`・`:510-515`・`:639-644`) の本体を

```python
        def spy_try_submit(self, kind, **kw):
            r = original_try_submit(self, kind, **kw)
            if r.accepted:
                captured.append(r.future)
            return r
```

  に (3 本とも同文)、`:589` と `:1597` を `future = app.supervisor.try_submit("trade", trigger="cron").future`、`:1673-1675` を

```python
        rejected = app.supervisor.try_submit("trade", trigger="cron")
        assert rejected.accepted is False and rejected.reason == "shutdown", (
            "shutdown() が th.join() より後に実行されている — 停止処理の"
            "最中に新しい Mission が受理されてしまう")
```

  に、`:3092` と `:3993-3994` を表の置換後の形にする。import 節に `from agentic_fx.core.supervisor import SubmitResult` を足す。
- [ ] `tests/test_wiring.py:82`・`tests/backtest/test_base_interval_convergence.py:849,857,912`・`tests/backtest/test_runner.py:670`・`tests/core/test_e2e_paper_cycle.py:57` を表の置換後の形にし、各ファイルに `from agentic_fx.core.supervisor import SubmitResult` を足す。
- [ ] red を確認する: `uv run pytest -q tests/core/test_scheduler_decision_timeframe.py tests/test_service_app.py -k "deferred_with_supervisor_phase or busy_message or cursor_advance_exception"`
      期待: `test_service_tick_records_deferred_with_supervisor_phase` は `AttributeError: 'bool' object has no attribute 'accepted'` (service の `on_trade_mission` がまだ `is not None` の bool を返す)、`test_supervisor_ask_returns_busy_message_on_rejection` は `AssertionError` (`SubmitResult` に `.result` が無く、既存の最終防波堤 `except Exception` が `(Mission 失敗: ...)` を返す)。**`test_cursor_advance_exception_leaves_day_close_and_sl_already_done` は Step 3-a で追加した時点で既に green の**既存不変条件の characterization test** である** (資金保護の実行順序は Ta2 までの変更と無関係に現物で既に成立しているため、この 1 本は「実装前に red、実装後に green」という他の Step 3-a テストと違い、最初から green — Ta3 の実装で新しく red にする対象ではない。この test を殺す変異は Ta3-M1 (下記) であり、逆変異でしか red にならないことがこのテストの検証点になる)。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): 指定 7 ファイルで `1 failed, 212 passed`** (着手前検証の時点では `test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling` の書換えを Ta3 側で確認していたため観測された失敗。本改訂で書換えを Ta1 Step 1-a に前倒ししたので、この Step の時点ではもう failed は出ない)。AC-B2-24 系はすべて期待どおり。

### Step 3-b: 実装を転写する

- [ ] `src/agentic_fx/service.py`: import 節の `from agentic_fx.core.supervisor import MissionSupervisor` を `from agentic_fx.core.supervisor import MissionSupervisor, SubmitResult` に。`_SupervisorAsk.ask_once` の `:657-660` を置換:

```python
        submitted = self._supervisor.try_submit("ask", question=question)
        if not submitted.accepted:
            return ("(現在 Mission 実行中のため質問を受け付けられません。"
                    "しばらくして再試行してください)")
        future = submitted.future
```

- [ ] 同 `on_trade_mission` (`:1077-1081`) を置換:

```python
        def on_trade_mission(trigger: str) -> SubmitResult:
            # trigger は scheduler._trade_mission_due() が返した起動理由。
            # 受理/拒否と拒否理由 (queued/running/shutdown・busy_since) を
            # そのまま scheduler へ返す (cron 締切の前進と見送り記録の材料)。
            return supervisor.try_submit("trade", trigger=trigger)
```

- [ ] `src/agentic_fx/backtest/runner.py`: import 節に `from agentic_fx.core.supervisor import SubmitResult`、`:419` を

```python
        # バックテストは判断 mission を起こさない (in-memory DB の ohlcv_cache
        # は空で cron は due にならない)。呼ばれても受理しない。
        on_trade_mission=lambda reason, **_: SubmitResult.rejected("shutdown"),
```

### Step 3-c: green

- [ ] `uv run pytest -q tests/core/test_scheduler_decision_timeframe.py tests/test_service_app.py tests/test_wiring.py tests/backtest/test_base_interval_convergence.py tests/backtest/test_runner.py tests/core/test_e2e_paper_cycle.py tests/test_closed_bars_wiring.py` が pass。**実測 (2026-09-26、worktree `tmp/wt/b2-verify`): 指定 7 ファイルで `213 passed`** (`test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling` を Ta1 Step 1-a で書換え済みのため、この時点で 1 failed は出ない — 着手前検証では書換えのタイミングを Ta3 側で確認したため一時的に `1 failed, 212 passed` を観測したが、本改訂で書換えを Ta1 Step 1-a に前倒ししたのでその失敗は Step 3-c より前に解消済み)。

### Step 3-d: 逆変異

| # | 対象 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| Ta3-M1 | `scheduler.py` `tick` | 受理ブロック (`reason = ...` 〜 `_record_cron_deferred`) を `finally:` の後に置く | 同ブロックを `try:` の先頭 (`open_now = ...` の直後、`_mark_to_market` より前) に移す | `test_cursor_advance_exception_leaves_day_close_and_sl_already_done` |
| Ta3-M2 | `service.py` `on_trade_mission` | `return supervisor.try_submit(...)` | `return SubmitResult.accepted_with(None)` (supervisor を通さない) | `test_service_tick_records_deferred_with_supervisor_phase` (2 回目も受理され deferred が出ない) |
| Ta3-M3 | `service.py` `_SupervisorAsk.ask_once` | `if not submitted.accepted:` | `if False:` (拒否でも `future.result` へ進み `(Mission 失敗: ...)` を返す) | `test_supervisor_ask_returns_busy_message_on_rejection` |
| Ta3-M4 | `supervisor.py` `try_submit` | `SubmitResult.rejected(self._phase, ...)` | `SubmitResult.rejected("running", ...)` | `test_service_tick_records_deferred_with_supervisor_phase` (`busy (running)` になる) / `test_try_submit_reports_queued_before_dispatch_starts` |

**実測 (2026-09-26、worktree `tmp/wt/b2-verify`)**: M1 殺す (`test_cursor_advance_exception_...` red)。M2 殺す (`test_service_tick_records_deferred_with_supervisor_phase` red)。M3 殺す (`test_supervisor_ask_returns_busy_message_on_rejection` red)。M4 は表のとおり実施予定 (未個別実測)。

- [ ] commit: `feat(decision-timeframe): service の on_trade_mission / ask を SubmitResult で配線し、全呼び出し元の fake を新契約へ`

---

## 段 a 全体の green 確認 (Ta1〜Ta3 統合後、main へ入れる前)

- [ ] 既存 scheduler 安全系の全実行 (day close / SL・TP / drawdown kill switch / tick 順序 / 停止): `uv run pytest -q -p no:cacheprovider tests/core/test_scheduler.py tests/core/test_scheduler_tick_order.py tests/core/test_scheduler_signal.py tests/core/test_scheduler_cache_maintenance.py tests/core/test_scheduler_improve_occurrence.py tests/core/test_scheduler_decision_timeframe.py tests/core/test_supervisor.py tests/core/test_risk_gate.py tests/core/test_accounting.py tests/test_stop_sequence.py tests/backtest/test_kill_switch_replay.py` → 段 a 単独では未実測 (Ta1〜Ta3 を段 b と同じ worktree で続けて検証したため、段 b 分と合わせた `Tb3` 節の `1426 passed` 実行がこの一括実行を兼ねている。段 a だけを切り出した実行が必要なら着手時に指揮者が改めて実測する)
- [ ] フルスイート: `uv run pytest -q -p no:cacheprovider --deselect tests/test_shell_interrupt.py --deselect "tests/test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end"` (`--basetemp` は付けない。background 起動 + 結果行を待つ)
      - baseline (`43c6042`、`--deselect` を追加した完全コマンドで実測): `1 failed, 4481 passed, 17 deselected in 658.56s` (failed は既存の環境依存 `test_init_completes_offline_with_unreachable_bridge`、単体実行では pass する。B-2 差分とは無関係)
      - 段 a+b 統合後 (worktree、`-x` なしの一覧取得): `11 failed, 4536 passed, 5 deselected in 665.06s`。11 件はすべて B-2 と無関係 (init 1 本は同じ環境依存の JSONDecodeError、shell_interrupt 9 本 + interactive 1 本はプランの deselect 条件が拾いきれていない既知の stdin/stop_event 系)。deselect 条件を揃えると baseline 4482 本・段 a+b 統合後 4535 本で、差 +53 が新規テスト (parametrize 展開後) に一致する。段 a だけを切り出したフルスイート値は未実測 (段 a→b を同じ worktree で連続実施したため)。**この +53 は本改訂 (v1.1) で追加した 5 本 (Ta1 2 本・Tb2 3 本、上記 Step 1-c・2-c 参照) を含まない実測 — 再実行時の期待差分は +58。**
- [ ] `grep -rn 'is not None' src/agentic_fx/service.py | grep try_submit` が 0 件 (bool 化の残りが無い)
- [ ] コメントの工程ラベル grep (Global Constraints) が 0 件、`git status` に実 DB・`config/settings.yaml`・`plugins/` の差分が無い
- [ ] 段 a を main へ統合してから段 b ファイルに進む

## spec との食い違い (本プラン執筆中に見つけたもの)

| # | 設計書の記述 | 現物 / 本プランの扱い | 印 |
|---|---|---|---|
| 1 | §11: AC-B2-24 を Ta2/Ta3 で通す (session 判定側の注入を含む) | `session_start` は段 b (Tb1) で新設されるので段 a には注入点が無い。(a) cursor 前進側を Ta3、(b) session 判定側を Tb1 で通す | spec v1.1 で修正済み (§11) |
| 2 | §11: AC-B2-21 を Ta2/Ta3 で通す | (a) 再起動で再判断しない・(b) 前セッション足 skip は段 b、(c) signal claim は段 c の挙動。段 a で pin できるのは「1h の発火リズム・回数が B-1 と同じ」(既存テスト無改変 green) だけ。(a) は Tb2、(b) は Tb1 | spec v1.1 で修正済み (§11) |
| 3 | §10: `test_service_app.py:1487-1589` を新式に書き換える | この範囲の 2 本は `_default_dispatch_ceiling_sec` の式を pin しておらず (1 本は順序関係、1 本は patch で 0.1 に固定)、書換え不要。式を pin するのは `test_stop_sequence.py:57` **と `:1409-1441` (`test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling`、実測で判明した見落とし、上記 Ta1 Critical 参照)** | spec v1.1 で修正済み (§10、`:1409-1441` の書換え式も明記) |
| 4 | §10: `try_submit` の spy は `test_service_app.py:452-463` | 同型の spy が `:506-517`・`:635-647` にもあり計 3 本 | spec v1.1 で修正済み (§10) |
| 5 | §3.3: `SubmitResult` は accepted/reason/busy_since/future | 経過時間を supervisor と同じ時計で出すため `checked_at` を足した (`busy_elapsed_sec = checked_at - busy_since`)。scheduler の `now` (wall clock) と `busy_since` (monotonic) は直接引けない | spec v1.1 で追記済み (§3.3, IV-12, §8-25) |
| 6 | §3.5 activity 行: 「mission 開始・見送り・合流の summary に `decision_tf=15m bar=…`」 | 現物の trade_loop に mission 開始の activity event は無い (`decision`・`gate_rejected` 等だけ)。本プランは見送り・合流・前セッション skip の 3 event の summary に `<pair> <interval> bar=<UTC>` を入れ、mission 開始 event は新設しない | spec v1.1 で該当行を削除・訂正 (§3.5) |
| 7 | §3.3 `_run` の説明 (「完了後の finally で両方クリア」) | 現物は future 解決の後に `finally` が走るため、`result()` 直後の `try_submit` が拒否され得る。本プランは future 解決の**前**にクリアする (Ta2 設計の具体化 2) | spec v1.1 で追記済み (現物の潜在欠陥として §1 に事実追加、§3.3, IV-12, AC-B2-03 に逆変異) |
| 8 | §3.3 ・§7: 派生専用足の拒否を「起動時」 | 設定読込み (`DatafeedSettings` の validator) で拒否する。worker 子プロセスの設定再検証でも同じ文面で落ちる | 記述の具体化のみ |
| 9 | §3.3 Cw の内訳 (各区間を同列の「打ち切り経路」として扱う) | §9-1 実測 (`tmp/design-b2/measurements.md` #1) で、kill wait・再実行・reader.join の 15 秒は正常系で消費されない安全マージン、`dispatcher.join` (`rpc_timeout_sec`+5) は RPC 張り付きで実測でも支配的になる経路と判明。数値は不変。**裁定 (2026-09-26): 採用。§3.3 に性質差の注記を追加し、Ta1 に `test_dispatcher_join_dominates_ceiling_when_rpc_hangs` (RPC 投げっぱなし fake、実 subprocess) を追加した (上記 Step 1-a)。** | spec 追記済み・プラン反映済み |

段 b 分の食い違いは段 b ファイル末尾に続けて列挙する。

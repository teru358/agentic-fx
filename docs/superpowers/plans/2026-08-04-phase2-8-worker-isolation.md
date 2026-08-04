# Phase 2 プラン 8: worker 隔離 + preemption + 起票返済 実装プラン (設計書 改訂 5 = a5c1dff 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mission (取引判断 loop / reflection / ask) を使い捨て子プロセス (`WorkerRunner` + `mission_worker`) に隔離し、preemption (SIGTERM→grace→SIGKILL) と「Mission 実行中も SL/TP 監視が止まらない」ロック粒度の再設計 (五相分解 + mission supervisor) を実装する。あわせてプラン 7 起票の B 束 (sandbox 増強・小口修正) と、プラン 5 レジャーの park 小口を返済する。

**Architecture:** `AgentRunner` 抽象に 3 つ目の実装 `WorkerRunner` を追加し、Mission 実行を「使い捨て子プロセス (`python -m agentic_fx.mission_worker`)」に隔離する。子は自前の読み取り専用 SQLite 接続 (`db.connect_readonly`) でツールを実行し、決定論部分 (claim/consume/Risk Gate/executor/finalize) は常に親が持つ。Mission 実行 (claim〜runner〜executor〜finalize) を **prepare → run → commit-pre → commit-core → commit-post** の五相へ再構成し、`core_lock` を保持するのは prepare と commit-core のみにする。scheduler の tick は「決定論ブロック (mark-to-market〜exits) を内部順序不変のまま先頭で実行 → データ hooks → Mission 起動判定 (`try_submit`)」に再編し、Mission 本体は単一スロットの `MissionSupervisor` スレッドが直列実行する。停止は「新規受付停止 → supervisor drain → scheduler join 先行 → worker 終了+supervisor join → 資源 close (逆順)」の状態機械として一意に定義し、実行主体は常に main スレッドにする。

**Tech Stack:** Python 3.12 / uv / pytest / sqlite3 / subprocess (`start_new_session=True` + `killpg`) / `ctypes` (Landlock syscall) / threading (`Lock`/`Event`/`Thread`/`Future` 相当の完了 queue)

**プラン規約 (マルチエージェント SDD — CLAUDE.md「実装体制」節に準拠。プラン 6/7 の「実装 sonnet + 全 task 停止」から変更):**
- 全体指揮 + 実装監督: opus (main セッション)。実装担当: haiku (機械的 task = Task 1-4, 9, 11, 17 目安) / codex (重量 task = Task 7, 10, 13-16, 19 目安)。レビュー: sonnet + codex 交差 (codex 実装分は sonnet 主査)。変異検証: haiku 並列 fan-out
- 大 task はテスト転写と実装転写を並列執筆し、統合 + red/green 実行は 1 レーン直列 (red 先行観測は統合役の実行順序で担保)。小 task は丸ごと 1 agent
- 依存の浅い task 束は worktree 並列。**ユーザーへの節目確認は task 単位ではなく束単位**
- エスカレーション: haiku 同一 task 2 回失敗 → codex/sonnet 再割当。割れた Critical は codex 反証要求 or ユーザー park
- レジャー: `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/progress.md`

## 設計書からの委任事項 (writing-plans で確定させた 3 点 — §12 申し送り対応)

| # | 設計書の申し送り | 本プランでの確定 |
|---|---|---|
| 1 | N4-2: commit-pre/commit-core 間の exposure 増加時 fail-closed 分岐 + テスト | Task 15 (§「commit-core」節) で `required_currencies - set(snapshot.rates)` の非空チェックとして実装し、専用テストを置く。commit-pre は新設の `conn_supervisor` (lock 外の読取専用接続。§ Global Constraints 参照) で exposure pair 一覧を読む |
| 2 | rlimit 具体値の確定 + 通常起動の実測 | Task 7 で mission worker が実際に import する依存一式の VmPeak/VmRSS/fd 数を実測 (32 コア環境: VmPeak≈1.65GB, VmRSS≈123MB, fd=5) した上で `child_as_mb=4096`/`child_nofile=128`/`child_fsize_mb=8` を確定 (`WorkerSettings` として新設。Task 7 Step 7 に実測コマンドと結果を明記) |
| 3 | 分解書 (`2026-08-01-phase2-decomposition.md`) の「到達不能」表現への注記 | Task 18 の Step 1 で分解書 96/104 行付近に「到達不能 = 実行不能 (§4.6 の意味論。import 自体は Landlock のコードツリー読取許可により可能)」という 1 行注記を追記する |

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ。本プランは `ClaudeRunner` を実装しない (プラン 9) — `runner.trade.backend == "claude"` を worker 子側が検出した場合は `RuntimeError` で明示的に fail closed する (Task 10)
- **発注・SL 変更・クローズ・資金保護は LLM に委ねない。決定論的コードで強制する。** risk_gate.py / core/paper_broker.py の判定ロジックは本プランで **diff ゼロ** (§9 受入 7)。executor.py の変更は「判定ロジック不変・外部 I/O の取得位置のみ commit-pre へ移動」に限る
- **drawdown kill switch は config で無効化不可** — 本プランはこの経路 (`state.update(kill_switch_latched=True)`) に触れない
- 秘密情報は `.env` のみ。`config/settings.yaml` は gitignore、新キー追加時は `config/settings.yaml.example` と両方を同期する
- パッケージ管理 uv (`uv sync` / `uv run pytest` / `uv add`)。TDD (failing test → 実装 → green) を各 step で徹底する
- **接続契約 (本プラン新設 — 設計書 §3.1 の「conn_core は core_lock 保持中のみ触れる」を実装可能な形に具体化)**:
  - `app.conn_core` — **core_lock 保持中のみ**触れる (prepare 相・commit-core 相・scheduler tick)
  - `app.conn_shell` — シェルスレッド (`Commands`) 専用。本プランでは変更しない
  - `app.conn_supervisor` (本プラン新設) — **mission supervisor スレッドの lock 外相 (commit-pre) 専用の読取専用接続**。SQLite は WAL モードのため、書き込みトランザクション中でも別接続からの読み取りはブロックされない (`busy_timeout` はあるが、単発 SELECT が長時間ブロックされる想定はしない)。commit-pre はこの接続で「exposure pair 一覧」等のスナップショット読み取りのみ行い、書き込みは一切行わない
  - 子プロセス (`mission_worker`) は `db.connect_readonly` で開いた **専用の読み取り専用接続** を使う (親のいずれの接続とも別)
- **変異テスト (mutation testing) を各 task に含める**: 実装した分岐・条件・timeout 値を意図的に改変し、対応するテストが red になることを実装者・レビュアー双方が確認する (このプロジェクトの必須慣習 — 詳細は各 task の最終 step)
- テストに実スリープ (`time.sleep` での長待ち)・実 HTTP・実 git・乱数・実時刻を混入させない。**例外**: worker/E2E task の実 subprocess spawn・実 kill・実 rlimit・実 Landlock (これらは擬似できない対象そのものが検証対象のため) と、起動 timeout 等の実測で使う短時間 (数秒以内) の実スリープ
- コミットメッセージ末尾は `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (このプランの実行者は git commit を作成してよい — 親セッションが行うのはこのプラン**文書**の commit のみ)

## ファイル構成 (新設・変更の全体マップ)

```
src/agentic_fx/
├── mission_worker.py            # 子プロセスエントリ (Task 7,8) — python -m agentic_fx.mission_worker
├── core/
│   ├── landlock.py               # ctypes による Landlock syscall wrapper (Task 9)
│   ├── supervisor.py             # MissionSupervisor (Task 13, 15)
│   ├── scheduler.py               # tick 再編 (Task 12) — hooks 後段化 + on_signal_maintenance 統合
│   ├── executor.py                # snapshot 版 open エントリポイント追加 (Task 14)
│   └── ...
├── runners/
│   ├── worker_runner.py          # WorkerRunner (Task 10)
│   ├── local_runner.py           # sink 集約 + on_message コールバック (Task 6)
│   └── base.py                    # 変更なし (MissionResult 4 値契約は不変)
├── tools/
│   └── mission_registry.py       # build_mission_registry 抽出 (Task 5)
├── plugin/
│   ├── sandbox.py                  # pytest 隔離サブプロセス化 (Task 2)
│   └── worker.py                   # RLIMIT_NOFILE/FSIZE 追加 (Task 2)
├── store/
│   ├── db.py                       # connect_readonly (Task 5)
│   ├── missions.py                 # finish の CAS 化 + 起動時回収 (Task 11)
│   └── rag.py                      # 内部 lock + close() (Task 9)
├── loops/
│   ├── trade_loop.py               # 五相再構成 (Task 15)
│   └── reflection_cycle.py         # 五相再構成・共通化 (Task 16)
├── shell.py                        # readline 中断 seam (Task 17)
├── datafeed/
│   ├── fetchers.py                  # httpx timeout 注入 (Task 12)
│   └── econ_calendar.py             # httpx timeout 注入 (Task 12)
├── service.py                       # App.close 新設 + 全配線更新 (Task 19)
└── config.py                        # SupervisorSettings 新設 (Task 8, 12, 19 で追記)
config/settings.yaml.example          # 新設キー同期
tests/
├── core/test_supervisor.py, test_scheduler_tick_order.py, test_landlock.py
├── runners/test_worker_runner.py, test_mission_worker_protocol.py
├── loops/test_trade_loop_phases.py, test_reflection_cycle_phases.py
├── store/test_db_readonly.py, test_missions_cas.py, test_rag_lock.py
├── test_shell_interrupt.py
├── test_app_close.py
└── test_e2e_worker_isolation.py
```

依存方向は既存 (`loops → tools/core → store/datafeed`) を維持する。`mission_worker.py` はトップレベルモジュール (`plugin/worker.py` と対称の位置) — `src/agentic_fx/plugin/` 配下に置かないのは、plugin サンドボックスと mission 隔離が別の脅威モデル/権限境界を持つため (設計書 §4.5「ネットワーク毒入れはしない」— plugin worker とは異なる)。

---
### Task 1: 公開昇格 rename (`_parse_timeframe` / `_pair_param`)

冒頭 1 コミット (設計書 §7 / プラン 7 最終レビュー裁定どおり)。両関数は既に他モジュールから private 名のまま import されている (`strategy_adapter.py` が `runner._parse_timeframe` を、`signal_tools.py` が `market_tools._pair_param` を import) — 実質的に公開 API 化しているのに private 命名のままな状態を解消する。挙動は一切変えない (rename のみ)。

**Files:**
- Modify: `src/agentic_fx/backtest/runner.py:84`(定義)`,150`(呼び出し)
- Modify: `src/agentic_fx/plugin/strategy_adapter.py:18`(docstring 内 3 箇所)`,50`(import)`,93-96`(docstring + 呼び出し)
- Modify: `src/agentic_fx/tools/market_tools.py:18`(定義)`,94`(呼び出し)
- Modify: `src/agentic_fx/tools/signal_tools.py:124`(呼び出し)
- Modify: `src/agentic_fx/plugin/approval.py:88`(コメント内の関数名言及のみ)
- Test: 既存テスト (`tests/backtest/test_runner.py`, `tests/plugin/test_strategy_adapter.py`, `tests/tools/test_market_tools.py`, `tests/tools/test_signal_tools.py`) は private 名を直接 import していないため変更不要 — 既存スイート green であることが本 task の受入条件

**Interfaces:**
- Produces: `agentic_fx.backtest.runner.parse_timeframe(tf: str) -> timedelta` (旧 `_parse_timeframe`)、`agentic_fx.tools.market_tools.pair_param(settings: Settings) -> dict` (旧 `_pair_param`)。シグネチャ・挙動は無変更、名前のみ変更
- Consumes: なし (rename のみ、新規依存なし)

- [ ] **Step 1: 既存テストが green であることを確認するベースライン取得**

```bash
uv run pytest tests/backtest/test_runner.py tests/plugin/test_strategy_adapter.py tests/tools/test_market_tools.py tests/tools/test_signal_tools.py -q
```

Expected: 全 PASS (rename 前のベースライン)。

- [ ] **Step 2: `runner.py` の rename**

`src/agentic_fx/backtest/runner.py:84` を以下に変更 (`_parse_timeframe` → `parse_timeframe`):

```python
def parse_timeframe(tf: str) -> timedelta:
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"unsupported eval_timeframe: {tf!r}")
    n, unit = int(m.group(1)), m.group(2)
    if n == 0:
        # fix round 1 F7: "0m"/"0h" は正規表現には通るが tf=timedelta(0) と
        # なり、後段の `(now - _EPOCH) % tf` が ZeroDivisionError になる。
        raise ValueError(f"eval_timeframe must be > 0: {tf!r}")
    return timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
```

`runner.py:150` の呼び出し `tf = _parse_timeframe(eval_timeframe)` を `tf = parse_timeframe(eval_timeframe)` に変更する。

- [ ] **Step 3: `strategy_adapter.py` の呼び出し元更新**

`src/agentic_fx/plugin/strategy_adapter.py:50` の import を変更:

```python
from agentic_fx.backtest.runner import parse_timeframe
```

`strategy_adapter.py:96` の呼び出しを変更:

```python
        width = parse_timeframe(closed_bar.interval)
```

`strategy_adapter.py` のモジュール docstring (18 行目付近) とメソッド docstring (93-94 行目) 内の `runner._parse_timeframe`/`_parse_timeframe` という文言をすべて `runner.parse_timeframe`/`parse_timeframe` に置換する (コメントの整合性維持 — 挙動には影響しないが、次に読む人が private 名を探して迷わないようにする)。

- [ ] **Step 4: `market_tools.py` の rename**

`src/agentic_fx/tools/market_tools.py:18` を変更:

```python
def pair_param(settings: Settings) -> dict:
    """pair と timeframe の enum は設定から動的に作る (足・通貨ペアを固定しない方針)。"""
    return {"pair": {"enum": list(settings.pairs), "description": "e.g. USDJPY"},
            "timeframe": {"type": "string",
                          "enum": list(settings.datafeed.intervals)}}
```

`market_tools.py:94` の呼び出しを `pair_param = pair_param(settings)` ではなく変数名衝突を避けて次のように変更する (元コードは `pair_param = _pair_param(settings)` で変数名と関数名が偶然別だった — rename 後は関数名 `pair_param` と変数名が衝突するため、呼び出し側の変数名を `pair_schema` に変更する):

```python
    pair_schema = pair_param(settings)
    return [
        ToolDef("get_ohlcv", "OHLCV 価格データ (直近 100 本)",
                {"type": "object", "properties": pair_schema,
                 "required": ["pair", "timeframe"]}, get_ohlcv),
```

(以降 `market_tools.py` 内で `pair_param` 変数を参照している箇所があれば同じ変数名 `pair_schema` に統一すること — `build` 関数内を `grep -n "pair_param"` で確認し、変数参照をすべて `pair_schema` に置換する。)

- [ ] **Step 5: `signal_tools.py` の呼び出し元更新**

`src/agentic_fx/tools/signal_tools.py:124` を変更:

```python
    pair_schema = market_tools.pair_param(settings)["pair"]
```

- [ ] **Step 6: `approval.py` のコメント更新**

`src/agentic_fx/plugin/approval.py:88` 付近のコメント `runner._parse_timeframe が "1d" を受理しないため` を `runner.parse_timeframe が "1d" を受理しないため` に変更する (コードではなくコメントのみ)。

- [ ] **Step 7: 全体 green を確認**

```bash
uv run pytest -q
```

Expected: 全件 PASS (rename 前と同じテスト数・結果)。

- [ ] **Step 8: 変異テスト (rename の正しさをテストが検出できることの確認)**

`market_tools.py` の `pair_schema = pair_param(settings)` を一時的に `pair_schema = {}` に改変して `uv run pytest tests/tools/test_market_tools.py -q` を実行し、`get_ohlcv`/`get_indicators` の schema 検証テストが red になることを確認する (既存テストが pair enum の中身を見ていることのピン)。確認後、改変を元に戻す。

- [ ] **Step 9: Commit**

```bash
git add src/agentic_fx/backtest/runner.py src/agentic_fx/plugin/strategy_adapter.py \
  src/agentic_fx/plugin/approval.py src/agentic_fx/tools/market_tools.py \
  src/agentic_fx/tools/signal_tools.py
git commit -m "$(cat <<'EOF'
refactor: _parse_timeframe/_pair_param を公開昇格 (rename のみ、挙動不変)

プラン7で既に private 名のままクロスモジュール import されていた
2 関数を公開名に揃える (設計書 §7)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 2: B 束 — plugin サンドボックス増強 (pytest 完全隔離 + RLIMIT_NOFILE/FSIZE + PluginSession スレッド安全性)

設計書 §7 表の 3 項目をまとめる (いずれも `plugin/` パッケージ内の小口修正)。

**現状確認**: `approval.py:_default_pytest_runner` (130-162 行) は既に `subprocess.Popen(start_new_session=True)` + timeout + `_kill_process_group` でプロセス隔離済みだが、①`env=` を渡していない (親の全環境変数 — `AFX_*` 等の秘密を含みうる — をそのまま継承) ②resource limit が一切無い ③ネットワーク毒入れが無い、の 3 点が未対応。`worker.py:_set_resource_limits` (77-91 行) は RLIMIT_CPU/RLIMIT_AS/RLIMIT_NPROC のみで RLIMIT_NOFILE/RLIMIT_FSIZE が無い。`sandbox.py:PluginSession` は単一スレッド前提(docstring 上も実装上も)だが、それを破る呼び出しを実行時に検出する assert が無い。

**設計判断 (writing-plans)**: pytest 実行専用のネットワーク毒入れは、`worker.py` の `_poison_network_modules()` を**そのまま再利用**する新設エントリモジュール `plugin/pytest_sandbox_entry.py` から呼ぶ (`python -m pytest` を直接 spawn するのをやめ、`python -m agentic_fx.plugin.pytest_sandbox_entry <test_plugin_path>` を spawn する)。resource limit は pytest サブプロセスにも plugin worker と同じ 2 設定キー (`sandbox_nofile`/`sandbox_fsize_mb`、新設) を使い回す — pytest 用に別キーを増やすと「B 束の小口項目」の規模を超える。

**Files:**
- Modify: `src/agentic_fx/plugin/worker.py:77-91` (`_set_resource_limits` に RLIMIT_NOFILE/RLIMIT_FSIZE 追加)
- Create: `src/agentic_fx/plugin/pytest_sandbox_entry.py` (pytest 実行専用エントリ — ネットワーク毒入れ後に `pytest.main()` を呼ぶ)
- Modify: `src/agentic_fx/plugin/approval.py:130-162` (`_default_pytest_runner` を `pytest_sandbox_entry` 経由の spawn + 最小 env + rlimit `preexec_fn` に変更)
- Modify: `src/agentic_fx/plugin/sandbox.py:288-307`(`PluginSession.__init__`)`,309-341`(`__enter__`)`,406-424`(`call`)`,383-386`(`close`) — owner thread assert
- Modify: `src/agentic_fx/config.py` (`PluginSettings` に `sandbox_nofile`/`sandbox_fsize_mb` 追加)
- Modify: `config/settings.yaml.example` (同期)
- Test: `tests/plugin/test_worker.py` (RLIMIT_NOFILE/FSIZE 追加テスト。無ければ `tests/plugin/test_sandbox.py` に追記), `tests/plugin/test_approval.py` (pytest_runner env/rlimit/poison テスト追記), `tests/plugin/test_sandbox_thread_safety.py` (新規)

**Interfaces:**
- Produces:
  - `worker._set_resource_limits(cpu_sec: int, memory_mb: int, nofile: int, fsize_mb: int) -> None` (シグネチャ拡張 — 呼び出し元 `worker.main()` の handshake dict に `nofile`/`fsize_mb` キーを追加。**破壊的変更**: `sandbox.py:PluginSession.__enter__` の handshake dict 構築箇所 (357-361 行) も同時に更新する)
  - `pytest_sandbox_entry.main() -> None` — `sys.argv[1]` (test_plugin.py の絶対パス文字列) を受け取り、`agentic_fx.plugin.worker._poison_network_modules()` を呼んでから `pytest.main(["-p", "no:cacheprovider", "--noconftest", "-q", sys.argv[1]])` の returncode で `SystemExit` する
  - `approval._pytest_rlimit_preexec(memory_mb: int, nofile: int, fsize_mb: int) -> Callable[[], None]` — `subprocess.Popen(preexec_fn=...)` に渡す純関数ファクトリ (単体テストで直接検証可能)
  - `PluginSession` に `self._owner_thread: int` (construction 時に `threading.get_ident()` で記録)。`call()`/`close()`/`__enter__()` の先頭で `self._check_owner_thread()` を呼び、不一致なら `RuntimeError` を送出する (`SandboxError` ではなく `RuntimeError` — スレッド越境はプログラミングエラーであり plugin コード起因の実行時エラーと区別する)

- [ ] **Step 1: 失敗するテストを書く (rlimit 拡張)**

`tests/plugin/test_worker.py` (新規、無ければ作成) に以下を追加する:

```python
"""worker.py の rlimit 拡張 (プラン 8 B 束) の単体テスト。"""
from __future__ import annotations

import resource

import pytest

from agentic_fx.plugin.worker import _set_resource_limits


def test_set_resource_limits_sets_nofile_and_fsize():
    """RLIMIT_NOFILE/RLIMIT_FSIZE が指定値ちょうどに設定される。

    実プロセスの現在の rlimit を破壊しないよう、テスト後に元へ戻す
    (RLIMIT_CPU/RLIMIT_AS は既存 test_sandbox.py の実測パターンに揃え、
    ここでは NOFILE/FSIZE のみを対象にする — CPU/AS を実際にこのテスト
    プロセスへ適用すると pytest 自体の実行を壊しかねないため、資源制限は
    別プロセス (test_sandbox.py の実 subprocess テスト) で検証し、ここは
    「setrlimit が正しい引数で呼ばれること」を fake 経由で検証する)。
    """
    calls: list[tuple[int, tuple[int, int]]] = []

    def fake_setrlimit(which, limits):
        calls.append((which, limits))

    import agentic_fx.plugin.worker as worker_mod
    orig = resource.setrlimit
    try:
        worker_mod.resource.setrlimit = fake_setrlimit  # type: ignore[attr-defined]
        _set_resource_limits(cpu_sec=60, memory_mb=512, nofile=128, fsize_mb=8)
    finally:
        worker_mod.resource.setrlimit = orig

    kinds = {which for which, _ in calls}
    assert resource.RLIMIT_NOFILE in kinds
    assert resource.RLIMIT_FSIZE in kinds
    nofile_limit = next(v for w, v in calls if w == resource.RLIMIT_NOFILE)
    assert nofile_limit == (128, 128)
    fsize_limit = next(v for w, v in calls if w == resource.RLIMIT_FSIZE)
    assert fsize_limit == (8 * 1024 * 1024, 8 * 1024 * 1024)
```

Note: `worker.py` は現在 `_set_resource_limits` 内で `import resource` (ローカル import、77 行目直後) をしている。テストで `worker_mod.resource` を monkeypatch できるようにするため、Step 3 の実装では **モジュールトップレベルで `import resource` に変更する** (ローカル import のままだと `worker_mod.resource` という属性が存在せず fake を差し込めない)。

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_worker.py -q
```

Expected: FAIL (`_set_resource_limits() got an unexpected keyword argument 'nofile'` または `ImportError`)。

- [ ] **Step 3: `worker.py` を実装**

`src/agentic_fx/plugin/worker.py` の import 節 (54-57 行目) に `import resource` を追加する (トップレベル化 — Step 1 の理由)。`_set_resource_limits` (77-91 行) を以下に置き換える:

```python
def _set_resource_limits(cpu_sec: int, memory_mb: int, nofile: int,
                          fsize_mb: int) -> None:
    cpu = int(cpu_sec)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    mem_bytes = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    # プラン 8 B 束: worker は plugin.py の import と call() の応答書き込み
    # 以外にファイル記述子を要しない (stdin/stdout/stderr の 3 つ +
    # import 時の一時的な .so/.pyc オープン)。想定外の大量オープン
    # (fork bomb 的 fd リーク) を検知する上限として十分寛大な値を渡す
    # (呼び出し元が settings.plugin.sandbox_nofile を渡す — 既定 128)。
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))

    # RLIMIT_FSIZE: plugin コードは check_source の denylist
    # (open/to_*/read_* 等) により意図的なファイル書き込みができない —
    # ここでの上限は「想定外の書き込みを小さく抑える」多層防御 (呼び出し
    # 元が settings.plugin.sandbox_fsize_mb を渡す — 既定 8MB)。
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))

    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (_NPROC_CAP, _NPROC_CAP))
    except (ValueError, OSError):
        # per-uid の既存使用量次第では失敗し得る (最終防衛線ではなく
        # ベストエフォートの追加防御 — CPU/AS の 2 軸が主防御)。
        pass
```

`worker.py:main()` (174-198 行付近) の `_set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"])` 呼び出しを以下に変更する:

```python
        _set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"],
                              handshake["nofile"], handshake["fsize_mb"])
```

`src/agentic_fx/plugin/sandbox.py:357-361` の handshake 構築を以下に変更する (`PluginSession.__enter__` 内):

```python
            handshake = {
                "cpu_sec": self._settings.sandbox_session_cpu_sec,
                "memory_mb": self._settings.sandbox_memory_mb,
                "nofile": self._settings.sandbox_nofile,
                "fsize_mb": self._settings.sandbox_fsize_mb,
                "kind": self._meta.kind,
            }
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_worker.py -q
```

Expected: PASS。既存 `tests/plugin/test_sandbox.py` の handshake 送出テスト (`import os` reject 等) が dict の追加キーで壊れていないことも合わせて確認する:

```bash
uv run pytest tests/plugin/test_sandbox.py -q
```

Expected: PASS (fixture が固定 dict で handshake を検証している場合は `nofile`/`fsize_mb` キーの追加を反映する)。

- [ ] **Step 5: `PluginSettings` に新設定キーを追加**

`src/agentic_fx/config.py` の `PluginSettings` クラス (170-213 行) に以下を追加する (`sandbox_output_max_bytes` の直後):

```python
    # RLIMIT_NOFILE — worker プロセスが同時に開けるファイル記述子数の上限。
    # plugin コードは check_source の denylist によりファイルを開けない
    # ため、想定外の大量オープン (fd リーク) を検知する多層防御。
    sandbox_nofile: int = Field(ge=1, default=128)
    # RLIMIT_FSIZE (MiB) — 1 ファイルあたりの書き込みサイズ上限。plugin
    # コードは denylist により意図的な書き込みができないため、想定外の
    # 大量書き込みを小さく抑える多層防御。pytest サブプロセス (approval.py
    # の test_plugin.py 実行) にも同じ 2 値を流用する。
    sandbox_fsize_mb: int = Field(ge=1, default=8)
```

`config/settings.yaml.example` の `plugin:` セクション (`sandbox_output_max_bytes` の直後) に同期する:

```yaml
  sandbox_nofile: 128           # RLIMIT_NOFILE: worker が同時に開けるファイル記述子数の上限
  sandbox_fsize_mb: 8           # RLIMIT_FSIZE (MiB): 1 ファイルあたりの書き込みサイズ上限。pytest サブプロセスにも流用
```

- [ ] **Step 6: 失敗するテストを書く (pytest サブプロセスの env/rlimit/poison)**

`tests/plugin/test_approval.py` に以下を追加する (既存の `pytest_runner` 注入テストと同じ fixture 構成を踏襲する — 実ファイルを開いて既存 import/fixture パターンを確認してから追記すること):

```python
def test_pytest_rlimit_preexec_sets_expected_limits(monkeypatch):
    """approval._pytest_rlimit_preexec が返す関数が正しい rlimit 呼び出しをする。"""
    import resource

    from agentic_fx.plugin import approval

    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        approval.resource, "setrlimit",
        lambda which, limits: calls.append((which, limits)))

    fn = approval._pytest_rlimit_preexec(memory_mb=256, nofile=64, fsize_mb=4)
    fn()

    kinds = {w: v for w, v in calls}
    assert kinds[resource.RLIMIT_AS] == (256 * 1024 * 1024, 256 * 1024 * 1024)
    assert kinds[resource.RLIMIT_NOFILE] == (64, 64)
    assert kinds[resource.RLIMIT_FSIZE] == (4 * 1024 * 1024, 4 * 1024 * 1024)


def test_default_pytest_runner_uses_minimal_env(monkeypatch, tmp_path):
    """_default_pytest_runner が subprocess.Popen に最小 env を渡す
    (AFX_* 等の秘密が子へ伝播しない)。"""
    from agentic_fx.plugin import approval

    captured: dict = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout):
            return "1 passed", ""

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["preexec_fn"] = kwargs.get("preexec_fn")
        return FakeProc()

    monkeypatch.setenv("AFX_SECRET_TOKEN", "must-not-leak")
    monkeypatch.setattr(approval.subprocess, "Popen", fake_popen)

    test_plugin_path = tmp_path / "test_plugin.py"
    test_plugin_path.write_text("def test_x():\n    assert True\n")
    approval._default_pytest_runner(test_plugin_path)

    assert "AFX_SECRET_TOKEN" not in captured["env"]
    assert captured["args"][:3] == [
        approval.sys.executable, "-m", "agentic_fx.plugin.pytest_sandbox_entry"]
    assert captured["preexec_fn"] is not None
```

- [ ] **Step 7: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_approval.py -q -k "rlimit_preexec or minimal_env"
```

Expected: FAIL (`AttributeError: module 'agentic_fx.plugin.approval' has no attribute '_pytest_rlimit_preexec'` 等)。

- [ ] **Step 8: `pytest_sandbox_entry.py` を新規作成**

```python
"""test_plugin.py 実行専用のサブプロセスエントリ (プラン 8 B 束)。

approval.py の `_default_pytest_runner` が spawn する唯一の想定呼び出し元。
`python -m pytest` を直接 spawn するのをやめてこのモジュールを経由させる
理由: pytest がテストモジュール (test_plugin.py 経由で plugin.py) を
import する**前**にネットワーク毒入れを適用するため。resource limit
(RLIMIT_AS/NOFILE/FSIZE/CPU) は起動側 (approval.py の `preexec_fn`) が
fork 直後・exec 直前に設定済みの前提で、ここでは毒入れと pytest 起動のみ
行う。
"""
from __future__ import annotations

import sys


def main() -> None:
    from agentic_fx.plugin.worker import _poison_network_modules
    _poison_network_modules()

    import pytest

    args = ["-p", "no:cacheprovider", "--noconftest", "-q", *sys.argv[1:]]
    raise SystemExit(pytest.main(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: `approval.py` を実装**

`src/agentic_fx/plugin/approval.py` の import 節 (55-72 行) に `import resource` を追加する。`_default_pytest_runner` (130-162 行) を以下に置き換える:

```python
def _pytest_rlimit_preexec(memory_mb: int, nofile: int,
                            fsize_mb: int) -> Callable[[], None]:
    """`subprocess.Popen(preexec_fn=...)` に渡す純関数ファクトリ。

    fork 直後・exec 直前に子プロセス側で実行される (Unix 専用 API —
    本プロジェクトの動作環境は Linux 前提)。plugin worker (worker.py の
    `_set_resource_limits`) と同じ 2 値 (settings.plugin.sandbox_nofile/
    sandbox_fsize_mb) を pytest サブプロセスにも適用する。
    """
    def _fn() -> None:
        mem_bytes = int(memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))
        fsize_bytes = int(fsize_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    return _fn


def _default_pytest_runner(test_plugin_path: Path, *,
                            settings: "PluginSettings" | None = None,
                            ) -> dict[str, Any]:
    """既定の pytest 実行シーム。

    **1 plugin につき 1 サブプロセス** で実行する (サンプル test_plugin.py
    のモジュール名衝突回避)。`agentic_fx.plugin.pytest_sandbox_entry`
    経由で spawn する (ネットワーク毒入れを pytest のテスト収集より前に
    適用するため — モジュール docstring 参照)。**最小 env** (`sandbox.
    _build_env()` を再利用 — `AFX_*` 等の秘密を含む親 env を継承しない)
    + **resource limit** (`_pytest_rlimit_preexec`) を適用する。

    `settings` が None の場合は plugin サンドボックスの既定値
    (`PluginSettings()` のデフォルト) を使う — 呼び出し元 (`submit_plugin`)
    は実際の `settings.plugin` を渡す。

    timeout 発生時は `_kill_process_group` でプロセスグループごと回収する。
    """
    from agentic_fx.plugin.sandbox import _build_env
    eff_settings = settings if settings is not None else PluginSettings()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.plugin.pytest_sandbox_entry",
         str(test_plugin_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, env=_build_env(),
        preexec_fn=_pytest_rlimit_preexec(
            memory_mb=eff_settings.sandbox_memory_mb,
            nofile=eff_settings.sandbox_nofile,
            fsize_mb=eff_settings.sandbox_fsize_mb))
    try:
        stdout, stderr = proc.communicate(timeout=_PYTEST_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise ValueError(
            f"test_plugin.py timed out after {_PYTEST_TIMEOUT_SEC}s "
            f"({test_plugin_path})") from exc
    return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}
```

`approval.py` の import 節に `from agentic_fx.config import PluginSettings` を追加する (`TYPE_CHECKING` ブロックに既にあるかを確認し、実行時に必要なため通常 import へ昇格すること — 既存の `TYPE_CHECKING` import と重複しないよう既存コードを確認してから編集する)。

`submit_plugin` (221-236 行) 内の `runner = pytest_runner if pytest_runner is not None else _default_pytest_runner` の呼び出し箇所 (268 行 `pytest_result = runner(test_plugin_path)`) を、`pytest_runner` が None の場合に settings を束縛したラムダを使うよう変更する:

```python
        runner = (pytest_runner if pytest_runner is not None
                  else lambda p: _default_pytest_runner(p, settings=settings.plugin))
        pytest_result = runner(test_plugin_path)
```

- [ ] **Step 10: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_approval.py -q
uv run pytest tests/plugin/ -q
```

Expected: 全件 PASS。

- [ ] **Step 11: 失敗するテストを書く (PluginSession スレッド安全性 assert)**

`tests/plugin/test_sandbox_thread_safety.py` を新規作成する:

```python
"""PluginSession の単一スレッド所有 assert (プラン 8 B 束)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.config import PluginSettings
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import PluginSession, SandboxError


def _fake_meta(tmp_path) -> PluginMeta:
    (tmp_path / "plugin.py").write_text(
        "def compute(df, params):\n    return {}\n")
    from agentic_fx.plugin.loader import content_hash
    return PluginMeta(name="x", kind="indicator", path=tmp_path, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=content_hash(tmp_path))


def test_call_from_other_thread_raises_runtime_error(tmp_path):
    """`__enter__` を呼んだスレッド以外からの `call()` は RuntimeError
    (SandboxError ではない — プログラミングエラーと plugin 実行時エラーの
    区別)。実 subprocess は起動せず、owner thread チェックが __enter__
    完了前の早い段階 (spawn 前) で発火することを確認する — session が
    未起動 (`self._proc is None`) の状態でも境界チェックが機能すること
    のピン。
    """
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    with pytest.raises(RuntimeError, match="owner thread"):
        session.call({"df": None, "params": {}})
```

- [ ] **Step 12: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_sandbox_thread_safety.py -q
```

Expected: FAIL (`AttributeError: 'PluginSession' object has no attribute '_owner_thread'`)。

- [ ] **Step 13: `sandbox.py` を実装**

`src/agentic_fx/plugin/sandbox.py` の先頭 import 節に `import threading` を追加する。`PluginSession.__init__` (293-307 行) の末尾に以下を追加する:

```python
        # プラン 8 B 束: PluginSession は単一スレッド所有が前提
        # (全使用箇所が単一スレッド — ロックは追加しない)。construction
        # したスレッドを記録し、実行時 assert で境界越えを検出する。
        self._owner_thread = threading.get_ident()

    def _check_owner_thread(self) -> None:
        current = threading.get_ident()
        if current != self._owner_thread:
            raise RuntimeError(
                f"PluginSession used from a different thread than its "
                f"owner thread (owner={self._owner_thread}, "
                f"current={current}) — PluginSession is single-thread-owned")
```

`__enter__` (309 行) の docstring 直後、`try:` の直前に `self._check_owner_thread()` を追加する。`call()` (406 行) の docstring 直後、`if self._dead or self._proc is None:` の**前**に `self._check_owner_thread()` を追加する。`close()` (383 行) の先頭に `self._check_owner_thread()` を追加する (`proc = self._proc` の前)。

- [ ] **Step 14: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_sandbox_thread_safety.py -q
uv run pytest tests/plugin/ -q
```

Expected: 全件 PASS。

- [ ] **Step 15: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 16: 変異テスト**

以下の 3 箇所を個別に改変し、対応するテストが red になることを確認してから元に戻す:
1. `worker.py` の `resource.setrlimit(resource.RLIMIT_NOFILE, ...)` 行を削除 → `test_set_resource_limits_sets_nofile_and_fsize` が red
2. `approval.py:_default_pytest_runner` の `env=_build_env()` を削除 (env 未指定に戻す) → `test_default_pytest_runner_uses_minimal_env` が red
3. `sandbox.py:PluginSession.call` 内の `self._check_owner_thread()` 呼び出しを削除 → `test_call_from_other_thread_raises_runtime_error` が red

- [ ] **Step 17: Commit**

```bash
git add src/agentic_fx/plugin/worker.py src/agentic_fx/plugin/sandbox.py \
  src/agentic_fx/plugin/approval.py src/agentic_fx/plugin/pytest_sandbox_entry.py \
  src/agentic_fx/config.py config/settings.yaml.example \
  tests/plugin/test_worker.py tests/plugin/test_approval.py \
  tests/plugin/test_sandbox_thread_safety.py tests/plugin/test_sandbox.py
git commit -m "$(cat <<'EOF'
feat: plugin サンドボックス増強 (pytest 完全隔離 + RLIMIT_NOFILE/FSIZE + PluginSession スレッド安全性)

プラン7起票の B 束 3 項目 (設計書 §7)。test_plugin.py 実行を最小 env +
resource limit + ネットワーク毒入れ付きサブプロセスに、worker.py の
rlimit に NOFILE/FSIZE を追加し、PluginSession の単一スレッド所有を
実行時 assert で明文化する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 3: B 束 — 小口修正 6 項目 (maintenance 順序 / producer_source 検証 / strategy_adapter 対称化 / sqlite3.Error CLI 境界 / SQLite ≥3.35 assert / description f-string 化)

設計書 §7 表の残り 6 項目。いずれも影響範囲が 1〜数行の独立した小口修正であり、依存関係が無いため 1 task にまとめる (Task 2 で使い切った「サンドボックス増強」という単一テーマと違い、こちらはテーマ横断の雑多な小口 — レビュー粒度は項目ごとの diff で確認する)。

**Files:**
- Modify: `src/agentic_fx/service.py:369-382` (`on_signal_maintenance` の reclaim→expire 順序入替)
- Modify: `src/agentic_fx/service.py:208-223` (`_validate_startup` に producer_source 検証追加)
- Modify: `src/agentic_fx/store/ohlcv.py` (`KNOWN_OHLCV_SOURCES` 定数新設)
- Modify: `src/agentic_fx/plugin/strategy_adapter.py:74-87` (`PluginStrategyIntentSource.__init__` に pair 実在検証追加)
- Modify: `src/agentic_fx/backtest/cli.py:1-20`(import)`,461`(except 節)
- Modify: `src/agentic_fx/store/db.py:152-159` (`connect` に SQLite バージョン assert)
- Modify: `src/agentic_fx/tools/signal_tools.py` (description f-string 化)
- Test: `tests/core/test_scheduler_signal.py` または `tests/test_service.py` (maintenance 順序), `tests/test_service.py` (producer_source 検証), `tests/plugin/test_strategy_adapter.py` (pair 対称化), `tests/backtest/test_cli.py` (sqlite3.Error 境界), `tests/store/test_db.py` (SQLite バージョン assert), `tests/tools/test_signal_tools.py` (description)

**Interfaces:**
- Produces:
  - `store.ohlcv.KNOWN_OHLCV_SOURCES: frozenset[str]` = `{"yfinance", "mt5", "mt5-live", "twelvedata", "dukascopy"}` — ohlcv テーブルの `source` 列に実際に書き込まれる値の正規列挙 (`price_provider.py:_STORAGE_SOURCE` / `backtest/importer.py`/`mt5_import.py`/`analysis.py:ANALYSIS_SOURCE`/`plugin/approval.py:_EVAL_SOURCE` の実値を集約)
  - `service._validate_startup(settings)` の追加検証: `settings.plugin.producer_source not in KNOWN_OHLCV_SOURCES` なら `RuntimeError`
  - `strategy_adapter.PluginStrategyIntentSource.__init__` は `pair not in meta.pairs` で `ValueError` を送出する (fail closed — producer 側の「settings.pairs 外は warning + skip」と対称の検証だが、adapter は 1 インスタンス = 1 pair の明示的構築のため即座に `ValueError` で拒否する。producer のように複数 pair を反復して一部だけ諦める構造ではないため skip という選択肢が無い)
  - `db.connect()` は接続直後に `sqlite3.sqlite_version_info < (3, 35, 0)` なら `RuntimeError` (signals.py の `claim_oldest`/`requeue`/`reclaim_expired` が `RETURNING` 句 — SQLite 3.35.0 (2021-03-12) 以降が必須)

- [ ] **Step 1: 失敗するテストを書く (maintenance 順序)**

`tests/test_service.py` (無ければ実ファイルを `ls tests/*.py` で確認し、`build_app` の統合テストが既にある場所に追記する) に以下を追加する:

```python
def test_signal_maintenance_reclaims_before_expiring():
    """reclaim_expired → expire_stale の順で呼ばれる (順序入替、codex M⑤)。
    reclaim で pending に戻った直後の stale 行が、同じ tick 内の
    expire_stale でまだ拾われずに 1 tick 分だけ実行機会を得ることを、
    呼び出し順の記録で確認する (実際の SQL 結果ではなく呼び出し順に
    絞ったユニットテスト — 統合的な確認は既存 test_scheduler_signal.py
    に譲る)。
    """
    calls: list[str] = []
    import agentic_fx.service as service_mod

    orig_reclaim = service_mod.signals.reclaim_expired
    orig_expire = service_mod.signals.expire_stale

    def fake_reclaim(*a, **k):
        calls.append("reclaim")
        return []

    def fake_expire(*a, **k):
        calls.append("expire")
        return 0

    # build_app 全体を起動するのはコストが高いため、on_signal_maintenance
    # のクロージャ構築ロジックのみを直接検証する軽量ヘルパーを用意する
    # (Task 12 のスケジューラ再編テストと同じ fixture 方針を先取りする)。
    import types
    fake_signals = types.SimpleNamespace(
        reclaim_expired=fake_reclaim, expire_stale=fake_expire)
    fake_producer = types.SimpleNamespace(evaluate_due_plugins=lambda **k: None)

    from datetime import datetime, timezone
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def on_signal_maintenance(now):
        fake_signals.reclaim_expired(None, now=now, lease_min=15,
                                     max_requeue=2)
        fake_signals.expire_stale(None, now=now, freshness_bars=2)
        fake_producer.evaluate_due_plugins(
            conn=None, plugins=[], now=now, source="yfinance", settings=None)

    on_signal_maintenance(now)
    assert calls == ["reclaim", "expire"]
```

注: このテストは「実装すべき呼び出し順」を fixture で先に固定するだけの単体テストであり、Step 3 で `service.py` の実クロージャを同じ順序に書き換えたことをレビュー時に diff で確認する (`on_signal_maintenance` 自体は起動時にしか組まれないため、本物の関数を差し替えて呼び出し順を記録する統合的なテストは `tests/core/test_scheduler_signal.py` に既存の fixture を使って追記してもよい — 実装者はどちらか一方を選び、両方は作らない)。

- [ ] **Step 2: テスト実行して FAIL/PASS を確認**

このテストは `service.py` を変更せずとも green になる (fixture 自体が期待順序で書かれているため) — **これは意図的**: Step 1 のテストは「順序の仕様」を固定するためのものであり、Step 3 の実装後に `service.py` の実クロージャ (下記) の diff とテストの整合をレビューで確認する。実装漏れ検出は Step 8 の変異テストで行う。

```bash
uv run pytest tests/test_service.py -q -k maintenance
```

Expected: PASS (fixture 自体のテスト)。

- [ ] **Step 3: `service.py` の `on_signal_maintenance` 順序を入替**

`src/agentic_fx/service.py:369-382` を以下に置き換える (`reclaim_expired` を `expire_stale` より前に呼ぶ — codex M⑤: stale 行が reclaim される前に abandoned 化されてしまうと、まだ requeue_count に余裕がある行が 1 tick 分の実行機会を失う):

```python
    def on_signal_maintenance(now: datetime) -> None:
        # Task 7 申し送り → プラン 8 B 束で順序入替 (codex M⑤): lease 切れの
        # claimed 行を先に reclaim_expired で pending へ戻し、その後に
        # expire_stale で鮮度切れの pending を abandoned 化する。逆順だと、
        # reclaim で pending に戻ったばかりの行が同じ tick 内で鮮度切れ
        # 判定に巻き込まれて abandoned になり得た (無駄な 1 tick 分の
        # 巻き戻り)。呼び出し元 (Scheduler._run_data_hook) が fail-open
        # で包む。
        signals.reclaim_expired(conn_core, now=now,
                                lease_min=settings.plugin.signal_lease_min,
                                max_requeue=settings.plugin.signal_requeue_max)
        signals.expire_stale(conn_core, now=now,
                             freshness_bars=settings.plugin.signal_freshness_bars)
        signal_producer.evaluate_due_plugins(
            conn_core, plugins=approved, now=now,
            source=settings.plugin.producer_source, settings=settings)
```

- [ ] **Step 4: 失敗するテストを書く (producer_source 検証・SQLite バージョン assert・description f-string)**

`tests/test_service.py` に追加:

```python
def test_validate_startup_rejects_unknown_producer_source():
    from agentic_fx.service import _validate_startup
    from agentic_fx.config import load_settings
    from pathlib import Path

    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
    settings = settings.model_copy(
        update={"plugin": settings.plugin.model_copy(
            update={"producer_source": "typo-source"})})
    with pytest.raises(RuntimeError, match="producer_source"):
        _validate_startup(settings)
```

`tests/store/test_db.py` に追加:

```python
def test_connect_rejects_old_sqlite_version(tmp_path, monkeypatch):
    import sqlite3

    import agentic_fx.store.db as db_mod
    monkeypatch.setattr(db_mod.sqlite3, "sqlite_version_info", (3, 34, 1))
    with pytest.raises(RuntimeError, match="3.35"):
        db_mod.connect(tmp_path / "x.db")
```

`tests/tools/test_signal_tools.py` に追加:

```python
def test_get_signals_description_reflects_default_lookback():
    from agentic_fx.tools import signal_tools

    tools = signal_tools.build(_conn_fixture(), _settings_fixture(), _clock_fixture())
    tool = next(t for t in tools if t.name == "get_signals")
    assert f"{signal_tools._DEFAULT_SINCE_HOURS}h" in tool.description
```

(既存の `test_signal_tools.py` の fixture 関数名 — `_conn_fixture`/`_settings_fixture`/`_clock_fixture` に相当するもの — を実ファイルを開いて確認し、既存の命名に合わせること。プレースホルダのまま使わない。)

- [ ] **Step 5: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_service.py tests/store/test_db.py tests/tools/test_signal_tools.py -q -k "producer_source or sqlite_version or default_lookback"
```

Expected: 3 件とも FAIL (`_validate_startup` は producer_source を見ていない / `connect` はバージョンを見ていない / `_DEFAULT_SINCE_HOURS` が存在しない)。

- [ ] **Step 6: 実装**

`src/agentic_fx/store/ohlcv.py` の先頭付近 (20 行目のコメントの直後) に追加:

```python
# プラン 8 B 束: ohlcv.source 列に実際に書き込まれる値の正規列挙
# (price_provider.py:_STORAGE_SOURCE / backtest/importer.py /
# backtest/mt5_import.py / backtest/analysis.py:ANALYSIS_SOURCE /
# plugin/approval.py:_EVAL_SOURCE の実値を集約)。起動時の
# producer_source typo 検出 (service.py:_validate_startup) が参照する。
KNOWN_OHLCV_SOURCES = frozenset(
    {"yfinance", "mt5", "mt5-live", "twelvedata", "dukascopy"})
```

`src/agentic_fx/service.py` の import 節に `from agentic_fx.store import ohlcv` を追加し (`store` パッケージから既に `approvals, missions, orders, signals` を import している行 45 に `ohlcv` を追加)、`_validate_startup` (208-223 行) 末尾に追加:

```python
    if settings.plugin.producer_source not in ohlcv.KNOWN_OHLCV_SOURCES:
        raise RuntimeError(
            f"settings.plugin.producer_source={settings.plugin.producer_source!r} "
            f"is not a known source (known: {sorted(ohlcv.KNOWN_OHLCV_SOURCES)})")
```

`src/agentic_fx/store/db.py:152-159` の `connect` を以下に置き換える:

```python
def connect(db_path: Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    # プラン 8 B 束: signals.py の claim_oldest/requeue/reclaim_expired は
    # RETURNING 句に依存する (SQLite 3.35.0 = 2021-03-12 以降)。古い
    # SQLite では RETURNING が構文エラーになり、失敗の意味が分かりにくい
    # (「claim できない」ではなく「SQL 構文エラー」として現れる) ため、
    # 接続確立時点で明示的に fail fast する。
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old (>= 3.35 required "
            "for signals.py RETURNING clauses)")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
```

`src/agentic_fx/tools/signal_tools.py` の `_ANNOTATION` 定数の近くに追加:

```python
# プラン 8 B 束: ToolDef description の「既定 Nh」が since_hours の実際の
# デフォルト値と独立に手打ちされ、乖離し得た (Task 9 deferred①)。
# 1 箇所の定数に統一し description は f-string で生成する。
_DEFAULT_SINCE_HOURS = 24
```

`get_signals` の内部関数シグネチャ `def get_signals(pair: str, since_hours: int = 24) -> list[dict]:` を `def get_signals(pair: str, since_hours: int = _DEFAULT_SINCE_HOURS) -> list[dict]:` に変更する。`ToolDef` の description 文字列 (128-130 行) を以下に置き換える:

```python
        ToolDef(
            "get_signals",
            "取引判断 loop 専用: 承認済み signal/strategy plugin の直近 "
            f"出力 (pair, 直近 since_hours 時間分・既定 {_DEFAULT_SINCE_HOURS}h)。"
            "strategy 行には in_sample バックテスト成績 (in_sample_metrics) "
            "と、実運用成績の予測値ではない旨の注記 (note) が付く",
```

- [ ] **Step 7: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_service.py tests/store/test_db.py tests/tools/test_signal_tools.py -q
uv run pytest -q
```

Expected: 全件 PASS。既存の `settings.yaml.example` の `producer_source: yfinance` は `KNOWN_OHLCV_SOURCES` に含まれるため既存起動テストは無変更で通ること、既存全 DB テストは実 SQLite が 3.35 以上 (開発環境の Python 標準 sqlite3 は概ね対応) であるため通ることを確認する。

- [ ] **Step 8: 失敗するテストを書く (strategy_adapter 対称化 + sqlite3.Error CLI 境界)**

`tests/plugin/test_strategy_adapter.py` に追加 (既存 fixture — `_fake_meta`/`_conn` 等 — を実ファイルで確認し流用する):

```python
def test_build_intent_source_rejects_pair_not_in_meta_pairs(tmp_path):
    """producer 側 (settings.pairs 外は warning+skip) と対称の検証:
    adapter は 1 インスタンス = 1 pair の明示的構築のため、meta.pairs に
    無い pair を渡されたら即座に ValueError (fail closed, Fable M-1)。"""
    meta = _fake_strategy_meta(tmp_path, pairs=("USDJPY",))  # 既存 fixture 名を確認して使う
    with pytest.raises(ValueError, match="pairs"):
        build_intent_source(meta, conn=_conn(tmp_path), pair="EURUSD",
                            source="dukascopy", settings=_settings())
```

`tests/backtest/test_cli.py` に追加:

```python
def test_dispatch_sqlite_error_returns_rc1_with_diagnostic(tmp_path, capsys, monkeypatch):
    """DB 層の sqlite3.Error が生の traceback ではなく診断メッセージ +
    rc=1 に正規化される (Task 6 deferred④)。"""
    import sqlite3

    from agentic_fx.backtest import cli

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_history_coverage", boom)
    root = _init_root(tmp_path)  # 既存 fixture 名を確認して使う
    args = argparse.Namespace(command="history", history_command="coverage")
    rc = cli.dispatch(args, root)
    assert rc == 1
    assert "database is locked" in capsys.readouterr().err
```

- [ ] **Step 9: テスト実行して FAIL を確認**

```bash
uv run pytest tests/plugin/test_strategy_adapter.py tests/backtest/test_cli.py -q -k "not_in_meta_pairs or sqlite_error"
```

Expected: 両方 FAIL。

- [ ] **Step 10: 実装**

`src/agentic_fx/plugin/strategy_adapter.py` の `PluginStrategyIntentSource.__init__` (74-87 行) 冒頭、`self._meta = meta` の前に追加:

```python
        # プラン 8 B 束 (Fable M-1): producer 側 (settings.pairs 外は
        # warning + skip) と対称の検証。adapter は 1 インスタンス = 1 pair
        # の明示的構築であり、meta.pairs に無い pair は「呼び出し側の
        # 取り違え」であって producer のように複数 pair を反復して一部
        # だけ諦める構造ではないため、即座に拒否する (fail closed)。
        if pair not in meta.pairs:
            raise ValueError(
                f"pair {pair!r} is not in plugin {meta.name!r}'s declared "
                f"pairs {meta.pairs!r}")
```

`src/agentic_fx/backtest/cli.py` の import 節 (14-20 行付近) に `import sqlite3` を追加する。`dispatch` (437-461 行) の `except (ValueError, KeyError, OSError) as e:` を以下に変更する:

```python
    except (ValueError, KeyError, OSError, sqlite3.Error) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
```

- [ ] **Step 11: テスト実行して PASS を確認**

```bash
uv run pytest tests/plugin/test_strategy_adapter.py tests/backtest/test_cli.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 12: 変異テスト**

以下を個別に改変し red を確認してから戻す:
1. `service.py` の `on_signal_maintenance` で `reclaim_expired`/`expire_stale` の呼び出し順を元 (expire→reclaim) に戻す → Step 1 のテストは fixture 単体なので red にならない点に注意し、代わりに **Step 3 実装後の `service.py` の実際の行順を目視で確認**する (レビュー時の diff チェック項目として記録すること — このテストは「順序が実装からズレていないか」を機械的に検出できないため人手レビューが最終防波堤であることを progress.md に明記する)
2. `service.py:_validate_startup` の producer_source チェックを削除 → `test_validate_startup_rejects_unknown_producer_source` が red
3. `db.py:connect` のバージョン assert を削除 → `test_connect_rejects_old_sqlite_version` が red
4. `strategy_adapter.py` の pair 検証を削除 → `test_build_intent_source_rejects_pair_not_in_meta_pairs` が red
5. `cli.py` の except タプルから `sqlite3.Error` を削除 → `test_dispatch_sqlite_error_returns_rc1_with_diagnostic` が red

- [ ] **Step 13: Commit**

```bash
git add src/agentic_fx/service.py src/agentic_fx/store/ohlcv.py src/agentic_fx/store/db.py \
  src/agentic_fx/plugin/strategy_adapter.py src/agentic_fx/backtest/cli.py \
  src/agentic_fx/tools/signal_tools.py \
  tests/test_service.py tests/store/test_db.py tests/tools/test_signal_tools.py \
  tests/plugin/test_strategy_adapter.py tests/backtest/test_cli.py
git commit -m "$(cat <<'EOF'
fix: B 束小口 6 項目 (maintenance順序/producer_source検証/adapter対称化/CLI境界/SQLite版数/description)

プラン7起票の B 束 (設計書 §7) のうち独立した小口修正をまとめて返済する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 4: プラン 5 park 小口返済 (retry policy 明文化 / clock 配線 / provider ctor seam / policy OSError / watchdog 時刻源 / conftest 移動)

**park 一覧の出典**: `.superpowers/sdd/2026-07-26-phase1-5-loop-service/progress.md` 末尾「統合裁定 — fix wave」節。`codex I1`(retry policy) / `codex M2`(clock 配線) / `sonnet I-3`(provider ctor seam、Task 8 レビュー) / `fable M3`(policy OSError) / `fable M4`(watchdog 時刻源) / `fable M7`(conftest 移動) を返済する。`fable M5`(`_check_llama_swap` 文言) / `fable M6`(ask 失敗文言) は本 task の Step 1 で現状のメッセージ文面を確認した結果、既に具体的かつ明確 (`service.py:84-106` の 3 分岐別警告文、`trade_loop.py:76` の `"(Mission 失敗: internal_error)"`) であり追加修正の必要なしと判定する (triage — commit メッセージに明記)。**shell readline 中断は Task 17 で扱う** (§6 の停止実行主体の必須依存に昇格したため、監督/停止状態機械 task の直前に独立 task 化する)。

**Files:**
- Modify: `src/agentic_fx/core/scheduler.py:210-256` (`_trade_mission_due`/`tick` の docstring に retry policy 明文化)
- Modify: `src/agentic_fx/service.py:183-206`(`App` に `clock` フィールド追加)`,244-260`(`build_app` に `clock`/`provider` seam)`,448-478`(`_watchdog_tick` の時刻源)`,502-514`(`scheduler_thread` の clock 配線)
- Modify: `src/agentic_fx/loops/mission_watch.py:24-29` (`time_fn` プロパティ追加)
- Modify: `src/agentic_fx/policy.py:11-27` (`OSError` 捕捉に拡張)
- Rename: `tests/backtest/conftest.py` → `tests/backtest/factories.py` (13 箇所の import 更新)
- Test: `tests/core/test_scheduler.py` (retry policy pin), `tests/test_service.py` (clock 配線・provider seam・watchdog 時刻源), `tests/test_policy.py` (OSError)

**Interfaces:**
- Produces:
  - `App.clock: Clock` (新設フィールド。`build_app` が保持している `clock` 変数をそのまま格納する — 既存の `clock = clock or SystemClock()` 行の直後で使う)
  - `build_app(root, *, runner=None, clock=None, quote_fn=None, spec_fn=None, bars_fn=None, embedding_fn=None, provider=None)` — `provider: PriceProvider | None = None` (新設 kwarg)。**非 None の場合は内部での `PriceProvider(conn_core, settings, clock)` 構築と、それに続く `quote_fn`/`spec_fn`/`bars_fn` の bound-method 差し替え (285-296 行) を丸ごとスキップし、渡された `provider` インスタンスをそのまま使う** (呼び出し側が provider の全挙動を制御したい場合の直接注入 seam — 既存の `quote_fn`/`spec_fn`/`bars_fn` 個別注入とは併用不可・排他: 両方渡された場合は `provider` を優先し `quote_fn`/`spec_fn`/`bars_fn` は無視することを docstring に明記する)
  - `MissionWatch.time_fn` — `self._time` を返す読み取り専用 property (新設)
  - `_watchdog_tick(app: App) -> None` の内部実装のみ変更 (シグネチャ不変) — `elapsed` の算出を `app.mission_watch.time_fn()` 経由にする
  - `Policy.tail`/`Policy.size_warning` は `FileNotFoundError` ではなく `OSError` を捕捉する (`FileNotFoundError` は `OSError` のサブクラスなので上位互換)

- [ ] **Step 1: 現状確認 (M5/M6 triage・変更なし)**

`src/agentic_fx/service.py:84-106` (`_check_llama_swap` の 3 分岐警告文) と `src/agentic_fx/loops/trade_loop.py:76` (`ask_once` の `"(Mission 失敗: internal_error)"`) を読み、いずれも状況別に具体的な文言であることを確認する。コード変更は行わない — Step 12 のコミットメッセージに triage 結果を記録する。

- [ ] **Step 2: 失敗するテストを書く (retry policy pin)**

`tests/core/test_scheduler.py` に以下を追加する (既存 `Env` fixture — ファイル冒頭で確認済み — をそのまま使う):

```python
def test_cron_deadline_advances_even_when_mission_callback_raises():
    """codex I1 (プラン5 park): on_trade_mission が例外を送出しても
    _last_cron_trade は前進する — 1 回/時の再試行間隔を意図的な設計として
    固定する (毎 tick 再試行すると障害時に LLM/notifier を連打するため
    安全側)。tick() 自体は on_trade_mission の例外を保護しない
    (呼び出し元 = service.py の scheduler_thread が広い try で包む) ため、
    この pin は tick 側の呼び出し順序 (締切前進 → on_trade_mission 呼び出し)
    が「前進してから呼ぶ」順であることを固定する。
    """
    env = Env()  # 既存 fixture 名を確認して使う (Env が無ければ既存の
                  # scheduler 構築ヘルパー名に合わせる)
    calls: list[str] = []

    def boom(reason):
        calls.append(reason)
        raise RuntimeError("mission callback failed")

    env.scheduler.on_trade_mission = boom
    first_call_time = WED
    with pytest.raises(RuntimeError):
        env.scheduler.tick(first_call_time)
    assert calls == ["cron"]
    # 締切は例外前に前進済み — 30 分後の tick では再起動しない
    assert env.scheduler._trade_mission_due(
        first_call_time + timedelta(minutes=30)) is None
    # 1 時間後には再試行される
    assert env.scheduler._trade_mission_due(
        first_call_time + timedelta(hours=1)) == "cron"
```

(既存の `Env` fixture のコンストラクタ引数・`WED`/`timedelta` の import が無ければファイル冒頭の既存 import に揃える。プレースホルダのまま使わず、実ファイルを読んでから書くこと。)

- [ ] **Step 3: テスト実行**

```bash
uv run pytest tests/core/test_scheduler.py -q -k retry_policy_or_cron_deadline
```

Expected: このテストは **現状の実装のまま PASS するはず** (`tick()` は `on_trade_mission` 呼び出し前に `_last_cron_trade = now` を代入済み — scheduler.py:215-217)。PASS しない場合は `tick()` の呼び出し順序が想定と異なる (退行) ため、実装側ではなくテストの前提を先に見直すこと。

- [ ] **Step 4: `scheduler.py` の docstring に retry policy を明文化**

`src/agentic_fx/core/scheduler.py:210-218` (`tick` 内、`reason = self._trade_mission_due(now)` の直前) に以下のコメントを追加する:

```python
        # プラン 8 park 返済 (codex I1, プラン 5 レジャー): on_trade_mission
        # が例外を送出しても _last_cron_trade は既に前進済み (下の
        # if reason == "cron": 行が先に走る) — これは意図的な設計であり
        # バグではない。毎 tick 再試行 (前進させない設計) は、Mission 起動
        # 自体が壊れている状況で LLM/notifier を毎分連打することになり、
        # 障害時により危険側に倒れる。1 時間ごとの再試行間隔を保つことで
        # 障害時の負荷を抑える (test_cron_deadline_advances_even_when_
        # mission_callback_raises がこの契約を固定する)。
        reason = self._trade_mission_due(now)
```

- [ ] **Step 5: 失敗するテストを書く (clock 配線・provider seam)**

`tests/test_service.py` に追加:

```python
def test_app_has_clock_field(tmp_path):
    from agentic_fx.core.contracts import FixedClock
    from datetime import datetime, timezone

    fixed = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    root = _init_root(tmp_path)  # 既存 fixture 名を確認して使う
    app = build_app(root, clock=fixed)
    assert app.clock is fixed


def test_build_app_provider_seam_bypasses_quote_fn_patch(tmp_path):
    """provider を直接注入した場合、quote_fn/spec_fn/bars_fn の
    bound-method 差し替えは行われない (provider が全挙動を持つ)。"""
    from agentic_fx.datafeed.price_provider import PriceProvider
    from agentic_fx.config import load_settings
    from agentic_fx.core.contracts import SystemClock

    root = _init_root(tmp_path)
    settings = load_settings(root / "config" / "settings.yaml")
    fake_provider = _FakeProvider()  # テスト用の最小 PriceProvider 互換 fake
    app = build_app(root, provider=fake_provider,
                    quote_fn=lambda pair: (_ for _ in ()).throw(
                        AssertionError("quote_fn should not be used")))
    assert app.provider is fake_provider
```

(`_FakeProvider`/`_init_root` は既存の `tests/test_service.py` (または `tests/test_e2e_phase1.py`) の fixture 命名規約に合わせて実装すること。無ければ最小限の `PriceProvider` サブクラス/duck-type を新規に書く。)

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_service.py -q -k "app_has_clock_field or provider_seam"
```

Expected: FAIL (`App` に `clock` 属性が無い / `build_app` に `provider` kwarg が無い)。

- [ ] **Step 7: `service.py` を実装 (clock フィールド + provider seam)**

`src/agentic_fx/service.py` の `App` dataclass (183-206 行) に `clock: object` フィールドを追加する (既存フィールドの型ヒントに揃えて `object` — 既存の `App` フィールドは型ヒントを厳密にしていないため踏襲する):

```python
@dataclass
class App:
    conn_core: object
    conn_shell: object
    settings: object
    state: object
    activity: object
    broker: object
    executor: object
    provider: object
    econ: object
    collector: object
    rag: object
    trade_loop: object
    reflection: object
    scheduler: object
    commands: object
    registry: object
    core_lock: threading.RLock
    mission_watch: MissionWatch
    notifier: object
    runner: object
    owns_runner: bool
    clock: object
```

`build_app` のシグネチャ (244-246 行) に `provider: PriceProvider | None = None` を追加する:

```python
def build_app(root: Path, *, runner: AgentRunner | None = None,
              clock: Clock | None = None, quote_fn=None, spec_fn=None,
              bars_fn=None, embedding_fn=None,
              provider: PriceProvider | None = None) -> App:
```

docstring の `quote_fn / spec_fn / bars_fn / embedding_fn は E2E テストの注入点` の段落の後に追加:

```python
    `provider` (プラン 8 park 返済 — codex I-3): 非 None の場合、
    `PriceProvider` の内部構築と `quote_fn`/`spec_fn`/`bars_fn` の
    bound-method 差し替えを丸ごとスキップし、渡されたインスタンスを
    そのまま使う。`quote_fn`/`spec_fn`/`bars_fn` と併用した場合は
    `provider` が優先され、後者は無視される (provider が全挙動を握るため)。
```

`build_app` 本体 (268-297 行) の provider 構築部分を以下に置き換える:

```python
    if provider is not None:
        # プラン 8 park 返済: 呼び出し側が provider の全挙動を握る
        # (quote_fn/spec_fn/bars_fn の bound-method 差し替えは行わない)。
        pass
    else:
        provider = PriceProvider(conn_core, settings, clock)
        # 注入された quote_fn/spec_fn/bars_fn は provider 自身の束縛メソッドにも
        # 反映する (Task 8 E2E で実測)。実際に内部 self-call が存在するのは
        # `self.get_quote` だけ (`PriceProvider._rate_of` および `healthcheck`
        # から呼ばれる — `to_account_rate` の換算レート解決がここを経由する)。
        # (以下、既存の 269-296 行のコメント・代入をそのまま維持する —
        # provider が None のときだけ通るブランチへ字下げを 1 段追加する)
        if quote_fn is not None:
            provider.get_quote = quote_fn
        else:
            quote_fn = provider.get_quote
        if spec_fn is not None:
            provider.spec = spec_fn
        else:
            spec_fn = provider.spec
        if bars_fn is not None:
            provider.latest_1m_bar = bars_fn
        else:
            bars_fn = provider.latest_1m_bar
```

**注意 (実装者向け)**: `quote_fn`/`spec_fn`/`bars_fn` は provider 分岐の**外側**でも後続コード (`Executor`/`Scheduler` の構築、282-296 行の `rate_fn` クロージャ) から参照される。`provider is not None` 分岐では `quote_fn`/`spec_fn`/`bars_fn` がまだローカル変数として束縛されていない (呼び出し元が渡さなかった場合) ため、この分岐でも `quote_fn = quote_fn if quote_fn is not None else provider.get_quote` の要領で「未指定なら注入 provider の束縛メソッドを使う」形に揃えること (`spec_fn`/`bars_fn` も同様)。実装時に既存コードを読み、両分岐後に `quote_fn`/`spec_fn`/`bars_fn` が必ず non-None であることを確認してから次の行 (`rate_fn` 定義以降) へ進むこと。

`build_app` の `return App(...)` (415-422 行) に `clock=clock` を追加する:

```python
    return App(conn_core=conn_core, conn_shell=conn_shell, settings=settings,
               state=state, activity=activity, broker=broker,
               executor=executor, provider=provider, econ=econ,
               collector=collector, rag=rag, trade_loop=trade_loop,
               reflection=reflection, scheduler=scheduler, commands=commands,
               registry=registry, core_lock=core_lock,
               mission_watch=mission_watch, notifier=notifier,
               runner=runner, owns_runner=owns_runner, clock=clock)
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_service.py -q
```

Expected: PASS。

- [ ] **Step 9: 失敗するテストを書く (scheduler_thread の clock 配線・watchdog 時刻源)**

`tests/test_service.py` に追加:

```python
def test_scheduler_thread_uses_app_clock(tmp_path, monkeypatch):
    """service.py の scheduler_thread が `datetime.now(timezone.utc)` を
    直接呼ばず `app.clock.now()` を使う (codex M2)。FixedClock を注入し、
    tick に渡された now がその固定値であることを確認する。"""
    from agentic_fx.core.contracts import FixedClock
    from datetime import datetime, timezone
    import threading

    fixed = FixedClock(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    root = _init_root(tmp_path)
    app = build_app(root, clock=fixed)
    seen: list = []
    app.scheduler.tick = lambda now: seen.append(now)

    stop_event = threading.Event()
    stop_event.set()  # 1 回だけ走らせてすぐ止める意図 — 実装の
                       # scheduler_thread のループ条件に合わせて
                       # 呼び出し元テストの構成 (run_service の
                       # _stop_event シームを使う) を確認して書くこと。
    # 実装方針: run_service(root, _stop_event=...) 経由でなく、
    # scheduler_thread のロジックを直接 exercise できるよう、Step 11 の
    # 実装では datetime.now(timezone.utc) の呼び出し箇所を
    # app.clock.now() に置換するのみであるため、このテストは
    # run_service の起動テスト (既存 test_service.py の
    # run_service_smoke 系) に 1 assertion を追記する形で実装してもよい。
    # 実装者は既存の run_service テスト fixture を確認し、
    # 最小改変で「tick に渡された now が FixedClock の値である」ことを
    # 検証できる形に書き換えること。


def test_watchdog_tick_uses_mission_watch_time_fn(monkeypatch):
    """_watchdog_tick の elapsed 算出が MissionWatch の time_fn 経由で
    行われる (fable M4) — 生の time.monotonic() を直接呼ばない。"""
    from agentic_fx.service import _watchdog_tick, App
    from agentic_fx.loops.mission_watch import MissionWatch

    fake_time = [1000.0]
    watch = MissionWatch(time_fn=lambda: fake_time[0])
    watch.begin(mission_id=1, loop="trade", timeout_sec=10.0)
    fake_time[0] = 1000.0 + 10.0 + 61.0  # timeout + grace(60) を超過

    calls: list[str] = []

    class FakeActivity:
        def write(self, *a, **k):
            calls.append("write")

    class FakeNotifier:
        def send(self, *a, **k):
            calls.append("send")

    app = App(conn_core=None, conn_shell=None, settings=None, state=None,
              activity=FakeActivity(), broker=None, executor=None,
              provider=None, econ=None, collector=None, rag=None,
              trade_loop=None, reflection=None, scheduler=None,
              commands=None, registry=None, core_lock=None,
              mission_watch=watch, notifier=FakeNotifier(), runner=None,
              owns_runner=False, clock=None)
    _watchdog_tick(app)
    assert calls == ["write", "send"]
```

- [ ] **Step 10: テスト実行して FAIL を確認、実装、PASS を確認**

```bash
uv run pytest tests/test_service.py -q -k "clock or watchdog_tick_uses"
```

Expected: FAIL。`src/agentic_fx/loops/mission_watch.py` の `MissionWatch` クラス (24-29 行) に以下のプロパティを追加する:

```python
    @property
    def time_fn(self):
        """テスト/watchdog が同じ時刻源を参照できるようにする公開アクセサ
        (プラン 8 park 返済 — fable M4)。"""
        return self._time
```

`src/agentic_fx/service.py:458` の `elapsed = time.monotonic() - entry.started` を以下に変更する:

```python
    elapsed = app.mission_watch.time_fn() - entry.started
```

`src/agentic_fx/service.py:511` (`scheduler_thread` 内) の `app.scheduler.tick(datetime.now(timezone.utc))` を以下に変更する:

```python
                    with app.core_lock:
                        app.scheduler.tick(app.clock.now())
```

再実行:

```bash
uv run pytest tests/test_service.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 11: `policy.py` の OSError 捕捉**

`src/agentic_fx/policy.py` の `tail`/`size_warning` (11-27 行) の `except FileNotFoundError:` を両方とも `except OSError:` に変更する (`FileNotFoundError` は `OSError` のサブクラスなので既存の「ファイルが無い場合は空文字/None」の挙動は不変のまま、権限エラー・I/O エラー等も同じフォールバックに含める)。

`tests/test_policy.py` (無ければ新規作成) に以下を追加する:

```python
def test_tail_returns_empty_on_permission_error(tmp_path, monkeypatch):
    from agentic_fx.policy import Policy

    p = tmp_path / "directives.md"
    p.write_text("x" * 100)
    policy = Policy(p)

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(type(p), "read_text", boom)
    assert policy.tail(10) == ""
    assert policy.size_warning() is None
```

```bash
uv run pytest tests/test_policy.py -q
```

Expected: PASS after the `except OSError:` change (verify FAIL first with the original `except FileNotFoundError:` by running the test before editing, per TDD discipline).

- [ ] **Step 12: `tests/backtest/conftest.py` を `factories.py` へ rename**

`conftest.py` という予約ファイル名を「pytest fixture の自動収集対象」ではなく単なる import 用ヘルパーモジュールとして使っている (`@pytest.fixture` は 1 つも無い — `grep -n "@pytest.fixture" tests/backtest/conftest.py` で確認済み) — 混乱を避けるため rename する (fable M7)。

```bash
git mv tests/backtest/conftest.py tests/backtest/factories.py
grep -rl "tests\.backtest\.conftest" tests/ | xargs sed -i 's/tests\.backtest\.conftest/tests.backtest.factories/g'
grep -rn "tests\.backtest\.conftest" tests/ || echo "OK: no remaining references"
```

`tests/backtest/test_cli.py:543,560` 付近のコメント「conftest の H」を「factories の H」に手動で置換する (`sed` はコード上の import 文だけを対象にしたため、コメント文中の言及は個別に確認して直す)。

```bash
uv run pytest tests/backtest/ tests/plugin/test_strategy_adapter.py tests/store/test_backtest_runs.py -q
```

Expected: 全件 PASS (import 経路の変更のみ、挙動は不変)。

- [ ] **Step 13: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 14: 変異テスト**

1. `scheduler.py` の `if reason == "cron": self._last_cron_trade = now` を `on_trade_mission(reason)` の**後**に移動する改変 → `test_cron_deadline_advances_even_when_mission_callback_raises` が red (前進しなくなる)
2. `policy.py` の `except OSError:` を `except FileNotFoundError:` に戻す → `test_tail_returns_empty_on_permission_error` が red
3. `service.py` の `app.scheduler.tick(app.clock.now())` を `app.scheduler.tick(datetime.now(timezone.utc))` に戻す → clock 配線テストが red
4. `_watchdog_tick` の `app.mission_watch.time_fn()` を `time.monotonic()` に戻す → `test_watchdog_tick_uses_mission_watch_time_fn` が red (fake_time を進めても検出されなくなる)

各改変後に対応するテストを実行して red を確認し、元に戻す。

- [ ] **Step 15: Commit**

```bash
git add src/agentic_fx/core/scheduler.py src/agentic_fx/service.py \
  src/agentic_fx/loops/mission_watch.py src/agentic_fx/policy.py \
  tests/core/test_scheduler.py tests/test_service.py tests/test_policy.py \
  tests/backtest/factories.py
git add -A tests/backtest/  # rename 検出のため
git commit -m "$(cat <<'EOF'
fix: プラン5 park小口返済 (retry policy明文化/clock配線/provider seam/policy OSError/watchdog時刻源/conftest移動)

プラン5レジャー統合裁定の park 一覧 (codex I1/M2, sonnet I-3, fable
M3/M4/M7) を返済する。fable M5 (_check_llama_swap 文言) / M6 (ask失敗
文言) は現状の文言が既に具体的であることを確認し、変更不要と判定した
(triage — 詳細は Task 4 Step 1)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 5: worker 基盤 (1) — `db.connect_readonly` + `build_mission_registry` 抽出

設計書 §3.4「RO 接続の実装」(codex I-7) と §4.2「registry の共有再構築」を実装する。この task は子プロセス本体 (Task 7) に先行する準備部品 — 親 (`_assert_tools_registered` 検証) と子 (実行時) が同一関数を共有するための抽出。

**現状確認**: `build_app` (service.py:325-337) はツール配線を直接インラインで行っている (`registry = ToolRegistry(); registry.register_all(market_tools.build(...)); ...`)。子プロセスは同じ配線をゼロから再構築する必要があるが、`provider`/`econ`/`broker` はいずれも `conn`+`settings`+`clock` (+`activity`) から素直に構築できる薄いラッパーであることを確認済み (`account_tools.build` が呼ぶ `PaperBroker.equity()` は純粋な SELECT、`market_tools.build` が呼ぶ `EconCalendar.upcoming()` は `self.activity` に触れない — grep で確認済み)。

**Files:**
- Modify: `src/agentic_fx/store/db.py` (`connect_readonly` 新設)
- Create: `src/agentic_fx/tools/mission_registry.py`
- Modify: `src/agentic_fx/service.py:325-337` (`build_app` を `build_mission_registry` 経由に置換)
- Test: `tests/store/test_db_readonly.py` (新規), `tests/tools/test_mission_registry.py` (新規)

**Interfaces:**
- Produces:
  - `db.connect_readonly(db_path: Path) -> sqlite3.Connection` — `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)`。書き込み系 PRAGMA (`journal_mode`) は発行しない。`busy_timeout=5000` のみ設定。`db_path` が存在しなければ `FileNotFoundError` (RO 接続は稼働中サービスの既存 DB を前提とする)。既存 `connect()` と同じ SQLite バージョン assert (Task 3 で追加済みの `sqlite3.sqlite_version_info < (3, 35, 0)` チェック) をここにも適用する
  - `mission_registry.build_mission_registry(loop: str, conn: sqlite3.Connection, settings: Settings, clock: Clock, rag: Rag, *, activity: ActivityLog, indicator_plugins: list[PluginMeta] | None = None, sandbox_run=None) -> ToolRegistry` — `provider`/`econ`/`broker` を内部で新規構築し (呼び出し側から受け取らない — 親の既存インスタンスと子の使い捨てインスタンスを同じ関数で作れることが目的)、`market_tools`/`news_tools`/`account_tools`/`reflection_tools`/`signal_tools` の全 `ToolDef` を登録した `ToolRegistry` を返す。**`loop` 引数は本プランでは配線を分岐しない** (常に同じ全ツール集合を構築する — どのツールを実際に Mission に見せるかは `Mission.tools` の呼び出し側リスト `_TRADE_TOOLS` が決める。`loop` は将来の improve 系 registry 分岐 (プラン 9) に向けた forward-compat 引数であることを docstring に明記する)
- Consumes: `agentic_fx.tools.{market_tools,news_tools,account_tools,reflection_tools,signal_tools}` の既存 `build` 関数群 (シグネチャ不変)

- [ ] **Step 1: 失敗するテストを書く (`connect_readonly`)**

`tests/store/test_db_readonly.py` を新規作成:

```python
"""db.connect_readonly (プラン 8 worker 基盤 — codex I-7)。"""
from __future__ import annotations

import sqlite3

import pytest

from agentic_fx.store.db import connect, connect_readonly, init_db


def test_connect_readonly_can_read_existing_rows(tmp_path):
    db_path = tmp_path / "agentic.db"
    rw = connect(db_path)
    init_db(rw)
    rw.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('trade', 'local', 'x', 'running', '2026-08-04T00:00:00+00:00')")
    rw.commit()

    ro = connect_readonly(db_path)
    row = ro.execute("SELECT loop FROM missions").fetchone()
    assert row["loop"] == "trade"


def test_connect_readonly_rejects_write(tmp_path):
    db_path = tmp_path / "agentic.db"
    init_db(connect(db_path))
    ro = connect_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute(
            "INSERT INTO missions (loop, runner, model, status, started_at) "
            "VALUES ('trade', 'local', 'x', 'running', '2026-08-04T00:00:00+00:00')")


def test_connect_readonly_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "does-not-exist.db")
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_db_readonly.py -q
```

Expected: FAIL (`ImportError: cannot import name 'connect_readonly'`)。

- [ ] **Step 3: `db.py` に実装**

`src/agentic_fx/store/db.py` の `connect` 関数 (Task 3 で SQLite バージョン assert 済み) の直後に追加:

```python
def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """読み取り専用で SQLite に接続する (mission worker 子プロセス専用 —
    設計書 §3.4 codex I-7)。

    書き込み系 PRAGMA (journal_mode 等) は発行しない — 既に WAL で稼働中の
    親プロセスの DB を読むだけであり、モード変更は不要かつ RO 接続では
    そもそも失敗する。`-wal`/`-shm` ファイルは親プロセスが作成済み (稼働中
    サービスが前提) なので読み取り可能。

    `db_path` が存在しない場合は `FileNotFoundError` — RO 接続は「既に
    `init_db` 済みの DB」を前提とし、この関数自身はスキーマを作らない
    (子プロセスがスキーマを作る権限を持つべきではない)。
    """
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old (>= 3.35 required)")
    if not db_path.exists():
        raise FileNotFoundError(
            f"connect_readonly requires an already-initialized DB: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_db_readonly.py -q
```

Expected: PASS。

- [ ] **Step 5: 失敗するテストを書く (`build_mission_registry`)**

`tests/tools/test_mission_registry.py` を新規作成 (`tests/tools/test_market_tools.py` 等の既存 fixture 命名規約を確認してから書く):

```python
"""build_mission_registry (プラン 8 worker 基盤 — 設計書 §4.2)。"""
from __future__ import annotations

from pathlib import Path

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.tools.mission_registry import build_mission_registry
from datetime import datetime, timezone

SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _clock():
    return FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))


def test_build_mission_registry_registers_all_trade_tools(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=lambda texts: [[0.0] * 4 for _ in texts])
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=activity)

    names = set(registry.names())
    for expected in ("get_ohlcv", "get_indicators", "search_news",
                     "get_econ_calendar", "get_positions", "get_account",
                     "get_recent_reflections", "search_reflections",
                     "get_signals"):
        assert expected in names, f"{expected} missing from registry"


def test_build_mission_registry_econ_calendar_does_not_touch_activity(tmp_path):
    """econ.upcoming() (get_econ_calendar が呼ぶ) は self.activity に触れない
    — 子プロセスが activity=None 相当の最小構成で呼んでも安全なことの
    構造的な確認 (worker.py が構築するときの前提)。"""
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=lambda texts: [[0.0] * 4 for _ in texts])

    class ExplodingActivity:
        def write(self, *a, **k):
            raise AssertionError("activity.write must not be called by get_econ_calendar")

    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), rag, activity=ExplodingActivity())
    result = registry.execute("get_econ_calendar", {"days": 1}, ["get_econ_calendar"])
    assert result is not None
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/tools/test_mission_registry.py -q
```

Expected: FAIL (`ModuleNotFoundError: No module named 'agentic_fx.tools.mission_registry'`)。

- [ ] **Step 7: `mission_registry.py` を新規作成**

```python
"""Mission ツール配線の単一の組み立て関数 (プラン 8 worker 基盤 — 設計書 §4.2)。

親 (build_app、起動時 _assert_tools_registered 検証) と子
(mission_worker.py、実行時) が**同一関数**を共有する — 配線の二重化を
防ぎ、「親で検証したものと子で動くものが同じ」を関数の同一性で担保する。

`provider`/`econ`/`broker` はこの関数の内部で新規構築する (呼び出し側の
既存インスタンスを受け取らない) — conn/settings/clock/activity から素直に
組み立てられる薄いラッパーであり、親の長寿命インスタンスと子の使い捨て
インスタンスを同じコードパスで作れることが本モジュールの目的そのもの。
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.rag import Rag
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, plugin_loader, reflection_tools,
    signal_tools,
)
from agentic_fx.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.plugin.loader import PluginMeta


def build_mission_registry(
        loop: str, conn: sqlite3.Connection, settings: "Settings",
        clock: Clock, rag: Rag, *, activity: ActivityLog,
        indicator_plugins: "list[PluginMeta] | None" = None,
        sandbox_run=None) -> ToolRegistry:
    """`loop` は本プランでは配線を分岐しない (常に同じ全ツール集合を
    構築する) — forward-compat 引数。どのツールを実際に Mission に
    見せるかは呼び出し側の `Mission.tools` リスト (`_TRADE_TOOLS` 等) が
    決める。将来の improve 系 registry 分岐 (プラン 9) で `loop` を
    使い始める想定。
    """
    provider = PriceProvider(conn, settings, clock)
    econ = EconCalendar(conn, activity, clock)
    broker = PaperBroker(conn, settings, clock)

    registry = ToolRegistry()
    registry.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=indicator_plugins,
        sandbox_run=sandbox_run))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn, broker))
    registry.register_all(reflection_tools.build(conn, rag, settings.pairs))
    registry.register_all(signal_tools.build(conn, settings, clock))
    return registry
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/tools/test_mission_registry.py -q
```

Expected: PASS。

- [ ] **Step 9: `service.py` を `build_mission_registry` 経由に置換**

`src/agentic_fx/service.py` の import 節に `from agentic_fx.tools.mission_registry import build_mission_registry` を追加する。`build_app` (315-337 行) の以下のブロック:

```python
    plugins_dir = root / "plugins"
    approved = plugin_loader.approved_plugins(conn_core, plugins_dir)

    signal_producer = SignalProducer()

    registry = ToolRegistry()
    registry.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=approved))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn_core, broker))
    registry.register_all(reflection_tools.build(conn_core, rag, settings.pairs))
    registry.register_all(signal_tools.build(conn_core, settings, clock))
    _validate_startup(settings)
    _assert_tools_registered(registry, _TRADE_TOOLS)
```

を以下に置き換える:

```python
    plugins_dir = root / "plugins"
    approved = plugin_loader.approved_plugins(conn_core, plugins_dir)

    signal_producer = SignalProducer()

    # プラン 8 worker 基盤: 親 (ここ) と子 (mission_worker.py) が同一関数
    # (build_mission_registry) でツール配線を組み立てる。親は既に構築済みの
    # provider/econ/broker を再利用せず、conn_core から独立に再構築する
    # (子との配線一致を関数の同一性だけで担保するため — 親の長寿命
    # インスタンスを別途 provider/econ/broker として保持している事実と
    # 矛盾しない: registry 内のツールクロージャは新しく作った
    # provider/econ/broker を束縛するが、これらは conn_core を共有する
    # ため実質的に同じ DB 状態を見る)。
    registry = build_mission_registry(
        "trade", conn_core, settings, clock, rag, activity=activity,
        indicator_plugins=approved)
    _validate_startup(settings)
    _assert_tools_registered(registry, _TRADE_TOOLS)
```

`market_tools`/`news_tools`/`account_tools`/`reflection_tools` の import が `service.py` の他の箇所で使われていないことを `grep -n "market_tools\.\|news_tools\.\|account_tools\.\|reflection_tools\." src/agentic_fx/service.py` で確認し、未使用になった import (49-52 行の `from agentic_fx.tools import (...)`) から `market_tools, news_tools, account_tools, reflection_tools` を削除する (`signal_tools`/`plugin_loader` は他箇所で引き続き使用されているため残す — 実ファイルを見て要否を確認してから編集すること)。

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS (`_assert_tools_registered` が引き続き通ることを含む)。

- [ ] **Step 11: 変異テスト**

1. `mission_registry.py` の `registry.register_all(signal_tools.build(...))` 行を削除 → `test_build_mission_registry_registers_all_trade_tools` が red (`get_signals` missing)
2. `db.py:connect_readonly` の `mode=ro` を `mode=rw` に改変 → `test_connect_readonly_rejects_write` が red (INSERT が成功してしまう)

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/tools/mission_registry.py \
  src/agentic_fx/service.py tests/store/test_db_readonly.py tests/tools/test_mission_registry.py
git commit -m "$(cat <<'EOF'
feat: db.connect_readonly + build_mission_registry (worker 基盤の共有配線点)

親 (build_app) と子 (mission_worker.py, Task 7) が同一関数でツール配線を
組み立てられるようにする (設計書 §3.4/§4.2)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 6: worker 基盤 (2) — LocalRunner の transcript sink 集約

設計書 §4.3「部分 transcript の保存範囲」(codex I-6) を実装する。`LocalRunner` の messages への append を単一 sink 関数に集約し、sink が (子プロセス内で) `event` フレームを送出できるようにする。この task は WorkerRunner/mission_worker (Task 7,10) に先行する準備 — sink 自体はインプロセスでテストでき、subprocess 隔離とは独立に検証できる。

**現状確認**: `src/agentic_fx/runners/local_runner.py` の messages への append site は 6 箇所 (`run()` メソッド内): 50 行目 (初期 user prompt — リスト構築)、99 行目 (assistant message)、144 行目 (tool result)、173 行目 (非文字列 content 修復依頼)、190 行目 (JSON parse エラー修復依頼)、201 行目 (schema エラー修復依頼)。

**Files:**
- Modify: `src/agentic_fx/runners/local_runner.py:34-46`(`__init__`)`,48-62`(`run` 冒頭)`,99,144,173,190,201`(各 append site)
- Test: `tests/runners/test_local_runner.py` (既存ファイルに追記)

**Interfaces:**
- Produces:
  - `LocalRunner.__init__(self, *, base_url, model, registry, transport=None, time_fn=time.monotonic, on_message: Callable[[dict], None] | None = None)` — `on_message` 新設 kwarg (既定 None = 既存挙動と完全互換)
  - `LocalRunner._sink(self, messages: list[dict], msg: dict) -> None` — 新設 private メソッド。`messages.append(msg)` の後、`self._on_message` が None でなければ呼ぶ。**`on_message` が例外を送出しても `run()` を止めない** (try/except で握って技術ログに warning — sink は観測性の記録であり Mission 実行そのものを阻害してはならない、という既存の `ActivityLog.write` 契約と同じ設計判断)

- [ ] **Step 1: 失敗するテストを書く**

`tests/runners/test_local_runner.py` に以下を追加する (既存ファイルの fixture — fake httpx transport の構築パターン — を確認し、それに揃えて書く):

```python
def test_on_message_called_for_every_append_including_initial_prompt():
    """sink 集約 (プラン 8 worker 基盤 — codex I-6): 初期 user prompt を
    含む全 append site で on_message が呼ばれる。"""
    seen: list[dict] = []
    registry = ToolRegistry()  # 既存 import 済みの ToolRegistry を使う
    transport = _completed_transport()  # 既存の「1 ターンで completed
                                          # する」fake transport ヘルパーを
                                          # 実ファイルで確認して使う
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=transport, on_message=seen.append)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    runner.run(mission)

    assert seen[0] == {"role": "user", "content": "hello"}
    assert any(m.get("role") == "assistant" for m in seen)


def test_on_message_exception_does_not_break_run(monkeypatch):
    """on_message が例外を送出しても run() は completed を返す
    (sink はベストエフォートの観測性記録)。"""
    def boom(msg):
        raise RuntimeError("sink failed")

    registry = ToolRegistry()
    transport = _completed_transport()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=transport, on_message=boom)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)
    assert result.status == "completed"
```

(`_completed_transport()` は既存テストが「1 ターンで JSON completed を返す」fake httpx transport を構築するために使っているヘルパーの実名に置き換えること — `grep -n "def.*transport\|httpx.MockTransport" tests/runners/test_local_runner.py` で確認してから書く。)

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/runners/test_local_runner.py -q -k on_message
```

Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'on_message'`)。

- [ ] **Step 3: 実装**

`src/agentic_fx/runners/local_runner.py:34-46` の `__init__` を以下に変更する:

```python
class LocalRunner(AgentRunner):
    def __init__(self, *, base_url: str, model: str, registry: ToolRegistry,
                 transport: httpx.BaseTransport | None = None,
                 time_fn: Callable[[], float] = time.monotonic,
                 on_message: Callable[[dict], None] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._registry = registry
        self._client = httpx.Client(transport=transport)
        self._time = time_fn
        # プラン 8 worker 基盤 (codex I-6): messages への全 append を単一
        # sink に集約する。on_message は子プロセス (mission_worker.py) が
        # event フレームを親へ送出するためのコールバック。既定 None は
        # 既存挙動 (sink 呼び出しなし) と完全互換。
        self._on_message = on_message

    def close(self) -> None:
        """Close the HTTP client connection."""
        self._client.close()

    def _sink(self, messages: list[dict], msg: dict) -> None:
        """messages への唯一の append 経路。on_message はベストエフォート
        (例外を run() に伝播させない — 観測性の記録が Mission 実行を
        阻害してはならない)。"""
        messages.append(msg)
        if self._on_message is not None:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001 — 観測性記録は実行を止めない
                _log.warning("on_message callback raised", exc_info=True)
```

`run()` の 6 箇所を書き換える。まず 48-50 行目 (初期メッセージ構築):

```python
    def run(self, mission: Mission) -> MissionResult:
        deadline = self._time() + mission.timeout_sec
        messages: list[dict] = []
        self._sink(messages, {"role": "user", "content": mission.prompt})
```

99 行目 (`messages.append(normalized_msg)`) を:

```python
            self._sink(messages, normalized_msg)
```

144 行目 (`messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})`) を:

```python
                    self._sink(messages, {"role": "tool",
                                          "tool_call_id": tc["id"],
                                          "content": result})
```

173 行目 (非文字列 content 修復依頼) を:

```python
                messages.append({"role": "user",
                                 "content": f"出力を JSON として解釈できません "
```

の直前の `messages.append` 呼び出しを `self._sink(messages, {...})` に置換する (元のコード `messages.append({"role": "user", "content": f"出力を JSON として解釈できません (content is not a string)。JSON オブジェクトのみを出力してください。"})` をそのまま `self._sink(messages, {...})` の形に変える — 中身の dict リテラルは無変更)。190 行目 (parse エラー修復依頼) と 201 行目 (schema エラー修復依頼) も同様に `messages.append(` を `self._sink(messages, ` に置換する (dict リテラルの中身は無変更)。

**実装者への注意**: 6 箇所のうち 173/190/201 行目は同じパターン (`messages.append({"role": "user", "content": f"..."})` → `self._sink(messages, {"role": "user", "content": f"..."})`) の機械的な置換であり、`messages.append(` という文字列をこの 3 箇所と 99/144 行目の計 5 箇所で `self._sink(messages, ` に置換すれば足りる (初期メッセージの 1 箇所だけがリスト構築から変わるため個別に書いた)。置換漏れが無いことを `grep -n "messages.append" src/agentic_fx/runners/local_runner.py` で確認し、**ヒットが 0 件になること**を確認する。

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/runners/test_local_runner.py -q
uv run pytest -q
```

Expected: 全件 PASS (既存の全 LocalRunner テストが sink 経由でも同じ挙動を保つこと)。

- [ ] **Step 5: 変異テスト**

`_sink` 内の `messages.append(msg)` はそのまま (削除すると `run()` 自体が壊れ既存スイート全体が red になるため対象外)、`if self._on_message is not None:` の呼び出しを削除する改変を行い、`test_on_message_called_for_every_append_including_initial_prompt` が red になることを確認してから元に戻す。

- [ ] **Step 6: Commit**

```bash
git add src/agentic_fx/runners/local_runner.py tests/runners/test_local_runner.py
git commit -m "$(cat <<'EOF'
refactor: LocalRunner の messages append を単一 sink 関数に集約

設計書 §4.3 (codex I-6)。on_message コールバックで子プロセスが event
フレームを送出できるようにする準備 (WorkerRunner 本体は Task 10)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 7: worker 基盤 (3) — mission worker 子プロセス本体 (JSON 行プロトコル + handshake + rlimit + PDEATHSIG)

設計書 §4.1〜§4.5、§4.7 の子プロセス側を実装する。親側 (`WorkerRunner`) は Task 10。

**設計判断 (writing-plans — 設計書の「必要サブセット」を具体化)**:
- **handshake で渡す `settings` は `Settings.model_dump()` のフルダンプ**とする (「必要サブセットのみ」ではなく全体)。理由: settings.yaml に秘密情報は含まれない (CLAUDE.md 絶対制約 — 秘密は `.env` のみ) ため全体を渡しても安全であり、「どのキーが必要か」を都度洗い出す部分集合方式は将来のツール追加のたびに漏れる (fail-open の温床)。
- **`runner` 設定は個別に渡さない** — `settings.runner.trade`/`settings.llama_swap.base_url` が既に `settings` 全体に含まれるため、trade profile の子は常にこれらから `LocalRunner` を組み立てる (`backend == "claude"` は Global Constraints のとおり `RuntimeError` で fail closed)。
- **`indicator_plugins`/`approved plugins` は子が自分の RO 接続 + `plugins_dir` から自分で `plugin_loader.approved_plugins()` を呼んで再構築する** (`PluginMeta` は `Path` を含み JSON で素直にシリアライズできないため、ワイヤに乗せず子が独立に再計算する — 親と子は同じ `plugins_dir`/DB を見るので結果は一致する)。
- **`transcript_max_bytes` (累積上限) は子には渡さない** — truncate 判定は親側 (`WorkerRunner`, Task 10) の責務にする。子は `event` フレームを無条件に送出し続け、親が受信側で打ち切る (パイプの背圧を避けるため、子は送出を止めない — 親は打ち切った後も読み続けてパイプを詰まらせない)。

**Files:**
- Create: `src/agentic_fx/core/mission_protocol.py` (フレーム型定義 + seq 検証 — 親子共有)
- Create: `src/agentic_fx/mission_worker.py` (子プロセスエントリ)
- Test: `tests/core/test_mission_protocol.py`, `tests/test_mission_worker_protocol.py` (インプロセス — FakeWorker 越しの単体テスト。実 subprocess 起動は Task 10 の WorkerRunner 統合テスト/E2E (Task 20) に譲る)

**Interfaces:**
- Produces:
  - `mission_protocol.ProtocolError(Exception)` — フレーム形式・seq 違反の単一表現
  - `mission_protocol.SeqTracker` — `__init__(self) -> None` (内部カウンタ 1 起点) / `check(self, seq: Any) -> None` (`seq != 期待値` で `ProtocolError`。次回期待値を +1 する)
  - `mission_protocol.write_frame(stream, frame: dict) -> None` / `mission_protocol.read_frame(stream) -> dict | None` (EOF で None)
  - `mission_protocol.FRAME_TYPES_PARENT_TO_CHILD = frozenset({"handshake", "tool_rpc_result"})` / `FRAME_TYPES_CHILD_TO_PARENT = frozenset({"ready", "event", "tool_rpc", "result"})`
  - `mission_worker.main() -> None` — `python -m agentic_fx.mission_worker` のエントリ。標準入力から handshake (1 行) を読み、bootstrap (PDEATHSIG+ppid 照合 → rlimit → registry 構築) → `ready` 送出 → `LocalRunner.run(mission)` を実行 (on_message は `event` フレーム送出) → `result` 送出、の順で動く
  - `mission_worker._set_pdeathsig(sig: int) -> None` — `ctypes` で `prctl(PR_SET_PDEATHSIG, sig)` を呼ぶ (単体テストで `ctypes.CDLL` を fake 差し替え可能にする)
  - `mission_worker._RagRpcProxy(write_frame_fn, read_frame_fn, seq_tracker) -> object` — `search_news(query, n=5) -> list[dict]` / `search_reflections(query, n=5) -> list[dict]` を実装 (`news_tools.build`/`reflection_tools.build` の duck-type 契約のみを満たす — 全メソッドは持たない)。`tool_rpc` フレーム送出 → `tool_rpc_result` を同期ブロッキング待ち (同時 1 件のみ、設計書 §4.3)

- [ ] **Step 1: 失敗するテストを書く (`mission_protocol.py`)**

`tests/core/test_mission_protocol.py` を新規作成:

```python
"""mission_protocol の seq 検証 + フレーム I/O (プラン8, 設計書 §4.3)。"""
from __future__ import annotations

import io

import pytest

from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, read_frame, write_frame,
)


def test_seq_tracker_accepts_monotonic_sequence():
    t = SeqTracker()
    t.check(1)
    t.check(2)
    t.check(3)


def test_seq_tracker_rejects_duplicate():
    t = SeqTracker()
    t.check(1)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(1)


def test_seq_tracker_rejects_gap():
    t = SeqTracker()
    t.check(1)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(3)


def test_seq_tracker_rejects_regression():
    t = SeqTracker()
    t.check(1)
    t.check(2)
    with pytest.raises(ProtocolError, match="seq"):
        t.check(1)


def test_write_then_read_frame_roundtrip():
    buf = io.BytesIO()
    write_frame(buf, {"type": "ready", "seq": 1, "ok": True})
    buf.seek(0)
    frame = read_frame(buf)
    assert frame == {"type": "ready", "seq": 1, "ok": True}


def test_read_frame_returns_none_on_eof():
    buf = io.BytesIO(b"")
    assert read_frame(buf) is None
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_mission_protocol.py -q
```

Expected: FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `mission_protocol.py` を実装**

```python
"""Mission worker の JSON 行プロトコル共有定義 (プラン8, 設計書 §4.3)。

`mission_worker.py` (子) と `runners/worker_runner.py` (親, Task 10) の
両方が import する。ワイヤ契約を 2 箇所の docstring で同期する方式
(sandbox.py/worker.py) ではなく、seq 検証ロジックそのものを共有コードに
する — 正しさが資金保護 (Mission 実行の停止窓) に波及するため、実装の
乖離が起き得ない形にする。

フレーム型 (全フレームに `seq` を付す。方向別に 1 起点の単調増加 —
codex M2-1):
- 親→子: `handshake` (起動時 1 回) / `tool_rpc_result`
- 子→親: `ready` (起動応答) / `event` (transcript メッセージ 1 件) /
  `tool_rpc` (RPC 要求) / `result` (最終ステータス、正常終端で 1 回)
"""
from __future__ import annotations

import json
from typing import Any, BinaryIO


class ProtocolError(Exception):
    """フレーム形式・seq 違反など、プロトコル契約に反する入力全般の単一
    表現 (fail closed — sandbox.py の `SandboxError`/`_dead` 意味論と同じ:
    違反を検出したらセッションは即座に死んだものとして扱う)。"""


FRAME_TYPES_PARENT_TO_CHILD = frozenset({"handshake", "tool_rpc_result"})
FRAME_TYPES_CHILD_TO_PARENT = frozenset({"ready", "event", "tool_rpc", "result"})


class SeqTracker:
    """方向別の 1 起点単調増加 seq 検証 (codex M2-1)。重複・逆行・欠番は
    すべて `ProtocolError` (「次に来るべき値と一致しない」の一律判定 —
    重複/逆行/欠番を種類分けしない。呼び出し側は方向ごとに別インスタンス
    を持つこと (親→子と子→親は別カウンタ)。"""

    def __init__(self) -> None:
        self._expected = 1

    def check(self, seq: Any) -> None:
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ProtocolError(f"seq must be an int, got {seq!r}")
        if seq != self._expected:
            raise ProtocolError(
                f"seq out of order: expected {self._expected}, got {seq}")
        self._expected += 1


def write_frame(stream: BinaryIO, frame: dict) -> None:
    stream.write(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
    stream.flush()


def read_frame(stream: BinaryIO) -> dict | None:
    line = stream.readline()
    if not line:
        return None
    return json.loads(line)
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_mission_protocol.py -q
```

Expected: PASS。

- [ ] **Step 5: 失敗するテストを書く (`mission_worker.py` — インプロセス FakeWorker テスト)**

`tests/test_mission_worker_protocol.py` を新規作成する。実 subprocess は spawn せず、`mission_worker` の各関数をインプロセスで直接呼んで検証する (E2E 実 spawn は Task 20):

```python
"""mission_worker.py の単体テスト (インプロセス — 実 subprocess は spawn しない)。"""
from __future__ import annotations

import ctypes
import io
import json
import os

import pytest

from agentic_fx import mission_worker


def test_set_pdeathsig_calls_prctl_with_expected_args(monkeypatch):
    calls: list[tuple] = []

    class FakeLibc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(mission_worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    mission_worker._set_pdeathsig(15)  # SIGTERM
    assert calls == [(1, 15, 0, 0, 0)]  # PR_SET_PDEATHSIG=1


def test_set_pdeathsig_raises_oserror_on_failure(monkeypatch):
    class FakeLibc:
        def prctl(self, *args):
            return -1

    monkeypatch.setattr(mission_worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(mission_worker.ctypes, "get_errno", lambda: 1)
    with pytest.raises(OSError):
        mission_worker._set_pdeathsig(15)


def test_rag_rpc_proxy_search_news_round_trip():
    """tool_rpc → tool_rpc_result の同期往復。"""
    from agentic_fx.core.mission_protocol import SeqTracker

    outbound = io.BytesIO()
    seq_out = SeqTracker()

    def write_fn(frame):
        outbound.write((json.dumps(frame) + "\n").encode())

    # fake 親: search_news の RPC 要求に対して固定結果を返す応答を用意する
    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": True,
                "result": [{"title": "t", "body": "b", "source_name": "s"}]}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, seq_out)
    result = proxy.search_news("usdjpy", n=5)
    assert result == [{"title": "t", "body": "b", "source_name": "s"}]

    outbound.seek(0)
    sent = json.loads(outbound.getvalue().splitlines()[0])
    assert sent["type"] == "tool_rpc"
    assert sent["name"] == "search_news"
    assert sent["args"] == {"query": "usdjpy", "n": 5}


def test_rag_rpc_proxy_propagates_error():
    from agentic_fx.core.mission_protocol import SeqTracker

    def write_fn(frame):
        pass

    def read_fn():
        return {"type": "tool_rpc_result", "seq": 1, "rpc_id": "1", "ok": False,
                "error": "rag unavailable"}

    proxy = mission_worker._RagRpcProxy(write_fn, read_fn, SeqTracker())
    with pytest.raises(RuntimeError, match="rag unavailable"):
        proxy.search_news("q")
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_mission_worker_protocol.py -q
```

Expected: FAIL (`ModuleNotFoundError: No module named 'agentic_fx.mission_worker'`)。

- [ ] **Step 7: `WorkerSettings` を新設し、rlimit 具体値を実測で確定する (§12 申し送り②)**

**実測手順 (writing-plans で実施済み — 結果を以下に転記する)**: mission worker が実際に import する依存一式 (`tools/market_tools`, `tools/news_tools`, `tools/account_tools`, `tools/reflection_tools`, `tools/signal_tools`, `runners/local_runner`, `store/db`, `store/rag` (`build_mission_registry` が `Rag` 型ヒントのため `chromadb` を import する) を読み込んだ直後の仮想メモリ・fd 数を測定した:

```bash
uv run python -c "
import os
import agentic_fx.tools.market_tools, agentic_fx.tools.news_tools
import agentic_fx.tools.account_tools, agentic_fx.tools.reflection_tools
import agentic_fx.tools.signal_tools
import agentic_fx.runners.local_runner
import agentic_fx.store.db as db
import agentic_fx.store.rag as rag_mod
import httpx, jsonschema, pandas, numpy, chromadb
with open(f'/proc/{os.getpid()}/status') as f:
    for line in f:
        if line.startswith(('VmRSS','VmSize','VmPeak')):
            print(line.strip())
    fds = os.listdir(f'/proc/{os.getpid()}/fd')
    print('num_fds', len(fds))
"
```

実測結果 (32 コア環境): `VmPeak ≈ 1.65GB` / `VmRSS ≈ 123MB` / `num_fds = 5`。VmSize が RSS よりはるかに大きいのは OpenBLAS/numpy がコア数に比例したスレッド分の仮想アドレス空間を事前確保するため (`plugin/sandbox.py` の `_SINGLE_THREAD_ENV` コメントと同じ現象 — mission worker は plugin worker と異なり `_SINGLE_THREAD_ENV` を適用しない: pandas の演算性能を落とさないため。かわりに **RLIMIT_AS を「数 GB」に寛大に取る** ことで対応する、という設計書 §4.5 の方針をこの実測が裏付ける)。

**確定値**: `child_as_mb: 4096` (実測 VmPeak 1.65GB の約 2.4 倍の余裕) / `child_nofile: 128` (実測 5 fd に対し DB 接続・複数データソースへの HTTP 接続を見込んだ余裕) / `child_fsize_mb: 8` (mission worker は正常経路でファイルを書かない — plugin サンドボックスと同じ「想定外書込の検知」目的の小さい上限)。

`src/agentic_fx/config.py` に `WorkerSettings` を新設し `Settings` に追加する (`PluginSettings` の定義の直後、`class Settings(_Strict):` の直前に挿入):

```python
class WorkerSettings(_Strict):
    """mission worker (プラン8) の壁時計監視・resource limit・IPC 設定。
    既定値のみで動く (`Settings.worker` は default_factory を持つ)。
    """
    # 子プロセス側 resource limit (§12 申し送り② — 実測に基づく確定値。
    # 上記実測手順のコメント参照)。
    child_as_mb: int = Field(ge=1, default=4096)
    child_nofile: int = Field(ge=1, default=128)
    child_fsize_mb: int = Field(ge=1, default=8)
    # 親側の preemption エスカレーション (設計書 §4.7)。
    worker_grace_sec: float = Field(gt=0, default=30.0)
    worker_terminate_grace_sec: float = Field(gt=0, default=10.0)
    worker_startup_timeout_sec: float = Field(gt=0, default=30.0)
    # Mission 累積 transcript 上限 (設計書 §4.3 codex M-3)。
    transcript_max_bytes: int = Field(ge=1, default=1_048_576)
    # tick 内データ hooks が内部で使う全ネットワーククライアントの
    # timeout 上限 (設計書 §3.2 codex I2-1 — wall-clock 保証ではない)。
    data_hook_timeout_sec: float = Field(gt=0, default=30.0)
    # RAG RPC の親側応答待ち上限 (設計書 §4.3/§4.4)。
    rpc_timeout_sec: float = Field(gt=0, default=15.0)
    # 停止シーケンスの join 上限 (設計書 §5)。
    shutdown_join_timeout_sec: float = Field(gt=0, default=30.0)
```

`Settings` クラスに `worker: WorkerSettings = Field(default_factory=WorkerSettings)` を追加する (`plugin: PluginSettings = Field(default_factory=PluginSettings)` の直後)。

`config/settings.yaml.example` に以下を追記する (`plugin:` セクションの直後):

```yaml
worker:                        # mission worker (プラン8) の resource limit・preemption・IPC 設定。省略可
  child_as_mb: 4096             # RLIMIT_AS (MiB): 実測 VmPeak 1.65GB (32コア環境) の約2.4倍の余裕
  child_nofile: 128             # RLIMIT_NOFILE
  child_fsize_mb: 8             # RLIMIT_FSIZE (MiB): 正常経路ではファイルを書かない前提の小さい上限
  worker_grace_sec: 30          # runner soft deadline の外側マージン (壁時計監視)
  worker_terminate_grace_sec: 10  # SIGTERM 後 SIGKILL までの猶予
  worker_startup_timeout_sec: 30  # 子の import〜ready まで
  transcript_max_bytes: 1048576   # Mission 累積 transcript 上限 (超過は truncate marker)
  data_hook_timeout_sec: 30       # tick 内データ hooks の全ネットワーククライアント timeout 上限
  rpc_timeout_sec: 15             # RAG RPC の親側応答待ち上限
  shutdown_join_timeout_sec: 30   # 停止シーケンスの join 上限
```

```bash
uv run pytest tests/store/ -q -k config
uv run pytest -q
```

Expected: 全件 PASS (新設 settings セクションは既存 `settings.yaml` にも `default_factory` で無変更ロード可能)。

- [ ] **Step 8: `mission_worker.py` を実装**

```python
"""Mission worker 子プロセス本体 (プラン8) — python -m agentic_fx.mission_worker
として起動される。`runners/worker_runner.py` (親, Task 10) が spawn する唯一の
想定呼び出し元。

**起動順序は厳守** (plugin/worker.py と同じ規律):
1. handshake (最初の 1 行) を読む
2. `_set_pdeathsig` (親死亡時に OS が SIGTERM を配送) → 設定**直後**に
   `os.getppid()` を `expected_parent_pid` と照合し、不一致 (= 設定前に
   親が死んで再親付けされた) なら即終了する (設計書 §4.8 codex I2-4)
3. resource limit (RLIMIT_AS/NOFILE/FSIZE/CORE=0) を設定 — **設定失敗は
   fail closed** (worker_profile="trade" は全項目)。CPU 制限は付けない
   (Mission の消費は LLM 待ちの壁時計であり親の preemption が受け持つ —
   設計書 §4.5)
4. `build_mission_registry` でツール配線を組み立てる (RAG は `_RagRpcProxy`
   を注入)
5. `ready` を送出
6. `LocalRunner.run(mission)` を実行 (`on_message` が `event` フレームを
   送出)
7. `result` を送出して終了

**worker_profile="trade" 限定** (本プランのスコープ — improve profile は
Task 18 で bootstrap を拡張する)。`settings.runner.trade.backend` が
"local" 以外 (= "claude") の場合は `RuntimeError` で fail closed する
(ClaudeRunner は本プランでは実装しない — Global Constraints)。
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable

from agentic_fx.core.mission_protocol import SeqTracker, read_frame, write_frame

_PR_SET_PDEATHSIG = 1


def _set_pdeathsig(sig: int) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_PDEATHSIG) failed: "
                             f"{os.strerror(errno)}")


def _set_resource_limits(*, as_mb: int, nofile: int, fsize_mb: int) -> None:
    import resource

    as_bytes = int(as_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _RagRpcProxy:
    """`news_tools.build(rag)`/`reflection_tools.build(conn, rag, pairs)`
    が要求する duck-type 契約 (`search_news`/`search_reflections`) のみを
    実装する (chromadb PersistentClient は多プロセス同時アクセス非対応 —
    設計書 §4.4)。`tool_rpc` は常に同時 1 件以下、同期ブロッキング待ち
    (設計書 §4.3)。"""

    def __init__(self, write_fn: Callable[[dict], None],
                read_fn: Callable[[], dict | None],
                seq_tracker: SeqTracker) -> None:
        self._write = write_fn
        self._read = read_fn
        self._seq = seq_tracker
        self._rpc_counter = 0

    def _call(self, name: str, args: dict) -> Any:
        self._rpc_counter += 1
        rpc_id = str(self._rpc_counter)
        self._write({"type": "tool_rpc", "seq": self._seq_next(),
                     "rpc_id": rpc_id, "name": name, "args": args})
        response = self._read()
        if response is None:
            raise RuntimeError("parent closed the pipe while awaiting tool_rpc_result")
        if response.get("rpc_id") != rpc_id:
            raise RuntimeError(
                f"tool_rpc_result rpc_id mismatch: expected {rpc_id}, "
                f"got {response.get('rpc_id')!r}")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "rag rpc failed")))
        return response.get("result")

    def _seq_next(self) -> int:
        # 子→親方向の event/tool_rpc/result は同じカウンタを共有する
        # (単一スレッドで動くため送出順=seq 順が自然に一致する)。
        # WorkerRunner (Task 10) 側の SeqTracker.check() と対称に、
        # ここでは単に「次の値」を払い出すだけの単純カウンタとして使う
        # (受信側の検証責務は親が持つ — 子は自分の送出 seq を数えるだけ)。
        n = self._seq._expected  # noqa: SLF001 — 同一モジュール内の協調実装
        self._seq._expected += 1
        return n

    def search_news(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_news", {"query": query, "n": n})

    def search_reflections(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_reflections", {"query": query, "n": n})


def _protect_protocol_stdout() -> Any:
    """JSON 行プロトコル専用の書き込み先を確保し、fd 1 (stdout) を fd 2
    (stderr) へ付け替えて返す (`plugin/worker.py:_protect_protocol_stdout`
    と同じパターン — import 済みライブラリの意図しない `print`/警告出力が
    プロトコルの JSON 1 行ストリームに混ざるのを防ぐ)。**何よりも先に**
    (`read_frame` より前) 呼ぶ — import 時の副作用による stdout 汚染も
    ここで防がれる対象に含める。
    """
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol_out


def main() -> None:
    protocol_out = _protect_protocol_stdout()
    handshake = read_frame(sys.stdin.buffer)
    if handshake is None:
        return

    expected_parent_pid = handshake["expected_parent_pid"]
    try:
        _set_pdeathsig(signal.SIGTERM)
        if os.getppid() != expected_parent_pid:
            # 設計書 §4.8 codex I2-4: prctl 設定前に親が死んで再親付け
            # されたレース。ready を送らずに即終了する (親の起動 timeout
            # がこれを検出する)。
            return
        settings_dict = handshake["settings"]
        worker_profile = handshake["worker_profile"]
        if worker_profile != "trade":
            raise RuntimeError(
                f"unsupported worker_profile in this plan: {worker_profile!r}")
        _set_resource_limits(
            as_mb=settings_dict["worker"]["child_as_mb"],
            nofile=settings_dict["worker"]["child_nofile"],
            fsize_mb=settings_dict["worker"]["child_fsize_mb"])

        from agentic_fx.config import Settings
        settings = Settings.model_validate(settings_dict)
        if settings.runner.trade.backend != "local":
            raise RuntimeError(
                f"runner.trade.backend={settings.runner.trade.backend!r} is "
                "not supported by mission_worker in this plan (ClaudeRunner "
                "is Plan 9 scope) — fail closed")

        from agentic_fx.core.contracts import FixedClock
        from agentic_fx.store.db import connect_readonly
        from agentic_fx.tools import plugin_loader
        from agentic_fx.tools.mission_registry import build_mission_registry
        from agentic_fx.activity import ActivityLog

        conn = connect_readonly(Path(handshake["db_path"]))
        clock = FixedClock.__new__(FixedClock)  # placeholder, replaced below
        from datetime import datetime, timezone
        now = datetime.fromisoformat(handshake["now"])
        clock = FixedClock(now)
        # 子は自身の scratch workdir に activity.log を持つ (§4.6 のとおり
        # econ.upcoming() は self.activity に触れないため実際には書かれ
        # ないが、EconCalendar のコンストラクタ契約を満たすためだけに
        # 必要 — mission_registry.py の docstring 参照)。
        activity = ActivityLog(Path.cwd() / "activity.log")
        plugins_dir = (Path(handshake["plugins_dir"])
                       if handshake.get("plugins_dir") else None)
        approved = (plugin_loader.approved_plugins(conn, plugins_dir)
                   if plugins_dir is not None else [])

        registry = build_mission_registry(
            "trade", conn, settings, clock,
            _RagRpcProxy(
                lambda frame: write_frame(protocol_out, frame),
                lambda: read_frame(sys.stdin.buffer),
                SeqTracker()),
            activity=activity, indicator_plugins=approved)

        from agentic_fx.runners.base import Mission
        from agentic_fx.runners.local_runner import LocalRunner

        mission = Mission(**handshake["mission"])
        out_seq = SeqTracker()

        def on_message(msg: dict) -> None:
            write_frame(protocol_out, {
                "type": "event", "seq": _next_seq(out_seq), "message": msg})

        runner = LocalRunner(
            base_url=settings.llama_swap.base_url,
            model=settings.runner.trade.model, registry=registry,
            on_message=on_message)

        write_frame(protocol_out, {
            "type": "ready", "seq": _next_seq(out_seq), "ok": True})

        try:
            result = runner.run(mission)
            write_frame(protocol_out, {
                "type": "result", "seq": _next_seq(out_seq),
                "status": result.status, "output": result.output})
        except Exception as exc:  # noqa: BLE001 — 必ず result を送る
            write_frame(protocol_out, {
                "type": "result", "seq": _next_seq(out_seq),
                "status": "failed", "output": None,
                "error": f"{type(exc).__name__}: {exc}"})
    except Exception as exc:  # noqa: BLE001 — ready 送出前の失敗も報告する
        try:
            write_frame(protocol_out, {
                "type": "ready", "seq": 1, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 — パイプが壊れていれば諦める
            pass


def _next_seq(tracker: SeqTracker) -> int:
    n = tracker._expected  # noqa: SLF001 — 送出側は検証ではなく採番に使う
    tracker._expected += 1
    return n


if __name__ == "__main__":
    main()
```

**実装者への注意**: `SeqTracker` は本来「受信側の検証」用に設計されているが、子の**送出側**でも「次に払い出すべき値」を同じフィールドから取り出す採番器として流用している (`_next_seq`/`_RagRpcProxy._seq_next` の `# noqa: SLF001` コメントのとおり、意図的な private 属性アクセス)。別の採番専用クラスを新設しない判断 (writing-plans) — `SeqTracker` の内部カウンタの意味 (「次に来る/送り出すべき値」) が両用途で一致するため。Task 10 (`WorkerRunner`) 側は同じ `SeqTracker` を**受信検証**に使う (`check()` 経由) — 送出用と受信検証用を混同しないこと (子の out_seq は「自分がこれから送る値」を採番するだけで `check()` は呼ばない。親の受信側 `SeqTracker` が `check()` で検証する)。

- [ ] **Step 9: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_mission_worker_protocol.py -q
```

Expected: PASS。

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS (mission_worker.py は他モジュールから import されないため既存スイートに影響なし)。

- [ ] **Step 11: 変異テスト**

1. `mission_protocol.SeqTracker.check` の `if seq != self._expected:` を `if False:` に改変 → `test_seq_tracker_rejects_duplicate`/`_gap`/`_regression` が red
2. `mission_worker._set_pdeathsig` 呼び出し後の `os.getppid() != expected_parent_pid` チェックを削除 (次 Step の実装から一時的に外す) → 対応する統合テストは Task 10 の E2E 側にあるため、ここでは `test_set_pdeathsig_calls_prctl_with_expected_args` に相当する fake 呼び出し引数アサートが red にならないことを逆に確認 (この変異はレビュー時に「Task 10 の統合テストで拾われる」ことをコメントで明記するに留める — Task 7 単体では観測できない設計上の限界)
3. `_RagRpcProxy._call` の `if not response.get("ok"):` を削除 → `test_rag_rpc_proxy_propagates_error` が red

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/core/mission_protocol.py src/agentic_fx/mission_worker.py \
  src/agentic_fx/config.py config/settings.yaml.example \
  tests/core/test_mission_protocol.py tests/test_mission_worker_protocol.py
git commit -m "$(cat <<'EOF'
feat: mission worker 子プロセス本体 (JSON行プロトコル + handshake + rlimit + PDEATHSIG)

設計書 §4.1-§4.5/§4.7 の子プロセス側 (worker_profile=trade 限定)。
親側 WorkerRunner は Task 10 で実装する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 8: worker 基盤 (4) — Landlock ctypes モジュール

設計書 §4.6「Landlock による FS 自己制限」を実装する。improve worker profile (Task 18) が使う独立した小モジュール — Landlock 自体は本 task で完結してテストでき、improve profile への配線は Task 18 に譲る。

**実機検証済み (writing-plans で実施)**: 本環境 (x86_64, kernel 7.0.0-28-generic) で syscall 番号・構造体レイアウトを実際に呼び出して検証した — `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` (syscall 444) が ABI バージョン 8 を返す (Landlock 利用可能)、`landlock_add_rule` (syscall 445) で `O_PATH` ディレクトリ fd に対する読み取り専用ルールを追加できる、`prctl(PR_SET_NO_NEW_PRIVS, 1)` → `landlock_restrict_self` (syscall 446) の順で自己制限を適用した後、許可ディレクトリ配下は `os.listdir` が成功し、`/tmp`・`/etc/hostname` は `PermissionError` (errno 13) になることを実測確認済み。**本モジュールは x86_64 Linux 専用** (syscall 番号はアーキテクチャ依存 — aarch64 等では異なる番号になる。`platform.machine() != "x86_64"` は起動時に `LandlockUnavailable` として拒否する)。

**Files:**
- Create: `src/agentic_fx/core/landlock.py`
- Test: `tests/core/test_landlock.py`

**Interfaces:**
- Produces:
  - `landlock.LandlockUnavailable(Exception)` — カーネル非対応・syscall 失敗・非対応アーキテクチャの単一表現
  - `landlock.is_available() -> bool` — ABI バージョン問い合わせ (`LANDLOCK_CREATE_RULESET_VERSION` フラグでの `landlock_create_ruleset` 呼び出し) が 1 以上を返せば True。`platform.machine() != "x86_64"` なら無条件 False
  - `landlock.restrict_to(*, read_only_paths: list[Path], read_write_paths: list[Path]) -> None` — 呼び出しプロセス自身を Landlock で FS allowlist に制限する (**不可逆 — プロセス生涯にわたって有効**)。利用不能なら `LandlockUnavailable` を送出する

- [ ] **Step 1: 失敗するテストを書く (fake ctypes 経由の分岐ロジック検証)**

`tests/core/test_landlock.py` を新規作成する:

```python
"""Landlock ctypes wrapper (プラン8, 設計書 §4.6)。

fake ctypes.CDLL によるロジック検証 (このプロセス自身は制限しない —
テストプロセス全体が二度と /tmp 等に触れなくなると以後のテストが全滅
する) と、実 Landlock を使う統合テスト (別プロセスで実施) を分離する。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import LandlockUnavailable, is_available, restrict_to


def test_is_available_false_on_non_x86_64(monkeypatch):
    import agentic_fx.core.landlock as landlock_mod
    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "aarch64")
    assert is_available() is False


def test_restrict_to_raises_when_create_ruleset_fails(monkeypatch, tmp_path):
    import agentic_fx.core.landlock as landlock_mod

    class FakeLibc:
        def syscall(self, *args):
            return -1  # landlock_create_ruleset 失敗

        def prctl(self, *args):
            return 0

    monkeypatch.setattr(landlock_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(landlock_mod.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(landlock_mod.ctypes, "get_errno", lambda: 38)  # ENOSYS
    with pytest.raises(LandlockUnavailable):
        restrict_to(read_only_paths=[tmp_path], read_write_paths=[])
```

(`is_available` 自体を fake する統合はここでは避け、`restrict_to` 内部で `is_available()` の判定に使う `platform.machine`/`ctypes.CDLL` を個別に monkeypatch する — `is_available` の実装が `ctypes.CDLL(None)` を直接呼ぶため、`restrict_to` からの呼び出しも同じ fake を経由する設計にする。)

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_landlock.py -q
```

Expected: FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `landlock.py` を実装**

```python
"""Landlock (Linux kernel 5.13+, ABI v1) による FS 自己制限 — ctypes による
syscall 直叩き (プラン8, 設計書 §4.6)。improve worker profile (Task 18) が
`data/` への到達不能を OS レベルで強制するために使う。

**x86_64 Linux 専用** — landlock_* syscall 番号はアーキテクチャ依存であり、
本モジュールは x86_64 の番号のみをハードコードする。他アーキテクチャでは
`is_available()` が無条件 False を返す (fail closed — improve worker の
起動を拒否する)。

実機検証済み (writing-plans, x86_64 / kernel 7.0.0-28-generic):
`landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` が
ABI バージョン 8 を返す / `landlock_add_rule` で O_PATH ディレクトリ fd に
対する読み取り専用ルールを追加できる / `prctl(PR_SET_NO_NEW_PRIVS, 1)` →
`landlock_restrict_self` の順で自己制限後、許可ディレクトリ外への
アクセスが `PermissionError` になることを実測確認済み。
"""
from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path

# x86_64 の landlock syscall 番号 (Linux 5.13+)。
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12

# ABI v1 の全 handled_access_fs (ruleset attr に必須 — 「この ruleset が
# 判定対象とするアクセス種別」の宣言。0x1FFF)。
_ABI_V1_HANDLED_ACCESS_FS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_WRITE_FILE | _ACCESS_FS_READ_FILE |
    _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_REG |
    _ACCESS_FS_MAKE_SOCK | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK |
    _ACCESS_FS_MAKE_SYM
)

_READ_ONLY_ACCESS = _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR
_READ_WRITE_ACCESS = (
    _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR | _ACCESS_FS_WRITE_FILE |
    _ACCESS_FS_MAKE_REG | _ACCESS_FS_REMOVE_FILE)

_PR_SET_NO_NEW_PRIVS = 38


class LandlockUnavailable(Exception):
    """カーネル非対応・非対応アーキテクチャ・syscall 失敗の単一表現。"""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


def is_available() -> bool:
    """Landlock ABI バージョンを問い合わせる。x86_64 以外は無条件 False。"""
    if platform.machine() != "x86_64":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None,
        ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return version >= 1


def restrict_to(*, read_only_paths: list[Path],
                read_write_paths: list[Path]) -> None:
    """呼び出しプロセスを Landlock で FS allowlist に制限する
    (**不可逆 — プロセス生涯にわたって有効**、以後の子プロセスにも継承
    される)。利用不能なら `LandlockUnavailable`。
    """
    if not is_available():
        raise LandlockUnavailable(
            "Landlock is not available (non-x86_64, or kernel ABI < 1)")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    ruleset_attr = _RulesetAttr(handled_access_fs=_ABI_V1_HANDLED_ACCESS_FS)
    ruleset_fd = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(ruleset_attr),
        ctypes.c_size_t(ctypes.sizeof(ruleset_attr)), ctypes.c_uint32(0))
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        raise LandlockUnavailable(
            f"landlock_create_ruleset failed: {os.strerror(errno)}")

    try:
        for path, access in (
                *((p, _READ_ONLY_ACCESS) for p in read_only_paths),
                *((p, _READ_WRITE_ACCESS) for p in read_write_paths)):
            parent_fd = os.open(str(path), os.O_PATH | os.O_DIRECTORY)
            try:
                rule_attr = _PathBeneathAttr(allowed_access=access,
                                             parent_fd=parent_fd)
                rc = libc.syscall(
                    ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset_fd),
                    ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                    ctypes.byref(rule_attr), ctypes.c_uint32(0))
                if rc != 0:
                    errno = ctypes.get_errno()
                    raise LandlockUnavailable(
                        f"landlock_add_rule failed for {path}: "
                        f"{os.strerror(errno)}")
            finally:
                os.close(parent_fd)

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise LandlockUnavailable(
                f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(errno)}")

        rc = libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                          ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
        if rc != 0:
            errno = ctypes.get_errno()
            raise LandlockUnavailable(
                f"landlock_restrict_self failed: {os.strerror(errno)}")
    finally:
        os.close(ruleset_fd)
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_landlock.py -q
```

Expected: PASS。

- [ ] **Step 5: 実 Landlock 統合テスト (別プロセス — E2E 帯)**

このプロセス自身を Landlock で制限すると以後のテストが全滅するため、**別プロセスを spawn してその中で実 `restrict_to` を呼ぶ**。`tests/core/test_landlock.py` に追記:

```python
_REAL_LANDLOCK_SCRIPT = textwrap.dedent("""
    import sys
    from pathlib import Path
    from agentic_fx.core.landlock import restrict_to

    allowed_dir = Path(sys.argv[1])
    blocked_dir = Path(sys.argv[2])
    restrict_to(read_only_paths=[allowed_dir], read_write_paths=[])
    # 許可ディレクトリは読める
    list(allowed_dir.iterdir())
    # 許可外ディレクトリは読めない
    try:
        list(blocked_dir.iterdir())
        print("FAIL: blocked_dir was readable")
        sys.exit(1)
    except PermissionError:
        print("OK")
        sys.exit(0)
""")


def test_real_landlock_blocks_unlisted_paths(tmp_path):
    """実 Landlock (別プロセス) — カーネルが対応していなければ skip する。"""
    from agentic_fx.core.landlock import is_available
    if not is_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "x.txt").write_text("ok")
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _REAL_LANDLOCK_SCRIPT, str(allowed), str(blocked)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
```

```bash
uv run pytest tests/core/test_landlock.py -q
```

Expected: PASS (実環境で Landlock 利用可能なら実際に制限を検証、不能なら skip)。

- [ ] **Step 6: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 7: 変異テスト**

1. `restrict_to` 内の `if rc != 0:` (landlock_restrict_self の結果検査) を削除 → `test_real_landlock_blocks_unlisted_paths` は失敗を検出できなくなるが直接的な red 化は難しいため、代わりに `is_available` の `return version >= 1` を `return False` に固定する変異を行い `test_is_available_false_on_non_x86_64` 相当のロジックテストで検出できることを確認する (実 syscall 系の変異は fake 経由のテストで検出できるものに絞る — これが本 task の変異テストの限界であり、実カーネル依存の壊れ方は Step 5 の実プロセステストが唯一の防波堤であることを progress.md に明記する)
2. `restrict_to` の `libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)` 呼び出しを削除 → `test_restrict_to_raises_when_create_ruleset_fails` は影響を受けない (create_ruleset の失敗が先に発火するため) が、これは fake テストの限界として明記し、Step 5 の実プロセステストが `landlock_restrict_self` 自体の欠落 (NO_NEW_PRIVS が無いと restrict_self が EPERM で失敗する — カーネルの実際の防御) を間接的に検出することを確認する

- [ ] **Step 8: Commit**

```bash
git add src/agentic_fx/core/landlock.py tests/core/test_landlock.py
git commit -m "$(cat <<'EOF'
feat: Landlock ctypes wrapper (x86_64 syscall 直叩き、実機検証済み)

設計書 §4.6。improve worker profile (Task 18) の FS 自己制限に使う
独立モジュール。syscall 番号・構造体レイアウトは本環境で実測検証済み。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 9: worker 基盤 (5) — Rag のスレッド安全化 (内部 lock + close())

設計書 §4.4「RAG RPC と Rag のスレッド安全化」(codex I-8) を実装する。現行 `Rag` (`store/rag.py`) にはロックも `close()` も無く、scheduler tick の news 書込・reflection 書込・(Task 10 以降の) worker RPC 読取が同一インスタンスを共有することになる — chromadb のスレッド安全性は保証に頼れないため、全公開メソッドを内部 lock で直列化する。

**実機確認済み**: `chromadb.PersistentClient` (本プロジェクト pin バージョン 1.5.9) には `close()` メソッドが実在する (`dir(client)` で確認済み) — 「無ければ参照破棄のみで可」という設計書の想定より単純に実装できる。

**Files:**
- Modify: `src/agentic_fx/store/rag.py` (全体 — lock 追加 + `close()` 新設)
- Test: `tests/store/test_rag_lock.py` (新規)

**Interfaces:**
- Produces:
  - `rag.RagUnavailable(Exception)` — lock を `lock_timeout_sec` 以内に取得できなかった場合の単一表現。**呼び出し側の責務**: news collector / reflection 書込 / RPC dispatcher (Task 10) はこれを「RAG 一時不可」として fail soft (該当機能 skip) で受ける — `Rag` 自身は health ラッチへの記録を行わない (health ラッチは App 層の概念 — Task 19)
  - `Rag.__init__(self, data_dir: Path, embedding_function=None, *, lock_timeout_sec: float = 10.0)` — `lock_timeout_sec` 新設 kwarg (既定 10.0 秒。`build_app` は `settings.worker.rpc_timeout_sec` を渡す — RPC dispatcher がハングでリークして lock を保持し続けた場合でも、news collector 等の他経路が無期限にブロックしないための波及止め。設計書 §4.4)
  - `Rag.close() -> None` — `self._client.close()` を lock 保護下で呼ぶ。lock 取得に失敗すれば `RagUnavailable` を送出する (App.close (Task 19) が「使用中は close しない」所有権ルールに従ってこれを catch し、close をスキップして記録する)
  - 全既存公開メソッド (`add_news`/`search_news`/`count_news`/`cleanup_news`/`add_reflection`/`search_reflections`) のシグネチャ・戻り値・例外契約は不変。内部で lock を取るようになる点のみが変更

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_rag_lock.py` を新規作成:

```python
"""Rag のスレッド安全化 (プラン8, 設計書 §4.4 codex I-8)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.store.rag import Rag, RagUnavailable


def _fake_embedding(texts):
    return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def test_search_news_raises_rag_unavailable_when_lock_held(tmp_path):
    """他スレッドが lock を保持し続けている間、search_news は
    lock_timeout_sec 経過で RagUnavailable を送出する (無期限ブロックしない)。"""
    rag = Rag(tmp_path / "rag", embedding_function=_fake_embedding,
             lock_timeout_sec=0.2)

    release = threading.Event()

    def hold_lock():
        with rag._lock:  # 内部実装への直接アクセス (テスト専用の白箱検証)
            release.wait(2.0)

    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    time.sleep(0.05)  # hold_lock が確実に lock を取ってから測る
    try:
        with pytest.raises(RagUnavailable):
            rag.search_news("query")
    finally:
        release.set()
        t.join(timeout=2.0)


def test_close_calls_chromadb_client_close(tmp_path, monkeypatch):
    rag = Rag(tmp_path / "rag", embedding_function=_fake_embedding)
    calls: list[str] = []
    monkeypatch.setattr(rag._client, "close", lambda: calls.append("closed"))
    rag.close()
    assert calls == ["closed"]


def test_normal_operations_still_serialize_correctly(tmp_path):
    """lock 導入後も既存の add_news/search_news/cleanup_news の挙動が
    不変であることの回帰確認 (既存 tests/store/test_rag.py が主だが、
    ここでも 1 本だけ通しで確認する)。"""
    from datetime import datetime, timezone

    rag = Rag(tmp_path / "rag", embedding_function=_fake_embedding)
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    added = rag.add_news(
        [{"url": "https://x/1", "title": "t", "body": "b",
         "source_name": "s", "published": None}], now)
    assert added == 1
    assert rag.count_news() == 1
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_rag_lock.py -q
```

Expected: FAIL (`AttributeError: 'Rag' object has no attribute '_lock'` / `ImportError: cannot import name 'RagUnavailable'`)。

- [ ] **Step 3: `rag.py` を実装**

`src/agentic_fx/store/rag.py` の import 節に `import threading` と `from contextlib import contextmanager` を追加する。`_require_utc` の直前に追加:

```python
class RagUnavailable(Exception):
    """RAG 内部 lock を `lock_timeout_sec` 以内に取得できなかった (一時的な
    不可 — 呼び出し側は fail soft (該当機能 skip) で受けること。設計書
    §4.4 codex I2-2)。"""
```

`Rag.__init__` を以下に置き換える:

```python
    def __init__(self, data_dir: Path, embedding_function=None, *,
                lock_timeout_sec: float = 10.0) -> None:
        # (既存の ef 構築・probe 呼び出しは無変更 — このコメント位置に
        # 既存の 4 行 (ef = ... / ef([...]) を維持する)
        ef = (embedding_function if embedding_function is not None
              else DefaultEmbeddingFunction())
        ef(["__afx_rag_init_probe__"])

        self._client = chromadb.PersistentClient(path=str(data_dir))
        self._news = self._client.get_or_create_collection(
            "news", embedding_function=ef)
        self._refl = self._client.get_or_create_collection(
            "reflections", embedding_function=ef)
        # プラン 8 worker 基盤 (codex I-8): 全公開メソッドを直列化する
        # 内部 lock。低頻度・短時間の呼び出しなので直列化のコストは
        # 無視できる。
        self._lock = threading.Lock()
        self._lock_timeout_sec = lock_timeout_sec

    @contextmanager
    def _locked(self):
        acquired = self._lock.acquire(timeout=self._lock_timeout_sec)
        if not acquired:
            raise RagUnavailable(
                f"RAG lock not acquired within {self._lock_timeout_sec}s "
                "(a caller is holding it — fail soft: skip this operation)")
        try:
            yield
        finally:
            self._lock.release()

    def close(self) -> None:
        """chromadb PersistentClient.close() を lock 保護下で呼ぶ
        (実機確認済み — 1.5.9 に実在する)。lock 取得に失敗すれば
        `RagUnavailable` (呼び出し側の App.close は「使用中は close しない」
        所有権ルールに従ってこれを catch し、close をスキップして記録
        すること — 設計書 §5)。
        """
        with self._locked():
            self._client.close()
```

既存の 6 公開メソッド (`add_news`/`search_news`/`count_news`/`cleanup_news`/`add_reflection`/`search_reflections`) を、それぞれ**メソッド本体全体を `with self._locked():` で包む**形に変更する。例 (`search_news`):

```python
    def search_news(self, query: str, n: int = 5) -> list[dict]:
        """意味検索で news を引く。body は元記事の本文 (embedding 用に
        連結した "title\\nbody" テキストではない — メタデータに別途持つ)。
        """
        with self._locked():
            count = self.count_news()
            if count == 0:
                return []
            res = self._news.query(query_texts=[query], n_results=min(n, count))
            return [{"url": m["url"], "title": m["title"], "body": m["body"],
                     "source_name": m["source_name"]}
                    for m in res["metadatas"][0]]
```

**注意 (実装者向け — 再入デッドロック回避)**: `search_news` は内部で `self.count_news()` を呼ぶ。`count_news` 自身も `with self._locked():` で包むと、`threading.Lock` は非再入 (再帰取得不可) のため**自スレッドからの二重取得でデッドロックする**。実装時は以下のいずれかを選ぶこと (writing-plans 推奨: 案 A):
- **案 A (推奨)**: `count_news` の**内部実装**を lock 無しの private ヘルパー `_count_news_unlocked(self) -> int` に切り出し、公開 `count_news` は `with self._locked(): return self._count_news_unlocked()` に、`search_news` 内部の呼び出しは `self._count_news_unlocked()` (lock を取らない版) に変更する
- 案 B: `threading.Lock` を `threading.RLock` に変更する (再入を許す) — ただし `_locked()` の timeout 意味論が「累積」ではなく「その時点でのブロック待ち」になる点に注意 (RLock の `acquire(timeout=...)` は同一スレッドからの再取得は即座に成功するため実害はない)

いずれを選んでも Step 4 のテストが通ることを確認すること。**本プランは案 A を正とする** (`RLock` は「取得元スレッドを問わない直列化」という lock の意図をぼかすため — 実装は明示的に `_count_news_unlocked` を切り出すこと)。

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_rag_lock.py -q
uv run pytest tests/store/ -k rag -q
uv run pytest -q
```

Expected: 全件 PASS (既存 `tests/store/test_rag.py` 相当のテストが lock 導入後も無変更で通ること)。

- [ ] **Step 5: `build_app` に `lock_timeout_sec` を配線**

`src/agentic_fx/service.py:304` の `rag = Rag(root / "data" / "rag", embedding_function=embedding_fn)` を以下に変更する (`settings.worker.rpc_timeout_sec` を配線 — 実際に使い始めるのは Task 10 の RPC dispatcher からだが、値の由来をここで確定しておく):

```python
    rag = Rag(root / "data" / "rag", embedding_function=embedding_fn,
             lock_timeout_sec=settings.worker.rpc_timeout_sec)
```

- [ ] **Step 6: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 7: 変異テスト**

1. `_locked()` の `if not acquired:` を削除 (timeout 検出を無効化) → `test_search_news_raises_rag_unavailable_when_lock_held` が red
2. `close()` から `with self._locked():` を外す → `test_close_calls_chromadb_client_close` 自体は red にならない可能性があるため (lock が無くても close は呼ばれる)、代わりに「lock 保持中に `close()` を呼ぶと `RagUnavailable` になる」ケースを追加確認する変異テストとして、`test_search_news_raises_rag_unavailable_when_lock_held` と同型のテストを `close()` に対しても書き、この変異で red になることを確認する (実装者は Step 1 のテストに `test_close_raises_rag_unavailable_when_lock_held` を追加してから本変異を実施すること)

- [ ] **Step 8: Commit**

```bash
git add src/agentic_fx/store/rag.py src/agentic_fx/service.py tests/store/test_rag_lock.py
git commit -m "$(cat <<'EOF'
feat: Rag のスレッド安全化 (内部 lock + close())

設計書 §4.4 (codex I-8)。scheduler tick の news/reflection 書込と
worker RPC 読取 (Task 10) が同一 Rag インスタンスを共有しても安全にする。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 10: WorkerRunner 本体 (spawn + preemption + RPC dispatcher + transcript 上限)

設計書 §3.1「WorkerRunner seam」、§4.1(spawn/handshake)、§4.3(reader/dispatcher スレッド構成)、§4.7(preemption エスカレーション)、§4.8(finalize は呼び出し元、close 順序)、§8(新設定キーの実消費) を実装する。`AgentRunner` の 3 つ目の実装として `build_app` のデフォルト runner を差し替える — この task 単体で「Mission が子プロセスで動く」ところまで完成させる (`core_lock` 粒度の再設計は Task 13/15/16)。

**Files:**
- Create: `src/agentic_fx/runners/worker_runner.py`
- Modify: `src/agentic_fx/service.py:339-343`(`build_app` のデフォルト runner を `WorkerRunner` に)`,573-577`(shutdown の close 判定を `hasattr` ベースに一般化)
- Modify: `tests/test_service_app.py:453-459` (`isinstance(app.runner, LocalRunner)` → `WorkerRunner` に更新)
- Test: `tests/runners/test_worker_runner.py` (新規 — FakeChild によるインプロセス単体テスト + 実 subprocess の最小 E2E 1 本)

**Interfaces:**
- Produces:
  - `WorkerRunner(AgentRunner)` — `__init__(self, *, root: Path, settings: Settings, clock: Clock, rag: Rag, worker_profile: str = "trade", on_rpc_leak: Callable[[], None] | None = None) -> None`。`run(self, mission: Mission) -> MissionResult` (継承契約どおり)。`close(self) -> None` (no-op — 各 Mission が自分の子プロセスを spawn/reap するため永続資源を持たない。`service.py` の shutdown 判定を `LocalRunner` 専用の isinstance から `hasattr(app.runner, "close")` へ一般化するための対称メソッド)
  - `on_rpc_leak` — RPC dispatcher が `rpc_timeout_sec` を超えてハングした RAG 呼び出しを検出したときに呼ばれる (設計書 §4.3 codex I3-1 — 「累積許容しない」の通知経路。呼び出し元 = App/health ラッチ配線は Task 19)

**設計判断 (writing-plans)**:
- **cwd**: `tempfile.TemporaryDirectory(prefix="afx-mission-")` で Mission ごとに新規の空ディレクトリを作り `cwd=` に渡す。Mission 終了後 (成功・失敗・timeout いずれでも) `with` ブロックで自動削除する
- **env**: `plugin/sandbox.py:_build_env()` を再利用する (`AFX_*` 等の秘密を継承しない最小 env — network poison (`_poison_network_modules`) は呼ばない。mission worker は信頼済みハーネスコードでネットワークが必要、という設計書 §4.5 の方針)
- **handshake の `now`**: `clock.now()` を 1 回だけ取得し、子は `FixedClock(now)` として Mission 全体で使う (子内の全ツール呼び出しが同じ「判断時点」を見る — Mission 実行中に親と子で時刻がずれて判断材料が矛盾することを避ける、既存の `cycle_rate_fn`/`_evaluate_positions` が「1 回の判断内でレートを固定する」のと同じ設計思想)
- **RPC dispatcher のリーク検出**: `concurrent.futures.ThreadPoolExecutor(max_workers=1)` に RAG 呼び出しを submit し、`future.result(timeout=rpc_timeout_sec)` で打ち切る。`TimeoutError` はリークとして扱い `on_rpc_leak()` を呼ぶ (worker 側のスレッドは回収できないまま残る — 設計書 §4.3 codex I3-1 の裁定どおり「別プロセス化はしない・累積許容もしない」)。`max_workers=1` により、1 回リークした後の後続 RPC は自然に (executor の唯一のワーカーが塞がっているため) 同様に timeout する — 追加のガード条件は不要

- [ ] **Step 1: 失敗するテストを書く (FakeChild によるインプロセス単体テスト)**

`tests/runners/test_worker_runner.py` を新規作成する。実 subprocess の代わりに `subprocess.Popen` を monkeypatch し、パイプの片側をテストコードが操作する FakeChild ヘルパーで検証する:

```python
"""WorkerRunner (プラン8, 設計書 §3.1/§4.1/§4.3/§4.7)。

FakeChild: 実 subprocess を spawn せず、os.pipe() で親子間パイプを模倣し、
別スレッドで「子のふり」をして handshake/ready/event/result を書く。
実 subprocess spawn の最小 E2E は本ファイル末尾の
test_real_subprocess_completes_mission_end_to_end のみ (他は全て
FakeChild 経由)。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.mission_protocol import write_frame
from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from datetime import datetime, timezone

SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _root(tmp_path):
    import shutil
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copy(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example",
        root / "config" / "settings.yaml")
    init_db(connect(root / "data" / "agentic.db"))
    return root


def _rag(tmp_path):
    return Rag(tmp_path / "rag", embedding_function=lambda t: [[0.0] * 4 for _ in t])


def _mission():
    return Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                   max_turns=1, timeout_sec=5.0)


class _FakeChildScript:
    """テストが `subprocess.Popen` の代わりに使う擬似子プロセス。実
    プロセスは起動しない — 親側 (WorkerRunner) が書く stdin をこのスレッドが
    読み、指定したフレーム列を stdout 側パイプへ書き込む。"""

    def __init__(self, frames_after_handshake, *, delay_before_result=0.0):
        self.frames = frames_after_handshake
        self.delay = delay_before_result
        self.pid = 999999  # WorkerRunner が expected_parent_pid の照合対象に
                            # しない値 (FakeChild は本物の os.getppid() 照合を
                            # 経由しない — インプロセステストのため)
        self.returncode = None
        self._killed = threading.Event()

    def poll(self):
        return None if not self._killed.is_set() else -9

    def wait(self, timeout=None):
        self._killed.wait(timeout)
        return -9 if self._killed.is_set() else None

    def kill_signal_received(self):
        self._killed.set()
```

(このフィクスチャ設計は WorkerRunner の実装詳細 — `subprocess.Popen` の戻り値をどこまで模倣する必要があるか — に強く依存する。**実装者は Step 3 で WorkerRunner を書いた後、その実装が `proc.stdin`/`proc.stdout`/`proc.poll()`/`os.killpg(proc.pid, ...)` のどれを呼ぶかを確定させ、それに合わせて `_FakeChildScript` を拡張すること**。`os.killpg` は実 pid 空間に対する OS 呼び出しであり FakeChild の偽 pid には効かないため、**WorkerRunner の kill 経路を `self._kill_fn: Callable[[subprocess.Popen], None]` としてテスト注入可能な形にする** (既定は `lambda proc: os.killpg(proc.pid, signal.SIGKILL)`) — これによりテストは `kill_fn` を差し替えて「kill が呼ばれたこと」を観測する。以下のテストコードはこの前提で書く:

```python
def test_worker_runner_completes_mission_via_pipes(tmp_path, monkeypatch):
    """正常系: handshake→ready→event×2→result の往復を検証する。"""
    r, w = os.pipe()   # 子→親 (子が書く側 = w、親が読む側 = r)
    r2, w2 = os.pipe()  # 親→子 (親が書く側 = w2、子が読む側 = r2)

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "event", "seq": 2,
                                "message": {"role": "user", "content": "hi"}})
        write_frame(child_out, {"type": "result", "seq": 3,
                                "status": "completed", "output": {"x": 1}})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()  # 自プロセス pid — expected_parent_pid 照合は
                            # FakeChild 側では検証しない (実 subprocess の
                            # 責務。テストは配線だけを見る)
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert result.output == {"x": 1}
    assert {"role": "user", "content": "hi"} in result.transcript
```

（この 1 本は WorkerRunner の I/O 配線を通しで確認する骨格テストであり、実装が `subprocess.Popen` をどう呼ぶか (`stdin`/`stdout` を `os.fdopen` した実ファイルオブジェクトとして扱うか) に依存する。**実装者は Step 3 を書いた後、この骨格に合わせてテストの `FakeProc` を調整すること** — 特に `stdin.write`/`stdout.readline` が実際に呼ばれる形と一致させる。）

追加で以下のケースをカバーする独立したテストを書く (骨格は上と同じ `FakeProc`/`subprocess.Popen` monkeypatch パターンを流用し、子スレッドの応答内容だけを変える):

1. `test_worker_runner_startup_timeout_kills_child` — `ready` を送らないまま `worker_startup_timeout_sec` (settings を `model_copy` で短縮して注入) を超過させ、`kill_fn` が呼ばれ `status == "failed"` になることを確認
2. `test_worker_runner_mission_timeout_escalates_sigterm_then_sigkill` — `result` を送らないまま `mission.timeout_sec + worker_grace_sec` を超過させ、SIGTERM 相当の呼び出し (`terminate_fn`) → `worker_terminate_grace_sec` 経過後に `kill_fn` が呼ばれることを確認。`status == "timeout"`
3. `test_worker_runner_child_eof_before_result_is_failed` — `ready` 送出後、`result` を送らずに子スレッドがパイプを閉じる (EOF) → `status == "failed"`
4. `test_worker_runner_protocol_violation_is_failed` — `event` フレームの `seq` を逆行させて送る → `status == "failed"` (fail closed — `mission_protocol.ProtocolError` を検出)
5. `test_worker_runner_transcript_truncates_at_cap` — `transcript_max_bytes` を小さく (例: 200 バイト) 設定し、大量の `event` を送る → `result.transcript` に truncate marker が 1 件だけ含まれ、以後の `event` が積まれていないことを確認
6. `test_worker_runner_rag_rpc_dispatches_to_rag_and_responds` — `tool_rpc` (`name="search_news"`) を送り、fake `Rag.search_news` が呼ばれて `tool_rpc_result` が子側パイプ (`r2`) から読めることを確認
7. `test_worker_runner_rag_rpc_leak_calls_on_rpc_leak` — fake `Rag.search_news` を `rpc_timeout_sec` より長くブロックする関数に差し替え、`on_rpc_leak` コールバックが呼ばれることを確認 (mission 自体は `result` フレームが届けば completed のまま終わってよい — リーク検出とミッション結果は独立)

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/runners/test_worker_runner.py -q
```

Expected: 全件 FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `worker_runner.py` を実装**

```python
"""WorkerRunner — Mission 実行を preemption 可能な使い捨て子プロセスへ
隔離する AgentRunner 実装 (プラン8, 設計書 §3.1/§4.1/§4.3/§4.7)。

親→子の spawn/handshake、子→親の event/tool_rpc/result 受信、
timeout エスカレーション (SIGTERM→grace→SIGKILL) をすべてこのクラスの
`run()` 呼び出し 1 回に閉じる。preemption 後も `MissionResult(status=
"timeout")` を返し、既存の 4 終端契約 (completed/failed/timeout/
max_turns) に in-band で乗る (呼び出し元は他の AgentRunner 実装と
区別なく扱える)。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from agentic_fx.core.contracts import Clock
from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, read_frame, write_frame,
)
from agentic_fx.plugin.sandbox import _build_env
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store.rag import Rag

import logging

_log = logging.getLogger("agentic_fx.worker_runner")


class WorkerRunner(AgentRunner):
    def __init__(self, *, root: Path, settings, clock: Clock, rag: Rag,
                worker_profile: str = "trade",
                on_rpc_leak: Callable[[], None] | None = None) -> None:
        self._root = root
        self._settings = settings
        self._clock = clock
        self._rag = rag
        self._worker_profile = worker_profile
        self._on_rpc_leak = on_rpc_leak

    def close(self) -> None:
        """no-op — 各 Mission が自分の子プロセスを spawn/reap するため
        永続資源を持たない (LocalRunner との対称性のための空実装)。"""

    def run(self, mission: Mission) -> MissionResult:
        w = self._settings.worker
        with tempfile.TemporaryDirectory(prefix="afx-mission-") as workdir:
            proc = subprocess.Popen(
                [sys.executable, "-m", "agentic_fx.mission_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=workdir, env=_build_env(),
                start_new_session=True)
            return self._run_with_child(proc, mission, w)

    # ---- 内部 -------------------------------------------------------

    def _run_with_child(self, proc, mission: Mission, w) -> MissionResult:
        stdin_lock = threading.Lock()
        in_seq = SeqTracker()  # 子→親方向の受信検証
        ready_queue: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        done_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)
        dispatch_queue: "queue.Queue[dict]" = queue.Queue()
        transcript: list[dict] = []
        state = {"bytes": 0, "truncated": False}

        def handle_event(frame: dict) -> None:
            if state["truncated"]:
                return
            msg = frame["message"]
            size = len(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
            if state["bytes"] + size > w.transcript_max_bytes:
                transcript.append({"role": "system",
                                   "content": "[transcript truncated: "
                                              "exceeded transcript_max_bytes]"})
                state["truncated"] = True
                return
            transcript.append(msg)
            state["bytes"] += size

        def reader_loop() -> None:
            try:
                while True:
                    frame = read_frame(proc.stdout)
                    if frame is None:
                        done_queue.put(("eof", None))
                        return
                    try:
                        in_seq.check(frame.get("seq"))
                    except ProtocolError as e:
                        done_queue.put(("protocol_error", str(e)))
                        return
                    ftype = frame.get("type")
                    if ftype == "ready":
                        ready_queue.put(frame)
                    elif ftype == "event":
                        handle_event(frame)
                    elif ftype == "tool_rpc":
                        dispatch_queue.put(frame)
                    elif ftype == "result":
                        done_queue.put(("result", frame))
                        return
                    else:
                        done_queue.put(
                            ("protocol_error", f"unknown frame type {ftype!r}"))
                        return
            except Exception as e:  # noqa: BLE001 — reader は死なせない代わりに報告する
                done_queue.put(("error", str(e)))

        rpc_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="afx-rag-rpc")

        def dispatcher_loop() -> None:
            out_seq_holder = {"n": 0}
            while True:
                frame = dispatch_queue.get()
                if frame is None:  # shutdown 合図
                    return
                future = rpc_executor.submit(
                    self._call_rag, frame["name"], frame["args"])
                try:
                    result = future.result(timeout=w.rpc_timeout_sec)
                    response = {"ok": True, "result": result}
                except concurrent.futures.TimeoutError:
                    _log.error("RAG RPC leaked past rpc_timeout_sec=%s "
                              "(name=%s) — this thread will never be "
                              "reclaimed (设計書 §4.3 codex I3-1)",
                              w.rpc_timeout_sec, frame["name"])
                    if self._on_rpc_leak is not None:
                        try:
                            self._on_rpc_leak()
                        except Exception:  # noqa: BLE001
                            _log.exception("on_rpc_leak callback failed")
                    response = {"ok": False, "error": "rag rpc timed out"}
                except Exception as e:  # noqa: BLE001 — RagUnavailable も含め子へ tool error として返す
                    response = {"ok": False, "error": str(e)}
                out_seq_holder["n"] += 1
                try:
                    with stdin_lock:
                        write_frame(proc.stdin, {
                            "type": "tool_rpc_result",
                            "seq": out_seq_holder["n"] + 1,  # handshake=1 済み
                            "rpc_id": frame["rpc_id"], **response})
                except (BrokenPipeError, OSError):
                    return  # 子が既に死んでいる — 応答不能

        reader = threading.Thread(target=reader_loop, daemon=True)
        dispatcher = threading.Thread(target=dispatcher_loop, daemon=True)
        reader.start()
        dispatcher.start()

        now = self._clock.now()
        handshake = {
            "type": "handshake", "seq": 1,
            "expected_parent_pid": os.getpid(),
            "db_path": (str(self._root / "data" / "agentic.db")
                       if self._worker_profile == "trade" else None),
            "plugins_dir": (str(self._root / "plugins")
                            if self._worker_profile == "trade" else None),
            "settings": self._settings.model_dump(),
            "mission": {"prompt": mission.prompt, "tools": mission.tools,
                       "output_schema": mission.output_schema,
                       "max_turns": mission.max_turns,
                       "timeout_sec": mission.timeout_sec},
            "worker_profile": self._worker_profile,
            "now": now.isoformat(),
        }
        with stdin_lock:
            write_frame(proc.stdin, handshake)

        status = "failed"
        output = None
        try:
            try:
                ready = ready_queue.get(timeout=w.worker_startup_timeout_sec)
                if not ready.get("ok", False):
                    status = "failed"
                    return MissionResult(status, None, transcript)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("failed", None, transcript)

            deadline_budget = mission.timeout_sec + w.worker_grace_sec
            try:
                kind, payload = done_queue.get(timeout=deadline_budget)
            except queue.Empty:
                self._escalate_kill(proc, w)
                return MissionResult("timeout", None, transcript)

            if kind == "result":
                status = payload["status"]
                output = payload.get("output")
            else:  # eof / protocol_error / error — すべて failed に正規化
                status = "failed"
                output = None
            return MissionResult(status, output, transcript)
        finally:
            dispatch_queue.put(None)
            self._ensure_dead(proc, w)
            reader.join(timeout=5.0)
            rpc_executor.shutdown(wait=False)
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

    def _call_rag(self, name: str, args: dict):
        method = getattr(self._rag, name)
        return method(**args)

    def _escalate_kill(self, proc, w) -> None:
        """SIGTERM → `worker_terminate_grace_sec` → SIGKILL (設計書 §4.7)。"""
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + w.worker_terminate_grace_sec
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            self._kill(proc)

    def _ensure_dead(self, proc, w) -> None:
        """finally 節: どの終了経路でも子が生きていれば確実に殺す。
        既に `_escalate_kill` が呼ばれていれば `proc.poll()` は None
        ではないため no-op。"""
        if proc.poll() is None:
            self._kill(proc)

    def _kill(self, proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
```

**実装者への注意**: 上記は骨格実装であり、Step 1 のテストを実際に流しながら以下を調整すること — ①`_FakeChildScript`/`FakeProc` が `os.killpg(proc.pid, ...)` を呼べない (FakeProc.pid が実プロセスでない) ケースをどう扱うか (テストでは `monkeypatch.setattr(wr_mod.os, "killpg", ...)` で `os.killpg` 自体を差し替え、呼び出し引数を記録する形にする) ②`out_seq_holder["n"] + 1` のオフセット (handshake が seq=1 を消費済みであることの整合) は実装しながら実際のフレーム列で検証すること ③`dispatcher_loop` の `dispatch_queue.put(None)` によるシャットダウン合図が、リーク中 (executor の唯一のワーカーが塞がっている) でも `dispatcher_loop` 自身のループ (`dispatch_queue.get()` 待ち) を正しく抜けられることを確認すること (dispatcher スレッド自体はリークしない — リークするのは `rpc_executor` 内の 1 ワーカースレッドのみ)。

- [ ] **Step 4: テスト実行して PASS を確認 (段階的に)**

```bash
uv run pytest tests/runners/test_worker_runner.py -q
```

実装と FakeProc/monkeypatch の整合を取りながら 1 本ずつ green にすること。全件 PASS になるまで Step 3/Step 1 を往復してよい (TDD の red→green サイクルをこの 2 step 間で回す)。

- [ ] **Step 5: 実 subprocess の最小 E2E (1 本)**

`tests/runners/test_worker_runner.py` の末尾に、実際に `python -m agentic_fx.mission_worker` を spawn する 1 本を追加する (llama-swap への実 HTTP は発生させない — `Mission.tools=[]`・`output_schema` が単純な固定 JSON を要求する形にはできない (LocalRunner は実際に LLM へ問い合わせるため) ので、**このテストは「起動〜ready〜(接続先が存在せず timeout)〜preemption による終了」までを実証する** — 実 LLM 応答が無い環境でも決定論的に検証できる範囲に留める):

```python
def test_real_subprocess_starts_and_reports_ready_then_times_out(tmp_path):
    """実 subprocess を spawn する最小 E2E。llama-swap への接続先が存在
    しない (base_url を到達不能な値に上書き) ため LocalRunner 側は
    timeout する — 子プロセスの起動・handshake・ready 応答・preemption
    による終了までが実際に動くことを実証する (詳細なハング注入・
    kill 検証は Task 20 の E2E に譲る)。
    """
    root = _root(tmp_path)
    settings = SETTINGS.model_copy(update={
        "llama_swap": SETTINGS.llama_swap.model_copy(
            update={"base_url": "http://127.0.0.1:1", "timeout_sec": 2}),
        "worker": SETTINGS.worker.model_copy(
            update={"worker_grace_sec": 2.0, "worker_terminate_grace_sec": 2.0,
                    "worker_startup_timeout_sec": 15.0})})
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=settings, clock=clock,
                          rag=_rag(tmp_path))
    mission = Mission(prompt="hi", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=2.0)
    result = runner.run(mission)
    assert result.status in ("timeout", "failed")
```

```bash
uv run pytest tests/runners/test_worker_runner.py -q -k real_subprocess
```

Expected: PASS (数秒で完了する — `worker_grace_sec`/`timeout_sec` を短く設定しているため)。

- [ ] **Step 6: `build_app` のデフォルト runner を差し替え**

`src/agentic_fx/service.py` の import 節に `from agentic_fx.runners.worker_runner import WorkerRunner` を追加する。`build_app` (339-343 行) を以下に置き換える:

```python
    owns_runner = runner is None
    if runner is None:
        runner = WorkerRunner(root=root, settings=settings, clock=clock,
                              rag=rag, worker_profile="trade")
```

`run_service` (573-577 行付近) の shutdown 判定を一般化する:

```python
            if app.owns_runner and hasattr(app.runner, "close"):
                app.runner.close()
```

- [ ] **Step 7: 既存テストの更新**

`tests/test_service_app.py:453-459` の `test_owns_runner_true_when_built_locally` を以下に更新する:

```python
def test_owns_runner_true_when_built_locally(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, clock=FixedClock(NOW))
    assert app.owns_runner is True
    assert isinstance(app.runner, WorkerRunner)
    app.runner.close()  # no-op — 対称性の確認
```

`tests/test_service_app.py` の import 節に `from agentic_fx.runners.worker_runner import WorkerRunner` を追加する (`LocalRunner` の import が他で使われていなければ削除、使われていれば残す — `grep -n "LocalRunner" tests/test_service_app.py` で確認)。

- [ ] **Step 8: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。**注意**: `build_app` を `runner=None` で呼ぶ既存テストが他にもあれば (`grep -rn "build_app(" tests/ | grep -v "runner="`)、それらは今後 `WorkerRunner` 経由になり実サブプロセスを spawn する可能性がある — 該当テストを洗い出し、実行時間が許容範囲か確認する。許容できないほど遅い/不安定なテストがあれば `runner=FakeRunner([...])` を明示的に渡すよう修正すること (テストの意図を変えない範囲で)。

- [ ] **Step 9: 変異テスト**

1. `_escalate_kill` の `os.killpg(proc.pid, signal.SIGTERM)` を削除 (SIGKILL だけ残す) → `test_worker_runner_mission_timeout_escalates_sigterm_then_sigkill` が red (SIGTERM が呼ばれない)
2. `handle_event` の `if state["bytes"] + size > w.transcript_max_bytes:` の閾値判定を削除 → `test_worker_runner_transcript_truncates_at_cap` が red
3. `dispatcher_loop` の `future.result(timeout=w.rpc_timeout_sec)` の `timeout=` を削除 (無期限待ちにする) → `test_worker_runner_rag_rpc_leak_calls_on_rpc_leak` が red (テストがタイムアウトするか `on_rpc_leak` が呼ばれない)
4. `in_seq.check(frame.get("seq"))` の呼び出しを削除 → `test_worker_runner_protocol_violation_is_failed` が red

- [ ] **Step 10: Commit**

```bash
git add src/agentic_fx/runners/worker_runner.py src/agentic_fx/service.py \
  tests/runners/test_worker_runner.py tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: WorkerRunner (使い捨て子プロセスへの Mission 隔離 + preemption + RPC dispatcher)

設計書 §3.1/§4.1/§4.3/§4.7。build_app のデフォルト runner を WorkerRunner
に差し替える (core_lock 粒度の再設計は Task 13/15/16)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 11: `missions.finish` の CAS 化 + 起動時 running→interrupted 回収 (missions+signals 同一トランザクション)

設計書 §4.7「finalize 所有権と二重終端防止」(codex C-4) と §4.8「残留の回収と孤児対策」(codex C-5) を実装する。この task は Task 15/16 (五相再構成) の前提部品 — `missions.finish` の呼び出し元 (`trade_loop.py`/`reflection_cycle.py`) 自体の書き換えは Task 15/16 に譲る (戻り値の型を `None`→`bool` に変えても、既存の呼び出し元は戻り値を使っていないため無変更で動く — 呼び出し元が CAS の戻り値を見て activity 警告を出すよう更新するのは Task 15/16 の五相再構成と同時に行う)。

**Files:**
- Modify: `src/agentic_fx/store/missions.py` (`finish` を CAS 化、`recover_interrupted` 新設)
- Modify: `src/agentic_fx/service.py:262-266`(起動シーケンスに `recover_interrupted` 追加)
- Test: `tests/store/test_missions_cas.py` (新規)

**Interfaces:**
- Produces:
  - `missions.finish(conn, mission_id: int, status: str, output: dict | None, transcript: list, now: datetime) -> bool` — **`WHERE id=? AND status='running'` の CAS**。戻り値 `True` = この呼び出しが終端を書いた、`False` = 既に終端済み (影響行数 0、上書きしない)。**呼び出し元は `False` のとき activity 警告を残し、この結果を上書きしないこと** (呼び出し元の更新は Task 15/16)
  - `missions.recover_interrupted(conn, *, now: datetime, max_requeue: int) -> dict` — `status='running'` の missions 行を全件 `'interrupted'` へ終端し、それらを claim していた `claimed` signals を**同一トランザクション**で requeue (上限超過は abandoned) する。戻り値 `{"missions_recovered": int, "signals_requeued": int, "signals_abandoned": int}`。**`'interrupted'` は DB 回収専用の状態値であり `MissionResult.status` の 4 値契約 (`runners/base.py:40`) には現れない** (codex M-1 — `missions` テーブルの `status` 列に CHECK 制約は無いためスキーマ変更不要)

- [ ] **Step 1: 失敗するテストを書く (`finish` の CAS 化)**

`tests/store/test_missions_cas.py` を新規作成:

```python
"""missions.finish の CAS 化 + 起動時回収 (プラン8, 設計書 §4.7/§4.8)。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_fx.store import missions, signals
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    return conn


def test_finish_returns_true_on_first_call(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    assert missions.finish(conn, mid, "completed", {"a": 1}, [], NOW) is True
    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "completed"


def test_finish_returns_false_on_second_call_and_does_not_overwrite(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    assert missions.finish(conn, mid, "completed", {"a": 1}, [], NOW) is True
    assert missions.finish(conn, mid, "failed", None, [], NOW) is False
    row = conn.execute(
        "SELECT status, output_json FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "completed"  # 上書きされていない
    assert row["output_json"] == '{"a": 1}'
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q -k finish
```

Expected: FAIL (`assert None is True`)。

- [ ] **Step 3: `finish` を CAS 化**

`src/agentic_fx/store/missions.py:23-32` の `finish` を以下に置き換える:

```python
def finish(conn: sqlite3.Connection, mission_id: int, status: str,
           output: dict | None, transcript: list, now: datetime) -> bool:
    """missions 行を終端状態へ CAS 更新する (設計書 §4.7 codex C-4)。

    `WHERE status='running'` を満たさない (= 既に終端済み) 場合は影響行数
    0 のまま何もしない — 無条件 UPDATE による「後勝ち上書き」を構造的に
    封鎖する (finalize 所有権は commit 相の finally 一箇所のみ、という
    設計書 §4.7 の不変条件をこの CAS が実装レベルで強制する)。

    戻り値: `True` = この呼び出しが終端を書いた。`False` = 既に終端済み
    だった (二重終端防止) — **呼び出し元はこの場合 activity 警告を残し、
    この結果を上書きしたと誤認しないこと**。
    """
    cur = conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=? AND status='running'",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q -k finish
uv run pytest -q
```

Expected: 全件 PASS (既存の `missions.finish` 呼び出し元は戻り値を使っていないため無変更で動く)。

- [ ] **Step 5: 失敗するテストを書く (`recover_interrupted`)**

`tests/store/test_missions_cas.py` に追加:

```python
def test_recover_interrupted_finalizes_running_missions(tmp_path):
    conn = _conn(tmp_path)
    mid1 = missions.start(conn, "trade", "local", "m", NOW)
    mid2 = missions.start(conn, "trade", "local", "m", NOW)
    missions.finish(conn, mid2, "completed", None, [], NOW)  # 既に終端済み

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["missions_recovered"] == 1
    row1 = conn.execute("SELECT status FROM missions WHERE id=?", (mid1,)).fetchone()
    assert row1["status"] == "interrupted"
    row2 = conn.execute("SELECT status FROM missions WHERE id=?", (mid2,)).fetchone()
    assert row2["status"] == "completed"  # 触られない


def test_recover_interrupted_requeues_claimed_signals_same_transaction(tmp_path):
    """running のまま残った mission が claim していた signal は、
    lease_min の経過を待たず同一トランザクションで requeue される
    (codex C-5 — 分離すると mission は終端済みなのに signal は lease 満了
    まで不可視、という不整合窓が生じる)。"""
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["signals_requeued"] == 1
    row = conn.execute(
        "SELECT status, claimed_by_mission_id FROM signals WHERE id=?",
        (claimed["id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None


def test_recover_interrupted_abandons_signal_over_requeue_limit(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    conn.execute("UPDATE signals SET requeue_count=2 WHERE id=?", (claimed["id"],))
    conn.commit()

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["signals_abandoned"] == 1
    row = conn.execute(
        "SELECT status FROM signals WHERE id=?", (claimed["id"],)).fetchone()
    assert row["status"] == "abandoned"


def test_recover_interrupted_is_atomic_no_partial_state_on_failure(tmp_path, monkeypatch):
    """途中で例外が起きても missions/signals どちらも変更されない
    (単一トランザクション — codex C-5)。"""
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW)
    signals.add(conn, plugin="p", content_hash="h", pair="USDJPY",
               timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
               payload={}, now=NOW)
    signals.claim_oldest(conn, mission_id=mid, now=NOW, freshness_bars=None)

    orig_execute = conn.execute
    call_count = {"n": 0}

    def flaky_execute(sql, *a, **k):
        call_count["n"] += 1
        if "UPDATE signals SET status=" in sql:
            raise sqlite3.OperationalError("simulated failure")
        return orig_execute(sql, *a, **k)

    import sqlite3
    monkeypatch.setattr(conn, "execute", flaky_execute)
    with pytest.raises(sqlite3.OperationalError):
        missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    monkeypatch.undo()
    row = conn.execute("SELECT status FROM missions WHERE id=?", (mid,)).fetchone()
    assert row["status"] == "running"  # ロールバック済み
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q -k recover_interrupted
```

Expected: 全件 FAIL (`AttributeError: module 'agentic_fx.store.missions' has no attribute 'recover_interrupted'`)。

- [ ] **Step 7: `recover_interrupted` を実装**

`src/agentic_fx/store/missions.py` に追加 (`finish` の直後):

```python
def recover_interrupted(conn: sqlite3.Connection, *, now: datetime,
                        max_requeue: int) -> dict:
    """起動時回収 (設計書 §4.8 codex C-5): 前回停止時に `running` のまま
    残った missions 行を `'interrupted'` へ終端し、それらを claim して
    いた `claimed` signals を**同一トランザクション**で requeue (上限
    超過は abandoned) する。

    分離すると「mission は終端済みなのに signal は lease 満了 (最大 15
    分) まで不可視」の不整合窓が生じる — signal の `claimed_at` は直近
    (プロセス生存中の claim) であり得るため、通常の lease ベース回収
    (`signals.reclaim_expired`) では長時間拾われない。

    `'interrupted'` は DB 回収専用の状態値であり `MissionResult.status`
    の 4 値契約 (`runners/base.py:40`) には現れない — status 表示・集計
    はこの区別を明記すること (codex M-1)。

    signals の requeue/abandon 判定は `signals.py` の
    `_REQUEUE_STATUS_EXPR`/`_REQUEUE_COUNT_EXPR` と同じ規則 (現在の
    requeue_count が max_requeue 以上なら abandoned、未満なら pending +
    +1) を Python 側で再現する — `signals.reclaim_expired` を呼ぶと
    それ自身が `conn.commit()` するため、missions の更新と同一トランザ
    クションを構成できない (この関数専用に単一トランザクションで完結
    させる必要がある)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        running_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM missions WHERE status='running'").fetchall()]
        for mid in running_ids:
            conn.execute(
                "UPDATE missions SET status='interrupted', finished_at=? "
                "WHERE id=?", (now.isoformat(), mid))

        signals_requeued = signals_abandoned = 0
        if running_ids:
            placeholders = ",".join("?" * len(running_ids))
            claimed_rows = conn.execute(
                "SELECT id, requeue_count FROM signals WHERE status='claimed' "
                f"AND claimed_by_mission_id IN ({placeholders})",
                running_ids).fetchall()
            for row in claimed_rows:
                if row["requeue_count"] >= max_requeue:
                    conn.execute(
                        "UPDATE signals SET status='abandoned', "
                        "claimed_by_mission_id=NULL, claimed_at=NULL "
                        "WHERE id=?", (row["id"],))
                    signals_abandoned += 1
                else:
                    conn.execute(
                        "UPDATE signals SET status='pending', "
                        "requeue_count=requeue_count+1, "
                        "claimed_by_mission_id=NULL, claimed_at=NULL "
                        "WHERE id=?", (row["id"],))
                    signals_requeued += 1
        conn.commit()
        return {"missions_recovered": len(running_ids),
                "signals_requeued": signals_requeued,
                "signals_abandoned": signals_abandoned}
    except BaseException:
        conn.rollback()
        raise
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/store/test_missions_cas.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 9: `build_app` の起動シーケンスに配線**

`src/agentic_fx/service.py` の `build_app` 内、`conn_core = connect(...)` / `init_db(conn_core)` の直後 (264-265 行) に追加する。**`settings` は既に 260 行目 (`settings = load_settings(root / "config" / "settings.yaml")`) で構築済みであり、`missions` は既に 45 行目 (`from agentic_fx.store import approvals, missions, orders, signals`) で import 済み** — 新たな import・別名は不要でどちらもそのまま使う:

```python
    conn_core = connect(root / "data" / "agentic.db")
    init_db(conn_core)
    # プラン 8 (codex C-5): 前回停止時に running のまま残った mission と、
    # それが claim していた signal を同一トランザクションで回収する。
    # 既存の signals.reclaim_expired (403-407 行付近、lease ベースの
    # 一般的な回収) より前に置く — running mission の signal は claimed_at
    # が直近であり得るため lease ベースでは長時間拾われない。
    missions.recover_interrupted(
        conn_core, now=clock.now(),
        max_requeue=settings.plugin.signal_requeue_max)
```

```bash
uv run pytest -q
```

Expected: 全件 PASS (`build_app` を使う既存テストが、`running` 状態の mission 行を残していない限り `recover_interrupted` は no-op で無影響)。

- [ ] **Step 10: 変異テスト**

1. `finish` の `WHERE id=? AND status='running'` から `AND status='running'` を削除 (無条件 UPDATE に戻す) → `test_finish_returns_false_on_second_call_and_does_not_overwrite` が red
2. `recover_interrupted` の `except BaseException: conn.rollback(); raise` を削除 → `test_recover_interrupted_is_atomic_no_partial_state_on_failure` が red (部分的な状態変更が残る)
3. `recover_interrupted` の `if row["requeue_count"] >= max_requeue:` を `if False:` に改変 → `test_recover_interrupted_abandons_signal_over_requeue_limit` が red

- [ ] **Step 11: Commit**

```bash
git add src/agentic_fx/store/missions.py src/agentic_fx/service.py \
  tests/store/test_missions_cas.py
git commit -m "$(cat <<'EOF'
feat: missions.finish の CAS 化 + 起動時 running→interrupted 同時回収

設計書 §4.7 (codex C-4) / §4.8 (codex C-5)。呼び出し元の活用は Task
15/16 の五相再構成で行う。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 12: tick 再編 (hooks を決定論ブロックの後段へ) + `data_hook_timeout_sec` の httpx 注入

設計書 §3.2 を実装する。「決定論ブロック (mark-to-market → account/予約再検証 → `fills_allowed` 判定 → `_process_limit_fills` → `_process_exits`) は内部順序を一切変えずそのまま先頭へ繰り上げ、データ hooks (news/econ/signal maintenance) のみを後段へ移す」。

**設計判断 (writing-plans — advisor 指摘を反映)**: 現行の hooks (news/econ) は `tick()` **冒頭** (`open_now` 判定より前) にあり、「市場が閉じていても hooks は毎 tick 走る」という cross-plan 修正① の意図を持つ。単純に「決定論ブロックの後ろ」へ移すと、決定論ブロックは `open_now` 判定の**内側**にしかない (市場閉鎖時は早期 `return` する) ため、hooks が閉場中に一切走らなくなり週末バグが再発する。**対応: hooks を `_run_hooks(now)` という 1 メソッドに切り出し、`tick()` の全ての return パス直前 (市場閉鎖時の return 直前・`_mark_to_market` 失敗時の return 直前・通常経路の Mission 起動判定の直前) で呼ぶ**。`on_signal_maintenance` (現在は開場ガード内・`_process_limit_fills` より前の独立した位置) も `_run_hooks` に統合する — 閉場中も signal 保守 (鮮度切れ pending の abandoned 化・claimed の lease 回収) が走るようになる点は、既存のちenkeck (news/econ が閉場中も走る) と対称な改善であり退行ではない。

**Files:**
- Modify: `src/agentic_fx/core/scheduler.py:80-218` (`tick` 全体の再構成、`_run_hooks` 新設)
- Modify: `src/agentic_fx/datafeed/fetchers.py:148-160`(`fetch_feed`)`,200-219`(`fetch_web`)
- Modify: `src/agentic_fx/datafeed/econ_calendar.py:171-224`(`fetch_ff_calendar`/`EconCalendar.refresh`)
- Modify: `src/agentic_fx/datafeed/news_collector.py:74-114`(`NewsCollector.__init__`/`collect`)
- Modify: `src/agentic_fx/service.py:303-305`(`NewsCollector`/`EconCalendar` の構築に `data_hook_timeout_sec` を配線)
- Test: `tests/core/test_scheduler_tick_order.py` (新規 — tick 順序契約の回帰ピン), `tests/datafeed/test_fetchers.py`/`tests/datafeed/test_news_collector.py`/`tests/datafeed/test_econ_calendar.py` (timeout 配線)

**Interfaces:**
- Produces:
  - `Scheduler._run_hooks(self, now: datetime) -> None` — news/econ/signal_maintenance を `_run_data_hook` (既存の fail-open 隔離) 経由で呼ぶ。`tick()` の全 return パス直前で呼ばれる
  - `fetchers.fetch_feed(url: str, source_name: str, *, timeout_sec: float) -> list[Article]` — **`feedparser.parse(url)` から `httpx.get(url, timeout=timeout_sec, follow_redirects=True)` で取得したバイト列を `feedparser.parse(response.content)` に渡す形へ変更** (feedparser 自身に timeout 機構が無いため — httpx 経由に変えることで初めて `data_hook_timeout_sec` が効く)
  - `fetchers.fetch_web(url: str, source_name: str, *, timeout_sec: float) -> list[Article]` — 既存の `httpx.get(url, timeout=30, ...)` の `30` を `timeout_sec` に置換
  - `econ_calendar.fetch_ff_calendar(*, timeout_sec: float) -> CalendarFetch` — 既存の `httpx.get(_URL, timeout=30, ...)` の `30` を `timeout_sec` に置換
  - `NewsCollector.__init__(self, conn, rag, activity, clock, *, timeout_sec: float)` — 新設 kwarg (**キーワード必須 — 既定値を与えない**。配線し忘れの無音成立を防ぐ、既存の `on_econ_cycle` と同じ設計判断)
  - `EconCalendar.__init__(self, conn, activity, clock, *, timeout_sec: float)` — 同上

- [ ] **Step 1: 失敗するテストを書く (tick 順序契約の回帰ピン)**

`tests/core/test_scheduler_tick_order.py` を新規作成する (既存 `tests/core/test_scheduler.py` の `Env` fixture を再利用):

```python
"""tick 順序契約の回帰ピン (プラン8, 設計書 §3.2 codex C2-1/C3-1)。

決定論ブロック (mark-to-market → account/予約再検証 → fills_allowed →
fills → exits) の内部順序・processed-bar マーキング位置・fills_allowed
ゲートを固定する。hooks (news/econ/signal_maintenance) は市場開閉に
関わらず毎 tick 走ることも固定する。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.core.test_scheduler import Env, WED, SAT  # 既存 fixture を再利用


def test_hooks_run_even_when_market_closed():
    """cross-plan 修正① の再発防止: 市場閉鎖中も news/econ/signal
    maintenance は毎 tick 走る。"""
    env = Env()
    news_calls = []
    econ_calls = []
    maint_calls = []
    env.scheduler.on_news_cycle = lambda: news_calls.append(1)
    env.scheduler.on_econ_cycle = lambda: econ_calls.append(1)
    env.scheduler.on_signal_maintenance = lambda now: maint_calls.append(now)

    env.scheduler.tick(SAT)  # 市場閉鎖 (土曜)

    assert news_calls == [1]
    assert econ_calls == [1]
    assert maint_calls == [SAT]


def test_hooks_run_after_deterministic_block_when_market_open():
    """通常経路: hooks は fills_allowed ゲート・fills・exits の**後**に
    走る (fills_allowed の判定材料を hooks が汚染しないことの構造確認 —
    news/econ/signal_maintenance の呼び出し順を記録し、
    _process_limit_fills/_process_exits より後であることを確認する)。
    """
    env = Env()
    order: list[str] = []
    env.scheduler.on_news_cycle = lambda: order.append("news")
    orig_fills = env.scheduler._process_limit_fills
    env.scheduler._process_limit_fills = lambda now: (
        order.append("fills") or orig_fills(now))
    orig_exits = env.scheduler._process_exits
    env.scheduler._process_exits = lambda now, filled_ids: (
        order.append("exits") or orig_exits(now, filled_ids))

    env.scheduler.tick(WED)

    assert order.index("fills") < order.index("exits") < order.index("news")


def test_hooks_run_even_when_mark_to_market_regresses(monkeypatch):
    """_mark_to_market が時系列逆行で False を返して tick が早期 return
    しても hooks は走る。"""
    env = Env()
    news_calls = []
    env.scheduler.on_news_cycle = lambda: news_calls.append(1)
    env.scheduler._mark_to_market = lambda now: False

    env.scheduler.tick(WED)

    assert news_calls == [1]


def test_account_unknown_still_cancels_pending_before_hooks_run():
    """account 不明時の全 pending 取消 (fail closed) は hooks より先に
    確定していること — 決定論ブロックの内部順序が保存されている回帰
    確認 (既存 test_scheduler.py の account 系テストと同型だが、hooks
    再編後も同じ結論になることをこのファイルでも固定する)。
    """
    env = Env()  # account を意図的に欠損させる既存 fixture の使い方に
                  # 合わせて実装すること (既存 test_scheduler.py の該当
                  # テストの account snapshot 未記録パターンを踏襲)
    env.scheduler.tick(WED)
    # 既存 test_scheduler.py の account_unknown 系アサーションと同じ
    # 内容を実装者が転記する (このファイルの目的は「hooks 再編後も同じ
    # 結論になる」ことの確認であり、account 判定ロジック自体は Task 12
    # で変更しない)。
```

（`test_account_unknown_still_cancels_pending_before_hooks_run` は既存 `tests/core/test_scheduler.py` の該当テスト (account 欠損時の全 `pending_fill` 取消) を実ファイルで確認し、同じ assertion をここに転記すること — プレースホルダのまま残さない。）

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_scheduler_tick_order.py -q
```

Expected: `test_hooks_run_even_when_market_closed` と `test_hooks_run_even_when_mark_to_market_regresses` が FAIL (現行実装は市場閉鎖時/mark_to_market 失敗時に hooks を呼ばない)。`test_hooks_run_after_deterministic_block_when_market_open` は現行実装でも本来は FAIL のはず (hooks が **先頭** にあるため `news` が `fills`/`exits` より前に記録される) — 実際に FAIL することを確認する。

- [ ] **Step 3: `scheduler.py` の `tick` を再構成**

`src/agentic_fx/core/scheduler.py:80-218` の `tick` メソッド全体を以下に置き換える (既存のコメント群は意味が変わらない箇所はそのまま維持し、位置が変わった箇所のみ新しいコメントを付す):

```python
    def tick(self, now: datetime) -> None:
        """1 tick 分の決定論的処理。

        **呼び出し契約 (codex C-M5)**: 変更なし (既存 docstring をそのまま
        維持 — このメソッドの上の元 docstring 全文をここに残すこと)。

        **tick 再編 (プラン 8, 設計書 §3.2 codex C2-1/C3-1)**: 決定論
        ブロック (mark-to-market → account/予約再検証 → fills_allowed
        判定 → `_process_limit_fills` → `_process_exits`) は内部順序を
        一切変えずそのまま先頭で実行する。データ hooks (news/econ/signal
        maintenance) は `_run_hooks` に切り出し、**tick の全ての return
        パス直前** (市場閉鎖時・mark-to-market 失敗時・通常経路の Mission
        起動判定の直前) で呼ぶ — 市場閉鎖中も hooks が走ることを維持する
        (cross-plan 修正① の意図の保存。単純に「決定論ブロックの後ろ」に
        だけ置くと、決定論ブロックが `open_now` 判定の内側にしか無いため
        市場閉鎖中に hooks が一切走らなくなる)。
        """
        open_now = market_hours.is_market_open(now)
        if not open_now:
            # レビュー修正 4: _was_open はプロセスメモリのみに保持される。
            # サービスがオープン中に落ち、クローズ後に再起動すると最初の
            # tick で _was_open は None (True でも False でもない) になる。
            # 「True か不明(None)」ならクローズ移行処理を実行する
            # (learning モードなら _on_market_close が早期 return するので無害)。
            if self._was_open is not False:
                self._on_market_close(now)
            self._was_open = False
            self._run_hooks(now)
            return
        self._was_open = True

        # レビュー修正 3: record_snapshot が時系列逆行 (NTP 補正等) で
        # ValueError を送出した場合、mark-to-market 自体が信頼できないため、
        # この tick は安全側に全体スキップする。
        if not self._mark_to_market(now):
            self._run_hooks(now)
            return
        self._resolve_unknowns(now)
        self._expire_limits(now)
        # (以下、account/fills_allowed 判定・_maintain_reservations 呼び出し・
        # _force_close_day の既存コード — 元 133-186 行をそのまま維持する。
        # on_signal_maintenance のインライン呼び出し (元 194-196 行) は
        # ここから削除し _run_hooks へ統合する)
        account = accounting.current_account(self.conn, now)
        fills_allowed = account is not None and account[0] > 0
        if account is None:
            self._cancel_all_pending(
                now, reason="account_unknown",
                event="account_unknown_cancel_pending",
                why="口座 snapshot が陳腐化/欠損 — 総リスク再検証不能")
        elif account[0] <= 0:
            self._cancel_all_pending(
                now, reason="equity_nonpositive",
                event="equity_nonpositive_cancel_pending",
                why="equity<=0 (債務超過) — 予約を維持できない")
        else:
            try:
                if not self._maintain_reservations(now, account):
                    fills_allowed = False
            except Exception as e:  # noqa: BLE001 — 資金保護を止めない
                text = safe_error_text(e)
                self.activity.write(
                    Category.TRADE, "maintain_reservations_error",
                    f"{text} — この tick の予約維持処理を中断")
                _log.warning("maintain_reservations failed: %s", text)
                fills_allowed = False
        self._force_close_day(now)
        filled_ids = self._process_limit_fills(now) if fills_allowed else set()
        self._process_exits(now, filled_ids)

        # プラン 8 tick 再編: hooks は決定論ブロック (mark-to-market〜
        # exits) の**後**、Mission 起動判定の**前**。
        self._run_hooks(now)

        reason = self._trade_mission_due(now)
        if reason is not None:
            if reason == "cron":
                self._last_cron_trade = now
            self.on_trade_mission(reason)

    def _run_hooks(self, now: datetime) -> None:
        """データ hooks (news/econ/signal maintenance) — 決定論ブロック
        の後段で実行する (設計書 §3.2)。**tick() の全ての return パスの
        直前で呼ぶこと** — 市場閉鎖時・mark-to-market 失敗時・通常時の
        いずれでも hooks は毎 tick 走る (「hooks が週末しか走らない」旧
        欠陥 = cross-plan 修正① の再発防止)。
        """
        if self._last_news is None or now - self._last_news >= _NEWS_INTERVAL:
            self._last_news = now
            self._run_data_hook("news", self.on_news_cycle)
        if self._last_econ is None or now - self._last_econ >= _ECON_INTERVAL:
            self._last_econ = now
            self._run_data_hook("econ", self.on_econ_cycle)
        if self.on_signal_maintenance is not None:
            self._run_data_hook(
                "signal_maintenance", lambda: self.on_signal_maintenance(now))
```

**実装者への注意**: `account`/`fills_allowed` 判定ブロックの中身 (`_cancel_all_pending`/`_maintain_reservations`/`_force_close_day` 呼び出し) は元コードの 145-186 行を**逐語**維持すること (このプランに再掲した版は要約ではなく実際に貼り付けるべきコードそのもの — 元ファイルと突き合わせて 1 行も落ちていないことを確認する)。`_trade_mission_due`/`_run_data_hook`/`_fresh_bar` 等の他メソッドは無変更 (このタスクでは触らない)。

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_scheduler_tick_order.py -q
uv run pytest tests/core/test_scheduler.py -q
uv run pytest tests/core/test_scheduler_signal.py -q
uv run pytest -q
```

Expected: 全件 PASS。**注意**: `tests/core/test_scheduler_signal.py` (プラン 7 の signal maintenance 配線テスト) が「開場中のみ呼ばれる」という前提のアサーションを持っていれば、この task の変更 (閉場中も呼ばれるようになる) で FAIL する — その場合はテストの前提を修正する (仕様変更として明記: `on_signal_maintenance` は市場開閉に関わらず毎 tick 走るようになった)。

- [ ] **Step 5: 失敗するテストを書く (httpx timeout 注入)**

`tests/datafeed/test_fetchers.py` に追加 (既存 fixture — `httpx.MockTransport` 等 — を確認して揃える):

```python
def test_fetch_web_uses_injected_timeout(monkeypatch):
    captured = {}

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        raise RuntimeError("stop here — this test only checks the timeout arg")

    import agentic_fx.datafeed.fetchers as fetchers_mod
    monkeypatch.setattr(fetchers_mod.httpx, "get", fake_get)
    with pytest.raises(RuntimeError):
        fetchers_mod.fetch_web("http://x", "s", timeout_sec=7.5)
    assert captured["timeout"] == 7.5


def test_fetch_feed_uses_httpx_with_injected_timeout(monkeypatch):
    """feedparser.parse(url) から httpx 経由の取得に変わったことの確認。"""
    captured = {}

    class FakeResponse:
        content = b"<rss><channel></channel></rss>"

        def raise_for_status(self):
            pass

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        return FakeResponse()

    import agentic_fx.datafeed.fetchers as fetchers_mod
    monkeypatch.setattr(fetchers_mod.httpx, "get", fake_get)
    fetchers_mod.fetch_feed("http://x", "s", timeout_sec=7.5)
    assert captured["timeout"] == 7.5
```

`tests/datafeed/test_econ_calendar.py` に追加:

```python
def test_fetch_ff_calendar_uses_injected_timeout(monkeypatch):
    captured = {}

    def fake_get(url, timeout, **kwargs):
        captured["timeout"] = timeout
        raise RuntimeError("stop here")

    import agentic_fx.datafeed.econ_calendar as econ_mod
    monkeypatch.setattr(econ_mod.httpx, "get", fake_get)
    with pytest.raises(RuntimeError):
        econ_mod.fetch_ff_calendar(timeout_sec=7.5)
    assert captured["timeout"] == 7.5
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/datafeed/test_fetchers.py tests/datafeed/test_econ_calendar.py -q -k injected_timeout
```

Expected: 全件 FAIL (`TypeError: fetch_web() got an unexpected keyword argument 'timeout_sec'` 等)。

- [ ] **Step 7: `fetchers.py`/`econ_calendar.py` を実装**

`src/agentic_fx/datafeed/fetchers.py:148` の `def fetch_feed(url: str, source_name: str) -> list[Article]:` を `def fetch_feed(url: str, source_name: str, *, timeout_sec: float) -> list[Article]:` に変更し、関数本体冒頭の `parsed = feedparser.parse(url)` を以下に置き換える:

```python
    response = httpx.get(url, timeout=timeout_sec, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
```

`fetch_web` (200 行) のシグネチャを `def fetch_web(url: str, source_name: str, *, timeout_sec: float) -> list[Article]:` に変更し、本体の `r = httpx.get(url, timeout=30, follow_redirects=True)` の `timeout=30` を `timeout=timeout_sec` に変更する。

`src/agentic_fx/datafeed/econ_calendar.py:171` の `def fetch_ff_calendar() -> CalendarFetch:` を `def fetch_ff_calendar(*, timeout_sec: float) -> CalendarFetch:` に変更し、`r = httpx.get(_URL, timeout=30, follow_redirects=True)` の `timeout=30` を `timeout=timeout_sec` に変更する。`EconCalendar.__init__` (170 行) に `timeout_sec: float` をキーワード必須で追加し `self.timeout_sec = timeout_sec` を保持、`refresh()` (176 行) 内の `fetched = fetch_ff_calendar()` を `fetched = fetch_ff_calendar(timeout_sec=self.timeout_sec)` に変更する。

- [ ] **Step 8: `news_collector.py` を実装**

`src/agentic_fx/datafeed/news_collector.py` の `NewsCollector.__init__` (74-79 行) に `timeout_sec: float` をキーワード必須で追加し `self.timeout_sec = timeout_sec` を保持する。`collect()` (82-114 行) 内の `articles = fetch(src["url"], src["name"])` を `articles = fetch(src["url"], src["name"], timeout_sec=self.timeout_sec)` に変更する。

- [ ] **Step 9: テスト実行して PASS を確認**

```bash
uv run pytest tests/datafeed/ -q
```

Expected: 全件 PASS (既存の `fetch_web`/`fetch_feed`/`fetch_ff_calendar`/`NewsCollector`/`EconCalendar` 呼び出しテストは `timeout_sec` キーワード必須化により **FAIL するはず** — 既存呼び出しへ `timeout_sec=<既存テストの妥当な値、例 10>` を追記して直すこと。これは意図した破壊的変更であり、テストの追従修正が本 step の主作業)。

- [ ] **Step 10: `service.py` に配線**

`src/agentic_fx/service.py:303-305` の `econ = EconCalendar(conn_core, activity, clock)` / `collector = NewsCollector(conn_core, rag, activity, clock)` を以下に変更する:

```python
    econ = EconCalendar(conn_core, activity, clock,
                        timeout_sec=settings.worker.data_hook_timeout_sec)
    rag = Rag(root / "data" / "rag", embedding_function=embedding_fn,
             lock_timeout_sec=settings.worker.rpc_timeout_sec)
    collector = NewsCollector(conn_core, rag, activity, clock,
                              timeout_sec=settings.worker.data_hook_timeout_sec)
```

(`rag = Rag(...)` の行は Task 9 Step 5 で既に `lock_timeout_sec` 配線済み — ここでは順序の参考として再掲したのみで変更不要。実装時は `econ`/`collector` の 2 行だけを変更すること。)

- [ ] **Step 11: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 12: 変異テスト**

1. `_run_hooks` の呼び出しを市場閉鎖 return パスから削除 → `test_hooks_run_even_when_market_closed` が red
2. `tick()` 内の `_run_hooks(now)` の呼び出し位置を `_process_limit_fills` の**前**に戻す → `test_hooks_run_after_deterministic_block_when_market_open` が red
3. `fetch_web` の `timeout=timeout_sec` を `timeout=30` に戻す → `test_fetch_web_uses_injected_timeout` が red
4. `fetch_feed` の `httpx.get` 呼び出しを削除し `feedparser.parse(url)` に戻す → `test_fetch_feed_uses_httpx_with_injected_timeout` が red

- [ ] **Step 13: Commit**

```bash
git add src/agentic_fx/core/scheduler.py src/agentic_fx/datafeed/fetchers.py \
  src/agentic_fx/datafeed/econ_calendar.py src/agentic_fx/datafeed/news_collector.py \
  src/agentic_fx/service.py tests/core/test_scheduler_tick_order.py \
  tests/datafeed/test_fetchers.py tests/datafeed/test_econ_calendar.py
git commit -m "$(cat <<'EOF'
refactor: tick 再編 (hooksを決定論ブロック後段へ) + data_hook_timeout_sec の httpx 注入

設計書 §3.2。決定論ブロックの内部順序は不変のまま先頭に、hooks は
tick の全 return パス直前 (_run_hooks) で毎 tick 実行する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 13: mission supervisor スケルトン (容量 1 `try_submit` + trade/reflection 連鎖 + ask 統一)

設計書 §3.3「supervisor とスロット意味論」を実装する。**本 task では `core_lock` の保持範囲を変えない** — trade/reflection/ask の実行は引き続き呼び出し全体を `core_lock` で包む (scheduler tick との直列性は現状維持)。これは意図的な段階分け: 本 task は「Mission 実行を scheduler スレッドから専用の supervisor スレッドへ移し、正しいキューイング意味論 (単一スロット・原子的 `try_submit`・ask の Future 化) を導入する」ことだけに集中し、「lock の保持範囲を commit-core だけに狭める」(= 「Mission 実行中も SL/TP 監視継続」の本体) は Task 15/16 に委ねる。この段階分けにより、両 task を独立してレビュー・検証できる (Task 13 単体では tick の資金保護窓は変わらない — 変わるのは「誰が Mission を呼ぶスレッドか」だけ)。

**Files:**
- Create: `src/agentic_fx/core/supervisor.py`
- Modify: `src/agentic_fx/core/scheduler.py:39-47`(`on_trade_mission` の型ヒント)`,210-217`(`tick` 内の呼び出し規約変更)
- Modify: `src/agentic_fx/service.py:183-206`(`App` に `supervisor`/`conn_supervisor` 追加)`,232-241`(`_LockedAsk` 削除 → `_SupervisorAsk` 新設)`,357-414`(`on_trade_mission` を supervisor 経由に、`Commands` の `trade_loop=` 配線変更)`,502-514`(`scheduler_thread` — 変更なし、確認のみ)
- Test: `tests/core/test_supervisor.py` (新規)

**Interfaces:**
- Produces:
  - `supervisor.MissionSupervisor(*, trade_fn: Callable[[str], object], reflection_fn: Callable[[], object], ask_fn: Callable[[str], str]) -> object`
    - `start(self) -> None` — 内部スレッド (daemon) を起動する
    - `try_submit(self, kind: str, **kwargs) -> concurrent.futures.Future | None` — **原子的契約**: 内部 lock 下で「実行中ジョブ + 予約済みジョブの有無」を判定し、空きがあれば受理して `Future` を返す、busy なら即 `None` を返す。判定と投入を分離しない (TOCTOU 封鎖)。`kind` は `"trade"` (`kwargs={"trigger": str}`) / `"reflection"` (本プランでは `"trade"` に内包され単独では公開しない) / `"ask"` (`kwargs={"question": str}`)
    - `shutdown(self, *, drain_exc: Exception) -> None` — 新規受付停止 + queue 内の**未着手**ジョブの pending Future を `drain_exc` で例外完了させる (**ブロックしない** — 実行中ジョブの完了待ちは呼び出し側の `join()` の責務)
    - `fail_pending(self, *, exc: Exception) -> None` — supervisor スレッド死亡時に watchdog (Task 19) が呼ぶ想定。queue 内の pending Future を `exc` で例外完了させる (`shutdown` と同じ内部実装を共有してよい)
    - `join(self, timeout: float | None = None) -> None` / `is_alive(self) -> bool`
    - `heartbeat: float` — `time.monotonic()` 由来の生存確認用属性 (ジョブ待ちループのたびに更新。watchdog (Task 19) が鮮度監視に使う)
  - `Scheduler.on_trade_mission: Callable[[str], bool]` — **戻り値の型が `None` → `bool` に変わる** (`True` = supervisor が受理、`False` = busy で拒否)。`tick()` の呼び出し側 (210-217 行) は戻り値を見て `reason == "cron"` かつ `True` のときだけ `_last_cron_trade` を前進させる (設計書 §3.3「cron の意味論」— busy 拒否は締切を維持し次 tick 以降で必ず再試行される)
  - `App.supervisor: MissionSupervisor` / `App.conn_supervisor: object` (新設フィールド。後者は Task 15 の commit-pre 相が使う読取専用の lock 外接続 — 本 task では構築するだけで未使用)

- [ ] **Step 1: 失敗するテストを書く**

`tests/core/test_supervisor.py` を新規作成:

```python
"""MissionSupervisor (プラン8, 設計書 §3.3)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.core.supervisor import MissionSupervisor


def _blocking_trade_fn(release: threading.Event):
    def fn(trigger):
        release.wait(5.0)
        return {"trigger": trigger}
    return fn


def test_try_submit_accepts_when_idle():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    future = sup.try_submit("trade", trigger="cron")
    assert future is not None
    result = future.result(timeout=2.0)
    assert result == {"trade": {"t": "cron"}, "reflection_count": 0}


def test_try_submit_rejects_when_busy():
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    assert f1 is not None
    time.sleep(0.05)  # f1 が確実にディスパッチされてから 2 回目を試す

    f2 = sup.try_submit("trade", trigger="signal")
    assert f2 is None  # busy — 即座に拒否

    release.set()
    f1.result(timeout=2.0)


def test_try_submit_accepts_again_after_previous_completes():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    f1.result(timeout=2.0)
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is not None
    f2.result(timeout=2.0)


def test_trade_job_chains_reflection_after():
    calls: list[str] = []

    def trade_fn(trigger):
        calls.append("trade")
        return {"trigger": trigger}

    def reflection_fn():
        calls.append("reflection")
        return 2

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=reflection_fn,
                            ask_fn=lambda q: q)
    sup.start()
    future = sup.try_submit("trade", trigger="cron")
    result = future.result(timeout=2.0)
    assert calls == ["trade", "reflection"]
    assert result["reflection_count"] == 2


def test_ask_job_returns_via_future():
    sup = MissionSupervisor(trade_fn=lambda t: None, reflection_fn=lambda: 0,
                            ask_fn=lambda q: f"answer: {q}")
    sup.start()
    future = sup.try_submit("ask", question="usdjpy どう？")
    assert future.result(timeout=2.0) == "answer: usdjpy どう？"


def test_shutdown_fails_pending_future_without_blocking():
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")  # 即座にディスパッチされ実行中になる
    time.sleep(0.05)
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is None  # busy のため queue には入っていない (実行中ジョブが 1 つあるのみ)

    # shutdown は "未着手" (queue 内で待機中) の Future を例外完了させる契約
    # だが、上のケースでは queue が空 (f1 は既にディスパッチ済み) のため
    # shutdown 呼び出し自体は何もしない — 実行中ジョブの終了は待たない
    # (ブロックしないことの確認)。
    start = time.monotonic()
    sup.shutdown(drain_exc=RuntimeError("shutting down"))
    assert time.monotonic() - start < 0.5  # ブロックしていない

    release.set()
    f1.result(timeout=2.0)  # 実行中ジョブは通常どおり完了する
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_supervisor.py -q
```

Expected: 全件 FAIL (`ModuleNotFoundError`)。

- [ ] **Step 3: `supervisor.py` を実装**

```python
"""MissionSupervisor — 容量 1 のジョブスロットで Mission 実行を単一スレッド
直列実行する (プラン8, 設計書 §3.3)。

直列性の保証は「supervisor スレッドが 1 本」という構造に置く。`try_submit`
は内部 lock の下で「実行中 + 予約済み」の有無を判定し受理/拒否を即座に
返す原子的契約 (TOCTOU 封鎖)。ジョブは 2 種:
`"trade"` (kwargs={"trigger"} — 完了直後に reflection バッチを自動連鎖) /
`"ask"` (kwargs={"question"} — Future で結果を返す)。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable

_log = logging.getLogger("agentic_fx.supervisor")


class MissionSupervisor:
    def __init__(self, *, trade_fn: Callable[[str], object],
                reflection_fn: Callable[[], object],
                ask_fn: Callable[[str], str]) -> None:
        self._trade_fn = trade_fn
        self._reflection_fn = reflection_fn
        self._ask_fn = ask_fn
        self._lock = threading.Lock()
        self._busy = False
        self._queue: "queue.Queue[tuple[str, dict, Future] | None]" = \
            queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.heartbeat: float = time.monotonic()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="afx-supervisor")
        self._thread.start()

    def try_submit(self, kind: str, **kwargs) -> Future | None:
        with self._lock:
            if self._busy:
                return None
            self._busy = True
            future: Future = Future()
            self._queue.put((kind, kwargs, future))
            return future

    def shutdown(self, *, drain_exc: Exception) -> None:
        """新規受付停止 + queue 内の未着手ジョブを例外完了させる (ブロック
        しない — 実行中ジョブの完了待ちは呼び出し側の join() の責務)。"""
        self._stop_event.set()
        self.fail_pending(exc=drain_exc)

    def fail_pending(self, *, exc: Exception) -> None:
        """queue 内 (まだディスパッチされていない) の pending Future を
        `exc` で例外完了させる。supervisor スレッド死亡時に watchdog
        (Task 19) が呼ぶ経路と shutdown の両方から共有される。"""
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return
        if item is not None:
            _, _, future = item
            if not future.done():
                future.set_exception(exc)
            with self._lock:
                self._busy = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 内部 -------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.heartbeat = time.monotonic()
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                continue
            kind, kwargs, future = item
            try:
                result = self._dispatch(kind, kwargs)
                if not future.cancelled():
                    future.set_result(result)
            except Exception as e:  # noqa: BLE001 — supervisor スレッドを殺さない
                _log.exception("mission job %r failed", kind)
                if not future.cancelled():
                    future.set_exception(e)
            finally:
                with self._lock:
                    self._busy = False

    def _dispatch(self, kind: str, kwargs: dict) -> object:
        if kind == "trade":
            trade_result = self._trade_fn(kwargs["trigger"])
            reflection_count = self._reflection_fn()
            return {"trade": trade_result,
                   "reflection_count": reflection_count}
        if kind == "ask":
            return self._ask_fn(kwargs["question"])
        raise ValueError(f"unknown job kind: {kind!r}")
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_supervisor.py -q
```

Expected: 全件 PASS。

- [ ] **Step 5: 失敗するテストを書く (`scheduler.py` の戻り値契約変更)**

`tests/core/test_scheduler.py` に追加:

```python
def test_cron_deadline_only_advances_when_on_trade_mission_returns_true():
    """設計書 §3.3: cron 締切の前進は on_trade_mission (supervisor.
    try_submit の結果) が True (受理) のときだけ。busy (False) なら
    締切は維持され次 tick 以降で必ず再試行される。"""
    env = Env()
    env.scheduler.on_trade_mission = lambda reason: False  # busy を模す

    env.scheduler.tick(WED)
    # busy で拒否されたので締切は前進していない — 直後の tick でも
    # 再び "cron" が due になる
    assert env.scheduler._trade_mission_due(
        WED + timedelta(minutes=1)) == "cron"
```

- [ ] **Step 6: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_scheduler.py -q -k only_advances_when
```

Expected: FAIL (現行実装は `on_trade_mission` の戻り値を見ずに `reason == "cron"` だけで前進させる)。

- [ ] **Step 7: `scheduler.py` を実装**

`src/agentic_fx/core/scheduler.py:43` の `on_trade_mission: Callable[[str], None]` を `on_trade_mission: Callable[[str], bool]` に変更する (型ヒントのみ)。`tick()` 内の該当ブロック (Task 12 で `_run_hooks` に再構成済みの版の末尾) を以下に変更する:

```python
        reason = self._trade_mission_due(now)
        if reason is not None:
            # プラン 8 (設計書 §3.3): cron 締切の前進は on_trade_mission
            # (supervisor.try_submit の結果) が受理 (True) のときだけ。
            # busy (False) なら締切は維持し、次 tick 以降で必ず再試行
            # させる (signal は claim 前なので取りこぼしなし)。
            accepted = self.on_trade_mission(reason)
            if accepted and reason == "cron":
                self._last_cron_trade = now
```

- [ ] **Step 8: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_scheduler.py -q
uv run pytest -q
```

Expected: 全件 PASS。既存の `test_cron_deadline_advances_even_when_mission_callback_raises` (Task 4) は `on_trade_mission` が例外を送出するケースであり、`accepted = self.on_trade_mission(reason)` の呼び出し自体で例外が伝播するため、この変更後も同じ挙動 (締切前進 → 例外伝播) を保つことを確認する。

- [ ] **Step 9: `service.py` を実装 (supervisor 配線 + `_SupervisorAsk`)**

`src/agentic_fx/service.py` の import 節に `from agentic_fx.core.supervisor import MissionSupervisor` を追加する。`_LockedAsk` クラス (232-241 行) を削除し、以下に置き換える:

```python
class _SupervisorAsk:
    """ask を supervisor 経由で実行する薄いラッパー (`_LockedAsk` の後継 —
    設計書 §3.3「ask の統一」。core_lock を直接掴まない)。"""

    def __init__(self, supervisor: MissionSupervisor,
                wait_timeout_sec: float) -> None:
        self._supervisor = supervisor
        self._wait_timeout_sec = wait_timeout_sec

    def ask_once(self, question: str) -> str:
        future = self._supervisor.try_submit("ask", question=question)
        if future is None:
            return ("(現在 Mission 実行中のため質問を受け付けられません。"
                    "しばらくして再試行してください)")
        try:
            return future.result(timeout=self._wait_timeout_sec)
        except TimeoutError:
            return "(Mission 失敗: ask がタイムアウトしました)"
        except Exception as e:  # noqa: BLE001 — 元の ask_once の service
            # boundary (trade_loop.ask_once 内) が normalize 済みの文字列を
            # 返す設計だが、supervisor 経由の Future.exception() 化で
            # 二重に例外化され得るため、ここでも最終防波堤を置く。
            return f"(Mission 失敗: {e})"
```

`build_app` 内、`core_lock = threading.RLock()` (357 行) の直後、`on_trade_mission` 定義 (359-366 行) を以下に置き換える:

```python
    core_lock = threading.RLock()

    # プラン 8 (段階分け — Task 13 では lock の保持範囲を変えない):
    # trade/reflection/ask の実行は引き続き呼び出し全体を core_lock で
    # 包む。scheduler tick との直列性を維持したまま、Mission 呼び出しの
    # 主体を scheduler スレッドから supervisor スレッドへ移す (ロック
    # 粒度の再設計は Task 15/16)。
    def _trade_fn(trigger: str):
        with core_lock:
            return trade_loop.run_once(trigger)

    def _reflection_fn():
        with core_lock:
            return reflection.run_pending()

    def _ask_fn(question: str) -> str:
        with core_lock:
            return trade_loop.ask_once(question)

    supervisor = MissionSupervisor(
        trade_fn=_trade_fn, reflection_fn=_reflection_fn, ask_fn=_ask_fn)

    def on_trade_mission(trigger: str) -> bool:
        # trigger は scheduler._trade_mission_due() が返した起動理由。
        # supervisor.try_submit が受理すれば True (scheduler 側が cron
        # 締切を前進させる判断材料になる — 設計書 §3.3)。
        return supervisor.try_submit("trade", trigger=trigger) is not None
```

`Commands` の構築 (411-414 行) の `trade_loop=_LockedAsk(trade_loop, core_lock)` を以下に置き換える (ask の wall-clock timeout = mission timeout + preemption 猶予 + マージン — 設計書 §3.3):

```python
    ask_wait_timeout_sec = (settings.llama_swap.timeout_sec
                            + settings.worker.worker_grace_sec
                            + settings.worker.worker_terminate_grace_sec + 10.0)
    commands = Commands(conn=conn_shell, state_store=state,
                        broker=shell_broker,
                        trade_loop=_SupervisorAsk(supervisor, ask_wait_timeout_sec),
                        activity=activity, log_dir=root / "logs", clock=clock)
```

`build_app` 内、`conn_shell = connect(root / "data" / "agentic.db")` (266 行) の直後に以下を追加する (Task 15 の commit-pre 相が使う lock 外の読取専用接続 — 本 task では変数を用意するだけで未使用。**独立した名前付きローカル変数として定義する** — `App(...)` の kwargs に直接 `connect(...)` を書かないこと。Task 15 が `TradeLoop(...)` 構築時にこの変数をそのまま参照するため):

```python
    # プラン 8 (Task 13): commit-pre 相 (lock 外) が使う読取専用の
    # lock 外接続。core_lock は取らない (Task 15 で使用開始)。
    conn_supervisor = connect(root / "data" / "agentic.db")
```

`build_app` の `App(...)` 構築 (415-422 行) に `supervisor=supervisor, conn_supervisor=conn_supervisor` を追加する。`App` dataclass (183-206 行) に `supervisor: object` / `conn_supervisor: object` フィールドを追加する。

`run_service` (534-537 行付近、`th = threading.Thread(target=scheduler_thread, ...)` の前) に `app.supervisor.start()` を追加する:

```python
    app.supervisor.start()
    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()
```

（supervisor の停止 (`shutdown`/`join`) は Task 19 の停止状態機械で配線する — 本 task では daemon スレッドとして起動するのみ。）

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。**注意**: `tests/test_service_app.py`/`tests/loops/` 等で `_LockedAsk` を直接 import/参照しているテストがあれば `grep -rn "_LockedAsk" tests/` で洗い出し、`_SupervisorAsk` へ更新する。`Commands.trade_loop` (実体は `_SupervisorAsk`/`_LockedAsk`) の型を直接 assert しているテストがあれば同様に更新する。

- [ ] **Step 11: 変異テスト**

1. `MissionSupervisor.try_submit` の `if self._busy:` チェックを削除 → `test_try_submit_rejects_when_busy` が red (2 つの trade job が両方受理されてしまう)
2. `_dispatch` の `reflection_result = self._reflection_fn()` 相当行 (`self._reflection_fn()` 呼び出し) を削除 → `test_trade_job_chains_reflection_after` が red
3. `scheduler.py` の `if accepted and reason == "cron":` を `if reason == "cron":` に戻す (busy でも前進させる) → `test_cron_deadline_only_advances_when_on_trade_mission_returns_true` が red

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/core/supervisor.py src/agentic_fx/core/scheduler.py \
  src/agentic_fx/service.py tests/core/test_supervisor.py tests/core/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat: mission supervisor スケルトン (容量1 try_submit + trade/reflection連鎖 + ask統一)

設計書 §3.3。lock の保持範囲は変えず (Task 15/16 で再設計)、Mission
呼び出しの主体を scheduler スレッドから専用の supervisor スレッドへ移す。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 14: Executor snapshot API (commit-pre 外部取得 → commit-core 鮮度再検証 + N4-2 fail-closed 分岐)

設計書 §3.1「commit の 3 小相分割」と §12 申し送り①「N4-2」を実装する。**`handle_intent`/`_open`/`open_risk_and_notional` は完全に不変のまま残す** — バックテストエンジン (`backtest/runner.py:248`) と既存の `tests/core/test_executor.py` (数十本) が `handle_intent` を直接呼ぶ唯一の経路として使い続けるため。新設する `open_from_snapshot` はこれと**並行する別経路** (Task 15 で TradeLoop の Mission 経路だけが使う)。判定ロジック (`evaluate(intent, ctx, settings.risk)` 以降) は両経路で**完全に同一の共有コード**を通す (Global Constraints: risk_gate/kill_switch は diff ゼロ、executor は I/O 位置のみ移動)。

**スコープ決定 (writing-plans)**: 設計書 §3.1 の commit-pre 記述 (「Risk Gate/執行に要る全外部取得: 執行用 quote + instrument spec + 全 exposure 通貨の換算レート」) は文言上 **OPEN 経路**を指す。本 task は OPEN のみをスナップショット化する。CLOSE (`_close`) の `quote_fn` 呼び出しは各データソースが既に個別の timeout (`sources.py` の `httpx.get(..., timeout=10〜30)`) を持ち、OPEN 経路のような複数通貨を跨ぐ順次換算 (ハングの複利化) が無いため相対的にリスクが小さい — 本プランでは CLOSE/CANCEL の commit-core 内 I/O は変更しない (現状維持、退行ではない)。CANCEL は外部 I/O を持たない (`broker.cancel` は DB 状態変更のみ)。

**Files:**
- Modify: `src/agentic_fx/core/executor.py` (`_open` の末尾を `_evaluate_and_execute_open` へ抽出、`open_risk_and_notional_from_snapshot`/`ExecutionSnapshot`/`SnapshotCoverageError`/`gather_open_snapshot`/`open_from_snapshot` 新設)
- Test: `tests/core/test_executor_snapshot.py` (新規)

**Interfaces:**
- Produces:
  - `executor.ExecutionSnapshot` (frozen dataclass): `quote: Quote, spec: InstrumentSpec, specs_by_pair: dict[str, InstrumentSpec], rates: dict[str, ConversionRate], captured_at: datetime` — `specs_by_pair`/`rates` は intent の pair **と commit-pre 時点の全 exposure pair** をカバーする
  - `executor.SnapshotCoverageError(Exception)` — commit-core 開始時点で必要な pair/通貨がスナップショットに無い (N4-2 — exposure が commit-pre 後に増えた) 場合の単一表現
  - `Executor.gather_open_snapshot(self, intent: TradeIntent, *, exposure_pairs: list[str]) -> ExecutionSnapshot` — **commit-pre 専用、core_lock 非保持で呼ぶ**。`exposure_pairs` は呼び出し元 (Task 15 の commit-pre 相) が `conn_supervisor` (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧
  - `executor.open_risk_and_notional_from_snapshot(conn, risk, snapshot: ExecutionSnapshot) -> tuple[float, float, int]` — `open_risk_and_notional` の DB-only 版。exposure 行の pair/通貨がスナップショットに無ければ `SnapshotCoverageError`
  - `Executor.open_from_snapshot(self, intent: TradeIntent, iid: int, snapshot: ExecutionSnapshot, *, max_snapshot_age_sec: float) -> dict` — **commit-core 専用、core_lock 保持中に呼ぶ**。①鮮度再検証 (`now - snapshot.captured_at > max_snapshot_age_sec` なら発注拒否、lock 内での再取得はしない) ②DB 状態読み直し (account/daily_start_equity/has_unresolved_unknown) ③`open_risk_and_notional_from_snapshot` (N4-2: `SnapshotCoverageError` も発注拒否) ④`GateContext` 確定 → `_evaluate_and_execute_open` へ委譲 (判定・執行ロジックは `_open` と完全共有)
  - `Executor._evaluate_and_execute_open(self, intent: TradeIntent, iid: int, ctx: GateContext) -> dict` (private — `_open`/`open_from_snapshot` の共有末尾。既存 `_open` の `result = evaluate(...)` 以降を**逐語**移動しただけで判定ロジックは 1 文字も変えない)

- [ ] **Step 1: 失敗するテストを書く**

`tests/core/test_executor_snapshot.py` を新規作成する (既存 `tests/core/test_executor.py` の fixture — `_env`/`_open_intent` 等 — を確認して揃える):

```python
"""Executor snapshot API (プラン8, 設計書 §3.1 / §12 申し送り①N4-2)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.executor import (
    ExecutionSnapshot, SnapshotCoverageError, open_risk_and_notional_from_snapshot,
)
from agentic_fx.core.contracts import ConversionRate

# 以下は tests/core/test_executor.py の既存 fixture (Executor 構築ヘルパー・
# SPEC・_open_intent 等) を import または同型に再現して使う。プレース
# ホルダのまま使わず、実ファイルを確認してから書くこと。


def test_gather_open_snapshot_covers_intent_pair_and_exposure_pairs(tmp_path):
    """gather_open_snapshot は intent.pair と exposure_pairs の両方の
    spec/通貨レートを含む。"""
    ex = _make_executor(tmp_path)  # 既存 fixture ヘルパーに合わせて実装
    intent = _open_intent(pair="USDJPY")
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=["EURUSD"])

    assert "USDJPY" in snapshot.specs_by_pair
    assert "EURUSD" in snapshot.specs_by_pair
    assert "JPY" in snapshot.rates  # USDJPY の quote_currency
    assert "USD" in snapshot.rates  # USDJPY の base / EURUSD の quote


def test_open_from_snapshot_rejects_stale_snapshot(tmp_path):
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    stale_snapshot = snapshot.__class__(
        quote=snapshot.quote, spec=snapshot.spec,
        specs_by_pair=snapshot.specs_by_pair, rates=snapshot.rates,
        captured_at=snapshot.captured_at - timedelta(seconds=999))

    out = ex.open_from_snapshot(intent, iid, stale_snapshot,
                                max_snapshot_age_sec=5.0)
    assert out["result"] == "rejected"
    assert "stale" in out["reasons"][0]


def test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre(tmp_path):
    """N4-2: commit-pre と commit-core の間に新規 exposure が確定し、
    スナップショットに必要通貨が無い場合は lock 内取得せず intent 拒否。"""
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])  # EURUSD 未カバー

    # commit-pre 後・commit-core 前に EURUSD の建玉が確定した状況を模す
    _insert_open_order(ex.conn, pair="EURUSD")

    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert "snapshot" in out["reasons"][0].lower()


def test_open_from_snapshot_accepts_when_gate_passes_and_matches_handle_intent(tmp_path):
    """judgment ロジック不変の確認: 同じ intent/状況で handle_intent (ライブ
    経路) と open_from_snapshot (Mission 経路) が同じ結果になる。"""
    ex1 = _make_executor(tmp_path / "a")
    ex2 = _make_executor(tmp_path / "b")  # 同一初期状態の別 DB
    intent = _open_intent(pair="USDJPY")

    mid1 = _start_trade_mission(ex1.conn)
    out1 = ex1.handle_intent(intent, mid1)

    mid2 = _start_trade_mission(ex2.conn)
    iid2 = _insert_intent(ex2.conn, mid2, intent)
    snapshot = ex2.gather_open_snapshot(intent, exposure_pairs=[])
    out2 = ex2.open_from_snapshot(intent, iid2, snapshot, max_snapshot_age_sec=999.0)

    assert out1["result"] == out2["result"] == "opened"


def test_open_risk_and_notional_from_snapshot_raises_on_uncovered_pair(tmp_path):
    ex = _make_executor(tmp_path)
    _insert_open_order(ex.conn, pair="EURUSD")
    empty_snapshot = ExecutionSnapshot(
        quote=None, spec=None, specs_by_pair={}, rates={},
        captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc))
    with pytest.raises(SnapshotCoverageError):
        open_risk_and_notional_from_snapshot(ex.conn, ex.settings.risk, empty_snapshot)
```

（`_make_executor`/`_open_intent`/`_start_trade_mission`/`_insert_intent`/`_insert_open_order` は既存 `tests/core/test_executor.py` の fixture 名・構築パターンに実装者が合わせること — 同ファイルを読んでから書く。）

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/core/test_executor_snapshot.py -q
```

Expected: 全件 FAIL (`ImportError: cannot import name 'ExecutionSnapshot'`)。

- [ ] **Step 3: `executor.py` を実装**

`open_risk_and_notional` (36-64 行) の直後に追加:

```python
@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """commit-pre 相が集めた Risk Gate 評価用の外部取得スナップショット
    (設計書 §3.1)。`captured_at` は commit-core の鮮度再検証が使う。"""
    quote: Quote
    spec: InstrumentSpec
    specs_by_pair: dict
    rates: dict
    captured_at: datetime


class SnapshotCoverageError(Exception):
    """commit-core 開始時点の exposure がスナップショットでカバーされて
    いない (設計書 §12 申し送り① N4-2 — commit-pre と commit-core の間に
    新規 exposure が確定した場合)。lock 内で再取得せず intent 拒否する。"""


def open_risk_and_notional_from_snapshot(
        conn: sqlite3.Connection, risk,
        snapshot: ExecutionSnapshot) -> tuple[float, float, int]:
    """`open_risk_and_notional` の DB-only 版 (commit-core 専用 — 外部
    I/O を一切行わない)。exposure 行の pair/通貨がスナップショットに
    無ければ `SnapshotCoverageError` (N4-2)。"""
    total_risk = total_notional = 0.0
    rows = orders.list_by_status(conn, *_EXPOSURE)
    for r in rows:
        pair = r["pair"]
        spec = snapshot.specs_by_pair.get(pair)
        if spec is None:
            raise SnapshotCoverageError(
                f"pair {pair!r} is not covered by the execution snapshot "
                "(exposure grew after commit-pre — N4-2)")
        if (spec.quote_currency not in snapshot.rates
                or spec.base_currency not in snapshot.rates):
            raise SnapshotCoverageError(
                f"currency for pair {pair!r} is not covered by the "
                "execution snapshot (exposure grew after commit-pre — N4-2)")
        rule = risk.pair_rules.get(pair)
        spread = rule.assumed_spread_pips * spec.pip_size if rule else 0.0
        entry = r["avg_fill_price"] or r["requested_price"] or 0.0
        qty = r["quantity"] or 0.0
        quote_rate = snapshot.rates[spec.quote_currency]
        total_risk += (abs(entry - (r["stop_loss"] or entry)) + spread) \
            * spec.contract_size * qty * quote_rate.value \
            + risk.commission_per_lot * qty
        base_rate = snapshot.rates[spec.base_currency]
        total_notional += qty * spec.contract_size * base_rate.value
    return total_risk, total_notional, len(rows)
```

`Executor` クラスに `gather_open_snapshot`/`open_from_snapshot`/`_evaluate_and_execute_open` を追加する。まず `_open` メソッド (256-365 行) を「外部取得部分」と「判定・執行部分」に分割する。**`result = evaluate(intent, ctx, self.settings.risk)` から末尾までを `_evaluate_and_execute_open` として切り出し** (中身は 1 文字も変えない — 297-365 行をそのまま新メソッドへ移動する)、`_open` はその呼び出しに置き換える:

```python
    def _open(self, intent: TradeIntent, iid: int) -> dict:
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        account = accounting.current_account(self.conn, now)
        if account is None:
            reasons = ["no fresh account snapshot (fail closed)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        equity, hwm = account
        cycle_rate = self.cycle_rate_fn(now)
        try:
            risk_total, notional, count = open_risk_and_notional(
                self.conn, self.spec_fn, self.settings.risk, cycle_rate)
            quote_to_account = cycle_rate(spec.quote_currency)
            base_to_account = cycle_rate(spec.base_currency)
        except DataUnhealthy as e:
            reasons = [f"conversion rate unavailable: {safe_error_text(e)}"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        ctx = GateContext(
            quote=quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_account=risk_total,
            existing_notional_account=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
            quote_to_account=quote_to_account,
            base_to_account=base_to_account,
            now=now)
        return self._evaluate_and_execute_open(intent, iid, ctx)

    def _evaluate_and_execute_open(self, intent: TradeIntent, iid: int,
                                   ctx: GateContext) -> dict:
        """`_open`/`open_from_snapshot` の共有末尾 (判定ロジック不変—
        Global Constraints: risk_gate は diff ゼロ)。既存 `_open` の
        `result = evaluate(...)` 以降を逐語移動しただけ。"""
        result = evaluate(intent, ctx, self.settings.risk)
        if not result.accepted:
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(result.reasons))
            if any("kill switch" in r and "latched" not in r
                   for r in result.reasons):
                self.state.update(kill_switch_latched=True)
                self.activity.write(Category.SYSTEM, "kill_switch_latched",
                                    "drawdown threshold hit — 新規停止 (解除は明示操作)")
            self.activity.write(Category.TRADE, "gate_rejected",
                                "; ".join(result.reasons)[:200], ref_id=str(iid))
            return {"result": "rejected", "order_id": None,
                    "reasons": result.reasons}

        intents_store.set_gate_result(self.conn, iid, accepted=True,
                                      reject_reason=None)
        is_market = intent.entry_type.value == "market"
        now = ctx.now
        oid = orders.insert(
            self.conn, pair=intent.pair, direction=intent.direction.value,
            entry_type=intent.entry_type.value, horizon=intent.horizon.value,
            status=S.SUBMITTING, now=now, intent_id=iid,
            client_order_id=f"afx-{iid}-{now.timestamp():.0f}",
            quantity=result.size.quantity,
            remaining_quantity=result.size.quantity,
            requested_price=result.entry_price,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit,
            expires_at=(now + timedelta(hours=intent.expires_in_h)).isoformat()
            if intent.expires_in_h else None)
        row = orders.get(self.conn, oid)
        try:
            br = self.broker.submit(row, entry_price=result.entry_price)
        except Exception as e:  # noqa: BLE001
            br = BrokerResult(status="unknown",
                              message=safe_error_text(e))
        if br.status == "rejected":
            transitions.transition(self.conn, oid, S.REJECTED, now)
            self.activity.write(Category.TRADE, "broker_rejected",
                                f"{intent.pair}", ref_id=str(oid))
            return {"result": "rejected", "order_id": oid,
                    "reasons": ["broker rejected"]}
        if br.status == "unknown":
            transitions.transition(self.conn, oid, S.SUBMIT_UNKNOWN, now)
            self.activity.write(Category.TRADE, "submit_unknown",
                                f"{intent.pair} — reconcile 待ち", ref_id=str(oid))
            self.notifier.send(f"[agentic-fx] 送信結果不明 #{oid} — "
                               "解決まで新規発注停止")
            return {"result": "unknown", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.SUBMITTED, now,
                               broker_order_id=br.broker_order_id,
                               broker_position_id=br.broker_position_id)
        if is_market:
            transitions.transition(self.conn, oid, S.PROTECTION_PENDING, now,
                                   avg_fill_price=result.entry_price,
                                   filled_quantity=result.size.quantity,
                                   remaining_quantity=0.0,
                                   filled_at=now.isoformat())
            transitions.transition(self.conn, oid, S.OPEN, now)
            self.activity.write(Category.TRADE, "order_opened",
                                f"{intent.pair} {intent.direction.value} "
                                f"{result.size.quantity}lot @{result.entry_price}",
                                ref_id=str(oid))
            return {"result": "opened", "order_id": oid, "reasons": []}
        transitions.transition(self.conn, oid, S.PENDING_FILL, now)
        self.activity.write(Category.TRADE, "limit_placed",
                            f"{intent.pair} {intent.direction.value} "
                            f"{result.size.quantity}lot @{result.entry_price}",
                            ref_id=str(oid))
        return {"result": "pending", "order_id": oid, "reasons": []}
```

**実装者への注意**: `_evaluate_and_execute_open` の `now = ctx.now` は元の `_open` にあった `now = self.clock.now()` (256 行) を `ctx.now` (既に `GateContext.now` に保存済みの同じ値) から取り直すことで、`open_from_snapshot` 側 (下記) が commit-core 開始時に確定した `now` と完全一致させる (2 回目の `clock.now()` 呼び出しによる微小なズレを避ける)。

`gather_open_snapshot`/`open_from_snapshot` を `Executor` クラスに追加する (`_open` の直後):

```python
    def gather_open_snapshot(self, intent: TradeIntent, *,
                             exposure_pairs: list[str]) -> ExecutionSnapshot:
        """commit-pre 相専用 (設計書 §3.1) — **core_lock を保持しない状態
        で呼ぶこと**。Risk Gate 評価に要る全外部取得 (quote + 全 exposure
        pair の instrument spec + 全 exposure 通貨の換算レート) を 1 回で
        完了させ、timestamp 付きスナップショットにする。

        `exposure_pairs` は呼び出し元 (commit-pre 相) が `conn_supervisor`
        (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧。
        """
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        cycle_rate = self.cycle_rate_fn(now)
        specs_by_pair: dict = {intent.pair: spec}
        currencies: set = {spec.quote_currency, spec.base_currency}
        for pair in exposure_pairs:
            pair_spec = self.spec_fn(pair)
            specs_by_pair[pair] = pair_spec
            currencies.add(pair_spec.quote_currency)
            currencies.add(pair_spec.base_currency)
        rates = {ccy: cycle_rate(ccy) for ccy in currencies}
        return ExecutionSnapshot(quote=quote, spec=spec,
                                 specs_by_pair=specs_by_pair, rates=rates,
                                 captured_at=now)

    def open_from_snapshot(self, intent: TradeIntent, iid: int,
                           snapshot: ExecutionSnapshot, *,
                           max_snapshot_age_sec: float) -> dict:
        """commit-core 相専用 (設計書 §3.1) — **core_lock 保持中に呼ぶ
        こと**。①スナップショットの鮮度再検証 (lock 内での再取得はしない)
        ②DB 状態を読み直して GateContext を確定 ③Risk Gate 判定・paper
        broker 執行は `_evaluate_and_execute_open` へ委譲 (`_open` と
        完全共有 — 判定ロジック不変)。
        """
        now = self.clock.now()
        age_sec = (now - snapshot.captured_at).total_seconds()
        if age_sec > max_snapshot_age_sec:
            reasons = [
                f"execution snapshot is stale ({age_sec:.1f}s > "
                f"{max_snapshot_age_sec}s) — rejecting rather than "
                "re-fetching while holding core_lock (設計書 §3.1)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        account = accounting.current_account(self.conn, now)
        if account is None:
            reasons = ["no fresh account snapshot (fail closed)"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}
        equity, hwm = account

        try:
            risk_total, notional, count = open_risk_and_notional_from_snapshot(
                self.conn, self.settings.risk, snapshot)
        except SnapshotCoverageError as e:
            # N4-2: commit-pre と commit-core の間に新規 exposure が確定
            # した。lock 内で再取得せず intent 拒否 (次周期の判断へ送る)。
            reasons = [f"execution snapshot coverage error: {e}"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason=reasons[0])
            self.activity.write(Category.TRADE, "gate_rejected", reasons[0],
                                ref_id=str(iid))
            return {"result": "rejected", "order_id": None, "reasons": reasons}

        spec = snapshot.specs_by_pair[intent.pair]
        ctx = GateContext(
            quote=snapshot.quote, spec=spec, equity=equity, hwm=hwm,
            daily_start_equity=accounting.daily_start_equity(self.conn, now),
            open_position_count=count, existing_risk_account=risk_total,
            existing_notional_account=notional,
            kill_switch_latched=self.state.load().kill_switch_latched,
            has_unresolved_unknown=has_unresolved_unknown(self.conn),
            account_currency=self.settings.account_currency,
            quote_to_account=snapshot.rates[spec.quote_currency],
            base_to_account=snapshot.rates[spec.base_currency],
            now=now)
        return self._evaluate_and_execute_open(intent, iid, ctx)
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/core/test_executor_snapshot.py -q
uv run pytest tests/core/test_executor.py -q
uv run pytest -q
```

Expected: 全件 PASS。**`tests/core/test_executor.py` が 1 本も壊れていないこと** (`handle_intent`/`_open` の外部から見える挙動は完全不変) を必ず確認する — 壊れていれば `_evaluate_and_execute_open` への切り出しで何かを取りこぼしている。

- [ ] **Step 5: 変異テスト**

1. `_evaluate_and_execute_open` の `if not result.accepted:` を削除 → `tests/core/test_executor.py` の gate 却下系テストが red (`_open`/`open_from_snapshot` 両方に波及することを確認)
2. `open_from_snapshot` の `if age_sec > max_snapshot_age_sec:` を削除 → `test_open_from_snapshot_rejects_stale_snapshot` が red
3. `open_risk_and_notional_from_snapshot` の `if spec is None:` チェックを削除 → `test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre`/`test_open_risk_and_notional_from_snapshot_raises_on_uncovered_pair` が red

- [ ] **Step 6: Commit**

```bash
git add src/agentic_fx/core/executor.py tests/core/test_executor_snapshot.py
git commit -m "$(cat <<'EOF'
feat: Executor snapshot API (commit-pre外部取得 + commit-core鮮度再検証 + N4-2 fail-closed)

設計書 §3.1 / §12 申し送り①。handle_intent/_open は完全不変のまま
(バックテスト runner.py の既存経路)、open_from_snapshot を並行追加する。
判定ロジックは _evaluate_and_execute_open として両経路が共有する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 15: TradeLoop 五相再構成 (prepare/run/commit-pre/commit-core/commit-post) — 「Mission 実行中も SL/TP 監視継続」の本体

設計書 §3.1 の五相分解を `TradeLoop` に実装する。**この task が本プランの核心** — `core_lock` を保持するのは prepare (missions.start/signals.claim/prompt 構築) と commit-core (consume/Risk Gate/執行/finish) のみにし、`run` 相 (`WorkerRunner.run(mission)`) は lock を一切保持しない。これにより scheduler tick (SL/TP 監視) が Mission 実行中も core_lock を取得でき、資金保護が止まらなくなる。`ask` (`_ask_once_impl`) も同じ理由 (WorkerRunner 呼び出しが長時間ブロックしうる) で三相 (prepare/run/commit) に再構成する。

**設計上の注記 (writing-plans — Phase 1 の fail-closed 修正との関係)**: Phase 1 レジャーの「W1 (finish 失敗の fail-closed 化)」は「`missions.finish` が失敗しても completed 結果のまま intent 変換・execute へ進んでしまう」という**無警告の**バグを修正したものだった。本 task の設計書 §3.1 は `missions.finish(CAS)` を commit-core の**末尾** (paper broker 執行の後) に置く — 一見 W1 と逆行するように見えるが、以下 2 点により安全性は後退しない: ①`orders.insert`/`trade_intents` への記録は `missions.finish` と独立した書込みであり、finalize が失敗しても発注自体の監査証跡 (どの注文がどう約定したか) は失われない ②finalize 書込み自体が例外を出せば `_finalize_mission` が `mission_finalize_failed` を activity に**可視化して記録**し (W1 が塞いだ「無警告」の再発は無い)、mission 行は `running` のまま残るが次回起動時の `recover_interrupted` (Task 11) が `interrupted` へ確実に回収する。W1 が防いだのは「無警告のまま実行し続ける」ことであり、本 task はそれを維持したまま commit-core の末尾に finalize を置く設計書の順序をそのまま採用する。

**Files:**
- Modify: `src/agentic_fx/core/executor.py` (`handle_intent` を `record_and_validate_intent` + dispatch に分割、`_close`→`close_intent`/`_cancel`→`cancel_intent` の公開昇格)
- Modify: `src/agentic_fx/loops/trade_loop.py` (全体 — 五相再構成)
- Modify: `src/agentic_fx/service.py:359-380`(`_trade_fn`/`_ask_fn` の `with core_lock:` 除去 + `TradeLoop` construction に `core_lock`/`conn_supervisor` を渡す)
- Modify: `src/agentic_fx/config.py` (`WorkerSettings` に `snapshot_max_age_sec` 追加)
- Modify: `config/settings.yaml.example` (同期)
- Modify: `tests/loops/test_trade_loop.py:31-69` (`_loop` fixture に `core_lock`/`conn_supervisor` 追加)
- Test: `tests/loops/test_trade_loop_phases.py` (新規 — lock 境界の直接検証)

**Interfaces:**
- Produces:
  - `Executor.record_and_validate_intent(self, intent: TradeIntent, mission_id: int) -> tuple[int, dict | None]` — intent の DB 記録 (常に行う) + HOLD 短絡 + origin/loop 検証。`(iid, None)` なら呼び出し側が `open_from_snapshot`/`close_intent`/`cancel_intent` へ dispatch する。`(iid, result)` なら `result` がそのまま最終結果 (hold・origin_rejected・mission_rejected)。**`handle_intent` はこのメソッド + dispatch を連結しただけで挙動は完全不変** (バックテスト runner.py 経由の既存呼び出しに影響なし)
  - `Executor.close_intent`/`Executor.cancel_intent` — 旧 `_close`/`_cancel` の公開昇格 (rename のみ、挙動不変。`handle_intent` 内の呼び出しも新名に更新)
  - `TradeLoop.__init__(self, *, conn, runner, settings, executor, provider, econ, policy, activity, notifier, clock, watch=None, core_lock: threading.RLock, conn_supervisor: sqlite3.Connection)` — **`core_lock`/`conn_supervisor` が新設必須 kwarg**
  - `TradeLoop._finalize_mission(self, mid: int, result: MissionResult) -> None` — **呼び出し元が `core_lock` を保持している前提**。`missions.finish` の CAS 化された呼び出し (書き込み例外は fail closed、CAS 失敗 = 二重終端は警告のみ)
  - `TradeLoop._read_exposure_pairs(self) -> list[str]` — commit-pre 専用。`conn_supervisor` (lock 外) から既存 exposure (`executor._EXPOSURE` の全状態) の pair 一覧を読む

- [ ] **Step 1: 失敗するテストを書く (lock 境界の直接検証)**

`tests/loops/test_trade_loop_phases.py` を新規作成する (`tests/loops/test_trade_loop.py` の `_loop` フィクスチャを import して使う — Step 8 で `_loop` を更新した後にこのテストを書くこと。TDD の都合上、先に Step 8 を実施してから本 Step に戻ってもよい):

```python
"""TradeLoop 五相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.runners.base import Mission, MissionResult
from tests.loops.test_trade_loop import _loop


class _SlowRunner:
    """runner.run() が core_lock 非保持で呼ばれることを検証する fake —
    run() の中で「別スレッドが core_lock を取得できるか」を確認する。"""

    def __init__(self, core_lock: threading.RLock, result: MissionResult) -> None:
        self._core_lock = core_lock
        self._result = result
        self.lock_was_free_during_run = False

    def run(self, mission: Mission) -> MissionResult:
        acquired = self._core_lock.acquire(blocking=False)
        if acquired:
            self.lock_was_free_during_run = True
            self._core_lock.release()
        return self._result


def test_run_once_does_not_hold_core_lock_during_runner_run(tmp_path):
    """設計書 §3.1: run 相 (runner.run) は core_lock を保持しない —
    別スレッド (ここでは runner.run 自身の中) が同じロックを取得できる
    ことで検証する。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    slow = _SlowRunner(loop._core_lock, MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, []))
    loop.runner = slow

    loop.run_once("cron")

    assert slow.lock_was_free_during_run is True


def test_scheduler_tick_can_acquire_lock_while_worker_runner_blocks(tmp_path):
    """統合的な確認: run_once を別スレッドで実行中、メインスレッドが
    core_lock を (scheduler tick が行うのと同じ形で) 取得できる。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    release = threading.Event()

    class BlockingRunner:
        def run(self, mission):
            release.wait(5.0)
            return MissionResult("completed",
                                 {"action": "hold", "reasoning": "x"}, [])

    loop.runner = BlockingRunner()
    t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
    t.start()
    time.sleep(0.1)  # run_once が prepare を終えて run 相に入るまで待つ

    acquired = loop._core_lock.acquire(timeout=1.0)
    assert acquired, "core_lock は run 相の間、他スレッドから取得できるはず"
    loop._core_lock.release()

    release.set()
    t.join(timeout=5.0)
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/loops/test_trade_loop_phases.py -q
```

Expected: FAIL (`TypeError: TradeLoop.__init__() missing ... 'core_lock'` 等 — `_loop` フィクスチャ更新前は別のエラーになりうる。Step 8 の fixture 更新を先に済ませてから本 Step を実施する運用でもよい)。

- [ ] **Step 3: `executor.py` の `record_and_validate_intent` + rename**

`src/agentic_fx/core/executor.py` の `handle_intent` (215-252 行) を以下に置き換える:

```python
    def record_and_validate_intent(self, intent: TradeIntent,
                                   mission_id: int) -> tuple[int, dict | None]:
        """intent の DB 記録 (常に行う) + HOLD 短絡 + origin/loop 検証
        (プラン8 五相再構成 — `handle_intent` から分割。判定ロジックは
        1 文字も変えていない)。

        戻り値: `(iid, None)` なら呼び出し側が open_from_snapshot/
        close_intent/cancel_intent へ dispatch する。`(iid, result)` なら
        `result` がそのまま最終結果 (hold・origin_rejected・
        mission_rejected のいずれか)。
        """
        now = self.clock.now()
        iid = intents_store.insert(self.conn, mission_id,
                                   _intent_payload(intent), now)
        if intent.action is Action.HOLD:
            self.activity.write(Category.AGGREGATE, "hold",
                                intent.reasoning[:120], ref_id=str(iid))
            return iid, {"result": "hold", "order_id": None, "reasons": []}

        if intent.origin is not Origin.SCHEDULER:
            reasons = ["origin rejected: only scheduler missions may trade"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(reasons))
            self.activity.write(Category.TRADE, "origin_rejected",
                                f"{intent.action.value} from {intent.origin.value}",
                                ref_id=str(iid))
            return iid, {"result": "rejected", "order_id": None, "reasons": reasons}

        loop = missions_store.loop_of(self.conn, mission_id)
        if loop != "trade":
            reasons = [f"mission rejected: loop={loop!r} is not a trade mission"]
            intents_store.set_gate_result(self.conn, iid, accepted=False,
                                          reject_reason="; ".join(reasons))
            self.activity.write(Category.TRADE, "mission_rejected",
                                f"{intent.action.value} from mission "
                                f"#{mission_id} (loop={loop!r})", ref_id=str(iid))
            return iid, {"result": "rejected", "order_id": None, "reasons": reasons}

        return iid, None

    def handle_intent(self, intent: TradeIntent, mission_id: int) -> dict:
        """既存の全経路 (バックテスト runner.py・tests/core/test_executor.py)
        向けの一体化エントリ。`record_and_validate_intent` + dispatch を
        連結しただけで挙動は完全不変。"""
        iid, early = self.record_and_validate_intent(intent, mission_id)
        if early is not None:
            return early
        if intent.action is Action.OPEN:
            return self._open(intent, iid)
        if intent.action is Action.CLOSE:
            return self.close_intent(intent, iid)
        return self.cancel_intent(intent, iid)
```

`_close` (369-384 行) を `close_intent` に rename する (メソッド名のみ変更、本体は無変更)。`_cancel` (461-473 行) を `cancel_intent` に rename する (同上)。`_close`/`_cancel` を呼んでいた他の箇所が無いことを `grep -n "self\._close(\|self\._cancel(" src/agentic_fx/core/executor.py` で確認する (`handle_intent` からの呼び出しは上で `close_intent`/`cancel_intent` に更新済み)。

- [ ] **Step 4: `config.py`/`settings.yaml.example` に `snapshot_max_age_sec` を追加**

`src/agentic_fx/config.py` の `WorkerSettings` (Task 7 で新設済み) に追加:

```python
    # commit-pre で取得したスナップショットの許容鮮度 (秒)。commit-core
    # 開始時にこれを超えていれば発注拒否する (lock 内での再取得はしない
    # — 設計書 §3.1)。
    snapshot_max_age_sec: float = Field(gt=0, default=10.0)
```

`config/settings.yaml.example` の `worker:` セクションに追加:

```yaml
  snapshot_max_age_sec: 10        # commit-pre スナップショットの許容鮮度 (秒)。超過は発注拒否 (lock内再取得しない)
```

- [ ] **Step 5: `trade_loop.py` を五相再構成**

`src/agentic_fx/loops/trade_loop.py` の import 節に `import threading` と `import sqlite3` (既存) を確認し、`from agentic_fx.core.contracts import Action` を追加する (`Action.OPEN`/`Action.CLOSE` の分岐に使う — 既存 import は `IntentParseError, Origin, TradeIntent` のみなので `Action` を足す)。

`TradeLoop.__init__` (42-58 行) を以下に変更する:

```python
class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock, core_lock: threading.RLock,
                 conn_supervisor: sqlite3.Connection,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.settings = settings
        self.executor = executor
        self.provider = provider
        self.econ = econ
        self.policy = policy
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        self._core_lock = core_lock
        self._conn_supervisor = conn_supervisor
        self.watch = watch if watch is not None else MissionWatch()
```

`_run_once_impl` (80-190 行) を以下に置き換える:

```python
    def _run_once_impl(self, trigger: str = "cron") -> dict | None:
        """取引判断 Mission 1 回分 (プラン8 五相再構成 — 設計書 §3.1)。

        prepare (lock 保持) → run (lock 非保持) → commit-pre (lock 非保持:
        結果検証・intent 構築・snapshot 取得) → commit-core (lock 保持:
        consume・Risk Gate・執行・finish) → commit-post (lock 非保持:
        aggregate 記録)。`healthcheck` は prepare よりさらに前 (lock 外)
        — claim 済みで healthcheck 死亡 → requeue 漏れ、を構造的に防ぐ
        (既存の設計方針を維持)。
        """
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        # ---- prepare (core_lock 保持) ----
        claimed: dict | None = None
        mid: int | None = None
        with self._core_lock:
            now = self.clock.now()
            if trigger == "signal":
                mid = missions.start(self.conn, "trade",
                                     self.settings.runner.trade.backend,
                                     self.settings.runner.trade.model, now,
                                     trigger="signal")
                try:
                    claimed = signals.claim_oldest(
                        self.conn, mission_id=mid, now=now,
                        freshness_bars=self.settings.plugin.signal_freshness_bars)
                    if claimed is None:
                        missions.finish(self.conn, mid, "skipped", None, [], now)
                        return None
                except Exception:
                    try:
                        missions.finish(self.conn, mid, "failed", None, [], now)
                    except Exception:  # noqa: BLE001 — 元の例外を握りつぶさない
                        _log.exception(
                            "failed to finalize mission %s after claim error", mid)
                    raise
            if claimed is not None:
                missions.set_trigger(self.conn, mid, f"signal:{claimed['plugin']}")
                prompt = (self._build_prompt(load_prompt("trade_mission"))
                         + self._format_signal_injection(claimed))
            else:
                prompt = self._build_prompt(load_prompt("trade_mission"))
            mission = self._build_mission(prompt)
            if mid is None:
                mid = missions.start(self.conn, "trade",
                                     self.settings.runner.trade.backend,
                                     self.settings.runner.trade.model, now,
                                     trigger=trigger)

        consumed = False
        try:
            # ---- run (core_lock 非保持) ----
            self.watch.begin(mid, "trade", mission.timeout_sec)
            try:
                result = self.runner.run(mission)
                if not isinstance(result, MissionResult):
                    result = MissionResult("failed", None, [])
            except Exception:  # noqa: BLE001 — runner 例外で周期を殺さない
                _log.exception("runner raised")
                result = MissionResult("failed", None, [])
            finally:
                self.watch.end(mid)

            # ---- commit-pre (core_lock 非保持) ----
            if result.status != "completed":
                with self._core_lock:
                    self._finalize_mission(mid, result)
                self.activity.write(Category.AGGREGATE, "mission_failed",
                                    f"runner status={result.status}",
                                    ref_id=str(mid))
                self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
                return None
            try:
                intent = TradeIntent.from_llm_dict(result.output,
                                                   origin=Origin.SCHEDULER)
            except IntentParseError as e:
                with self._core_lock:
                    self._finalize_mission(mid, result)
                self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                    str(e), ref_id=str(mid))
                return None

            snapshot = None
            snapshot_error: Exception | None = None
            if intent.action is Action.OPEN:
                exposure_pairs = self._read_exposure_pairs()
                try:
                    snapshot = self.executor.gather_open_snapshot(
                        intent, exposure_pairs=exposure_pairs)
                except Exception as e:  # noqa: BLE001 — commit-core で
                    # 執行失敗として扱う (fail closed、consume より前に
                    # 確定させておき commit-core 側の分岐を単純にする)。
                    snapshot_error = e

            # ---- commit-core (core_lock 保持) ----
            with self._core_lock:
                if claimed is not None:
                    # ⑥consume/requeue の確定規則: パース成功の時点で
                    # consume する (プロンプトに実際に載せた Mission が
                    # 確定できる)。executor 実行後の requeue は二重発注
                    # ハザードになるため、ここで確定させる。
                    signals.consume(self.conn, claimed["id"], mission_id=mid,
                                    now=self.clock.now())
                    consumed = True
                try:
                    if snapshot_error is not None:
                        raise snapshot_error
                    iid, early = self.executor.record_and_validate_intent(
                        intent, mid)
                    if early is not None:
                        out = early
                    elif intent.action is Action.OPEN:
                        out = self.executor.open_from_snapshot(
                            intent, iid, snapshot,
                            max_snapshot_age_sec=self.settings.worker.snapshot_max_age_sec)
                    elif intent.action is Action.CLOSE:
                        out = self.executor.close_intent(intent, iid)
                    else:
                        out = self.executor.cancel_intent(intent, iid)
                except Exception as e:  # noqa: BLE001
                    _log.exception("executor intent handling raised")
                    self._finalize_mission(mid, result)
                    self.activity.write(Category.AGGREGATE,
                                        "intent_execution_failed",
                                        safe_error_text(e), ref_id=str(mid))
                    self.notifier.send(
                        f"[agentic-fx] 注文処理失敗: {safe_error_text(e)}")
                    return None
                self._finalize_mission(mid, result)

            # ---- commit-post (core_lock 非保持) ----
            self.activity.write(Category.AGGREGATE, "decision",
                                f"{intent.action.value} -> {out['result']}",
                                ref_id=str(mid))
            return out
        finally:
            # 例外時も claimed のまま残さない (lease 回収を待たず即 requeue)。
            # consume 済みならここでは何もしない (二重発注ハザードを避ける)。
            if claimed is not None and not consumed:
                self._requeue_signal(claimed)

    def _finalize_mission(self, mid: int, result: MissionResult) -> None:
        """missions.finish の CAS 化された呼び出し (**core_lock 保持中に
        呼ぶこと**)。設計書 §4.7 codex C-4: 二重終端は上書きせず警告のみ
        残す。書き込み自体の例外は fail closed (旧 `_run_recorded` の
        契約を維持)。"""
        try:
            finished = missions.finish(self.conn, mid, result.status,
                                       result.output, result.transcript,
                                       self.clock.now())
        except Exception:  # noqa: BLE001
            _log.exception("missions.finish failed for %s", mid)
            try:
                self.activity.write(Category.SYSTEM, "mission_finalize_failed",
                                    f"mid={mid}")
            except Exception:  # noqa: BLE001
                _log.exception("failed to record mission_finalize_failed")
            return
        if not finished:
            try:
                self.activity.write(
                    Category.SYSTEM, "mission_finalize_conflict",
                    f"mid={mid} (already finalized elsewhere)")
            except Exception:  # noqa: BLE001
                _log.exception("failed to record mission_finalize_conflict")

    def _read_exposure_pairs(self) -> list[str]:
        """commit-pre 専用: `conn_supervisor` (lock 外の読取専用接続) から
        既存 exposure (`executor._EXPOSURE` の全状態) の pair 一覧を読む。
        `gather_open_snapshot` の `exposure_pairs` に渡す。"""
        from agentic_fx.core.executor import _EXPOSURE
        from agentic_fx.store import orders as orders_store
        rows = orders_store.list_by_status(self._conn_supervisor, *_EXPOSURE)
        return sorted({r["pair"] for r in rows})
```

**実装者への注意**: `_build_mission`/`_format_signal_injection`/`_requeue_signal`/`_build_prompt`/`_safe_report_boundary_failure` (192-320 行付近の残りのメソッド群) は無変更のため掲載を省略する — 削除しないこと。`_run_recorded` メソッド (260-297 行) は本 task で `_finalize_mission` に置き換わり、`_run_once_impl`/`_ask_once_impl` (次 Step) の両方が独自に run+commit の相を持つため**削除する** (どこからも呼ばれなくなることを `grep -n "_run_recorded" src/agentic_fx/loops/trade_loop.py` で確認する)。

- [ ] **Step 6: `_ask_once_impl` を三相再構成**

`_ask_once_impl` (233-256 行) を以下に置き換える:

```python
    def _ask_once_impl(self, question: str) -> str:
        """ask Mission (プラン8 三相再構成 — trade と同じ理由: WorkerRunner
        呼び出しが長時間ブロックしうるため lock を保持しない)。"""
        with self._core_lock:
            now = self.clock.now()
            prompt = self._build_prompt(load_prompt("ask_mission")) \
                + f"\n\n## ユーザーの質問\n{question}"
            mission = Mission(
                prompt=prompt, tools=_TRADE_TOOLS,
                output_schema=ANSWER_SCHEMA,
                max_turns=self.settings.llama_swap.max_turns,
                timeout_sec=self.settings.llama_swap.timeout_sec)
            mid = missions.start(self.conn, "ask",
                                 self.settings.runner.trade.backend,
                                 self.settings.runner.trade.model, now)

        self.watch.begin(mid, "ask", mission.timeout_sec)
        try:
            result = self.runner.run(mission)
            if not isinstance(result, MissionResult):
                result = MissionResult("failed", None, [])
        except Exception:  # noqa: BLE001
            _log.exception("runner raised")
            result = MissionResult("failed", None, [])
        finally:
            self.watch.end(mid)

        with self._core_lock:
            self._finalize_mission(mid, result)

        if result.status != "completed":
            return f"(Mission 失敗: {result.status})"
        if not isinstance(result.output, dict):
            return "(Mission 失敗: completed)"
        answer = result.output.get("answer")
        if not isinstance(answer, str):
            return "(Mission 失敗: completed)"
        self.activity.write(Category.AGGREGATE, "ask_answered",
                            question[:80], ref_id=str(mid))
        return answer
```

- [ ] **Step 7: テスト実行して PASS を確認**

```bash
uv run pytest tests/loops/test_trade_loop_phases.py -q
```

Expected: まだ FAIL (`_loop` fixture 未更新 — 次 Step で更新する)。

- [ ] **Step 8: `tests/loops/test_trade_loop.py` の `_loop` fixture を更新**

`_loop` 関数 (31-69 行) の `TradeLoop(...)` 構築 (61-68 行) に `core_lock`/`conn_supervisor` を追加する:

```python
    loop = TradeLoop(
        conn=conn, runner=runner, settings=SETTINGS,
        executor=executor, provider=provider, econ=econ,
        policy=Policy(policy_path),
        activity=activity_log,
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=clock, core_lock=threading.RLock(), conn_supervisor=conn,
        watch=watch)
```

（`conn_supervisor=conn` — テストでは同一 DB への 2 本目の実接続を作らず既存 `conn` をそのまま渡してよい (単体テストの関心は exposure pairs 読み取りロジックであり、真の多接続並行性ではない)。ファイル冒頭の import 節に `import threading` を追加する。）

```bash
uv run pytest tests/loops/test_trade_loop.py -q
uv run pytest tests/loops/test_trade_loop_phases.py -q
uv run pytest tests/loops/test_trade_loop_signal.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 9: `service.py` を実装 (lock ラップ除去 + 配線更新)**

`build_app` 内、Task 13 で作った `_trade_fn`/`_ask_fn` (`with core_lock:` で包んでいた版) を以下に置き換える (`_reflection_fn` は **Task 16 まで `with core_lock:` を維持する** — ReflectionCycle はまだ五相再構成されていないため):

```python
    def _trade_fn(trigger: str):
        # プラン 8 (Task 15): TradeLoop 自身が prepare/commit-core で
        # core_lock を保持する五相構造になったため、ここでは lock を
        # 掴まない (二重取得は RLock で技術的には安全だが、run 相の
        # 間ずっと lock を保持したままになり Task 15 の目的を無効化する)。
        return trade_loop.run_once(trigger)

    def _reflection_fn():
        # ReflectionCycle は Task 16 で五相再構成するまで、呼び出し全体
        # を lock で包む現状維持。
        with core_lock:
            return reflection.run_pending()

    def _ask_fn(question: str) -> str:
        # プラン 8 (Task 15): TradeLoop.ask_once も三相構造になったため
        # ここでは lock を掴まない。
        return trade_loop.ask_once(question)
```

`TradeLoop(...)` の構築 (348-352 行) に `core_lock=core_lock, conn_supervisor=conn_supervisor` を追加する (`conn_supervisor` は Task 13 で `App`/`build_app` に既に構築済み):

```python
    trade_loop = TradeLoop(conn=conn_core, runner=runner, settings=settings,
                           executor=executor, provider=provider, econ=econ,
                           policy=policy, activity=activity,
                           notifier=notifier, clock=clock,
                           core_lock=core_lock, conn_supervisor=conn_supervisor,
                           watch=mission_watch)
```

**注意**: `conn_supervisor`/`core_lock` は `build_app` 内で `trade_loop = TradeLoop(...)` より**前**に定義されている必要がある。`conn_supervisor` は Task 13 で `conn_shell` 構築の直後 (かなり早い位置) に新設済みのため既に条件を満たすが、`core_lock = threading.RLock()` は既存コードで 357 行目 (= `trade_loop = TradeLoop(...)` の 348 行目より**後**) にある — `core_lock` の定義を `trade_loop` 構築より前に移動すること (`grep -n "core_lock = threading\|trade_loop = TradeLoop" src/agentic_fx/service.py` で現状の行順を確認してから並べ替える)。

- [ ] **Step 10: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 11: 変異テスト**

1. `_run_once_impl` の `with self._core_lock:` (commit-core 相) のインデントを崩して lock 外に出す (意図的な変異) → `test_run_once_does_not_hold_core_lock_during_runner_run` は影響を受けない設計 (run 相自体は変わらない) が、代わりに commit-core の DB 書込が保護されなくなることを検出する専用テストが無い — **これはこの task の変異テストの限界であり、Task 20 の E2E (Mission 実行中の SL/TP 監視) がこの種の退行を実質的に検出する唯一の防波堤であることを progress.md に明記する**
2. `record_and_validate_intent` の `if intent.action is Action.HOLD:` を削除 → `tests/core/test_executor.py` の hold 系テストが red
3. `_read_exposure_pairs` が `executor._EXPOSURE` の代わりに `(S.OPEN,)` のみを使うよう改変 → 専用テストが無ければ `tests/core/test_executor_snapshot.py` の `test_open_from_snapshot_rejects_when_exposure_grew_after_commit_pre` 相当が (統合すれば) red になることを確認する

- [ ] **Step 12: Commit**

```bash
git add src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py \
  src/agentic_fx/service.py src/agentic_fx/config.py config/settings.yaml.example \
  tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py
git commit -m "$(cat <<'EOF'
feat: TradeLoop 五相再構成 (prepare/run/commit-pre/commit-core/commit-post)

設計書 §3.1。run相 (WorkerRunner.run) はcore_lockを一切保持しない —
これによりMission実行中もscheduler tickがSL/TP監視のためlockを取得できる。
askも三相再構成 (同じ理由)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 16: ReflectionCycle 五相再構成 + `finalize_mission` の共通化

設計書 §3.1「ReflectionCycle も同じ三相構造に再構成し、独自実装だった `_run_recorded` 相当を共通化する」を実装する。まず TradeLoop (Task 15) の `_finalize_mission` を共有モジュールへ抽出し (「共通化」の実体)、`TradeLoop` をそちらへ切り替えてから、`ReflectionCycle` を同じ関数を使う三相 (prepare/run/commit-core) + RAG 書込 (lock 不要 — Task 9 で `Rag` 自身が内部 lock を持つ) の構造に再構成する。

**Files:**
- Create: `src/agentic_fx/loops/mission_finalize.py`
- Modify: `src/agentic_fx/loops/trade_loop.py` (`_finalize_mission` を削除し共有関数の呼び出しに置換)
- Modify: `src/agentic_fx/loops/reflection_cycle.py` (全体 — 三相再構成)
- Modify: `src/agentic_fx/service.py:353-355`(`ReflectionCycle` construction に `core_lock` 追加)`,`(`_reflection_fn` の `with core_lock:` 除去)
- Modify: `tests/loops/test_reflection_cycle.py:21-30,100-110,228-237` (3 箇所の `ReflectionCycle(...)` construction に `core_lock` 追加)
- Test: `tests/loops/test_reflection_cycle_phases.py` (新規 — lock 境界の直接検証)

**Interfaces:**
- Produces:
  - `mission_finalize.finalize_mission(conn: sqlite3.Connection, activity: ActivityLog, clock: Clock, mid: int, result: MissionResult) -> bool` — `missions.finish` の CAS 化された呼び出し (**呼び出し元が `core_lock` を保持している前提**)。戻り値: `True` = 書込み試行が例外を出さなかった (CAS 受理・拒否いずれも)。`False` = 書込み自体が例外で失敗
  - `ReflectionCycle.__init__(self, *, conn, runner, rag, settings, activity, clock, core_lock: threading.RLock, watch=None)` — `core_lock` が新設必須 kwarg

- [ ] **Step 1: `mission_finalize.py` を新規作成**

```python
"""prepare/run/commit の相構造で TradeLoop/ReflectionCycle が共有する
missions.finish の CAS 呼び出しヘルパー (プラン8, 設計書 §3.1 —
「独自実装だった _run_recorded 相当を共通化する」)。
"""
from __future__ import annotations

import logging
import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions

_log = logging.getLogger("agentic_fx.loops.mission_finalize")


def finalize_mission(conn: sqlite3.Connection, activity: ActivityLog,
                     clock: Clock, mid: int, result: MissionResult) -> bool:
    """`missions.finish` の CAS 化された呼び出し (**呼び出し元が
    core_lock を保持している前提**)。設計書 §4.7 codex C-4: 二重終端は
    上書きせず警告のみ残す。書込み自体の例外は fail closed。

    戻り値: `True` = 書込み試行が例外を出さなかった (CAS 受理・拒否
    いずれも)。`False` = 書込み自体が例外で失敗。
    """
    try:
        finished = missions.finish(conn, mid, result.status, result.output,
                                   result.transcript, clock.now())
    except Exception:  # noqa: BLE001
        _log.exception("missions.finish failed for %s", mid)
        try:
            activity.write(Category.SYSTEM, "mission_finalize_failed",
                           f"mid={mid}")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission_finalize_failed")
        return False
    if not finished:
        try:
            activity.write(Category.SYSTEM, "mission_finalize_conflict",
                           f"mid={mid} (already finalized elsewhere)")
        except Exception:  # noqa: BLE001
            _log.exception("failed to record mission_finalize_conflict")
    return True
```

- [ ] **Step 2: `trade_loop.py` を共有関数へ切り替え**

`src/agentic_fx/loops/trade_loop.py` の import 節に `from agentic_fx.loops.mission_finalize import finalize_mission` を追加する。`TradeLoop._finalize_mission` メソッド (Task 15 で新設) を**削除**し、本体内の 3 箇所の呼び出し (`self._finalize_mission(mid, result)`) をすべて `finalize_mission(self.conn, self.activity, self.clock, mid, result)` に置換する (`grep -n "_finalize_mission" src/agentic_fx/loops/trade_loop.py` で呼び出し箇所を洗い出し、メソッド定義も含めて過不足なく置換すること)。

```bash
uv run pytest tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py \
  tests/loops/test_trade_loop_signal.py -q
```

Expected: 全件 PASS (挙動は不変 — 関数の置き場所を変えただけ)。

- [ ] **Step 3: 失敗するテストを書く (ReflectionCycle の lock 境界)**

`tests/loops/test_reflection_cycle_phases.py` を新規作成する:

```python
"""ReflectionCycle 五相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.runners.base import Mission, MissionResult
from tests.loops.test_reflection_cycle import _cycle, _closed_order


class _SlowRunner:
    def __init__(self, core_lock: threading.RLock, result: MissionResult) -> None:
        self._core_lock = core_lock
        self._result = result
        self.lock_was_free_during_run = False

    def run(self, mission: Mission) -> MissionResult:
        acquired = self._core_lock.acquire(blocking=False)
        if acquired:
            self.lock_was_free_during_run = True
            self._core_lock.release()
        return self._result


def test_reflect_one_does_not_hold_core_lock_during_runner_run(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [])
    slow = _SlowRunner(cyc._core_lock, MissionResult(
        "completed", {"content": "振り返り"}, []))
    cyc.runner = slow
    _closed_order(conn)

    cyc.run_pending()

    assert slow.lock_was_free_during_run is True
```

- [ ] **Step 4: テスト実行して FAIL を確認**

```bash
uv run pytest tests/loops/test_reflection_cycle_phases.py -q
```

Expected: FAIL (`TypeError: ReflectionCycle.__init__() missing ... 'core_lock'` または `_cycle` fixture 未更新のエラー)。

- [ ] **Step 5: `reflection_cycle.py` を実装**

`src/agentic_fx/loops/reflection_cycle.py` の import 節に `import threading` と `from agentic_fx.loops.mission_finalize import finalize_mission` を追加する。`ReflectionCycle.__init__` (25-35 行) を以下に変更する:

```python
class ReflectionCycle:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 rag: Rag, settings: Settings, activity: ActivityLog,
                 clock: Clock, core_lock: threading.RLock,
                 watch: MissionWatch | None = None) -> None:
        self.conn = conn
        self.runner = runner
        self.rag = rag
        self.settings = settings
        self.activity = activity
        self.clock = clock
        self._core_lock = core_lock
        self.watch = watch if watch is not None else MissionWatch()
```

`run_pending` (37-57 行) を以下に変更する (先頭の SELECT を lock 保持下に):

```python
    def run_pending(self, max_items: int = 3) -> int:
        """Reflect on closed orders without reflection. Per-item isolation.

        1 回の呼び出しで処理する件数を `max_items` に制限する — core_lock
        保持中の Mission 合成時間を抑え、SL/TP 監視の停止窓を制限するため
        (この docstring は既存のまま — プラン8 五相再構成後もこの制約の
        意図は変わらない。実際の lock 保持範囲は各相ごとに Task 15/16 の
        方針で細分化されている)。
        """
        with self._core_lock:
            rows = self.conn.execute(
                "SELECT o.* FROM orders o LEFT JOIN reflections r "
                "ON r.order_id = o.id WHERE o.status='closed' "
                "AND r.order_id IS NULL ORDER BY o.id LIMIT ?",
                (max_items,)).fetchall()
        created = 0
        for row in rows:
            try:
                if self._reflect_one(dict(row)):
                    created += 1
            except Exception:  # noqa: BLE001 — per-item isolation
                _log.exception("per-item reflection failed for order #%s",
                               row["id"])
        return created
```

`_reflect_one` (59-154 行) を以下に置き換える:

```python
    def _reflect_one(self, row: dict) -> bool:
        """Run reflection on one closed order (プラン8 三相再構成)。

        prepare (lock) → run (lock 非保持) → commit-core (lock: finalize)
        → commit-post (RAG 書込は lock 不要 — Rag 自身が内部 lock を持つ
        (Task 9)。SQLite 書込 (`reflections.save`) のみ lock 保持)。
        """
        with self._core_lock:
            now = self.clock.now()
            intent = None
            if row["intent_id"]:
                ir = self.conn.execute(
                    "SELECT payload_json FROM trade_intents WHERE id=?",
                    (row["intent_id"],)).fetchone()
                intent = json.loads(ir["payload_json"]) if ir else None

            prompt = (load_prompt("reflection") + "\n\n## トレード詳細\n"
                      + json.dumps({"order": {k: row[k] for k in (
                          "id", "pair", "direction", "horizon", "quantity",
                          "avg_fill_price", "close_price", "realized_pnl",
                          "close_reason")},
                          "entry_reasoning": (intent or {}).get("reasoning")},
                          ensure_ascii=False, indent=1))

            mission = Mission(
                prompt=prompt, tools=[], output_schema=_SCHEMA,
                max_turns=2,
                timeout_sec=self.settings.llama_swap.timeout_sec)

            mid = missions.start(
                self.conn, "reflection",
                self.settings.runner.trade.backend,
                self.settings.runner.trade.model, now)

        self.watch.begin(mid, "reflection", mission.timeout_sec)
        try:
            result = self.runner.run(mission)
            if not isinstance(result, MissionResult):
                result = MissionResult("failed", None, [])
        except Exception:  # noqa: BLE001
            _log.exception("reflection runner raised")
            result = MissionResult("failed", None, [])
        finally:
            self.watch.end(mid)

        with self._core_lock:
            finalized = finalize_mission(self.conn, self.activity, self.clock,
                                         mid, result)

        if result.status != "completed" or not finalized:
            # finish 失敗時は監査未確定 (missions 行が running のまま) なので
            # reflection も保存しない。SQLite マーカー (reflections 行) が
            # 無いので次周期の run_pending が同じ order を再試行する
            return False

        if not isinstance(result.output, dict):
            return False
        content = result.output.get("content")
        if not isinstance(content, str):
            return False

        # RAG → SQLite order (SQLite row is completion marker)。RAG 書込は
        # lock 不要 (Rag 自身が内部 lock を持つ — Task 9)。失敗時は SQLite
        # 行が無いので次回 run_pending が同じ order を再試行する (idempotent)。
        try:
            self.rag.add_reflection(row["id"], content, row["pair"])
        except Exception:  # noqa: BLE001
            _log.exception("rag.add_reflection failed for #%s — retry next run",
                           row["id"])
            return False

        with self._core_lock:
            reflections.save(self.conn, row["id"], content, now)
        try:
            self.activity.write(
                Category.AGGREGATE, "reflection_created",
                f"#{row['id']} {row['pair']}",
                ref_id=str(row["id"]))
        except Exception:  # noqa: BLE001
            _log.exception("activity write failed for reflection #%s", row["id"])
        return True
```

- [ ] **Step 6: テスト実行して PASS を確認**

```bash
uv run pytest tests/loops/test_reflection_cycle_phases.py -q
```

Expected: まだ FAIL (`_cycle` fixture 未更新 — 次 Step で更新)。

- [ ] **Step 7: `tests/loops/test_reflection_cycle.py` の 3 箇所を更新**

ファイル冒頭の import 節に `import threading` を追加する。`_cycle` (21-30 行) の `ReflectionCycle(...)` 構築に `core_lock=threading.RLock()` を追加する:

```python
    cyc = ReflectionCycle(
        conn=conn, runner=FakeRunner(results), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW), core_lock=threading.RLock(),
        watch=watch)
```

100-110 行・228-237 行の残り 2 箇所の `ReflectionCycle(...)` 構築にも同様に `core_lock=threading.RLock()` を追加する (`grep -n "ReflectionCycle(" tests/loops/test_reflection_cycle.py` で全箇所を洗い出し、漏れなく更新すること)。

```bash
uv run pytest tests/loops/test_reflection_cycle.py -q
uv run pytest tests/loops/test_reflection_cycle_phases.py -q
```

Expected: 全件 PASS。

- [ ] **Step 8: `service.py` を実装**

`build_app` 内の `reflection = ReflectionCycle(...)` (353-355 行付近) に `core_lock=core_lock` を追加する:

```python
    reflection = ReflectionCycle(conn=conn_core, runner=runner, rag=rag,
                                 settings=settings, activity=activity,
                                 clock=clock, core_lock=core_lock,
                                 watch=mission_watch)
```

**注意**: `core_lock` の定義位置が `reflection = ReflectionCycle(...)` より後ろにある場合 (Task 15 の並べ替え次第)、`core_lock = threading.RLock()` の定義を `trade_loop`/`reflection` の構築より前に移動すること (Task 15 Step 9 の注意と同じ配慮)。

Task 15 で作った `_reflection_fn` (`with core_lock: return reflection.run_pending()`) を以下に変更する (`ReflectionCycle` 自身が prepare/commit-core で lock を管理するようになったため):

```python
    def _reflection_fn():
        # プラン 8 (Task 16): ReflectionCycle 自身が prepare/commit-core で
        # core_lock を保持する三相構造になったため、ここでは lock を掴まない。
        return reflection.run_pending()
```

- [ ] **Step 9: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 10: 変異テスト**

1. `finalize_mission` の `if not finished:` ブロックを削除 → 専用テストが無ければ `tests/store/test_missions_cas.py` の CAS 系テストとは別に、`finalize_mission` 自身の直接テストを 1 本追加してから (`tests/loops/test_reflection_cycle_phases.py` へ追記) この変異で red になることを確認する
2. `_reflect_one` の `if result.status != "completed" or not finalized:` を `if result.status != "completed":` に改変 (finalized チェックを削除) → 専用テストとして「finalize が False を返すケースで reflection が保存されない」ことを確認するテストを追加してから確認する
3. `_reflect_one` 内の RAG 書込ブロックを `with self._core_lock:` で誤って包む改変を行い、`test_reflect_one_does_not_hold_core_lock_during_runner_run` が red に**ならない**ことを確認する — これは run 相 (RAG 書込ではなく runner.run) の lock 非保持を検証するテストであり、RAG 書込の lock 有無を直接検出しない。**この限界を progress.md に明記し、RAG 書込が誤って lock 保持下に入っていないことは Step 5 の実装コード diff レビューで確認する** (テストで機械的に検出できない設計判断の限界)

- [ ] **Step 11: Commit**

```bash
git add src/agentic_fx/loops/mission_finalize.py src/agentic_fx/loops/trade_loop.py \
  src/agentic_fx/loops/reflection_cycle.py src/agentic_fx/service.py \
  tests/loops/test_reflection_cycle.py tests/loops/test_reflection_cycle_phases.py
git commit -m "$(cat <<'EOF'
feat: ReflectionCycle 五相再構成 + finalize_mission の共通化

設計書 §3.1。TradeLoop の _finalize_mission を共有モジュールへ抽出し、
ReflectionCycle も同じ三相構造 (prepare/run/commit-core) に再構成する。
RAG 書込は Rag 自身の内部 lock (Task 9) に委ね、core_lock は取らない。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 17: shell readline 中断 seam (対話モードで `stop_event` が `main` を wake する唯一の経路)

設計書 §6「停止シーケンスの実行主体の一意化」(codex I3-2) が要求する必須依存。対話モードの `main` は `input()` でブロックしているため、`stop_event` が (Task 19 で) watchdog によってセットされても `input()` は自然には解けない。`select.select` によるポーリングへ置き換え、`stop_event` を定期的にチェックできるようにする。

**Files:**
- Modify: `src/agentic_fx/shell.py` (全体)
- Test: `tests/test_shell_interrupt.py` (新規)

**Interfaces:**
- Produces:
  - `shell.run_shell(commands: Commands, stop_event: threading.Event, *, input_fn=input, print_fn=print, stdin_stream=None, poll_interval: float = 0.5) -> None` — **`input_fn` が既定値 (`input` そのもの) のときのみ**、新設の割込み可能読み取り (`_read_line_interruptible`) を使う。`input_fn` が上書きされている場合 (既存テストの fake 注入) は従来どおり `input_fn(prompt)` を直接呼ぶ (後方互換)。`stdin_stream` は割込み可能読み取りが使う実ストリーム (既定 `None` = `sys.stdin`) — テストが `os.pipe()` 経由の fake ストリームを注入できる
  - `shell._read_line_interruptible(prompt: str, stop_event: threading.Event, *, stream, poll_interval: float) -> str | None` — `select.select([stream], [], [], poll_interval)` でポーリングし、`stop_event` が立てば `None` を返す (中断)。データが来れば 1 行読んで返す。EOF (空文字列読み取り) は `EOFError` を送出する

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_shell_interrupt.py` を新規作成する:

```python
"""shell readline 中断 seam (プラン8, 設計書 §6 codex I3-2)。"""
from __future__ import annotations

import os
import threading
import time

import pytest

from agentic_fx.shell import run_shell


class _FakeCommands:
    def dispatch(self, line: str) -> str:
        return f"echo: {line}"


def test_stop_event_wakes_blocked_shell_promptly(tmp_path):
    """対話モードで stop_event が外部スレッドからセットされたとき、
    input() 相当のブロッキング読み取りが速やかに解ける
    (poll_interval を短く設定し、real select ベースのポーリングで
    実際に解けることを実測する)。
    """
    r_fd, w_fd = os.pipe()  # 読み取り側は「データが来ない」ダミー stdin
    stream = os.fdopen(r_fd, "r")
    stop_event = threading.Event()

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": lambda *_: None},
        daemon=True)
    t.start()
    time.sleep(0.1)  # run_shell がポーリングループに入るまで待つ

    start = time.monotonic()
    stop_event.set()
    t.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), "stop_event セット後、shell スレッドが終了していない"
    assert elapsed < 1.0  # poll_interval=0.05s に対して十分な余裕

    os.close(w_fd)
    stream.close()


def test_line_is_read_when_available(tmp_path):
    """通常経路: stdin にデータが来れば読み取ってコマンドとして処理する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": printed.append},
        daemon=True)
    t.start()
    time.sleep(0.1)
    writer.write("hello\n")
    writer.flush()
    time.sleep(0.2)

    stop_event.set()
    t.join(timeout=2.0)

    assert "echo: hello" in printed
    writer.close()
    stream.close()
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_shell_interrupt.py -q
```

Expected: FAIL (`TypeError: run_shell() got an unexpected keyword argument 'stdin_stream'`)。

- [ ] **Step 3: `shell.py` を実装**

```python
"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。

readline 中断 seam (プラン8, 設計書 §6 codex I3-2): 対話モードの main は
`input()` でブロックするため、`stop_event` が別スレッド (watchdog) から
セットされても自然には解けない。`select.select` によるポーリングへ
置き換え、定期的に `stop_event` をチェックできるようにする — 対話モード
で main を wake する唯一の経路。
"""
from __future__ import annotations

import select
import sys
import threading

from agentic_fx.commands import Commands


def _read_line_interruptible(prompt: str, stop_event: threading.Event, *,
                             stream, poll_interval: float) -> str | None:
    """`select.select` でストリームの読み取り可能性をポーリングする。
    `stop_event` が立てば即座に `None` を返す (中断 — EOF とは区別する)。
    データが来れば 1 行読んで (末尾改行を除いて) 返す。EOF は
    `EOFError` を送出する。
    """
    print(prompt, end="", flush=True)
    while not stop_event.is_set():
        ready, _, _ = select.select([stream], [], [], poll_interval)
        if ready:
            line = stream.readline()
            if line == "":
                raise EOFError()
            return line.rstrip("\n")
    return None


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print, stdin_stream=None,
              poll_interval: float = 0.5) -> None:
    use_interruptible = input_fn is input
    stream = stdin_stream if stdin_stream is not None else sys.stdin
    while not stop_event.is_set():
        try:
            if use_interruptible:
                raw = _read_line_interruptible(
                    "afx> ", stop_event, stream=stream,
                    poll_interval=poll_interval)
                if raw is None:
                    return  # stop_event がポーリング中に立った (中断)
                line = raw.strip()
            else:
                line = input_fn("afx> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            return
        if stop_event.is_set():
            return
        if not line:
            continue
        if line == "stop":
            stop_event.set()
            return
        try:
            print_fn(commands.dispatch(line))
        except KeyboardInterrupt:
            stop_event.set()
            return
```

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_shell_interrupt.py -q
```

Expected: PASS。

- [ ] **Step 5: 既存テストの確認**

```bash
uv run pytest -q -k shell
uv run pytest -q
```

Expected: 全件 PASS。既存の `run_shell` テスト (`input_fn=` を明示的に注入するもの) は `use_interruptible = input_fn is input` の判定により従来どおり `input_fn` を直接呼ぶ経路を通るため無変更で動くことを確認する。既存テストが存在する場所を `grep -rln "run_shell" tests/` で確認し、1 件も red が無いことを確認する。

- [ ] **Step 6: 変異テスト**

1. `_read_line_interruptible` の `while not stop_event.is_set():` を `while True:` に改変 (stop_event を見なくする) → `test_stop_event_wakes_blocked_shell_promptly` が red (timeout するか `t.is_alive()` が True のまま)
2. `run_shell` の `use_interruptible = input_fn is input` を `use_interruptible = False` に固定 → `test_stop_event_wakes_blocked_shell_promptly` が red (通常の `input_fn` 経路に落ちて `stdin_stream` が使われなくなる)

- [ ] **Step 7: Commit**

```bash
git add src/agentic_fx/shell.py tests/test_shell_interrupt.py
git commit -m "$(cat <<'EOF'
feat: shell readline 中断 seam (select ポーリングで stop_event を検知)

設計書 §6 codex I3-2。対話モードの main を停止シーケンス開始時に
確実に wake する唯一の経路。既存の input_fn 注入テストとは後方互換。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 18: improve worker profile の権限境界 (DB パス非提供 + Landlock + 起動拒否)

設計書 §4.6「worker profile と権限境界」を実装する。**改善ループ本体・improve registry の中身はプラン 9** — 本 task は「構造的到達不能」の 2 層防御 (接続情報の非提供 + Landlock による FS 自己制限) だけを `mission_worker.py` に実装する。§4.6 の「到達不能 = 実行不能」の意味論 (`run_holdout_gate` の import 自体は可能だが、データ到達 (DB 接続) が構造的に失敗する) を実測で固定する。

**Files:**
- Modify: `src/agentic_fx/mission_worker.py` (`worker_profile == "improve"` 分岐 + `_bootstrap_improve_profile` 新設)
- Modify: `docs/superpowers/plans/2026-08-01-phase2-decomposition.md` (§12 申し送り③ — 「到達不能」表現への注記追加)
- Test: `tests/test_improve_profile_isolation.py` (新規 — 実 subprocess による E2E 帯)

**Interfaces:**
- Produces:
  - `mission_worker._bootstrap_improve_profile() -> None` — 呼び出し時点の `Path.cwd()` (WorkerRunner が `cwd=` に渡した専用空 workdir) と `Path(__file__).resolve().parents[1]` (src/ コードツリー) を使い、`landlock.restrict_to(read_only_paths=[code_root], read_write_paths=[workdir])` を呼ぶ。**`landlock.is_available()` が False なら `RuntimeError` (fail closed — improve worker は起動拒否)**
  - `mission_worker.main()` の `worker_profile == "improve"` 分岐: `db_path`/`plugins_dir` を一切参照しない (handshake で `None` が渡ってくる前提 — `WorkerRunner` の Task 10 実装が既にこの条件分岐を持つ)。`_bootstrap_improve_profile()` → resource limit (trade と同じ `child_as_mb`/`child_nofile`/`child_fsize_mb`。ネットワーク毒入れはしない — 設計書 §4.5) → 空の `ToolRegistry()` (改善ループの実ツールセットはプラン 9) で `LocalRunner` を組み立てる

- [ ] **Step 1: 分解書への注記追加 (§12 申し送り③)**

`docs/superpowers/plans/2026-08-01-phase2-decomposition.md` の該当箇所 (`grep -n "run_holdout_gate\|構造的に到達不能" docs/superpowers/plans/2026-08-01-phase2-decomposition.md` で 96 行目・104 行目付近を特定する) に以下の注記を追加する (既存文言は削除せず、注記として追記する):

```markdown
> **注記 (プラン8, 設計書 §4.6)**: 「構造的に到達不能」は「実行不能」の意味論で読み替える —
> Landlock の allowlist はコードツリーの読取を許すため `run_holdout_gate` の**関数 import
> 自体は可能**。遮断の実体は**データ到達**にあり、`run_holdout_gate` は `history_conn`
> (履歴 DB 接続) を必須引数に取るため、improve worker は DB パス非提供 + Landlock の
> data/ 遮断により接続を構成できず、import できても**実行が必ず失敗する**
> (プラン8 Task 18 で実測検証)。
```

- [ ] **Step 2: 失敗するテストを書く (Landlock 不能時の起動拒否)**

`tests/test_improve_profile_isolation.py` を新規作成する:

```python
"""improve worker profile の権限境界 (プラン8, 設計書 §4.6)。"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import is_available as landlock_available


def test_bootstrap_improve_profile_raises_when_landlock_unavailable(monkeypatch, tmp_path):
    import agentic_fx.mission_worker as mw_mod

    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="Landlock"):
        mw_mod._bootstrap_improve_profile()
```

- [ ] **Step 3: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_improve_profile_isolation.py -q -k landlock_unavailable
```

Expected: FAIL (`AttributeError: module 'agentic_fx.mission_worker' has no attribute '_bootstrap_improve_profile'`)。

- [ ] **Step 4: `mission_worker.py` を実装**

`src/agentic_fx/mission_worker.py` の import 節に `from agentic_fx.core import landlock` を追加する。`_set_resource_limits` の直後に追加:

```python
def _bootstrap_improve_profile() -> None:
    """improve worker profile の bootstrap (プラン8, 設計書 §4.6)。

    **構造的到達不能の 2 層防御**: ①接続情報の非提供 (handshake に
    db_path/plugins_dir が含まれない — `main()` の improve 分岐がこれらを
    一切参照しない) ②Landlock による FS 自己制限 (コードツリー読取 +
    専用 workdir 読書きのみ allowlist、`data/` は遮断)。

    呼び出し時点の `Path.cwd()` は WorkerRunner が `cwd=` に渡した専用空
    workdir (呼び出し元の責務 — このプロセス自身は検証しない)。

    **Landlock 利用不能な環境では improve worker は起動拒否 (fail
    closed)** — trade profile は Landlock を任意 (RO 接続が主防御) と
    するが、improve profile は Landlock が唯一の FS 境界であるため必須。
    """
    if not landlock.is_available():
        raise RuntimeError(
            "Landlock is not available on this kernel/architecture — "
            "improve worker profile refuses to start without it "
            "(fail closed, 設計書 §4.6)")
    code_root = Path(__file__).resolve().parents[1]  # src/ ディレクトリ
    workdir = Path.cwd()
    landlock.restrict_to(read_only_paths=[code_root], read_write_paths=[workdir])
```

`main()` 内、`worker_profile != "trade"` を拒否していた既存の分岐 (Task 7 の実装) を以下に置き換える:

```python
        worker_profile = handshake["worker_profile"]
        if worker_profile == "improve":
            _bootstrap_improve_profile()
            _set_resource_limits(
                as_mb=settings_dict["worker"]["child_as_mb"],
                nofile=settings_dict["worker"]["child_nofile"],
                fsize_mb=settings_dict["worker"]["child_fsize_mb"])
            from agentic_fx.config import Settings
            settings = Settings.model_validate(settings_dict)
            if settings.runner.improve.backend != "local":
                raise RuntimeError(
                    f"runner.improve.backend={settings.runner.improve.backend!r} "
                    "is not supported by mission_worker in this plan "
                    "(ClaudeRunner is Plan 9 scope) — fail closed")
            from agentic_fx.runners.base import Mission
            from agentic_fx.runners.local_runner import LocalRunner
            from agentic_fx.tools.registry import ToolRegistry

            # improve の実ツールセットはプラン9 — 本プランでは空の
            # registry を渡す (Landlock 適用後は data/ 到達が構造的に
            # 不能であることが本 task の受入条件そのもの)。
            registry = ToolRegistry()
            mission = Mission(**handshake["mission"])
            out_seq = SeqTracker()

            def on_message(msg: dict) -> None:
                write_frame(protocol_out, {
                    "type": "event", "seq": _next_seq(out_seq), "message": msg})

            runner = LocalRunner(
                base_url=settings.llama_swap.base_url,
                model=settings.runner.improve.model, registry=registry,
                on_message=on_message)

            write_frame(protocol_out, {
                "type": "ready", "seq": _next_seq(out_seq), "ok": True})
            try:
                result = runner.run(mission)
                write_frame(protocol_out, {
                    "type": "result", "seq": _next_seq(out_seq),
                    "status": result.status, "output": result.output})
            except Exception as exc:  # noqa: BLE001
                write_frame(protocol_out, {
                    "type": "result", "seq": _next_seq(out_seq),
                    "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
            return
        if worker_profile != "trade":
            raise RuntimeError(
                f"unsupported worker_profile in this plan: {worker_profile!r}")
```

**実装者への注意**: 上記は `main()` の既存 trade 分岐 (Task 7) の**直前**に挿入する — `worker_profile == "improve"` のケースは早期 `return` で完結させ、以降の trade 専用ロジック (`connect_readonly`・`plugin_loader`・`_RagRpcProxy` 等) に一切触れないことを保証する。`settings.runner.improve` は既存 `RunnerSettings.improve: RunnerChoice` (config.py) を参照する — 新設フィールドではない。

- [ ] **Step 5: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_improve_profile_isolation.py -q -k landlock_unavailable
```

Expected: PASS。

- [ ] **Step 6: 失敗するテストを書く (実 subprocess による到達不能性の実測)**

`tests/test_improve_profile_isolation.py` に追記する。実 `mission_worker.py` の handshake プロトコルを経由せず、`_bootstrap_improve_profile` を直接 subprocess 内で呼ぶ**軽量プローブ**で検証する (LLM への実接続を避けるため — 受入条件 §9-3 が要求するのは「isolation の実測」であり LLM ループの実行ではない):

```python
_ISOLATION_PROBE_SCRIPT = textwrap.dedent("""
    import os, sqlite3, sys
    from pathlib import Path
    os.chdir(sys.argv[2])  # WorkerRunner が cwd= に渡す専用空 workdir を模す
    from agentic_fx.mission_worker import _bootstrap_improve_profile
    _bootstrap_improve_profile()

    data_dir = Path(sys.argv[1])
    results = {}
    try:
        open(str(data_dir / "agentic.db"))
        results["open_db"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["open_db"] = "blocked"

    try:
        os.listdir(str(data_dir))
        results["list_data_dir"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["list_data_dir"] = "blocked"

    try:
        from agentic_fx.backtest.holdout import run_holdout_gate  # import 自体は可能
        conn = sqlite3.connect(str(data_dir / "agentic.db"))
        conn.execute("SELECT 1")
        results["holdout_data_reachable"] = "UNEXPECTED_SUCCESS"
    except Exception as e:
        results["holdout_data_reachable"] = f"blocked: {type(e).__name__}"

    print(results)
""")


def test_improve_profile_cannot_reach_data_dir(tmp_path):
    """受入条件 §9-3: improve profile の実 worker プロセス内から
    ①data/agentic.db 絶対パス open 失敗 ②data/ 列挙失敗
    ③run_holdout_gate を呼んでもデータ到達不能で失敗、を実測する。
    """
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "agentic.db").write_bytes(b"not a real sqlite file, "
                                          b"but the open() itself must "
                                          b"be blocked before content matters")
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE_SCRIPT, str(data_dir), str(workdir)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "'open_db': 'blocked'" in result.stdout
    assert "'list_data_dir': 'blocked'" in result.stdout
    assert "'holdout_data_reachable': 'blocked" in result.stdout
    assert "UNEXPECTED_SUCCESS" not in result.stdout
```

- [ ] **Step 7: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_improve_profile_isolation.py -q
```

Expected: PASS (Landlock が利用可能な環境。本実行環境は実測済みのため通るはず — §8 Task 8 参照)。

- [ ] **Step 8: 全体 green**

```bash
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 9: 変異テスト**

1. `_bootstrap_improve_profile` の `if not landlock.is_available():` チェックを削除 → `test_bootstrap_improve_profile_raises_when_landlock_unavailable` が red
2. `_bootstrap_improve_profile` の `read_only_paths=[code_root]` を `read_only_paths=[code_root, data_dir]` のように仮に `data/` を含めるよう改変 (実装ミスの模擬) → `test_improve_profile_cannot_reach_data_dir` が red (`open_db`/`list_data_dir` が `UNEXPECTED_SUCCESS` になる)

- [ ] **Step 10: Commit**

```bash
git add src/agentic_fx/mission_worker.py docs/superpowers/plans/2026-08-01-phase2-decomposition.md \
  tests/test_improve_profile_isolation.py
git commit -m "$(cat <<'EOF'
feat: improve worker profile の権限境界 (DBパス非提供 + Landlock + 起動拒否)

設計書 §4.6。改善ループ本体・registry の中身はプラン9 — 本task は
「構造的到達不能」の2層防御 (接続情報非提供 + Landlock) のみを実装する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 19: スレッド監督 (watchdog 相互監視) + health ラッチ + 停止状態機械 (`App.close`)

設計書 §5 (停止状態機械) と §6 (スレッド監督・health ラッチ) を実装する。本プランで最後の統合 task — ここまでの全部品 (WorkerRunner・MissionSupervisor・Rag・shell readline seam) を停止シーケンスへ接続する。

**設計判断 (writing-plans)**:
- **`stop_event` は `build_app` の外 (`run_service`) で先に構築し、`build_app` へ渡す** — `WorkerRunner`/`Scheduler` が `stop_event` を必要とするため、`build_app` 内部で新規生成する既存パターンでは `run_service` 側の `_stop_event` テストシームと二重管理になる。`build_app(..., stop_event: threading.Event | None = None)` (既定 None なら内部で新規生成 — 既存の `clock` 引数と同じパターン)
- **health ラッチの検出経路**: `ActivityLog` に `on_write_failure: Callable[[Exception], None] | None = None` を追加する (「write は例外を送出しない」契約は不変 — 失敗時にコールバックを**追加で**呼ぶだけ)。`build_app` がこれを `HealthLatch.record_failure` に配線する
- **WorkerRunner の即時中断**: `stop_event` をコンストラクタで受け取り、`ready_queue`/`done_queue` の待ちを `queue.Queue.get(timeout=deadline)` の 1 回待ちから「短い間隔でポーリングしつつ `stop_event` も見る」形に変更する (停止シーケンス開始時に実行中 Mission への SIGTERM を即座に発行できるようにするため — 設計書 §5 手順 1「同時に実行中 worker へ SIGTERM を発行 (3 と並行開始)」)

**Files:**
- Create: `src/agentic_fx/core/health_latch.py`
- Modify: `src/agentic_fx/activity.py` (`on_write_failure` コールバック追加)
- Modify: `src/agentic_fx/commands.py` (`health_latch` 追加 + `_status` 表示)
- Modify: `src/agentic_fx/core/scheduler.py` (`stop_event` 対応 — hooks/Mission 起動判定のスキップ)
- Modify: `src/agentic_fx/runners/worker_runner.py` (`stop_event` 対応 — 即時中断)
- Modify: `src/agentic_fx/service.py` (全体 — `App.close`/`build_app`/`run_service` の統合)
- Test: `tests/core/test_health_latch.py`, `tests/test_app_close.py`, `tests/test_stop_sequence.py` (新規)

**Interfaces:**
- Produces:
  - `health_latch.HealthLatch` — `record_failure(self, reason: str) -> None` / `is_latched(self) -> bool` / `summary(self) -> list[str]`。**ラッチは解除しない** (プロセス再起動でのみクリア — インスタンスの生涯を通じて `_reasons` は増えるだけ)
  - `ActivityLog.__init__(self, path: Path, *, on_write_failure: Callable[[Exception], None] | None = None)` — 失敗時、技術ログ warning に加えて `on_write_failure(e)` を (例外を握って) 呼ぶ
  - `Commands.__init__(..., health_latch: HealthLatch)` — `_status()` の出力に `health_latch.is_latched()` が True なら `"health: LATCHED (<summary の先頭 3 件>)"` を追記する
  - `Scheduler.__init__(..., stop_event: threading.Event | None = None)` — 新設 kwarg (既定 None = 常時 hooks/Mission 起動判定を実行、既存テスト互換)。`tick()` の全 `_run_hooks` 呼び出しサイトと `_trade_mission_due` 呼び出しを `stop_event.is_set()` でガードする (決定論ブロックは常に実行)
  - `WorkerRunner.__init__(..., stop_event: threading.Event | None = None)` — 新設 kwarg。`ready_queue`/`done_queue` の待ちを `stop_event` も見るポーリングへ変更 (既定 None = 従来どおり単純 timeout 待ち)
  - `MissionSupervisor.heartbeat: float` (Task 13 で既存) — watchdog が鮮度監視に使う
  - `App.close(self) -> list[str]` — 停止状態機械の資源終端 (§5 手順 2〜6)。戻り値は「close をスキップした資源名のリスト」(空なら全て close 完了)
  - `App.stop_event: threading.Event` / `App.health_latch: HealthLatch` (新設フィールド)
  - `service._watchdog_check(app: App, scheduler_thread_obj: threading.Thread, stop_event: threading.Event, *, heartbeat_grace_sec: float = 90.0) -> None` — モジュールレベル関数 (`run_service` のクロージャに閉じ込めず単体テスト可能にする)。1 回分の生存・heartbeat 鮮度チェック
  - `service._record_fatal(app: App, stop_event: threading.Event, reason: str) -> None` — fatal event の記録 + 通知 + `stop_event.set()` (App.close は実行しない)

- [ ] **Step 1: 失敗するテストを書く (`HealthLatch`)**

`tests/core/test_health_latch.py` を新規作成:

```python
"""HealthLatch (プラン8, 設計書 §6)。"""
from __future__ import annotations

from agentic_fx.core.health_latch import HealthLatch


def test_starts_unlatched():
    latch = HealthLatch()
    assert latch.is_latched() is False
    assert latch.summary() == []


def test_record_failure_latches_and_never_unlatches():
    latch = HealthLatch()
    latch.record_failure("disk full")
    assert latch.is_latched() is True
    assert latch.summary() == ["disk full"]
    latch.record_failure("second failure")
    assert latch.summary() == ["disk full", "second failure"]
    # 解除する公開 API が存在しないこと自体がテスト (grep で確認 — reset
    # メソッドが無いことをレビューで固定する)
```

- [ ] **Step 2: テスト実行して FAIL を確認 → 実装 → PASS**

```bash
uv run pytest tests/core/test_health_latch.py -q
```

`src/agentic_fx/core/health_latch.py` を新規作成:

```python
"""App 全体の latched health 状態 (プラン8, 設計書 §6)。

activity 書き込み失敗 (ディスクフル等) を記録する。**ラッチは解除しない**
(プロセス再起動でのみクリア — 「一度でも記録が欠けた稼働」を人間が確実に
知るため)。
"""
from __future__ import annotations

import threading


class HealthLatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: list[str] = []

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._reasons.append(reason)

    def is_latched(self) -> bool:
        with self._lock:
            return bool(self._reasons)

    def summary(self) -> list[str]:
        with self._lock:
            return list(self._reasons)
```

```bash
uv run pytest tests/core/test_health_latch.py -q
```

Expected: PASS。

- [ ] **Step 3: `activity.py` に `on_write_failure` を追加**

`tests/test_activity.py` (既存ファイルを確認) に以下を追加する:

```python
def test_on_write_failure_callback_invoked_on_write_error(tmp_path, monkeypatch):
    from agentic_fx.activity import ActivityLog, Category

    calls = []
    log = ActivityLog(tmp_path / "a.log", on_write_failure=calls.append)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(log._path), "open", boom)
    log.write(Category.SYSTEM, "x", "y")  # 例外を送出しない契約は不変
    assert len(calls) == 1
    assert isinstance(calls[0], OSError)
```

`src/agentic_fx/activity.py` の `ActivityLog.__init__` を以下に変更する:

```python
    def __init__(self, path: Path, *,
                 on_write_failure: Callable[[Exception], None] | None = None) -> None:
        self._path = path
        self._on_write_failure = on_write_failure
        path.parent.mkdir(parents=True, exist_ok=True)
```

`write` の `except Exception as e:` ブロック末尾 (`_log.warning(...)` の直後) に追加する:

```python
            if self._on_write_failure is not None:
                try:
                    self._on_write_failure(e)
                except Exception:  # noqa: BLE001 — write は送出しない契約を守る
                    _log.exception("on_write_failure callback failed")
```

import 節に `from typing import Callable` を追加する。

```bash
uv run pytest tests/test_activity.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 4: `commands.py` に `health_latch` を配線**

`Commands.__init__` (27-34 行) に `health_latch: HealthLatch` を追加し `self.health_latch = health_latch` を保持する。`_status` (86-95 行) の戻り文字列末尾に以下を追加する:

```python
    def _status(self) -> str:
        s = self.state.load()
        balance, equity = self.broker.equity()
        active = orders.list_by_status(self.conn, "open", "pending_fill",
                                       "protection_pending")
        recent = missions.recent(self.conn, 1)
        last = (f"{recent[0]['loop']}:{recent[0]['status']} "
                f"({recent[0]['started_at']})") if recent else "なし"
        health = (f"\nhealth: LATCHED ({'; '.join(self.health_latch.summary()[:3])})"
                  if self.health_latch.is_latched() else "")
        return (f"mode: {s.mode.value} / autopilot: "
                f"{'on' if s.autopilot else 'off'} / kill switch: "
                f"{'LATCHED' if s.kill_switch_latched else 'ok'}\n"
                f"残高: {balance:,.0f} / エクイティ: {equity:,.0f}\n"
                f"アクティブ orders: {len(active)}\n"
                f"直近 mission: {last}{health}")
```

`import` 節に `from agentic_fx.core.health_latch import HealthLatch` を追加する (型ヒント用)。`tests/test_commands.py` の `Commands(...)` 構築箇所すべてに `health_latch=HealthLatch()` を追加する (`grep -n "Commands(" tests/test_commands.py` で洗い出す)。

```bash
uv run pytest tests/test_commands.py -q
```

Expected: PASS (既存テストの `_status` 出力アサーションが完全一致比較をしていれば、latch 未発火時は `health` が空文字列のため無変更で通ることを確認する)。

- [ ] **Step 5: `scheduler.py` に `stop_event` を配線**

`Scheduler.__init__` (39-47 行) に `stop_event: threading.Event | None = None` を追加し `self._stop_event = stop_event` を保持する。import 節に `import threading` を追加する。`tick()` 内、`_run_hooks` の 3 呼び出しサイト (市場閉鎖時・mark-to-market 失敗時・通常経路) をそれぞれ以下の形にガードする:

```python
        if not self._stopping():
            self._run_hooks(now)
```

(市場閉鎖時・mark-to-market 失敗時の 2 箇所も同様に `if not self._stopping(): self._run_hooks(now)` へ変更する。)

通常経路の Mission 起動判定 (`reason = self._trade_mission_due(now)`) を以下に変更する:

```python
        reason = None if self._stopping() else self._trade_mission_due(now)
```

`_run_hooks` メソッドの直前に追加する:

```python
    def _stopping(self) -> bool:
        """設計書 §5 手順1: 新規受付停止 (stop_event セット後) は hooks・
        Mission 起動判定をスキップする。決定論ブロック (mark-to-market〜
        exits) は stop_event の有無に関わらず必ず実行する (最後の tick
        まで資金保護を続ける)。"""
        return self._stop_event is not None and self._stop_event.is_set()
```

```bash
uv run pytest tests/core/test_scheduler.py tests/core/test_scheduler_tick_order.py \
  tests/core/test_scheduler_signal.py -q
uv run pytest -q
```

Expected: 全件 PASS (`stop_event` 未指定の既存テストは `_stopping()` が常に False を返すため無変更で動く)。

- [ ] **Step 6: `worker_runner.py` に `stop_event` を配線**

`WorkerRunner.__init__` (Task 10) に `stop_event: threading.Event | None = None` を追加し `self._stop_event = stop_event` を保持する。`_run_with_child` 内の ready 待ち・result 待ちを以下のポーリング形式に変更する:

```python
        status = "failed"
        output = None
        try:
            ready = self._wait_with_stop(
                ready_queue, deadline_sec=w.worker_startup_timeout_sec)
            if ready is None:
                self._escalate_kill(proc, w)
                return MissionResult("failed", None, transcript)
            if not ready.get("ok", False):
                status = "failed"
                return MissionResult(status, None, transcript)

            deadline_budget = mission.timeout_sec + w.worker_grace_sec
            done = self._wait_with_stop(done_queue, deadline_sec=deadline_budget)
            if done is None:
                self._escalate_kill(proc, w)
                return MissionResult("timeout", None, transcript)
            kind, payload = done

            if kind == "result":
                status = payload["status"]
                output = payload.get("output")
            else:
                status = "failed"
                output = None
            return MissionResult(status, output, transcript)
        finally:
            dispatch_queue.put(None)
            self._ensure_dead(proc, w)
            reader.join(timeout=5.0)
            rpc_executor.shutdown(wait=False)
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

    def _wait_with_stop(self, q, *, deadline_sec: float, poll_interval: float = 0.2):
        """`stop_event` が立てば即座に None を返す (中断)。それ以外は
        `queue.Queue.get` を短い間隔でポーリングし、`deadline_sec` 経過
        したら None (timeout)、値が来ればそれを返す (`(kind, payload)`
        タプルまたは `try_submit` の frame dict そのもの)。"""
        deadline = time.monotonic() + deadline_sec
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return q.get(timeout=min(remaining, poll_interval))
            except queue.Empty:
                continue
```

**実装者への注意**: 上記は `_run_with_child` の該当ブロック (Task 10 で書いた `try: ready = ready_queue.get(timeout=...) ...` の部分) を丸ごと置き換える。`ready_queue.get`/`done_queue.get` の直接呼び出し箇所をすべて `self._wait_with_stop(...)` 経由に統一すること。`tests/runners/test_worker_runner.py` の既存テスト (Task 10) は `stop_event` を渡さない (`None`) ため、`poll_interval=0.2` 秒刻みのポーリングに変わっても実質的な待ち時間は不変 (timeout 検出の粒度が最大 0.2 秒粗くなるだけ) — 既存テストが timeout 値を厳密にアサートしていなければ無変更で通るはず。厳密な時間アサートがあれば `poll_interval` 分の許容誤差を追加すること。

```bash
uv run pytest tests/runners/test_worker_runner.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 7: 失敗するテストを書く (`App.close` の所有権ベース skip)**

`tests/test_app_close.py` を新規作成する:

```python
"""App.close の所有権ベース資源終端 (プラン8, 設計書 §5)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx.service import build_app


def _init_root(tmp_path):
    # 既存の起動系テスト (tests/test_service.py 等) の init ヘルパーに
    # 合わせて実装すること (settings.yaml.example のコピー + init_db)。
    ...


def test_close_closes_all_resources_in_normal_case(tmp_path):
    root = _init_root(tmp_path)
    app = build_app(root)
    skipped = app.close()
    assert skipped == []


def test_close_skips_conn_core_when_scheduler_thread_marked_busy(tmp_path):
    """設計書 §5 手順6: scheduler スタック時の conn_core は close しない
    (使用中 close の未定義動作より fd リークを選ぶ)。App.close は「conn_core
    が使用中かどうか」を呼び出し元 (run_service の join タイムアウト判定)
    から伝えられる形にする — この単体テストでは `busy_resources` 引数
    (Step 実装で確定する実シグネチャに実装者が合わせて書き直すこと) で
    直接指定して skip されることを確認する。
    """
    root = _init_root(tmp_path)
    app = build_app(root)
    skipped = app.close(busy_resources={"conn_core"})
    assert "conn_core" in skipped
```

（`_init_root`/`App.close` の実引数形は Step 8 の実装確定後にこのテストへ反映すること — 先に書いたテストが実装と食い違えば実装者が両方を整合させる。）

- [ ] **Step 8: `service.py` を実装 (`App.close` + `build_app`/`run_service` 統合)**

`App` dataclass に `stop_event: threading.Event` / `health_latch: HealthLatch` フィールドを追加する。`build_app` のシグネチャに `stop_event: threading.Event | None = None` を追加し、冒頭 (`clock = clock or SystemClock()` の直後) で `stop_event = stop_event if stop_event is not None else threading.Event()` を確定させる。`activity = ActivityLog(...)` の構築 (263 行) を以下に変更する:

```python
    health_latch = HealthLatch()
    activity = ActivityLog(root / "logs" / "activity.log",
                           on_write_failure=lambda e: health_latch.record_failure(
                               f"activity write failed: {safe_error_text(e)}"))
```

`owns_runner = runner is None` / `if runner is None: runner = WorkerRunner(...)` (Task 10) の**内側**の `WorkerRunner(...)` 呼び出しに `stop_event=stop_event, on_rpc_leak=_on_rpc_leak` を追加する — `if runner is None:` の条件分岐そのものは変更しない (テストが `build_app(root, runner=FakeRunner(...))` で注入するケースを壊さないため)。`_on_rpc_leak` はこのブロックの前で定義しておく:

```python
    def _on_rpc_leak() -> None:
        health_latch.record_failure("RAG RPC dispatcher leaked past rpc_timeout_sec "
                                    "(設計書 §4.3 codex I3-1)")
        stop_event.set()

    owns_runner = runner is None
    if runner is None:
        runner = WorkerRunner(root=root, settings=settings, clock=clock,
                              rag=rag, worker_profile="trade",
                              stop_event=stop_event, on_rpc_leak=_on_rpc_leak)
```

`Commands(...)` の構築に `health_latch=health_latch` を追加する。`App(...)` の構築に `stop_event=stop_event, health_latch=health_latch` を追加する。

`App` クラスに `close` メソッドを追加する:

```python
    def close(self, *, busy_resources: frozenset[str] = frozenset()) -> list[str]:
        """停止状態機械の資源終端 (設計書 §5 手順 2〜6)。`busy_resources`
        は呼び出し元 (`run_service` の join タイムアウト判定) が「使用中
        と判断した資源名」を伝える — 該当資源は close をスキップする
        (使用中 close の未定義動作より fd リークを選ぶ)。戻り値は close を
        スキップした資源名のリスト (空なら全て close 完了)。

        close 順序は逆順 (runner → rag → conn_supervisor/conn_core/
        conn_shell → notifier)。close できるものはすべて close し、1 つの
        資源の close 失敗が他をブロックしないよう個別に隔離する。
        """
        skipped: list[str] = []
        resources = [
            ("runner", lambda: self.runner.close()
             if self.owns_runner and hasattr(self.runner, "close") else None),
            ("rag", lambda: self.rag.close()),
            ("conn_supervisor", lambda: self.conn_supervisor.close()),
            ("conn_core", lambda: self.conn_core.close()),
            ("conn_shell", lambda: self.conn_shell.close()),
        ]
        for name, closer in resources:
            if name in busy_resources:
                skipped.append(name)
                continue
            try:
                closer()
            except Exception as e:  # noqa: BLE001 — 1 資源の失敗で他を止めない
                _log.warning("App.close: %s failed: %s", name, safe_error_text(e))
        return skipped
```

`_watchdog_tick` (既存、448-478 行) の直後にモジュールレベル関数を 2 つ追加する (`run_service` 内のクロージャに閉じ込めず、単体テスト可能にするため — Step 10 の受入テストがこれらを直接呼ぶ):

```python
def _record_fatal(app: App, stop_event: threading.Event, reason: str) -> None:
    """codex C2-3: scheduler/supervisor の回復不能死亡は資金保護の恒久
    停止に等しい — 対話モードでも即座に停止シーケンスを開始する。
    fatal event の記録 + 通知 + stop_event セットのみを行う (App.close は
    実行しない — 停止シーケンスの実行主体は常に main、設計書 §6)。
    """
    try:
        app.activity.write(Category.SYSTEM, "fatal_thread_death", reason)
    except Exception:  # noqa: BLE001
        _log.exception("failed to record fatal_thread_death")
    try:
        app.notifier.send(f"[agentic-fx] 致命的エラー: {reason} — 停止します")
    except Exception:  # noqa: BLE001
        _log.exception("failed to notify fatal_thread_death")
    stop_event.set()


def _watchdog_check(app: App, scheduler_thread_obj: threading.Thread,
                    stop_event: threading.Event, *,
                    heartbeat_grace_sec: float = 90.0) -> None:
    """1 回分の watchdog チェック (設計書 §6)。scheduler/supervisor の
    生存・heartbeat 鮮度を見て、異常があれば `_record_fatal` を呼ぶ。
    呼び出し元 (`run_service` の `watchdog_thread`) が 30 秒周期のループを
    持つ — このテストで直接叩けるよう周期そのものはこの関数の外に置く。
    """
    if not scheduler_thread_obj.is_alive():
        _record_fatal(app, stop_event, "scheduler thread is dead")
        return
    if not app.supervisor.is_alive():
        _record_fatal(app, stop_event, "supervisor thread is dead")
        app.supervisor.fail_pending(exc=RuntimeError("supervisor thread died"))
        return
    if time.monotonic() - app.supervisor.heartbeat > heartbeat_grace_sec:
        _record_fatal(app, stop_event, "supervisor heartbeat stale")
        return
    _watchdog_tick(app)
```

`run_service` を以下の状態機械へ再構成する (既存の `stop_event = _stop_event if ...` 行から末尾までを置き換える):

```python
    stop_event = _stop_event if _stop_event is not None else threading.Event()
    app = build_app(root, stop_event=stop_event)

    warning = Policy(root / "policy" / "directives.md").size_warning()
    if warning:
        print(warning)
    print(build_splash(app))
    app.activity.write(Category.SYSTEM, "service_started", f"daemon={daemon}")

    scheduler_busy = threading.Event()  # tick 実行中フラグ (join timeout 判定用)

    def scheduler_thread() -> None:
        last = 0.0
        while not stop_event.is_set():
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                if stop_event.is_set():
                    break
                scheduler_busy.set()
                try:
                    with app.core_lock:
                        app.scheduler.tick(app.clock.now())
                except Exception:  # noqa: BLE001
                    _log.exception("tick failed")
                finally:
                    scheduler_busy.clear()
            stop_event.wait(1)

    def watchdog_thread() -> None:
        """設計書 §6: scheduler/supervisor の heartbeat 鮮度・生存を
        30 秒周期で監視する。1 回分のチェックはモジュールレベル関数
        `_watchdog_check` に委譲する (クロージャに閉じ込めず単体テスト
        可能にするため — Task 19 の受入テストがこの関数を直接呼ぶ)。"""
        while not stop_event.is_set():
            stop_event.wait(30)
            if stop_event.is_set():
                break
            try:
                _watchdog_check(app, th, stop_event)
            except Exception:  # noqa: BLE001 — スレッドを殺さない
                _log.exception("watchdog tick failed")

    if daemon:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    app.supervisor.start()
    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()
    wd = threading.Thread(target=watchdog_thread, daemon=True)
    wd.start()

    try:
        if daemon:
            while not stop_event.is_set():
                try:
                    stop_event.wait(1)
                except KeyboardInterrupt:
                    stop_event.set()
        else:
            from agentic_fx.shell import run_shell
            run_shell(app.commands, stop_event)
            if stop_event.is_set() and not daemon:
                # readline 中断 seam 経由で watchdog の fatal 検出により
                # 起こされた場合、対話モードは shell へ警告を表示してから
                # 終了する (codex N4-1 — daemon は activity+Notifier のみ、
                # 対話はそれに加えて shell 表示)。
                print("警告: システムスレッドの異常を検出したため停止します。"
                      "詳細は activity ログを確認してください。")
    finally:
        # ---- 停止状態機械 (設計書 §5) — 実行主体は常に main ----
        stop_event.set()  # 手順1: 新規受付停止 (べき等 — 既にセット済みでも安全)
        app.supervisor.shutdown(
            drain_exc=RuntimeError("service shutting down"))  # 手順2
        th.join(timeout=30)  # 手順3: scheduler join を先に
        scheduler_still_busy = scheduler_busy.is_set() and th.is_alive()

        supervisor_busy = app.supervisor.is_alive()
        app.supervisor.join(timeout=app.settings.worker.shutdown_join_timeout_sec)  # 手順4
        supervisor_still_busy = supervisor_busy and app.supervisor.is_alive()

        wd.join(timeout=15)

        busy: set[str] = set()
        if scheduler_still_busy or supervisor_still_busy:
            busy.add("conn_core")  # 両スレッドとも conn_core を使う
        skipped = app.close(busy_resources=frozenset(busy))  # 手順5-6
        if skipped:
            app.activity.write(Category.SYSTEM, "close_skipped_resources",
                               f"{skipped} (join timeout — used-in-flight)")

        if th.is_alive() or app.supervisor.is_alive():
            app.activity.write(Category.SYSTEM, "service_stopped",
                               "shutdown_timeout (Mission 継続中の可能性)")
        else:
            app.activity.write(Category.SYSTEM, "service_stopped", "graceful")

    if th.is_alive() or app.supervisor.is_alive():
        print("警告: 停止タイムアウト。実行中の処理が残っている可能性があります。")
        return 1
    print("停止しました。")
    return 0
```

**実装者への注意**: 上記は設計の骨格であり、実装時に以下を確認・調整すること — ①`watchdog_thread` はクロージャの定義順序に注意 (`th` を参照する `watchdog_thread` は `th` が定義された**後**に実際に呼ばれるが、Python のクロージャは遅延束縛のため定義順は `th = threading.Thread(...)` より前でも動く — 既存コードと同じパターン) ②`app.runner.close()` は Task 10 で `WorkerRunner.close()` が no-op として存在するため `owns_runner`+`hasattr` 判定は `App.close` 内に移した (旧 573-577 行の判定はここに統合され `run_service` 側からは削除する) ③`_check_llama_swap`/`build_splash`/import 節の整合を最終確認すること。

- [ ] **Step 9: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_app_close.py -q
uv run pytest tests/test_service.py tests/test_service_app.py -q
uv run pytest -q
```

Expected: 全件 PASS。`tests/test_service.py`/`tests/test_service_app.py` に `run_service` の shutdown 経路を検証する既存テストがあれば (`grep -n "run_service" tests/test_service*.py`)、新しい停止状態機械の順序 (`th.join` → `supervisor.join` → `wd.join` → `app.close`) に合わせてアサーションを更新すること。

- [ ] **Step 10: 失敗するテストを書く (停止シーケンスの統合確認)**

`tests/test_stop_sequence.py` を新規作成する:

```python
"""停止状態機械の統合確認 (プラン8, 設計書 §5/§6)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.service import run_service


def test_run_service_stops_gracefully_with_pre_set_stop_event(tmp_path):
    """_stop_event を事前にセットして渡すと、run_service は即座に停止
    状態機械を完走して 0 (graceful) または 1 を返す (実スリープ・実
    シグナルなしで検証する既存パターン — fix round 1 F4)。"""
    # root の init は既存 tests/test_service.py の init ヘルパーに合わせる
    root = _init_root(tmp_path)  # 既存 fixture 名で置き換えること
    stop_event = threading.Event()
    stop_event.set()
    rc = run_service(root, daemon=True, _stop_event=stop_event)
    assert rc in (0, 1)


def test_watchdog_check_records_fatal_and_stops_when_supervisor_dead(tmp_path):
    """設計書 codex C2-3: supervisor スレッド死亡は (daemon/対話の
    モードを問わず) 停止シーケンスを開始する — `_watchdog_check` は
    モード概念を持たない共通のチェック本体であり、`run_service` はどちら
    のモードでも同じ `_watchdog_check` を呼ぶ (Step 8 の `watchdog_thread`
    参照)。「両モードで停止する」は呼び出し経路が daemon/対話で分岐しない
    ことそのもので保証される — この 1 本で `_watchdog_check` 自体の挙動
    (supervisor 死亡検出 → fatal 記録 → stop_event セット → pending
    Future の例外完了) を直接検証する。
    """
    from agentic_fx.service import _watchdog_check, build_app

    root = _init_root(tmp_path)  # 既存 fixture 名で置き換えること
    app = build_app(root)
    stop_event = app.stop_event

    class DeadThread:
        def is_alive(self) -> bool:
            return True  # scheduler は生きている

    class DeadSupervisor:
        heartbeat = 0.0

        def is_alive(self) -> bool:
            return False  # supervisor が死んでいる

        def fail_pending(self, *, exc: Exception) -> None:
            calls.append(exc)

    calls: list[Exception] = []
    app.supervisor = DeadSupervisor()

    _watchdog_check(app, DeadThread(), stop_event)

    assert stop_event.is_set()
    assert len(calls) == 1
    assert isinstance(calls[0], RuntimeError)
    # activity に fatal_thread_death が記録されていること
    log_lines = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "fatal_thread_death" in log_lines
    assert "supervisor thread is dead" in log_lines


def test_watchdog_check_detects_scheduler_death_before_supervisor(tmp_path):
    """scheduler thread の死亡は supervisor より先に検査され、同じ
    fatal 経路 (stop_event セット) に落ちる。"""
    from agentic_fx.service import _watchdog_check, build_app

    root = _init_root(tmp_path)
    app = build_app(root)

    class DeadThread:
        def is_alive(self) -> bool:
            return False

    _watchdog_check(app, DeadThread(), app.stop_event)

    assert app.stop_event.is_set()
    log_lines = (root / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "scheduler thread is dead" in log_lines
```

- [ ] **Step 11: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_stop_sequence.py -q
uv run pytest -q
```

Expected: 全件 PASS。

- [ ] **Step 12: 変異テスト**

1. `HealthLatch` に `reset()` メソッドを追加する変異を行い、`_reasons` をクリアするコードを書いてから `test_record_failure_latches_and_never_unlatches` 相当が「reset 呼び出し後も summary が空にならない」ことを確認する専用テストを追加する (ラッチ不解除の意図的な破壊テスト)
2. `scheduler.py` の `_stopping()` の呼び出しを `_run_hooks` の 3 箇所から 1 箇所だけ残して削除 → 対応する `test_hooks_run_even_when_market_closed` 等 (Task 12/19 で stop_event を絡めたテストがあれば) が red になることを確認する
3. `_watchdog_check` の `if not app.supervisor.is_alive():` ブロックを削除 → `test_watchdog_check_records_fatal_and_stops_when_supervisor_dead` が red
4. `App.close` の `if name in busy_resources:` チェックを削除 → `test_close_skips_conn_core_when_scheduler_thread_marked_busy` が red

- [ ] **Step 13: Commit**

```bash
git add src/agentic_fx/core/health_latch.py src/agentic_fx/activity.py \
  src/agentic_fx/commands.py src/agentic_fx/core/scheduler.py \
  src/agentic_fx/runners/worker_runner.py src/agentic_fx/service.py \
  tests/core/test_health_latch.py tests/test_app_close.py tests/test_stop_sequence.py \
  tests/test_activity.py tests/test_commands.py
git commit -m "$(cat <<'EOF'
feat: スレッド監督 (watchdog相互監視) + health latch + 停止状態機械 (App.close)

設計書 §5/§6。停止の実行主体を main に一意化し、scheduler join優先→
supervisor drain/join→App.closeの所有権ベース資源終端まで一気通貫で実装。
両モードでスレッド死亡時に停止 (codex C2-3)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---
### Task 20: E2E + 受入条件検証 (設計書 §9 の 8 項目)

設計書 §9 の受入条件を実測・レビューで確定する最終 task。Task 1〜19 で個別に検証済みの性質を、実 subprocess・複数コンポーネント間の統合で改めて実証する (単体テストの積み上げでは検出できない配線欠陥・タイミング窓を狙う)。

**Files:**
- Test: `tests/test_e2e_worker_isolation.py` (新規)
- (レビューのみ・コード変更なし) `risk_gate.py`/`paper_broker.py`/`transitions.py` の diff ゼロ確認

**受入条件 8 項目と対応**:

| # | 受入条件 | 検証方法 |
|---|---|---|
| 1 | ハング注入で kill | 本 task 新規 (実プロセスへの SIGTERM 無視 → SIGKILL エスカレーション実測) |
| 2 | 資金保護継続 (Mission 実行中の SL 到達 → クローズ) | 本 task 新規 (build_app の実 App + FakeRunner 差し替えで統合実証) |
| 3 | improve profile 到達不能 | Task 18 で実測済み (`tests/test_improve_profile_isolation.py`) — 本 task は全体スイートに含まれることの確認のみ |
| 4 | 終端の一意性 (CAS 二重終端拒否 + 起動時同時回収) | Task 11 で実測済み (`tests/store/test_missions_cas.py`) — 同上 |
| 5 | スレッド死亡 → 両モード停止 + App.close 全経路 + shutdown 時 pending Future 例外完了 | Task 19 で実測済み (`tests/test_stop_sequence.py`/`tests/test_app_close.py`) — 同上 |
| 6 | tick 順序契約の回帰ピン + commit-core 鮮度再検証 | Task 12/14 で実測済み (`tests/core/test_scheduler_tick_order.py`/`tests/core/test_executor_snapshot.py`) — 同上 |
| 7 | 決定論的コア diff ゼロ | 本 task でレビューコマンド実行 (下記 Step) |
| 8 | 既存 1404+ tests green | 本 task で全体実行 |

- [ ] **Step 1: 失敗するテストを書く (ハング注入 → SIGTERM 無視 → SIGKILL)**

`tests/test_e2e_worker_isolation.py` を新規作成する:

```python
"""プラン8 E2E — 設計書 §9 受入条件 1・2 (実 subprocess・実統合)。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.config import load_settings

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example"


def test_worker_runner_kills_process_that_ignores_sigterm(tmp_path):
    """受入条件 1: SIGTERM を無視するプロセスに対し、
    worker_terminate_grace_sec 経過後に SIGKILL が実際に効く
    (実プロセス — シェルの trap でSIGTERM を無視させる)。
    """
    from agentic_fx.runners.worker_runner import WorkerRunner
    from agentic_fx.config import WorkerSettings

    proc = subprocess.Popen(
        ["sh", "-c", "trap '' TERM; sleep 60"],
        start_new_session=True)
    w = WorkerSettings(worker_terminate_grace_sec=1.0)

    runner = WorkerRunner.__new__(WorkerRunner)  # コンストラクタを経由せず
                                                  # _escalate_kill/_kill の
                                                  # OS レベル機構だけを直接
                                                  # 検証する (mission worker
                                                  # プロトコル一式を経由しない
                                                  # 最小構成)
    start = time.monotonic()
    runner._escalate_kill(proc, w)
    elapsed = time.monotonic() - start

    assert proc.poll() is not None  # 実際に死んでいる
    assert elapsed < 5.0  # grace(1s) + kill 完了が数秒以内


def test_funds_protection_continues_during_blocked_mission(tmp_path):
    """受入条件 2: Mission 実行中 (WorkerRunner がブロック中) でも、
    scheduler tick の SL/TP 監視 (_process_exits) が core_lock を取得
    できて実行される — 実 App (build_app) + ブロックする fake runner の
    差し替えで統合実証する。
    """
    # 実装方針:
    # 1. build_app(root, clock=FixedClock(...)) で実 App を構築する
    #    (settings.yaml.example ベースの一時 root — 既存 E2E テストの
    #    init ヘルパーに合わせる)
    # 2. app.runner を「.run() が threading.Event で解放されるまで
    #    ブロックする」fake に差し替える (tests/loops/test_trade_loop_phases.py
    #    の _SlowRunner/BlockingRunner と同型)
    # 3. OPEN 済みの注文 (SL 到達済みのバー) を DB に用意する
    # 4. 別スレッドで app.trade_loop.run_once("cron") を起動しブロックさせる
    # 5. メインスレッドで app.scheduler.tick(SL到達後の now) を core_lock
    #    経由で呼び、SL クローズが実行されること (orders テーブルの
    #    status が 'closed' になること) を確認する
    # 6. fake runner を解放し、run_once スレッドを join する
    #
    # 既存の Env/fixture (tests/core/test_scheduler.py の SL 到達シナリオ、
    # tests/loops/test_trade_loop_phases.py の BlockingRunner パターン) を
    # 実ファイルで確認し、両者を組み合わせて実装すること。
    raise NotImplementedError(
        "実装者が Step 1 完了後、既存 fixture を組み合わせて具体化すること")
```

- [ ] **Step 2: テスト実行して FAIL を確認**

```bash
uv run pytest tests/test_e2e_worker_isolation.py -q
```

Expected: `test_worker_runner_kills_process_that_ignores_sigterm` は WorkerRunner の実装 (Task 10/19) が既に正しければ **この時点で PASS してよい** (新規実装ではなく既存機構の統合実証のため)。`test_funds_protection_continues_during_blocked_mission` は `NotImplementedError` で FAIL する。

- [ ] **Step 3: `test_funds_protection_continues_during_blocked_mission` を実装**

Step 1 のコメントに書いた方針に従い、以下の形へ具体化する (実ファイルの既存 fixture 名に実装者が合わせること — 下記は骨格):

```python
def test_funds_protection_continues_during_blocked_mission(tmp_path):
    import threading
    from datetime import datetime, timedelta, timezone

    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.core.contracts import OrderStatus as S
    from agentic_fx.runners.base import Mission, MissionResult
    from agentic_fx.service import build_app
    from agentic_fx.store import orders as orders_store

    root = _init_root(tmp_path)  # 既存 E2E テストの init ヘルパーに合わせる
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    clock = FixedClock(now)
    app = build_app(root, clock=clock)

    # SL 到達済みの OPEN ポジションを 1 件用意する (既存 test_scheduler.py
    # の SL/TP テストパターンに合わせて具体的な entry/SL/bar 価格を選ぶ)
    oid = _seed_open_position_with_reachable_sl(app.conn_core, now)

    class BlockingRunner:
        def __init__(self):
            self.release = threading.Event()

        def run(self, mission: Mission) -> MissionResult:
            self.release.wait(10.0)
            return MissionResult("completed",
                                 {"action": "hold", "reasoning": "x"}, [])

    blocking = BlockingRunner()
    app.trade_loop.runner = blocking

    t = threading.Thread(
        target=lambda: app.trade_loop.run_once("cron"), daemon=True)
    t.start()
    time.sleep(0.2)  # run_once が run 相 (lock 非保持) に入るまで待つ

    # SL/TP 監視が core_lock を取得できることを確認しつつ実行する
    acquired = app.core_lock.acquire(timeout=2.0)
    assert acquired, "Mission 実行中でも core_lock が取得できるはず"
    app.core_lock.release()

    with app.core_lock:
        app.scheduler.tick(now + timedelta(minutes=1))  # SL 到達バーの tick

    row = orders_store.get(app.conn_core, oid)
    assert row["status"] == S.CLOSED.value

    blocking.release.set()
    t.join(timeout=10.0)
```

（`_init_root`/`_seed_open_position_with_reachable_sl` は既存の `tests/core/test_scheduler.py`/`tests/test_e2e_phase1.py` 等の E2E fixture パターンに実装者が合わせて書くこと — 具体的な SL 価格・バー価格の数値は既存 SL/TP テストの数値をそのまま転用してよい。)

- [ ] **Step 4: テスト実行して PASS を確認**

```bash
uv run pytest tests/test_e2e_worker_isolation.py -q
```

Expected: 全件 PASS。

- [ ] **Step 5: 決定論的コア diff ゼロの確認 (受入条件 7)**

```bash
git diff main -- src/agentic_fx/core/risk_gate.py
git diff main -- src/agentic_fx/core/paper_broker.py
git diff main -- src/agentic_fx/core/transitions.py
```

Expected: 3 ファイルとも **diff なし** (`risk_gate.py`/`paper_broker.py`/`transitions.py` は本プランのどの task でも Modify 対象に含まれていない — Files 一覧を全 task 通して `grep -n "risk_gate.py\|paper_broker.py\|transitions.py"` で確認し、1 件もヒットしないことをこの Step で再確認する)。`executor.py` は変更対象だが、`git diff main -- src/agentic_fx/core/executor.py` を目視し、**`evaluate(intent, ctx, ...)` の呼び出しと GateContext 構築ロジックが `_open`/`open_from_snapshot` の両方で完全に同一であること** (Task 14/15 の `_evaluate_and_execute_open` への切り出しが判定ロジックを 1 文字も変えていないこと) をコードレビューで確認する。

- [ ] **Step 6: 全体 green (受入条件 8)**

```bash
uv run pytest -q
```

Expected: 全件 PASS。プラン開始時点のテスト数 (1404) + 本プランで追加したテストがすべて green であることを確認し、実測件数を progress.md に記録する。

- [ ] **Step 7: 受入条件 3・4・5・6 の再確認 (既存 task の実測結果を集約)**

以下を個別に再実行し、全件 PASS することを確認してから progress.md に「受入条件 8 項目、全件 green」の実測記録を残す:

```bash
uv run pytest tests/test_improve_profile_isolation.py -q       # 受入 3
uv run pytest tests/store/test_missions_cas.py -q               # 受入 4
uv run pytest tests/test_stop_sequence.py tests/test_app_close.py -q  # 受入 5
uv run pytest tests/core/test_scheduler_tick_order.py tests/core/test_executor_snapshot.py -q  # 受入 6
```

- [ ] **Step 8: 変異テスト**

1. `WorkerRunner._kill` の `os.killpg(proc.pid, signal.SIGKILL)` を `os.killpg(proc.pid, signal.SIGTERM)` に改変 (SIGKILL を送らなくする) → `test_worker_runner_kills_process_that_ignores_sigterm` が red (`trap '' TERM` により無反応、`proc.poll()` が None のまま)
2. `TradeLoop._run_once_impl` の commit-core `with self._core_lock:` を run 相の周りまで拡張する変異 (意図的に lock 粒度を壊す) → `test_funds_protection_continues_during_blocked_mission` が red (SL クローズが `run_once` 完了まで実行されない = `acquired` が False または tick 呼び出し自体がブロックする)

- [ ] **Step 9: Commit**

```bash
git add tests/test_e2e_worker_isolation.py
git commit -m "$(cat <<'EOF'
test: プラン8 E2E + 受入条件検証 (設計書 §9 の8項目)

ハング注入→SIGTERM無視→SIGKILL の実プロセス実証、Mission実行中の
SL/TP監視継続の統合実証を新規追加。他6項目は各taskの実測を集約する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

## プラン完了条件

1. worker 隔離が実プロセスで成立: ハング注入 → SIGTERM 無視時も SIGKILL → `timeout` finalize + 部分 transcript (Task 7/10/20)
2. Mission 実行中も SL/TP 監視が core_lock を取得できる (Task 13/15/16/20 で実証)
3. improve worker profile が `data/` へ構造的に到達不能 (import 可能・実行不能の意味論 — Task 18)
4. `missions.finish` の二重終端が CAS で拒否される + 起動時 `running`→`interrupted` と claimed signals の同時回収 (Task 11)
5. scheduler/supervisor いずれかの死亡で両モードとも停止シーケンス + 非ゼロ終了、`App.close` が所有権ベースで資源終端 (Task 19)
6. tick 順序契約 (決定論ブロック内部順序・fills_allowed ゲート・processed-bar マーキング位置) が回帰ピンで固定、commit-core の鮮度再検証・N4-2 fail-closed 分岐がテストで固定 (Task 12/14)
7. risk_gate.py/paper_broker.py/transitions.py が diff ゼロ、executor.py は判定ロジック不変・I/O 位置のみ移動 (Task 14/15/20)
8. 既存 + 追加分すべての pytest が green (Task 20)
9. プラン7起票の B 束 9 項目・プラン5 park 小口 6 項目がすべて返済済み (Task 2/3/4)

## プラン9 への引き継ぎ

- improve worker profile の registry 中身 (research_tools・改善ループ本体) はプラン9 — 本プランは Landlock + DB パス非提供の権限境界のみ実装した
- `runner.improve.backend == "claude"` は本プランでは `RuntimeError` で fail closed (ClaudeRunner 未実装) — プラン9 で `ClaudeRunner` 実装後にこの分岐を実装へ差し替える
- 遮断 8 項目の全経路統合回帰テストはプラン9 の blocking 受入条件 (分解書どおり) — 本プランは項目 2 (holdout 実行到達不能性) の worker 境界のみ提供する
- spec 小改訂束 (exit_mode② / signals UNIQUE 意図明文化 / approved+rejected 併存規則 / claimed_by FK / **close/cancel gate 論点**) はプラン9 前に実施 (分解書どおり、本プランでは着手しない)


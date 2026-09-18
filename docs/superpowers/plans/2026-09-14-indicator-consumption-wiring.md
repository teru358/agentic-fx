# [indicator-consumption-wiring] 実装プラン v1.8 (設計書 = `docs/superpowers/specs/2026-09-14-indicator-consumption-wiring-design.md` v1.6 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (推奨) または superpowers:executing-plans で task ごとに実行すること。Step は
> チェックボックス (`- [ ]`) で追跡する。

**Goal:** strategy plugin が `config.yaml` の `indicators:` で宣言した配備済 indicator plugin の
出力を、strategy worker プロセス内で計算して `evaluate(df, indicators, signals, params)` の
`indicators` 引数に渡す。依存の版は `config.yaml` の `pin` (= `content_hash` の署名対象) に
書き込む「ロック方式」で固定し、既存 identity `(name, content_hash)` を一切変えない。

**Architecture:** 新規 `plugin/resolve.py` を「依存解決の唯一の場所」にする。
`tools/plugin_loader.approved_plugins` を二相化して `InventoryBuildResult` を返し、
composition root (service 起動 / 改善 mission / 人間 CLI / 承認回廊 / trade worker) が
それぞれ 1 回だけ inventory を構築して同じ `ResolvedIndicatorSet` オブジェクトを配る。
indicator の実行は strategy と**同一 worker プロセス内**で行う (IPC を deps 倍にしない、
設計書 §3)。失敗は全経路で fail closed (固定文言)。

**Tech Stack:** Python 3.13 / uv / pytest / pandas / numpy / sqlite3 / PyYAML。
**DB スキーマ変更なし** (`backtest_runs.indicator_deps` 列は設計書 §2.7 で撤回済み。
additive 列も追加しない)。

**Spec:** `docs/superpowers/specs/2026-09-14-indicator-consumption-wiring-design.md` (v1.4)。
survey: `tmp/design-indicator-wiring/survey.md`。**設計を変えない** — ユーザー裁定 U1〜U6 と
codex 設計レビュー 9 周 + opus 1 周の全件採用は設計書 §0/§8〜§14 に確定記録済みなので
「裁定待ち」は無い。設計書に無い判断が必要になったら実装を止めて指揮者へ申告すること。

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ
- **発注・SL 変更・クローズ・資金保護・Risk Gate は本プランの対象外** (触らない)。
  本束が触るのは「plugin にどの値を渡すか」と「採用判断 (approval を作るか)」だけ
- **実 DB (`data/agentic.db`) を読み書きしないこと**。テストは必ず `tmp_path` 上の
  sqlite (`tests/backtest/factories._conn` 流儀) を使う。`tests/conftest.py` の
  session ガードを無効化・迂回しない ([[tests-touching-real-repo-resources]])
- **実 `plugins/` ディレクトリを書き換えないこと**。fixture plugin は `tmp_path` 配下に作る
- **遮断 8**: 改善 worker に渡る文字列 (sink 一覧は設計書 §5 — `last_result` /
  `gate_failed` activity / prompt / 改善 RPC 応答 / 提案レポート本文 /
  `inventory_view` の 6 経路) に holdout の数値・段名 (`in_sample`/`holdout`)・
  pair・baseline 差分を混ぜない。本束が追加する `last_result` 語彙は
  **`indicator_unresolved` の完全一致のみ** (alias も cause も付けない)。
  **`last_result` / `gate_failed` の `reason` / 提案レポート本文には alias/cause を
  載せない** — alias/cause はここでは activity 行の追加フィールドにだけ書く
  (activity は agent に渡らない)。**改善 RPC 応答は設計書 §2.9c のとおり
  `{"started": false, "error": "indicator_unresolved", "alias", "reason",
  "available": [...]}` を返す** — この alias/reason は解決失敗の種別
  (`pin_mismatch` 等) であって holdout 由来の情報ではなく、agent が自分の
  `config.yaml` を是正するために必要 (codex 設計レビュー r4 C1 で確定)
- **`run_kind_gate` はゲート判定で例外を投げない** — 判別子 `verdict_kind` を載せた
  `GateOutcome` を常に返す。raise の決定は `_run_full_gate` が判別子から行う。
  **例外メッセージの部分一致による失敗種別の分類は禁止**
- **固定文言の語彙一覧** (逐語。テストは完全一致で pin する):
  - resolver の reason: `not_found` / `not_indicator` / `over_max_bars_limit` /
    `params_not_json_safe` / `unpinned` / `pin_mismatch` / `handshake_too_large` /
    `outputs_undeclared`
  - loader の reject reason: `indicators_not_allowed_for_kind` /
    `outputs_not_allowed_for_kind` / `indicator_ref_unknown_key:<key>` /
    `indicator_ref_missing_plugin` / `indicator_ref_bad_plugin_name` /
    `indicator_ref_bad_alias` / `indicator_ref_duplicate_alias` /
    `indicator_ref_bad_pin` / `params_not_json_safe:<path>` / `outputs_bad_entry` /
    `outputs_duplicate` / `too_many_indicators` / `too_many_outputs` /
    `params_too_large:<path>`
  - 例外文言: `ValueError("indicator_unresolved:<alias>:<reason>")` /
    `ValueError("outputs_required")` / `ValueError("candidate_changed")`
  - CLI stderr (1 行、逐語): `indicator_unresolved:<alias>:<reason>`
  - `GateOutcome.verdict_kind` の新値: `"indicator_unresolved"`
  - `last_result` / `_finalize_gate_failed(reason=...)`: `indicator_unresolved` /
    `outputs_required`
  - activity: `switch_reverted reason=indicator_unresolved alias=<alias> cause=<reason>` /
    `IMPROVE gate_failed mission=<id> reason=indicator_unresolved alias=<alias> cause=<reason>` /
    `IMPROVE backtest_cpu mission=<id> plugin=<name> scope=<in_sample|holdout> pair=<pair> deps=<n> cpu_sec=<float|null>`
  - noop: `noop_copy_of:<label>` (既存、変えない)
- **上限値** (コード定数。config には出さない — `settings.yaml` / `.example` は本束では
  変更しない):
  - `MAX_INDICATOR_DEPS = 8` (`indicators` の要素数、8 通過 / 9 拒否)
  - `MAX_OUTPUTS = 32` (`outputs` の要素数、32 通過 / 33 拒否)
  - `MAX_PARAMS_BYTES = 8192` (各 `params` の canonical JSON、8192 通過 / 8193 拒否)
  - `MAX_HANDSHAKE_BYTES = 262144` (展開後の handshake 総 byte 数、262144 通過 / 262145 拒否)
- **`outputs` は承認で必須 (U4)**: `submit_candidate` / `bless_candidate` / 改善 commit gate は
  `kind == "indicator"` かつ `meta.outputs is None` を固定 `ValueError("outputs_required")` /
  `gate_failed reason=outputs_required` で拒否する。approval 行を作らない。
  loader (`discover`) では `outputs` は任意のまま (既存配備 `rsi_indicator` / `rsi_wilder` の
  discover 互換)
- **`config/settings.yaml` に新規キーを追加しない** (本束の上限はすべてコード定数)。
  もし追加が必要になったら `config/settings.yaml.example` と両方同期し、指揮者へ申告する
- **Landlock の allowlist・profile を変更しない**。`plugin/worker.py` の resource limit も変えない
- **`plugins/`・`data/`・`logs/` はコミット対象外のまま** (本プランは `src/` / `tests/` /
  `docs/` のみ変更)
- **tuple の拡張禁止**: 戻り値の拡張は named result 型で行う
  (`_run_full_gate` は 4 要素 tuple を維持、`GateOutcome` / `StrategyGateVerdict` /
  `InventoryBuildResult` にフィールドを足す)
- **モックで実 worker を潰さない**: E2E 系の pin (A1 / A1-b / A1' / C1 / V2 / V3 / N1) は
  実 sqlite + 実 worker サブプロセスで書く ([[test-fixtures-from-real-transcripts]])
- **本プラン中の `src/…:NNN` という行番号参照は v1 執筆時点のもので、着手時にはズレている**
  (opus r1 M10 実測: `_DENY_NAMES` は `:120-127` と書いてあるが現物 `:120-129`、
  `check_source` は `:153-188` と書いてあるが現物 `:153-190`)。**逐語転写の前に必ず
  `rg -n '<記号名>' <path>` で行番号を再取得すること**。`sed -n 'A,Bp'` を本文の行番号
  そのままで使わない
- **コード片中の `...` は「この行以降は既存コードのまま、触らない」という意味**であり、
  貼り付ける文字列ではない (opus r1 §5)。本プランに **12 箇所**残っている (v1.3 時点)
  (v1.3 時点で **12 箇所**。着手時に
  `rg -n '^\s*\.\.\.' docs/superpowers/plans/2026-09-14-indicator-consumption-wiring.md`
  で再取得すること — 各 `...` の行末コメントがどの既存ブロックを指すかを書いてある)。
  `...` の前後を貼る前に**その周辺の既存コードを `git diff` で確認し、既存行を
  消していないこと**を diff で確かめてから次へ進む
  ([[transcription-must-be-machine-diffed]])
- **fixture の実測は済んでいる** (着手前検証、`tmp/plan-indicator-wiring/probe_fixture.py` /
  `probe_fixture.txt`): 設計書 §6 の bar 生成式 → 1h resample → Wilder RSI(14)
  (`ewm(alpha=1/14, adjust=False, min_periods=14)`) を in_sample `[2025-11-03, 2026-02-01)` で
  実行すると **RSI min = 27.2159 / max = 78.4162、long open 52 回 / short open 50 回 (計 102)、
  先頭 14 本が NaN で index 14 (15 本目) から値が入る**。設計書 §6 の warmup 記述
  ("最初の評価から 14 本目までは NaN で hold、15 本目以降で値") と一致する。
  **T6a の fixture 自己整合テストの期待値はこの実測値を上限・下限の目安として書くこと**
  (逐語の 102 を pin すると resample 実装差で脆いので、`opens >= 30` と
  `27.0 < rsi.min() < 30.0` / `70.0 < rsi.max() < 80.0` の形で pin する)

---

## File Structure

### 新規

| path | 責務 |
|---|---|
| `src/agentic_fx/plugin/resolve.py` | 依存解決の**唯一の場所**。`ApprovedInventory` / `ResolvedIndicator` / `ResolvedIndicatorSet` / `RejectedStrategy` / `InventoryBuildResult` / `IndicatorResolutionError` / `resolve_indicator_deps` / `freeze_params` / `thaw` / `lock_config` / `strip_pins` / `same_modulo_pins` / `is_relock_transition` / 4 つの上限定数 |
| `src/agentic_fx/core/plugin_contract.py` | `validate_indicator_result(result, *, df_index, outputs)` の**唯一の実装**。worker (同居実行の検証) と sandbox (standalone wire の境界再検証) の双方が import する。`plugin/worker.py` は `sandbox.py` を import できない (contracts / subprocess を子プロセスへ持ち込まない既存規律) ため、共有先を独立モジュールにする |
| `tests/plugin/test_resolve.py` | R1 (表駆動 resolver) / lock_config / `same_modulo_pins` / `is_relock_transition` の単体 |
| `tests/plugin/test_indicator_wiring_e2e.py` | A1 / A1-b / C1 / N1 / P3'。実 worker + 実 sqlite |
| `tests/plugin/test_indicator_containment.py` | V3 (差し替え拒否、3 root) |
| `tests/fixtures/indicator_wiring.py` | 受入 fixture の唯一の生成器 (3 indicator + `rsi_pullback` + synthetic bars + 独立参照実装 oracle)。**`tests/` 配下、`plugins/` は触らない** |
| `tests/fixtures/wiring_envs.py` | T4 / T5 のテストが共有する環境ビルダ 22 本 (`switch_env` / `reconcile_env` / `improve_env` / `prepare_ctx` / `activity_text` / `shell_env` / `rpc_tooldefs` / `rpc_tools` / `deploy_strategy` / `bump_indicator_version` / `stage_switched_journal` 等)。**本プラン内で定義される唯一の場所** (T6b)。T3 は使わない (opus r1 M12) |
| `tests/fixtures/test_wiring_envs.py` | 上記 22 ビルダの smoke test (T6b、opus r1 I3 / codex plan r1 C1)。**ビルダ数は Produces の逐語シグネチャ一覧が正** — v1.2 は「20 本」と書いていたが実際の列挙は 21 本だった (codex plan r1 の是正で `install_gate_double` を足して 22 本) |
| `docs/examples/plugins/rsi_pullback/{plugin.py,config.yaml,test_plugin.py}` | 依存ありの strategy 例 (設計書 §2.10 で判定式が固定済み) |

### 変更

| path | 変更 |
|---|---|
| `src/agentic_fx/plugin/loader.py` | `_KNOWN_CONFIG_KEYS` に `indicators` / `outputs`、`IndicatorRef` dataclass、`PluginMeta.indicators` / `.outputs`、全 kind の `params` 再帰 JSON-safe 検証、reject reason 語彙、上限 |
| `src/agentic_fx/tools/plugin_loader.py` | `approved_plugins(conn, plugins_dir, *, settings) -> InventoryBuildResult` (二相) |
| `src/agentic_fx/plugin/sandbox.py` | `PluginSession(..., resolved=None)`、`__enter__` の indicator 検査、handshake 拡張、`_KIND_PAYLOAD_KEYS["strategy"] = ("df","params")`、graceful close + `cpu_sec` property、`_DENY_NAMES` 追加 + 属性 Store reject、standalone wire の境界再検証 |
| `src/agentic_fx/plugin/worker.py` | indicator の一意名 import、tail + deep copy → compute → validate → NaN 整列、`evaluate(df, indicators, None, params)`、standalone wire、`op=close` 応答 (`cpu_sec`)、グローバル状態 assert |
| `src/agentic_fx/plugin/strategy_adapter.py` | `resolved` 必須 kwarg、`decision_sink`、`cpu_sec` property、payload `{df, params}` |
| `src/agentic_fx/plugin/strategy_gate.py` | `evaluate_strategy_adoption_gate(..., inventory, resolved)`、baseline を inventory 正本に、`StrategyGateVerdict.cpu_samples` |
| `src/agentic_fx/plugin/approval.py` | `GateOutcome.verdict_kind="indicator_unresolved"` + `indicator_alias` / `indicator_reason` / `resolved`、`run_kind_gate(..., inventory=)` |
| `src/agentic_fx/plugin/switch.py` | `_plugin_locks`、`_run_full_gate` の inventory 構築 + `outputs_required` + 固定 ValueError、`submit_candidate` / `bless_candidate` / `approve_candidate` のロック集合と再解決、`reconcile_switch_journals` の pin 破れ revert、payload `indicator_deps` |
| `src/agentic_fx/plugin/noop_gate.py` | `find_noop_copy(..., examples_dir, inventory)`、`same_modulo_pins` + `is_relock_transition` 例外 |
| `src/agentic_fx/plugin/signal_producer.py` | `evaluate_due_plugins(..., resolved_by_identity)`、strategy は解決済みのみ評価 (codex plan r2 束2 Critical: キーは `(meta.name, meta.content_hash)`。session cache は現状どおり `content_hash` 単独のまま — 根拠は Step 3-2c 参照) |
| `src/agentic_fx/service.py` | `approved_plugins(..., settings=)` の新契約、`InventoryBuildResult` を producer / registry へ |
| `src/agentic_fx/mission_worker.py` | trade 子: `approved_plugins(conn, plugins_dir, settings=settings)` → `.inventory.metas`。improve 子: `inventory_view` を registry へ |
| `src/agentic_fx/runners/worker_runner.py` | handshake に `inventory_view` |
| `src/agentic_fx/tools/market_tools.py` | `get_indicators` の系列末尾射影 + NaN キー除去 |
| `src/agentic_fx/backtest/cli.py` | `_backtest_run_plugin` の解決 (`check`) + rc=1 固定 stderr、`afx plugin lock --from _human <name>` サブコマンド |
| `src/agentic_fx/loops/improve_run_context.py` | `inventory: InventoryBuildResult` / `inventory_view: dict` |
| `src/agentic_fx/loops/improve_loop.py` | `_materialize_workspace` の二相 snapshot、`ImproveRunContext` 拡張、`run_backtest_handler` の解決 + `started`、commit gate の解決 + `outputs_required`、`_build_approval_payload` の `indicator_deps`、質検査の再ロック除外、`backtest_cpu` activity |
| `src/agentic_fx/loops/improve_context.py` | inventory 表示に `params` / `outputs` / `content_hash` |
| `src/agentic_fx/tools/improve_rpc_tools.py` | `started is False` で `release_backtest` |
| `src/agentic_fx/tools/mission_counters.py` | `release_backtest(name)` |
| `src/agentic_fx/tools/improve_staging_tools.py` | `list_deployed_plugins` / `lock_staging_deps` |
| `src/agentic_fx/tools/mission_registry.py` | `inventory_view` の透通 |
| `src/agentic_fx/loops/prompts/improve_mission.md` | 規律文 + 使用可能 tool 列挙 |
| `src/agentic_fx/commands.py` | `_approval_detail` の依存 strategy 2 欄 |
| `docs/examples/plugins/rsi_indicator/` | 系列返却 + `outputs: [rsi]` (Wilder 平滑) |
| `docs/superpowers/specs/2026-07-25-agentic-fx-design.md` | plugin 契約文 (indicator は系列も返せる / strategy は宣言依存を受ける) |
| `docs/superpowers/plans/2026-08-02-phase2-7-plugins.md:246` | 「indicators/signals 供給は将来拡張 — None 固定」の訂正 |
| tests (既存) | `test_strategy_adapter.py:167-170` 反転、`test_sandbox.py` の payload 縮小、`test_plugin_loader.py` / `test_service_app.py:305-334` / `test_e2e_plugin_signal.py:202-206` の `InventoryBuildResult` 移行、`test_improve_loop_plugin_gate.py` の `find_noop_copy` 新引数 |

### 実行順序と並列可否

**T1 → T2 → T6a → {T3 ∥ T6b} → T4a → T4b → T5a → T5b** (9 task)。

> **opus r1 観点 7 で T6 / T4 / T5 を分割した** (旧 6 task はいずれも 1 セッションの
> 上限を超えていた): T6 → **T6a** (Step 6-1〜6-4: example + fixture + docs) /
> **T6b** (`tests/fixtures/wiring_envs.py` の 22 ビルダ + 各ビルダの smoke test)、
> T4 → **T4a** (Step 4-1〜4-3) / **T4b** (Step 4-4〜4-8)、
> T5 → **T5a** (Step 5-1〜5-3) / **T5b** (Step 5-4〜5-6)。

**依存グラフ**:

```
T1 ──► T2 ──► T6a ──┬──► T3 ───┐
                    └──► T6b ──┴──► T4a ──► T4b ──► T5a ──► T5b
                         (T4a 以降のすべてが wiring_envs を Consumes)
```

| task | 着手条件 (main にマージ済み) | worktree 並列 |
|---|---|---|
| T1 | — | 不可 |
| T2 | T1 | 不可 |
| T6a | T2 | **T2 完了コミットから並列着手可** (T3 着手前にマージ) |
| T6b | T6a | **T3 と並列可** (T3 は `wiring_envs` を使わない — opus r1 M12)。T4a 着手前にマージ |
| T3 | T1・T2・T6a | **T6b と並列可** |
| T4a | T3・T6b | 不可 |
| T4b | T4a・T6b | 不可 |
| T5a | T4a・T4b・T6b | 不可 |
| T5b | T5a・T4b | 不可 |

- T6a は T2 完了後に**別 worktree で並列着手してよい**が、**T3 着手前に main へマージすること** —
  T3 の A1' と T4a の A1 / A1-b は T6a の fixture (`tests/fixtures/indicator_wiring.py`) を
  `Consumes` する。
- **T6b と T3 の 2 レーンだけが同時進行可能**。T6b は **T4a 着手前**に main へマージする
  (T4a Step 4-2 のテストが `wiring_envs.switch_env` を使う)。
- それ以外は互いに前段の型・シグネチャを `Consumes` するため worktree 並列不可 (1 レーン直列)。
- task 内分散は可 (テスト転写と実装転写を並列執筆 → 統合 + red/green は 1 レーン直列)。

---

## T1: loader の新キー + `plugin/resolve.py` + `approved_plugins` 二相 [loader-and-resolver]

**対応**: 設計書 §2.2 / §2.3 / §4 の `loader.py` / `resolve.py` / `tools/plugin_loader.py` 行。
**完了条件の受入 ID**: L1 / R1 / U4b (resolver 側) / P1 のうち `lock_config` 単体。

**Files:**
- Modify: `src/agentic_fx/plugin/loader.py:75-87` (`_KNOWN_CONFIG_KEYS`)、`:90-100` (`PluginMeta`)、
  `:137-208` (`_validate_config`)、`:426-436` (`PluginMeta` 構築)
- Create: `src/agentic_fx/plugin/resolve.py`
- Modify: `src/agentic_fx/tools/plugin_loader.py:37-70` (`approved_plugins`)
- Modify: `src/agentic_fx/service.py:823`、`src/agentic_fx/mission_worker.py:913`、
  `src/agentic_fx/loops/improve_loop.py:845`、`src/agentic_fx/loops/improve_context.py:84`
  (caller 移行の最小形 — 意味を変えず `.inventory.metas` を取るだけ)
- Test: `tests/plugin/test_loader.py` (追記)、`tests/plugin/test_resolve.py` (新規)、
  `tests/tools/test_plugin_loader.py` (移行)、`tests/test_service_app.py:305-334` (移行)、
  `tests/test_e2e_plugin_signal.py:202-206` (移行)

**Interfaces:**

- Consumes: 既存 `loader.PluginMeta(name, kind, path, params, timeframe, pairs, max_bars,
  content_hash, artifact_hash=None)`、`loader.discover(plugins_dir, *, activity=None) -> list[PluginMeta]`、
  `loader._reject(name, reason)`、`loader.content_hash(plugin_dir) -> str`、
  `config.Settings.plugin.max_bars_limit: int`。
- Produces (後続 task が使う正確なシグネチャ):

```python
# src/agentic_fx/plugin/loader.py
@dataclass(frozen=True, slots=True)
class IndicatorRef:
    alias: str
    plugin: str
    params: dict          # JSON-safe、検証済み (mutable dict のまま — 現行 wire を壊さない)
    pin: str | None       # ^[0-9a-f]{64}$ または None

@dataclass(frozen=True, slots=True)
class PluginMeta:
    name: str
    kind: str
    path: Path
    params: dict
    timeframe: str | None
    pairs: tuple[str, ...]
    max_bars: int
    content_hash: str
    artifact_hash: str | None = None
    indicators: tuple[IndicatorRef, ...] = ()      # 宣言順。strategy 以外は必ず ()
    outputs: tuple[str, ...] | None = None         # indicator 以外は必ず None

MAX_INDICATOR_DEPS: int = 8
MAX_OUTPUTS: int = 32
MAX_PARAMS_BYTES: int = 8192

# src/agentic_fx/plugin/resolve.py
MAX_HANDSHAKE_BYTES: int = 262144

class IndicatorResolutionError(Exception):
    def __init__(self, alias: str | None, reason: str) -> None: ...
    alias: str | None
    reason: str
    # str(exc) == f"indicator_unresolved:{alias}:{reason}" (alias が None なら "-")

@dataclass(frozen=True, slots=True)
class ApprovedInventory:
    root: Path                       # 実体パス (resolve() 済み)
    metas: tuple[PluginMeta, ...]
    def by_name(self, name: str) -> PluginMeta | None: ...

@dataclass(frozen=True, slots=True)
class ResolvedIndicator:
    alias: str
    plugin_name: str
    plugin_py: Path
    content_hash: str
    params: tuple                    # freeze_params の戻り (再帰 frozen)
    max_bars: int
    outputs: tuple[str, ...]
    pinned: bool

@dataclass(frozen=True, slots=True)
class ResolvedIndicatorSet:
    inventory_root: Path
    items: tuple[ResolvedIndicator, ...]     # alias 昇順
    all_pinned: bool
    @staticmethod
    def empty(inventory_root: Path) -> "ResolvedIndicatorSet": ...
    def pins(self) -> dict: ...              # {alias: content_hash} (lock_config への入力)
    def pin_object(self) -> dict: ...        # {alias: {plugin, content_hash, params}} plain JSON
    def handshake_items(self) -> list[dict]: ...
        # [{"alias", "plugin_py": str, "params": <plain JSON>, "max_bars", "outputs": [...]}]

@dataclass(frozen=True, slots=True)
class RejectedStrategy:
    name: str
    content_hash: str
    alias: str | None
    reason: str

@dataclass(frozen=True)
class InventoryBuildResult:
    inventory: ApprovedInventory
    phase1_metas: tuple[PluginMeta, ...]
    resolved: dict[tuple[str, str], ResolvedIndicatorSet]   # (name, content_hash) -> set
    rejected_strategies: tuple[RejectedStrategy, ...]

def freeze_params(obj): ...              # dict -> ("d", frozenset of (k, frozen)), list -> ("l", tuple)
def thaw(frozen): ...                    # call ごとに完全独立な plain JSON object
def resolve_indicator_deps(meta: PluginMeta, inventory: ApprovedInventory, *,
                           settings, pin_mode: Literal["require", "check", "ignore"],
                           ) -> ResolvedIndicatorSet: ...
def lock_config(candidate_dir: Path, pins: Mapping[str, str]
                ) -> tuple[str, str, str]: ...
    # (before_text, after_text, new_content_hash) を返す。副作用 = config.yaml の
    # 再シリアライズ書き込み。new_content_hash は書き込み**後**に disk から
    # content_hash() を再計算した値 (ユーザー裁定 2026-09-14 ⑥: lock 後は必ず
    # snapshot を取り直す — 呼び出し元は resolve 時点の hash を使い回さない)
def strip_pins(config: dict) -> dict: ...
def same_modulo_pins(candidate_dir: Path, other_dir: Path) -> bool: ...
def is_relock_transition(candidate_dir: Path, deployed_dir: Path,
                         inventory: ApprovedInventory) -> bool: ...

# src/agentic_fx/tools/plugin_loader.py
def approved_plugins(conn: sqlite3.Connection, plugins_dir: Path, *,
                     settings) -> InventoryBuildResult: ...
```

### Step 1-1: loader の `indicators` / `outputs` schema

- [ ] **Step 1-1a: Write the failing test**

`tests/plugin/test_loader.py` の末尾に追記:

```python
# --- [indicator-consumption-wiring] T1: indicators / outputs schema (L1) ---

_STRATEGY_PY = (
    "def evaluate(df, indicators, signals, params):\n"
    "    return {'action': 'hold', 'rationale': 'x'}\n")
_INDICATOR_PY = "def compute(df, params):\n    return {}\n"
_TEST_PY = "def test_placeholder():\n    pass\n"


def _write(base, name, *, plugin_py, config_yaml):
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(_TEST_PY)
    return d


_STRATEGY_HEAD = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
                  "exit_mode: levels\nmax_bars: 200\n")


@pytest.mark.parametrize("body,reason", [
    ("indicators:\n  rsi: {plugin: rsi, bogus: 1}\n",
     "indicator_ref_unknown_key:bogus"),
    ("indicators:\n  rsi: {params: {p: 1}}\n", "indicator_ref_missing_plugin"),
    ("indicators:\n  rsi: {plugin: 'Bad Name'}\n", "indicator_ref_bad_plugin_name"),
    ("indicators:\n  RSI: {plugin: rsi}\n", "indicator_ref_bad_alias"),
    ("indicators:\n  rsi: {plugin: rsi, pin: 'zz'}\n", "indicator_ref_bad_pin"),
    ("outputs: [rsi]\n", "outputs_not_allowed_for_kind"),
])
def test_strategy_config_rejects_with_fixed_reason(tmp_path, body, reason):
    d = _write(tmp_path, "s1", plugin_py=_STRATEGY_PY,
               config_yaml=_STRATEGY_HEAD + body)
    meta, got = loader.discover_one_with_reason(d, "s1")
    assert meta is None
    assert got == reason


def test_indicator_config_rejects_indicators_key(tmp_path):
    d = _write(tmp_path, "i1", plugin_py=_INDICATOR_PY,
               config_yaml="kind: indicator\nindicators:\n  a: {plugin: b}\n")
    meta, got = loader.discover_one_with_reason(d, "i1")
    assert meta is None
    assert got == "indicators_not_allowed_for_kind"


@pytest.mark.parametrize("outputs,reason", [
    ("outputs: []\n", "outputs_bad_entry"),
    ("outputs: [rsi, rsi]\n", "outputs_duplicate"),
    ("outputs: [RSI]\n", "outputs_bad_entry"),
    ("outputs: [1]\n", "outputs_bad_entry"),
])
def test_indicator_outputs_rejects(tmp_path, outputs, reason):
    d = _write(tmp_path, "i2", plugin_py=_INDICATOR_PY,
               config_yaml="kind: indicator\n" + outputs)
    meta, got = loader.discover_one_with_reason(d, "i2")
    assert meta is None
    assert got == reason


def test_same_plugin_via_multiple_aliases_is_accepted(tmp_path):
    # opus r1 M7 是正: 旧名 `test_duplicate_alias_via_two_entries_is_rejected`
    # は「拒否」を名乗りながら**受理**を検査していた (U3 で同一 plugin の
    # 複数 alias は許容される)。名前を内容に合わせる。
    # YAML の重複キーは _NoDuplicateKeySafeLoader が先に落とすため、
    # 別名の重複は「同じ plugin を 2 alias」ではなく alias 自身の重複を作る
    # 経路が無い。ここでは alias 数の上限と、同一 plugin の複数 alias が
    # **許容される** ことを固定する (U3)。
    body = "indicators:\n" + "".join(
        f"  a{i}: {{plugin: rsi}}\n" for i in range(8))
    d = _write(tmp_path, "s8", plugin_py=_STRATEGY_PY,
               config_yaml=_STRATEGY_HEAD + body)
    meta, reason = loader.discover_one_with_reason(d, "s8")
    assert reason is None
    assert len(meta.indicators) == 8
    assert [r.alias for r in meta.indicators] == [f"a{i}" for i in range(8)]
    assert all(r.plugin == "rsi" and r.pin is None for r in meta.indicators)


def test_nine_indicator_deps_rejected(tmp_path):
    body = "indicators:\n" + "".join(
        f"  a{i}: {{plugin: rsi}}\n" for i in range(9))
    d = _write(tmp_path, "s9", plugin_py=_STRATEGY_PY,
               config_yaml=_STRATEGY_HEAD + body)
    meta, reason = loader.discover_one_with_reason(d, "s9")
    assert meta is None
    assert reason == "too_many_indicators"


def test_outputs_32_ok_33_rejected(tmp_path):
    ok = "outputs: [" + ", ".join(f"o{i}" for i in range(32)) + "]\n"
    ng = "outputs: [" + ", ".join(f"o{i}" for i in range(33)) + "]\n"
    d_ok = _write(tmp_path, "i32", plugin_py=_INDICATOR_PY,
                  config_yaml="kind: indicator\n" + ok)
    meta, reason = loader.discover_one_with_reason(d_ok, "i32")
    assert reason is None and len(meta.outputs) == 32
    d_ng = _write(tmp_path, "i33", plugin_py=_INDICATOR_PY,
                  config_yaml="kind: indicator\n" + ng)
    meta, reason = loader.discover_one_with_reason(d_ng, "i33")
    assert meta is None and reason == "too_many_outputs"


def test_indicator_without_outputs_still_discovers(tmp_path):
    """U4: loader では outputs は任意 (既存配備との discover 互換)。"""
    d = _write(tmp_path, "i0", plugin_py=_INDICATOR_PY,
               config_yaml="kind: indicator\nparams:\n  period: 14\n")
    meta, reason = loader.discover_one_with_reason(d, "i0")
    assert reason is None
    assert meta.outputs is None
    assert meta.indicators == ()


def test_strategy_indicator_ref_is_frozen_and_ordered(tmp_path):
    body = ("indicators:\n"
            "  rsi: {plugin: rsi_wilder, params: {period: 21}}\n"
            "  adx: {plugin: adx, pin: '" + "3f9a" * 16 + "'}\n")
    d = _write(tmp_path, "s2", plugin_py=_STRATEGY_PY,
               config_yaml=_STRATEGY_HEAD + body)
    meta, reason = loader.discover_one_with_reason(d, "s2")
    assert reason is None
    assert [r.alias for r in meta.indicators] == ["rsi", "adx"]  # 宣言順
    assert meta.indicators[0].params == {"period": 21}
    assert meta.indicators[1].pin == "3f9a" * 16
    assert meta.outputs is None
    with pytest.raises(Exception):
        meta.indicators[0].alias = "x"   # frozen dataclass
```

- [ ] **Step 1-1b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_loader.py -k "indicator_ref or outputs or indicators" -q`
Expected: FAIL — `unknown config keys: ['indicators']` が reason に返る (新語彙が無い)、
`AttributeError: 'PluginMeta' object has no attribute 'indicators'`。

- [ ] **Step 1-1c: Write minimal implementation**

`src/agentic_fx/plugin/loader.py`:

```python
# :86-87 を置換
_KNOWN_CONFIG_KEYS = frozenset(
    {"kind", "params", "timeframe", "pairs", "exit_mode", "max_bars",
     "indicators", "outputs"})

# 上限 (設計書 §2.2、codex r5 C1)。handshake 総量上限は resolve.py が持つ。
MAX_INDICATOR_DEPS = 8
MAX_OUTPUTS = 32
MAX_PARAMS_BYTES = 8192

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_OUTPUT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PIN_RE = re.compile(r"^[0-9a-f]{64}$")
_INDICATOR_REF_KEYS = frozenset({"plugin", "params", "pin"})


@dataclass(frozen=True, slots=True)
class IndicatorRef:
    alias: str
    plugin: str
    params: dict
    pin: str | None
```

`PluginMeta` に 2 フィールドを追加 (既存フィールドの後ろ、既定値付きなので
既存の位置引数呼び出しを壊さない):

```python
@dataclass(frozen=True, slots=True)
class PluginMeta:
    name: str
    kind: str
    path: Path
    params: dict
    timeframe: str | None
    pairs: tuple[str, ...]
    max_bars: int
    content_hash: str
    artifact_hash: str | None = None  # プラン10 Task 5 5-F (申し送り③)
    # [indicator-consumption-wiring] 設計書 §2.2。strategy 以外は必ず ()、
    # indicator 以外の outputs は必ず None (kind 限定は _validate_config)。
    indicators: tuple["IndicatorRef", ...] = ()
    outputs: tuple[str, ...] | None = None
```

`_validate_config` の `max_bars` 検証の直後 (`:200` の後) に追加し、戻り dict へ 2 キーを足す:

```python
    indicators: tuple[IndicatorRef, ...] = ()
    if "indicators" in raw:
        if kind != "strategy":
            _reject(name, "indicators_not_allowed_for_kind")
            return None
        refs = raw["indicators"]
        if not isinstance(refs, dict) or not refs:
            _reject(name, "indicator_ref_bad_alias")
            return None
        if len(refs) > MAX_INDICATOR_DEPS:
            _reject(name, "too_many_indicators")
            return None
        built: list[IndicatorRef] = []
        seen: set[str] = set()
        for alias, ref in refs.items():
            if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
                _reject(name, "indicator_ref_bad_alias")
                return None
            if alias in seen:
                _reject(name, "indicator_ref_duplicate_alias")
                return None
            seen.add(alias)
            if not isinstance(ref, dict):
                _reject(name, "indicator_ref_missing_plugin")
                return None
            unknown = sorted(set(ref) - _INDICATOR_REF_KEYS)
            if unknown:
                _reject(name, f"indicator_ref_unknown_key:{unknown[0]}")
                return None
            plugin_name = ref.get("plugin")
            if not isinstance(plugin_name, str) or not plugin_name:
                _reject(name, "indicator_ref_missing_plugin")
                return None
            if not _PLUGIN_NAME_RE.fullmatch(plugin_name):
                _reject(name, "indicator_ref_bad_plugin_name")
                return None
            ref_params = ref.get("params", {})
            if not isinstance(ref_params, dict):
                _reject(name, f"params_not_json_safe:indicators.{alias}.params")
                return None
            bad = _check_json_safe(ref_params, f"indicators.{alias}.params")
            if bad is not None:
                _reject(name, bad)
                return None
            pin = ref.get("pin")
            if pin is not None and (not isinstance(pin, str)
                                    or not _PIN_RE.fullmatch(pin)):
                _reject(name, "indicator_ref_bad_pin")
                return None
            built.append(IndicatorRef(alias=alias, plugin=plugin_name,
                                      params=ref_params, pin=pin))
        indicators = tuple(built)

    outputs: tuple[str, ...] | None = None
    if "outputs" in raw:
        if kind != "indicator":
            _reject(name, "outputs_not_allowed_for_kind")
            return None
        raw_outputs = raw["outputs"]
        if not isinstance(raw_outputs, list) or not raw_outputs:
            _reject(name, "outputs_bad_entry")
            return None
        if len(raw_outputs) > MAX_OUTPUTS:
            _reject(name, "too_many_outputs")
            return None
        for item in raw_outputs:
            if not isinstance(item, str) or not _OUTPUT_RE.fullmatch(item):
                _reject(name, "outputs_bad_entry")
                return None
        if len(set(raw_outputs)) != len(raw_outputs):
            _reject(name, "outputs_duplicate")
            return None
        outputs = tuple(raw_outputs)

    return {
        "kind": kind,
        "params": params,
        "timeframe": timeframe,
        "pairs": pairs,
        "max_bars": max_bars,
        "indicators": indicators,
        "outputs": outputs,
    }
```

`_discover_one` の `PluginMeta(...)` 構築 (`:426-436`) に 2 引数を追加:

```python
        indicators=fields["indicators"],
        outputs=fields["outputs"],
```

(`_check_json_safe` の扱い — opus r1 M5 で**一意に確定**: Step 1-1 では
`def _check_json_safe(value: object, path: str) -> str | None: return None`
の**スタブを置く**。Step 1-1 は import エラーなく green にし、Step 1-2 で
このスタブを本実装に置き換える。「スタブを置かず Step 1-2 と同じコミットに
入れてもよい」という選択肢は取らない — Step 1-1d の `Expected: PASS` と
矛盾するため。)

- [ ] **Step 1-1d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_loader.py -q`
Expected: PASS (既存テストも全て緑 — 新キーは既定値付きなので既存 config を壊さない)

- [ ] **Step 1-1e: Commit**

```bash
git add src/agentic_fx/plugin/loader.py tests/plugin/test_loader.py
git commit -m "feat(loader): indicators/outputs config keys with fixed reject reasons (L1)"
```

### Step 1-2: 全 kind の `params` 再帰 JSON-safe 検証

- [ ] **Step 1-2a: Write the failing test**

`tests/plugin/test_loader.py` に追記:

```python
@pytest.mark.parametrize("config_body,reason", [
    ("params:\n  d: 2026-01-01\n", "params_not_json_safe:params.d"),
    ("params:\n  s: !!set {a: null}\n", "params_not_json_safe:params.s"),
    ("params:\n  f: .inf\n", "params_not_json_safe:params.f"),
    ("params:\n  f: .nan\n", "params_not_json_safe:params.f"),
    ("params:\n  b: !!binary aGk=\n", "params_not_json_safe:params.b"),
    ("params:\n  nested:\n    - {deep: .inf}\n",
     "params_not_json_safe:params.nested[0].deep"),
])
def test_params_json_safe_rejects(tmp_path, config_body, reason):
    d = _write(tmp_path, "pj", plugin_py=_INDICATOR_PY,
               config_yaml="kind: indicator\n" + config_body)
    meta, got = loader.discover_one_with_reason(d, "pj")
    assert meta is None
    assert got == reason


def test_params_json_safe_accepts_all_allowed_types(tmp_path):
    body = ("params:\n  s: text\n  i: 3\n  b: true\n  f: 1.5\n  n: null\n"
            "  l: [1, two, null]\n  m: {a: 1}\n")
    d = _write(tmp_path, "pok", plugin_py=_INDICATOR_PY,
               config_yaml="kind: indicator\n" + body)
    meta, reason = loader.discover_one_with_reason(d, "pok")
    assert reason is None
    assert meta.params["l"] == [1, "two", None]


def test_params_too_large_boundary(tmp_path):
    from agentic_fx.plugin.loader import MAX_PARAMS_BYTES
    import json as _json
    # canonical JSON がちょうど MAX_PARAMS_BYTES になる値を作る
    fixed = len(_json.dumps({"big": ""}, separators=(",", ":"),
                            sort_keys=True, ensure_ascii=False))
    ok_value = "x" * (MAX_PARAMS_BYTES - fixed)
    d_ok = _write(tmp_path, "pbok", plugin_py=_INDICATOR_PY,
                  config_yaml=f"kind: indicator\nparams:\n  big: '{ok_value}'\n")
    meta, reason = loader.discover_one_with_reason(d_ok, "pbok")
    assert reason is None
    d_ng = _write(tmp_path, "pbng", plugin_py=_INDICATOR_PY,
                  config_yaml=f"kind: indicator\nparams:\n  big: '{ok_value}x'\n")
    meta, reason = loader.discover_one_with_reason(d_ng, "pbng")
    assert meta is None
    assert reason == "params_too_large:params"


def test_indicator_ref_params_too_large(tmp_path):
    from agentic_fx.plugin.loader import MAX_PARAMS_BYTES
    value = "x" * MAX_PARAMS_BYTES
    body = f"indicators:\n  rsi: {{plugin: rsi, params: {{big: '{value}'}}}}\n"
    d = _write(tmp_path, "sbig", plugin_py=_STRATEGY_PY,
               config_yaml=_STRATEGY_HEAD + body)
    meta, reason = loader.discover_one_with_reason(d, "sbig")
    assert meta is None
    assert reason == "params_too_large:indicators.rsi.params"
```

- [ ] **Step 1-2b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_loader.py -k "json_safe or too_large" -q`
Expected: FAIL — 現行は `params` が dict でありさえすれば通る (`loader.py:153-156`)。

- [ ] **Step 1-2c: Write minimal implementation**

`src/agentic_fx/plugin/loader.py` にモジュール関数を追加 (`_validate_config` の前):

```python
def _check_json_safe(value, path: str) -> str | None:
    """`params` が JSON-safe かを再帰検査する (設計書 §2.2)。
    reject reason 文字列 (`params_not_json_safe:<path>` /
    `params_too_large:<path>`) を返す。問題なければ None。

    許容: str / int (bool 含む) / 有限 float / None / list / dict (キーは str)。
    YAML の date・datetime・set・bytes・±Inf・NaN は reject
    (json.dumps は NaN/Inf を通してしまうため型検査で先に落とす)。
    """
    stack = [(value, path)]
    while stack:
        node, node_path = stack.pop()
        if node is None or isinstance(node, (str, bool)):
            continue
        if isinstance(node, int):
            continue
        if isinstance(node, float):
            if not math.isfinite(node):
                return f"params_not_json_safe:{node_path}"
            continue
        if isinstance(node, list):
            for i, item in enumerate(node):
                stack.append((item, f"{node_path}[{i}]"))
            continue
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    return f"params_not_json_safe:{node_path}"
                stack.append((item, f"{node_path}.{key}"))
            continue
        return f"params_not_json_safe:{node_path}"
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True,
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return f"params_not_json_safe:{path}"
    if len(encoded.encode("utf-8")) > MAX_PARAMS_BYTES:
        return f"params_too_large:{path}"
    return None
```

import を追加 (`loader.py:17-26` の import 群):

```python
import json
import math
```

`_validate_config` の `params` 検証 (`:153-156`) を差し替え:

```python
    params = raw.get("params", {})
    if not isinstance(params, dict):
        _reject(name, "params must be a mapping")
        return None
    bad = _check_json_safe(params, "params")
    if bad is not None:
        _reject(name, bad)
        return None
```

- [ ] **Step 1-2d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_loader.py -q && uv run pytest tests/plugin -q`
Expected: PASS

- [ ] **Step 1-2e: Commit**

```bash
git add src/agentic_fx/plugin/loader.py tests/plugin/test_loader.py
git commit -m "feat(loader): recursive JSON-safe params validation for all kinds (L1)"
```

### Step 1-3: `plugin/resolve.py` の型と freeze/thaw

- [ ] **Step 1-3a: Write the failing test**

`tests/plugin/test_resolve.py` (新規):

```python
"""[indicator-consumption-wiring] plugin/resolve.py の単体テスト (R1)。

実 subprocess は使わない — resolver は純粋にファイル読取と辞書操作のみ。
DB は使わない (approved_plugins 二相のテストは tests/tools/test_plugin_loader.py)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_fx.plugin import resolve
from agentic_fx.plugin.loader import PluginMeta, content_hash, discover_one_with_reason
from agentic_fx.plugin.resolve import (
    ApprovedInventory, IndicatorResolutionError, ResolvedIndicatorSet,
    freeze_params, resolve_indicator_deps, thaw,
)

from tests.backtest.factories import SETTINGS

_TEST_PY = "def test_placeholder():\n    pass\n"
_INDICATOR_PY = "def compute(df, params):\n    return {}\n"
_STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
                "    return {'action': 'hold', 'rationale': 'x'}\n")


def _plugin(base: Path, name: str, config_yaml: str, plugin_py: str) -> PluginMeta:
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(config_yaml)
    (d / "test_plugin.py").write_text(_TEST_PY)
    meta, reason = discover_one_with_reason(d, name)
    assert reason is None, reason
    return meta


def _indicator(base: Path, name: str, *, outputs: str = "outputs: [v]\n",
               params: str = "params:\n  period: 14\n",
               max_bars: str = "") -> PluginMeta:
    return _plugin(base, name, "kind: indicator\n" + outputs + params + max_bars,
                   _INDICATOR_PY)


def _strategy(base: Path, name: str, body: str) -> PluginMeta:
    head = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
            "exit_mode: levels\nmax_bars: 200\n")
    return _plugin(base, name, head + body, _STRATEGY_PY)


def test_freeze_then_thaw_roundtrips_and_is_independent():
    src = {"a": [1, {"b": 2}], "c": None, "d": True}
    frozen = freeze_params(src)
    first = thaw(frozen)
    second = thaw(frozen)
    assert first == src
    assert second == src
    first["a"][1]["b"] = 999
    assert second["a"][1]["b"] == 2      # call ごとに完全独立 (deep)
    assert thaw(frozen)["a"][1]["b"] == 2


def test_frozen_params_is_hashable():
    hash(freeze_params({"a": [1, 2], "b": {"c": 3}}))


def test_empty_set_has_no_items_and_is_all_pinned():
    empty = ResolvedIndicatorSet.empty(Path("/plugins"))
    assert empty.items == ()
    assert empty.all_pinned is True
    assert empty.pin_object() == {}
    assert empty.handshake_items() == []


def test_pin_object_is_alias_sorted_plain_json(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    adx = _indicator(root, "adx")
    inv = ApprovedInventory(root=root.resolve(), metas=(rsi, adx))
    s = _strategy(tmp_path / "cand", "s",
                  "indicators:\n"
                  "  zeta: {plugin: rsi, params: {period: 21}}\n"
                  "  alpha: {plugin: adx}\n")
    resolved = resolve_indicator_deps(s, inv, settings=SETTINGS, pin_mode="ignore")
    assert [i.alias for i in resolved.items] == ["alpha", "zeta"]   # alias 昇順
    obj = resolved.pin_object()
    assert list(obj) == ["alpha", "zeta"]
    assert obj["zeta"] == {"plugin": "rsi", "content_hash": rsi.content_hash,
                           "params": {"period": 21}}
    assert obj["alpha"] == {"plugin": "adx", "content_hash": adx.content_hash,
                            "params": {"period": 14}}
    import json
    json.dumps(obj)  # plain JSON であること


def test_indicator_resolution_error_str_is_fixed_text():
    exc = IndicatorResolutionError("rsi", "pin_mismatch")
    assert exc.alias == "rsi" and exc.reason == "pin_mismatch"
    assert str(exc) == "indicator_unresolved:rsi:pin_mismatch"
    assert str(IndicatorResolutionError(None, "handshake_too_large")) == \
        "indicator_unresolved:-:handshake_too_large"
```

- [ ] **Step 1-3b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_resolve.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentic_fx.plugin.resolve'`

- [ ] **Step 1-3c: Write minimal implementation**

`src/agentic_fx/plugin/resolve.py` (新規、型 + freeze/thaw + 骨格):

```python
"""indicator 依存の解決 — 唯一の場所 ([indicator-consumption-wiring] 設計書 §2.3)。

strategy の `config.yaml` の `indicators:` 宣言を、承認済み inventory
(`ApprovedInventory`) に対して解決し、worker へ渡す `ResolvedIndicatorSet`
を作る。**merge/lookup/pin 検査の規則はこのモジュールの 1 箇所にのみ存在する** —
composition root (service / 改善 mission / 人間 CLI / 承認回廊) は
`resolve_indicator_deps` を呼ぶだけで、独自の突合を書かない。

`pin_mode` の使い分け (設計書 §2.3 の表が正):
- `"require"`: submit / bless / commit gate / `approved_plugins` 第 2 相。
  pin 必須かつ inventory の hash と一致。
- `"check"`: 探索中の `run_backtest` / 人間 CLI。pin があれば一致を要求、
  無ければ通す。
- `"ignore"`: ロック操作。既存 pin が古くても名前で解決し直す
  (`require`/`check` のままでは古い pin の再ロックに到達できない — codex r4)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

from agentic_fx.plugin.loader import PluginMeta, content_hash

# 展開後の canonical handshake 総 byte 数の上限 (設計書 §2.2、codex r5 C1)。
# 既存 `sandbox._STARTUP_MAX_BYTES` (65536、worker → 親の起動応答) とは
# 別枠 — こちらは親 → worker の送信側上限。
MAX_HANDSHAKE_BYTES = 262144

PinMode = Literal["require", "check", "ignore"]


class IndicatorResolutionError(Exception):
    """解決失敗の単一表現。`str(exc)` は固定文言
    `indicator_unresolved:<alias>:<reason>` (alias が None なら `-`)。
    呼び出し元はこの文字列をそのまま stderr / 例外文言に使う。"""

    def __init__(self, alias: str | None, reason: str) -> None:
        self.alias = alias
        self.reason = reason
        super().__init__(f"indicator_unresolved:{alias or '-'}:{reason}")


def freeze_params(obj: Any):
    """再帰的に hashable な frozen 表現へ。dict は ("d", frozenset of
    (key, frozen)) 、list は ("l", tuple of frozen)、スカラーはそのまま。
    `PluginMeta.params` は従来どおり dict のまま (現行 wire の json.dumps
    を壊さない、codex r3 C3) なので、不変性はこの別表現で担保する。"""
    if isinstance(obj, dict):
        return ("d", frozenset((k, freeze_params(v)) for k, v in obj.items()))
    if isinstance(obj, list):
        return ("l", tuple(freeze_params(v) for v in obj))
    return obj


def thaw(frozen: Any):
    """`freeze_params` の逆。**call ごとに完全に独立した plain JSON object**
    を作る (deep) — 同じ frozen から 2 回 thaw した結果は互いに影響しない。"""
    if isinstance(frozen, tuple) and len(frozen) == 2 and frozen[0] == "d":
        return {k: thaw(v) for k, v in frozen[1]}
    if isinstance(frozen, tuple) and len(frozen) == 2 and frozen[0] == "l":
        return [thaw(v) for v in frozen[1]]
    return frozen


@dataclass(frozen=True, slots=True)
class ApprovedInventory:
    root: Path                       # 実体パス (resolve() 済み)
    metas: tuple[PluginMeta, ...]

    def by_name(self, name: str) -> PluginMeta | None:
        for meta in self.metas:
            if meta.name == name:
                return meta
        return None


@dataclass(frozen=True, slots=True)
class ResolvedIndicator:
    alias: str
    plugin_name: str
    plugin_py: Path
    content_hash: str
    params: Any                      # freeze_params の戻り
    max_bars: int
    outputs: tuple[str, ...]
    pinned: bool


@dataclass(frozen=True, slots=True)
class ResolvedIndicatorSet:
    inventory_root: Path
    items: tuple[ResolvedIndicator, ...]
    all_pinned: bool

    @staticmethod
    def empty(inventory_root: Path) -> "ResolvedIndicatorSet":
        return ResolvedIndicatorSet(inventory_root=inventory_root, items=(),
                                    all_pinned=True)

    def pins(self) -> dict:
        """`lock_config` へ渡す `{alias: content_hash}` (alias 昇順)。"""
        return {i.alias: i.content_hash for i in self.items}

    def pin_object(self) -> dict:
        """承認 payload 用の plain JSON object (alias 昇順)。"""
        return {i.alias: {"plugin": i.plugin_name,
                          "content_hash": i.content_hash,
                          "params": thaw(i.params)}
                for i in self.items}

    def handshake_items(self) -> list[dict]:
        """`PluginSession.__enter__` が handshake に載せる形 (alias 順)。"""
        return [{"alias": i.alias, "plugin_py": str(i.plugin_py),
                 "params": thaw(i.params), "max_bars": i.max_bars,
                 "outputs": list(i.outputs)}
                for i in self.items]


@dataclass(frozen=True, slots=True)
class RejectedStrategy:
    name: str
    content_hash: str
    alias: str | None
    reason: str


@dataclass(frozen=True)
class InventoryBuildResult:
    inventory: ApprovedInventory
    phase1_metas: tuple[PluginMeta, ...]
    resolved: Mapping[tuple[str, str], ResolvedIndicatorSet]
    rejected_strategies: tuple[RejectedStrategy, ...]
```

- [ ] **Step 1-3d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_resolve.py -q -k "freeze or hashable or empty_set or error_str"`
Expected: PASS (`pin_object` のテストは Step 1-4 で緑になる)

- [ ] **Step 1-3e: Commit**

```bash
git add src/agentic_fx/plugin/resolve.py tests/plugin/test_resolve.py
git commit -m "feat(resolve): immutable resolution types + freeze/thaw"
```

### Step 1-4: `resolve_indicator_deps` (reason 語彙 + pin_mode + 上限)

- [ ] **Step 1-4a: Write the failing test**

`tests/plugin/test_resolve.py` に追記:

```python
def _inv(root: Path, *metas: PluginMeta) -> ApprovedInventory:
    return ApprovedInventory(root=root.resolve(), metas=tuple(metas))


def test_resolve_not_found(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root), settings=SETTINGS, pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "not_found")


def test_resolve_not_indicator(tmp_path):
    root = tmp_path / "plugins"
    other = _strategy(root, "rsi", "")
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, other), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "not_indicator")


def test_resolve_outputs_undeclared(tmp_path):
    """U4b: outputs 宣言なしの配備済 indicator は依存先にできない。"""
    root = tmp_path / "plugins"
    legacy = _indicator(root, "rsi", outputs="")
    assert legacy.outputs is None
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    for mode in ("require", "check"):
        with pytest.raises(IndicatorResolutionError) as ei:
            resolve_indicator_deps(s, _inv(root, legacy), settings=SETTINGS,
                                   pin_mode=mode)
        assert (ei.value.alias, ei.value.reason) == ("rsi", "outputs_undeclared")


def test_resolve_over_max_bars_limit(tmp_path):
    root = tmp_path / "plugins"
    big = _indicator(root, "rsi", max_bars=f"max_bars: {SETTINGS.plugin.max_bars_limit + 1}\n")
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, big), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "over_max_bars_limit")


def test_resolve_params_not_json_safe_after_merge(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    # strategy 側の上書きに非 JSON-safe 値を差し込む (loader を通さずに
    # meta を組み替えて merge 後検証だけを突く — loader は既に L1 で pin 済み)
    from agentic_fx.plugin.loader import IndicatorRef
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    broken = PluginMeta(
        name=s.name, kind=s.kind, path=s.path, params=s.params,
        timeframe=s.timeframe, pairs=s.pairs, max_bars=s.max_bars,
        content_hash=s.content_hash, artifact_hash=s.artifact_hash,
        indicators=(IndicatorRef(alias="rsi", plugin="rsi",
                                 params={"period": float("inf")}, pin=None),),
        outputs=None)
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(broken, _inv(root, ind), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "params_not_json_safe")


def test_resolve_unpinned_only_fails_under_require(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                               pin_mode="require")
    assert (ei.value.alias, ei.value.reason) == ("rsi", "unpinned")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="check")
    assert got.all_pinned is False and got.items[0].pinned is False


def test_resolve_pin_mismatch_under_require_and_check_but_ignored_under_ignore(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    stale = "a" * 64
    s = _strategy(tmp_path / "c", "s",
                  f"indicators:\n  rsi: {{plugin: rsi, pin: '{stale}'}}\n")
    for mode in ("require", "check"):
        with pytest.raises(IndicatorResolutionError) as ei:
            resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                   pin_mode=mode)
        assert (ei.value.alias, ei.value.reason) == ("rsi", "pin_mismatch")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="ignore")
    assert got.items[0].content_hash == ind.content_hash   # 名前で解決し直す
    assert got.items[0].pinned is True and got.all_pinned is True


def test_resolve_pin_match_under_require(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi")
    s = _strategy(tmp_path / "c", "s",
                  f"indicators:\n  rsi: {{plugin: rsi, pin: '{ind.content_hash}'}}\n")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="require")
    assert got.all_pinned is True
    assert got.items[0].plugin_py == ind.path / "plugin.py"
    assert got.items[0].outputs == ("v",)
    assert got.inventory_root == root.resolve()


def test_resolve_merges_strategy_params_over_indicator_defaults(tmp_path):
    root = tmp_path / "plugins"
    ind = _indicator(root, "rsi", params="params:\n  period: 14\n  src: close\n")
    s = _strategy(tmp_path / "c", "s",
                  "indicators:\n  rsi: {plugin: rsi, params: {period: 21}}\n")
    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="check")
    assert thaw(got.items[0].params) == {"period": 21, "src": "close"}
    # indicator 側の params dict は書き換えられない (deep copy)
    assert ind.params == {"period": 14, "src": "close"}


def test_resolve_handshake_too_large_boundary(tmp_path, monkeypatch):
    """R1 上限境界: `>` 判定を実測サイズの前後 1 byte で挟む。

    注: loader が通す入力 (deps <= 8 / params <= 8 KiB) では handshake は
    最大でも ~136 KB にしかならず、既定の 256 KiB には到達しない。よって
    定数を monkeypatch して境界そのものを pin する (設計 R1 の申し送り参照)。
    """
    import json
    root = tmp_path / "plugins"
    from agentic_fx.plugin.loader import MAX_PARAMS_BYTES
    blob = "x" * (MAX_PARAMS_BYTES - 64)
    ind = _indicator(root, "rsi", params=f"params:\n  big: '{blob}'\n")
    body = "indicators:\n" + "".join(
        f"  a{i}: {{plugin: rsi}}\n" for i in range(8))
    s = _strategy(tmp_path / "c", "s", body)

    got = resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                                 pin_mode="check")
    assert len(got.items) == 8
    size = len(json.dumps(got.handshake_items(), separators=(",", ":"),
                          sort_keys=True, ensure_ascii=False).encode("utf-8"))

    # ちょうど上限は通る (実装は `>` 判定)
    monkeypatch.setattr(resolve, "MAX_HANDSHAKE_BYTES", size)
    assert len(resolve_indicator_deps(
        s, _inv(root, ind), settings=SETTINGS, pin_mode="check").items) == 8

    # 1 byte 下げると落ちる
    monkeypatch.setattr(resolve, "MAX_HANDSHAKE_BYTES", size - 1)
    with pytest.raises(IndicatorResolutionError) as ei:
        resolve_indicator_deps(s, _inv(root, ind), settings=SETTINGS,
                               pin_mode="check")
    assert (ei.value.alias, ei.value.reason) == (None, "handshake_too_large")


def test_resolver_failure_spawns_no_worker(tmp_path, monkeypatch):
    """R1: 拒否時に worker spawn 0 回。"""
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("worker spawned")))
    root = tmp_path / "plugins"
    root.mkdir()
    s = _strategy(tmp_path / "c", "s", "indicators:\n  rsi: {plugin: rsi}\n")
    with pytest.raises(IndicatorResolutionError):
        resolve_indicator_deps(s, _inv(root), settings=SETTINGS, pin_mode="check")
    assert calls == []
```

- [ ] **Step 1-4b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_resolve.py -q`
Expected: FAIL — `AttributeError: module 'agentic_fx.plugin.resolve' has no attribute
'resolve_indicator_deps'`

- [ ] **Step 1-4c: Write minimal implementation**

`src/agentic_fx/plugin/resolve.py` に追記:

```python
def _merge_params(base: dict, override: dict) -> dict:
    """indicator の params の deep copy に strategy 側を 1 段上書き
    (設計書 §2.3)。base は絶対に書き換えない。"""
    merged = json.loads(json.dumps(base))   # deep copy (JSON-safe が前提)
    merged.update(json.loads(json.dumps(override)))
    return merged


def resolve_indicator_deps(meta: PluginMeta, inventory: ApprovedInventory, *,
                           settings, pin_mode: PinMode) -> ResolvedIndicatorSet:
    """`meta.indicators` を `inventory` に対して解決する。失敗は
    `IndicatorResolutionError(alias, reason)` — reason 語彙は設計書 §2.3 の
    8 語のみ。`max_bars` は「渡す履歴の最大本数」であって必要 warmup 本数
    ではないので、大小比較による拒否は入れない (codex r4 I3)。"""
    items: list[ResolvedIndicator] = []
    for ref in meta.indicators:
        dep = inventory.by_name(ref.plugin)
        if dep is None:
            raise IndicatorResolutionError(ref.alias, "not_found")
        if dep.kind != "indicator":
            raise IndicatorResolutionError(ref.alias, "not_indicator")
        if dep.outputs is None:
            raise IndicatorResolutionError(ref.alias, "outputs_undeclared")
        if dep.max_bars > settings.plugin.max_bars_limit:
            raise IndicatorResolutionError(ref.alias, "over_max_bars_limit")
        if pin_mode != "ignore":
            if ref.pin is None:
                if pin_mode == "require":
                    raise IndicatorResolutionError(ref.alias, "unpinned")
            elif ref.pin != dep.content_hash:
                raise IndicatorResolutionError(ref.alias, "pin_mismatch")
        try:
            merged = _merge_params(dep.params, ref.params)
            json.dumps(merged, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise IndicatorResolutionError(
                ref.alias, "params_not_json_safe") from exc
        items.append(ResolvedIndicator(
            alias=ref.alias, plugin_name=dep.name,
            plugin_py=dep.path / "plugin.py", content_hash=dep.content_hash,
            params=freeze_params(merged), max_bars=dep.max_bars,
            outputs=dep.outputs,
            pinned=(pin_mode == "ignore") or ref.pin is not None))
    items.sort(key=lambda i: i.alias)
    resolved = ResolvedIndicatorSet(
        inventory_root=inventory.root, items=tuple(items),
        all_pinned=all(i.pinned for i in items))
    encoded = json.dumps(resolved.handshake_items(), separators=(",", ":"),
                         sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_HANDSHAKE_BYTES:
        raise IndicatorResolutionError(None, "handshake_too_large")
    return resolved
```

- [ ] **Step 1-4d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_resolve.py -q`
Expected: PASS (14 tests)

- [ ] **Step 1-4e: Commit**

```bash
git add src/agentic_fx/plugin/resolve.py tests/plugin/test_resolve.py
git commit -m "feat(resolve): resolve_indicator_deps with fixed reason vocabulary and limits (R1/U4b)"
```

### Step 1-5: `lock_config` / `strip_pins` / `same_modulo_pins` / `is_relock_transition`

- [ ] **Step 1-5a: Write the failing test**

`tests/plugin/test_resolve.py` に追記:

```python
import shutil

import yaml

from agentic_fx.plugin.resolve import (
    is_relock_transition, lock_config, same_modulo_pins, strip_pins,
)


def test_lock_config_writes_pins_and_keeps_discoverable(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    adx = _indicator(root, "adx")
    cand = tmp_path / "cand"
    s = _strategy(cand, "s", "indicators:\n"
                             "  rsi: {plugin: rsi}\n  adx: {plugin: adx}\n")
    before_hash = s.content_hash
    resolved = resolve_indicator_deps(s, _inv(root, rsi, adx), settings=SETTINGS,
                                      pin_mode="ignore")
    before_text, after_text, new_hash = lock_config(cand / "s", resolved.pins())
    assert "pin:" not in before_text
    written = yaml.safe_load((cand / "s" / "config.yaml").read_text())
    assert written["indicators"]["rsi"]["pin"] == rsi.content_hash
    assert written["indicators"]["adx"]["pin"] == adx.content_hash
    assert after_text == (cand / "s" / "config.yaml").read_text()
    # ユーザー裁定 2026-09-14 ⑥: lock 後は必ず snapshot (content_hash) を
    # disk から取り直す。返り値の new_hash が、書き込み後の config.yaml を
    # 独立に再読して計算した content_hash() と一致すること (呼び出し元が
    # resolve 時点の値を使い回していないことの pin)。
    assert new_hash == content_hash(cand / "s")
    relocked, reason = discover_one_with_reason(cand / "s", "s")
    assert reason is None                       # P1: 書き換え後も discover を通る
    assert relocked.content_hash != before_hash  # pin は content_hash の署名対象
    assert relocked.content_hash == new_hash     # snapshot 再取得の値と一致
    # 既に同じ pin なら no-op (2 回目の lock で内容が変わらない)
    resolved2 = resolve_indicator_deps(relocked, _inv(root, rsi, adx),
                                       settings=SETTINGS, pin_mode="ignore")
    b2, a2, h2 = lock_config(cand / "s", resolved2.pins())
    assert b2 == a2
    assert h2 == new_hash                        # no-op でも snapshot は再計算される


def test_lock_config_overwrites_stale_pin(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    cand = tmp_path / "cand"
    s = _strategy(cand, "s",
                  "indicators:\n  rsi: {plugin: rsi, pin: '" + "b" * 64 + "'}\n")
    resolved = resolve_indicator_deps(s, _inv(root, rsi), settings=SETTINGS,
                                      pin_mode="ignore")
    lock_config(cand / "s", resolved.pins())
    written = yaml.safe_load((cand / "s" / "config.yaml").read_text())
    assert written["indicators"]["rsi"]["pin"] == rsi.content_hash


def test_strip_pins_removes_only_pins():
    cfg = {"kind": "strategy", "indicators": {
        "rsi": {"plugin": "rsi", "pin": "a" * 64, "params": {"p": 1}}}}
    out = strip_pins(cfg)
    assert out["indicators"]["rsi"] == {"plugin": "rsi", "params": {"p": 1}}
    assert cfg["indicators"]["rsi"]["pin"] == "a" * 64   # 入力は不変


def test_same_modulo_pins(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")
    a = tmp_path / "a"
    _strategy(a, "s", f"indicators:\n  rsi: {{plugin: rsi, pin: '{'a' * 64}'}}\n")
    b = tmp_path / "b"
    _strategy(b, "s", f"indicators:\n  rsi: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    c = tmp_path / "c"
    _strategy(c, "s", "indicators:\n  rsi: {plugin: rsi, params: {p: 1}}\n")
    assert same_modulo_pins(a / "s", b / "s") is True
    assert same_modulo_pins(a / "s", c / "s") is False


def test_is_relock_transition(tmp_path):
    root = tmp_path / "plugins"
    rsi = _indicator(root, "rsi")          # 現在 inventory の hash
    inv = _inv(root, rsi)
    deployed = tmp_path / "dep"
    _strategy(deployed, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{'a' * 64}'}}\n")  # 破れ
    cand = tmp_path / "cand"
    _strategy(cand, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    assert is_relock_transition(cand / "s", deployed / "s", inv) is True
    # 候補が古い pin のまま = 再ロックではない
    stale_cand = tmp_path / "stale"
    _strategy(stale_cand, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{'a' * 64}'}}\n")
    assert is_relock_transition(stale_cand / "s", deployed / "s", inv) is False
    # deployed の pin が破れていない = 再ロックではない
    fresh_dep = tmp_path / "fresh"
    _strategy(fresh_dep, "s",
              f"indicators:\n  rsi: {{plugin: rsi, pin: '{rsi.content_hash}'}}\n")
    assert is_relock_transition(cand / "s", fresh_dep / "s", inv) is False
    # 依存なしの strategy 同士は再ロックではない
    nodep_a, nodep_b = tmp_path / "na", tmp_path / "nb"
    _strategy(nodep_a, "s", "")
    _strategy(nodep_b, "s", "")
    assert is_relock_transition(nodep_a / "s", nodep_b / "s", inv) is False
```

- [ ] **Step 1-5b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_resolve.py -k "lock_config or strip_pins or modulo or relock" -q`
Expected: FAIL — `ImportError: cannot import name 'lock_config'`

- [ ] **Step 1-5c: Write minimal implementation**

`src/agentic_fx/plugin/resolve.py` に追記:

```python
def lock_config(candidate_dir: Path, pins: Mapping[str, str]
                ) -> tuple[str, str, str]:
    """候補の `config.yaml` の各 alias に `pin` を書き込む (U5: 全体を
    `yaml.safe_load` → pin 追加 → `yaml.safe_dump(sort_keys=False)` で
    再シリアライズ。コメント・キー順は保持しない — 呼び出し元が差分を
    人間へ表示する)。戻り値 `(before_text, after_text, new_content_hash)`。

    `pins` は `{alias: content_hash}`。resolver 経路の呼び出し元は
    `ResolvedIndicatorSet.pins()` を渡し (`pin_mode="ignore"` で解決した
    もの)、改善 worker の `lock_staging_deps` は `inventory_view` から
    組んだ dict を渡す — **YAML 書き換えの実装はここ 1 箇所に閉じる**
    (設計書 §2.3)。`pins` に無い alias は触らない。**既に同じ pin なら
    書き込み内容は変わらない** (before == after)。

    **snapshot 再取得** (ユーザー裁定 2026-09-14 ⑥): 書き込み後、disk 上の
    `config.yaml` を独立に再読して `content_hash()` を計算し直し、それを
    `new_content_hash` として返す。`check_candidate_snapshot`
    (`gate_pytest.py`) は 3 ファイルの存在・属性しか見ず内容の一致は
    検査しないため、その「lock で壊れるものはない」という主張だけに
    依存せず、呼び出し元 (`_plugin_lock` CLI / `lock_staging_deps` tool)
    は **resolve 時点で計算した hash を使い回さず、必ずこの戻り値を
    使う**。"""
    path = candidate_dir / "config.yaml"
    before_text = path.read_text(encoding="utf-8")
    config = yaml.safe_load(before_text)
    if not isinstance(config, dict):
        raise ValueError(f"lock_config: {path} is not a mapping")
    refs = config.get("indicators") or {}
    for alias, pin in pins.items():
        ref = refs.get(alias)
        if isinstance(ref, dict):
            ref["pin"] = pin
    after_text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                                default_flow_style=False)
    if after_text != before_text:
        path.write_text(after_text, encoding="utf-8")
    new_content_hash = content_hash(candidate_dir)
    return before_text, after_text, new_content_hash


def strip_pins(config: dict) -> dict:
    """`config` の deep copy から `indicators.<alias>.pin` だけを除いたもの。
    入力は書き換えない。pin は作者の設計判断ではなくハーネスの派生値なので、
    「実質的に同じ候補か」の比較はこの形で行う (設計書 §2.7)。

    **比較専用** (opus r1 M2): `default=str` は YAML の date / datetime を
    黙って文字列にする。loader が JSON-safe 検証をかけるのは `params` だけ
    なので、`config.yaml` の他キー (例: 作者が書いた `note: 2026-01-01`) に
    date があると、この関数を通した値は元の config と型が変わる。**この
    戻り値は `same_modulo_pins` / `is_relock_transition` の等価比較に
    しか使わない** — 両辺を同じ変換に通すので比較意味論は壊れないが、
    **この戻り値を書き戻したり handshake に載せたりしてはならない**
    (書き戻しは `lock_config` の `yaml.safe_load` → `safe_dump` 経路のみ)。
    """
    out = json.loads(json.dumps(config, default=str))
    refs = out.get("indicators")
    if isinstance(refs, dict):
        for ref in refs.values():
            if isinstance(ref, dict):
                ref.pop("pin", None)
    return out


def _config_of(plugin_dir: Path) -> dict:
    return yaml.safe_load((plugin_dir / "config.yaml").read_text(encoding="utf-8"))


def same_modulo_pins(candidate_dir: Path, other_dir: Path) -> bool:
    """正規化 AST の一致 **かつ** `strip_pins(config)` の一致。
    `noop_gate.normalized_plugin_ast` を再利用する (AST 正規化の実装は
    noop_gate 側の 1 箇所のみ)。"""
    from agentic_fx.plugin.noop_gate import normalized_plugin_ast
    if (normalized_plugin_ast(candidate_dir / "plugin.py")
            != normalized_plugin_ast(other_dir / "plugin.py")):
        return False
    return strip_pins(_config_of(candidate_dir)) == strip_pins(_config_of(other_dir))


def _pins_of(plugin_dir: Path) -> dict[str, str | None]:
    refs = (_config_of(plugin_dir) or {}).get("indicators") or {}
    return {alias: (ref.get("pin") if isinstance(ref, dict) else None)
            for alias, ref in refs.items()}


def is_relock_transition(candidate_dir: Path, deployed_dir: Path,
                         inventory: ApprovedInventory) -> bool:
    """「正式な再ロック経路 (複製 → pin だけ I1→I2 → 提出)」かどうか
    (設計書 §2.7、codex r4 C2)。`same_modulo_pins` **かつ** deployed の
    pin の少なくとも 1 つが現在 inventory と不一致 **かつ** candidate の
    pin が全て現在 inventory と一致。依存が 0 本なら常に False。"""
    if not same_modulo_pins(candidate_dir, deployed_dir):
        return False
    deployed_pins = _pins_of(deployed_dir)
    candidate_pins = _pins_of(candidate_dir)
    if not deployed_pins or not candidate_pins:
        return False

    # opus r1 M1 是正: 元案は `_current(alias, pins)` と引数 `pins` を
    # 取りながら本文で使っていなかった (死に引数 = 嘘のシグネチャ)。
    # plugin 名の引き元は candidate / deployed のどちらでもよい
    # (`same_modulo_pins` が `strip_pins` 一致を先に保証しているので
    # `indicators` の alias → plugin 対応は両者で同一) が、**どちらを
    # 読むかを明示**するため `plugin_dir` を引数にする。
    def _current(alias: str, plugin_dir: Path) -> str | None:
        refs = (_config_of(plugin_dir) or {}).get("indicators") or {}
        ref = refs.get(alias)
        if not isinstance(ref, dict):
            return None
        dep = inventory.by_name(ref.get("plugin"))
        return dep.content_hash if dep is not None else None

    stale = any(pin != _current(alias, deployed_dir)
                for alias, pin in deployed_pins.items())
    fresh = all(pin is not None and pin == _current(alias, candidate_dir)
                for alias, pin in candidate_pins.items())
    return stale and fresh
```

- [ ] **Step 1-5d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_resolve.py -q`
Expected: PASS

- [ ] **Step 1-5e: Commit**

```bash
git add src/agentic_fx/plugin/resolve.py tests/plugin/test_resolve.py
git commit -m "feat(resolve): lock_config / strip_pins / same_modulo_pins / is_relock_transition (P1)"
```

### Step 1-6: `approved_plugins` の二相化

- [ ] **Step 1-6a: Write the failing test**

`tests/tools/test_plugin_loader.py` に追記 (既存 helper `_write_plugin` / `_approve` / `_conn`
をそのまま使う):

```python
# --- [indicator-consumption-wiring] T1: 二相 inventory ---------------------

from tests.backtest.factories import SETTINGS as _SETTINGS

_STRATEGY_PY2 = ("def evaluate(df, indicators, signals, params):\n"
                 "    return {'action': 'hold', 'rationale': 'x'}\n")
_IND_SERIES_CONFIG = "kind: indicator\noutputs: [v]\nparams:\n  period: 14\n"


def _strategy_config(pin: str | None) -> str:
    ref = "{plugin: rsi" + (f", pin: '{pin}'" if pin else "") + "}"
    return ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
            "exit_mode: levels\nmax_bars: 200\n"
            f"indicators:\n  rsi: {ref}\n")


def test_phase2_admits_pinned_strategy_and_keeps_resolved(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    from agentic_fx.plugin.loader import content_hash
    rsi_dir = _write_plugin(plugins_dir, "rsi", config_yaml=_IND_SERIES_CONFIG)
    rsi_hash = content_hash(rsi_dir)
    s_dir = _write_plugin(plugins_dir, "s", plugin_py=_STRATEGY_PY2,
                          config_yaml=_strategy_config(rsi_hash))
    conn = _conn(tmp_path)
    _approve(conn, "rsi", rsi_hash)
    _approve(conn, "s", content_hash(s_dir))

    result = plugin_loader.approved_plugins(conn, plugins_dir, settings=_SETTINGS)

    assert sorted(m.name for m in result.inventory.metas) == ["rsi", "s"]
    assert sorted(m.name for m in result.phase1_metas) == ["rsi", "s"]
    assert result.rejected_strategies == ()
    key = ("s", content_hash(s_dir))
    assert key in result.resolved
    assert result.resolved[key].items[0].content_hash == rsi_hash
    assert result.inventory.root == plugins_dir.resolve()


def test_phase2_drops_strategy_with_broken_pin(tmp_path, caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    from agentic_fx.plugin.loader import content_hash
    rsi_dir = _write_plugin(plugins_dir, "rsi", config_yaml=_IND_SERIES_CONFIG)
    s_dir = _write_plugin(plugins_dir, "s", plugin_py=_STRATEGY_PY2,
                          config_yaml=_strategy_config("a" * 64))
    conn = _conn(tmp_path)
    _approve(conn, "rsi", content_hash(rsi_dir))
    _approve(conn, "s", content_hash(s_dir))

    with caplog.at_level(logging.WARNING):
        result = plugin_loader.approved_plugins(conn, plugins_dir,
                                                settings=_SETTINGS)

    assert [m.name for m in result.inventory.metas] == ["rsi"]
    assert sorted(m.name for m in result.phase1_metas) == ["rsi", "s"]
    assert len(result.rejected_strategies) == 1
    rej = result.rejected_strategies[0]
    assert (rej.name, rej.alias, rej.reason) == ("s", "rsi", "pin_mismatch")
    assert "pin_mismatch" in caplog.text
    assert result.resolved == {}


def test_phase2_drops_unpinned_strategy(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    from agentic_fx.plugin.loader import content_hash
    rsi_dir = _write_plugin(plugins_dir, "rsi", config_yaml=_IND_SERIES_CONFIG)
    s_dir = _write_plugin(plugins_dir, "s", plugin_py=_STRATEGY_PY2,
                          config_yaml=_strategy_config(None))
    conn = _conn(tmp_path)
    _approve(conn, "rsi", content_hash(rsi_dir))
    _approve(conn, "s", content_hash(s_dir))
    result = plugin_loader.approved_plugins(conn, plugins_dir, settings=_SETTINGS)
    assert [m.name for m in result.inventory.metas] == ["rsi"]
    assert result.rejected_strategies[0].reason == "unpinned"


def test_missing_plugins_dir_returns_empty_result(tmp_path):
    conn = _conn(tmp_path)
    result = plugin_loader.approved_plugins(conn, tmp_path / "nope",
                                            settings=_SETTINGS)
    assert result.inventory.metas == ()
    assert result.phase1_metas == ()
    assert result.resolved == {}
    assert result.rejected_strategies == ()
```

既存テスト (`test_plugin_loader.py` の `metas = plugin_loader.approved_plugins(conn, plugins_dir)`
が **19 箇所**) は同じコミットで新契約へ機械的に移行する:

> **codex plan r1 I1 是正**: v1.2 は「17 箇所」と書いていたが、`rg -n
> 'approved_plugins\(' src tests` の実測は **`tests/tools/test_plugin_loader.py`
> の実呼び出し 19 箇所** (`:91, 107, 129, 147, 167, 175, 456, 473, 503, 528,
> 551, 576, 603, 622, 646, 686, 714, 739, 768` — `:352` は docstring 言及で
> 呼び出しではない)。**着手時に必ず `rg` を打ち直して差分を確認すること**
> (行番号は v1.3 執筆時点)。他ファイルの実呼び出しは
> `tests/test_e2e_plugin_signal.py:205` / `tests/test_service_app.py:306`
> (docstring) と src 4 箇所 (`service.py:823` / `mission_worker.py:913` /
> `loops/improve_loop.py:845` / `loops/improve_context.py:84`)、定義は
> `src/agentic_fx/tools/plugin_loader.py:37`。

```python
metas = plugin_loader.approved_plugins(conn, plugins_dir, settings=_SETTINGS).inventory.metas
```

(`assert metas == []` は `assert metas == ()` へ、`len(metas)` / `metas[0]` はそのまま。)

- [ ] **Step 1-6b: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_plugin_loader.py -q`
Expected: FAIL — `TypeError: approved_plugins() got an unexpected keyword argument 'settings'`

- [ ] **Step 1-6c: Write minimal implementation**

`src/agentic_fx/tools/plugin_loader.py:37-70` を置換:

```python
def approved_plugins(conn: sqlite3.Connection, plugins_dir: Path, *,
                     settings) -> "InventoryBuildResult":
    """`plugins_dir` を discover し、承認済みかつハッシュ一致の plugin を
    **二相**で admit する ([indicator-consumption-wiring] 設計書 §2.3)。

    第 1 相 = 既存規律 (discover + 最新決定 approved + hash 一致)。
    第 2 相 = strategy を第 1 相の indicator 集合に対して
    `resolve_indicator_deps(pin_mode="require")` に通し、成功したものだけ
    最終 admit する (失敗は warning + reason を log)。

    戻り値は `InventoryBuildResult` — **`list[PluginMeta]` ではない**。
    最終 admit 済の一覧は `result.inventory.metas`、snapshot の材料は
    `result.phase1_metas` (pin 破れ strategy を含む)、prompt の
    「pin 破れ N 本」は `result.rejected_strategies` から作る。
    第 2 相で成功した strategy の `ResolvedIndicatorSet` は
    `result.resolved[(name, content_hash)]` に入る — **消費側は再解決せず
    これをそのまま渡す** (resolver 呼び出しは strategy ごとに 1 回、
    codex r5 I1 / r6 I1)。
    """
    from agentic_fx.plugin.resolve import (
        ApprovedInventory, IndicatorResolutionError, InventoryBuildResult,
        RejectedStrategy, resolve_indicator_deps,
    )
    if not plugins_dir.is_dir():
        _log.info("plugins dir %s does not exist — no plugins loaded", plugins_dir)
        empty = ApprovedInventory(root=plugins_dir, metas=())
        return InventoryBuildResult(inventory=empty, phase1_metas=(),
                                    resolved={}, rejected_strategies=())

    metas = discover(plugins_dir)
    approved_hashes_by_name = _approved_hashes_by_name(conn)

    phase1: list[PluginMeta] = []
    for meta in metas:
        if meta.content_hash in approved_hashes_by_name.get(meta.name, ()):
            phase1.append(meta)
        else:
            _log.warning(
                "plugin %s: 未承認 (承認済みハッシュと不一致、または承認要求が"
                "存在しない) — excluding from load", meta.name)

    root = plugins_dir.resolve()
    indicator_inventory = ApprovedInventory(
        root=root, metas=tuple(m for m in phase1 if m.kind == "indicator"))

    admitted: list[PluginMeta] = []
    resolved: dict[tuple[str, str], "ResolvedIndicatorSet"] = {}
    rejected: list[RejectedStrategy] = []
    for meta in phase1:
        if meta.kind != "strategy":
            admitted.append(meta)
            continue
        try:
            rset = resolve_indicator_deps(
                meta, indicator_inventory, settings=settings, pin_mode="require")
        except IndicatorResolutionError as exc:
            _log.warning(
                "plugin %s: indicator_unresolved:%s:%s — excluding from load",
                meta.name, exc.alias or "-", exc.reason)
            rejected.append(RejectedStrategy(
                name=meta.name, content_hash=meta.content_hash,
                alias=exc.alias, reason=exc.reason))
            continue
        admitted.append(meta)
        resolved[(meta.name, meta.content_hash)] = rset

    return InventoryBuildResult(
        inventory=ApprovedInventory(root=root, metas=tuple(admitted)),
        phase1_metas=tuple(phase1), resolved=resolved,
        rejected_strategies=tuple(rejected))
```

モジュール冒頭に型 import を追加 (`TYPE_CHECKING` 下):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentic_fx.plugin.resolve import InventoryBuildResult, ResolvedIndicatorSet
```

- [ ] **Step 1-6d: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_plugin_loader.py -q`
Expected: PASS

- [ ] **Step 1-6e: Commit**

```bash
git add src/agentic_fx/tools/plugin_loader.py tests/tools/test_plugin_loader.py
git commit -m "feat(plugin_loader): two-phase approved_plugins returning InventoryBuildResult"
```

### Step 1-7: 全 caller の移行 (`rg 'approved_plugins\(' src tests` の全結果)

- [ ] **Step 1-7a: Write the failing test**

`tests/test_service_app.py:305-334` の mock 戻り値を新契約へ:

```python
    # [indicator-consumption-wiring] T1: approved_plugins は
    # InventoryBuildResult を返す。mock も同型で返す (list 固定は不正確)。
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult

    def _fake_approved(conn, plugins_dir, *, settings):
        calls.append((conn, plugins_dir, settings))
        inv = ApprovedInventory(root=plugins_dir, metas=(meta,))
        return InventoryBuildResult(inventory=inv, phase1_metas=(meta,),
                                    resolved={}, rejected_strategies=())
```

`tests/test_e2e_plugin_signal.py:202-206` の直接 iteration を
`.inventory.metas` へ:

```python
        metas = plugin_loader.approved_plugins(
            app.conn_core, root / "plugins", settings=app.settings).inventory.metas
        assert [m.name for m in metas] == ["e2e_sig"]
```

さらに移行の全数性を pin するテストを `tests/tools/test_plugin_loader.py` に追加:

```python
def test_no_caller_uses_legacy_approved_plugins_signature():
    """[indicator-consumption-wiring] T1: `approved_plugins(conn, dir)` の
    2 引数呼び出しが **`src/` にも `tests/` にも**残っていないこと
    (settings= を渡し忘れると TypeError で落ちるが、動的呼び出しが混ざると
    発見が遅れる)。

    **codex plan r1 I1 是正**: v1.2 は `src/` しか走査しておらず、設計書
    §2.3 が要求する `rg 'approved_plugins\\(' src tests` の全移行を
    構造的に保証できていなかった (テスト側 19 + 1 箇所は「たまたま今回
    全部直した」以上の保証が無い)。**両方を走査する**。

    **AST で `ast.Call` ノードだけを見る** (opus r1 I2 是正)。正規表現
    `approved_plugins\(([^)]*)\)` は日本語 docstring 中の
    `` `plugin_loader.approved_plugins()` `` (引数なし) 6 箇所
    (`plugin/sandbox.py` 2 / `plugin/signal_producer.py` 1 /
    `tools/market_tools.py` 1 / `service.py` 2 — 着手時に
    `rg -n 'approved_plugins' src` で再取得すること) にもマッチし、
    移行後も永久に offender として残る。AST 走査の前例は
    `tests/test_no_duplicate_module_level_defs.py`。"""
    import ast
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    roots = [repo / "src" / "agentic_fx", repo / "tests"]
    offenders = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            # 除外は**定義そのもの** (`src/agentic_fx/tools/plugin_loader.py`)
            # の 1 ファイルだけ。**本テストが住む
            # `tests/tools/test_plugin_loader.py` は除外しない** — 19 箇所の
            # 移行漏れを捕まえるのが目的であり、本テスト自身は
            # `approved_plugins(...)` を 1 度も呼ばない (文字列比較だけ) ので
            # 自己除外は不要 (codex plan r1 の是正時に付けた自己除外を撤去)。
            if path == repo / "src" / "agentic_fx" / "tools" / "plugin_loader.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else None)
                if name != "approved_plugins":
                    continue
                if not any(kw.arg == "settings" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(repo)}:{node.lineno}")
    assert offenders == [], (
        "approved_plugins(...) を settings= 無しで呼んでいる箇所: "
        f"{offenders}")


def test_every_approved_plugins_caller_consumes_the_inventory_result():
    """[indicator-consumption-wiring] T1 (codex plan r1 I1、r2 束1 Important
    で強化): `settings=` は付いたが戻り値を `list[PluginMeta]` のまま使って
    いる呼び出しが残ると、`len()` / index / iteration が `InventoryBuildResult`
    に対して静かに誤動作する (dataclass なので `TypeError` になるとは限らない)。
    **呼び出し式の親が `.inventory` / `.phase1_metas` / `.resolved` /
    `.rejected_strategies` のいずれかの属性参照であること**を AST で検査する。

    **codex plan r2 束1 Important 是正**: v1.3 は「中間変数に代入する形
    (`result = approved_plugins(...)`) はこの検査の対象外にし、変数名の
    命名規約 (`*_result` / `*_inventory`) だけで読み手に示す」としていたが、
    命名規約は**構造的に強制されない** — 現行の全 4 caller
    (`service.py` の `inventory_result`、`mission_worker.py` の直接
    チェーン、`improve_loop.py` の `inventory_result`、
    `improve_context.py` の直接チェーン) のうち 2 本 (`service.py` /
    `improve_loop.py`) はまさにこの中間変数形なので、旧案では
    `inventory_result.inventory` 等の属性参照が**一度も検査されていなかった**
    (「全結果が `InventoryBuildResult` を受けている」を実際には pin できて
    いない)。**`x = approved_plugins(...)` の代入先変数 `x` を追跡し、
    `x.<attr>` という後続の属性参照も同じ許可属性集合に限定する** (2 パス:
    1 パス目で追跡対象の変数名を集め、2 パス目で直後属性参照と追跡変数属性
    参照の両方を検査する)。"""
    import ast
    from pathlib import Path
    _OK = {"inventory", "phase1_metas", "resolved", "rejected_strategies"}
    repo = Path(__file__).resolve().parents[2]
    offenders = []

    def _is_approved_plugins_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        name = (f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name) else None)
        return name == "approved_plugins"

    for root in (repo / "src" / "agentic_fx", repo / "tests"):
        for path in sorted(root.rglob("*.py")):
            if path == repo / "src" / "agentic_fx" / "tools" / "plugin_loader.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            # 1 パス目: `x = approved_plugins(...)` の x を集める (直接代入
            # のみ — tuple unpack / augassign は現行 4 caller に無い)。
            tracked_names: set[str] = set()
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign)
                        and _is_approved_plugins_call(node.value)
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    tracked_names.add(node.targets[0].id)

            # 2 パス目: 直後の属性参照 (`approved_plugins(...).X`) と、
            # 追跡変数経由の属性参照 (`x.X`) の両方を検査する。
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if _is_approved_plugins_call(node.value):
                    if node.attr not in _OK:
                        offenders.append(
                            f"{path.relative_to(repo)}:{node.lineno}:.{node.attr}")
                    continue
                if (isinstance(node.value, ast.Name)
                        and node.value.id in tracked_names
                        and node.attr not in _OK):
                    offenders.append(
                        f"{path.relative_to(repo)}:{node.lineno}:.{node.attr} "
                        f"(via {node.value.id})")
    assert offenders == [], (
        "approved_plugins(...) の戻り値から未知の属性を読んでいる箇所: "
        f"{offenders}")
```

- [ ] **Step 1-7b: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_plugin_loader.py::test_no_caller_uses_legacy_approved_plugins_signature tests/test_service_app.py tests/test_e2e_plugin_signal.py -q`
Expected: FAIL — offenders に **`ast.Call` 由来の 5 件**が並ぶ:
src 4 件 (`service.py:823` / `mission_worker.py:913` / `loops/improve_loop.py:845` /
`loops/improve_context.py:84`) + tests 1 件 (`tests/test_e2e_plugin_signal.py:205`)。
`tests/tools/test_plugin_loader.py` の 19 箇所は **Step 1-6c で既に移行済み**
なので残らない (Step 1-6 と 1-7 を 1 コミットにまとめる場合は 24 件になる)。
`tests/test_service_app.py:306` は `_fake_approved` の**定義**であって
`approved_plugins(...)` の呼び出しではないので offenders に入らない (代わりに
新契約の戻り型で返すことを本 Step で直す)。docstring 言及 6 箇所 +
`tests/plugin/test_sandbox.py:463` は `ast.Call` ではないので**含まれない**
(正規表現版なら 10 件以上になっていた — opus r1 I2)

- [ ] **Step 1-7c: Write minimal implementation**

4 箇所を最小形で移行する (意味は変えない — T3/T5a / T5b が本格配線する):

`src/agentic_fx/service.py:823`:

```python
        inventory_result = plugin_loader.approved_plugins(
            conn_core, plugins_dir, settings=settings)
        approved = list(inventory_result.inventory.metas)
```

`src/agentic_fx/mission_worker.py:913`:

```python
        approved = (list(plugin_loader.approved_plugins(
                        conn, plugins_dir, settings=settings).inventory.metas)
                   if plugins_dir is not None else [])
```

`src/agentic_fx/loops/improve_loop.py:845`:

```python
        inventory_result = approved_plugins(conn, plugins_dir,
                                            settings=self._settings)
        metas = list(inventory_result.phase1_metas)   # snapshot 材料は第 1 相
        copy_source_snapshot(metas, dest_root=source_snapshot_root,
                            plugin_lock=threading.Lock())
```

`src/agentic_fx/loops/improve_context.py:84`:

```python
    plugins = approved_plugins(conn, plugins_dir,
                               settings=settings).inventory.metas
```

- [ ] **Step 1-7d: Run test to verify it passes**

Run: `uv run pytest -q`
Expected: PASS (フルスイート)

- [ ] **Step 1-7e: Commit**

```bash
git add src/agentic_fx tests
git commit -m "refactor: migrate every approved_plugins caller to InventoryBuildResult"
```

**T1 完了条件**:
- [ ] **L1**: loader 表駆動テストが §2.2 の reason 語彙を**完全一致**で返し、
      語彙外の文字列が戻り集合に含まれない (`tests/plugin/test_loader.py`)。
      **例外 1 件**: `indicator_ref_duplicate_alias` は YAML 側の重複キーが
      `_construct_mapping_no_duplicates` で先に落ちるため、正常系の入力経路
      からは到達できない (実装は fail-closed の保険として残すが、表駆動の
      到達テストは書かない — 「14 語彙すべてを観測した」とは主張しない)
- [ ] **R1**: resolver 表駆動テストが 8 reason 語彙 + `pin_mode` 3 値 + `pin_object()` の
      alias 順/型 + 上限の境界 (deps 8/9・outputs 32/33・params 8192/8193) +
      拒否時 spawn 0 回を pin。**`handshake_too_large` は
      `MAX_HANDSHAKE_BYTES` を実測サイズ / 実測サイズ − 1 に monkeypatch して
      `>` 判定を挟む** (loader が通す入力の最大 handshake は ~136 KB で
      既定の 256 KiB に到達しないため — 既定選択 ⑧)
- [ ] **U4b (resolver 部分)**: `outputs` 宣言なしの indicator が `require`/`check` の
      両方で `outputs_undeclared` になる
- [ ] **P1 (lock_config 単体)**: pin 書き込み → `content_hash` 変化 → 再 discover 成功、
      同一 pin は no-op、古い pin は上書き
- [ ] `rg 'approved_plugins\(' src tests` の全結果が `settings=` 付きで
      `InventoryBuildResult` を受けている (移行の全数性テスト 2 本
      — `test_no_caller_uses_legacy_approved_plugins_signature` は
      **`src` と `tests` の両方**を走査、
      `test_every_approved_plugins_caller_consumes_the_inventory_result` は
      戻り値の属性を検査 — がともに緑。codex plan r1 I1)
- [ ] 段 0 変異 red: (a) `_check_json_safe` の float 分岐を `return None` に潰す →
      `test_params_json_safe_rejects[.inf]` が落ちる (b) `resolve_indicator_deps` の
      `pin_mode == "require"` 判定を `pin_mode == "ignore"` に書き換える →
      `test_resolve_unpinned_only_fails_under_require` が落ちる (c) 第 2 相の
      `rejected.append(...)` を削って `admitted.append(meta)` にする →
      `test_phase2_drops_strategy_with_broken_pin` が落ちる

---

## T2: sandbox + worker の同居実行 [colocated-indicator-execution]

**対応**: 設計書 §2.4 / §2.5 / §3 / §4 の `sandbox.py` / `worker.py` 行。
**完了条件の受入 ID**: V1 / V2 / V3 / S1 / C1 / N1。
(**`S1'` は独立した受入 ID ではない** — 設計書 §6 に `S1'` という行は無く、
「系列は末尾射影、スカラー NaN / 系列末尾 NaN はキー単位で落ちる」は S1 行の
後半そのものである。本プランでは以後 **S1 内の観測点**として扱い、
受入 ID の総数は設計書 §6 の 32 行と一致させる — codex plan r1 M2。)

**Files:**
- Create: `src/agentic_fx/core/plugin_contract.py`
- Modify: `src/agentic_fx/plugin/worker.py:135-231`
- Modify: `src/agentic_fx/plugin/sandbox.py:103-131` (deny)、`:153-188` (check_source)、
  `:272-276` (`_KIND_PAYLOAD_KEYS`)、`:292-475` (`PluginSession`)、`:648-661`
  (`_validate_indicator_result`)
- Modify: `src/agentic_fx/tools/market_tools.py:51-89` (`get_indicators`)
- Test: `tests/plugin/test_sandbox.py` (追記 + payload 縮小)、
  `tests/plugin/test_indicator_containment.py` (新規)、
  `tests/tools/test_plugin_loader.py` (S1 の `get_indicators` 側)

**Interfaces:**

- Consumes (T1 が Produce した型):
  `resolve.ResolvedIndicatorSet(inventory_root, items, all_pinned)` /
  `ResolvedIndicatorSet.empty(root)` / `.handshake_items() -> list[dict]` /
  `resolve.ResolvedIndicator(alias, plugin_name, plugin_py, content_hash, params,
  max_bars, outputs, pinned)`。
- Produces:

```python
# src/agentic_fx/core/plugin_contract.py
class IndicatorResultError(ValueError): ...
def validate_indicator_result(result, *, df_index, outputs: tuple[str, ...] | None):
    """戻り値 = {key: float | list[float|None]}。
    df_index は pandas.DatetimeIndex (同居実行) または None (standalone の
    親側境界再検証 — 系列長だけ検査する)。IndicatorResultError を送出。"""

# src/agentic_fx/plugin/sandbox.py
class PluginSession:
    def __init__(self, meta: PluginMeta, *, settings: "PluginSettings",
                 resolved: "ResolvedIndicatorSet | None" = None) -> None: ...
    @property
    def cpu_sec(self) -> float | None: ...   # close 完了後に確定
_KIND_PAYLOAD_KEYS = {"indicator": ("df", "params"), "signal": ("df", "params"),
                      "strategy": ("df", "params")}

# wire 契約 (sandbox.py / worker.py の docstring を両方同期すること)
# handshake: {"cpu_sec", "memory_mb", "nofile", "fsize_mb", "kind",
#             "outputs": [...]|null,            # kind == "indicator" のときのみ
#             "indicators": [{"alias","plugin_py","params","max_bars","outputs"}]}
# close request:  {"op": "close"}
# close response: {"ok": true, "cpu_sec": <float>, "pid": int}
# standalone indicator response result:
#   {key: float | {"series": [float|null, ...]}}   NaN は null、allow_nan=False
```

### Step 2-1: `validate_indicator_result` の抽出

- [ ] **Step 2-1a: Write the failing test**

`tests/core/test_plugin_contract.py` (新規):

```python
"""[indicator-consumption-wiring] T2: indicator 戻り値の共通 validator (V1)。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agentic_fx.core.plugin_contract import (
    IndicatorResultError, validate_indicator_result,
)


def _idx(n: int = 5) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")


def test_scalar_and_series_accepted():
    idx = _idx()
    out = validate_indicator_result(
        {"a": 1.5, "b": pd.Series([1.0] * 5, index=idx)},
        df_index=idx, outputs=("a", "b"))
    assert out["a"] == 1.5
    assert list(out["b"]) == [1.0] * 5


def test_nan_is_allowed_in_scalar_and_series():
    idx = _idx()
    out = validate_indicator_result(
        {"a": float("nan"), "b": pd.Series([float("nan")] * 5, index=idx)},
        df_index=idx, outputs=("a", "b"))
    assert np.isnan(out["a"])
    assert bool(pd.isna(out["b"]).all())


def test_list_and_ndarray_series_accepted_by_length():
    idx = _idx()
    out = validate_indicator_result(
        {"a": [1.0, 2.0, 3.0, 4.0, 5.0],
         "b": np.array([1.0, 2.0, 3.0, 4.0, 5.0])},
        df_index=idx, outputs=("a", "b"))
    assert len(out["a"]) == 5 and len(out["b"]) == 5


@pytest.mark.parametrize("value", [
    True,                               # bool は数値として拒否
    float("inf"),
    float("-inf"),
])
def test_scalar_rejects(value):
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": value}, df_index=idx, outputs=("a",))


def test_series_index_mismatch_rejected():
    idx = _idx()
    other = pd.date_range("2027-01-01", periods=5, freq="1h", tz="UTC")
    with pytest.raises(IndicatorResultError, match="index"):
        validate_indicator_result({"a": pd.Series([1.0] * 5, index=other)},
                                  df_index=idx, outputs=("a",))


def test_series_length_mismatch_rejected():
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="length"):
        validate_indicator_result({"a": [1.0, 2.0]}, df_index=idx, outputs=("a",))


def test_series_with_inf_or_bool_rejected():
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [1.0, 2.0, float("inf"), 4.0, 5.0]},
                                  df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [True] * 5}, df_index=idx, outputs=("a",))


def test_outputs_set_must_match_exactly():
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="outputs"):
        validate_indicator_result({"a": 1.0}, df_index=idx, outputs=("a", "b"))
    with pytest.raises(IndicatorResultError, match="outputs"):
        validate_indicator_result({"a": 1.0, "x": 1.0}, df_index=idx,
                                  outputs=("a",))


def test_outputs_none_allows_empty_dict_for_standalone():
    idx = _idx()
    assert validate_indicator_result({}, df_index=idx, outputs=None) == {}
    assert validate_indicator_result({"whatever": 1.0}, df_index=idx,
                                     outputs=None) == {"whatever": 1.0}


def test_non_dict_and_non_str_keys_rejected():
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result([1, 2], df_index=idx, outputs=None)
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({1: 2.0}, df_index=idx, outputs=None)


def test_numeric_string_is_rejected():
    """opus r1 M3: `float("1.5")` が通るため素の `float()` では数値文字列を
    受理してしまう。スカラー経路・系列経路の両方を pin する。"""
    idx = _idx()
    with pytest.raises(IndicatorResultError, match="got str"):
        validate_indicator_result({"a": "1.5"}, df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError, match="got str"):
        validate_indicator_result({"a": ["1.5"] * 5}, df_index=idx,
                                  outputs=("a",))


def test_nested_container_element_raises_indicator_result_error():
    """opus r1 M4: 要素が list / dict のとき `pd.isna(v)` は配列を返し、
    素の `ValueError`(truth value ambiguous) が漏れていた。必ず
    `IndicatorResultError` に写像されること。"""
    idx = _idx()
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [[1.0], [2.0], [3.0], [4.0], [5.0]]},
                                  df_index=idx, outputs=("a",))
    with pytest.raises(IndicatorResultError):
        validate_indicator_result({"a": [{"x": 1}] * 5}, df_index=idx,
                                  outputs=("a",))
```

- [ ] **Step 2-1b: Run test to verify it fails**

Run: `uv run pytest tests/core/test_plugin_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentic_fx.core.plugin_contract'`

- [ ] **Step 2-1c: Write minimal implementation**

`src/agentic_fx/core/plugin_contract.py` (新規):

```python
"""indicator の戻り値検証 — 唯一の実装 ([indicator-consumption-wiring] §2.5)。

**親プロセス (`plugin/sandbox.py`) と sandbox worker (`plugin/worker.py`)
の両方から import される** — ①置き場の裁定 (ユーザー裁定 2026-09-14):
`core/` は `core/contracts.py` の前例どおり上位層に依存しない性格の
モジュールを置く場所であり、worker エントリは既に
`agentic_fx.core.landlock` を import している。**このモジュールは
`agentic_fx.plugin.*` を import してはならない** — `plugin/worker.py`
(子プロセス、RLIMIT_AS 下) が import するため、`subprocess` や DB 接続を
巻き込む `plugin.sandbox` / `plugin.switch` 等への依存を作ると子プロセスに
不要な依存を持ち込む。`pandas`/`numpy` は関数内 lazy import にする
(import 自体は worker がどのみち行うが、親プロセス側の単体テストで
余計な import を強制しない)。

検証内容 (設計書 §2.5):
- dict[str, value] であること (キーは str)
- value は (a) float/int (bool・±Inf は拒否、NaN は許可) または
  (b) 系列 — `pd.Series` は `index.equals(df_index)` 必須、list/ndarray は
  `len == len(df_index)`、要素は数値 (bool・±Inf 拒否、NaN 許可)
- `outputs` 宣言済みは毎回**宣言キー集合と完全一致** (warmup は NaN で埋める)
- `outputs is None` は standalone (`get_indicators`) 経由でのみ到達し、
  任意のキー集合 (空 dict 含む) を許す (既存 `rsi_wilder` 互換)
"""
from __future__ import annotations

import math
from typing import Any


class IndicatorResultError(ValueError):
    """indicator の戻り値が契約に反する。呼び出し元 (worker / sandbox) が
    それぞれの層の例外 (`SandboxError` 等) へ写像する。"""


def _check_number(value: Any, where: str) -> float:
    # opus r1 M3 是正: `float(value)` を先に呼ぶと `float("1.5")` が通り、
    # **数値文字列を受理**してしまう (設計書 §2.5 は「要素は数値」)。
    # 型を先に見て、数値型以外は無条件で拒否する。`numbers.Real` を使うと
    # `Decimal`/`Fraction` が漏れるので、実際に扱う型 (int / float /
    # numpy スカラー) だけを allowlist する。
    import numpy as np

    if isinstance(value, bool) or isinstance(value, np.bool_):
        raise IndicatorResultError(f"{where} must be a number, got bool")
    if value is None:
        raise IndicatorResultError(f"{where} must be a number, got None")
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise IndicatorResultError(
            f"{where} must be a number, got {type(value).__name__}")
    try:
        fvalue = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IndicatorResultError(
            f"{where} must be a number, got {type(value).__name__}") from exc
    if math.isinf(fvalue):
        raise IndicatorResultError(f"{where} must not be +/-Inf")
    return fvalue


def validate_indicator_result(result: Any, *, df_index, outputs):
    import numpy as np
    import pandas as pd

    if not isinstance(result, dict):
        raise IndicatorResultError(
            f"indicator must return a dict, got {type(result).__name__}")
    for key in result:
        if not isinstance(key, str):
            raise IndicatorResultError(
                f"indicator result keys must be str, got {key!r}")
    if outputs is not None and set(result) != set(outputs):
        raise IndicatorResultError(
            f"indicator result keys {sorted(result)} do not match declared "
            f"outputs {sorted(outputs)}")

    expected_len = None if df_index is None else len(df_index)
    out: dict[str, Any] = {}
    for key, value in result.items():
        where = f"indicator result[{key!r}]"
        if isinstance(value, pd.Series):
            if df_index is not None and not value.index.equals(df_index):
                raise IndicatorResultError(f"{where} series index must equal df index")
            values = [None if pd.isna(v) else _check_number(v, where)
                      for v in value.tolist()]
            out[key] = pd.Series(values, index=value.index, dtype="float64")
            continue
        if isinstance(value, (list, tuple, np.ndarray)):
            seq = list(value)
            if expected_len is not None and len(seq) != expected_len:
                raise IndicatorResultError(
                    f"{where} series length {len(seq)} != df length {expected_len}")
            # opus r1 M4 是正: 元案は `pd.isna(v)` を要素に直接かけていたが、
            # `v` が list / dict / ndarray のとき `pd.isna` は**配列**を返し、
            # `if` が `ValueError: truth value of an array is ambiguous` を
            # 素で送出する (= `IndicatorResultError` にならず親の
            # `except IndicatorResultError` を素通りする)。NaN 判定を
            # スカラー数値型に限定し、それ以外は `_check_number` に回して
            # 必ず `IndicatorResultError` へ写像する。
            def _nan_or_number(v: Any) -> float | None:
                if v is None:
                    return None
                if isinstance(v, (float, np.floating)) and math.isnan(float(v)):
                    return None
                if v is pd.NaT or (v is pd.NA):
                    return None
                return _check_number(v, where)

            values = [_nan_or_number(v) for v in seq]
            out[key] = (pd.Series(values, index=df_index, dtype="float64")
                        if df_index is not None else values)
            continue
        out[key] = _check_number(value, where)
    return out
```

- [ ] **Step 2-1d: Run test to verify it passes**

Run: `uv run pytest tests/core/test_plugin_contract.py -q`
Expected: PASS

- [ ] **Step 2-1e: Commit**

```bash
git add src/agentic_fx/core/plugin_contract.py tests/core/test_plugin_contract.py
git commit -m "feat(plugin): shared indicator result validator supporting series (V1)"
```

### Step 2-2: worker の同居実行 + `PluginSession` の `resolved` / `__enter__` 検査

> **opus r1 M8 是正で旧 Step 2-2 と旧 Step 2-3 を 1 Step に統合した。**
> 旧 Step 2-2d は「Run test to verify it **passes**」というチェックボックスの
> 直下に「この Step ではまだ FAIL のまま」と書かれており自己矛盾していた
> (worker 側だけでは handshake の送り手がいないので緑にならない)。
> **worker (子) と `PluginSession` (親) は 1 つの red → green で扱う。**
> 旧 2-3a〜2-3e は本 Step の d〜h に対応する (旧番号を参照する記述は無い)。

- [ ] **Step 2-2a: Write the failing test (worker 側)**

`tests/plugin/test_sandbox.py` に追記 (実 subprocess — 既存 `_meta` は
`config.yaml` を `kind: <kind>` 1 行で書くので、`resolved` 用の indicator は
専用ヘルパで作る):

```python
# --- [indicator-consumption-wiring] T2: 同居実行 --------------------------

from agentic_fx.plugin.resolve import (
    ResolvedIndicator, ResolvedIndicatorSet, freeze_params,
)

RSI_SERIES_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    period = int(params.get("period", 14))
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False,
                                     min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False,
                                        min_periods=period).mean()
    rs = gain / loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(loss != 0.0, 100.0)
    out = out.where(~((gain == 0.0) & (loss == 0.0)), 50.0)
    return {"rsi": out}
"""

STRATEGY_READS_INDICATORS_PY = """
def evaluate(df, indicators, signals, params):
    r = indicators["rsi"]["rsi"]
    return {"action": "hold",
            "rationale": f"len={len(r)} last={float(r.iloc[-1]):.4f} "
                         f"signals={signals!r} keys={sorted(indicators)}"}
"""

MUTATOR_INDICATOR_PY = """
import pandas as pd


def compute(df, params):
    df["close"] = 0.0
    params["nested"]["x"].append(999)
    return {"v": pd.Series([float(len(params["nested"]["x"]))] * len(df),
                           index=df.index)}
"""


def _indicator_dir(base, name, plugin_py, *, outputs=("v",), params=None,
                   max_bars=200):
    import yaml
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": list(outputs),
         "params": params or {}, "max_bars": max_bars}, sort_keys=False))
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return d


def _resolved(root, *entries):
    """entries = [(alias, dir, outputs, params, max_bars)]

    opus r1 C5 是正: `ResolvedIndicator` は 8 フィールド全てが既定値なしの
    必須引数 (`@dataclass(frozen=True, slots=True)`、Step 1-3 の Produces)
    なので `pinned=` を落とすと `TypeError: __init__() missing 1 required
    positional argument: 'pinned'` で本ヘルパを使う 6 テストが全滅する。
    同 task の `tests/plugin/test_indicator_containment.py` 側は
    `pinned=True` を渡しており、こちらが転写ミスだった。
    """
    from agentic_fx.plugin.loader import content_hash as _ch
    items = tuple(sorted(
        (ResolvedIndicator(alias=a, plugin_name=d.name, plugin_py=d / "plugin.py",
                           content_hash=_ch(d), params=freeze_params(p),
                           max_bars=mb, outputs=tuple(o), pinned=True)
         for a, d, o, p, mb in entries), key=lambda i: i.alias))
    return ResolvedIndicatorSet(inventory_root=root.resolve(), items=items,
                                all_pinned=True)


def test_strategy_receives_indicator_series_from_same_worker(tmp_path,
                                                             plugin_settings):
    root = tmp_path / "plugins"
    rsi_dir = _indicator_dir(root, "rsi", RSI_SERIES_PY, outputs=("rsi",),
                             params={"period": 14})
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("rsi", rsi_dir, ("rsi",), {"period": 14}, 200))
    df = _df(40)
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        out = s.call({"df": df, "params": {}})
    assert out["action"] == "hold"
    assert f"len={len(df)}" in out["rationale"]
    assert "keys=['rsi']" in out["rationale"]
    # N1: signals は依然 None
    assert "signals=None" in out["rationale"]


def test_indicator_mutation_does_not_leak_across_calls_or_to_strategy(
        tmp_path, plugin_settings):
    """V2: df / params (nested) の書き換えが strategy にも次回 call にも届かない。"""
    root = tmp_path / "plugins"
    mut_dir = _indicator_dir(root, "mut", MUTATOR_INDICATOR_PY,
                             params={"nested": {"x": [1]}})
    meta = _meta(tmp_path, "strat2", "strategy", """
def evaluate(df, indicators, signals, params):
    return {"action": "hold",
            "rationale": f"close0={float(df['close'].iloc[0]):.2f} "
                         f"v={float(indicators['mut']['v'].iloc[-1]):.0f} "
                         f"p={params['keep']}"}
""", timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("mut", mut_dir, ("v",), {"nested": {"x": [1]}}, 200))
    df = _df(10)
    first_close = float(df["close"].iloc[0])
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        out1 = s.call({"df": df, "params": {"keep": [1]}})
        out2 = s.call({"df": df, "params": {"keep": [1]}})
    # indicator が書き換えた close は strategy に届かない
    assert f"close0={first_close:.2f}" in out1["rationale"]
    # 次回 call でも params は元の 1 要素から始まる (v=2 が 2 回)
    assert "v=2" in out1["rationale"] and "v=2" in out2["rationale"]
    assert "p=[1]" in out1["rationale"] and "p=[1]" in out2["rationale"]
    # 親側の df も無傷
    assert float(df["close"].iloc[0]) == first_close


def test_indicator_failure_becomes_sandbox_error(tmp_path, plugin_settings):
    root = tmp_path / "plugins"
    bad = _indicator_dir(root, "bad", "def compute(df, params):\n    raise ValueError('boom')\n")
    meta = _meta(tmp_path, "strat3", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("bad", bad, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="boom"):
            s.call({"df": _df(10), "params": {}})


def test_indicator_output_set_mismatch_becomes_sandbox_error(tmp_path,
                                                             plugin_settings):
    """V1: outputs 集合不一致は親に SandboxError として届く。"""
    root = tmp_path / "plugins"
    wrong = _indicator_dir(root, "wrong",
                           "def compute(df, params):\n    return {'other': 1.0}\n",
                           outputs=("v",))
    meta = _meta(tmp_path, "strat4", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("wrong", wrong, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="outputs"):
            s.call({"df": _df(10), "params": {}})


def test_indicator_tail_is_clamped_to_its_own_max_bars(tmp_path, plugin_settings):
    root = tmp_path / "plugins"
    counter = _indicator_dir(
        root, "cnt",
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    return {'v': pd.Series([float(len(df))] * len(df), index=df.index)}\n",
        max_bars=5)
    meta = _meta(tmp_path, "strat5", "strategy", """
def evaluate(df, indicators, signals, params):
    v = indicators["cnt"]["v"]
    return {"action": "hold",
            "rationale": f"n={len(v)} head={'nan' if v.isna().iloc[0] else 'val'} "
                         f"last={float(v.iloc[-1]):.0f}"}
""", timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("cnt", counter, ("v",), {}, 5))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        out = s.call({"df": _df(20), "params": {}})
    # 系列は strategy の df.index (20 本) に左 NaN 埋めで整列される
    assert "n=20" in out["rationale"]
    assert "head=nan" in out["rationale"]
    assert "last=5" in out["rationale"]      # indicator は 5 本だけ見た
```

既存の `test_sandbox.py` の strategy 呼び出し (`:708, 718, 725, 736, 748, 758, 846`) から
`"indicators": {}` / `"signals": []` を削り、`{"df": _df(), "params": {...}}` に縮小する
(同じコミット)。

- [ ] **Step 2-2b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_sandbox.py -k "indicator_series or mutation_does_not_leak or tail_is_clamped or output_set_mismatch or indicator_failure_becomes" -q`
Expected: FAIL — `TypeError: PluginSession.__init__() got an unexpected keyword argument 'resolved'`

- [ ] **Step 2-2c: Write minimal implementation**

`src/agentic_fx/plugin/worker.py` — モジュール docstring のワイヤ形式節を
新契約 (handshake の `outputs` / `indicators`、`op=close`、strategy request は
`{"df", "params"}`) へ書き換え、本体を変更:

```python
def _import_plugin_as(module_name: str, plugin_path: str):
    """`plugin.py` を **一意なモジュール名**で import する。strategy と
    同居する indicator を `"plugin"` 固定名で import すると sys.modules が
    衝突して 2 本目以降が 1 本目に化ける。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module from {plugin_path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_plugin(plugin_dir: str):
    return _import_plugin_as("plugin", os.path.join(plugin_dir, "plugin.py"))


def _deep_copy_json(obj):
    """params の deep copy (call ごとに独立。nested mutation を次 call へ
    持ち越さない — codex r3 C3)。params は JSON-safe が loader/resolver で
    保証済みなので json 往復で足りる。"""
    return json.loads(json.dumps(obj))


def _cpu_sec() -> float:
    import resource as _res
    usage = _res.getrusage(_res.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)
```

`main()` を書き換え (handshake 解釈 + indicator 読み込み + ループ):

```python
def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agentic_fx.plugin.worker <plugin_dir>")
    plugin_dir = sys.argv[1]

    protocol_out = _protect_protocol_stdout()

    handshake = _read_line()
    if handshake is None:
        return

    pid = os.getpid()
    try:
        _set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"],
                              handshake["nofile"], handshake["fsize_mb"])
        _poison_network_modules()
        plugin_module = _import_plugin(plugin_dir)
        kind = handshake["kind"]
        func_name = _KIND_FUNC[kind]
        fn = getattr(plugin_module, func_name)
        main_outputs = handshake.get("outputs") if kind == "indicator" else None
        # 同居 indicator (strategy のみ非空)。alias 順で来る。
        deps = []
        for spec in handshake.get("indicators") or []:
            module = _import_plugin_as(f"indicator_{spec['alias']}",
                                       spec["plugin_py"])
            deps.append({
                "alias": spec["alias"], "compute": getattr(module, "compute"),
                "params": spec["params"], "max_bars": int(spec["max_bars"]),
                "outputs": tuple(spec["outputs"])})
    except Exception as exc:  # noqa: BLE001 — 起動失敗を構造化エラーで報告
        _write_line(protocol_out, {"ok": False, "ready": False,
                    "error": f"{type(exc).__name__}: {exc}", "pid": pid})
        return

    _write_line(protocol_out, {"ok": True, "ready": True, "pid": pid})

    import pandas as pd
    import numpy as np
    from agentic_fx.core.plugin_contract import validate_indicator_result

    baseline_chained = pd.get_option("mode.chained_assignment")
    baseline_errstate = dict(np.geterr())

    while True:
        request = _read_line()
        if request is None:
            return
        # graceful close (設計書 §2.4): 親が {"op": "close"} を送る。
        if request.get("op") == "close":
            _write_line(protocol_out,
                        {"ok": True, "cpu_sec": _cpu_sec(), "pid": pid})
            return
        try:
            df = _wire_to_df(request["df"])
            params = _deep_copy_json(request["params"])
            if kind == "strategy":
                indicators = {}
                for dep in deps:
                    sub_df = df.tail(dep["max_bars"]).copy(deep=True)
                    raw = dep["compute"](sub_df, _deep_copy_json(dep["params"]))
                    validated = validate_indicator_result(
                        raw, df_index=sub_df.index, outputs=dep["outputs"])
                    indicators[dep["alias"]] = {
                        key: (value.reindex(df.index)
                              if isinstance(value, pd.Series) else value)
                        for key, value in validated.items()}
                # グローバル状態の不変 assert (設計書 §2.4、V2)
                if (pd.get_option("mode.chained_assignment") != baseline_chained
                        or dict(np.geterr()) != baseline_errstate):
                    raise RuntimeError(
                        "indicator mutated shared pandas/numpy global state")
                result = fn(df, indicators, None, params)
            else:
                result = fn(df, params)
                if kind == "indicator":
                    result = _indicator_result_to_wire(
                        validate_indicator_result(result, df_index=df.index,
                                                  outputs=main_outputs))
            _write_line(protocol_out, {"ok": True, "result": result, "pid": pid})
        except Exception as exc:  # noqa: BLE001
            _write_line(protocol_out, {"ok": False,
                        "error": f"{type(exc).__name__}: {exc}", "pid": pid})
```

standalone の wire 変換 (§2.5) を追加:

```python
def _indicator_result_to_wire(validated: dict) -> dict:
    """standalone (`get_indicators`) 応答の wire 表現。
    スカラーは float、系列は `{"series": [float|null, ...]}`。
    NaN は `null` にしてから送る (親は allow_nan=False で読める形)。"""
    import math

    import pandas as pd

    out: dict = {}
    for key, value in validated.items():
        if isinstance(value, pd.Series):
            out[key] = {"series": [None if pd.isna(v) else float(v)
                                   for v in value.tolist()]}
        elif isinstance(value, list):
            out[key] = {"series": [None if v is None else float(v) for v in value]}
        else:
            out[key] = None if math.isnan(float(value)) else float(value)
    return out
```

`_write_line` を `allow_nan=False` にする (NaN が素の `NaN` トークンで
出て親の `json.loads` が非標準 JSON を受けるのを防ぐ):

```python
def _write_line(stream: Any, obj: dict[str, Any]) -> None:
    stream.write(json.dumps(obj, allow_nan=False).encode("utf-8") + b"\n")
    stream.flush()
```

> ここまでで worker (子プロセス) 側が揃った。**まだ green にならない**
> (handshake の送り手 = `PluginSession` が未実装)。続けて親側を書く。

- [ ] **Step 2-2d: Write the failing test (`PluginSession` 側、V3)**

`tests/plugin/test_indicator_containment.py` (新規、V3):

```python
"""[indicator-consumption-wiring] T2 V3: 解決後の差し替え拒否と root containment。

実 subprocess を起動する経路だが、`__enter__` の検査は Popen より**前**に
走るため worker は 1 つも起動しない (spawn spy で確認する)。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_fx.config import load_settings
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.resolve import (
    ResolvedIndicator, ResolvedIndicatorSet, freeze_params,
)
from agentic_fx.plugin.sandbox import PluginSession, SandboxError

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
_STRATEGY_PY = ("def evaluate(df, indicators, signals, params):\n"
                "    return {'action': 'hold', 'rationale': 'x'}\n")
_INDICATOR_PY = ("import pandas as pd\n"
                 "def compute(df, params):\n"
                 "    return {'v': pd.Series([1.0] * len(df), index=df.index)}\n")


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


def _dir(base, name, plugin_py, config):
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return d


def _strategy_meta(base):
    d = _dir(base, "s", _STRATEGY_PY,
             {"kind": "strategy", "timeframe": "1h", "pairs": ["USDJPY"],
              "exit_mode": "levels", "max_bars": 200})
    return PluginMeta(name="s", kind="strategy", path=d, params={},
                      timeframe="1h", pairs=("USDJPY",), max_bars=200,
                      content_hash=content_hash(d))


def _rset(root, ind_dir):
    return ResolvedIndicatorSet(
        inventory_root=root.resolve(),
        items=(ResolvedIndicator(
            alias="v", plugin_name=ind_dir.name, plugin_py=ind_dir / "plugin.py",
            content_hash=content_hash(ind_dir), params=freeze_params({}),
            max_bars=200, outputs=("v",), pinned=True),),
        all_pinned=True)


def _no_spawn(monkeypatch):
    calls = []
    real = subprocess.Popen

    def _spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _spy)
    return calls


def test_enter_rejects_indicator_content_change(tmp_path, plugin_settings,
                                                monkeypatch):
    root = tmp_path / "plugins"
    ind = _dir(root, "ind", _INDICATOR_PY,
               {"kind": "indicator", "outputs": ["v"]})
    meta = _strategy_meta(tmp_path / "cand")
    resolved = _rset(root, ind)
    (ind / "plugin.py").write_text(_INDICATOR_PY + "\n# tampered\n")
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="hash"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


@pytest.mark.parametrize("root_name", ["live", "snapshot", "human"])
def test_enter_rejects_indicator_outside_inventory_root(tmp_path, plugin_settings,
                                                        monkeypatch, root_name):
    root = tmp_path / root_name / "plugins"
    root.mkdir(parents=True)
    outside = _dir(tmp_path / "elsewhere", "ind", _INDICATOR_PY,
                   {"kind": "indicator", "outputs": ["v"]})
    meta = _strategy_meta(tmp_path / "cand")
    resolved = _rset(root, outside)
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="inventory_root"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


def test_enter_rejects_symlinked_indicator_escaping_root(tmp_path,
                                                         plugin_settings,
                                                         monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    outside = _dir(tmp_path / "elsewhere", "ind", _INDICATOR_PY,
                   {"kind": "indicator", "outputs": ["v"]})
    (root / "ind").symlink_to(outside)
    meta = _strategy_meta(tmp_path / "cand")
    resolved = ResolvedIndicatorSet(
        inventory_root=root.resolve(),
        items=(ResolvedIndicator(
            alias="v", plugin_name="ind", plugin_py=root / "ind" / "plugin.py",
            content_hash=content_hash(outside), params=freeze_params({}),
            max_bars=200, outputs=("v",), pinned=True),),
        all_pinned=True)
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="inventory_root"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


def test_enter_rejects_indicator_failing_check_source(tmp_path, plugin_settings,
                                                      monkeypatch):
    root = tmp_path / "plugins"
    ind = _dir(root, "ind", "def compute(df, params):\n    return eval('{}')\n",
               {"kind": "indicator", "outputs": ["v"]})
    meta = _strategy_meta(tmp_path / "cand")
    resolved = _rset(root, ind)
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="eval"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []


def test_strategy_session_without_resolved_is_rejected(tmp_path, plugin_settings,
                                                       monkeypatch):
    meta = _strategy_meta(tmp_path / "cand")
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="resolved"):
        PluginSession(meta, settings=plugin_settings).__enter__()
    assert calls == []


def test_indicator_session_with_resolved_is_rejected(tmp_path, plugin_settings,
                                                      monkeypatch):
    """codex plan r2 束1 Minor: 契約は片方向だけでは不十分 — strategy は
    `resolved` 必須 (上のテスト) だが、indicator/signal は `resolved` を
    **受け取ってはいけない**。誤って渡すと依存情報が indicator/signal の
    handshake に混入し得るので `SandboxError` で拒否する。"""
    root = tmp_path / "plugins"
    ind_dep = _dir(root, "dep", _INDICATOR_PY, {"kind": "indicator", "outputs": ["v"]})
    ind = _dir(root, "ind", _INDICATOR_PY, {"kind": "indicator", "outputs": ["v"]})
    meta = PluginMeta(name="ind", kind="indicator", path=ind, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=content_hash(ind), outputs=("v",))
    resolved = _rset(root, ind_dep)   # indicator に誤って resolved を渡す
    calls = _no_spawn(monkeypatch)
    with pytest.raises(SandboxError, match="resolved"):
        PluginSession(meta, settings=plugin_settings,
                      resolved=resolved).__enter__()
    assert calls == []
```

- [ ] **Step 2-2e: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_indicator_containment.py tests/plugin/test_sandbox.py -q`
Expected: FAIL — `TypeError: PluginSession.__init__() got an unexpected keyword argument 'resolved'`
(Step 2-2a の worker 側テストも同じ理由で FAIL のまま = 統合後の red はここ 1 回)

- [ ] **Step 2-2f: Write minimal implementation (`PluginSession` 側)**

`src/agentic_fx/plugin/sandbox.py`:

```python
# :272-276 を置換 — strategy の request も {df, params} だけになった
# (indicators は worker 内で計算、signals は None 固定)。
_KIND_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "indicator": ("df", "params"),
    "signal": ("df", "params"),
    "strategy": ("df", "params"),
}
```

`PluginSession.__init__` を置換:

```python
    def __init__(self, meta: PluginMeta, *, settings: "PluginSettings",
                 resolved: "ResolvedIndicatorSet | None" = None) -> None:
        """`resolved` は strategy kind で**必須** (依存なしでも
        `ResolvedIndicatorSet.empty(root)`)。indicator/signal は `None`。
        セッションは内部で再解決しない — 解決は composition root の責務
        (設計書 §2.3)。"""
        self._meta = meta
        self._settings = settings
        self._resolved = resolved
        self._proc: subprocess.Popen | None = None
        self._dead = False
        self._cpu_sec: float | None = None
        self.pid: int | None = None
        self._owner_thread = threading.get_ident()

    @property
    def cpu_sec(self) -> float | None:
        """worker の累積 CPU 秒。**`close()` 完了後に確定する** —
        **正常終了・plugin error 後はいずれも float** (plugin コード自身の例外は
        セッションを `_dead` にしない既存契約なので graceful close が成立する)。
        **`None` は 2 経路のみ**: SIGKILL fallback (timeout 後の強制終了) と、
        `__enter__` 失敗による worker 未起動 (設計書 v1.4 §6 C1、ユーザー裁定 ④)。
        (codex plan r1 M3: 旧文言は plugin error 後を `None` と書いており、
        直下の `test_cpu_sec_is_float_after_plugin_error` と末尾の裁定 ④ に
        矛盾していた。)"""
        return self._cpu_sec
```

`__enter__` の hash 検査の直後に indicator 検査を挿入する
(`check_source(self._meta.path / "plugin.py")` の後、`subprocess.Popen` の前):

```python
            if self._meta.kind == "strategy" and self._resolved is None:
                raise SandboxError(
                    f"plugin {self._meta.name!r}: strategy session requires "
                    "a resolved indicator set (resolved=...)")
            # codex plan r2 束1 Minor: 契約は片方向だけでは不十分 —
            # strategy は `resolved` 必須 (上記) だが、indicator/signal は
            # `resolved` を**受け取ってはいけない** (`None` 固定)。ここを
            # 検査しないと、indicator/signal に誤って `ResolvedIndicatorSet`
            # を渡す呼び出しが静かに通り、依存情報 (indicator の実体パス等)
            # が handshake に混入し得る (kind 別の契約が片方向にしか pin
            # されていなかった)。
            if self._meta.kind != "strategy" and self._resolved is not None:
                raise SandboxError(
                    f"plugin {self._meta.name!r}: kind={self._meta.kind!r} "
                    "session must not receive a resolved indicator set "
                    "(resolved= is strategy-only)")
            indicator_specs: list[dict] = []
            if self._resolved is not None:
                inventory_root = Path(self._resolved.inventory_root).resolve()
                for item in self._resolved.items:
                    real = Path(item.plugin_py).resolve()
                    try:
                        real.relative_to(inventory_root)
                    except ValueError:
                        raise SandboxError(
                            f"indicator {item.plugin_name!r} resolves outside "
                            f"inventory_root ({real})") from None
                    current = content_hash(real.parent)
                    if current != item.content_hash:
                        raise SandboxError(
                            f"indicator {item.plugin_name!r}: content changed "
                            "since resolution (hash mismatch) — refusing to "
                            f"execute (expected {item.content_hash}, got {current})")
                    check_source(real)
                indicator_specs = self._resolved.handshake_items()
                # 検査済みの実体パスだけを handshake に載せる
                for spec, item in zip(indicator_specs, self._resolved.items):
                    spec["plugin_py"] = str(Path(item.plugin_py).resolve())
```

handshake 構築を拡張:

```python
            handshake = {
                "cpu_sec": self._settings.sandbox_session_cpu_sec,
                "memory_mb": self._settings.sandbox_memory_mb,
                "nofile": self._settings.sandbox_nofile,
                "fsize_mb": self._settings.sandbox_fsize_mb,
                "kind": self._meta.kind,
                "indicators": indicator_specs,
            }
            # [indicator-consumption-wiring] §2.4 (codex plan r1 M5):
            # `outputs` は **kind == "indicator" のときだけ**載せる契約
            # (設計書 §2.4 / codex 設計 r7 M1)。辞書リテラルに直接書くと
            # strategy / signal にもキー自体 (`null`) が届き、契約が
            # 「常に存在する nullable キー」に変質する。**分岐で足す**。
            if self._meta.kind == "indicator":
                handshake["outputs"] = (list(self._meta.outputs)
                                        if self._meta.outputs is not None
                                        else None)
```

`worker.py` 側は `handshake.get("outputs")` で読む (非 indicator ではキー自体が
無い = `None`)。`test_sandbox.py` に「strategy / signal の handshake に
`"outputs"` キーが**存在しない**」を 1 行 pin する:

```python
    assert "outputs" not in sent_handshake          # kind == "strategy"
```

import を追加:

```python
from pathlib import Path   # 既存
if TYPE_CHECKING:
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet
```

- [ ] **Step 2-2g: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_indicator_containment.py tests/plugin/test_sandbox.py -q`
Expected: PASS (統合後の green はここ 1 回。worker 側・親側の両テストが同時に緑になる)

- [ ] **Step 2-2h: Commit**

```bash
git add src/agentic_fx/plugin/worker.py src/agentic_fx/plugin/sandbox.py \
        tests/plugin/test_sandbox.py tests/plugin/test_indicator_containment.py
git commit -m "feat(sandbox): colocated indicator execution with containment checks (V1/V2/V3/N1)"
```

### Step 2-4: graceful close と `cpu_sec`

- [ ] **Step 2-4a: Write the failing test**

`tests/plugin/test_sandbox.py` に追記:

```python
def test_cpu_sec_is_float_after_graceful_close(tmp_path, plugin_settings):
    """C1: 正常終了で float。close 前は None。"""
    meta = _meta(tmp_path, "ind_cpu", "indicator", INDICATOR_OK_PY)
    session = PluginSession(meta, settings=plugin_settings)
    session.__enter__()
    assert session.cpu_sec is None
    session.call({"df": _df(), "params": {}})
    assert session.cpu_sec is None
    session.close()
    assert isinstance(session.cpu_sec, float)
    assert session.cpu_sec >= 0.0


def test_cpu_sec_is_none_when_enter_failed(tmp_path, plugin_settings):
    """C1: __enter__ 失敗 (worker 未起動) は None のまま。"""
    meta = _meta(tmp_path, "ind_bad_hash", "indicator", INDICATOR_OK_PY)
    (meta.path / "plugin.py").write_text(INDICATOR_OK_PY + "\n# tamper\n")
    session = PluginSession(meta, settings=plugin_settings)
    with pytest.raises(SandboxError):
        session.__enter__()
    assert session.cpu_sec is None


def test_cpu_sec_is_none_after_timeout_kill(tmp_path):
    """C1: SIGKILL fallback (timeout でセッションが死んだ後) は None。"""
    settings = load_settings(EXAMPLE).plugin.model_copy(
        update={"sandbox_timeout_sec": 1.0})
    meta = _meta(tmp_path, "ind_spin", "indicator",
                 "def compute(df, params):\n"
                 "    while True:\n        pass\n")
    session = PluginSession(meta, settings=settings)
    session.__enter__()
    with pytest.raises(SandboxError, match="timed out"):
        session.call({"df": _df(), "params": {}})
    session.close()
    assert session.cpu_sec is None


def test_cpu_sec_is_float_after_plugin_error(tmp_path, plugin_settings):
    """C1: plugin error 後は worker が生きているので graceful close が成立し
    `cpu_sec` は **float**。plugin コード自身の例外はセッションを `_dead` に
    しない既存契約 (`sandbox.py`、着手時に `rg -n '_dead' src/agentic_fx/plugin/sandbox.py`
    で再取得) のため。`None` になるのは SIGKILL fallback (timeout 後) と
    worker 未起動 (`__enter__` 失敗) の 2 経路だけ。

    opus r1 §5 / I10 是正: 旧名 `test_cpu_sec_is_none_after_plugin_error` は
    名前 (None) と内容 (float) が逆だった。**設計書 §6 の C1 行も v1.2 で
    この解釈に改訂済み** (「正常終了 / plugin error 後で float、SIGKILL
    fallback / worker 未起動で None」)。"""
    meta = _meta(tmp_path, "ind_err", "indicator",
                 "def compute(df, params):\n    raise ValueError('x')\n")
    session = PluginSession(meta, settings=plugin_settings)
    session.__enter__()
    with pytest.raises(SandboxError):
        session.call({"df": _df(), "params": {}})
    session.close()
    assert isinstance(session.cpu_sec, float)
```

> **実装者への申し送り**: 設計書 v1.1 §6 C1 は「plugin error 後 / SIGKILL
> fallback で `None`」と書いていたが、plugin コード自身の例外はセッションを
> `_dead` にしないという既存契約があるため graceful close が成立し float が
> 入る。**この食い違いはプラン着手前検証 (opus r1 I10) で指揮者へ申告し、
> 設計書 v1.2 の §6 C1 行を「正常終了 / plugin error 後で `cpu_sec` は float、
> SIGKILL fallback (timeout 後の強制終了) / worker 未起動 (`__enter__` 失敗)
> で `None`」へ改訂済み**。実装者はこれ以上の解釈を足さないこと
> (`_dead` の判定行は着手時に `rg -n '_dead' src/agentic_fx/plugin/sandbox.py`
> で再取得する — 本文の行番号参照は当てにしない)。

- [ ] **Step 2-4b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_sandbox.py -k cpu_sec -q`
Expected: FAIL — `AttributeError: 'PluginSession' object has no attribute 'cpu_sec'`
(Step 2-2f で property を足していれば `assert isinstance(session.cpu_sec, float)` が
`None` で落ちる)

- [ ] **Step 2-4c: Write minimal implementation**

`src/agentic_fx/plugin/sandbox.py` の `close()` を置換:

```python
    def close(self) -> None:
        """graceful close (設計書 §2.4): worker が生きていれば
        `{"op": "close"}` を送り `{"ok": true, "cpu_sec": ...}` を
        `sandbox_timeout_sec` 以内で待つ。応答が来れば `cpu_sec` が確定し、
        来なければ従来どおり SIGKILL (`cpu_sec` は None のまま)。"""
        self._check_owner_thread()
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            if not self._dead:
                try:
                    self._write_line({"op": "close"})
                    response = self._read_response(
                        self._settings.sandbox_timeout_sec, _STARTUP_MAX_BYTES)
                    if response.get("ok"):
                        value = response.get("cpu_sec")
                        if isinstance(value, (int, float)) and not isinstance(
                                value, bool):
                            self._cpu_sec = float(value)
                    try:
                        proc.wait(timeout=self._settings.sandbox_timeout_sec)
                    except subprocess.TimeoutExpired:
                        pass
                except (SandboxError, OSError):
                    # timeout/EOF/書き込み失敗 — fallback で kill する
                    # (`_read_response` は失敗時に自分で _kill する)。
                    self._cpu_sec = None
            if proc.poll() is None:
                self._kill()
        # 不変条件: この時点で proc は必ず終了済み ( `_kill()` が `wait()`
        # まで済ませている)。順序を変えないこと (デッドロックの実績あり)。
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._proc = None
```

- [ ] **Step 2-4d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_sandbox.py -q`
Expected: PASS

- [ ] **Step 2-4e: Commit**

```bash
git add src/agentic_fx/plugin/sandbox.py tests/plugin/test_sandbox.py
git commit -m "feat(sandbox): graceful close with cpu_sec property (C1)"
```

### Step 2-5: `check_source` の共有状態遮断 (deny 名 + 属性 Store)

- [ ] **Step 2-5a: Write the failing test**

`tests/plugin/test_sandbox.py` に追記 (V2 の 7 ケース):

```python
@pytest.mark.parametrize("snippet,needle", [
    ("pd.set_option('mode.chained_assignment', None)", "set_option"),
    ("pd.reset_option('mode.chained_assignment')", "reset_option"),
    ("pd.set_eng_float_format(accuracy=3)", "set_eng_float_format"),
    ("np.seterr(all='ignore')", "seterr"),
    ("np.seterrcall(None)", "seterrcall"),
    ("np.setbufsize(8192)", "setbufsize"),
    ("np.set_printoptions(threshold=5)", "set_printoptions"),
])
def test_check_source_denies_global_state_mutators(tmp_path, snippet, needle):
    path = tmp_path / f"p_{needle}.py"
    path.write_text("import numpy as np\nimport pandas as pd\n"
                    f"def compute(df, params):\n    {snippet}\n    return {{}}\n")
    with pytest.raises(SandboxError, match=needle):
        check_source(path)


@pytest.mark.parametrize("snippet", [
    "pd.options.mode.chained_assignment = None",
    "pd.options.display.max_rows: int = 5",
    "pd.options.display.max_rows += 1",
    "(a, np.x.y) = (1, 2)",
    "for pd.options.x.y in [1]:\n        pass",
    "del pd.options.x.y",
    "with open_ctx() as pd.options.x.y:\n        pass",
])
def test_check_source_rejects_attribute_store(tmp_path, snippet):
    path = tmp_path / "attr_store.py"
    path.write_text("import numpy as np\nimport pandas as pd\n"
                    f"def compute(df, params):\n    {snippet}\n    return {{}}\n")
    with pytest.raises(SandboxError, match="attribute assignment"):
        check_source(path)


def test_check_source_allows_local_attribute_free_assignment(tmp_path):
    """自ローカル変数への代入・添字代入は従来どおり許す (誤検出しない)。"""
    path = tmp_path / "ok.py"
    path.write_text(
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    out = {}\n"
        "    out['v'] = 1.0\n"
        "    x, y = 1, 2\n"
        "    for i in range(3):\n        x += i\n"
        "    return out\n")
    check_source(path)


def test_worker_asserts_global_state_unchanged(tmp_path, plugin_settings):
    """V2: check_source を通り抜けた動的変更があっても worker の
    不変 assert が SandboxError に倒す (多層防御)。"""
    root = tmp_path / "plugins"
    sneaky = _indicator_dir(
        root, "sneaky",
        "import numpy as np\n"
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    fn = np.__dict__['seterr']\n"
        "    fn(all='ignore')\n"
        "    return {'v': 1.0}\n")
    meta = _meta(tmp_path, "strat_g", "strategy",
                 STRATEGY_READS_INDICATORS_PY.replace('indicators["rsi"]["rsi"]',
                                                      'indicators["sneaky"]["v"]')
                 .replace("len(r)", "1").replace("r.iloc[-1]", "r"),
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("sneaky", sneaky, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="global state"):
            s.call({"df": _df(10), "params": {}})
```

- [ ] **Step 2-5b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_sandbox.py -k "global_state or attribute_store" -q`
Expected: FAIL — `DID NOT RAISE SandboxError` (deny 名も属性 Store 検査も無い)

- [ ] **Step 2-5c: Write minimal implementation**

`src/agentic_fx/plugin/sandbox.py:120-127` の `_DENY_NAMES` に 7 語を追加:

```python
_DENY_NAMES = frozenset({
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "getattr", "setattr", "delattr", "vars",
    "load", "loads", "loadtxt", "genfromtxt", "save", "savetxt", "savez",
    "memmap", "fromfile", "tofile", "pickle", "unpickle", "dump", "dumps",
    "ExcelWriter", "HDFStore", "importorskip",
    "capsys", "capfd", "capsysbinary", "capfdbinary",
    # [indicator-consumption-wiring] §2.4 (codex r3 I7): strategy と
    # indicator が同一 worker プロセスを共有するため、pandas/numpy の
    # **プロセス全体のグローバル状態**を書き換える API を遮断する
    # (`np.errstate` / `pd.option_context` は with 脱出で復元するので
    # 足さない)。`_is_denied_bare_name` は `ast.Attribute.attr` にも
    # 効くので `pd.set_option(...)` の形も拒否できる。
    "set_option", "reset_option", "set_eng_float_format",
    "seterr", "seterrcall", "setbufsize", "set_printoptions",
})
```

`check_source` の walk ループに属性 Store/Del の遮断を追加
(`elif isinstance(node, ast.Attribute):` の分岐を拡張):

```python
        elif isinstance(node, ast.Attribute):
            if _is_denied_bare_name(node.attr):
                raise SandboxError(f"{path}: attribute {node.attr!r} is not allowed")
            # [indicator-consumption-wiring] §2.4 (codex r4 I7): 外部
            # オブジェクトの属性への**代入・削除**は構文種別
            # (Assign / AugAssign / AnnAssign / タプル target / for target /
            # del / with ... as) によらず一律 reject する。`ctx` を見れば
            # 構文種別を列挙せずに全形を捕まえられる (現行 example に
            # 外部属性代入の正当用途は無い)。
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                raise SandboxError(
                    f"{path}: attribute assignment/deletion "
                    f"({node.attr!r}) is not allowed")
```

(worker 側の不変 assert は Step 2-2 で既に実装済み。)

- [ ] **Step 2-5d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_sandbox.py -q && uv run pytest tests/plugin tests/tools -q`
Expected: PASS。`docs/examples/plugins/*` と `plugins/` 配下の既存 plugin が
新 deny に触れていないことも同時に確認する:

```bash
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
for p in sorted(Path('docs/examples/plugins').glob('*/plugin.py')):
    check_source(p); print('ok', p)
"
```

- [ ] **Step 2-5e: Commit**

```bash
git add src/agentic_fx/plugin/sandbox.py tests/plugin/test_sandbox.py
git commit -m "feat(sandbox): deny shared pandas/numpy global state mutation (V2)"
```

### Step 2-6: standalone wire (系列) と `get_indicators` の末尾射影

- [ ] **Step 2-6a: Write the failing test**

`tests/tools/test_plugin_loader.py` に追記 (S1 と、その観測点「末尾射影」):

```python
def test_get_indicators_projects_series_to_last_value_and_drops_nan(tmp_path):
    """S1 (観測点 2): 系列は末尾値へ射影。スカラー NaN と系列末尾 NaN は
    キー単位で落とす。"""
    meta = PluginMeta(name="ind", kind="indicator", path=tmp_path,
                      params={}, timeframe=None, pairs=(), max_bars=50,
                      content_hash="h" * 64, outputs=("a", "b", "c", "d"))
    provider = MagicMock()
    provider.get_bars.return_value = _bars()

    def fake_sandbox_run(meta_arg, payload, *, settings):
        # opus r1 I4 是正: `sandbox_run` の既定は `sandbox.run_plugin` であり、
        # その戻り値は **`_validate_indicator_result` を通した後**の形
        # (`{key: float | list[float|None] | None}`) — wire 形式
        # `{"series": [...]}` ではない。fake も同じ形で返す。
        # **契約 (1 行で固定)**: `market_tools.get_indicators` は
        # `run_plugin` の戻りしか見ない = 系列は **list**。wire の
        # `{"series": [...]}` 封筒を解くのは `sandbox.py` の
        # `_validate_indicator_result` の責務であり、
        # `_project_indicator_output` は封筒を知らない (設計書 §2.5 と整合)。
        return {"a": 1.5,
                "b": [1.0, 2.0, 3.0],
                "c": None,               # スカラー NaN (wire の null を親が None 化)
                "d": [1.0, None]}        # 系列末尾 NaN

    tools = market_tools.build(provider, MagicMock(), _SETTINGS,
                               indicator_plugins=[meta],
                               sandbox_run=fake_sandbox_run)
    get_indicators = next(t.func for t in tools if t.name == "get_indicators")
    out = get_indicators("USDJPY", "1h")
    assert out["plugin:ind"] == {"a": 1.5, "b": 3.0}


def test_scalar_only_indicator_still_works_standalone(tmp_path, plugin_settings=None):
    """S1: outputs 宣言なし・スカラー返却の既存 plugin は無変更で通る。"""
    from agentic_fx.config import load_settings
    from agentic_fx.plugin.sandbox import run_plugin
    settings = _SETTINGS.plugin
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "plugin.py").write_text(
        "def compute(df, params):\n    return {'rsi_14': 55.0}\n")
    (d / "config.yaml").write_text("kind: indicator\nparams:\n  period: 14\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    from agentic_fx.plugin.loader import discover_one_with_reason
    meta, reason = discover_one_with_reason(d, "legacy")
    assert reason is None and meta.outputs is None
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
         "volume": [1.0]},
        index=pd.date_range("2026-01-01", periods=1, freq="1h", tz="UTC"))
    out = run_plugin(meta, {"df": df, "params": meta.params}, settings=settings)
    assert out == {"rsi_14": 55.0}


```

**この 1 本だけ `tests/plugin/test_sandbox.py` (T2 Files、既存 `_validate_indicator_result`
のテストが既にある場所) に追記する** — `_validate_indicator_result` は
`sandbox.py` のモジュール関数であり `plugin_loader` 側ではないため:

```python
def test_standalone_indicator_response_rejects_extra_or_missing_outputs_keys():
    """codex plan r2 束1 Important: worker を迂回した/破損した応答 (wire を
    直接偽装した呼び出し) が `meta.outputs` と食い違うキー集合を返したとき、
    **親側の `_validate_indicator_result` が拒否する**こと (worker 側の検査
    だけに頼らない — 親子の信頼境界を跨いだ値の再検証、S1)。"""
    from agentic_fx.plugin.sandbox import SandboxError, _validate_indicator_result

    outputs = ("a", "b")
    # 欠落: outputs=("a","b") のうち "b" が無い
    with pytest.raises(SandboxError, match="outputs"):
        _validate_indicator_result({"a": 1.0}, outputs=outputs)
    # 余分: 宣言に無い "c" が混ざる
    with pytest.raises(SandboxError, match="outputs"):
        _validate_indicator_result({"a": 1.0, "b": 2.0, "c": 3.0},
                                   outputs=outputs)
    # outputs=None (standalone 宣言なし、S1) は任意のキー集合を許す
    assert _validate_indicator_result({"whatever": 1.0}, outputs=None) == \
        {"whatever": 1.0}
```

- [ ] **Step 2-6b: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_plugin_loader.py -k "projects_series or scalar_only" -q`
Expected: FAIL — 現行 `get_indicators` は `plugin_result` をそのまま合成するので
`{"a": 1.5, "b": [1.0, 2.0, 3.0], "c": None, "d": [1.0, None]}` が返る
(期待は `{"a": 1.5, "b": 3.0}`)

Run: `uv run pytest tests/plugin/test_sandbox.py -k "extra_or_missing_outputs" -q`
Expected: FAIL — `TypeError: _validate_indicator_result() got an unexpected
keyword argument 'outputs'` (現行シグネチャは `result` 単独)

- [ ] **Step 2-6c: Write minimal implementation**

`src/agentic_fx/plugin/sandbox.py:648-661` の `_validate_indicator_result` を
**wire 境界の再検証**へ置換 (**codex plan r2 束1 Important 是正**:
形だけでなく `meta.outputs` 宣言との一致も**共通 validator
(`core.plugin_contract.validate_indicator_result`) で再検査**する — 旧案は
wire の型・有限性しか見ておらず、worker を迂回した/破損した応答が余分な
key・欠落 key を持ち込んでも親が黙って受理してしまい、File Structure が
謳う「worker と sandbox が唯一の実装を import する」とも矛盾していた):

```python
def _validate_indicator_result(result: Any, *,
                               outputs: "tuple[str, ...] | None") -> dict[str, Any]:
    """standalone (`run_plugin` / `PluginSession.call(kind="indicator")`) の
    応答を親側で**再検証**する ([indicator-consumption-wiring] §2.5)。

    worker は既に `plugin_contract.validate_indicator_result` を通した
    値を wire 形式 (`{key: float | {"series": [float|null, ...]}}`) で返して
    いるが、信頼境界を跨いだ値なのでここで形だけもう一度見る。
    NaN は wire 上で `null` になっている。戻り値は
    `{key: float | list[float | None]}`。

    **codex plan r2 束1 Important**: wire 形式を Python 値
    (`float | list[float|None]`) へ復元した**後**、
    `core.plugin_contract.validate_indicator_result(out, df_index=None,
    outputs=outputs)` を必ず通す。`df_index=None` なので系列長は検査しない
    (wire 変換の時点で `list` になっており、系列長は worker 側で既に
    `df` に対して検査済み — ここで二重にやり直せるのは `outputs` 宣言との
    キー集合一致のみ、これが親側で唯一欠けていた検査)。`outputs=None`
    (standalone 宣言なし indicator、S1) は任意のキー集合を許す (従来どおり)。
    """
    if not isinstance(result, dict):
        raise SandboxError(
            f"indicator must return a dict, got {type(result).__name__}")
    out: dict[str, Any] = {}
    for key, value in result.items():
        if not isinstance(key, str):
            raise SandboxError(f"indicator result keys must be str, got {key!r}")
        if isinstance(value, dict):
            series = value.get("series")
            if set(value) != {"series"} or not isinstance(series, list):
                raise SandboxError(
                    f"indicator result[{key!r}] series envelope is malformed")
            for item in series:
                if item is None:
                    continue
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise SandboxError(
                        f"indicator result[{key!r}] series must contain "
                        f"numbers or null, got {item!r}")
                if not math.isfinite(float(item)):
                    raise SandboxError(
                        f"indicator result[{key!r}] series must be finite")
            out[key] = [None if item is None else float(item) for item in series]
            continue
        if value is None:
            out[key] = None       # スカラー NaN (wire では null)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SandboxError(
                f"indicator result[{key!r}] must be a number, got {value!r}")
        fvalue = float(value)
        if not math.isfinite(fvalue):
            raise SandboxError(
                f"indicator result[{key!r}] must be finite, got {value!r}")
        out[key] = fvalue

    from agentic_fx.core.plugin_contract import (
        IndicatorResultError, validate_indicator_result as _validate_common)
    try:
        _validate_common(out, df_index=None, outputs=outputs)
    except IndicatorResultError as exc:
        raise SandboxError(str(exc)) from exc
    return out
```

`PluginSession.call()` (`sandbox.py:470-472`、既存コード) の indicator 分岐を
`outputs` を渡す形に変える:

```python
        if kind == "indicator":
            return _validate_indicator_result(result, outputs=self._meta.outputs)
```

`src/agentic_fx/tools/market_tools.py` の `get_indicators` の合成部分を置換:

```python
            result[f"plugin:{meta.name}"] = _project_indicator_output(plugin_result)
```

同ファイルにモジュール関数を追加:

```python
def _project_indicator_output(plugin_result: dict) -> dict:
    """[indicator-consumption-wiring] §2.5: 系列は**末尾値**へ射影し、

    **入力は `sandbox.run_plugin` の戻り (= `_validate_indicator_result` を
    通した後) に限る** — 系列は `list[float | None]`、スカラー NaN は
    `None` (opus r1 I4 で契約を固定)。wire 封筒 `{"series": [...]}` は
    ここへは来ない。

    値が未確定 (スカラー NaN = None、系列末尾 None) のキーは落とす
    (fail-open 維持 — LLM 向けの参考情報なので「値が無い」ことを
    `null` で見せるより落とす方が誤読が少ない)。空系列も落とす。"""
    out: dict = {}
    for key, value in plugin_result.items():
        if isinstance(value, list):
            if not value or value[-1] is None:
                continue
            out[key] = float(value[-1])
            continue
        if value is None:
            continue
        out[key] = value
    return out
```

- [ ] **Step 2-6d: Run test to verify it passes**

Run: `uv run pytest tests/tools/test_plugin_loader.py tests/plugin -q`
Expected: PASS

- [ ] **Step 2-6e: Commit**

```bash
git add src/agentic_fx/plugin/sandbox.py src/agentic_fx/tools/market_tools.py \
        tests/tools/test_plugin_loader.py tests/plugin/test_sandbox.py
git commit -m "feat(market_tools): project standalone indicator series to last value (S1)"
```

**T2 完了条件**:
- [ ] **V1**: validator の index 不一致 / 長さ不一致 / bool / ±Inf / outputs 集合不一致が
      `SandboxError` として**親に届く**。NaN は通る。全 kind の main plugin params が
      wire (dict) を通る。**standalone 応答の再検証 (親側)** も
      `outputs` 集合不一致 (余分 key / 欠落 key) を `SandboxError` で拒否する
      (`test_standalone_indicator_response_rejects_extra_or_missing_outputs_keys`。
      codex plan r2 束1 Important — worker 側の検査だけに頼らない)
- [ ] **V2**: mutation 回帰 (df / nested params / 次回 call) + `check_source` の 7 deny 名 +
      7 属性 Store ケース + worker の `pd.get_option("mode.chained_assignment")` /
      `np.geterr()` 不変 assert
- [ ] **V3**: 解決後の indicator ファイル書き換え → `__enter__` が `SandboxError`。
      `inventory_root` 外の実体パス (live / snapshot / human の 3 root) も `SandboxError`。
      いずれも worker spawn 0 回。**kind 契約は双方向**: strategy は `resolved` 必須
      (`test_strategy_session_without_resolved_is_rejected`)、indicator/signal は
      `resolved` を受け取ると `SandboxError`
      (`test_indicator_session_with_resolved_is_rejected`。codex plan r2 束1 Minor)
- [ ] **S1**: スカラー返却・`outputs` 宣言なしの既存 indicator が無変更で
      **standalone (`get_indicators`) 経由で**動く (設計書 v1.4 §6 S1 — 依存先には
      できない。依存に書くと resolver が `outputs_undeclared`、それは U4b の観測点)。
      観測点 2 = 系列は末尾射影、スカラー NaN / 系列末尾 NaN はキー単位で落ちる
- [ ] **C1 (sandbox 部分)**: 正常終了で `cpu_sec` float、`__enter__` 失敗 / SIGKILL fallback
      で `None`
- [ ] **N1**: worker が `evaluate` に渡す `signals` が `None` (strategy の rationale 経由で観測)
- [ ] 段 0 変異 red: (a) worker の `sub_df.copy(deep=True)` を `df.tail(...)` に戻す →
      `test_indicator_mutation_does_not_leak...` が落ちる (b) `__enter__` の
      `content_hash(real.parent)` 比較を削る → `test_enter_rejects_indicator_content_change`
      が落ちる (c) `check_source` の `ast.Store` 分岐を `ast.Del` のみにする →
      `test_check_source_rejects_attribute_store[pd.options...= None]` が落ちる
      (d) `close()` の `{"op": "close"}` 送信を削って即 `_kill()` にする →
      `test_cpu_sec_is_float_after_graceful_close` が落ちる
      (e) `_validate_indicator_result` 末尾の `_validate_common(out, df_index=None,
      outputs=outputs)` 呼び出しを削る →
      `test_standalone_indicator_response_rejects_extra_or_missing_outputs_keys`
      が落ちる (codex plan r2 束1 Important)
      (f) `__enter__` の `self._meta.kind != "strategy" and self._resolved is not None`
      判定を削る → `test_indicator_session_with_resolved_is_rejected` が落ちる
      (codex plan r2 束1 Minor)

---

## T6a: example + 受入 fixture + 設計書の契約文 [examples-and-fixture]

**対応**: 設計書 §2.10 / §6 の fixture 節 / §4 の `docs/examples/plugins/` と「設計書」行。
**完了条件の受入 ID**: A1 の fixture 部分 (fixture 3 indicator + synthetic bars + 独立参照
実装 oracle が自己整合していること)。A1 / A1-b / A1' 本体は T3 / T4 で使う。

**着手条件**: T2 完了 (worker が系列を受けられること)。**T3 着手前に main へマージすること。**

**Files:**
- Modify: `docs/examples/plugins/rsi_indicator/{plugin.py,config.yaml,test_plugin.py}`
- Create: `docs/examples/plugins/rsi_pullback/{plugin.py,config.yaml,test_plugin.py}`
- Create: `tests/fixtures/indicator_wiring.py`
- Create: `tests/fixtures/__init__.py` (無ければ)
  (**`tests/fixtures/wiring_envs.py` は T6a の Files ではない** — 所有者は T6b
  1 箇所だけ。T6a と T6b は worktree で並列着手しうるので、同一ファイルを
  2 task の Files に挙げると衝突を誘発する — codex plan r1 M1)
- Modify: `docs/superpowers/specs/2026-07-25-agentic-fx-design.md:391`
- Modify: `docs/superpowers/plans/2026-08-02-phase2-7-plugins.md:246`
- Modify: `src/agentic_fx/plugin/sandbox.py:3-10` (脅威モデル注記)
- Test: `tests/fixtures/test_indicator_wiring_fixture.py` (新規、fixture の自己整合)

**Interfaces:**

- Consumes: `plugin_contract.validate_indicator_result` の契約 (系列は `pd.Series`、
  warmup は NaN)、`loader` の `outputs` / `indicators` schema (T1)、
  `resolve.ResolvedIndicatorSet` (T2)、`ohlcv.import_history_bars(conn, rows, source=...)`、
  `market_hours.is_market_open(now) -> bool`、
  `timeframes.load_resampled_frame(conn, pair, timeframe, *, source, base_interval,
  until, max_bars)`。
- Produces:

```python
# tests/fixtures/indicator_wiring.py
NOW = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
BARS_START = datetime(2025, 11, 3, 0, 0, tzinfo=timezone.utc)   # 月曜
BARS_COUNT = 33_984                                             # k = 0..33983
HOLDOUT_MONTHS = 1
PAIR = "USDJPY"
SOURCE = "dukascopy"
BASE_INTERVAL = "5m"
PIP_SIZE = 0.01

def synthetic_rows() -> list[tuple]: ...          # import_history_bars 用 5m 行
def seed_history(conn) -> None: ...               # import_history_bars(source="dukascopy")
def write_indicator(base: Path, name: str) -> Path: ...   # name in {"sma","rsi","adx"}
def write_rsi_pullback(base: Path, *, pins: dict[str, str] | None) -> Path: ...
def deploy_approved(conn, plugins_root: Path, names: list[str], *,
                    now: datetime) -> dict[str, str]: ...   # name -> content_hash
def expected_eval_timestamps() -> list[datetime]: ...
    # opus r1 I9 #3: 旧案は `conn` を受けていたが本文で一切使っていなかった
    # (実際のバーの有無を見ず、暦格子 × `is_market_open` だけで決まる)。
    # 死に引数なので落とす。
def oracle_decisions(conn) -> dict[datetime, dict]: ...
    # {bucket_end: {"action","direction","entry_type","stop_loss","take_profit"}}
    # opus r1 M11: 同一 conn に対する結果をモジュール内でメモ化する
    # (in_sample の全 bucket ≒ 2,100 点で `load_resampled_frame` を回すため、
    # T6a の自己整合テストと T4a の A1 で計 3 回走ると無視できない)。
def assert_decisions_match(recorded: list[tuple[datetime, dict]], conn) -> None: ...
```

### Step 6-1: `rsi_indicator` の系列化

- [ ] **Step 6-1a: Write the failing test**

`docs/examples/plugins/rsi_indicator/test_plugin.py` を全面差し替え:

```python
"""rsi_indicator の最小テスト。

docs/examples/plugins/ は discover の対象外 (承認・本番配線とは独立の
サンプル)。承認フロー (プラン 7 Task 6) はこのファイルを実行して plugin
提出者が「動くテスト」を書いていることを検証する。

[indicator-consumption-wiring] 2026-09-14: 戻り値を**系列** (`pd.Series`、
df と同じ index、warmup 行は NaN) にした。`config.yaml` の `outputs: [rsi]`
が宣言キー — ハーネスは毎回この集合と完全一致することを検証する。
"""
from __future__ import annotations

import pandas as pd

from plugin import compute


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)},
        index=idx)


def test_compute_returns_declared_output_key_only():
    out = compute(_df([100 + i * 0.1 for i in range(30)]), {})
    assert set(out) == {"rsi"}


def test_compute_returns_series_aligned_to_df_index():
    df = _df([100 + i * 0.1 for i in range(30)])
    out = compute(df, {})
    assert isinstance(out["rsi"], pd.Series)
    assert out["rsi"].index.equals(df.index)


def test_warmup_rows_are_nan_and_later_rows_have_values():
    df = _df([100 + i * 0.1 for i in range(30)])
    rsi = compute(df, {})["rsi"]
    # period=14 (`min_periods=14`) → **先頭 14 行 (index 0..13) が NaN**、
    # index 14 (15 本目) から値が入る。probe 実測 (`tmp/plan-indicator-wiring/
    # probe_fixture.txt`) と設計書 §6 の warmup 記述と一致する。
    # codex plan r1 M4: `iloc[:13]` では index 13 を見ておらず、warmup が
    # 1 本早く明ける変異を検出できなかった。**境界 2 点を pin する**。
    assert bool(rsi.iloc[:14].isna().all())
    assert not pd.isna(rsi.iloc[14])
    assert not pd.isna(rsi.iloc[-1])
    assert 0.0 <= float(rsi.iloc[-1]) <= 100.0


def test_short_frame_returns_all_nan_series_not_empty_dict():
    df = _df([100.0] * 5)
    out = compute(df, {})
    assert set(out) == {"rsi"}
    assert bool(out["rsi"].isna().all())


def test_uptrend_yields_high_rsi():
    rsi = compute(_df([100 + i for i in range(20)]), {})["rsi"]
    assert float(rsi.iloc[-1]) > 70.0


def test_custom_period_param_keeps_the_same_output_key():
    out = compute(_df([100 + i * 0.1 for i in range(10)]), {"period": 5})
    assert set(out) == {"rsi"}
    assert not pd.isna(out["rsi"].iloc[-1])


def test_flat_series_yields_neutral_rsi():
    rsi = compute(_df([100.0] * 20), {})["rsi"]
    assert float(rsi.iloc[-1]) == 50.0
```

- [ ] **Step 6-1b: Run test to verify it fails**

Run: `cd docs/examples/plugins/rsi_indicator && uv run pytest test_plugin.py -q; cd -`
Expected: FAIL — 現行 `compute` はスカラー `{"rsi_14": float}` を返し、短い df では `{}`

- [ ] **Step 6-1c: Write minimal implementation**

`docs/examples/plugins/rsi_indicator/config.yaml`:

```yaml
kind: indicator
outputs: [rsi]
params:
  period: 14
```

`docs/examples/plugins/rsi_indicator/plugin.py` の `compute` を差し替え
(docstring の warmup 規約節は「空 dict を返す」→「全 NaN の系列を返す」へ
書き換えること):

```python
def compute(df: pd.DataFrame, params: dict) -> dict:
    """Wilder 平滑の RSI を **系列** (df と同じ index) で返す。

    注意1 (warmup): 系列契約では「行が足りないので何も返さない」はできない
    (ハーネスは宣言 `outputs` と完全一致するキー集合を毎回要求する)。
    足りない期間は **NaN** にする — 消費側 (strategy) は `pd.isna` を見て
    hold を返す規約。`max_bars` は「渡す DataFrame の末尾最大本数の上限
    宣言」であり、常に同じ本数が入っている保証ではない。

    注意2: 本 plugin は timeframe を宣言しないため (indicator は timeframe
    省略可)、呼び出し側が渡す任意の足で使われ得る。
    """
    period = int(params.get("period", 14))

    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    # avg_gain/avg_loss が未確定 (warmup) の行は NaN のまま残す。
    rsi = rsi.where(~(avg_gain.isna() | avg_loss.isna()))
    return {"rsi": rsi}
```

`plugin.py` 冒頭の契約説明に 1 行足す:

```python
# 戻り値は `{outputs で宣言したキー: float | pd.Series}`。系列は df と同じ
# index で返すこと (ハーネスが index 一致を検証する)。
```

`rsi_14` を期待している既存テストを同じコミットで更新する
(`rg -n "rsi_14" tests src` の全結果):

- `tests/tools/test_tool_impls.py` / `tests/datafeed/test_indicators.py` の
  `rsi_14` は**組み込み指標** (`datafeed/indicators.compute_indicators`) の
  キーなので**変更しない** (example plugin とは別物)。
- example plugin の `rsi_14` を参照しているテストがあれば `rsi` へ更新する
  (`rg -n "plugin:rsi_indicator" tests`)。

- [ ] **Step 6-1d: Run test to verify it passes**

Run:
```bash
cd docs/examples/plugins/rsi_indicator && uv run pytest test_plugin.py -q; cd -
uv run pytest -q
```
Expected: PASS

- [ ] **Step 6-1e: Commit**

```bash
git add docs/examples/plugins/rsi_indicator tests
git commit -m "feat(examples): rsi_indicator returns a declared series output"
```

### Step 6-2: 新規 example `rsi_pullback`

- [ ] **Step 6-2a: Write the failing test**

`docs/examples/plugins/rsi_pullback/test_plugin.py` (新規):

```python
"""rsi_pullback (依存あり strategy の例) の最小テスト。

`indicators` はハーネスが渡す `{alias: {output_key: float | pd.Series}}`。
このテストではそれを手で組み立てる (plugin は他 plugin を import できない)。
"""
from __future__ import annotations

import pandas as pd

from plugin import evaluate

PARAMS = {"oversold": 30, "overbought": 70, "stop_loss_pips": 30,
          "take_profit_pips": 60, "pip_size": 0.01}


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=idx)


def _ind(df, values):
    return {"rsi": {"rsi": pd.Series(values, index=df.index, dtype="float64")}}


def test_long_on_upward_cross_of_oversold():
    df = _df([150.0, 150.0])
    out = evaluate(df, _ind(df, [25.0, 35.0]), None, PARAMS)
    assert out["action"] == "open"
    assert out["direction"] == "long"
    assert out["entry_type"] == "market"
    assert out["stop_loss"] == 150.0 - 0.30
    assert out["take_profit"] == 150.0 + 0.60


def test_short_on_downward_cross_of_overbought():
    df = _df([150.0, 150.0])
    out = evaluate(df, _ind(df, [75.0, 65.0]), None, PARAMS)
    assert out["action"] == "open"
    assert out["direction"] == "short"
    assert out["stop_loss"] == 150.0 + 0.30
    assert out["take_profit"] == 150.0 - 0.60


def test_hold_when_no_cross():
    df = _df([150.0, 150.0])
    assert evaluate(df, _ind(df, [40.0, 45.0]), None, PARAMS)["action"] == "hold"


def test_hold_when_either_value_is_nan():
    df = _df([150.0, 150.0])
    assert evaluate(df, _ind(df, [float("nan"), 35.0]), None,
                    PARAMS)["action"] == "hold"
    assert evaluate(df, _ind(df, [25.0, float("nan")]), None,
                    PARAMS)["action"] == "hold"


def test_hold_when_fewer_than_two_bars():
    df = _df([150.0])
    assert evaluate(df, _ind(df, [35.0]), None, PARAMS)["action"] == "hold"


def test_boundary_equal_to_threshold_is_not_a_cross():
    """`prev <= oversold and curr > oversold` — curr == oversold は hold。"""
    df = _df([150.0, 150.0])
    assert evaluate(df, _ind(df, [25.0, 30.0]), None, PARAMS)["action"] == "hold"
```

- [ ] **Step 6-2b: Run test to verify it fails**

Run: `cd docs/examples/plugins/rsi_pullback && uv run pytest test_plugin.py -q; cd -`
Expected: FAIL — `ModuleNotFoundError: No module named 'plugin'` (plugin.py が無い)

- [ ] **Step 6-2c: Write minimal implementation**

`docs/examples/plugins/rsi_pullback/config.yaml`:

```yaml
kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: 200
indicators:
  rsi:
    plugin: rsi_indicator
    params:
      period: 14
params:
  oversold: 30
  overbought: 70
  stop_loss_pips: 30
  take_profit_pips: 60
  pip_size: 0.01
```

`docs/examples/plugins/rsi_pullback/plugin.py`:

```python
"""配備済 indicator に依存する strategy の例 (RSI プルバック)。

plugin 契約: strategy kind は `evaluate(df, indicators, signals, params) -> dict`
を実装する。`indicators` は **`config.yaml` の `indicators:` で宣言した依存**
だけが入る `{alias: {output_key: float | pd.Series}}` — ここでは
`indicators["rsi"]["rsi"]` が df と同じ index の `pd.Series` (warmup 行は NaN)。
`signals` は現行ハーネスでは常に `None`。

**依存の版 (`pin`) はハーネスが書く** — 作者は書かない。探索中は pin 無しの
ままでよいが、**提出 (submit / bless) の前に必ずロックすること**:
改善 worker は `lock_staging_deps(name="rsi_pullback")`、人間は
`afx plugin lock --from _human rsi_pullback`。ロック後に self-test と
backtest を再実行する (テストした artifact == 提出する artifact)。

**`max_bars` は「依存の warmup + 自分の lookback」を覆うように宣言すること。**
本例は RSI(14) の Wilder 平滑に十分な 200 本を宣言している。系列が全 NaN の
ままなら warmup 不足の兆候。

純関数のみで書くこと (I/O・乱数・実時計へのアクセス禁止)。使ってよいのは
pandas/numpy/math のみ。pandas/numpy の**グローバル設定**
(`pd.set_option` / `np.seterr` / `pd.options.* = ...`) は同一プロセスを共有
する他 plugin に影響するため禁止 (ハーネスが AST で拒否する)。
"""
from __future__ import annotations

import pandas as pd


def evaluate(df: pd.DataFrame, indicators: dict, signals: list,
             params: dict) -> dict:
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))
    stop_loss_pips = float(params.get("stop_loss_pips", 30))
    take_profit_pips = float(params.get("take_profit_pips", 60))
    pip_size = float(params.get("pip_size", 0.01))

    rsi = indicators["rsi"]["rsi"]
    if len(rsi) < 2:
        return {"action": "hold", "rationale": "insufficient bars"}
    prev, curr = rsi.iloc[-2], rsi.iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return {"action": "hold", "rationale": "indicator warmup"}

    close = float(df["close"].iloc[-1])
    stop = stop_loss_pips * pip_size
    target = take_profit_pips * pip_size
    if prev <= oversold and curr > oversold:
        return {"action": "open", "direction": "long", "entry_type": "market",
                "stop_loss": close - stop, "take_profit": close + target,
                "rationale": "RSI recovered above the oversold threshold"}
    if prev >= overbought and curr < overbought:
        return {"action": "open", "direction": "short", "entry_type": "market",
                "stop_loss": close + stop, "take_profit": close - target,
                "rationale": "RSI fell below the overbought threshold"}
    return {"action": "hold", "rationale": "no RSI threshold crossing"}
```

- [ ] **Step 6-2d: Run test to verify it passes**

Run:
```bash
cd docs/examples/plugins/rsi_pullback && uv run pytest test_plugin.py -q; cd -
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/rsi_pullback')
meta, reason = discover_one_with_reason(d, 'rsi_pullback')
assert reason is None, reason
assert [r.alias for r in meta.indicators] == ['rsi']
check_source(d / 'plugin.py')
print('ok')
"
```
Expected: PASS

- [ ] **Step 6-2e: Commit**

```bash
git add docs/examples/plugins/rsi_pullback
git commit -m "feat(examples): rsi_pullback strategy depending on a deployed indicator"
```

### Step 6-3: 受入 fixture (`tests/fixtures/indicator_wiring.py`)

- [ ] **Step 6-3a: Write the failing test**

`tests/fixtures/test_indicator_wiring_fixture.py` (新規、fixture 自身の自己整合):

```python
"""[indicator-consumption-wiring] T6: 受入 fixture の自己整合 (A1 の前提)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import market_hours
from agentic_fx.plugin.loader import discover_one_with_reason
from tests.backtest.factories import _conn
from tests.fixtures import indicator_wiring as fx


def test_bar_generation_matches_the_spec_formula():
    rows = fx.synthetic_rows()
    assert len(rows) == fx.BARS_COUNT == 33_984
    symbol, interval, ts, o, h, l, c, v, _spread = rows[0]
    assert (symbol, interval) == (fx.PAIR, fx.BASE_INTERVAL)
    assert ts == fx.BARS_START.isoformat()
    assert o == 150.0 and c == 150.0 and v == 100
    assert h == pytest.approx(150.05) and l == pytest.approx(149.95)
    last_ts = datetime.fromisoformat(rows[-1][2])
    assert last_ts == datetime(2026, 2, 28, 23, 55, tzinfo=timezone.utc)
    # 欠損なし: 隣接行はちょうど 5 分差
    for a, b in zip(rows, rows[1:]):
        assert (datetime.fromisoformat(b[2])
                - datetime.fromisoformat(a[2])) == timedelta(minutes=5)


def test_open_is_previous_close():
    rows = fx.synthetic_rows()
    for a, b in zip(rows[:200], rows[1:201]):
        assert b[3] == pytest.approx(a[6])


@pytest.mark.slow   # opus r1 M11: `seed_history` が 33,984 行を投入する
def test_expected_eval_timestamps_follow_market_hours(tmp_path):
    conn = _conn(tmp_path)
    fx.seed_history(conn)
    stamps = fx.expected_eval_timestamps()
    assert stamps, "no evaluation timestamps derived"
    assert all(market_hours.is_market_open(ts) for ts in stamps)
    assert all(ts.minute == 0 and ts.second == 0 for ts in stamps)
    # opus r1 M6 是正: 旧案は `grid = (fx.NOW - fx.BARS_START) // 1h` と
    # **holdout 期間まで含んだ暦格子**と比べていたので、市場時間を完全に
    # 無視する実装でも常に真になる空振り assert だった (`stamps` は
    # `in_sample_until` = 2026-02-01 までしか無い)。同じ期間の暦格子と
    # 比べ、かつ「週末の timestamp が 1 件も含まれない」を直接 pin する。
    from agentic_fx.backtest.holdout import in_sample_until
    end = in_sample_until(fx.NOW, fx.HOLDOUT_MONTHS,
                          base_interval=fx.BASE_INTERVAL)
    grid = (end - (fx.BARS_START + timedelta(hours=1))) // timedelta(hours=1)
    assert 0 < len(stamps) < grid
    # 土曜 00:00Z〜日曜 21:00Z は市場休止 (サーバ UTC+3 固定、週末境界
    # 21:00 UTC) — 1 件も含まれないことを直接見る
    assert not [ts for ts in stamps
                if ts.weekday() == 5], "土曜の評価時点が混ざっている"


def test_fixture_plugins_discover_and_pin(tmp_path):
    root = tmp_path / "plugins"
    for name in ("sma", "rsi", "adx"):
        d = fx.write_indicator(root, name)
        meta, reason = discover_one_with_reason(d, name)
        assert reason is None, reason
        assert meta.kind == "indicator" and meta.outputs == (name,)
    conn = _conn(tmp_path)
    hashes = fx.deploy_approved(conn, root, ["sma", "rsi", "adx"], now=fx.NOW)
    d = fx.write_rsi_pullback(root, pins={"rsi": hashes["rsi"]})
    meta, reason = discover_one_with_reason(d, "rsi_pullback")
    assert reason is None, reason
    assert meta.max_bars == 200
    assert meta.indicators[0].pin == hashes["rsi"]


@pytest.mark.slow   # opus r1 M11: in_sample の全 bucket (≒2,100 点) で
                    # `load_resampled_frame` を回すため。A1 (Step 4-3) にも
                    # 既に `slow` が付いている
def test_oracle_produces_hold_during_warmup_then_values(tmp_path):
    conn = _conn(tmp_path)
    fx.seed_history(conn)
    stamps = fx.expected_eval_timestamps()
    decisions = fx.oracle_decisions(conn)
    assert set(decisions) == set(stamps)
    # 最初の 14 評価は RSI warmup で必ず hold
    for ts in stamps[:14]:
        assert decisions[ts] == {"action": "hold", "direction": None,
                                 "entry_type": None, "stop_loss": None,
                                 "take_profit": None}
    opens = sum(1 for d in decisions.values() if d["action"] == "open")
    # A1 の gate は `total_trades >= EVALUABLE_MIN_TRADES` (= 30、
    # `backtest/metrics.py:20`) を満たさないと `insufficient_trades` で
    # 早期 return する。fixture の生成式・閾値は設計書 §6 の逐語なので
    # 調整できない — ここで下限を先に pin して、A1 の偽陰性を T6a の段階で
    # 検出する (open 数 >= 成立 trade 数 なので必要条件)。
    assert opens >= 30, f"fixture produces only {opens} entries — A1 would fail "\
                        "with insufficient_trades (escalate to 指揮者)"
```

- [ ] **Step 6-3b: Run test to verify it fails**

Run: `uv run pytest tests/fixtures/test_indicator_wiring_fixture.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fixtures.indicator_wiring'`

- [ ] **Step 6-3c: Write minimal implementation**

`tests/fixtures/__init__.py` (空ファイル、無ければ作る)。

`tests/fixtures/indicator_wiring.py` (新規):

```python
"""[indicator-consumption-wiring] 受入 fixture (設計書 §6 逐語)。

**実 DB・実 `plugins/` には一切触れない** — 呼び出し元が渡す `tmp_path`
配下の sqlite と plugins root にだけ書く。

bar 生成式 (決定論、設計書 §6):
    ts_k    = 2025-11-03T00:00:00Z (月曜) + k*5min   (k = 0 .. 33983)
    close_k = 150.0 + 2.0*sin(2*pi*k/288) + 0.5*sin(2*pi*k/2016)
    open_k  = close_{k-1}   (open_0 = 150.0)
    high_k  = max(open_k, close_k) + 0.05
    low_k   = min(open_k, close_k) - 0.05
    volume_k = 100
週末も生成する — runner が `is_market_open` で評価を止めるだけ (codex r7 I1)。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

NOW = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
BARS_START = datetime(2025, 11, 3, 0, 0, tzinfo=timezone.utc)   # 月曜
BARS_COUNT = 33_984
HOLDOUT_MONTHS = 1
PAIR = "USDJPY"
SOURCE = "dukascopy"
BASE_INTERVAL = "5m"
EVAL_TIMEFRAME = "1h"
PIP_SIZE = 0.01
RSI_PERIOD = 14
OVERSOLD = 30.0
OVERBOUGHT = 70.0
STOP_LOSS_PIPS = 30.0
TAKE_PROFIT_PIPS = 60.0
MAX_BARS = 200

_TEST_PY = "def test_placeholder():\n    pass\n"

_SMA_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    period = int(params.get("period", 20))
    return {"sma": df["close"].astype(float).rolling(window=period).mean()}
"""

_RSI_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    period = int(params.get("period", 14))
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    out = out.where(~(avg_gain.isna() | avg_loss.isna()))
    return {"rsi": out}
"""

_ADX_PY = """
from __future__ import annotations

import pandas as pd


def _wilder(series, period):
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute(df, params):
    period = int(params.get("period", 14))
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    atr = _wilder(tr, period)
    plus_di = 100.0 * _wilder(plus_dm, period) / atr
    minus_di = 100.0 * _wilder(minus_dm, period) / atr
    denom = (plus_di + minus_di).replace(0.0, float("nan"))
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    return {"adx": _wilder(dx.fillna(0.0), period)}
"""

_PULLBACK_PY = (Path(__file__).resolve().parents[2] / "docs" / "examples"
                / "plugins" / "rsi_pullback" / "plugin.py")

_INDICATOR_SOURCES = {"sma": _SMA_PY, "rsi": _RSI_PY, "adx": _ADX_PY}
_INDICATOR_PARAMS = {"sma": {"period": 20}, "rsi": {"period": RSI_PERIOD},
                     "adx": {"period": 14}}


def _closes() -> list[float]:
    return [150.0 + 2.0 * math.sin(2 * math.pi * k / 288)
            + 0.5 * math.sin(2 * math.pi * k / 2016) for k in range(BARS_COUNT)]


def synthetic_rows() -> list[tuple]:
    closes = _closes()
    rows = []
    prev_close = 150.0
    for k, close in enumerate(closes):
        ts = BARS_START + timedelta(minutes=5 * k)
        open_ = 150.0 if k == 0 else prev_close
        rows.append((PAIR, BASE_INTERVAL, ts.isoformat(), open_,
                     max(open_, close) + 0.05, min(open_, close) - 0.05,
                     close, 100, 0.01))
        prev_close = close
    return rows


def seed_history(conn) -> None:
    from agentic_fx.store import ohlcv
    ohlcv.import_history_bars(conn, synthetic_rows(), source=SOURCE)


def write_indicator(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(_INDICATOR_SOURCES[name])
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": [name],
         "params": _INDICATOR_PARAMS[name], "max_bars": MAX_BARS},
        sort_keys=False))
    (d / "test_plugin.py").write_text(_TEST_PY)
    return d


def write_rsi_pullback(base: Path, *, pins: dict[str, str] | None = None) -> Path:
    d = base / "rsi_pullback"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(_PULLBACK_PY.read_text(encoding="utf-8"))
    ref = {"plugin": "rsi", "params": {"period": RSI_PERIOD}}
    if pins and "rsi" in pins:
        ref["pin"] = pins["rsi"]
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "strategy", "timeframe": EVAL_TIMEFRAME, "pairs": [PAIR],
         "exit_mode": "levels", "max_bars": MAX_BARS,
         "indicators": {"rsi": ref},
         "params": {"oversold": OVERSOLD, "overbought": OVERBOUGHT,
                    "stop_loss_pips": STOP_LOSS_PIPS,
                    "take_profit_pips": TAKE_PROFIT_PIPS,
                    "pip_size": PIP_SIZE}},
        sort_keys=False))
    (d / "test_plugin.py").write_text(_TEST_PY)
    return d


def deploy_approved(conn, plugins_root: Path, names, *, now) -> dict[str, str]:
    """`plugins_root/<name>` を `.versions/<name>/<artifact_hash>` への
    正規形 symlink にして approval 行を approved で作る (配備の最小形)。"""
    from agentic_fx.plugin.loader import artifact_hash_bytes, content_hash
    from agentic_fx.store import approvals
    hashes: dict[str, str] = {}
    for name in names:
        src = plugins_root / name
        chash = content_hash(src)
        ahash = artifact_hash_bytes((src / "plugin.py").read_bytes(),
                                    (src / "config.yaml").read_bytes(),
                                    (src / "test_plugin.py").read_bytes())
        version_dir = plugins_root / ".versions" / name / ahash
        version_dir.mkdir(parents=True, exist_ok=True)
        for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
            (version_dir / rel).write_bytes((src / rel).read_bytes())
        for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
            (src / rel).unlink()
        src.rmdir()
        src.symlink_to(Path(".versions") / name / ahash)
        kind = yaml.safe_load(
            (version_dir / "config.yaml").read_text(encoding="utf-8"))["kind"]
        aid = approvals.create(conn, "plugin",
                               {"name": name, "kind": kind,
                                "content_hash": chash}, now)
        approvals.apply_decision(conn, aid, status="approved",
                                 decided_by="fixture", now=now)
        hashes[name] = chash
    return hashes


_ORACLE_CACHE: dict[int, dict] = {}


def expected_eval_timestamps() -> list[datetime]:
    """`backtest/runner.run_replay` が `intent_source(closed_bar)` を呼ぶ
    時点と同じ集合を、ハーネスのコード (`market_hours.is_market_open`) から
    導出する。plugin コードは一切 import しない。

    **runner の実挙動との対応 (opus r1 I9 で実測確認済み)**: 着手時に
    `rg -n 'bucket_start|is_market_open|first_decision_at' src/agentic_fx/backtest/runner.py`
    で再取得して照合すること。v1 執筆時点の実測では
    `bucket_start = now - tf` → `closed_bar = _aggregate_bucket(..., bucket_start, tf)`
    → `if closed_bar is not None and market_hours.is_market_open(now)` で
    `intent_source(closed_bar)` を呼ぶ。この `now` は **bucket_end** なので
    本関数の `is_market_open(ts)` (ts = bucket_end) と**同じ述語**。開始点も
    `first_decision_at = ceil_to_bucket(start, eval_timeframe) + tf`
    = `BARS_START + 1h` で一致する。**週末境界のズレは発生しない**。

    `conn` は受けない (opus r1 I9 #3: 旧案は受けていたが本文で使っていな
    かった = 死に引数)。"""
    from agentic_fx.backtest.holdout import in_sample_until
    from agentic_fx.core import market_hours
    end = in_sample_until(NOW, HOLDOUT_MONTHS, base_interval=BASE_INTERVAL)
    step = timedelta(hours=1)
    first = BARS_START + step          # 最初の完成 1h バケットの終端
    stamps = []
    ts = first
    while ts < end:
        if market_hours.is_market_open(ts):
            stamps.append(ts)
        ts += step
    return stamps


def oracle_decisions(conn) -> dict[datetime, dict]:
    """**plugin コードを import しない独立参照実装** (設計書 §6)。
    各評価時点で、ハーネスが渡すのと同じ df (`load_resampled_frame(...,
    until=bucket_end, max_bars=200)`) に対して Wilder RSI を計算し、
    `rsi_pullback` の判定式を pandas で転写する。

    `ewm(alpha=1/period, adjust=False)` は窓の先頭から経路依存なので、
    **必ず同じ 200 本の tail に対して**計算すること (全期間の系列から
    切り出すと期待値が実行結果と乖離する)。"""
    import pandas as pd

    from agentic_fx.backtest.timeframes import load_resampled_frame

    # opus r1 M11: 同一 conn に対する再計算を避ける (T6a の自己整合テストと
    # T4a の A1 で計 3 回走る)。`functools.lru_cache` は conn を hashable と
    # して保持し続けるので使わず、`id(conn)` キーの module-level dict で
    # 明示的にメモ化する (テストは関数スコープの conn なので衝突しない)。
    cached = _ORACLE_CACHE.get(id(conn))
    if cached is not None:
        return cached

    out: dict[datetime, dict] = {}
    for bucket_end in expected_eval_timestamps():
        df = load_resampled_frame(conn, PAIR, EVAL_TIMEFRAME, source=SOURCE,
                                  base_interval=BASE_INTERVAL,
                                  until=bucket_end, max_bars=MAX_BARS)
        # opus r1 I9 #1 是正: 旧案はここに `if df.empty: continue` があり、
        # `expected_eval_timestamps` (df を見ない) と集合が食い違う可能性が
        # あった (`test_oracle_produces_hold_during_warmup_then_values` は
        # `set(decisions) == set(stamps)` を要求する)。fixture は欠損なしの
        # 33,984 本なのでここへは到達しない — **到達したら fixture が
        # 壊れているので落ちる方が正しい**。continue せず素通しする
        # (`close.diff()` が空 Series になり `len(rsi) < 2` で hold に落ちる
        # のではなく、`df["close"]` の KeyError / IndexError で落ちること
        # 自体が検出になる)。
        assert not df.empty, (
            f"fixture has a gap at {bucket_end.isoformat()} — "
            "expected_eval_timestamps と oracle の集合が食い違う")
        close = df["close"].astype(float)
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1.0 / RSI_PERIOD, adjust=False,
                            min_periods=RSI_PERIOD).mean()
        avg_loss = loss.ewm(alpha=1.0 / RSI_PERIOD, adjust=False,
                            min_periods=RSI_PERIOD).mean()
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi = rsi.where(avg_loss != 0.0, 100.0)
        rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
        rsi = rsi.where(~(avg_gain.isna() | avg_loss.isna()))
        hold = {"action": "hold", "direction": None, "entry_type": None,
                "stop_loss": None, "take_profit": None}
        if len(rsi) < 2 or pd.isna(rsi.iloc[-2]) or pd.isna(rsi.iloc[-1]):
            out[bucket_end] = hold
            continue
        prev, curr = float(rsi.iloc[-2]), float(rsi.iloc[-1])
        last_close = float(close.iloc[-1])
        stop = STOP_LOSS_PIPS * PIP_SIZE
        target = TAKE_PROFIT_PIPS * PIP_SIZE
        if prev <= OVERSOLD and curr > OVERSOLD:
            out[bucket_end] = {"action": "open", "direction": "long",
                               "entry_type": "market",
                               "stop_loss": last_close - stop,
                               "take_profit": last_close + target}
        elif prev >= OVERBOUGHT and curr < OVERBOUGHT:
            out[bucket_end] = {"action": "open", "direction": "short",
                               "entry_type": "market",
                               "stop_loss": last_close + stop,
                               "take_profit": last_close - target}
        else:
            out[bucket_end] = hold
    _ORACLE_CACHE[id(conn)] = out
    return out


def assert_decisions_match(recorded, conn) -> None:
    """`StrategyAdapter(decision_sink=...)` が記録した
    `[(bucket_end, decision_dict), ...]` を oracle と逐次比較する。
    比較対象 (codex r8 I3): `(action, direction)`、open のときは
    `entry_type == "market"` と `stop_loss` / `take_profit` を ±1e-9。
    `rationale` は比較しない。

    **adapter 側の逐語契約 (opus r1 I9 #2)**: `StrategyAdapter.__call__` は
    `load_resampled_frame(...)` の結果が `df.empty` のとき **`decision_sink` を
    呼ばずに `None` を返す** (Step 3-1c の実装を参照)。一方この関数は
    `[ts for ts, _ in recorded] == sorted(expected)` の**完全一致**を要求する。
    欠損のない本 fixture では `df.empty` に到達しないので両者は一致するが、
    **fixture を欠損ありに変えた瞬間に片側だけが落ちる非対称**なので、
    fixture の bar 生成式を変更するときはこの 2 つを同時に見直すこと。"""
    expected = oracle_decisions(conn)
    assert [ts for ts, _ in recorded] == sorted(expected), \
        "evaluated timestamps do not match the market-hours grid"
    for ts, decision in recorded:
        want = expected[ts]
        assert (decision["action"], decision["direction"]) == \
            (want["action"], want["direction"]), f"mismatch at {ts.isoformat()}"
        if want["action"] == "open":
            assert decision["entry_type"] == "market"
            assert abs(decision["stop_loss"] - want["stop_loss"]) < 1e-9
            assert abs(decision["take_profit"] - want["take_profit"]) < 1e-9
```

- [ ] **Step 6-3d: Run test to verify it passes**

Run: `uv run pytest tests/fixtures/test_indicator_wiring_fixture.py -q`
Expected: PASS

> **この懸念は着手前に実測で解消済み** (opus r1 観点 7 #10 / I9 補足、
> `tmp/plan-indicator-wiring/probe_fixture.py` + `probe_fixture.txt`)。
> 設計書 §6 の生成式で in_sample `[2025-11-03, 2026-02-01)` の 1h RSI(14) は
> **min 27.2159 / max 78.4162、long open 52 回 + short open 50 回 = 102 回**、
> warmup は**先頭 14 本が NaN、index 14 (15 本目) から値**。`opens >= 30` は
> 余裕で満たす。
>
> それでも `opens >= 30` が落ちたら、resample の `label`/`closed` や
> `load_resampled_frame` の tail 幅が probe と違う可能性が高い。**設計書 §6 の
> 生成式は逐語で固定なので式を変えず**、`OVERSOLD` / `OVERBOUGHT` も動かさず、
> **指揮者へ申告すること**。

- [ ] **Step 6-3e: Commit**

```bash
git add tests/fixtures
git commit -m "test(fixtures): deterministic indicator-wiring fixture with independent oracle"
```

### Step 6-4: 設計書・プラン文書・脅威モデルの更新

- [ ] **Step 6-4a: Write the failing test**

`tests/fixtures/test_indicator_wiring_fixture.py` に追記 (文書の逐語 pin —
契約文が実装と乖離したまま残る事故を防ぐ):

```python
def test_design_docs_describe_the_new_plugin_contract():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    design = (root / "docs" / "superpowers" / "specs"
              / "2026-07-25-agentic-fx-design.md").read_text(encoding="utf-8")
    assert "compute(df, params) -> dict" in design
    assert "系列" in design and "indicators" in design
    phase27 = (root / "docs" / "superpowers" / "plans"
               / "2026-08-02-phase2-7-plugins.md").read_text(encoding="utf-8")
    assert "indicators/signals 供給は将来拡張 — None 固定" not in phase27
    sandbox = (root / "src" / "agentic_fx" / "plugin"
               / "sandbox.py").read_text(encoding="utf-8")
    assert "同居" in sandbox and "グローバル状態" in sandbox
```

- [ ] **Step 6-4b: Run test to verify it fails**

Run: `uv run pytest tests/fixtures/test_indicator_wiring_fixture.py -k design_docs -q`
Expected: FAIL — `phase2-7-plugins.md:246` の旧文言が残っている

- [ ] **Step 6-4c: Write minimal implementation**

`docs/superpowers/specs/2026-07-25-agentic-fx-design.md:391` の該当箇条を差し替え:

```markdown
- インターフェースは意図的に極小、かつ**純関数に限定** (I/O・外部アクセス禁止)。`indicator` は `compute(df, params) -> dict` (値は最終バーのスカラー `float`、**または df と同じ index の系列 `pd.Series`** — 系列を返す plugin は `config.yaml` の `outputs` で出力キーを宣言する。新規承認では `outputs` は必須)、`signal` は `detect(df, params) -> list[Signal]`、`strategy` は `evaluate(df, indicators, signals, params) -> StrategyDecision`。**`indicators` には strategy が `config.yaml` の `indicators:` で宣言した依存 indicator の出力だけが `{alias: {output_key: value}}` の形で入る** (依存の版は `pin` として `config.yaml` に書かれ `content_hash` の署名対象になる — [indicator-consumption-wiring] 2026-09-14)。`signals` は現行ハーネスでは常に `None`。`params` は plugin_loader が `config.yaml` を読み込んで渡す。qwen3.6 クラスの実装力でも品質が安定し、テストが決定論的になる粒度にする
```

`docs/superpowers/plans/2026-08-02-phase2-7-plugins.md:246` を差し替え:

```markdown
3. session で `evaluate(df, indicators, signals=None, params)` (**indicators は
   [indicator-consumption-wiring] (2026-09-14) で配線済み** — strategy が
   `config.yaml` の `indicators:` で宣言した依存だけが渡る。signals は
   依然 None 固定)
```

`src/agentic_fx/plugin/sandbox.py:3-10` の脅威モデル節の末尾に追記:

```python
**同居実行の残余リスク ([indicator-consumption-wiring] §3)**: strategy と、
その依存 indicator は**同一 worker プロセス**で実行される (IPC を deps 倍に
しないため — 非ベクトル化 1 本で 60 秒 CPU 予算が破れた実測がある)。承認前の
strategy 候補と承認済 indicator が同居するため、以下の 3 面を追加で塞いでいる:
(i) module 到達 — `import` 遮断 + 一意名 import (`indicator_<alias>`) で
`sys.modules` 衝突も防ぐ (ii) df / params の mutation — call ごとに deep copy
(iii) pandas/numpy の**プロセス全体のグローバル状態** — `check_source` の
deny 名 (`set_option`/`reset_option`/`set_eng_float_format`/`seterr`/
`seterrcall`/`setbufsize`/`set_printoptions`) と「外部属性への代入・削除の
一律拒否」、加えて worker 側で `pd.get_option("mode.chained_assignment")` と
`np.geterr()` の call 前後不変を assert する。いずれも「善意だが不注意な
plugin の事故」を防ぐ多層防御であり、悪意ある攻撃者からの完全な隔離を
保証しない — **最終防衛線は人間承認**。
```

- [ ] **Step 6-4d: Run test to verify it passes**

Run: `uv run pytest tests/fixtures -q`
Expected: PASS

- [ ] **Step 6-4e: Commit**

```bash
git add docs src/agentic_fx/plugin/sandbox.py tests/fixtures
git commit -m "docs: update plugin contract for indicator series and colocated execution"
```

---

**T6a 完了条件**:
- [ ] `docs/examples/plugins/rsi_indicator` が系列 + `outputs: [rsi]` で、
      自身の `test_plugin.py` が緑
- [ ] `docs/examples/plugins/rsi_pullback` が新設され、判定式が設計書 §2.10 逐語
      (境界 `curr == oversold` は hold)、`discover` と `check_source` を通る、
      unpinned のまま
- [ ] **A1 の fixture 部分**: `tests/fixtures/indicator_wiring.py` の bar 生成式が
      設計書 §6 逐語 (33,984 本・端点・`open_k = close_{k-1}`)、
      `expected_eval_timestamps` が `market_hours.is_market_open` から導出され
      (引数なし)、oracle が plugin コードを import せず warmup 14 本の hold を
      再現し、**open が 30 件以上** (着手前 probe 実測 102 件)
- [ ] 設計書 `2026-07-25` / `phase2-7-plugins.md:246` / sandbox 脅威モデルが更新済み
      (`tests/fixtures/wiring_envs.py` は **T6b** の完了条件へ移した — opus r1 I3)
- [ ] 段 0 変異 red: (a) oracle の `load_resampled_frame(max_bars=MAX_BARS)` を
      `max_bars=None` にする → `test_oracle_produces_hold_during_warmup_then_values`
      の warmup 期待が崩れる (b) `expected_eval_timestamps` の `is_market_open`
      判定を削る → `test_expected_eval_timestamps_follow_market_hours` が落ちる

---

## T6b: 共通テストヘルパ `tests/fixtures/wiring_envs.py` [wiring-test-envs]

**この task は opus r1 I3 で T6 から切り出した独立 task。** `wiring_envs` は 20 個の
ビルダを定義し、T4 / T5 の受入テストがほぼ全部これに依存する。T6 のままだと
検証が `switch_env` 1 本のみで、**壊れたまま「T6 緑」になり T4/T5 の実装中に一気に
爆発する**構造だった (実際 opus r1 では `improve_env` (C2) / `activity_text` (C3) /
`shell_env` (C4) / `stage_switched_journal` (I8) の 4 ビルダが現物 API と噛み合って
いなかった)。**各ビルダに最小 1 本の smoke test を付ける** — 「起動できる」ではなく
「返り値で 1 手進める」まで踏む ([[measure-capability-not-startup]])。

**対応**: 設計書 §6 の fixture 節 (テスト環境の再現)。受入 ID は持たない (他 task の
受入を支える基盤) が、**T4 の R2 / D1 と T5 の F4 / F5 / C1 の成否を丸ごと決める**。

**着手条件**: T6a が main にマージ済み (`tests.fixtures.indicator_wiring` を import する)。
**T3 と並列可** (T3 は `wiring_envs` を使わない — opus r1 M12)。**T4 着手前に main へマージ**。

**Files:**
- Create: `tests/fixtures/wiring_envs.py`
- Modify: `tests/fixtures/test_wiring_envs.py` (新規、ビルダごとの smoke test)

**Interfaces (Produces — 逐語シグネチャ。後続 task はこの名前しか使わない):**

```python
# tests/fixtures/wiring_envs.py
SETTINGS_FIXTURE: Settings                    # holdout_months / eval_source / base_interval を fixture 値に

def switch_env(root: Path) -> tuple[Connection, Path]: ...
    # (conn, plugins_root)。plugins/ と plugins/_human を作る
def reconcile_env(root: Path) -> tuple[Connection, Path, ActivityLog]: ...
def improve_env(root: Path) -> tuple[ImproveLoop, Connection, Path]: ...
    # factory は毎回 connect(db_path) (opus r1 I6)。conn はテスト専用の別接続
def improve_env_with_activity(root: Path) -> tuple[ImproveLoop, Connection, Path, ActivityLog]: ...
def loop_env(root: Path) -> tuple[ImproveLoop, Connection, Path]: ...
    # (loop, conn, plugins_root) — 質検査 (P5) 用の薄い別名
def prepare_ctx(loop: ImproveLoop, *, now: datetime) -> ImproveRunContext: ...
    # opus r1 C1: loop.prepare(slot_key=None, now=now) の 3-tuple から ctx を取る
def synthetic_ctx(loop: ImproveLoop, conn: Connection, root: Path) -> ImproveRunContext: ...
    # prepare_ctx が tmp 環境で成立しない場合のみ使う fallback
def activity_text(activity: ActivityLog) -> str: ...
    # opus r1 C3: ActivityLog に read_text() は無い。tail(10_000) の join
def shell_env(root: Path) -> tuple[Commands, Connection, Path]: ...
    # opus r1 C4: commands.Shell ではなく commands.Commands
def rpc_tooldefs(root: Path, *, counters: MissionToolCounters,
                 run_backtest_handler: Callable) -> list[ToolDef]: ...
    # ToolRegistry(on_execute=…, on_result=…).register_all(defs) にそのまま渡す
def rpc_tools(root: Path, *, counters: MissionToolCounters,
              run_backtest_handler: Callable) -> dict[str, Callable]: ...
    # tooldef を直接呼ぶ経路 (registry を通らない = errors/streak は増えない)
def deploy_strategy(conn: Connection, plugins_root: Path, name: str, *,
                    pins: dict) -> str: ...                      # content_hash
def bump_indicator_version(conn: Connection, plugins_root: Path, name: str, *,
                           now: datetime) -> str: ...            # 新 content_hash
def submit_indicator_v2(conn: Connection, plugins_root: Path,
                        name: str) -> tuple[int, str]: ...       # (approval_id, content_hash)
def approve_indicator_v2(conn: Connection, plugins_root: Path, name: str, *,
                         approval_id: int) -> None: ...
def stage_switched_journal(conn: Connection, plugins_root: Path, *, name: str,
                           pins: dict, now: datetime
                           ) -> tuple[str, str, int, str]: ...
    # (old_target, new_target, approval_id, op_id)。opus r1 I8: payload の
    # content_hash は新 version dir の実体から、advance は commit=True
def copy_example(dest_root: Path, name: str) -> Path: ...
def rename_dependency(plugin_dir: Path, old: str, new: str) -> None: ...
def write_dependency_free_strategy(base: Path, name: str) -> Path: ...
def completed_result(output: dict) -> MissionResult: ...
def mission_for(ctx: ImproveRunContext) -> Mission: ...

def install_gate_double(monkeypatch, *, pytest_ok: bool = True) -> list[dict]: ...  # v1.4b: 本文 (Step 6b-1c) に合わせて訂正、T6b 実装済み
    # [codex plan r1 C1] `strategy_gate.evaluate_strategy_adoption_gate` を
    # 差し替え、受け取った kwargs を記録する list を返す。
```

### Step 6b-1: `wiring_envs` の全ビルダと smoke test

- [ ] **Step 6b-1a: Write the failing test**

`tests/fixtures/test_wiring_envs.py` (新規)。**ビルダごとに 1 本**、「返り値で
1 手進める」ところまで書く:

```python
"""[indicator-consumption-wiring] T6b: `wiring_envs` の各ビルダの smoke test。

opus r1 I3: 元案は `switch_env` 1 本しか検証しておらず、他の 21 ビルダは
「壊れたまま緑」で T4/T5 へ渡る構造だった。各ビルダについて**返り値を使って
1 手進める**ところまで検査する。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentic_fx.activity import Category
from tests.fixtures import indicator_wiring as fx
from tests.fixtures import wiring_envs as env


def test_switch_env_produces_isolated_roots(tmp_path):
    conn, plugins_root = env.switch_env(tmp_path / "a")
    assert plugins_root.is_dir() and plugins_root.name == "plugins"
    assert (plugins_root / "_human").is_dir()
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0
    assert str(plugins_root).startswith(str(tmp_path))


def test_install_gate_double_makes_a_strategy_submit_fast_and_green(
        tmp_path, monkeypatch):
    """codex plan r1 C1: gate double が `submit_candidate` を実 backtest
    無しで通す。

    **ここで `resolved` は見ない** — T6b は T3 と並列、T4a より前にマージ
    される task なので、この時点では `evaluate_strategy_adoption_gate` に
    `resolved` が渡っていない可能性がある (T3 未マージ) し、実解決が
    行われるのは T4a 以降。「resolver は double より前に 1 回だけ走る」は
    **T4a Step 4-1a の `test_run_kind_gate_resolves_once_and_carries_the_
    same_object` が pin する** — ここでは double 自体が機能することだけを
    見る ([[measure-capability-not-startup]]: 「返り値で 1 手進める」= 
    approval 行が実際に作られるところまで)。"""
    from agentic_fx.plugin import switch as plugin_switch
    conn, plugins_root = env.switch_env(tmp_path / "gd")
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": hashes["rsi"]})
    seen = env.install_gate_double(monkeypatch)
    approval_id = plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=env.SETTINGS_FIXTURE, now=fx.NOW)
    assert approval_id > 0
    assert len(seen) == 1                      # gate は 1 回だけ呼ばれた
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "pending"


def test_reconcile_env_activity_is_writable(tmp_path):
    conn, plugins_root, activity = env.reconcile_env(tmp_path / "b")
    activity.write(Category.APPROVAL, "probe", "hello world")
    assert "probe" in env.activity_text(activity)


def test_activity_text_returns_written_lines(tmp_path):
    """opus r1 C3: `ActivityLog.read_text()` は存在しない。行の**形**
    (tab 区切り 5 列、3 列目 = event、4 列目 = summary) をここで 1 度だけ
    固定し、R2 / F5 / C1 の逐語 pin はこの形を前提に書く。"""
    _conn, _root, activity = env.reconcile_env(tmp_path / "c")
    activity.write(Category.IMPROVE, "backtest_cpu",
                   "mission=1 plugin=rsi_pullback cpu_sec=1.5")
    line = env.activity_text(activity).splitlines()[-1]
    cols = line.split("\t")
    assert len(cols) == 5
    assert cols[1] == "IMPROVE"
    assert cols[2] == "backtest_cpu"
    assert cols[3] == "mission=1 plugin=rsi_pullback cpu_sec=1.5"
    assert cols[4] == "-"


def test_improve_env_survives_a_closed_handler_conn(tmp_path):
    """opus r1 I6: 親の `run_backtest_handler` は自分で開いた conn を
    `finally` で閉じる。factory が毎回新規接続を返さないと、1 回回した
    時点でテスト側の conn まで閉じる。"""
    loop, conn, root = env.improve_env(tmp_path / "d")
    handler_conn = loop._db_write_conn_factory()
    handler_conn.close()
    # テスト側の conn は生きている
    assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_prepare_ctx_builds_a_run_context(tmp_path):
    """opus r1 C1: `prepare` は 3-tuple を返し `conn` 引数を持たない。
    tmp 環境で `WorkerRunner` 構築まで通ることをここで確かめる
    (通らなければ `synthetic_ctx` へ切り替え、指揮者へ申告する)。"""
    loop, _conn, root = env.improve_env(tmp_path / "e")
    ctx = env.prepare_ctx(loop, now=fx.NOW)
    assert ctx.staging_dir.is_dir()
    assert ctx.source_snapshot_dir.is_dir()
    assert ctx.mission_id > 0 and ctx.run_id > 0


def test_synthetic_ctx_is_usable_without_prepare(tmp_path):
    loop, conn, root = env.improve_env(tmp_path / "f")
    ctx = env.synthetic_ctx(loop, conn, root)
    assert ctx.staging_dir.is_dir() and ctx.rpc_handlers == {}


def test_shell_env_dispatches(tmp_path):
    """opus r1 C4: `commands.Shell` は存在しない。`Commands.dispatch` が
    動くところまで進める。"""
    cmds, conn, plugins_root = env.shell_env(tmp_path / "g")
    assert "learning" in cmds.dispatch("status")
    assert cmds.plugins_root == plugins_root
    assert cmds.settings is env.SETTINGS_FIXTURE


def test_rpc_tools_expose_run_backtest(tmp_path):
    from agentic_fx.tools.mission_counters import MissionToolCounters
    calls = []
    tools = env.rpc_tools(tmp_path / "h", counters=MissionToolCounters(),
                          run_backtest_handler=lambda a: calls.append(a)
                          or {"started": True})
    assert "run_backtest" in tools
    tools["run_backtest"](name="cand", pair="USDJPY")
    assert calls and calls[0]["name"] == "cand"


def test_rpc_tooldefs_can_be_registered(tmp_path):
    """opus r1 I5: F4 の `errors` / refusal streak は `ToolRegistry` の
    `on_result` 経由でしか増えない。tooldef のリストがそのまま
    `ToolRegistry` に渡せることをここで据える。"""
    from agentic_fx.tools.mission_counters import MissionToolCounters
    from agentic_fx.tools.registry import ToolRegistry
    counters = MissionToolCounters()
    defs = env.rpc_tooldefs(tmp_path / "h2", counters=counters,
                            run_backtest_handler=lambda a: {
                                "started": False, "error": "indicator_unresolved"})
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)
    registry.execute("run_backtest", {"name": "cand", "pair": "USDJPY"},
                     allowed=registry.names())
    assert counters.errors == 1


def test_deploy_strategy_is_discoverable_and_hashes_match(tmp_path):
    from agentic_fx.plugin.loader import content_hash, discover_one_with_reason
    conn, plugins_root = env.switch_env(tmp_path / "i")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    chash = env.deploy_strategy(conn, plugins_root, "rsi_pullback",
                                pins={"rsi": hashes["rsi"]})
    meta, reason = discover_one_with_reason(plugins_root / "rsi_pullback",
                                            "rsi_pullback")
    assert reason is None and meta.content_hash == chash
    assert chash == content_hash((plugins_root / "rsi_pullback").resolve())


def test_bump_indicator_version_changes_hash_and_symlink(tmp_path):
    conn, plugins_root = env.switch_env(tmp_path / "j")
    before = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)["rsi"]
    after = env.bump_indicator_version(conn, plugins_root, "rsi",
                                       now=fx.NOW + timedelta(hours=1))
    assert after != before
    assert (plugins_root / "rsi").is_symlink()


def test_submit_and_approve_indicator_v2_round_trip(tmp_path):
    # `agentic_fx.store.approvals` に `get(conn, id)` は**存在しない**
    # (公開 API は create / apply_decision / pending / expire_due /
    # list_due_for_expiry / set_reason。着手時に
    # `rg -n '^def ' src/agentic_fx/store/approvals.py` で再取得)。
    # 既存テスト (`tests/test_commands.py`) と同じ SQL で status を読む。
    def _status(conn, aid):
        return conn.execute(
            "SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()[0]

    conn, plugins_root = env.switch_env(tmp_path / "k")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    approval_id, chash = env.submit_indicator_v2(conn, plugins_root, "rsi")
    assert _status(conn, approval_id) == "pending"
    env.approve_indicator_v2(conn, plugins_root, "rsi",
                             approval_id=approval_id)
    assert _status(conn, approval_id) == "approved"


@pytest.mark.slow
def test_stage_switched_journal_reconciles_to_decided_when_pin_intact(tmp_path):
    """opus r1 I8: このヘルパは R2 の成否を丸ごと決める。**pin が破れて
    いないとき reconcile が `decided` まで進む**ことをここで据える
    (T4b Step 4-7 の R2 は「破れているとき reverted」を見る裏返し)。"""
    from agentic_fx.plugin import switch as plugin_switch
    conn, plugins_root = env.switch_env(tmp_path / "l")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    old_target, new_target, approval_id, op_id = env.stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    # 現物は `reconcile_switch_journals(conn, *, plugins_root, now, settings,
    # activity=None, force_revert_op_id=None)` (着手時に
    # `rg -n 'def reconcile_switch_journals' -A 5 src/agentic_fx/plugin/switch.py`
    # で再取得)。T4b Step 4-7 で引数が増えたら本テストも更新すること。
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, settings=env.SETTINGS_FIXTURE,
        now=fx.NOW + timedelta(minutes=1))
    # テーブル名は **`plugin_switch_journal`** (`store/db.py` の DDL)
    row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id = ?",
        (op_id,)).fetchone()
    assert row[0] == "decided", (
        "pin が破れていないのに decided へ進まない — payload の content_hash か "
        "advance_switch_journal(commit=True) を疑う (opus r1 I8)")
    assert (plugins_root / "rsi_pullback").readlink().name \
        == new_target.rsplit("/", 1)[-1]


def test_copy_example_does_not_touch_the_repo(tmp_path):
    dest = env.copy_example(tmp_path / "m", "rsi_pullback")
    assert (dest / "plugin.py").exists()
    assert str(dest).startswith(str(tmp_path))


def test_rename_dependency_rewrites_config(tmp_path):
    import yaml
    d = fx.write_rsi_pullback(tmp_path / "n", pins=None)
    env.rename_dependency(d, "rsi", "rsi_other")
    config = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8"))
    assert config["indicators"]["rsi"]["plugin"] == "rsi_other"


def test_write_dependency_free_strategy_discovers_with_no_indicators(tmp_path):
    from agentic_fx.plugin.loader import discover_one_with_reason
    d = env.write_dependency_free_strategy(tmp_path / "o", "plain")
    meta, reason = discover_one_with_reason(d, "plain")
    assert reason is None and meta.indicators == ()


def test_completed_result_and_mission_for_are_accepted_shapes(tmp_path):
    loop, conn, root = env.improve_env(tmp_path / "p")
    ctx = env.synthetic_ctx(loop, conn, root)
    result = env.completed_result({"summary": "x"})
    assert result.status == "completed"
    assert env.mission_for(ctx).max_turns == 1
```

> **smoke test で「起動できる」で止めない** ([[measure-capability-not-startup]])。
> 上の各テストは必ず「返り値を 1 手使う」ところまで踏んでいる
> (`dispatch("status")` / `discover_one_with_reason` / `reconcile → decided` /
> `prepare → ctx.staging_dir`)。

- [ ] **Step 6b-1b: Run test to verify it fails**

Run: `uv run pytest tests/fixtures/test_wiring_envs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.fixtures.wiring_envs'`

- [ ] **Step 6b-1c: Write minimal implementation**

`tests/fixtures/wiring_envs.py` (新規):

```python
"""[indicator-consumption-wiring] テスト環境ビルダ (T3〜T5 共有)。

**実 DB・実 `plugins/`・`docs/examples/` を書き換えない** — 全て `tmp_path`
配下に作る。各テストモジュールは名前を `_` 付きで別名 import してよい
(例: `from tests.fixtures.wiring_envs import switch_env as _switch_env`)。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from agentic_fx.config import load_settings
from agentic_fx.store.db import connect, connect_readonly, init_db
from tests.fixtures import indicator_wiring as fx

_REPO = Path(__file__).resolve().parents[2]

# opus r1 (未確認 #9 への予防): 元案は `load_settings(...)` の戻りを**その場で
# mutate** していた。`Settings` は pydantic モデルで既定 mutable なので動くが、
# module-level の共有オブジェクトを書き換える形はテスト間汚染の温床。
# `model_copy` で新しい object を作る (着手時に `rg -n 'class Settings' src/agentic_fx/config.py`
# で pydantic v2 であることと `backtest` のフィールド名を再確認すること)。
SETTINGS_FIXTURE = load_settings(_REPO / "config" / "settings.yaml.example").model_copy(
    deep=True)
SETTINGS_FIXTURE.backtest = SETTINGS_FIXTURE.backtest.model_copy(
    update={"holdout_months": fx.HOLDOUT_MONTHS, "eval_source": fx.SOURCE,
            "base_interval": fx.BASE_INTERVAL})


class _FakeRag:
    """`ImproveLoop.__init__(rag=...)` を満たすだけの no-op。
    `tests/loops/conftest.py` の `_FakeRag` からの逐語転写
    (手書きの偽形状は禁止 — [[test-fixtures-from-real-transcripts]])。"""


def _db(root: Path):
    (root / "data").mkdir(parents=True, exist_ok=True)
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    return conn


def switch_env(root: Path):
    """`(conn, plugins_root)`。`plugins/` と `plugins/_human` を作る。"""
    root.mkdir(parents=True, exist_ok=True)
    conn = _db(root)
    plugins_root = root / "plugins"
    (plugins_root / "_human").mkdir(parents=True, exist_ok=True)
    return conn, plugins_root


def reconcile_env(root: Path):
    """`(conn, plugins_root, activity)`。activity は `ActivityLog`。"""
    from agentic_fx.activity import ActivityLog
    conn, plugins_root = switch_env(root)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return conn, plugins_root, ActivityLog(root / "logs" / "activity.log")


def improve_env(root: Path):
    """`(loop, conn, root)`。`ImproveLoop` を本番同型で組む。

    **opus r1 C2 / I6 是正**。元案には 2 つの欠陥があった:

    1. `ImproveLoop.__init__` は
       `(*, root, settings, clock, db_write_conn_factory, db_readonly_conn_factory,
       activity: ActivityLog, rag: Rag)` で **`activity` / `rag` も必須**
       (着手時に `rg -n 'def __init__' -A 12 src/agentic_fx/loops/improve_loop.py`
       で再取得)。5 引数だけでは `TypeError`。
    2. write/readonly の両 factory に**同一の 1 本の conn** を返すと、
       親の `run_backtest_handler` が `finally: conn.close()` する既存構造
       (Step 5-4c 参照) により 1 回 backtest を回した時点でテスト側の
       `conn` まで閉じ、以降の `conn.execute(...)` が
       `ProgrammingError: Cannot operate on a closed database` になる。

    本番 (`service.py` の配線) と同型の「毎回 db_path へ新規接続」にし、
    テストが握る `conn` は**別に 1 本**開く。`tests/loops/conftest.py` の
    `loop_no_seam` fixture がこの形の現物なので逐語転写する
    (`_conn_for_test` seam は**立てない** — seam を立てると prepare が
    conn を閉じない代わりに上記 1 本共有に戻ってしまう)。
    """
    from agentic_fx.activity import ActivityLog
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.loops.improve_loop import ImproveLoop
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "plugins").mkdir(exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    db_path = root / "data" / "agentic.db"
    bootstrap = connect(db_path)
    init_db(bootstrap)
    bootstrap.close()
    conn = connect(db_path)          # テストが握る接続 (loop のものとは別)
    loop = ImproveLoop(root=root, settings=SETTINGS_FIXTURE,
                       clock=FixedClock(fx.NOW),
                       db_write_conn_factory=lambda: connect(db_path),
                       db_readonly_conn_factory=lambda: connect_readonly(db_path),
                       activity=ActivityLog(root / "logs" / "activity.log"),
                       rag=_FakeRag())
    return loop, conn, root


def improve_env_with_activity(root: Path):
    """`(loop, conn, root, activity)`。`activity` は `improve_env` が
    `ImproveLoop(activity=...)` へ渡した `ActivityLog` そのもの
    (`ImproveLoop.__init__` が `self._activity = activity` を持つのは現物で
    確認済み — 着手時に `rg -n '_activity' src/agentic_fx/loops/improve_loop.py`
    で再確認する)。"""
    loop, conn, root = improve_env(root)
    return loop, conn, root, loop._activity


def prepare_ctx(loop, *, now):
    """`ImproveRunContext` を 1 本で取る (**opus r1 C1 是正**)。

    現物のシグネチャは
    `prepare(self, *, slot_key: tuple[str, int] | None, now: datetime,
    on_ready: Callable[[dict], None] | None = None) -> tuple[Mission, ImproveRunContext, WorkerRunner]`
    (着手時に `rg -n 'def prepare' -A 4 src/agentic_fx/loops/improve_loop.py`
    で再取得)。すなわち **`conn` 引数は無く** (conn は
    `db_write_conn_factory` から自前で取る)、**`slot_key` はキーワード必須**、
    **戻り値は 3-tuple で ctx は 2 番目**。プラン v1 が全テストで書いていた
    `ctx = _prepare_ctx(loop, now=...)` は
    `TypeError: prepare() got an unexpected keyword argument 'conn'` で即死する。
    T5 の全テストは**このヘルパ経由**にすること。

    `prepare` は副作用として `WorkerRunner` を構築する。tmp 環境で成立する
    ことは T6b の smoke test (`test_prepare_ctx_builds_a_run_context`) で
    確かめる — 成立しない場合のみ `ImproveRunContext` 直組みの
    `synthetic_ctx` (下記) に切り替える。
    """
    _mission, ctx, _runner = loop.prepare(slot_key=None, now=now)
    return ctx


def synthetic_ctx(loop, conn, root: Path):
    """`prepare` を経由せず `ImproveRunContext` を直組みする fallback。
    `tests/loops/conftest.py` の `loop_and_ctx` fixture からの逐語転写。
    **`prepare_ctx` が tmp 環境で成立する限り使わない** (T6b の smoke test が
    判定する)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.loops.improve_run_context import ImproveRunContext
    staging_dir = root / "staging"
    source_snapshot_dir = root / "source"
    staging_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot_dir.mkdir(parents=True, exist_ok=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": 600.0, "analyze_corr": 600.0})
    return ImproveRunContext(
        mission_id=1, run_id=1, staging_dir=staging_dir,
        source_snapshot_dir=source_snapshot_dir,
        allowed_backlog_ids=frozenset(), slot_key=None, ledger=ledger,
        rpc_handlers={})


def activity_text(activity) -> str:
    """activity ログの全文 (**opus r1 C3 是正**)。

    `ActivityLog` の公開メソッドは `write(category, event, summary, ref_id=None)`
    と `tail(n=20, category=None)` **だけ** — `read_text()` は存在しない
    (プラン v1 は 4 箇所で `activity.read_text()` を呼んでおり全部
    `AttributeError`)。逐語 pin はこのヘルパ経由で行う。

    **行の形 (逐語)**: `ActivityLog.write` は
    `"\t".join([ts_iso_seconds, category.value, event, " ".join(summary.split()), ref_id or "-"])`
    を 1 行として書く。つまり受入の「逐語」は**行全体の完全一致ではなく
    `event` と `summary` 部分の一致**で見る (先頭 2 列は時刻とカテゴリ)。
    T6b の smoke test (`test_activity_text_returns_written_lines`) で
    この形を 1 度だけ確かめてから 4 箇所へ展開すること。
    """
    return "\n".join(activity.tail(10_000))


def loop_env(root: Path):
    """`(loop, conn, plugins_root)` — 質検査 (P5) 用の薄い別名。"""
    loop, conn, root = improve_env(root)
    return loop, conn, root / "plugins"


def shell_env(root: Path):
    """`(cmds, conn, plugins_root)` — `commands.Commands` (**opus r1 C4 是正**)。

    `agentic_fx.commands` にあるクラスは `Shell` ではなく **`Commands`** で、
    `__init__` は `conn / state_store / broker / trade_loop / activity /
    log_dir / clock` が**すべて必須**、`root` 引数は存在しない
    (`plugins_root` / `settings` / `health_latch` などが任意)。
    `tests/test_commands.py::_commands` の組み方を逐語転写する。
    `_approval_detail` (Step 4-8c) は `self.plugins_root` と `self.settings` を
    読むので両方渡す。
    """
    from unittest.mock import MagicMock

    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.core.health_latch import HealthLatch
    from agentic_fx.core.paper_broker import PaperBroker
    from agentic_fx.store.state import StateStore
    conn, plugins_root = switch_env(root)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    clock = FixedClock(fx.NOW)
    cmds = Commands(
        conn=conn, state_store=StateStore(root / "s.json"),
        broker=PaperBroker(conn, SETTINGS_FIXTURE, clock),
        trade_loop=MagicMock(),
        activity=ActivityLog(root / "logs" / "activity.log"),
        log_dir=root / "logs", clock=clock, health_latch=HealthLatch(),
        plugins_root=plugins_root, settings=SETTINGS_FIXTURE)
    return cmds, conn, plugins_root


def rpc_tooldefs(root: Path, *, counters, run_backtest_handler) -> list:
    """`improve_rpc_tools.build_improve_rpc_tooldefs` の薄いラッパ
    (staging_dir 検証を通すため候補を 1 本置く)。**`ToolDef` のリスト**を
    返すので、`ToolRegistry(on_execute=counters.record_call,
    on_result=counters.record_tool_result)` を作って `register_all(defs)` で
    登録し `execute(name, args, allowed=registry.names())` で呼べる
    (`ToolRegistry.__init__` はキーワード専用で tooldef を取らない —
    `mission_registry.py` の構築行の逐語)。F4 の `errors` / refusal streak は
    registry の `on_result` 経由でしか増えないため必要 (opus r1 I5)。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.tools import improve_rpc_tools
    staging = root / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    (staging / "cand").mkdir(parents=True, exist_ok=True)
    shutil.copytree(staging / "rsi_pullback", staging / "cand",
                    dirs_exist_ok=True)
    return improve_rpc_tools.build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 60,
                                                         "analyze_corr": 60}),
        run_backtest_handler=run_backtest_handler,
        analyze_corr_handler=lambda a: {},
        staging_dir=staging, counters=counters,
        budget=SETTINGS_FIXTURE.improve.tool_budget)


def rpc_tools(root: Path, *, counters, run_backtest_handler):
    """`{tool 名: func}` — tooldef を**直接呼ぶ**経路 (registry を通らない)。"""
    return {d.name: d.func for d in
            rpc_tooldefs(root, counters=counters,
                         run_backtest_handler=run_backtest_handler)}


def deploy_strategy(conn, plugins_root: Path, name: str, *, pins: dict) -> str:
    """`rsi_pullback` 相当の strategy を `name` で配備 (承認 + 正規形 symlink)。
    戻り値 = `content_hash`。"""
    from agentic_fx.plugin.loader import content_hash
    src = fx.write_rsi_pullback(plugins_root, pins=pins)
    if name != "rsi_pullback":
        dest = plugins_root / name
        shutil.copytree(src, dest)
        shutil.rmtree(src)
        src = dest
    chash = content_hash(src)
    fx.deploy_approved(conn, plugins_root, [name], now=fx.NOW)
    return chash


def bump_indicator_version(conn, plugins_root: Path, name: str, *,
                           now: datetime) -> str:
    """配備済 indicator を「出力不変の変更」で I2 に更新し、承認して
    live symlink を差し替える。戻り値 = 新 `content_hash`。"""
    from agentic_fx.plugin.loader import artifact_hash_bytes, content_hash
    from agentic_fx.store import approvals
    current = (plugins_root / name).resolve()
    plugin_py = current.read_bytes() if current.is_file() else \
        (current / "plugin.py").read_bytes()
    new_py = plugin_py + b"\n# v2 (output-preserving change)\n"
    config = (current / "config.yaml").read_bytes()
    test_py = (current / "test_plugin.py").read_bytes()
    ahash = artifact_hash_bytes(new_py, config, test_py)
    version_dir = plugins_root / ".versions" / name / ahash
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "plugin.py").write_bytes(new_py)
    (version_dir / "config.yaml").write_bytes(config)
    (version_dir / "test_plugin.py").write_bytes(test_py)
    link = plugins_root / name
    if link.is_symlink():
        link.unlink()
    link.symlink_to(Path(".versions") / name / ahash)
    chash = content_hash(version_dir)
    aid = approvals.create(conn, "plugin",
                           {"name": name, "kind": "indicator",
                            "content_hash": chash}, now)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t",
                             now=now)
    return chash


def submit_indicator_v2(conn, plugins_root: Path, name: str):
    """I2 を `plugins/_human/<name>` から submit する (pending のまま)。
    戻り値 = `(approval_id, content_hash)`。"""
    from agentic_fx.plugin import switch as plugin_switch
    from agentic_fx.plugin.loader import content_hash
    human = plugins_root / "_human" / name
    shutil.copytree((plugins_root / name).resolve(), human, dirs_exist_ok=True)
    (human / "plugin.py").write_text(
        (human / "plugin.py").read_text() + "\n# v2\n")
    approval_id = plugin_switch.submit_candidate(
        conn, name=name, staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    return approval_id, content_hash(human)


def approve_indicator_v2(conn, plugins_root: Path, name: str, *,
                         approval_id: int) -> None:
    from agentic_fx.plugin import switch as plugin_switch
    plugin_switch.approve_candidate(
        conn, approval_id, decided_by="human_cli", now=fx.NOW,
        plugins_root=plugins_root, settings=SETTINGS_FIXTURE)


def stage_switched_journal(conn, plugins_root: Path, *, name: str, pins: dict,
                           now: datetime):
    """`switched` 段で止まった journal を再現する
    (版 dir 作成 + live symlink 差し替え + journal phase=switched、DB は
    まだ `decided` にしない)。戻り値 =
    `(old_target, new_target, approval_id, op_id)`。

    **opus r1 I8 是正 (2 点)**:

    1. 元案は新 version dir に `plugin.py + "\n# next\n"` を書きながら
       approval payload には**旧** `content_hash` を入れていた。
       reconcile → `retry_approval` → `approve_candidate` →
       `_version_dir_hashes_ok(version_dir, content_hash=payload["content_hash"], ...)`
       が不一致で落ちるため、R2 の裏テスト
       (`test_switched_journal_with_intact_pin_proceeds_to_decided`) が
       そもそも `decided` に到達しない。**payload の `content_hash` は
       新 version dir の実体から `loader.content_hash(version_dir)` で
       算出する** (`artifact_hash` は既に `artifact_hash_bytes` で
       新実体から算出しているので整合する)。
    2. `advance_switch_journal(...)` は既定 `commit=False` なので、
       phase を書いても**同一 conn の外からは見えない**。
       `commit=True` を明示する (`begin_switch_journal` 側は既に
       `commit=True`)。

    このヘルパは R2 の成否を丸ごと決めるので、**T6b で「実際に reconcile が
    `decided` まで進む」smoke test を 1 本据えてから** T4b Step 4-7 へ渡すこと。
    """
    from agentic_fx.plugin import switch as plugin_switch
    from agentic_fx.plugin.loader import artifact_hash_bytes
    from agentic_fx.store import approvals
    old_hash = deploy_strategy(conn, plugins_root, name, pins=pins)
    old_target = f".versions/{name}/{(plugins_root / name).readlink().name}"
    human = plugins_root / "_human" / name
    shutil.copytree((plugins_root / name).resolve(), human, dirs_exist_ok=True)
    (human / "plugin.py").write_text(
        (human / "plugin.py").read_text() + "\n# next\n")
    ahash = artifact_hash_bytes((human / "plugin.py").read_bytes(),
                                (human / "config.yaml").read_bytes(),
                                (human / "test_plugin.py").read_bytes())
    new_target = f".versions/{name}/{ahash}"
    version_dir = plugins_root / new_target
    version_dir.mkdir(parents=True, exist_ok=True)
    for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
        (version_dir / rel).write_bytes((human / rel).read_bytes())
    from agentic_fx.plugin.loader import content_hash as _content_hash
    new_hash = _content_hash(version_dir)   # opus r1 I8: 新実体から算出
    assert new_hash != old_hash, (
        "stage_switched_journal: 新 version dir の content_hash が旧と同じ "
        "— plugin.py への追記が効いていない")
    approval_id = approvals.create(
        conn, "plugin",
        {"name": name, "kind": "strategy", "candidate_origin": "human",
         "candidate_path": f"plugins/_human/{name}",
         "content_hash": new_hash, "artifact_hash": ahash}, now)
    op_id = plugin_switch.begin_switch_journal(
        conn, kind="approve", approval_id=approval_id, name=name,
        old_kind="symlink", old_target=old_target, new_target=new_target,
        switch_required=True, actor="human_cli", now=now, commit=True)
    plugin_switch.switch_live(plugins_root, name, new_target=new_target,
                              op_id=op_id)
    plugin_switch.advance_switch_journal(conn, op_id, phase="switched", now=now,
                                        commit=True)   # opus r1 I8
    return old_target, new_target, approval_id, op_id


def copy_example(dest_root: Path, name: str) -> Path:
    """`docs/examples/plugins/<name>` を `dest_root/<name>` へ逐語コピー
    (`__pycache__` は除く)。"""
    dest = dest_root / name
    dest.mkdir(parents=True, exist_ok=True)
    src = _REPO / "docs" / "examples" / "plugins" / name
    for rel in ("plugin.py", "config.yaml", "test_plugin.py"):
        (dest / rel).write_bytes((src / rel).read_bytes())
    return dest


def rename_dependency(plugin_dir: Path, old: str, new: str) -> None:
    """候補 `config.yaml` の `indicators.*.plugin` を置換する。"""
    path = plugin_dir / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    for ref in (config.get("indicators") or {}).values():
        if isinstance(ref, dict) and ref.get("plugin") == old:
            ref["plugin"] = new
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def write_dependency_free_strategy(base: Path, name: str) -> Path:
    """`indicators:` を持たない strategy 候補 (`indicator_deps == {}` の pin 用)。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(
        "def evaluate(df, indicators, signals, params):\n"
        "    return {'action': 'hold', 'rationale': 'no dependency'}\n")
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "strategy", "timeframe": "1h", "pairs": [fx.PAIR],
         "exit_mode": "levels", "max_bars": 200, "params": {}},
        sort_keys=False))
    (d / "test_plugin.py").write_text(
        "from plugin import evaluate\n\n\n"
        "def test_holds():\n"
        "    assert evaluate(None, {}, None, {})['action'] == 'hold'\n")
    return d


def completed_result(output: dict):
    """`MissionResult(status='completed', output=output)`。"""
    from agentic_fx.runners.base import MissionResult
    return MissionResult("completed", output, [], reason=None)


def mission_for(ctx):
    """`ImproveLoop.commit(mission=...)` に渡す最小の Mission。"""
    from agentic_fx.runners.base import Mission
    return Mission(prompt="", tools=[], output_schema=None, max_turns=1,
                   timeout_sec=60)


def install_gate_double(monkeypatch, *, pytest_ok: bool = True):
    """[codex plan r1 C1] strategy gate (= 実 backtest) を差し替える。

    **なぜ必要か**: `switch_env` は空 DB しか作らないのに、`submit_candidate`
    は実 `_run_full_gate` → `run_kind_gate` → `evaluate_strategy_adoption_gate`
    から **実 backtest** を回す。履歴が無ければ `NoHistoryError` で、履歴が
    あれば 3 ヶ月 × 1h の実 worker 実行で数分かかる。**承認回廊の lock 順序 /
    payload 形状 / 再解決タイミングを見る受入 (A2 / P2' / P2'' / P3 / P3') は
    gate の中身を見ていない**ので、gate を double にして高速・決定論にする。

    **保たれるもの** (double にしても観測点が死なない理由): `run_kind_gate` の
    `resolve_indicator_deps(...)` 呼び出しは **gate 呼び出しより前**にあるので、
    `GateOutcome.resolved` は実 resolver の産物のままであり、
    `indicator_unresolved` の判別子・`indicator_deps` payload・P3' の
    `is` 同一性はすべて実経路で観測できる。

    **使ってはいけないところ**: 実 backtest そのものが受入の対象である
    A1 / A1-b / C1 (`tests/plugin/test_indicator_wiring_e2e.py`) は実 worker で
    回す (Global Constraints「モックで実 worker を潰さない」)。

    **verdict の形は既存の動作実績のある double からの逐語転写**
    ([[test-fixtures-from-real-transcripts]]): `tests/plugin/
    test_approval_payload_common_contract.py:90-100` の `_fake_pytest_ok` /
    `_fake_evaluable_gate` (着手時に
    `rg -n '_fake_evaluable_gate|_fake_pytest_ok' -A 8
    tests/plugin/test_approval_payload_common_contract.py` で再取得)。
    **`cpu_samples` は載せない** — このフィールドは T4a で追加されるので、
    T6b (= T4a より前にマージする task) の smoke test 時点では
    `TypeError` になる。T4a 以降の呼び出し元で cpu_samples が必要な
    テストは、その場で `dataclasses.replace(verdict, cpu_samples=...)`
    するか実 gate を使うこと。

    `pytest_ok=True` (既定) は `switch.run_gate_pytest` も差し替える —
    候補ごとの実 pytest 実行は本 task の観測対象ではなく、上記の既存
    テストも同じ組で差し替えている。

    戻り値 = gate が受け取った kwargs を積む list。
    """
    from agentic_fx.plugin import strategy_gate
    from agentic_fx.plugin.gate_pytest import GateResult
    seen: list[dict] = []

    def _fake(conn, **kw):
        seen.append(kw)
        return strategy_gate.StrategyGateVerdict(
            evaluable=True, baseline_variant="no_strategy",
            baseline_row={"plugin_ref": "no_strategy:cand",
                          "variant": "no_strategy"},
            candidate_metrics={"USDJPY": {"trades": 40, "pf": 1.2}})

    monkeypatch.setattr(
        strategy_gate, "evaluate_strategy_adoption_gate", _fake)
    # `approval.py` は `from agentic_fx.plugin import strategy_gate` で
    # **モジュールを** import しているので (`rg -n 'import strategy_gate'
    # src/agentic_fx/plugin/approval.py` で着手時に再確認)、モジュール属性の
    # 差し替えで call site を捕捉できる。関数を直接 import する形に変わって
    # いたらここも `approval.evaluate_strategy_adoption_gate` に変えること
    # ([[shared-reader-crosses-layer-boundaries]])。
    if pytest_ok:
        from agentic_fx.plugin import switch as _switch
        monkeypatch.setattr(
            _switch, "run_gate_pytest",
            lambda plugin_dir, *, settings: GateResult(
                passed=True, returncode=0, stdout_tail="1 passed",
                duration_sec=0.1))
    return seen
```

(`GateResult` の import 元は着手時に
`rg -n 'class GateResult|def run_gate_pytest' src/agentic_fx/plugin/` で再取得する。)

各テストモジュールの先頭で別名 import する:

```python
from tests.fixtures.wiring_envs import (
    SETTINGS_FIXTURE, activity_text as _activity_text,
    approve_indicator_v2 as _approve_indicator_v2,
    bump_indicator_version as _bump_indicator_version,
    completed_result as _completed_result, copy_example as _copy_example,
    deploy_strategy as _deploy_strategy, improve_env as _improve_env,
    improve_env_with_activity as _improve_env_with_activity,
    install_gate_double as _install_gate_double,
    loop_env as _loop_env, mission_for as _mission,
    prepare_ctx as _prepare_ctx, reconcile_env as _reconcile_env,
    rename_dependency as _rename_dependency,
    rpc_tooldefs as _rpc_tooldefs, rpc_tools as _rpc_tools,
    shell_env as _shell_env,
    stage_switched_journal as _stage_switched_journal,
    submit_indicator_v2 as _submit_indicator_v2, switch_env as _switch_env,
    synthetic_ctx as _synthetic_ctx,
    write_dependency_free_strategy as _write_dependency_free_strategy,
)
```

> **`_last_rendered_prompt` について**: `ImproveLoop._render_improve_mission_prompt`
> の戻り値を `self._last_rendered_prompt` に保持する 1 行を T5a Step 5-1c で足す
> (テスト専用の観測面。本番挙動は変わらない)。

- [ ] **Step 6b-1d: Run test to verify it passes**

Run: `uv run pytest tests/fixtures -q && uv run pytest tests/fixtures -q -m slow`
Expected: PASS (22 ビルダ全部に smoke test が緑で付いている)

- [ ] **Step 6b-1e: Commit**

```bash
git add tests/fixtures/wiring_envs.py tests/fixtures/test_wiring_envs.py
git commit -m "test(fixtures): shared environment builders with per-builder smoke tests"
```

**T6b 完了条件**:
- [ ] `tests/fixtures/wiring_envs.py` が Produces の 20 シンボルを逐語シグネチャで定義し、
      **それぞれに smoke test が 1 本以上**ある (`switch_env` だけの検証にしない)
- [ ] `improve_env` が `ImproveLoop(activity=..., rag=...)` を渡し、両 factory が
      **毎回新規接続**を返す (handler の `conn.close()` でテスト側 conn が死なない)
- [ ] `prepare_ctx` が tmp 環境で `ImproveRunContext` を返す
      (返らない場合は `synthetic_ctx` へ切替 + 指揮者へ申告)
- [ ] `activity_text` の行の形 (tab 5 列) が pin されている
- [ ] `shell_env` が `Commands` を組み `dispatch("status")` が通る
- [ ] `stage_switched_journal` から `reconcile_switch_journals` が `decided` へ進む
- [ ] 実 DB (`data/agentic.db`) / 実 `plugins/` / `docs/examples/` を書き換えない
      (全て `tmp_path` 配下、`copy_example` は read-only)
- [ ] 段 0 変異 red: (a) `improve_env` の factory を `lambda: conn` 共有に戻す →
      `test_improve_env_survives_a_closed_handler_conn` が落ちる
      (b) `stage_switched_journal` の `content_hash` を旧 hash に戻す →
      `test_stage_switched_journal_reconciles_to_decided_when_pin_intact` が落ちる
      (c) `advance_switch_journal` の `commit=True` を外す → 同上

---

## T3: composition root への配線 [composition-roots]

**対応**: 設計書 §2.3 の root 表、§4 の `strategy_adapter.py` / `service.py` /
`signal_producer.py` / `mission_worker.py` / `market_tools.py` / `backtest/cli.py` 行。
**完了条件の受入 ID**: F1 / F2 / A1' / A1'' / P1 (人間 CLI 部分)。

**着手条件**: T1・T2・**T6a** が main にマージ済み (A1' は T6a の fixture を使う)。
T6b には依存しない (opus r1 M12) ので **T6b と並列可**。

**Files:**
- Modify: `src/agentic_fx/plugin/strategy_adapter.py:200-283`
- Modify: `src/agentic_fx/plugin/strategy_gate.py:167-298`、`:321-336` (引数の透通のみ)
- Modify: `src/agentic_fx/plugin/approval.py:119-179` (`_validate_strategy` の透通)、
  `:283` 付近 (`run_kind_gate` の本体が `evaluate_strategy_adoption_gate` へ
  `resolved=ResolvedIndicatorSet.empty(meta.path.parent)` を渡す暫定 — **シグネチャは
  変えない**、T4a Step 4-1c で実解決に置換。codex plan r1 C2)
- (**`src/agentic_fx/plugin/switch.py` は T3 では触らない** — `_run_full_gate` の
  `plugins_root` 引数と inventory 構築は **T4a Step 4-2c** が所有する。codex plan r1 C2)
- Modify: `src/agentic_fx/service.py:823-838`、`src/agentic_fx/plugin/signal_producer.py:87-131`
- Modify: `src/agentic_fx/mission_worker.py:908-928`、`src/agentic_fx/tools/market_tools.py:25-38`
- Modify: `src/agentic_fx/backtest/cli.py:118-142` (parser)、`:374-440`、`:483-600` (`_plugin_lock`)、
  `:632-643` (dispatch)
- Modify: `src/agentic_fx/loops/improve_loop.py:889-900` (`run_backtest_handler` の透通のみ)
- Test: `tests/plugin/test_strategy_adapter.py`、`tests/test_service_app.py`、
  `tests/backtest/test_cli.py`、`tests/plugin/test_signal_producer.py`

**Interfaces:**

- Consumes: `resolve.ResolvedIndicatorSet` / `.empty(root)` / `resolve_indicator_deps` /
  `IndicatorResolutionError(alias, reason)` (T1)、`PluginSession(meta, *, settings,
  resolved=None)` / `.cpu_sec` (T2)、`tests.fixtures.indicator_wiring` (T6a Step 6-3)、
  `plugin_loader.approved_plugins(conn, plugins_dir, *, settings) -> InventoryBuildResult` (T1)。
  **`tests.fixtures.wiring_envs` (T6b) は使わない** — T3 のテストは
  `_cli_root_with_deployed_rsi_pullback` をローカル定義する (opus r1 M12)。
  そのため **T3 は T6b と並列に進められる**。
- Produces:

```python
# src/agentic_fx/plugin/strategy_adapter.py
class PluginStrategyIntentSource:
    def __init__(self, meta: PluginMeta, *, conn, pair: str, dataset,
                 settings: "Settings", resolved: "ResolvedIndicatorSet",
                 session: _SessionLike | None = None,
                 decision_sink: "Callable[[datetime, dict], None] | None" = None
                 ) -> None: ...
    eval_count: int
    @property
    def cpu_sec(self) -> float | None: ...
def build_intent_source(meta, *, conn, pair, dataset, settings,
                        resolved: "ResolvedIndicatorSet",
                        session=None, decision_sink=None
                        ) -> PluginStrategyIntentSource: ...

# src/agentic_fx/plugin/strategy_gate.py
def evaluate_strategy_adoption_gate(conn, *, meta, now, settings,
                                    resolved: "ResolvedIndicatorSet",
                                    name=None, pairs=None, timeframe=None,
                                    content_hash=None, kind="strategy",
                                    history_conn=None, run_in_sample_fn=None,
                                    run_holdout_gate_fn=None, record_fn=None,
                                    floor_mode="enforce",
                                    inventory: "InventoryBuildResult | None" = None,
                                    ) -> "StrategyGateVerdict | None": ...

# src/agentic_fx/plugin/approval.py
def _validate_strategy(conn, meta, *, settings, now, run_in_sample_fn,
                       resolved: "ResolvedIndicatorSet",
                       ) -> tuple[dict[str, dict], bool]: ...
# `run_kind_gate` の**シグネチャは T3 では不変** (`inventory` / `pin_mode` の
# 追加と `GateOutcome` の判別子は T4a)。本体だけが
# `evaluate_strategy_adoption_gate(..., resolved=ResolvedIndicatorSet.empty(...))`
# を渡す暫定になる — codex plan r1 C2

# src/agentic_fx/plugin/signal_producer.py
def evaluate_due_plugins(self, conn, *, plugins, now, source, sandbox_run=None,
                         settings,
                         resolved_by_identity: (
                             "dict[tuple[str, str], ResolvedIndicatorSet]"),
                         # ^ キーは `(meta.name, meta.content_hash)`
                         # (codex plan r2 束2 Critical — 旧案 `resolved_by_hash:
                         # dict[str, ...]` は content_hash 単独キーだったため、
                         # 同一 content_hash・別名の 2 strategy が存在すると
                         # `InventoryBuildResult.resolved` (キーは `(name, hash)`)
                         # から dict comprehension で潰す際にどちらか一方が
                         # 消え、P3' の「同一オブジェクト」不変条件と Global
                         # Constraints の既存 identity `(name, content_hash)`
                         # の両方に反していた)
                         ) -> int: ...

# src/agentic_fx/backtest/cli.py
# `afx plugin lock --from _human <name>` サブコマンド (`args.plugin_command == "lock"`)
def _plugin_lock(conn, settings, args, root: Path) -> int: ...
```

### Step 3-1: adapter の `resolved` 必須化 + `decision_sink` + `cpu_sec`

- [ ] **Step 3-1a: Write the failing test**

> **先に読む — `build_intent_source` の既存呼び出しの全数移行表 (opus r1 I1)**
>
> `resolved` を**キーワード必須**にすると、既存の呼び出し・patch がすべて
> `TypeError` になる。プラン v1 は「4 つの src 呼び出し元へ透通させる」しか
> 書いておらず、**テスト側の移行が欠落**していた (Step 3-1d は
> `tests/plugin tests/backtest -q` の PASS を期待しているので必ず赤になる)。
> T1 Step 1-7 と同じ流儀で **`rg -n 'build_intent_source' src tests` の全結果を
> 貼り、着手時に再取得して差分を確認すること** (行番号は v1.3 執筆時点の値)。
>
> > **codex plan r1 I2 是正**: v1.1/v1.2 の表は「patch 12 + 直接呼び出し 7 =
> > 19 箇所」と書いていたが、`rg -n 'build_intent_source' src tests` の実測は
> > **patch 15 + `tests/plugin/test_strategy_adapter.py` の直接呼び出し 16 =
> > 31 箇所** (+ src 5 + 定義 1 + docstring 言及 6 = 全 43 行)。以下の表を
> > 実測どおりに再生成した。**着手時に `rg` を打ち直すこと**。
>

> **src 側 (5 箇所、本 Step と Step 3-4 / T4 / T5 で `resolved=` を渡す)**
>
> | path:line | 呼び出し元 | 渡す `resolved` |
> |---|---|---|
> | `plugin/strategy_gate.py:287` | in_sample 経路 | `evaluate_strategy_adoption_gate(resolved=)` |
> | `plugin/strategy_gate.py:323` | holdout 経路 | 同上 (**同一 object**、P3') |
> | `plugin/approval.py:162` | `_validate_strategy` | 引数で透通 (Produces 参照) |
> | `backtest/cli.py:405` | 人間 CLI | Step 3-4 で `check` 解決 |
> | `loops/improve_loop.py:895` | 改善 RPC | T5b Step 5-4 で解決 |
>
> **テスト側 patch (15 箇所)** — `def fake_build_intent_source(meta_arg, *, conn,
> pair, dataset, settings)` のような**固定シグネチャの fake** が置かれている。
> **一律方針: fake の引数末尾に `**kwargs` を足す** (`resolved` / `decision_sink` を
> 受け流す)。fake が `resolved` を検査する必要がある箇所だけ明示引数にする。
>
> | path | patch 行 | fake 定義行 |
> |---|---|---|
> | `tests/plugin/test_approval.py` | `:520, 677, 719` | `:511, 668, 708` |
> | `tests/plugin/test_strategy_gate.py` | `:52, 280, 323` | (lambda / インライン) |
> | `tests/plugin/test_switch_floor_modes.py` | `:530` | 同上 |
> | `tests/loops/test_improve_loop_rpc_handlers.py` | `:91, 332, 561` | 同上 |
> | `tests/loops/test_improve_loop_finalize.py` | `:516` | 同上 |
> | `tests/integration/test_improve_forbidden_regression.py` | `:297` | 同上 |
> | `tests/backtest/test_cli.py` | `:567, 634, 686` | 同上 |
>
> (patch 対象が `agentic_fx.plugin.approval.strategy_adapter.build_intent_source`
> のように**モジュール属性経由**なので、`patch(...)` の文字列自体は変えない)
>
> **テスト側の直接呼び出し (16 箇所、すべて `tests/plugin/test_strategy_adapter.py`)** —
> `:89, 119, 138, 158, 189, 213, 239, 257, 279, 297, 313, 326, 372, 398, 500, 530`
> (+ 本 Step で追記する分)。これらは **`resolved=_EMPTY` を足す**。
> `:158` 付近の「`resolved` 無しでも通る」契約テストは本 Step で反転する (F1)。
> `:530` は `pytest.raises` の中なので、`resolved=` を足しても期待例外は変わらない
> ことを確認してから直すこと。
>
> **AST による `resolved=` 必須検査** (codex plan r1 I2、T1 Step 1-7 の
> `approved_plugins` 全数性テストと同じ流儀)。`tests/plugin/test_strategy_adapter.py`
> に追記する:
>
> ```python
> def test_no_caller_calls_build_intent_source_without_resolved():
>     """[indicator-consumption-wiring] T3 (F1): `build_intent_source(...)` の
>     呼び出しが `src` にも `tests` にも `resolved=` 無しで残っていないこと。
>     `resolved` はキーワード必須なので実行すれば `TypeError` になるが、
>     patch された経路・未到達分岐では発見が遅れる。**`ast.Call` だけを見る**
>     (docstring 言及 6 箇所を拾わないため)。`**kwargs` で受け流す fake の
>     定義側は `ast.Call` ではないので対象外。"""
>     import ast
>     from pathlib import Path
>     repo = Path(__file__).resolve().parents[2]
>     offenders = []
>     for root in (repo / "src" / "agentic_fx", repo / "tests"):
>         for path in sorted(root.rglob("*.py")):
>             if path == Path(__file__).resolve():
>                 continue
>             tree = ast.parse(path.read_text(encoding="utf-8"),
>                              filename=str(path))
>             for node in ast.walk(tree):
>                 if not isinstance(node, ast.Call):
>                     continue
>                 f = node.func
>                 name = (f.attr if isinstance(f, ast.Attribute)
>                         else f.id if isinstance(f, ast.Name) else None)
>                 if name != "build_intent_source":
>                     continue
>                 if not any(kw.arg == "resolved" for kw in node.keywords):
>                     offenders.append(f"{path.relative_to(repo)}:{node.lineno}")
>     assert offenders == [], (
>         "build_intent_source(...) を resolved= 無しで呼んでいる箇所: "
>         f"{offenders}")
> ```
>
> (本テスト自身は `resolved=` を足した呼び出ししか書かないので自己除外は
> 不要だが、`tests/plugin/test_strategy_adapter.py` には `resolved` 無しで
> `TypeError` を確かめる F1 のテストが**ある** — そちらは
> `strategy_adapter.build_intent_source` を**変数経由**ではなく直接書くため
> offenders に入る。したがって**自ファイルは走査対象から外す** (上の
> `continue`)。F1 の `TypeError` は同ファイル内の実行テストで担保する。)
>
> **Step 3-1d の Run はこの移行を含めて緑にすること** (codex plan r1 I2:
> v1.2 の `tests/plugin tests/backtest` では `tests/loops` /
> `tests/integration` の patch 5 箇所が検証されない):
> `uv run pytest tests/plugin tests/backtest tests/loops tests/integration -q`

`tests/plugin/test_strategy_adapter.py:150-180` の契約テストを反転し、追記する:

```python
from agentic_fx.plugin.resolve import ResolvedIndicatorSet

_EMPTY = ResolvedIndicatorSet.empty(Path("/nonexistent/plugins"))


def test_payload_is_df_and_params_only(tmp_path):
    """[indicator-consumption-wiring] T3: indicators は worker 内で計算される
    ので payload に載らない。signals も送らない (worker が None を渡す)。"""
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="1h", max_bars=2, params={"period": 7})
    session = _FakeSession()
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY, session=session)
    assert src(_bar(H + timedelta(hours=5))) is None
    payload = session.calls[0]
    assert set(payload) == {"df", "params"}
    assert payload["params"] == {"period": 7}


def test_build_intent_source_requires_resolved(tmp_path):
    """F1: `resolved` 無しは TypeError、worker は 1 つも起動しない。"""
    import subprocess
    conn = _conn(tmp_path)
    meta = _meta(timeframe="1h")
    calls = []
    real = subprocess.Popen
    try:
        subprocess.Popen = lambda *a, **k: calls.append(a)
        with pytest.raises(TypeError):
            strategy_adapter.build_intent_source(
                meta, conn=conn, pair="USDJPY", dataset=DATASET_1M,
                settings=SETTINGS)
    finally:
        subprocess.Popen = real
    assert calls == []


def test_resolved_is_passed_to_the_lazily_created_session(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="1h")
    seen = {}

    class _Spy:
        def __init__(self, meta_arg, *, settings, resolved=None):
            seen["resolved"] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            return dict(_HOLD_RESULT)
        def close(self):
            return None
        cpu_sec = 1.25

    monkeypatch.setattr(strategy_adapter, "PluginSession", _Spy)
    sentinel = ResolvedIndicatorSet.empty(Path("/plugins"))
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=sentinel)
    src(_bar(H))
    assert seen["resolved"] is sentinel      # 同一オブジェクト (再解決しない)
    src.close()
    assert src.cpu_sec == 1.25


def test_decision_sink_records_every_evaluation_including_holds(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    meta = _meta(timeframe="1h")
    open_result = {"action": "open", "rationale": "x", "direction": "long",
                   "entry_type": "market", "limit_price": None,
                   "stop_loss": 99.0, "take_profit": 101.0}
    session = _FakeSession(results=[dict(_HOLD_RESULT), open_result])
    recorded = []
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair="USDJPY", dataset=DATASET_1M, settings=SETTINGS,
        resolved=_EMPTY, session=session, decision_sink=lambda ts, d:
            recorded.append((ts, d)))
    src(_bar(H))                      # hold
    src(_bar(H + timedelta(hours=1)))  # open
    assert [ts for ts, _ in recorded] == [H + timedelta(hours=1),
                                          H + timedelta(hours=2)]
    assert recorded[0][1]["action"] == "hold"
    assert recorded[1][1]["action"] == "open"
    assert recorded[1][1]["stop_loss"] == 99.0


def test_decision_sink_defaults_to_none_in_production_path(tmp_path):
    conn = _conn(tmp_path)
    _seed_flat(conn, H, 8 * 60 + 1)
    src = strategy_adapter.build_intent_source(
        _meta(timeframe="1h"), conn=conn, pair="USDJPY", dataset=DATASET_1M,
        settings=SETTINGS, resolved=_EMPTY, session=_FakeSession())
    assert src._decision_sink is None
```

- [ ] **Step 3-1b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_strategy_adapter.py -q`
Expected: FAIL — `TypeError: build_intent_source() got an unexpected keyword argument 'resolved'`

- [ ] **Step 3-1c: Write minimal implementation**

`src/agentic_fx/plugin/strategy_adapter.py` の `__init__` / `__call__` / `close` /
`build_intent_source` を置換:

```python
    def __init__(self, meta: PluginMeta, *, conn: sqlite3.Connection,
                pair: str, dataset, settings: "Settings",
                resolved: "ResolvedIndicatorSet",
                session: _SessionLike | None = None,
                decision_sink: "Callable[[datetime, dict], None] | None" = None,
                ) -> None:
        """`resolved` は**必須** ([indicator-consumption-wiring] §2.3) —
        依存なしでも `ResolvedIndicatorSet.empty(inventory_root)` を渡す。
        アダプタは**内部で再解決しない**: 解決は composition root で 1 回、
        同じオブジェクトがここと `PluginSession` を通って worker まで届く。

        `decision_sink` は**テスト専用の観測面** (設計書 §6、codex r7 I2)。
        `backtest_runs` は metrics しか持たず `orders` は open しか表せない
        ため、hold を含む全時点の照合はこの sink でしか取れない。本番経路は
        常に `None` (既定) — 呼び出し元は渡さない。
        """
        if pair not in meta.pairs:
            raise ValueError(
                f"pair {pair!r} is not in plugin {meta.name!r}'s declared "
                f"pairs {meta.pairs!r}")
        self._meta = meta
        self._conn = conn
        self._pair = pair
        self._dataset = dataset
        self._settings = settings
        self._resolved = resolved
        self._session = session
        self._decision_sink = decision_sink
        self._owns_session = session is None
        self._cpu_sec: float | None = None
        self.eval_count = 0

    @property
    def cpu_sec(self) -> float | None:
        """自分が生成したセッションの累積 CPU 秒 (`close()` 後に確定)。
        注入セッション・未評価・異常終了では `None` (設計書 §2.4、C1)。"""
        return self._cpu_sec

    def __call__(self, closed_bar: Bar) -> dict | None:
        width = parse_timeframe(closed_bar.interval)
        bucket_end = closed_bar.ts + width
        if floor_to_bucket(bucket_end, self._meta.timeframe) != bucket_end:
            return None

        df = load_resampled_frame(
            self._conn, self._pair, self._meta.timeframe,
            source=self._dataset.source,
            base_interval=self._dataset.base_interval,
            until=bucket_end, max_bars=self._meta.max_bars)
        if df.empty:
            return None

        session = self._ensure_session()
        self.eval_count += 1
        result = session.call({"df": df, "params": self._meta.params})
        if self._decision_sink is not None:
            self._decision_sink(bucket_end, result)
        return _strategy_result_to_intent(result, self._pair)

    def _ensure_session(self) -> _SessionLike:
        if self._session is None:
            session = PluginSession(self._meta, settings=self._settings.plugin,
                                    resolved=self._resolved)
            session.__enter__()
            self._session = session
        return self._session

    def close(self) -> None:
        """自分が生成したセッションのみ閉じる。close 完了後に `cpu_sec` を
        取り込む (`PluginSession.cpu_sec` は close 後に確定する property)。"""
        if self._owns_session and self._session is not None:
            self._session.close()
            self._cpu_sec = getattr(self._session, "cpu_sec", None)
            self._session = None


def build_intent_source(meta: PluginMeta, *, conn: sqlite3.Connection,
                        pair: str, dataset, settings: "Settings",
                        resolved: "ResolvedIndicatorSet",
                        session: _SessionLike | None = None,
                        decision_sink: "Callable[[datetime, dict], None] | None" = None,
                        ) -> PluginStrategyIntentSource:
    """`meta` (kind="strategy") から `pair` 用の `IntentSource` を組み立てる。
    呼び出し元は `run_replay` 実行後、必ず `close()` を呼ぶこと (try/finally)。
    `resolved` は必須 — 依存なしでも空 set を渡す。"""
    return PluginStrategyIntentSource(
        meta, conn=conn, pair=pair, dataset=dataset, settings=settings,
        resolved=resolved, session=session, decision_sink=decision_sink)
```

import を追加:

```python
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from agentic_fx.config import Settings
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet
```

**同じコミットで 4 つの呼び出し元へ `resolved` を透通させる** (意味は変えない —
値の出所を正しくするのは Step 3-2 以降と T4/T5):

1. `strategy_gate.evaluate_strategy_adoption_gate` に **キーワード必須**引数
   `resolved: "ResolvedIndicatorSet"` を足し、`:287` と `:323` の
   `build_intent_source(...)` へ `resolved=resolved` を渡す。docstring に
   「`resolved` は呼び出し元 (`run_kind_gate` / `ImproveLoop`) が composition
   root で 1 回だけ解決したもの。この関数は再解決しない」を書く。
2. `approval._validate_strategy` にキーワード必須引数 `resolved` を足し、
   `:162` の `build_intent_source(...)` へ渡す。`_validate_kind` は
   `resolved` をそのまま透通させる (`indicator`/`signal` 分岐では使わない)。
   `submit_plugin` は kind=strategy を拒否するので
   `resolved=ResolvedIndicatorSet.empty(meta.path.parent)` を渡して素通りさせる。
   **`_validate_kind` 側の `resolved` は `resolved: "ResolvedIndicatorSet | None"
   = None` (キーワード任意)** にする — `indicator` / `signal` 分岐は使わないので、
   T3 と T4a の間で `run_kind_gate` の非 strategy 経路が壊れない。
3. **`approval.run_kind_gate` の本体 (`approval.py:283` 付近) だけ**を直し、
   `evaluate_strategy_adoption_gate(...)` へ暫定の空集合を渡す:

   ```python
       verdict = strategy_gate.evaluate_strategy_adoption_gate(
           conn, meta=meta, settings=settings, now=now,
           floor_mode=floor_mode, record_fn=_sink,
           # [indicator-consumption-wiring] T3 暫定 (T4a Step 4-1c で
           # `resolve_indicator_deps(...)` の実解決に置き換える)。
           # 依存 0 本なので `empty()` の root 引数は解決に使われない。
           # 依存ありの候補は worker 内で `KeyError` → `SandboxError` →
           # gate 不合格になる = fail closed (下の 4. と同じ理屈)。
           resolved=ResolvedIndicatorSet.empty(meta.path.parent))
   ```

   **`run_kind_gate` のシグネチャは T3 では変えない** (`inventory` /
   `pin_mode` キーワードの追加と `GateOutcome` の判別子は T4a Step 4-1c)。

   > **codex plan r1 C2 是正 — `_run_full_gate` は T3 では触らない。**
   > v1.2 はここで `switch._run_full_gate` に `plugins_root` を足して inventory を
   > 構築し `run_kind_gate(..., resolved=resolved, inventory=inventory)` を
   > 呼ばせていたが、**この契約はどの段でも成立しない**:
   > (a) T3 単独では `run_kind_gate` がまだ `resolved` / `inventory` を受けないので
   > `TypeError`、(b) T4a 適用後の `run_kind_gate` は `resolved` を**受けず内部で
   > 解決する**設計 (Step 4-1c、P3' の「候補ごとに 1 回」) なので、外から渡す形は
   > 二重解決になり P3' に反する。
   >
   > **採る方式 = resolver / inventory 構築 / `run_kind_gate` 新契約を T4a に
   > 一本化する** (codex の提案 2 案のうち前者)。根拠:
   > 1. **解決は 1 箇所 (P3')** が本束の中心的な不変条件で、T3 に前倒しすると
   >    T3 と T4a の 2 箇所に resolver 呼び出しが並ぶ期間が生まれ、T4a で
   >    片方を消す作業が発生する。「一時的に 2 箇所」を許すと段 0 の変異
   >    スイープ (M1 = `pin_mode` 反転) の観測点が段によって変わる。
   > 2. **`_run_full_gate` は T3 の受入 ID を 1 つも持たない** — T3 の受入は
   >    F1 (adapter、`strategy_adapter.py`) / F2 (service 起動、`service.py`) /
   >    A1'・A1'' (人間 CLI、`backtest/cli.py`) / P1 (`afx plugin lock`) で、
   >    承認回廊 (`switch.py`) は 1 件も含まれない。逆に `_run_full_gate` を
   >    使う受入 (F3' / U4a) は**すべて T4a の完了条件**にある。
   > 3. 「T3 で `plugins_root` 引数だけ足す」(codex の提案の字面) は**採らない** —
   >    使われない引数を 1 task 分持ち越すことになり、レビューで「どの root を
   >    渡すのが正しいのか」という判断不能な問いを残す。**引数と用途を
   >    同じ Step (T4a Step 4-2c) で足す**。
   >
   > この結果、T3 が `switch.py` に加える変更は**ゼロ**になる (T3 の Files から
   > `switch.py` の行を削除した)。`_run_full_gate(..., plugins_root)` は
   > **T4a の Produces** で、`submit_candidate` / `bless_candidate` からの
   > 受け渡しも T4a Step 4-2c で同時に足す。
4. `improve_loop.run_backtest_handler` (`:895`) は
   `resolved=ResolvedIndicatorSet.empty(self._root / "plugins")` を渡す
   (**T5b Step 5-4 で `ImproveRunContext.inventory` からの `check` 解決に
   置き換える**。それまでは依存ありの staging 候補は worker 内で
   `KeyError` → `SandboxError` → `backtest_failed` になる = fail closed)。
5. `improve_loop._run_strategy_gate` (`improve_loop.py:1391`) に
   `resolved` を足して `evaluate_strategy_adoption_gate` へ中継する。
   `:2258` の呼び出し元は暫定で
   `resolved=ResolvedIndicatorSet.empty(self._root / "plugins")` を渡す
   (**T5b Step 5-5 で `ImproveRunContext.inventory` からの `require` 解決に
   置き換える**)。**この 5 番目を落とすと T3 以降の改善 E2E が
   `TypeError` で赤くなり、Step 3-5d の全数実行が通らない。**

- [ ] **Step 3-1d: Run test to verify it passes**

Run: `uv run pytest tests/plugin tests/backtest tests/loops tests/integration -q`
Expected: PASS

(**codex plan r1 I2 是正**: v1.2 は `tests/plugin tests/backtest` だけを走らせて
いたが、本 Step は `loops/improve_loop.py` の 2 箇所 (`:895` の
`run_backtest_handler` と `:1391` / `:2258` の `_run_strategy_gate`) を変更し、
patch 5 箇所が `tests/loops` / `tests/integration` にある。**その 2 ディレクトリを
含めないと移行漏れが検出できない**。)

- [ ] **Step 3-1e: Commit**

```bash
git add src/agentic_fx/plugin src/agentic_fx/loops/improve_loop.py \
       tests/plugin tests/loops tests/integration tests/backtest
git commit -m "feat(adapter): require resolved indicator set and add decision_sink (F1)"
```

(**codex plan r1 I2 是正**: v1.2 の `git add src/agentic_fx/plugin tests/plugin`
は、本 Step で実際に変更する `src/agentic_fx/loops/improve_loop.py` と
非-plugin テスト 5 ファイルを stage しておらず、コミットが**その時点で赤い
main** を作る。)

### Step 3-2: service 起動と signal producer

- [ ] **Step 3-2a: Write the failing test**

`tests/test_service_app.py` に追記:

```python
def test_service_startup_excludes_pin_broken_strategy_from_producer(tmp_path, caplog):
    """F2 (設計書 v1.3 §6 で緩和): pin 破れ strategy は warning 1 行
    (reason 込み) で producer の plugin 一覧に含まれない。session cache への
    未登録は producer 一覧に無いことの帰結にすぎないため、別項目としては
    観測しない (申し送り F2、2026-09-14 裁定)。"""
    import logging

    from agentic_fx.plugin.loader import content_hash
    from agentic_fx.service import build_app, run_init
    from tests.fixtures import indicator_wiring as fx

    root = tmp_path
    run_init(root)
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(exist_ok=True)
    fx.write_indicator(plugins_dir, "rsi")
    conn = connect(root / "data" / "agentic.db")
    hashes = fx.deploy_approved(conn, plugins_dir, ["rsi"], now=fx.NOW)
    strat_dir = fx.write_rsi_pullback(plugins_dir, pins={"rsi": "a" * 64})
    from agentic_fx.store import approvals
    aid = approvals.create(conn, "plugin",
                           {"name": "rsi_pullback", "kind": "strategy",
                            "content_hash": content_hash(strat_dir)}, fx.NOW)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t",
                             now=fx.NOW)
    conn.close()

    with caplog.at_level(logging.WARNING):
        app = build_app(root)
    try:
        names = [m.name for m in app.approved_plugins]
        assert "rsi" in names and "rsi_pullback" not in names
        assert "indicator_unresolved:rsi:pin_mismatch" in caplog.text
        assert app.inventory_result.rejected_strategies[0].reason == "pin_mismatch"
    finally:
        app.close()
```

`tests/plugin/test_signal_producer.py` に追記:

```python
def test_producer_requires_resolved_for_strategy_and_reuses_the_same_object(
        tmp_path, monkeypatch):
    """F2: producer は解決済み strategy しか評価せず、`InventoryBuildResult`
    が持つ `ResolvedIndicatorSet` を**そのまま** session へ渡す。"""
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet
    seen = {}

    class _Spy:
        def __init__(self, meta, *, settings, resolved=None):
            seen[meta.content_hash] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            return {"action": "hold"}
        def close(self):
            return None

    monkeypatch.setattr(plugin_sandbox, "PluginSession", _Spy)
    sentinel = ResolvedIndicatorSet.empty(tmp_path)
    producer = SignalProducer()
    producer.evaluate_due_plugins(
        conn, plugins=[strategy_meta], now=NOW, source="mt5", settings=SETTINGS,
        resolved_by_identity={(strategy_meta.name, strategy_meta.content_hash):
                              sentinel})
    assert seen[strategy_meta.content_hash] is sentinel


def test_producer_skips_strategy_without_resolution(tmp_path, caplog):
    import logging
    producer = SignalProducer()
    with caplog.at_level(logging.WARNING):
        inserted = producer.evaluate_due_plugins(
            conn, plugins=[strategy_meta], now=NOW, source="mt5",
            settings=SETTINGS, resolved_by_identity={})
    assert inserted == 0
    assert "unresolved" in caplog.text


def test_producer_uses_name_and_hash_identity_not_hash_alone(tmp_path, monkeypatch):
    """P3' 回帰 (codex plan r2 束2 Critical): 同一 `content_hash`・別名の 2
    strategy が同時に inventory に存在するとき、producer は
    `InventoryBuildResult.resolved` の `(name, content_hash)` キーを保ったまま
    それぞれの `ResolvedIndicatorSet` を渡すこと — `content_hash` だけを
    キーにした辞書へ潰すと、どちらか一方の strategy が他方の
    `ResolvedIndicatorSet` を受け取ってしまう (2 つの `PluginSession` は
    `content_hash` が同一なので session cache 上は 1 個に共有されるが、渡す
    `resolved` はそれぞれの `(name, hash)` エントリでなければならない)。"""
    import dataclasses
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet
    shared_hash = "b" * 64
    # `PluginMeta` は `@dataclass(frozen=True, slots=True)` (`__dict__` を
    # 持たない) — `dataclasses.replace` で複製する。
    meta_a = dataclasses.replace(
        strategy_meta, name="strat_a", content_hash=shared_hash)
    meta_b = dataclasses.replace(
        strategy_meta, name="strat_b", content_hash=shared_hash)
    seen = {}

    class _Spy:
        def __init__(self, meta, *, settings, resolved=None):
            seen[meta.name] = resolved
        def __enter__(self):
            return self
        def call(self, payload):
            return {"action": "hold"}
        def close(self):
            return None

    monkeypatch.setattr(plugin_sandbox, "PluginSession", _Spy)
    sentinel_a = ResolvedIndicatorSet.empty(tmp_path / "a")
    sentinel_b = ResolvedIndicatorSet.empty(tmp_path / "b")
    producer = SignalProducer()
    producer.evaluate_due_plugins(
        conn, plugins=[meta_a, meta_b], now=NOW, source="mt5", settings=SETTINGS,
        resolved_by_identity={
            ("strat_a", shared_hash): sentinel_a,
            ("strat_b", shared_hash): sentinel_b})
    assert seen["strat_a"] is sentinel_a
    assert seen["strat_b"] is sentinel_b
```

- [ ] **Step 3-2b: Run test to verify it fails**

Run: `uv run pytest tests/test_service_app.py tests/plugin/test_signal_producer.py -q`
Expected: FAIL — `AttributeError: 'App' object has no attribute 'inventory_result'` /
`TypeError: evaluate_due_plugins() got an unexpected keyword argument 'resolved_by_identity'`

- [ ] **Step 3-2c: Write minimal implementation**

`src/agentic_fx/service.py:823` 周辺:

```python
        # プラン 7 Task 3 + [indicator-consumption-wiring] §2.3:
        # 起動時に 1 回だけ inventory を構築する (hot reload しない)。
        # 第 2 相を通った strategy だけが producer に渡り、その
        # `ResolvedIndicatorSet` は**再解決せず**そのまま使う (P3')。
        inventory_result = plugin_loader.approved_plugins(
            conn_core, plugins_dir, settings=settings)
        approved = list(inventory_result.inventory.metas)
```

`App` (dataclass / クラス) に `inventory_result` と `approved_plugins` を保持させ、
scheduler tick の producer 呼び出し (`service.py:617` 付近) を:

```python
            signal_producer.evaluate_due_plugins(
                conn_core, plugins=approved, now=now,
                source=settings.plugin.producer_source, settings=settings,
                # codex plan r2 束2 Critical: キーは `InventoryBuildResult.resolved`
                # と同じ `(name, content_hash)` のまま渡す — `content_hash` だけへ
                # 潰すと、同一 content_hash・別名の 2 strategy が存在するとき
                # どちらか一方の `ResolvedIndicatorSet` が失われる。
                resolved_by_identity=dict(inventory_result.resolved))
```

`src/agentic_fx/plugin/signal_producer.py`:

```python
    def evaluate_due_plugins(self, conn: "sqlite3.Connection", *,
                             plugins: list[PluginMeta], now: datetime,
                             source: str, sandbox_run: SandboxRunFn | None = None,
                             settings: "Settings",
                             resolved_by_identity: (
                                 "dict[tuple[str, str], ResolvedIndicatorSet]"),
                             ) -> int:
        """承認済み signal/strategy plugin のうち評価期限が来たバケットを
        評価し、新規挿入した signals 行の総数を返す。

        [indicator-consumption-wiring] §2.3 (codex plan r2 束2 Critical 是正):
        `resolved_by_identity` は `InventoryBuildResult.resolved` をそのまま
        (キー `(name, content_hash)`) 渡したもの。**strategy は解決済みの
        ものしか評価しない** — 未解決の strategy は warning + skip
        (fail closed。producer には再解決の責務を持たせない)。

        `resolved` の取得キーは `(meta.name, meta.content_hash)` (Global
        Constraints の既存 identity と同じ) — `content_hash` だけでは、
        同一 content_hash (= `plugin.py` + `config.yaml` が完全に同一バイト
        列) を持つ別名 strategy が存在したとき、どちらの `ResolvedIndicatorSet`
        を渡すべきか一意に決まらない。

        **session cache のキーは `content_hash` 単独のまま変えない**
        (codex plan r2 束2 Critical への回答、根拠 3 点):
        (1) `content_hash` は `plugin.py + config.yaml` のバイト列そのものの
        hash であり、`config.yaml` には依存宣言 (`indicators:` ブロックと
        `pin`) も含まれる — content_hash が一致する 2 つの strategy は
        **依存宣言も含めてバイト単位で同一**なので、同じ inventory 構築
        (= 同じ improve/service 起動) の中で解決された `ResolvedIndicatorSet`
        は値として等価になる。subprocess はコード実体に対して起動するもの
        であり、名前が違うだけの同一コードに対して 2 subprocess を持つのは
        無駄な二重起動になる。
        (2) `resolved` の**受け渡し**は `(name, hash)` 単位に保つ (上記) ため、
        「渡す値の identity 保証」と「subprocess 再利用の単位」を分離できる
        — session cache だけ `content_hash` のままでも、各 `PluginSession`
        の生成時に渡る `resolved` は必ずその meta 自身の `(name, hash)`
        エントリになる (下記 `_call` 参照: session 未生成時のみ `resolved=`
        を渡すため、2 度目以降の同一 content_hash session 再利用では
        `resolved` は再送しないが、対象コードが同一である以上、初回に渡した
        `resolved` で十分)。
        (3) `(name, hash)` を session cache キーに含めると、同一コードの
        strategy を改名しただけで無駄に subprocess が増える退行を生む
        (改善ループが探索中に同じコードを複数名で試す運用は想定内)。
        """
        inserted = 0
        sessions: dict[str, plugin_sandbox.PluginSession] = {}

        def _call(meta: PluginMeta, payload: dict) -> dict:
            if sandbox_run is not None:
                return sandbox_run(meta, payload, settings=settings.plugin)
            session = sessions.get(meta.content_hash)
            if session is None:
                session = plugin_sandbox.PluginSession(
                    meta, settings=settings.plugin,
                    resolved=resolved_by_identity.get(
                        (meta.name, meta.content_hash)))
                session.__enter__()
                sessions[meta.content_hash] = session
            return session.call(payload)

        try:
            for meta in plugins:
                if meta.kind not in ("signal", "strategy"):
                    continue
                if (meta.kind == "strategy"
                        and (meta.name, meta.content_hash)
                        not in resolved_by_identity):
                    _log.warning(
                        "plugin %s: indicator dependencies are unresolved — "
                        "skipping (fail closed)", meta.name)
                    continue
                ...  # 以降は現行のまま
```

`_evaluate_one` の strategy 呼び出し (`:228-231`) を payload 縮小:

```python
        result = call(meta, {"df": df, "params": meta.params})
```

- [ ] **Step 3-2d: Run test to verify it passes**

Run: `uv run pytest tests/test_service_app.py tests/plugin tests/test_e2e_plugin_signal.py -q`
Expected: PASS

- [ ] **Step 3-2e: Commit**

```bash
git add src/agentic_fx/service.py src/agentic_fx/plugin/signal_producer.py tests
git commit -m "feat(service): pass resolved indicator sets to the signal producer (F2)"
```

### Step 3-3: trade worker の別 root

- [ ] **Step 3-3a: Write the failing test**

`tests/test_mission_worker.py` に追記:

```python
def test_trade_worker_builds_its_own_inventory_and_passes_indicator_metas(
        tmp_path, monkeypatch):
    """[indicator-consumption-wiring] §2.3: trade worker は **別 root** で
    `approved_plugins` を再実行する (producer との版ずれは許容)。
    `market_tools` には indicator kind だけが届く。

    **codex plan r1 I3 是正**: v1.2 の本テストは fake `approved_plugins` を
    **テスト自身が直接呼び**、その結果を**テスト自身が** `build_mission_registry`
    に渡していた。`mission_worker.py` のコードは 1 行も実行されないので、
    src を何も直さなくても assert が成立する = 記載どおりの Expected FAIL に
    ならない ([[verify-integration-not-just-units]])。**trade worker の実入口を
    起動し、`plugins_dir` / `settings` / `.inventory.metas` / indicator-only
    filtering を spy で観測する**。

    **実入口の形は着手時に現物から取ること** (`mission_worker.py:880-935` 付近、
    `rg -n 'def main|worker_profile|handshake\\[' src/agentic_fx/mission_worker.py`)。
    `main()` は stdin/stdout のフレームプロトコルを話すので**丸ごとは回せない**。
    現行の trade 分岐は `main()` 内のインラインブロックなので、
    **Step 3-3c で `_build_trade_indicator_metas(conn, plugins_dir, settings)`
    という純粋 helper に切り出し**、`main()` からはそれを呼ぶ形にする。
    テストは helper を**実物として**呼び、`approved_plugins` 側だけを spy する
    (helper 抽出はふるまいを変えない — 切り出し前後で `main()` の
    `approved` 変数の値は同一)。"""
    from agentic_fx import mission_worker
    from agentic_fx.plugin.loader import PluginMeta
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult

    ind = PluginMeta(name="rsi", kind="indicator", path=tmp_path, params={},
                     timeframe=None, pairs=(), max_bars=200,
                     content_hash="a" * 64, outputs=("rsi",))
    strat = PluginMeta(name="s", kind="strategy", path=tmp_path, params={},
                       timeframe="1h", pairs=("USDJPY",), max_bars=200,
                       content_hash="b" * 64)
    seen_args = {}

    def _spy(conn, plugins_dir, *, settings):
        seen_args.update(conn=conn, plugins_dir=plugins_dir, settings=settings)
        return InventoryBuildResult(
            inventory=ApprovedInventory(root=plugins_dir, metas=(ind, strat)),
            phase1_metas=(ind, strat), resolved={}, rejected_strategies=())

    # `mission_worker` は `from agentic_fx.tools import plugin_loader` で
    # **モジュールを** import しているので (着手時に
    # `rg -n 'import plugin_loader' src/agentic_fx/mission_worker.py` で確認)、
    # そのモジュール属性を差し替えれば実入口の呼び出しを捕捉できる。
    monkeypatch.setattr(mission_worker.plugin_loader, "approved_plugins", _spy)

    conn = MagicMock()
    metas = mission_worker._build_trade_indicator_metas(
        conn, tmp_path / "plugins", SETTINGS)

    # 実入口が渡した引数 (別 root / settings 透通)
    assert seen_args["plugins_dir"] == tmp_path / "plugins"
    assert seen_args["settings"] is SETTINGS
    assert seen_args["conn"] is conn
    # `.inventory.metas` から indicator kind だけを取り出している
    assert [m.name for m in metas] == ["rsi"]
    assert all(m.kind == "indicator" for m in metas)


def test_trade_worker_indicator_metas_is_empty_without_a_plugins_dir(tmp_path):
    """`plugins_dir is None` (handshake にキーが無い) なら空リスト。
    現行の `if plugins_dir is not None else []` を helper 側で保つ。"""
    from agentic_fx import mission_worker
    assert mission_worker._build_trade_indicator_metas(
        MagicMock(), None, SETTINGS) == []
```

`tests/tools/test_plugin_loader.py` に追記:

```python
def test_market_tools_ignores_non_indicator_kind_after_two_phase(tmp_path):
    """S1 の周辺: `inventory.metas` をそのまま渡してよい契約は維持される。"""
    strategy = PluginMeta(name="s", kind="strategy", path=tmp_path, params={},
                          timeframe="1h", pairs=("USDJPY",), max_bars=200,
                          content_hash="h" * 64)
    provider = MagicMock()
    provider.get_bars.return_value = _bars()
    tools = market_tools.build(provider, MagicMock(), _SETTINGS,
                               indicator_plugins=[strategy],
                               sandbox_run=lambda *a, **k: {"x": 1.0})
    get_indicators = next(t.func for t in tools if t.name == "get_indicators")
    assert not any(k.startswith("plugin:") for k in get_indicators("USDJPY", "1h"))
```

- [ ] **Step 3-3b: Run test to verify it fails**

Run: `uv run pytest tests/test_mission_worker.py -k trade_worker -q`
Expected: FAIL — `AttributeError: module 'agentic_fx.mission_worker' has no
attribute '_build_trade_indicator_metas'` (codex plan r1 I3: v1.2 の
「docstring が未記載なので落ちる」という Expected は成立していなかった —
テストが src を 1 行も実行していなかったため、実装前でも緑だった)

- [ ] **Step 3-3c: Write minimal implementation**

`src/agentic_fx/mission_worker.py` にモジュール関数を新設する
(**codex plan r1 I3**: `main()` のインラインブロックのままだと実入口を
テストから起動できず、「配線そのもの」が検証できない):

```python
def _build_trade_indicator_metas(conn, plugins_dir, settings) -> list:
    """[indicator-consumption-wiring] §2.3: **これは producer とは別の
    composition root** — 子が handshake の `plugins_dir` から
    `approved_plugins` を再実行する。trade LLM 向けの参考情報
    (`get_indicators`) であり、producer との版ずれは許容する
    (strategy の解決はここでは行わない — 第 2 相は strategy にしか
    効かず、`market_tools` は indicator kind しか使わない)。

    `main()` の trade 分岐から呼ぶだけの薄い helper。**切り出しは
    ふるまいを変えない** — 切り出し前の `approved` 変数と同じ値を返す。
    """
    if plugins_dir is None:
        return []
    return [m for m in plugin_loader.approved_plugins(
                conn, plugins_dir, settings=settings).inventory.metas
            if m.kind == "indicator"]
```

`src/agentic_fx/mission_worker.py:913` を置換:

```python
        approved = _build_trade_indicator_metas(conn, plugins_dir, settings)
```

(`plugin_loader` は現状 `main()` 内のローカル import なので、**モジュール
先頭の import に移す** — helper から見える必要があり、テストが
`mission_worker.plugin_loader` を patch する前提でもある。着手時に
`rg -n 'from agentic_fx.tools import plugin_loader' src/agentic_fx/mission_worker.py`
で現物を確認し、循環 import にならないことを `uv run python -c "import
agentic_fx.mission_worker"` で確かめること。もし循環するなら helper 内で
ローカル import したうえでテストの patch 先を
`agentic_fx.tools.plugin_loader.approved_plugins` に変える。)

`src/agentic_fx/tools/market_tools.py:28-36` の docstring に 1 段落追記:

```python
    """`indicator_plugins` は `plugin_loader.approved_plugins()` の
    `result.inventory.metas` をそのまま渡してよい — `kind != "indicator"` の
    要素はここで無視する (kind="indicator" だけが `get_indicators` の対象)。

    [indicator-consumption-wiring] §2.3: 親 (`service.py`) と子
    (`mission_worker.py`) は**別々の composition root** で inventory を
    構築する。子は handshake の `plugins_dir` から再実行するため、
    live producer と版がずれ得る — LLM 向けの参考情報なので許容する。
    """
```

- [ ] **Step 3-3d: Run test to verify it passes**

Run: `uv run pytest tests/test_mission_worker.py tests/tools -q`
Expected: PASS

- [ ] **Step 3-3e: Commit**

```bash
git add src/agentic_fx/mission_worker.py src/agentic_fx/tools/market_tools.py tests
git commit -m "docs(trade-worker): document the separate indicator inventory root"
```

### Step 3-4: 人間 CLI backtest の解決 (A1' / A1'')

- [ ] **Step 3-4a: Write the failing test**

`tests/backtest/test_cli.py` に追記:

```python
# --- [indicator-consumption-wiring] T3: CLI の依存解決 (A1'/A1'') ----------

from tests.fixtures import indicator_wiring as fx


def _cli_root_with_deployed_rsi_pullback(tmp_path):
    """配備済 (pinned) `rsi_pullback` + 承認済 `rsi` を持つ CLI root を作る。"""
    from agentic_fx.plugin.loader import content_hash
    from agentic_fx.store import approvals
    from agentic_fx.store.db import connect, init_db
    root = tmp_path
    (root / "data").mkdir(parents=True, exist_ok=True)
    plugins = root / "plugins"
    plugins.mkdir(exist_ok=True)
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    fx.seed_history(conn)
    fx.write_indicator(plugins, "rsi")
    hashes = fx.deploy_approved(conn, plugins, ["rsi"], now=fx.NOW)
    strat = fx.write_rsi_pullback(plugins, pins={"rsi": hashes["rsi"]})
    aid = approvals.create(conn, "plugin",
                           {"name": "rsi_pullback", "kind": "strategy",
                            "content_hash": content_hash(strat)}, fx.NOW)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t",
                             now=fx.NOW)
    conn.commit()
    conn.close()
    return root


def test_cli_backtest_run_plugin_with_dependency(tmp_path, capsys, monkeypatch):
    """A1': rc=0、`scope='human_custom'` 行 1 本。承認行を消しても走る
    (手元評価は承認不要という既存契約の維持)。"""
    from agentic_fx.backtest.cli import main
    from agentic_fx.store.db import connect
    root = _cli_root_with_deployed_rsi_pullback(tmp_path)
    monkeypatch.chdir(root)
    rc = main(["backtest", "run", "--plugin", "rsi_pullback",
               "--symbol", "USDJPY", "--source", "dukascopy",
               "--base-interval", "5m",
               "--from", "2025-12-01", "--to", "2026-01-01"])
    assert rc == 0
    conn = connect(root / "data" / "agentic.db")
    rows = conn.execute(
        "SELECT scope, plugin_ref FROM backtest_runs").fetchall()
    assert [(r["scope"], r["plugin_ref"]) for r in rows] == \
        [("human_custom", "plugins/rsi_pullback")]
    conn.execute("DELETE FROM approval_requests "
                 "WHERE json_extract(payload_json,'$.name')='rsi_pullback'")
    conn.commit()
    conn.close()
    assert main(["backtest", "run", "--plugin", "rsi_pullback",
                 "--symbol", "USDJPY", "--source", "dukascopy",
                 "--base-interval", "5m",
                 "--from", "2025-12-01", "--to", "2026-01-01"]) == 0


def _redeploy_rsi_variant(root: Path, mutate) -> None:
    """配備済 `rsi` を削除し、`mutate` を当てた実体で配備し直す。

    `.versions/<name>/<artifact_hash>` の中身を直接書き換えると
    `loader.py:342-353` の artifact_hash 照合で discover ごと落ち、
    reason が常に `not_found` になってしまう (= `not_indicator` /
    `over_max_bars_limit` を観測できない)。よって**正規の配備手順を
    もう一度回す**。`rsi_pullback` の pin は古い `content_hash` のまま
    になるが、resolver は kind / outputs / max_bars を pin より先に
    見るので観測したい reason が先に出る (Step 1-4c の順序)。"""
    import shutil

    from agentic_fx.store.db import connect
    from tests.fixtures import indicator_wiring as fx
    conn = connect(root / "data" / "agentic.db")
    conn.execute("DELETE FROM approval_requests "
                 "WHERE json_extract(payload_json,'$.name')='rsi'")
    conn.commit()
    plugins = root / "plugins"
    link = plugins / "rsi"
    version_dir = link.resolve()
    link.unlink()
    shutil.rmtree(version_dir)
    d = fx.write_indicator(plugins, "rsi")
    cfg = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8"))
    mutate(cfg, d)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    fx.deploy_approved(conn, plugins, ["rsi"], now=fx.NOW)
    conn.close()


@pytest.mark.parametrize("break_it,alias,reason", [
    ("delete_indicator", "rsi", "not_found"),
    ("kind_swap", "rsi", "not_indicator"),
    ("max_bars", "rsi", "over_max_bars_limit"),
])
def test_cli_backtest_run_plugin_unresolved(tmp_path, capsys, monkeypatch,
                                            break_it, alias, reason):
    """A1'': rc=1、stderr が固定 1 行、backtest_runs 行数不変、traceback なし。"""
    import yaml

    from agentic_fx.backtest.cli import main
    from agentic_fx.store.db import connect
    root = _cli_root_with_deployed_rsi_pullback(tmp_path)
    monkeypatch.chdir(root)
    conn = connect(root / "data" / "agentic.db")
    before = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]
    conn.close()
    plugins = root / "plugins"
    if break_it == "delete_indicator":
        conn2 = connect(root / "data" / "agentic.db")
        conn2.execute("DELETE FROM approval_requests "
                      "WHERE json_extract(payload_json,'$.name')='rsi'")
        conn2.commit()
        conn2.close()
    elif break_it == "kind_swap":
        def mutate(cfg, d):
            cfg.pop("outputs")
            cfg.update({"kind": "signal", "timeframe": "1h",
                        "pairs": ["USDJPY"]})
            (d / "plugin.py").write_text(
                "def detect(df, params):\n    return []\n")
        _redeploy_rsi_variant(root, mutate)
    else:
        def mutate(cfg, d):
            cfg["max_bars"] = 100000
        _redeploy_rsi_variant(root, mutate)

    rc = main(["backtest", "run", "--plugin", "rsi_pullback",
               "--symbol", "USDJPY", "--source", "dukascopy",
               "--base-interval", "5m",
               "--from", "2025-12-01", "--to", "2026-01-01"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip().splitlines()[-1] == \
        f"indicator_unresolved:{alias}:{reason}"
    assert "Traceback" not in captured.err
    conn3 = connect(root / "data" / "agentic.db")
    assert conn3.execute(
        "SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == before
    conn3.close()
```

> **注**: `kind_swap` / `max_bars` で `.versions/<name>/<artifact_hash>` の中身を
> 直接書き換えてはならない。`loader.py:342-353` の artifact_hash 照合で discover
> ごと落ち、観測される reason が常に `not_found` になるため (この 2 ケースが
> `delete_indicator` と区別できなくなる)。上の `_redeploy_rsi_variant` は
> **symlink と version ディレクトリと承認行を捨てて正規の配備手順をやり直す**
> ので、変異後の実体が第 1 相を通過し、resolver の kind / max_bars 判定に到達する。
> `kind: signal` へ振り替えるときは `plugin.py` も `detect(df, params)` に
> 書き換えること (`loader._KIND_FUNCS` の AST 照合が引数名まで一致を要求する)。

- [ ] **Step 3-4b: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_cli.py -k "run_plugin_with_dependency or run_plugin_unresolved" -q`
Expected: FAIL — `TypeError: build_intent_source() missing 1 required keyword-only
argument: 'resolved'` (Step 3-1 で必須化したため CLI 経路が落ちる)

- [ ] **Step 3-4c: Write minimal implementation**

`src/agentic_fx/backtest/cli.py:_backtest_run_plugin` の `check_source` の直後、
`_empty_history_guard` の**前**に解決を挟む:

```python
    # [indicator-consumption-wiring] §2.3 の root 表 (人間 CLI):
    # strategy meta は `discover` のまま (承認不要の手元評価という既存契約を
    # 変えない)。依存解決用の inventory だけ `approved_plugins` から作り、
    # `pin_mode="check"` (pin があれば一致を要求、無ければ通す) で解決する。
    # **解決は保存前・try の中** — 失敗は rc=1 / 固定 stderr / 行なし。
    inventory = tools_plugin_loader.approved_plugins(
        conn, plugins_dir, settings=settings)
    try:
        resolved = resolve_indicator_deps(
            meta, inventory.inventory, settings=settings, pin_mode="check")
    except IndicatorResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
```

`build_intent_source(...)` に `resolved=resolved` を渡す。import を追加:

```python
from agentic_fx.plugin.resolve import (
    IndicatorResolutionError, resolve_indicator_deps,
)
from agentic_fx.tools import plugin_loader as tools_plugin_loader
```

> `main()` の外側 `except (ValueError, ...)` は `IndicatorResolutionError` を
> 捕まえない (`Exception` 直下のため) — ここで明示的に catch して固定文言を
> 出すこと。`str(exc)` が既に `indicator_unresolved:<alias>:<reason>` なので
> 加工しない。

- [ ] **Step 3-4d: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_cli.py -q`
Expected: PASS

- [ ] **Step 3-4e: Commit**

```bash
git add src/agentic_fx/backtest/cli.py tests/backtest/test_cli.py
git commit -m "feat(cli): resolve indicator deps before backtest run --plugin (A1'/A1'')"
```

### Step 3-5: `afx plugin lock --from _human <name>`

- [ ] **Step 3-5a: Write the failing test**

`tests/backtest/test_cli.py` に追記:

```python
def test_plugin_lock_writes_pins_and_prints_diff(tmp_path, capsys, monkeypatch):
    """P1 (人間 CLI): pin を書き、content_hash が変わり、書き換え後も
    discover を通る。差分を表示する。"""
    from agentic_fx.backtest.cli import main
    from agentic_fx.plugin.loader import content_hash, discover_one_with_reason
    root = _cli_root_with_deployed_rsi_pullback(tmp_path)
    monkeypatch.chdir(root)
    human = root / "plugins" / "_human"
    fx.write_rsi_pullback(human, pins=None)     # unpinned な候補
    before = content_hash(human / "rsi_pullback")

    rc = main(["plugin", "lock", "--from", "_human", "rsi_pullback"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "rsi" in out and "pin" in out
    meta, reason = discover_one_with_reason(human / "rsi_pullback", "rsi_pullback")
    assert reason is None
    assert meta.indicators[0].pin is not None
    assert content_hash(human / "rsi_pullback") != before
    # 2 回目は no-op (内容が変わらない)
    after_first = (human / "rsi_pullback" / "config.yaml").read_text()
    assert main(["plugin", "lock", "--from", "_human", "rsi_pullback"]) == 0
    assert (human / "rsi_pullback" / "config.yaml").read_text() == after_first


def test_plugin_lock_without_from_is_refused(tmp_path, capsys, monkeypatch):
    from agentic_fx.backtest.cli import main
    root = _cli_root_with_deployed_rsi_pullback(tmp_path)
    monkeypatch.chdir(root)
    assert main(["plugin", "lock", "rsi_pullback"]) == 1
    assert "--from _human" in capsys.readouterr().err


def test_plugin_lock_overwrites_stale_pin(tmp_path, monkeypatch):
    from agentic_fx.backtest.cli import main
    from agentic_fx.plugin.loader import discover_one_with_reason
    root = _cli_root_with_deployed_rsi_pullback(tmp_path)
    monkeypatch.chdir(root)
    human = root / "plugins" / "_human"
    fx.write_rsi_pullback(human, pins={"rsi": "a" * 64})
    assert main(["plugin", "lock", "--from", "_human", "rsi_pullback"]) == 0
    meta, _ = discover_one_with_reason(human / "rsi_pullback", "rsi_pullback")
    assert meta.indicators[0].pin != "a" * 64


def test_plugin_lock_reports_unresolvable_dependency(tmp_path, capsys, monkeypatch):
    from agentic_fx.backtest.cli import main
    root = _cli_root_with_deployed_rsi_pullback(tmp_path)
    monkeypatch.chdir(root)
    human = root / "plugins" / "_human"
    d = fx.write_rsi_pullback(human, pins=None)
    import yaml
    cfg = yaml.safe_load((d / "config.yaml").read_text())
    cfg["indicators"]["rsi"]["plugin"] = "nope"
    (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert main(["plugin", "lock", "--from", "_human", "rsi_pullback"]) == 1
    assert capsys.readouterr().err.strip().splitlines()[-1] == \
        "indicator_unresolved:rsi:not_found"
```

- [ ] **Step 3-5b: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_cli.py -k plugin_lock -q`
Expected: FAIL — `argparse` が `lock` を知らず `SystemExit: 2`

- [ ] **Step 3-5c: Write minimal implementation**

`src/agentic_fx/backtest/cli.py` の parser (`:135` の `materialize_parser` の前) に追加:

```python
    lock_parser = plugin_sub.add_parser(
        "lock", help="候補の indicators 依存を現在の配備版でロックする "
                     "(config.yaml の pin を書き換える)")
    lock_parser.add_argument("name")
    lock_parser.add_argument(
        "--from", dest="from_kind", choices=["_human"], default=None,
        help="'_human' 必須 (submit/bless と同じ --from 規約)")
```

同ファイルに handler を追加 (`_plugin_materialize` の前):

```python
_LOCK_NO_FROM_ERROR = (
    "afx plugin lock は候補領域を明示する必要があります。"
    "'afx plugin lock --from _human <name>' を実行してください "
    "(配備済 plugin は既にロック済みです)。")


def _plugin_lock(conn, settings, args: argparse.Namespace, root: Path) -> int:
    """[indicator-consumption-wiring] §2.1: 人間の明示的なロック操作。
    **pin はハーネスが書く** — 作者が 64 hex を写さない。現在の inventory で
    `pin_mode="ignore"` 解決し (古い pin は上書き)、`lock_config` が
    `config.yaml` を再シリアライズする (U5: コメント・キー順は保持しない
    ので差分を表示する)。書き換え後に `discover` を通ることを同じ関数内で
    確認する (壊れた config を残さない)。

    **snapshot 再取得** (ユーザー裁定 2026-09-14 ⑥): `check_candidate_snapshot`
    (`gate_pytest.py`) はファイル 3 本の存在・属性しか見ず、lock による
    内容変更を検査しない。そのため「lock 後も submit は無条件で通る」と
    決め打ちせず、ここでは `lock_config` が書き込み**後**に返す
    `new_hash` (disk から再計算した content_hash) を唯一の正とし、
    `resolve_indicator_deps` 実行時点で得た `meta.content_hash` を
    使い回さない。`discover_one_with_reason` の再実行結果
    (`relocked.content_hash`) と一致することも assert する
    (どちらも同じ disk 状態を独立に読んでいるので、食い違えば
    `lock_config` かここの配線のバグ)。"""
    if getattr(args, "from_kind", None) != "_human":
        print(_LOCK_NO_FROM_ERROR, file=sys.stderr)
        return 1
    plugins_dir = root / "plugins"
    candidate_dir = plugins_dir / "_human" / args.name
    meta, reason = plugin_loader.discover_one_with_reason(candidate_dir, args.name)
    if meta is None:
        print(f"エラー: 候補 {candidate_dir} を読めません ({reason})",
             file=sys.stderr)
        return 1
    if meta.kind != "strategy":
        print(f"エラー: plugin '{args.name}' の kind は {meta.kind!r} です "
             "(lock は kind=strategy のみ対象)", file=sys.stderr)
        return 1
    inventory = tools_plugin_loader.approved_plugins(
        conn, plugins_dir, settings=settings)
    try:
        resolved = resolve_indicator_deps(
            meta, inventory.inventory, settings=settings, pin_mode="ignore")
    except IndicatorResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    before_text, after_text, new_hash = plugin_resolve.lock_config(
        candidate_dir, resolved.pins())
    relocked, relock_reason = plugin_loader.discover_one_with_reason(
        candidate_dir, args.name)
    if relocked is None:
        print(f"エラー: ロック後の config.yaml が discover を通りません "
             f"({relock_reason}) — 元に戻してください", file=sys.stderr)
        return 1
    assert relocked.content_hash == new_hash, (
        "lock_config の snapshot 再取得値と discover の再取得値が食い違う"
        f" ({new_hash} != {relocked.content_hash})")
    if before_text == after_text:
        print(f"lock: {args.name} は既に最新の pin です (変更なし)")
        return 0
    print(f"lock: {args.name} content_hash {meta.content_hash} -> "
         f"{new_hash}")
    for line in difflib.unified_diff(
            before_text.splitlines(), after_text.splitlines(),
            fromfile="config.yaml (before)", tofile="config.yaml (after)",
            lineterm=""):
        print(line)
    print("ロック後に self-test と backtest を再実行してから submit してください "
         "(テストした artifact == 提出する artifact)")
    return 0
```

dispatch (`:634` 付近) に追加:

```python
                if args.plugin_command == "lock":
                    return _plugin_lock(conn, settings, args, root)
```

import を追加:

```python
import difflib

from agentic_fx.plugin import resolve as plugin_resolve
```

- [ ] **Step 3-5d: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_cli.py -q && uv run pytest -q`
Expected: PASS

- [ ] **Step 3-5e: Commit**

```bash
git add src/agentic_fx/backtest/cli.py tests/backtest/test_cli.py
git commit -m "feat(cli): afx plugin lock --from _human writes dependency pins (P1)"
```

**T3 完了条件**:
- [ ] **F1**: `build_intent_source` を `resolved` 無しで呼ぶと `TypeError`、worker 0 起動
- [ ] **F2** (設計書 v1.3 §6 で緩和): service 起動で pin 破れ strategy が
      warning 1 行 (reason 込み) になり、producer の plugin 一覧に含まれない
      (session cache 未登録はこの帰結であり別途観測しない、2026-09-14 裁定)。
      producer は `InventoryBuildResult.resolved` の**同一オブジェクト**を
      `(name, content_hash)` の identity を保ったまま session へ渡す
      (`test_producer_uses_name_and_hash_identity_not_hash_alone` — 同一
      content_hash・別名の 2 strategy が互いの `ResolvedIndicatorSet` を
      取り違えない。codex plan r2 束2 Critical)
- [ ] **A1'**: 配備済 (pinned) `rsi_pullback` の `afx backtest run --plugin` が rc=0 /
      `scope='human_custom'` 行 1 本。承認行を消しても rc=0 (手元評価契約の維持)
- [ ] **A1''**: 未解決 3 種 (`not_found` / `not_indicator` / `over_max_bars_limit`) で
      rc=1、stderr 最終行が `indicator_unresolved:<alias>:<reason>` 逐語、
      `backtest_runs` 行数不変、traceback なし
- [ ] **P1 (人間 CLI)**: `afx plugin lock --from _human` が pin を書き、`content_hash` が
      変わり、再 discover を通り、差分を表示する。同じ pin は no-op、古い pin は上書き、
      `--from` なしは固定文言で拒否
- [ ] adapter の `decision_sink` が本番経路で `None` (既定) のまま
- [ ] 段 0 変異 red: (a) `build_intent_source` の `resolved` に既定値
      `ResolvedIndicatorSet.empty(Path('.'))` を与える → F1 のテストが落ちる
      (b) CLI の解決を `_empty_history_guard` の**後**へ移す →
      `test_cli_backtest_run_plugin_unresolved` の「行数不変」は通るが、
      解決を `save_human_run` の後へ移すと落ちる (順序 pin)
      (c) producer の `(meta.name, meta.content_hash) not in resolved_by_identity`
      判定を削る → `test_producer_skips_strategy_without_resolution` が落ちる
      (d) `_call` の `resolved_by_identity.get((meta.name, meta.content_hash))` を
      `resolved_by_identity.get(meta.content_hash)` (name を落とす) に戻す →
      `test_producer_uses_name_and_hash_identity_not_hash_alone` が落ちる

---

## T4a: gate の判別子と人間回廊の非送出 + A1 E2E [approval-corridor-1]

> **opus r1 観点 7 で旧 T4 (8 Step / 約 1,700 行) を T4a (Step 4-1〜4-3) と
> T4b (Step 4-4〜4-8) に分割した。** 境界は「gate の判別子が確定したところ」。

**対応**: 設計書 §2.8 (判別子)、§2.3 の `_run_full_gate` 行、§4 の `approval.py` /
`strategy_gate.py` / `switch.py` (`_run_full_gate` のみ) 行。
**完了条件の受入 ID**: F3 / F3' / A1 / A1-b / U4a / U4b (承認経路) / C1 (verdict 経路)。

**着手条件**: T1・T2・T6a・T3・**T6b** が main にマージ済み
(Step 4-2 の F3'/U4a テストが `wiring_envs.switch_env` を使う)。**worktree 並列不可**。

**Files:**
- Modify: `src/agentic_fx/plugin/approval.py:198-315` (`GateOutcome` / `run_kind_gate`)
- Modify: `src/agentic_fx/plugin/strategy_gate.py:28-49` (`StrategyGateVerdict`)、`:257-261`
  (baseline 探索)、`:284-336` (cpu_samples)
- Modify: `src/agentic_fx/plugin/switch.py:779-882` (`_run_full_gate` —
  **`plugins_root` 引数の追加と inventory 構築はこの task が所有する**。
  codex plan r1 C2)、`:902-960` (`submit_candidate`)、`:1560-1600`
  (`bless_candidate`) の 2 箇所は `plugins_root=` を渡すだけ
  (`_plugin_locks` / `approve_candidate` / `reconcile` / `noop_gate` /
  `commands.py` は **T4b** の Files)
- Test: `tests/plugin/test_approval.py`、`tests/plugin/test_switch_paths.py`、
  `tests/plugin/test_switch_floor_modes.py` (`run_kind_gate` 5 caller の移行)、
  `tests/plugin/test_indicator_wiring_e2e.py` (新規)
  (`tests/plugin/test_reconcile.py` / `tests/loops/test_improve_loop_plugin_gate.py` /
  `tests/test_commands.py` は **T4b** の Files — codex plan r1 C2 の整理で
  `noop_gate.py` / `commands.py` を T4a の Files から外したのに伴う)

**Interfaces:**

- Consumes: T1 の `InventoryBuildResult` / `resolve_indicator_deps` /
  `IndicatorResolutionError` / `tools.plugin_loader.approved_plugins(..., settings=)`、
  T3 の `evaluate_strategy_adoption_gate(..., resolved)` /
  `build_intent_source(..., resolved, decision_sink)`。
  (**`_run_full_gate(..., plugins_root)` は T3 の Produces ではない** — T4a の
  Produces に移した。codex plan r1 C2: T3 で引数だけ足しても使い道が無く、
  `run_kind_gate` の新契約と分離すると T3 単独で `TypeError` になる)、
  T6a の `tests.fixtures.indicator_wiring`、
  T6b の `tests.fixtures.wiring_envs`(`switch_env` / `SETTINGS_FIXTURE` のみ)。
  (`same_modulo_pins` / `is_relock_transition` と `wiring_envs` の残りのビルダは
  **T4b** の Consumes — opus r1 観点 7)
- Produces:

```python
# src/agentic_fx/plugin/approval.py
@dataclass(frozen=True)
class GateOutcome:
    metrics: dict
    evaluable: bool
    verdict_kind: Literal["ok", "insufficient_trades", "floor",
                          "indicator_unresolved"] = "ok"
    floor_warning: str = ""
    floor_detail: str = ""
    insufficient_trades_reason: str = ""
    gate_rows: "tuple[dict, ...] | list[dict]" = field(default_factory=tuple)
    # [indicator-consumption-wiring] §2.8
    indicator_alias: str | None = None
    indicator_reason: str = ""
    resolved: "ResolvedIndicatorSet | None" = None
    cpu_samples: tuple[tuple[str, str, float | None], ...] = ()

def run_kind_gate(conn, meta, *, settings, now, inventory: "InventoryBuildResult",
                  sandbox_run=None, run_in_sample_fn=None,
                  floor_mode="enforce", record_fn=None, sink=None,
                  pin_mode: "PinMode" = "require") -> GateOutcome: ...

# src/agentic_fx/plugin/strategy_gate.py
@dataclass(frozen=True)
class StrategyGateVerdict:
    evaluable: bool
    observation_reason: str = ""
    baseline_variant: str = "no_strategy"
    baseline_row: dict | None = None
    candidate_metrics: dict | None = None
    floor_reason: str = ""
    floor_detail: str = ""
    holdout_metrics: dict | None = None
    cpu_samples: tuple[tuple[str, str, float | None], ...] = ()
    # (scope, pair, cpu_sec|None)  scope in {"in_sample", "holdout"}

# src/agentic_fx/plugin/switch.py
def _run_full_gate(conn, candidate_dir, *, name, settings, now, floor_mode,
                   plugins_root: Path, activity=None
                   ) -> tuple[PluginMeta, str, str, GateOutcome]: ...
    # [codex plan r1 C2] `plugins_root` の**追加と用途 (inventory 構築) は
    # この task が同時に持つ** — 4 要素 tuple は維持 (Global Constraints)。
    # `submit_candidate` / `bless_candidate` は既に `plugins_root` を
    # ローカル変数で持っているので、渡すだけ (Step 4-2c)。

# (`_plugin_locks` / `find_noop_copy` / `Commands._dependent_strategies` は
#  **T4b の Produces** — codex plan r1 C2 の整理で重複を解消した)
```

> **T3 との境界 (codex plan r1 C2)**: T3 は `run_kind_gate` の**本体**だけを直し、
> `evaluate_strategy_adoption_gate` へ `ResolvedIndicatorSet.empty(meta.path.parent)`
> を渡す暫定になっている。本 task の Step 4-1c が**その 1 行を実解決に置き換える**
> (シグネチャの `inventory` / `pin_mode` 追加も同じ Step)。`git diff` の削除行に
> `ResolvedIndicatorSet.empty(meta.path.parent)` が**含まれていること**を検収で確認する
> ([[spec-must-check-existing-guards]])。

### Step 4-1: `GateOutcome` の `indicator_unresolved` 判別子と `run_kind_gate`

- [ ] **Step 4-1a: Write the failing test**

> **先に読む — `run_kind_gate` の既存呼び出しの全数移行表 (opus r1 C6)**
>
> `inventory` を**キーワード必須**にすると、既存の 5 呼び出しが全部
> `TypeError` になる。プラン v1 のどの Step にも移行指示が無く、Step 4-1d の
> `uv run pytest tests/plugin -q` は必ず赤になっていた。T1 Step 1-7 /
> T3 Step 3-1a と同じ流儀で **`rg -n 'run_kind_gate\(' src tests` の全結果を
> 貼る** (行番号は v1 執筆時点。着手時に再取得すること):
>
> | path:line | 現行の呼び出し | 移行後 |
> |---|---|---|
> | `src/agentic_fx/plugin/approval.py:229` | 定義 | 新シグネチャ (本 Step 4-1c) |
> | `src/agentic_fx/plugin/switch.py:833` | `_run_full_gate` 内 (手順 7) | Step 4-2c で `inventory=` を渡す |
> | `tests/plugin/test_switch_floor_modes.py:127` | `run_kind_gate(conn, meta, settings=settings, now=NOW)` | `+ inventory=_empty_inventory(plugins_dir)` |
> | `tests/plugin/test_switch_floor_modes.py:144` | `..., floor_mode="enforce")` | 同上 |
> | `tests/plugin/test_switch_floor_modes.py:478` | `..., floor_mode="warn")` | 同上 |
> | `tests/plugin/test_switch_floor_modes.py:508` | `run_kind_gate(conn, meta, settings=settings, now=NOW)` | 同上 |
> | `tests/plugin/test_switch_floor_modes.py:576` | `..., sink=my_sink)` | 同上 |
>
> **移行後の期待値は変わらない**: これら 5 件は候補 `st`
> (`_write_strategy_candidate` = `indicators:` を持たない strategy) を
> `evaluate_strategy_adoption_gate` の monkeypatch で通しているので、
> **resolver は deps 0 本で即 `ResolvedIndicatorSet.empty(inventory.inventory.root)`
> を返す** = 判定に影響しない。`_empty_inventory(...)` は下で定義するヘルパを
> `tests/plugin/test_switch_floor_modes.py` 側にも import する
> (`from tests.plugin.test_approval import _empty_inventory` ではなく、
> `tests/fixtures/wiring_envs.py` にも置かず、**同ファイル内にローカル定義**
> する — T4a は T6b の `wiring_envs` を Consumes するがこのヘルパは
> `wiring_envs` の Produces に無いため)。
>
> **Step 4-1d の Run はこの移行を含めて緑にすること**:
> `uv run pytest tests/plugin -q`

`tests/plugin/test_approval.py` に追記:

```python
# --- [indicator-consumption-wiring] T4: 未解決の非送出 (F3) ---------------

from agentic_fx.plugin.resolve import (
    ApprovedInventory, InventoryBuildResult, ResolvedIndicatorSet,
)
from tests.fixtures import indicator_wiring as fx


def _empty_inventory(root):
    return InventoryBuildResult(
        inventory=ApprovedInventory(root=root, metas=()),
        phase1_metas=(), resolved={}, rejected_strategies=())


def test_run_kind_gate_returns_indicator_unresolved_without_raising(tmp_path):
    """F3: 未解決は例外ではなく判別子で返る。行数は不変。"""
    conn = _conn(tmp_path)
    root = tmp_path / "plugins"
    root.mkdir()
    cand = fx.write_rsi_pullback(tmp_path / "cand", pins=None)
    meta, reason = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")
    assert reason is None
    before = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0]

    outcome = approval.run_kind_gate(
        conn, meta, settings=SETTINGS, now=fx.NOW,
        inventory=_empty_inventory(root))

    assert outcome.verdict_kind == "indicator_unresolved"
    assert outcome.indicator_alias == "rsi"
    assert outcome.indicator_reason == "not_found"
    assert outcome.resolved is None
    assert outcome.gate_rows == [] or tuple(outcome.gate_rows) == ()
    assert conn.execute(
        "SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_run_kind_gate_unpinned_is_indicator_unresolved_under_require(tmp_path):
    """F3': require では unpinned が reason='unpinned'。"""
    conn = _conn(tmp_path)
    root = tmp_path / "plugins"
    fx.write_indicator(root, "rsi")
    inv_meta, _ = plugin_loader.discover_one_with_reason(root / "rsi", "rsi")
    inventory = InventoryBuildResult(
        inventory=ApprovedInventory(root=root.resolve(), metas=(inv_meta,)),
        phase1_metas=(inv_meta,), resolved={}, rejected_strategies=())
    cand = fx.write_rsi_pullback(tmp_path / "cand", pins=None)
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")

    outcome = approval.run_kind_gate(conn, meta, settings=SETTINGS, now=fx.NOW,
                                     inventory=inventory)

    assert outcome.verdict_kind == "indicator_unresolved"
    assert (outcome.indicator_alias, outcome.indicator_reason) == ("rsi", "unpinned")


def test_run_kind_gate_resolves_once_and_carries_the_same_object(tmp_path,
                                                                 monkeypatch):
    """P3': resolver 呼び出しは候補ごとに 1 回。GateOutcome.resolved と
    in_sample/holdout session が同一オブジェクト。"""
    conn = _conn(tmp_path)
    root = tmp_path / "plugins"
    fx.write_indicator(root, "rsi")
    inv_meta, _ = plugin_loader.discover_one_with_reason(root / "rsi", "rsi")
    from agentic_fx.plugin.loader import content_hash
    inventory = InventoryBuildResult(
        inventory=ApprovedInventory(root=root.resolve(), metas=(inv_meta,)),
        phase1_metas=(inv_meta,), resolved={}, rejected_strategies=())
    cand = fx.write_rsi_pullback(tmp_path / "cand",
                                 pins={"rsi": content_hash(root / "rsi")})
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")

    calls = []
    real = approval.resolve_indicator_deps

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("pin_mode"))
        return real(*args, **kwargs)

    monkeypatch.setattr(approval, "resolve_indicator_deps", _spy)
    seen = []
    monkeypatch.setattr(
        approval.strategy_gate, "evaluate_strategy_adoption_gate",
        lambda conn_, **kw: seen.append(kw["resolved"]) or
        approval.strategy_gate.StrategyGateVerdict(
            evaluable=True, candidate_metrics={"USDJPY": {"trades": 40}}))

    outcome = approval.run_kind_gate(conn, meta, settings=SETTINGS, now=fx.NOW,
                                     inventory=inventory)

    assert calls == ["require"]
    assert outcome.resolved is seen[0]
```

- [ ] **Step 4-1b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_approval.py -k "indicator_unresolved or resolves_once" -q`
Expected: FAIL — `TypeError: run_kind_gate() got an unexpected keyword argument 'inventory'`

- [ ] **Step 4-1c: Write minimal implementation**

`src/agentic_fx/plugin/approval.py`:

```python
from agentic_fx.plugin.resolve import (
    IndicatorResolutionError, PinMode, ResolvedIndicatorSet,
    resolve_indicator_deps,
)
```

`GateOutcome` に 4 フィールドを追加 (末尾、既定値付き):

```python
    # [indicator-consumption-wiring] §2.8 (codex r6 I1): 未解決は
    # 判別子で返す (raise しない)。成功時は `resolved` に解決結果を載せ、
    # submit / bless / `_build_approval_payload` はここから
    # `pin_object()` を作る (外で再解決しない)。
    indicator_alias: str | None = None
    indicator_reason: str = ""
    resolved: "ResolvedIndicatorSet | None" = None
    # (scope, pair, cpu_sec|None) — commit gate は adapter を内部生成する
    # ため、CPU 実測は verdict 経由でしか ImproveLoop に届かない (r5 I4)。
    cpu_samples: tuple[tuple[str, str, float | None], ...] = ()
```

`verdict_kind` の Literal に `"indicator_unresolved"` を追加。

`run_kind_gate` を置換 (strategy 分岐の冒頭に解決を置く):

```python
def run_kind_gate(conn: sqlite3.Connection, meta: PluginMeta, *,
                  settings: "Settings", now: datetime,
                  inventory: "InventoryBuildResult",
                  sandbox_run: SandboxRunFn | None = None,
                  run_in_sample_fn: RunInSampleFn | None = None,
                  floor_mode: Literal["enforce", "warn"] = "enforce",
                  record_fn: "Callable[[dict], None] | None" = None,
                  sink: "list[dict] | None" = None,
                  pin_mode: PinMode = "require",
                  ) -> GateOutcome:
    """... (既存 docstring はそのまま) ...

    [indicator-consumption-wiring] §2.8: **解決エラーは捕捉して
    `GateOutcome(verdict_kind="indicator_unresolved")` を返す** (raise
    しない — Global Constraints)。成功時は `resolved` に解決結果を載せ、
    `evaluate_strategy_adoption_gate` へ**同じオブジェクト**を渡す
    (resolver 呼び出しは候補ごとに 1 回、P3')。`inventory` は呼び出し元が
    composition root で 1 回だけ構築したもの — この関数は構築しない。
    """
    if meta.kind == "strategy":
        rows: list[dict] = sink if sink is not None else []
        try:
            resolved = resolve_indicator_deps(
                meta, inventory.inventory, settings=settings, pin_mode=pin_mode)
        except IndicatorResolutionError as exc:
            return GateOutcome(
                metrics={}, evaluable=False,
                verdict_kind="indicator_unresolved",
                indicator_alias=exc.alias, indicator_reason=exc.reason,
                gate_rows=rows)

        def _sink(row: dict) -> None:
            rows.append(row)
            if record_fn is not None:
                record_fn(row)

        verdict = strategy_gate.evaluate_strategy_adoption_gate(
            conn, meta=meta, settings=settings, now=now,
            floor_mode=floor_mode, record_fn=_sink, resolved=resolved,
            inventory=inventory)
        cpu_samples = verdict.cpu_samples if verdict is not None else ()
        if verdict is None or not verdict.evaluable:
            reason = verdict.observation_reason if verdict is not None else ""
            return GateOutcome(
                metrics={}, evaluable=False,
                verdict_kind="insufficient_trades",
                insufficient_trades_reason=reason, gate_rows=rows,
                resolved=resolved, cpu_samples=cpu_samples)
        if verdict.floor_reason:
            return GateOutcome(
                metrics=verdict.candidate_metrics or {}, evaluable=True,
                verdict_kind="floor", floor_warning=verdict.floor_reason,
                floor_detail=verdict.floor_detail, gate_rows=rows,
                resolved=resolved, cpu_samples=cpu_samples)
        return GateOutcome(
            metrics=verdict.candidate_metrics or {}, evaluable=verdict.evaluable,
            verdict_kind="ok", gate_rows=rows, resolved=resolved,
            cpu_samples=cpu_samples)
    metrics, evaluable = _validate_kind(
        conn, meta, settings=settings, now=now, sandbox_run=sandbox_run,
        run_in_sample_fn=run_in_sample_fn,
        resolved=ResolvedIndicatorSet.empty(inventory.inventory.root))
    return GateOutcome(metrics=metrics, evaluable=evaluable, verdict_kind="ok",
                       gate_rows=sink if sink is not None else [])
```

`strategy_gate.evaluate_strategy_adoption_gate` に `cpu_samples` の収集を足す
(in-sample / holdout の各ループの `finally` の後で `intent_source.cpu_sec` を読む):

```python
    cpu_samples: list[tuple[str, str, float | None]] = []
    # (既存の baseline 判定 / dataset 取得はそのまま)
    for pair in pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, dataset=dataset, settings=settings,
            resolved=resolved)
        try:
            per_pair[pair] = run_in_sample(          # 現行の引数のまま
                settings, history_conn=history_conn, symbol=pair,
                dataset=dataset, intent_source=intent_source,
                eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                content_hash=content_hash, kind="strategy", now=now,
                record_fn=_in_sample_record_fn)
        finally:
            intent_source.close()
            # [indicator-consumption-wiring] §2.9(e): close 完了後に確定する
            # property。例外終了時も必ず 1 件積む (cpu_sec=None)。
            cpu_samples.append(("in_sample", pair, intent_source.cpu_sec))
```

holdout ループも同形 (`scope="holdout"`)。`StrategyGateVerdict(...)` を返す
**3 箇所すべて**に `cpu_samples=tuple(cpu_samples)` を渡す
(`:302-304` の `insufficient_trades` 早期 return、`:316-319` の in_sample
フロア早期 return、holdout 後の通常 return)。1 つでも落とすと
Step 4-3 の `test_cpu_samples_reach_the_verdict` が `cpu_samples == ()` で落ちる。

baseline 探索 (`:257-261`) を inventory 正本へ置換:

```python
    # [indicator-consumption-wiring] §2.7: baseline 判定は
    # `ApprovedInventory` の strategy 集合を正本にする — approval_requests
    # を直接引くと、pin 破れで配備から外れた同名 strategy を baseline と
    # 誤認する (それは既に live で動いていない)。`inventory` が渡されない
    # 経路 (テストの直接呼び出し) は従来の SQL にフォールバックする。
    if inventory is not None:
        has_approved_baseline = any(
            m.name == name and m.kind == "strategy"
            for m in inventory.inventory.metas)
    else:
        has_approved_baseline = conn.execute(
            "SELECT 1 FROM approval_requests WHERE kind='plugin' "
            "AND status='approved' "
            "AND json_extract(payload_json,'$.name')=? "
            "AND json_extract(payload_json,'$.kind')='strategy' "
            "LIMIT 1", (name,)).fetchone() is not None
```

以降の `approved_row is None` 判定は `not has_approved_baseline` に置換する。

**`strategy_gate` 側の申し送り 2 件 (opus r1 M9 で旧 Step 4-3c から移動)** —
本 Step で必ず一緒に入れること:

1. `StrategyGateVerdict` を返す**すべての return** (`insufficient_trades` /
   in_sample フロア / holdout 後の通常 return。着手時に
   `rg -n 'StrategyGateVerdict(' src/agentic_fx/plugin/strategy_gate.py` で
   全数を再取得する) に `cpu_samples=tuple(cpu_samples)` を渡す。早期 return を
   1 つでも落とすと `test_cpu_samples_reach_the_verdict` が `cpu_samples == ()`
   で落ちる。
2. `cpu_samples.append((scope, pair, intent_source.cpu_sec))` は
   `finally: intent_source.close()` の**後**に置く (`cpu_sec` は graceful
   close 後に確定する property)。in_sample ループと holdout ループの
   **両方**に置く。

- [ ] **Step 4-1d: Run test to verify it passes**

Run: `uv run pytest tests/plugin -q`
Expected: PASS (**Step 4-1a の移行表の 5 件 (`test_switch_floor_modes.py`) を
移行済みであること** — opus r1 C6)

- [ ] **Step 4-1e: Commit**

```bash
git add src/agentic_fx/plugin/approval.py src/agentic_fx/plugin/strategy_gate.py \
        tests/plugin/test_approval.py
git commit -m "feat(gate): indicator_unresolved verdict + carried resolved set (F3/F3'/P3')"
```

### Step 4-2: `_run_full_gate` の固定 ValueError と `outputs_required`

- [ ] **Step 4-2a: Write the failing test**

`tests/plugin/test_switch_paths.py` に追記:

```python
# --- [indicator-consumption-wiring] T4: 人間回廊の非送出 (F3'/U4a) --------

from tests.fixtures import indicator_wiring as fx


def test_submit_candidate_rejects_unpinned_with_fixed_text(tmp_path):
    """F3': unpinned → ValueError('indicator_unresolved:rsi:unpinned')、
    approval 行 0 / gate 行 0。"""
    conn, plugins_root = _switch_env(tmp_path)     # tests.fixtures.wiring_envs (T6b)
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins=None)
    with pytest.raises(ValueError) as ei:
        plugin_switch.submit_candidate(
            conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
            candidate_origin="human", mission_id=None, backlog_id=None,
            settings=SETTINGS, now=fx.NOW)
    assert str(ei.value) == "indicator_unresolved:rsi:unpinned"
    assert conn.execute("SELECT COUNT(*) FROM approval_requests "
                        "WHERE json_extract(payload_json,'$.name')="
                        "'rsi_pullback'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_submit_candidate_rejects_indicator_without_outputs(tmp_path):
    """U4a: kind=indicator で outputs 無し → ValueError('outputs_required')、
    approval 行 0。"""
    conn, plugins_root = _switch_env(tmp_path)
    d = plugins_root / "_human" / "legacy_ind"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(
        "def compute(df, params):\n    return {'rsi_14': 1.0}\n")
    (d / "config.yaml").write_text("kind: indicator\nparams:\n  period: 14\n")
    (d / "test_plugin.py").write_text("def test_x():\n    pass\n")
    with pytest.raises(ValueError) as ei:
        plugin_switch.submit_candidate(
            conn, name="legacy_ind", staging_dir=plugins_root / "_human",
            candidate_origin="human", mission_id=None, backlog_id=None,
            settings=SETTINGS, now=fx.NOW)
    assert str(ei.value) == "outputs_required"
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_bless_candidate_rejects_indicator_without_outputs(tmp_path):
    conn, plugins_root = _switch_env(tmp_path)
    d = plugins_root / "_human" / "legacy_ind"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(
        "def compute(df, params):\n    return {'rsi_14': 1.0}\n")
    (d / "config.yaml").write_text("kind: indicator\nparams:\n  period: 14\n")
    (d / "test_plugin.py").write_text("def test_x():\n    pass\n")
    with pytest.raises(ValueError, match="^outputs_required$"):
        plugin_switch.bless_candidate(
            conn, name="legacy_ind", human_dir=d, settings=SETTINGS,
            now=fx.NOW, decided_by="human_cli")
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_indicator_with_outputs_passes_the_gate(tmp_path):
    """U4a の裏: outputs を宣言した indicator は従来どおり通る。"""
    conn, plugins_root = _switch_env(tmp_path)
    fx.write_indicator(plugins_root / "_human", "rsi")
    approval_id = plugin_switch.submit_candidate(
        conn, name="rsi", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS, now=fx.NOW)
    assert approval_id > 0
```

- [ ] **Step 4-2b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_switch_paths.py -k "unpinned or outputs_required or outputs_passes" -q`
Expected: FAIL — `outputs_required` は誰も送出せず、unpinned は `TypeError`
(`run_kind_gate` の `inventory` 未配線) になる

- [ ] **Step 4-2c: Write minimal implementation**

`src/agentic_fx/plugin/switch.py::_run_full_gate` を置換
(`_discover_one` の直後に U4a、`assert_max_bars_within_limit` の後に解決):

```python
    meta = loader._discover_one(candidate_dir, name)
    if meta is None:
        raise ValueError(
            f"plugin {name!r}: candidate at {candidate_dir} failed discovery "
            "validation (config/AST ゲート不合格 — ログ参照)")

    # [indicator-consumption-wiring] U4 (2026-09-14): 新規承認では
    # kind=indicator の `outputs` 宣言を必須にする。固定文言のみ
    # (候補名も理由も足さない — Global Constraints の語彙一覧)。
    # 位置は `assert_max_bars_within_limit` と同じ「ゲート本体の手前」。
    if meta.kind == "indicator" and meta.outputs is None:
        raise ValueError("outputs_required")

    rows: list[dict] = []
    try:
        ...  # check_source / pytest / hash 再検査 は現行のまま
        approval.assert_max_bars_within_limit(meta, settings=settings)

        # [indicator-consumption-wiring] §2.3: 承認回廊の inventory は
        # **ここで 1 回だけ**構築する (submit / bless の両方が通る唯一の
        # 場所)。`run_kind_gate` は `pin_mode="require"` で解決し、
        # 失敗は判別子で返る (raise しない)。
        inventory = tools_plugin_loader.approved_plugins(
            conn, plugins_root, settings=settings)
        outcome = approval.run_kind_gate(
            conn, meta, settings=settings, now=now, floor_mode=floor_mode,
            sink=rows, inventory=inventory, pin_mode="require")
        assert outcome.gate_rows is rows
    except SandboxError as exc:
        ...  # 現行のまま
    except BaseException:
        ...  # 現行のまま

    # [indicator-consumption-wiring] §2.8: 判別子から固定 ValueError。
    # 例外メッセージの部分一致による分類は禁止 — ここも判別子だけを見る。
    if outcome.verdict_kind == "indicator_unresolved":
        _persist_human_gate_rows(conn, rows, mission_outcome="gate_failed", now=now)
        raise ValueError(
            f"indicator_unresolved:{outcome.indicator_alias or '-'}:"
            f"{outcome.indicator_reason}")
    if outcome.verdict_kind == "insufficient_trades":
        ...  # 現行のまま
```

**`_run_full_gate` に `plugins_root: Path` をキーワード必須で足すのも本 Step**
(codex plan r1 C2 — v1.2 は「Step 3-1 で足した引数」と書いていたが、T3 は
`switch.py` を触らなくなった)。`submit_candidate` / `bless_candidate` は
`_run_full_gate(..., plugins_root=plugins_root)` を渡す (両者とも冒頭で
`plugins_root` をローカル変数に持っている — `submit_candidate` は
`_candidate_locator(...)` の戻り、`bless_candidate` は `human_dir.parent.parent`。
着手時に `rg -n '_run_full_gate\(' src tests` で全呼び出しを再取得すること)。

`switch.py` に import を追加:

```python
from agentic_fx.tools import plugin_loader as tools_plugin_loader
```

- [ ] **Step 4-2d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_switch_paths.py tests/plugin -q`
Expected: PASS

- [ ] **Step 4-2e: Commit**

```bash
git add src/agentic_fx/plugin/switch.py tests/plugin/test_switch_paths.py
git commit -m "feat(switch): fixed-text refusal for unresolved deps and missing outputs (F3'/U4a)"
```

### Step 4-3: A1 / A1-b の E2E (gate 完走 + decision sink 照合)

- [ ] **Step 4-3a: Write the failing test**

`tests/plugin/test_indicator_wiring_e2e.py` (新規、**実 sqlite + 実 worker**):

```python
"""[indicator-consumption-wiring] T4: 依存 1 本の strategy が採用ゲートを
完走する E2E (A1 / A1-b / C1 / N1 / P3')。

**モックは使わない** — 実 sqlite (tmp_path) と実 worker サブプロセスで回す
([[test-fixtures-from-real-transcripts]]: モックが潰した次元は変異では取れない)。
実 DB・実 `plugins/` には触れない。
"""
from __future__ import annotations

import pytest

from agentic_fx.backtest import holdout
from agentic_fx.plugin import approval, strategy_adapter, strategy_gate
from agentic_fx.plugin.loader import content_hash, discover_one_with_reason
from agentic_fx.plugin.resolve import (
    ApprovedInventory, InventoryBuildResult, resolve_indicator_deps,
)
from tests.backtest.factories import SETTINGS, _conn
from tests.fixtures import indicator_wiring as fx

pytestmark = pytest.mark.slow


@pytest.fixture
def wired(tmp_path):
    """`rsi` 配備済 + `rsi_pullback` (pinned、staging 相当) + 履歴。"""
    conn = _conn(tmp_path)
    fx.seed_history(conn)
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    cand = fx.write_rsi_pullback(tmp_path / "cand", pins={"rsi": hashes["rsi"]})
    meta, reason = discover_one_with_reason(cand, "rsi_pullback")
    assert reason is None, reason
    ind_meta, _ = discover_one_with_reason((plugins_root / "rsi").resolve(), "rsi")
    inventory = InventoryBuildResult(
        inventory=ApprovedInventory(root=plugins_root.resolve(),
                                    metas=(ind_meta,)),
        phase1_metas=(ind_meta,), resolved={}, rejected_strategies=())
    settings = SETTINGS.model_copy(deep=True)
    settings.backtest.holdout_months = fx.HOLDOUT_MONTHS
    settings.backtest.eval_source = fx.SOURCE
    settings.backtest.base_interval = fx.BASE_INTERVAL
    return conn, plugins_root, meta, inventory, settings


def test_a1_gate_completes_with_candidate_and_no_strategy_rows(wired):
    """A1: `evaluate_strategy_adoption_gate` 経路 (run_kind_gate) を完走し、
    candidate の in_sample 行 1 本と no_strategy 行 1 本が残る。"""
    conn, _root, meta, inventory, settings = wired
    # `floor_mode="warn"`: A1 は「配線が通って行が残るか」の受入であって
    # 収益性フロアの受入ではない。`enforce` だと in_sample フロア不合格で
    # `strategy_gate.py:316-319` が holdout 前に早期 return し、
    # `no_strategy` 行 (`:353-365`) が書かれずこのテストが偽陰性になる。
    outcome = approval.run_kind_gate(conn, meta, settings=settings, now=fx.NOW,
                                     inventory=inventory, floor_mode="warn")
    assert outcome.verdict_kind in ("ok", "floor"), outcome.verdict_kind
    for row in outcome.gate_rows:
        from agentic_fx.store import backtest_runs as store
        store.save_harness_run(conn, **row)
    rows = conn.execute(
        "SELECT scope, variant, plugin_ref, content_hash FROM backtest_runs "
        "WHERE scope='in_sample' ORDER BY id").fetchall()
    assert [(r["variant"], r["plugin_ref"]) for r in rows] == [
        ("candidate", "plugins/rsi_pullback"),
        ("no_strategy", "no_strategy:rsi_pullback")]
    assert rows[0]["content_hash"] == content_hash(meta.path)


def test_a1b_run_in_sample_direct_writes_only_the_candidate_row(wired):
    """A1-b: `holdout.run_in_sample` を直接叩くと candidate 行のみ。"""
    conn, root, meta, inventory, settings = wired
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=settings, pin_mode="require")
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair=fx.PAIR, dataset=settings.backtest.dataset(),
        settings=settings, resolved=resolved)
    try:
        metrics = holdout.run_in_sample(
            settings, history_conn=conn, symbol=fx.PAIR,
            dataset=settings.backtest.dataset(), intent_source=src,
            eval_timeframe=fx.EVAL_TIMEFRAME,
            plugin_ref=f"plugins/{meta.name}", content_hash=meta.content_hash,
            kind="strategy", now=fx.NOW)
    finally:
        src.close()
    assert metrics["trades"] >= 0
    rows = conn.execute("SELECT variant FROM backtest_runs").fetchall()
    assert [r["variant"] for r in rows] == ["candidate"]
    # C1: 実 worker を graceful close したので cpu_sec は float
    assert isinstance(src.cpu_sec, float)


def test_decision_sink_matches_the_independent_oracle(wired):
    """A1 の核: 全評価時点の (action, direction) と SL/TP が独立参照実装と
    一致する。plugin コードは oracle 側で一切 import しない。"""
    conn, root, meta, inventory, settings = wired
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=settings, pin_mode="require")
    recorded: list = []
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair=fx.PAIR, dataset=settings.backtest.dataset(),
        settings=settings, resolved=resolved,
        decision_sink=lambda ts, d: recorded.append((ts, d)))
    try:
        holdout.run_in_sample(
            settings, history_conn=conn, symbol=fx.PAIR,
            dataset=settings.backtest.dataset(), intent_source=src,
            eval_timeframe=fx.EVAL_TIMEFRAME,
            plugin_ref=f"plugins/{meta.name}", content_hash=meta.content_hash,
            kind="strategy", now=fx.NOW)
    finally:
        src.close()
    fx.assert_decisions_match(recorded, conn)


def test_cpu_samples_reach_the_verdict(wired):
    """C1 (verdict 経路): in_sample / holdout × pair ごとに 1 件ずつ。

    **codex plan r1 I7 是正**: v1.2 は in_sample の 1 件しか assert して
    おらず、**holdout が 0 件でも緑**だった (holdout ループが
    `cpu_samples.append` を忘れる変異を検出できない)。さらに既定の
    `floor_mode="enforce"` では in_sample がフロア不合格になった時点で
    holdout に進まず早期 return しうるので、`floor_mode="warn"` で
    holdout まで必ず走らせる。**scope 列が厳密に
    `["in_sample", "holdout"]` (この順、各 1 件)** であることを pin する。"""
    conn, root, meta, inventory, settings = wired
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=settings, pin_mode="require")
    verdict = strategy_gate.evaluate_strategy_adoption_gate(
        conn, meta=meta, now=fx.NOW, settings=settings, resolved=resolved,
        inventory=inventory, record_fn=lambda row: None,
        floor_mode="warn")
    scopes = [s for s, _pair, _cpu in verdict.cpu_samples]
    assert scopes == ["in_sample", "holdout"]        # pair は 1 本 (USDJPY)
    assert all(pair == fx.PAIR for _s, pair, _c in verdict.cpu_samples)
    # C1: 正常終了 / plugin error 後はいずれも float。ここは正常終了経路
    # なので **float であること**まで踏む (`None` 許容にしない — 設計書
    # v1.4 §6 C1、`None` は SIGKILL fallback / worker 未起動の 2 経路のみ)。
    assert all(isinstance(cpu, float) for _s, _p, cpu in verdict.cpu_samples)


def test_resolved_object_identity_across_both_scopes(wired, monkeypatch):
    """P3' (`is` 同一性の全数、codex plan r1 I6): resolver は候補ごとに
    **1 回**、in_sample session / holdout session に渡った `resolved` と
    `GateOutcome.resolved` が**すべて同一オブジェクト**。

    T4a Step 4-1a の unit 版 (`test_run_kind_gate_resolves_once_and_carries_
    the_same_object`) は `evaluate_strategy_adoption_gate` を丸ごと double に
    するため **gate へ 1 回渡った object しか見えない**。ここは実 gate を
    走らせ、`strategy_gate` が scope ごとに呼ぶ `build_intent_source` を
    wrap して両 scope 分の `resolved` を集める。"""
    conn, root, meta, inventory, settings = wired

    calls: list = []
    real_resolve = approval.resolve_indicator_deps

    def _resolve_spy(*a, **k):
        calls.append(k.get("pin_mode"))
        return real_resolve(*a, **k)

    monkeypatch.setattr(approval, "resolve_indicator_deps", _resolve_spy)

    seen_resolved: list = []
    real_build = strategy_gate.strategy_adapter.build_intent_source

    def _build_spy(meta_arg, **kw):
        seen_resolved.append(kw["resolved"])
        return real_build(meta_arg, **kw)

    monkeypatch.setattr(strategy_gate.strategy_adapter,
                        "build_intent_source", _build_spy)

    outcome = approval.run_kind_gate(
        conn, meta, settings=settings, now=fx.NOW, inventory=inventory,
        floor_mode="warn")

    assert calls == ["require"]                 # 候補ごとに 1 回
    # pair 1 本 × (in_sample, holdout) = 2 回。両方とも同じ object。
    assert len(seen_resolved) == 2
    assert seen_resolved[0] is outcome.resolved
    assert seen_resolved[1] is outcome.resolved
    # payload の `indicator_deps` を作る元も同じ object (T4b Step 4-5c で
    # `outcome.resolved.pin_object()` を使う契約 — ここではその値が
    # 一致することまでを固定する)
    assert outcome.resolved.pin_object() == {
        "rsi": {"plugin": "rsi", "content_hash": inventory.inventory.metas[0]
                .content_hash, "params": {"period": fx.RSI_PERIOD}}}
```

- [ ] **Step 4-3b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_indicator_wiring_e2e.py -q`
Expected: FAIL — `StrategyGateVerdict` に `cpu_samples` が無い / gate に
`resolved` / `inventory` を渡す経路が未完成

> **opus r1 M9: 旧 Step 4-3c は削除した。** 「Write minimal implementation」と
> いう見出しの下に実装が無く、内容は Step 4-1c への申し送り 2 件だけだった
> (変更履歴には「4-1c へ移動」と書かれていたが本文は移動していなかった)。
> 申し送りは **Step 4-1c の末尾へ実際に移した**。本 Step (4-3) は
> **a / b / d / e の 4 段**で、実装は持たず E2E の検証のみ。

- [ ] **Step 4-3d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_indicator_wiring_e2e.py -q`
Expected: PASS

> このテストは 1 候補あたり数千回の `evaluate` を実プロセスで回す。
> 目安 1〜3 分。`pytest.mark.slow` を付けてあるので、
> `uv run pytest -m "not slow"` で日常ループから外せる (CI/検収では必ず回す)。

- [ ] **Step 4-3e: Commit**

```bash
git add tests/plugin/test_indicator_wiring_e2e.py src/agentic_fx/plugin
git commit -m "test(e2e): rsi_pullback completes the adoption gate against an oracle (A1/A1-b)"
```

**T4a 完了条件**:
- [ ] **F3**: `run_kind_gate` が未解決で `verdict_kind == "indicator_unresolved"` +
      alias + reason を**返す** (例外なし)、`backtest_runs` / `approval_requests` 行数不変
- [ ] **F3'**: `_run_full_gate` / submit / bless が unpinned で
      `ValueError("indicator_unresolved:rsi:unpinned")`、approval 行 0 / gate 行 0
- [ ] **U4a**: submit / bless (kind=indicator) で `outputs` 無し →
      `ValueError("outputs_required")` (完全一致)、approval 行 0。`outputs` 宣言済みは通る
- [ ] **U4b (承認経路)**: `outputs` 宣言なしの配備済 indicator に依存する strategy は
      `indicator_unresolved:<alias>:outputs_undeclared` で拒否される
- [ ] **A1**: `rsi_pullback` (依存 1) が gate 経路を完走し、candidate の
      `scope='in_sample'` 行 1 (`content_hash` = pinned config の hash) と
      `no_strategy` 行 1 が残る。**A1-b**: `run_in_sample` 直接では candidate 行のみ
- [ ] **A1 の核**: `decision_sink` の記録が独立参照実装と全時点で一致
      (`(action, direction)` + `entry_type` + SL/TP ±1e-9)
- [ ] **C1 (verdict 経路)**: `StrategyGateVerdict.cpu_samples` の scope 列が
      **厳密に `["in_sample", "holdout"]`** (pair 1 本、各 1 件、`floor_mode="warn"`
      で早期 return を避ける)、値は **float** (codex plan r1 I7 — v1.2 は
      in_sample 1 件しか assert せず holdout 0 件でも緑だった)
- [ ] **P3' (`is` 同一性の全数)**: `test_resolved_object_identity_across_both_scopes`
      が resolver 1 回 + in_sample / holdout 両 session の `resolved` が
      `GateOutcome.resolved` と `is` 一致 (codex plan r1 I6)。
      spy は **call site である `approval.resolve_indicator_deps`** を patch する
      (`plugin.resolve` モジュール属性ではない — 関数を直接 import しているため)
- [ ] **`run_kind_gate(inventory=)` の既存 5 呼び出しが移行済み** (opus r1 C6、
      Step 4-1a の移行表)
- [ ] 段 0 変異 red: (a) `run_kind_gate` の `except IndicatorResolutionError` を
      `raise` に変える → F3 のテストが落ちる (b) `_run_full_gate` の
      `outputs_required` 判定を `meta.outputs == ()` にする → U4a が落ちる

---

## T4b: ロック集合 / 再解決 / payload / noop / reconcile / 承認詳細 [approval-corridor-2]

**この task は opus r1 観点 7 で T4 から切り出した** (旧 T4 = 8 Step / 約 1,700 行 /
src 6 ファイルで 1 セッションの上限を超えていた)。境界は「gate の判別子が確定した
ところ」。**T4b は T4a の `GateOutcome.resolved` / `GateOutcome.verdict_kind` を
Consumes するだけ**で、gate の内部には触らない。

**対応**: 設計書 §2.3 の承認回廊行 (lock 集合 / approve 時再解決 / bless TOCTOU)、
§2.7 (noop の pin 除去比較・再ロック例外・質検査)、§2.8、§4 の `switch.py` /
`noop_gate.py` / `commands.py` 行。
**完了条件の受入 ID**: A2 / P2 / P2' / P2'' / P3 (payload 部分) / P3' / P4 / P5 / D1 / R2。

**着手条件**: T4a が main にマージ済み。T6b も必須 (`stage_switched_journal` /
`shell_env` / `deploy_strategy` / `bump_indicator_version` を使う)。
**worktree 並列不可** (T4a の型を Consumes するので 1 レーン直列)。

**Files:**
- Modify: `src/agentic_fx/plugin/switch.py` (`_plugin_lock` / `_plugin_locks` /
  `_dependency_names`、`submit_candidate`、`approve_candidate`、`bless_candidate`、
  submit / bless の payload 構築 (`indicator_deps`)、
  `reconcile_switch_journals` + `_unresolved_after_switch`)
- Modify: `src/agentic_fx/plugin/noop_gate.py`
- Modify: `src/agentic_fx/commands.py` (`_approval_detail` + `_dependent_strategies`)
- Modify: `src/agentic_fx/loops/improve_loop.py` (質検査の再ロック除外 +
  `_build_approval_payload(resolved=None)` の受け口と `indicator_deps` 生成 —
  実配線は T5b。codex plan r1 I5) + `_finalize_success` の P5 呼び出し
  (下記 `_check_duplicate_metrics_for_approval` 参照、codex plan r2 束3 Critical)
- Modify: `src/agentic_fx/loops/improve_run_context.py` (**`inventory` /
  `inventory_view` の 2 フィールドを互換既定値付きで追加 — codex plan r1 C4 の形を
  T5a から本 task へ前倒し。codex plan r2 束3 Critical**: T5a Step 5-1 (P5 の
  `_check_duplicate_metrics_for_approval(inventory=ctx.inventory, ...)` の
  直前) は元々この 2 フィールドの追加を Produce する task だったが、依存順で
  T4b → T5a なので T4b の時点では `ImproveRunContext` に `inventory` 属性が
  まだ無く `AttributeError` になっていた。**追加そのもの (dataclass の 2
  フィールド、default 値のみ) だけを本 task へ前倒しする** — 「実
  `prepare()` が非空の `InventoryBuildResult` を作ってここへ渡す」配線
  (`_materialize_workspace` / `build_inventory_view` / `synthetic_ctx` の更新)
  は**前倒ししない**、引き続き T5a Step 5-1 が Consumes して行う。本 task の
  時点では `ctx.inventory` は常に既定値 `None` のまま (`prepare()` がまだ
  書き込まないため) — P5 の呼び出し側がこれを `self._inventory_for_gate(conn)`
  へフォールバックする (下記))
- Test: `tests/plugin/test_switch_paths.py`、`tests/plugin/test_reconcile.py`、
  `tests/plugin/test_approval_payload_common_contract.py` (P3 三経路、
  codex plan r1 I5)、`tests/loops/test_improve_loop_plugin_gate.py`、
  `tests/loops/test_improve_loop_duplicate_metrics.py` (P5、codex plan r1 C3)、
  `tests/test_commands.py`

**Interfaces:**

- Consumes: T4a の `GateOutcome.resolved` / `.verdict_kind` / `.indicator_alias` /
  `.indicator_reason`、T1 の `same_modulo_pins` / `is_relock_transition` /
  `ResolvedIndicatorSet.pin_object()`、T6b の `wiring_envs`
  (`switch_env` / `SETTINGS_FIXTURE` / `stage_switched_journal` / `shell_env` /
  `deploy_strategy` / `bump_indicator_version` / `submit_indicator_v2` /
  `approve_indicator_v2` / **`install_gate_double`**)。
- **本 task の全テストで守る規律 (codex plan r1 C1)**: `switch_env` が作るのは
  **空 DB** で、`submit_candidate` / `bless_candidate` は kind=strategy のとき
  実 `_run_full_gate` → `run_kind_gate` → `evaluate_strategy_adoption_gate` から
  **実 backtest** を回す。したがって **strategy を submit / bless する全ケースで、
  `_install_gate_double(monkeypatch)` を呼ぶか `fx.seed_history(conn)` で
  履歴を入れるかを明示的に選ぶこと** (どちらも無いと `NoHistoryError` で、
  lock・A2・payload・P2 の観測点に到達する前に落ちる)。
  本 task では**全ケースで gate double を使う** — 本 task の受入
  (A2 / P2 / P2' / P2'' / P3 / P3' / P4 / P5 / D1 / R2) はいずれも gate の
  中身ではなく lock・再解決・payload・journal を見ており、実 backtest は
  T4a の A1 / A1-b / C1 (`test_indicator_wiring_e2e.py`、実 worker、`slow`) が
  担保する。`settings` は `SETTINGS_FIXTURE` に統一する (fixture の
  `eval_source` / `base_interval` / `holdout_months` と食い違わせない)。
  **例外**: `_run_full_gate` に到達する前に落ちるケース
  (`candidate_changed` / `outputs_required` / `unpinned` / kind=indicator) は
  double 不要 — その旨をテスト docstring に 1 行書く。
- Produces:

```python
# src/agentic_fx/plugin/switch.py
@contextlib.contextmanager
def _plugin_locks(plugins_root: Path, names: "Iterable[str]"): ...
    # sorted(set(names)) の順に flock を取る (deadlock 回避のための順序 pin)
def _unresolved_after_switch(conn, row: dict, *, plugins_root: Path,
                             settings) -> tuple[str, str] | None: ...

# src/agentic_fx/plugin/noop_gate.py
def find_noop_copy(plugin_dir: Path, *, source_snapshot_dir: Path,
                   examples_dir: Path, name: str,
                   inventory: "InventoryBuildResult") -> str | None: ...

# src/agentic_fx/commands.py
class Commands:
    def _dependent_strategies(self, *, indicator_name: str,
                              candidate_hash: str) -> tuple[list[str], list[str]]: ...

# src/agentic_fx/loops/improve_run_context.py (codex plan r2 束3 Critical —
# T5a Step 5-1 の C4 フィールド追加を前倒し。既存 8 フィールドは無変更)
@dataclass(frozen=True)
class ImproveRunContext:
    mission_id: int
    run_id: int
    staging_dir: Path
    source_snapshot_dir: Path
    allowed_backlog_ids: frozenset[int] | None
    slot_key: tuple[str, int] | None
    ledger: ImproveRpcLedger
    rpc_handlers: dict[str, Callable[[dict], dict]]
    inventory: "InventoryBuildResult | None" = None     # 本 task では常に None
    inventory_view: dict = field(default_factory=dict)  # (実配線は T5a)

# src/agentic_fx/loops/improve_loop.py
def _check_duplicate_metrics_for_approval(
        self, conn, approval_payload: dict, *,
        inventory: "InventoryBuildResult", staging_dir: Path,
        ) -> "_DuplicateDemotion | None": ...
def _deployed_dir_for(self, name: str, *, inventory) -> "Path | None": ...
def _candidate_dir_for(self, approval_payload, *, staging_dir) -> "Path | None": ...
def _inventory_for_gate(self, conn) -> "InventoryBuildResult": ...
    # 本 task 内 (Step 4-6c) で定義する暫定 helper (`approved_plugins` を
    # 都度呼ぶ)。`_run_plugin_gate` / `commit()` の暫定解決 (既存記述) と
    # **P5 (`_check_duplicate_metrics_for_approval` 呼び出し) の両方**が
    # `ctx.inventory is None` のときのフォールバック先として共有する
    # (Step 4-6c 参照)。T5a Step 5-1 で `ctx.inventory` が実配線された後に
    # 削除する。
```

### Step 4-4: ロック集合 + `approve_candidate` の決定時解決 + bless の TOCTOU

- [ ] **Step 4-4a: Write the failing test**

`tests/plugin/test_switch_paths.py` に追記:

```python
def test_plugin_locks_acquires_sorted_unique_names(tmp_path, monkeypatch):
    """P2'': 同一 indicator を 2 alias で参照しても lock 取得は 1 回。
    名前昇順 (順序 pin — 逆順で取っても deadlock しない)。"""
    acquired = []
    import contextlib as _ctx
    real = plugin_switch._plugin_lock

    @_ctx.contextmanager
    def _spy(root, name):
        acquired.append(name)
        with real(root, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    root = tmp_path / "plugins"
    root.mkdir()
    with plugin_switch._plugin_locks(root, ["s", "rsi", "rsi", "adx"]):
        pass
    assert acquired == ["adx", "rsi", "s"]


def test_plugin_lock_order_for_approve_candidate_is_sorted_unique(
        tmp_path, monkeypatch):
    """P2' (設計書 v1.3): `_plugin_lock` の取得順序が常に**名前昇順・
    重複なし**であることを spy で pin する (順序が崩れる変異を検出)。
    `approve_candidate` の実経路で `_plugin_lock` を monkeypatch し、
    記録した取得順が `sorted(set(names))` と一致することを assert する
    (指揮者へ申告済みの設計是正 — 「逆順で取っても deadlock しない」は
    実測不能な主張だったため、実測可能な順序 pin に置換した)。"""
    acquired: list[str] = []
    import contextlib as _ctx
    real = plugin_switch._plugin_lock

    @_ctx.contextmanager
    def _spy(root, name):
        acquired.append(name)
        with real(root, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    conn, plugins_root = _switch_env(tmp_path)
    # [codex plan r1 C1] `_switch_env` は**空 DB**しか作らないが
    # `submit_candidate` は実 `_run_full_gate` から strategy backtest を回す。
    # 本テストが見るのは lock の取得順序だけなので gate は double にする
    # (resolver は double より前に実物が走るので観測点は死なない)。
    _install_gate_double(monkeypatch)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": hashes["rsi"]})
    approval_id = plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    acquired.clear()  # submit 経路のロックは対象外、approve 経路だけを見る
    plugin_switch.approve_candidate(
        conn, approval_id, decided_by="human_cli", now=fx.NOW,
        plugins_root=plugins_root, settings=SETTINGS_FIXTURE)
    # 依存は alias "rsi" -> plugin "rsi" の 1 本、名前集合は
    # {"rsi_pullback", "rsi"} (`_dependency_names` = [name, *deps])。
    assert acquired == sorted({"rsi_pullback", "rsi"})


def test_dependency_lock_blocks_a_concurrent_indicator_approval(tmp_path):
    """並行実行の相互排除 (順序 pin 自体は上の
    `test_plugin_lock_order_for_approve_candidate_is_sorted_unique` が
    spy で検証する): S の approve が握っている間、依存 indicator I の
    approve は待たされる。

    `fcntl.flock(LOCK_EX)` は**同一プロセスでも別 fd 同士なら競合する**
    ので、スレッド 2 本で観測できる (`_plugin_lock` は fd を毎回開く)。"""
    import threading

    root = tmp_path / "plugins"
    root.mkdir()
    holding = threading.Event()
    release = threading.Event()
    acquired_second = threading.Event()

    def hold_strategy_locks():
        with plugin_switch._plugin_locks(root, ["rsi_pullback", "rsi"]):
            holding.set()
            release.wait(5.0)

    def take_indicator_lock():
        with plugin_switch._plugin_lock(root, "rsi"):
            acquired_second.set()

    a = threading.Thread(target=hold_strategy_locks)
    a.start()
    assert holding.wait(5.0), "strategy locks were never acquired"
    b = threading.Thread(target=take_indicator_lock)
    b.start()
    try:
        # 保持中は取れない
        assert not acquired_second.wait(0.5), \
            "indicator lock was granted while the strategy held it"
    finally:
        release.set()
    a.join(5.0)
    # 解放後は取れる
    assert acquired_second.wait(5.0)
    b.join(5.0)
    assert not a.is_alive() and not b.is_alive()


def test_approve_candidate_rejects_pin_mismatch_and_stays_pending(tmp_path,
                                                                  monkeypatch):
    """A2: submit 後に indicator を更新 → approve で
    ValueError('indicator_unresolved:rsi:pin_mismatch')、pending のまま、
    `.versions` に新版なし、symlink 不変。"""
    conn, plugins_root = _switch_env(tmp_path)
    _install_gate_double(monkeypatch)   # codex plan r1 C1 (submit は実 gate)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": hashes["rsi"]})
    approval_id = plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)

    # indicator を別内容で再承認 (I1 -> I2)。
    # [codex plan r1 I4 是正] v1.2 は live symlink の**解決先**
    # (`(plugins_root / "rsi").resolve()`) の `plugin.py` に直接追記してから
    # `_bump_indicator_version` を呼んでいた。これは
    # (a) 承認済 I1 の version directory を **immutable でなくし**、
    # (b) approval 行に記録済の I1 の `content_hash` を**ディスク側だけ**
    #     書き換えるので、`fx.deploy_approved` が作った承認状態が壊れる。
    # A2 が再現したいのは「I1 は無傷のまま I2 が別版として承認され、
    # live が I2 を指す」状態なので、**解決先には一切触らず**
    # `bump_indicator_version` だけを呼ぶ (このビルダは現行の中身を読んで
    # `# v2 (output-preserving change)` を足した**新しい version directory**
    # を作り、承認して symlink を差し替える — T6b の Produces 参照)。
    _bump_indicator_version(conn, plugins_root, "rsi", now=fx.NOW)
    before_versions = sorted(
        p.name for p in (plugins_root / ".versions" / "rsi_pullback").glob("*")) \
        if (plugins_root / ".versions" / "rsi_pullback").is_dir() else []
    before_link = (plugins_root / "rsi_pullback").is_symlink()

    with pytest.raises(ValueError) as ei:
        plugin_switch.approve_candidate(
            conn, approval_id, decided_by="human_cli", now=fx.NOW,
            plugins_root=plugins_root, settings=SETTINGS_FIXTURE)
    assert str(ei.value) == "indicator_unresolved:rsi:pin_mismatch"
    assert conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()["status"] == "pending"
    after_versions = sorted(
        p.name for p in (plugins_root / ".versions" / "rsi_pullback").glob("*")) \
        if (plugins_root / ".versions" / "rsi_pullback").is_dir() else []
    assert after_versions == before_versions
    assert (plugins_root / "rsi_pullback").is_symlink() == before_link


def test_bless_detects_candidate_change_between_read_and_lock(tmp_path,
                                                              monkeypatch):
    """P2'': 事前読取 → lock 取得の間に候補 config.yaml を差し替えると
    固定文言 `candidate_changed`、approval 行 0、`.versions`/symlink 不変。

    gate double は不要 — `candidate_changed` は `_plugin_locks` の内側・
    `_run_full_gate` の**手前**で送出されるので backtest に到達しない
    (codex plan r1 C1 の例外ケース)。"""
    conn, plugins_root = _switch_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    human = plugins_root / "_human"
    d = fx.write_rsi_pullback(human, pins={"rsi": hashes["rsi"]})

    import contextlib as _ctx
    real = plugin_switch._plugin_locks

    @_ctx.contextmanager
    def _tamper(root, names):
        (d / "config.yaml").write_text(
            (d / "config.yaml").read_text() + "\n# tampered\n")
        with real(root, names):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_locks", _tamper)
    with pytest.raises(ValueError, match="^candidate_changed$"):
        plugin_switch.bless_candidate(
            conn, name="rsi_pullback", human_dir=d,
            settings=SETTINGS_FIXTURE,
            now=fx.NOW, decided_by="human_cli")
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 1  # rsi のみ
    assert not (plugins_root / ".versions" / "rsi_pullback").exists()


def test_bless_locks_are_all_released_after_candidate_changed(tmp_path,
                                                              monkeypatch):
    """同上: 全 lock が解放されている (次の操作がブロックしない)。"""
    conn, plugins_root = _switch_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    human = plugins_root / "_human"
    d = fx.write_rsi_pullback(human, pins={"rsi": hashes["rsi"]})
    import contextlib as _ctx
    real = plugin_switch._plugin_locks

    @_ctx.contextmanager
    def _tamper(root, names):
        (d / "config.yaml").write_text(
            (d / "config.yaml").read_text() + "\n# tampered\n")
        with real(root, names):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_locks", _tamper)
    with pytest.raises(ValueError):
        plugin_switch.bless_candidate(
            conn, name="rsi_pullback", human_dir=d,
            settings=SETTINGS_FIXTURE,
            now=fx.NOW, decided_by="human_cli")
    monkeypatch.undo()
    # 解放されていれば即座に取れる (blocking flock なのでハングしない)
    with plugin_switch._plugin_locks(plugins_root, ["rsi_pullback", "rsi"]):
        pass
```

- [ ] **Step 4-4b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_switch_paths.py -k "plugin_locks or sorted_unique or pin_mismatch_and_stays_pending or candidate_change" -q`
Expected: FAIL — `AttributeError: module has no attribute '_plugin_locks'`

- [ ] **Step 4-4c: Write minimal implementation**

`src/agentic_fx/plugin/switch.py` に追加 (`_plugin_lock` の直後):

```python
@contextlib.contextmanager
def _plugin_locks(plugins_root: Path, names):
    """[indicator-consumption-wiring] §2.3 (codex r4 I1 / r5 I2):
    strategy + 依存 indicator 名を `sorted(set(names))` の順に取る。

    - **重複排除**: 同一 indicator を複数 alias から参照しても 1 回だけ取る
      (二重取得は同一プロセス内の flock 再入で無害だが、spy が数える
      「取得回数」を契約として固定する — P2'')。
    - **名前昇順**: 逆順で取る呼び出し元が現れても deadlock しないよう
      順序を 1 箇所に固定する (P2')。
    """
    with contextlib.ExitStack() as stack:
        for name in sorted(set(names)):
            stack.enter_context(_plugin_lock(plugins_root, name))
        yield


def _dependency_names(candidate_dir: Path, name: str) -> list[str]:
    """候補 `config.yaml` から依存 indicator 名を読む (lock 集合の材料)。
    読めない/依存なしなら `[name]` だけを返す。"""
    try:
        config = yaml.safe_load(
            (candidate_dir / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return [name]
    refs = (config or {}).get("indicators") or {}
    deps = [ref.get("plugin") for ref in refs.values()
            if isinstance(ref, dict) and isinstance(ref.get("plugin"), str)]
    return [name, *deps]
```

`submit_candidate` / `bless_candidate` の `with _plugin_lock(plugins_root, name):` を
`with _plugin_locks(plugins_root, _dependency_names(candidate_dir, name)):` に置換する。

`bless_candidate` に TOCTOU 検査を足す (lock 取得の**前**に候補を読み、lock 内で再読):

```python
    # [indicator-consumption-wiring] §2.3 (codex r5 I2): bless は候補
    # (mutable な `_human`) から依存名を lock **前**に読むので、lock 取得後に
    # 候補の content_hash と依存名集合を再読し、事前読取と完全一致しなければ
    # 全解放して固定文言 `candidate_changed` で失敗する (retry しない)。
    pre_hash = loader.content_hash(human_dir)
    pre_deps = sorted(set(_dependency_names(human_dir, name)))

    with _plugin_locks(plugins_root, pre_deps):  # 0b
        if (loader.content_hash(human_dir) != pre_hash
                or sorted(set(_dependency_names(human_dir, name))) != pre_deps):
            raise ValueError("candidate_changed")
        ...  # 以降は現行のまま
```

`approve_candidate` に決定時の `require` 解決を足す (0c superseded 判定と同じ位置、
`_is_superseded` の直後):

```python
    payload_pre = json.loads(row["payload_json"])
    name = payload_pre["name"]
    candidate_origin_pre = payload_pre["candidate_origin"]
    candidate_path_pre = payload_pre["candidate_path"]
    try:
        candidate_dir_pre = resolve_candidate_dir(
            plugins_root, candidate_origin=candidate_origin_pre,
            candidate_path=candidate_path_pre, name=name)
        dep_names = _dependency_names(candidate_dir_pre, name)
    except CandidateMissingError:
        dep_names = [name]

    with _plugin_locks(plugins_root, dep_names):  # 0b
        ...  # 既存の pending 判定 / _is_superseded

        # [indicator-consumption-wiring] §2.3: P2 (決定時) に `require` で
        # **解決だけ**行う (gate は再実行しない)。submit → approve の間に
        # indicator が更新されていれば pending のまま拒否する — 版作成・
        # symlink 切替に進まない。inventory はこの lock の内側で構築する
        # (lock の外で読むと切替との間に別ロックの承認が割り込む)。
        meta_for_deps = loader._discover_one(candidate_dir_pre, name)
        if meta_for_deps is not None and meta_for_deps.kind == "strategy":
            inventory = tools_plugin_loader.approved_plugins(
                conn, plugins_root, settings=settings)
            try:
                resolve_indicator_deps(
                    meta_for_deps, inventory.inventory, settings=settings,
                    pin_mode="require")
            except IndicatorResolutionError as exc:
                raise ValueError(str(exc)) from exc
```

import を追加:

```python
import yaml

from agentic_fx.plugin.resolve import (
    IndicatorResolutionError, resolve_indicator_deps,
)
```

- [ ] **Step 4-4d: Run test to verify it passes**

Run: `uv run pytest tests/plugin -q`
Expected: PASS

- [ ] **Step 4-4e: Commit**

```bash
git add src/agentic_fx/plugin/switch.py tests/plugin/test_switch_paths.py
git commit -m "feat(switch): dependency lock set, approve-time re-resolution, bless TOCTOU (A2/P2'/P2'')"
```

### Step 4-5: 承認 payload の `indicator_deps` (三経路同形)

- [ ] **Step 4-5a: Write the failing test**

`tests/plugin/test_approval_payload_common_contract.py` に追記:

```python
def test_indicator_deps_is_present_and_identical_in_all_three_corridors(
        tmp_path, monkeypatch):
    """P3: submit / bless / `_build_approval_payload` の 3 経路が
    `indicator_deps` を**同形 (plain object) かつ同値**で載せる。

    **codex plan r1 I5 是正**: v1.2 の本テストは submit payload しか検査して
    おらず (改善経路は T5b の別テスト、bless は誰も見ていなかった)、
    **bless の実装から `indicator_deps` キーを落としても緑のまま**だった。
    同じ依存候補を `bless_candidate` にも通し、**3 つの payload を相互比較**
    する (`submit == bless == improve` の等値。improve 経路の payload は
    `_build_approval_payload` の `resolved=` 受け口は**本 Step 4-5c で足す**
    ので、ここでは `_build_approval_payload` を**直接呼んで**同形を確かめる。
    改善 mission の実経路で `ctx.inventory` から `resolved` が届くことは
    T5b が P3 の残りとして pin する)。

    gate double: submit / bless とも kind=strategy なので実 backtest が走る
    → `_install_gate_double` で潰す (codex plan r1 C1)。"""
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet
    from tests.fixtures import indicator_wiring as fx

    conn, plugins_root = _switch_env(tmp_path)
    _install_gate_double(monkeypatch)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": hashes["rsi"]})
    submit_id = plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (submit_id,)).fetchone()["payload_json"])
    expected = {"rsi": {"plugin": "rsi", "content_hash": hashes["rsi"],
                        "params": {"period": fx.RSI_PERIOD}}}
    assert payload["indicator_deps"] == expected
    json.dumps(payload)     # plain JSON であること

    # --- 経路 2: bless (codex plan r1 I5) -------------------------------
    # 別名の候補を `_human` に置いて bless する (submit 済の
    # `rsi_pullback` と名前が衝突しないように `rename` ではなく別ディレクトリ)。
    bless_dir = fx.write_rsi_pullback(plugins_root / "_human2",
                                      pins={"rsi": hashes["rsi"]})
    bless_id = plugin_switch.bless_candidate(
        conn, name="rsi_pullback", human_dir=bless_dir,
        settings=SETTINGS_FIXTURE, now=fx.NOW, decided_by="human_cli")
    bless_payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (bless_id,)).fetchone()["payload_json"])
    assert bless_payload["indicator_deps"] == expected
    json.dumps(bless_payload)

    # --- 経路 3: `ImproveLoop._build_approval_payload` (改善経路) --------
    # 同じ resolver 結果から作った payload が同形・同値であること。
    # **同ファイルの既存ヘルパ `_payload_from_improve_loop(tmp_path, kind)`
    # を流用する** (`_build_approval_payload` は `ImproveLoop` の
    # **メソッド**で、`conn` + 11 個のキーワードを取る — 着手時に
    # `rg -n 'def _build_approval_payload' -A 5 src/agentic_fx/loops/improve_loop.py`
    # と `sed -n '166,186p' tests/plugin/test_approval_payload_common_contract.py`
    # で現物を再取得すること)。`resolved=` キーワードは**本 Step (4-5c) で
    # 足す** (既定 `None`) — T5b は `ctx.inventory` からの実配線だけを行う:
    from agentic_fx.plugin import loader as plugin_loader
    from agentic_fx.plugin.resolve import resolve_indicator_deps
    from agentic_fx.tools import plugin_loader as tools_plugin_loader
    meta, _reason = plugin_loader.discover_one_with_reason(
        plugins_root / "_human" / "rsi_pullback", "rsi_pullback")
    inventory = tools_plugin_loader.approved_plugins(
        conn, plugins_root, settings=SETTINGS_FIXTURE)
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=SETTINGS_FIXTURE,
                                      pin_mode="require")
    improve_payload = _payload_from_improve_loop(
        tmp_path, "strategy", resolved=resolved)
    assert improve_payload["indicator_deps"] == expected

    # 3 経路の相互比較 (どれか 1 つが落ちれば検出する)
    assert (payload["indicator_deps"] == bless_payload["indicator_deps"]
            == improve_payload["indicator_deps"])
```

同ファイルの既存ヘルパ `_payload_from_improve_loop` に `resolved` を通す
(既存の 4 系統比較テストは `resolved=None` 既定でそのまま緑):

```python
def _payload_from_improve_loop(tmp_path, kind, *, resolved=None):
    ...     # 既存のまま (ImproveLoop 構築 / ledger.freeze())
    return loop._build_approval_payload(
        conn, name=f"{kind}_d", kind=kind, content_hash="h", artifact_hash="a",
        ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path=f"plugins/_staging/1/{kind}_d",
        gate_metrics=gate_metrics, output={"selection_rationale": ""}, now=NOW,
        resolved=resolved)


def test_indicator_deps_is_empty_object_for_a_strategy_without_deps(
        tmp_path, monkeypatch):
    conn, plugins_root = _switch_env(tmp_path)
    _install_gate_double(monkeypatch)   # 依存 0 でも kind=strategy は実 gate
    _write_dependency_free_strategy(plugins_root / "_human", "plain_strat")
    approval_id = plugin_switch.submit_candidate(
        conn, name="plain_strat", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=NOW)
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["payload_json"])
    assert payload["indicator_deps"] == {}


def test_indicator_deps_is_empty_object_for_indicator_kind(tmp_path):
    """indicator 候補には依存が無いので `indicator_deps` は `{}` (キーは
    schema 安定のため必ず存在する — 他の 4 キーと同じ規約)。

    gate double 不要 — kind=indicator は `_validate_kind` 経路で strategy
    backtest を回さない (codex plan r1 C1 の例外ケース)。"""
    conn, plugins_root = _switch_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root / "_human", "rsi")
    approval_id = plugin_switch.submit_candidate(
        conn, name="rsi", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["payload_json"])
    assert payload["indicator_deps"] == {}


def test_payload_indicator_deps_comes_from_the_gate_outcome_not_a_re_resolve(
        tmp_path, monkeypatch):
    """P3' (呼び出し回数): submit 1 回あたり resolver 呼び出しは 1 回だけ。
    `_run_full_gate` の外で再解決していない。

    **codex plan r1 I6 是正**: v1.2 は `agentic_fx.plugin.resolve` **モジュール
    属性**を patch していたが、T4a の実装は `approval.py` の冒頭で
    `from agentic_fx.plugin.resolve import resolve_indicator_deps` と
    **関数を直接 import** する (Step 4-1c の import ブロック逐語)。
    モジュール属性を差し替えても call site は元の関数オブジェクトを
    握ったままなので **spy が 1 度も呼ばれず、`calls == []` で落ちる**
    (= 正しい実装でも赤くなる判別力ゼロの pin)。**call site である
    `approval.resolve_indicator_deps` を patch する**
    ([[shared-reader-crosses-layer-boundaries]])。`switch.py` 側でも
    `approve_candidate` が同じ関数を直接 import するので、そちらを数える
    テストでは `plugin_switch.resolve_indicator_deps` を patch すること。
    着手時に `rg -n 'resolve_indicator_deps' src/agentic_fx/plugin/` で
    import 形を再確認する。"""
    from agentic_fx.plugin import approval as approval_mod
    calls = []
    real = approval_mod.resolve_indicator_deps
    monkeypatch.setattr(
        approval_mod, "resolve_indicator_deps",
        lambda *a, **k: calls.append(k.get("pin_mode")) or real(*a, **k))
    conn, plugins_root = _switch_env(tmp_path)
    _install_gate_double(monkeypatch)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": hashes["rsi"]})
    plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    assert calls == ["require"]     # 候補ごとに 1 回だけ
```

> **P3' の `is` 同一性の全数 pin は T4a Step 4-3 の E2E に置く (codex plan r1 I6)。**
> 設計書 §6 P3' は「in_sample session と holdout session に渡った `resolved`、
> `GateOutcome.resolved`、payload の `indicator_deps` を作った元が**同一 object**
> (`is`)」を要求するが、T4a Step 4-1a の
> `test_run_kind_gate_resolves_once_and_carries_the_same_object` は
> `evaluate_strategy_adoption_gate` を丸ごと double にしているため
> **gate へ 1 回渡った object しか見えない** — in_sample / holdout の 2 scope で
> **同じ**object が使われたかは観測できない。`build_intent_source` を
> double にして両 scope の `resolved` を収集する形は、gate の実装
> (`strategy_gate.py` が pair ごと・scope ごとに `build_intent_source` を呼ぶ)
> に踏み込むので **T4a Step 4-3 の実 gate E2E (`wired` fixture)** に置く。
> 具体形は Step 4-3a の `test_resolved_object_identity_across_both_scopes` を参照。

- [ ] **Step 4-5b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_approval_payload_common_contract.py -k indicator_deps -q`
Expected: FAIL — `KeyError: 'indicator_deps'`

- [ ] **Step 4-5c: Write minimal implementation**

`src/agentic_fx/plugin/switch.py` の `submit_candidate` / `bless_candidate` の
payload 構築に 1 キーずつ追加:

```python
            # [indicator-consumption-wiring] §2.7: 表示・監査用 (identity には
            # 使わない — identity は既存の `content_hash` のまま)。
            # **`outcome.resolved` から作る** — ここで再解決しない (P3')。
            "indicator_deps": (outcome.resolved.pin_object()
                               if outcome.resolved is not None else {}),
```

**改善経路 (`ImproveLoop._build_approval_payload`) の同キーも本 Step で足す**
(codex plan r1 I5 + advisor 指摘): 三経路同形テストが 3 つ目の payload を
`_build_approval_payload` から取るので、そのキーが T5b まで存在しないと
Step 4-5a が `TypeError` で赤いまま T4b を終えられない。**ここでは
キーワード引数の受け口と `indicator_deps` の生成だけを足し、
`ctx.inventory` からの実配線は T5b Step 5-4/5-5 が行う** (既定 `None` の
ままなら `{}` = 依存なしと同じ、既存の 4 系統比較テストは無変更で緑):

```python
    def _build_approval_payload(self, conn, *, name, kind, content_hash,
                                artifact_hash, ctx_ledger, mission_id,
                                backlog_id, candidate_origin, candidate_path,
                                gate_metrics, output, now,
                                resolved: "ResolvedIndicatorSet | None" = None,
                                ) -> dict:
        ...
        return {
            ...,
            # [indicator-consumption-wiring] §2.7: switch.py の
            # submit / bless と**同形**。呼び出し元が解決済み集合を
            # 渡す (ここで再解決しない — P3')。
            "indicator_deps": (resolved.pin_object()
                               if resolved is not None else {}),
        }
```

(`src/agentic_fx/loops/improve_loop.py` は既に T4b の Files に入っている
[質検査の再ロック除外] ので、task をまたがない。)

- [ ] **Step 4-5d: Run test to verify it passes**

Run: `uv run pytest tests/plugin -q`
Expected: PASS

- [ ] **Step 4-5e: Commit**

```bash
git add src/agentic_fx/plugin/switch.py tests/plugin
git commit -m "feat(approval): carry indicator_deps in submit/bless payloads (P3)"
```

### Step 4-6: noop gate の pin 除去比較と再ロック例外 + 成績質検査

- [ ] **Step 4-6a: Write the failing test**

`tests/loops/test_improve_loop_plugin_gate.py` に追記 (既存 5 件は新引数へ移行):

```python
# --- [indicator-consumption-wiring] T4: P4 / P5 ---------------------------

from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from tests.fixtures import indicator_wiring as fx


def _inventory_of(plugins_root, names):
    from agentic_fx.plugin.loader import discover_one_with_reason
    metas = []
    for name in names:
        meta, reason = discover_one_with_reason(
            (plugins_root / name).resolve(), name)
        assert reason is None, reason
        metas.append(meta)
    inv = ApprovedInventory(root=plugins_root.resolve(), metas=tuple(metas))
    return InventoryBuildResult(inventory=inv, phase1_metas=tuple(metas),
                                resolved={}, rejected_strategies=())


def test_locked_copy_of_an_example_is_still_a_noop(tmp_path):
    """P4: `_examples/rsi_pullback` を逐語コピーして lock しただけの候補は
    `noop_copy_of:_examples/rsi_pullback`。"""
    from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    snapshot = tmp_path / "snap"
    examples = snapshot / "_examples"
    _copy_example(examples, "rsi_pullback")      # docs/examples から逐語コピー
    cand = _copy_example(tmp_path / "staging", "rsi_pullback")
    inventory = _inventory_of(plugins_root, ["rsi"])
    # `rsi_pullback` の依存は `rsi_indicator` なので、fixture の `rsi` に
    # 読み替えてから lock する (example は unpinned のまま)
    _rename_dependency(cand, "rsi_indicator", "rsi")
    _rename_dependency(examples / "rsi_pullback", "rsi_indicator", "rsi")
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")
    lock_config(cand, resolve_indicator_deps(
        meta, inventory.inventory, settings=SETTINGS, pin_mode="ignore").pins())
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=examples, name="rsi_pullback",
                          inventory=inventory) == "_examples/rsi_pullback"


def test_relocked_copy_of_a_deployed_strategy_is_not_a_noop(tmp_path):
    """P4: 配備済 S (pin I1、I2 承認済で pin 破れ) を複製して I2 に再ロック
    した候補は noop にならない (正式な再ロック経路)。"""
    from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")            # = I2 (現在 inventory)
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    fx.write_rsi_pullback(snapshot, pins={"rsi": "a" * 64})   # 配備済 S (I1)
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins={"rsi": "a" * 64})
    meta, _ = plugin_loader.discover_one_with_reason(cand, "rsi_pullback")
    lock_config(cand, resolve_indicator_deps(
        meta, inventory.inventory, settings=SETTINGS, pin_mode="ignore").pins())
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback", inventory=inventory) is None


def test_same_copy_without_relock_is_a_noop(tmp_path):
    """P4: 同じ複製を pin I1 のまま (再ロックなし) 提出すると
    `noop_copy_of:rsi_pullback`。"""
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    fx.write_rsi_pullback(snapshot, pins={"rsi": "a" * 64})
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins={"rsi": "a" * 64})
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback",
                          inventory=inventory) == "rsi_pullback"


def test_param_only_variant_is_still_not_a_noop(tmp_path):
    """既存契約の維持: config だけ違う候補は noop ではない。"""
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    inventory = _inventory_of(plugins_root, ["rsi"])
    snapshot = tmp_path / "snap"
    fx.write_rsi_pullback(snapshot, pins=None)
    cand = fx.write_rsi_pullback(tmp_path / "staging", pins=None)
    import yaml
    cfg = yaml.safe_load((cand / "config.yaml").read_text())
    cfg["params"]["oversold"] = 25
    (cand / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    assert find_noop_copy(cand, source_snapshot_dir=snapshot,
                          examples_dir=snapshot / "_examples",
                          name="rsi_pullback", inventory=inventory) is None
```

`tests/loops/test_improve_loop_duplicate_metrics.py` に追記 (P5)。
まずヘルパ 3 本を同ファイルの先頭に置く:

```python
def loop_staging(loop):
    """候補置き場 (このテストでは mission を回さないので直接作る)。"""
    d = loop._root / "plugins" / "_staging" / "1"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_approved_candidate_metrics(conn, *, pair, trades, pf, avg_r):
    """既承認候補の in_sample 行を 1 本入れる
    (`find_matching_approved_metrics` が引き当てる母集団)。"""
    from datetime import timedelta

    from agentic_fx.store import approvals, backtest_runs as br
    aid = approvals.create(conn, "plugin",
                           {"name": "rsi_pullback", "kind": "strategy",
                            "content_hash": "d" * 64}, NOW)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t", now=NOW)
    br.save_harness_run(
        conn, scope="in_sample", plugin_ref="plugins/rsi_pullback",
        content_hash="d" * 64, kind="strategy", pair=pair, timeframe="1h",
        source="dukascopy", base_interval="5m", params={},
        period=(NOW - timedelta(days=90), NOW),
        metrics={"trades": trades, "pf": pf, "win_rate": 0.5, "avg_r": avg_r,
                 "max_drawdown": 0.05, "total_pnl": 100.0, "evaluable": True,
                 "fallback_spread_used": False},
        settings_hash="h", core_commit="c", initial_balance=1_000_000.0,
        now=NOW, variant="candidate")


def monkeypatch_dirs(loop, *, deployed, candidate):
    """`_deployed_dir_for` / `_candidate_dir_for` を固定する。

    **これは注入シームではなく短絡である** (codex plan r1 C3): これを使う
    テストは 2 つの helper の中身を 1 行も実行しない。`is_relock_transition`
    の判定そのものを見るために使い、**helper の配線は下の
    `test_relock_detection_uses_the_real_dir_helpers` が実物で検証する**。"""
    loop._deployed_dir_for = lambda name, *, inventory=None: deployed
    loop._candidate_dir_for = lambda payload, *, staging_dir=None: candidate
```

```python
def test_relock_only_resubmission_skips_the_duplicate_metrics_check(tmp_path):
    """P5: indicator を出力不変の変更で更新 → 依存 strategy を再ロック →
    再提出の成績が既承認版と一致しても `duplicate_metrics_of` で降格されない。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, plugins_root = _loop_env(tmp_path)     # tests.fixtures.wiring_envs (T6b)
    fx.write_indicator(plugins_root, "rsi")            # = I2 (現在 inventory)
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    # 既承認版 S (pin I1 = 破れている) を snapshot / 配備側に置く
    deployed = fx.write_rsi_pullback(plugins_root, pins={"rsi": "a" * 64})
    # 再ロック済み候補 S' (pin I2)
    candidate = fx.write_rsi_pullback(loop_staging(loop), pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)        # 既承認版と同じ成績
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    monkeypatch_dirs(loop, deployed=deployed, candidate=candidate)
    demotion = loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=loop_staging(loop))
    assert demotion is None


def test_unrelated_duplicate_metrics_still_demote(tmp_path):
    """P5 の裏: 再ロックでない一致は従来どおり降格する。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    # 配備済も候補も現在 inventory に pin 済み = 再ロック遷移ではない
    deployed = fx.write_rsi_pullback(plugins_root, pins={"rsi": hashes["rsi"]})
    candidate = fx.write_rsi_pullback(loop_staging(loop),
                                      pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    monkeypatch_dirs(loop, deployed=deployed, candidate=candidate)
    assert loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=loop_staging(loop)
    ) is not None


def test_relock_detection_uses_the_real_dir_helpers(tmp_path):
    """**codex plan r1 C3**: `monkeypatch_dirs` を使わず、`_deployed_dir_for` /
    `_candidate_dir_for` の**実装そのもの**を通す。上の 2 本は helper を
    差し替えるので `NameError` / 誤った引数契約を隠してしまう。

    配備側 `plugins/rsi_pullback` と候補 `<staging>/rsi_pullback` を
    ディスク上の実際の場所に置き、`inventory` / `staging_dir` だけを
    渡して再ロック判定が成立することを見る。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    # 配備済 S (pin I1 = 破れ) を **実際の配備位置**に置く
    fx.write_rsi_pullback(plugins_root, pins={"rsi": "a" * 64})
    # 再ロック済 S' を **実際の staging 位置**に置く
    staging = loop_staging(loop)
    fx.write_rsi_pullback(staging, pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}

    # helper 単体 (実装を直接見る)
    assert loop._deployed_dir_for("rsi_pullback", inventory=inventory) == \
        (plugins_root / "rsi_pullback")
    assert loop._candidate_dir_for(payload, staging_dir=staging) == \
        (staging / "rsi_pullback")
    # 配線 (差し替えなしで再ロック除外が効く)
    assert loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=staging) is None


def test_finalize_success_falls_back_to_inventory_for_gate_when_ctx_inventory_is_none(
        tmp_path):
    """codex plan r2 束3 Critical: `ImproveRunContext.inventory` は本 task の
    時点では常に既定値 `None` (実配線は T5a Step 5-1) — `_finalize_success`
    が `ctx.inventory` をそのまま渡すと strategy kind の承認で
    `_deployed_dir_for(name, inventory=None)` が `None.phase1_metas` の
    `AttributeError` を出す。`self._inventory_for_gate(conn)` へフォールバック
    すれば `ctx.inventory is None` のままでも P5 (質検査) が完走すること
    を見る。**`synthetic_ctx` (T6b) は本 task の時点では `inventory=` を
    渡していない**ので既定 `None` のままである (前提を assert で確認)。

    deployed / candidate を同一 pin で配置 (= 再ロック遷移では**ない**) し、
    `_seed_approved_candidate_metrics` で既承認候補と同じ成績にすることで
    `_check_duplicate_metrics_for_approval` が `_DuplicateDemotion` を返し、
    `_finalize_success` は `BEGIN IMMEDIATE` 直後の rollback + early return で
    終わる (`_persist_ledger_in_tx` 以降の mission/run 行を用意しなくてよい)。
    """
    from tests.fixtures import indicator_wiring as fx
    from tests.fixtures.wiring_envs import synthetic_ctx
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root, pins={"rsi": hashes["rsi"]})
    fx.write_rsi_pullback(loop_staging(loop), pins={"rsi": hashes["rsi"]})
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    ctx = synthetic_ctx(loop, conn, plugins_root.parent)
    assert ctx.inventory is None
    demotion = loop._finalize_success(
        conn, mission_id=1, run_id=1, backlog_id=1, slot_key=None,
        approval_payload=payload, now=fx.NOW, ctx=ctx)
    assert demotion is not None   # AttributeError にならず質検査まで完走した
```

(`_inventory_with_phase1(conn, plugins_root)` は同ファイルのローカルヘルパ:
`tools_plugin_loader.approved_plugins(conn, plugins_root, settings=SETTINGS_FIXTURE)`
をそのまま返す。pin 破れの `rsi_pullback` は第 2 相で落ちて `phase1_metas` にだけ
残る — `_deployed_dir_for` が `phase1_metas` を見る理由がこれ。)

- [ ] **Step 4-6b: Run test to verify it fails**

Run: `uv run pytest tests/loops/test_improve_loop_plugin_gate.py tests/loops/test_improve_loop_duplicate_metrics.py -q`
Expected: FAIL — `TypeError: find_noop_copy() got an unexpected keyword argument
'examples_dir'`

- [ ] **Step 4-6c: Write minimal implementation**

`src/agentic_fx/plugin/noop_gate.py::find_noop_copy` を置換:

```python
def find_noop_copy(plugin_dir: Path, *, source_snapshot_dir: Path,
                   examples_dir: Path, name: str,
                   inventory) -> str | None:
    """コードと config が既存 plugin と両方一致する候補を検出する。

    [indicator-consumption-wiring] §2.7 (codex r4 C2 / r8 I1):
    - 比較は **`same_modulo_pins`** (正規化 AST + `strip_pins(config)`) —
      pin は作者の設計判断ではなくハーネスの派生値なので、「lock しただけの
      コピー」は noop のまま検出される (ロックで noop を回避できない)。
    - ただし**同名**の snapshot plugin との比較だけは
      `is_relock_transition` を例外にする。例外が無いと正式な再ロック経路
      (複製 → pin だけ I1→I2 → 提出) が必ず `noop_copy_of:S` で拒否される。
      「現在 inventory の hash」は `inventory.inventory` (最終 admit 済
      indicator) から引く。
    - 入力 (`source_snapshot_dir` / `examples_dir` / `inventory`) は**明示
      注入**する — 呼び出し元が持っている値を関数内で推測しない。

    AST 同一性は逐語コピー検出の下限。``pass`` 追加・注釈・import 順などの
    無害変形は検出対象外 (設計判断 2026-09-01)。
    """
    from agentic_fx.plugin.resolve import is_relock_transition, same_modulo_pins

    comparisons: list[tuple[str, Path, bool]] = []
    if examples_dir.is_dir():
        comparisons.extend(
            (f"_examples/{item.name}", item, False)
            for item in sorted(examples_dir.iterdir()) if item.is_dir())
    if source_snapshot_dir.is_dir():
        comparisons.extend(
            (item.name, item, item.name == name)
            for item in sorted(source_snapshot_dir.iterdir())
            if item.is_dir() and not item.name.startswith("_")
            and (item / "plugin.py").is_file())

    for label, comparison, is_same_name in comparisons:
        try:
            if not same_modulo_pins(plugin_dir, comparison):
                continue
            if is_same_name and is_relock_transition(
                    plugin_dir, comparison, inventory.inventory):
                continue
        except (OSError, UnicodeError, SyntaxError, yaml.YAMLError):
            continue
        return label
    return None
```

`src/agentic_fx/loops/improve_loop.py:1335` の呼び出しを更新:

```python
            noop_copy = find_noop_copy(
                plugin_dir, source_snapshot_dir=source_snapshot_dir,
                examples_dir=source_snapshot_dir / "_examples", name=name,
                inventory=inventory)
```

(`_run_plugin_gate` に `inventory` 引数を足し、`commit()` が `ctx.inventory` を渡す —
`ImproveRunContext.inventory` フィールド自体は本 task の頭で前倒し追加済み
(上記 Interfaces) だが、`prepare()` が実際に非空の値を書き込む配線は
**T5a Step 5-1 の仕事のまま**なので、**それまでは** `ctx.inventory` は
常に既定値 `None` — `self._inventory_for_gate(conn)` という private helper
(本 Step で定義。中身は `approved_plugins(conn, self._root / "plugins",
settings=self._settings)` を都度呼ぶだけ) へフォールバックする。P5
(`_check_duplicate_metrics_for_approval` 呼び出し) も同じ helper を共有する
(下記、codex plan r2 束3 Critical)。)

`_check_duplicate_metrics_for_approval` の冒頭に再ロック判定を足す:

```python
        if approval_payload.get("kind") != "strategy":
            return None
        # [indicator-consumption-wiring] §2.7 (opus I5): 再ロックのみの
        # 再提出は母集団から除外する — pin はハーネスの派生値であり、
        # 「同じ戦略を新しい indicator 版に貼り直しただけ」の再提出が
        # 成績一致で降格されると正式な再ロック経路が塞がる。
        deployed_dir = self._deployed_dir_for(approval_payload.get("name"),
                                              inventory=inventory)
        candidate_dir = self._candidate_dir_for(approval_payload,
                                                staging_dir=staging_dir)
        if (deployed_dir is not None and candidate_dir is not None
                and is_relock_transition(candidate_dir, deployed_dir,
                                        inventory.inventory)):
            return None
```

> **codex plan r1 C3 是正 — `inventory` と候補 dir は明示引数で渡す。**
> v1.2 の断片は `_check_duplicate_metrics_for_approval(conn, payload)` の中で
> `inventory.inventory` を読み、`_deployed_dir_for(name)` が
> `inventory.phase1_metas` を、`_candidate_dir_for(payload)` が `ctx.staging_dir` を
> 読むと書いていたが、**`inventory` も `ctx` も引数にも局所変数にも存在しない**
> (現行 `improve_loop.py:1939` の定義は `(self, conn, approval_payload)`。着手時に
> `rg -n 'def _check_duplicate_metrics_for_approval' -A 6 src/agentic_fx/loops/improve_loop.py`
> で再取得)。本番では `NameError`、テストは 2 つの helper を monkeypatch する
> ので**それを隠す**。
>
> **新しい契約 (すべて明示引数)**:
>
> ```python
>     def _check_duplicate_metrics_for_approval(
>             self, conn, approval_payload, *,
>             inventory: "InventoryBuildResult", staging_dir: Path,
>             ) -> "_DuplicateDemotion | None": ...
>             # ^ codex plan r2 束3 Minor: 実装 (Step 4-6a のテストも)
>             # 一貫して `_DuplicateDemotion | None` を返す — `str | None` は
>             # 既存契約 (`_finalize_success` の docstring・呼び出し元の
>             # `if demotion is not None:` 分岐) と食い違う誤記だった
>
>     def _deployed_dir_for(self, name, *, inventory) -> Path | None:
>         """`inventory.phase1_metas` のうち同名 strategy の `meta.path`。
>         見つからなければ `None`。"""
>
>     def _candidate_dir_for(self, approval_payload, *, staging_dir) -> Path | None:
>         """`staging_dir / approval_payload["name"]`。存在しなければ `None`。"""
> ```
>
> **codex plan r2 束3 Critical 是正 — `_finalize_success` から渡す `inventory`
> は `ctx.inventory` を直に渡さず、`None` フォールバックを挟む。**
> `ImproveRunContext.inventory` の 2 フィールドは本 task 冒頭で前倒し追加した
> (上記 Interfaces / Produces) が、`prepare()` が実際に非空の
> `InventoryBuildResult` を書き込む配線は**まだ T5a Step 5-1 の仕事のまま**
> — 本 task が終わった時点でも `ctx.inventory` は常に既定値 `None` である。
> `_finalize_success` が `inventory=ctx.inventory` をそのまま渡すと、strategy
> kind の承認で `_deployed_dir_for(name, inventory=None)` →
> `None.phase1_metas` の `AttributeError` になる。**`ctx.inventory` が
> `None` のときは Step 4-6c で定義済みの `self._inventory_for_gate(conn)`
> (`_run_plugin_gate` が既に使っている暫定 helper) へフォールバックする**
> — これにより本 task は T5a を待たずに自己完結して緑になり、T5a が実配線を
> 済ませた後は自然に `ctx.inventory` の方が使われる (フォールバック条件が
> `is None` なので、以後のコード変更は不要):
>
> ```python
>     demotion = self._check_duplicate_metrics_for_approval(
>         conn, approval_payload,
>         inventory=(ctx.inventory
>                    if ctx is not None and ctx.inventory is not None
>                    else self._inventory_for_gate(conn)),
>         staging_dir=(ctx.staging_dir if ctx is not None else None))
>     # `ctx is None` は `_finalize_success(ctx=None)` の直接呼び出し
>     # テスト専用経路 (本番は常に ctx を渡す) — その経路は
>     # `approval_payload["kind"] != "strategy"` のケースしか使っていない
>     # ため、`staging_dir=None` は `_candidate_dir_for` まで届かず安全
>     # (kind チェックが先に `return None` する)。
> ```
>
> **呼び出し元の移行表** (`rg -n '_check_duplicate_metrics_for_approval\(' src tests`
> の全結果。行番号は v1.3 執筆時点、着手時に再取得):
>
> | path:line | 移行 |
> |---|---|
> | `src/agentic_fx/loops/improve_loop.py:1939` | 定義 (上記の新シグネチャ) |
> | `src/agentic_fx/loops/improve_loop.py:2014` | `_finalize_success` 内。上記フォールバック式で `inventory=` / `staging_dir=` を渡す (`ctx` はこのスコープに**ある**ことを着手時に `sed -n '1990,2020p'` で確認すること。無ければ `_finalize_success` の引数まで遡って透通させる) |
> | `tests/loops/test_improve_loop_duplicate_metrics.py:358, 364` | `inventory=` / `staging_dir=` を足す (既存 2 本は依存なし strategy なので `_empty_inventory(tmp_path / "plugins")` でよい) |
>
> **`monkeypatch_dirs` は「注入シーム」ではなく「短絡」である**ことを明記する —
> 2 つの helper を差し替えたテストは helper の中身を検証していない。
> **実 helper を使う統合テストを 1 本足す** (下の
> `test_relock_detection_uses_the_real_dir_helpers`)。**さらに、`ctx.inventory`
> が `None` のままでも P5 が `_inventory_for_gate(conn)` フォールバックで
> 正しく動くことを** `test_finalize_success_falls_back_to_inventory_for_gate_
> when_ctx_inventory_is_none` **で 1 本 pin する** (`prepare_ctx` 実経路は
> `ctx.inventory` が非 None になるのは T5a 以降なので、本 task の時点では
> `synthetic_ctx` または `dataclasses.replace(ctx, inventory=None)` で明示的に
> `None` を再現し、それでも再ロック除外判定が機能することを確認する)。

- [ ] **Step 4-6d: Run test to verify it passes**

Run: `uv run pytest tests/loops tests/plugin -q`
Expected: PASS

- [ ] **Step 4-6e: Commit**

```bash
git add src/agentic_fx/plugin/noop_gate.py src/agentic_fx/loops/improve_loop.py tests/loops
git commit -m "feat(noop): compare modulo pins with a relock exception (P4/P5)"
```

### Step 4-7: reconcile の pin 破れ revert (R2)

- [ ] **Step 4-7a: Write the failing test**

`tests/plugin/test_reconcile.py` に追記:

```python
def test_switched_journal_with_broken_pin_is_reverted(tmp_path):
    """R2: switch 直後 (journal `switched`、新 version dir 作成済) で停止 →
    依存 indicator を更新・承認 → 再起動 → `require` 解決に失敗 →
    live symlink は旧 target、journal `reverted`、approval は `pending`、
    **新 version dir は `.versions` に残る**、activity 逐語。"""
    conn, plugins_root, activity = _reconcile_env(tmp_path)   # tests.fixtures.wiring_envs (T6b)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    old_target, new_target, approval_id, op_id = _stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    # live は既に新 target を指している (switched)
    assert (plugins_root / "rsi_pullback").readlink().as_posix() == new_target
    new_version_dir = plugins_root / new_target
    assert new_version_dir.is_dir()

    _bump_indicator_version(conn, plugins_root, "rsi", now=fx.NOW)  # I1 -> I2

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=fx.NOW, settings=SETTINGS,
        activity=activity)

    assert (plugins_root / "rsi_pullback").readlink().as_posix() == old_target
    assert conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id=?",
        (op_id,)).fetchone()["phase"] == "reverted"
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "pending"
    assert new_version_dir.is_dir()      # 回収は本束の範囲外 (codex r8 I2)
    assert ("switch_reverted reason=indicator_unresolved alias=rsi "
            "cause=pin_mismatch") in _activity_text(activity)


def test_switched_journal_with_intact_pin_proceeds_to_decided(tmp_path):
    """R2 の裏: pin が破れていなければ従来どおり `decided` まで進む。"""
    conn, plugins_root, activity = _reconcile_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _old, _new, approval_id, op_id = _stage_switched_journal(
        conn, plugins_root, name="rsi_pullback",
        pins={"rsi": hashes["rsi"]}, now=fx.NOW)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=fx.NOW, settings=SETTINGS,
        activity=activity)
    assert conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id=?",
        (op_id,)).fetchone()["phase"] == "decided"
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "approved"
```

- [ ] **Step 4-7b: Run test to verify it fails**

Run: `uv run pytest tests/plugin/test_reconcile.py -k broken_pin -q`
Expected: FAIL — 現行 reconcile は `live_target == new_norm` で無条件に
`retry_approval` へ進み、`decided` になる

- [ ] **Step 4-7c: Write minimal implementation**

`src/agentic_fx/plugin/switch.py::reconcile_switch_journals` の
`if live_target == new_norm:` 分岐の先頭に挿入:

```python
            if live_target == new_norm:
                # [indicator-consumption-wiring] §2.4 (codex r5 I3):
                # switched (symlink 切替済・DB decided 前) のまま停止し、
                # 再起動までに indicator が更新されて pin が破れた場合、
                # そのまま decided にすると **live に未解決の strategy が
                # 残る**。当該 strategy + 依存名の lock 下で `require`
                # 解決を再試行し、失敗なら旧 target へ原子的に戻して
                # journal を `reverted`、approval は `pending` のまま残す
                # (人間が再ロック → 再承認)。新 version dir は回収しない
                # (現行 reconcile も version store を回収しない — GC は
                # 既存の archive/versions 規律に任せる、codex r8 I2)。
                unresolved = _unresolved_after_switch(
                    conn, row, plugins_root=plugins_root, settings=settings)
                if unresolved is not None:
                    alias, reason = unresolved
                    _revert_one(conn, row, plugins_root=plugins_root, now=now,
                                activity=activity)
                    conn.commit()
                    if activity is not None:
                        activity.write(
                            Category.APPROVAL, "switch_reverted",
                            f"name={row['name']} op_id={row['op_id']} "
                            f"reason=indicator_unresolved alias={alias} "
                            f"cause={reason}")
                    continue
                retry_approval(...)   # 現行のまま
```

同ファイルに helper を追加:

```python
def _unresolved_after_switch(conn: sqlite3.Connection, row: dict, *,
                             plugins_root: Path, settings
                             ) -> tuple[str, str] | None:
    """switched 行の新 target (= live が既に指している版) を `require` で
    解決し直す。解決できれば `None`、できなければ `(alias, reason)`。
    strategy 以外・meta を読めない場合は `None` (従来の収束規則に委ねる)。"""
    version_dir = (plugins_root / row["new_target"]).resolve()
    meta = loader._discover_one(version_dir, row["name"])
    if meta is None or meta.kind != "strategy":
        return None
    # **TOCTOU 窓の明記 (opus r1 M13)**: 自己デッドロックは起きない —
    # `rg -n '_plugin_lock\(' src/agentic_fx/plugin/switch.py` の実測では
    # 定義 1 + 入口 4 (submit / approve / bless ほか) で入れ子は無く、
    # `retry_approval` → `approve_candidate` も lock を取るのは 1 回。
    # そのため本 helper は with を**抜けてから** `retry_approval` を呼ぶ
    # 設計で正しい。ただし **lock 解放から `retry_approval` が lock を
    # 取り直すまでの窓**で、別プロセスが依存 indicator を承認して pin を
    # 破り得る。その場合は `approve_candidate` 側の決定時 `require` 解決
    # (Step 4-4) が `pin_mismatch` で弾き、approval は pending のまま残る
    # (= 多層防御で fail closed)。**この窓を塞ぐために本 helper と
    # `retry_approval` を同一 lock 内へまとめてはならない** (approve 経路の
    # lock 取得と二重になる)。
    with _plugin_locks(plugins_root, _dependency_names(version_dir, row["name"])):
        inventory = tools_plugin_loader.approved_plugins(
            conn, plugins_root, settings=settings)
        try:
            resolve_indicator_deps(meta, inventory.inventory, settings=settings,
                                   pin_mode="require")
        except IndicatorResolutionError as exc:
            return (exc.alias or "-", exc.reason)
    return None
```

- [ ] **Step 4-7d: Run test to verify it passes**

Run: `uv run pytest tests/plugin/test_reconcile.py -q`
Expected: PASS

- [ ] **Step 4-7e: Commit**

```bash
git add src/agentic_fx/plugin/switch.py tests/plugin/test_reconcile.py
git commit -m "feat(reconcile): revert switched journals whose dependency pin broke (R2)"
```

### Step 4-8: 承認詳細の依存 strategy 2 欄 (D1) と pin 検算 (P2)

- [ ] **Step 4-8a: Write the failing test**

`tests/test_commands.py` に追記:

```python
def test_indicator_approval_detail_lists_dependent_strategies_in_two_columns(
        tmp_path):
    """D1: (i) この候補の hash に pin 済み / (ii) 同名 indicator の別 hash に
    pin (承認すると外れる) の 2 欄。決定順 (id) で表示。"""
    shell, conn, plugins_root = _shell_env(tmp_path)     # tests.fixtures.wiring_envs (T6b)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _deploy_strategy(conn, plugins_root, "s_old", pins={"rsi": hashes["rsi"]})
    i2_id, i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")

    out = shell._approval_detail(i2_id)

    assert "dependent_pinned_here=" in out
    assert "dependent_pinned_elsewhere=s_old" in out
    assert "dependent_pinned_here=-" in out   # まだ誰も I2 に pin していない


def test_dependent_strategy_moves_to_the_first_column_after_relock(tmp_path):
    shell, conn, plugins_root = _shell_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _deploy_strategy(conn, plugins_root, "s_old", pins={"rsi": hashes["rsi"]})
    i2_id, i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")
    _approve_indicator_v2(conn, plugins_root, "rsi", approval_id=i2_id)
    # s_old は pin 破れで inventory から外れる → (ii) 欄に残る
    assert "dependent_pinned_elsewhere=s_old" in shell._approval_detail(i2_id)
    # 再ロックした s_new を配備すると (i) 欄へ移る
    _deploy_strategy(conn, plugins_root, "s_new", pins={"rsi": i2_hash})
    out = shell._approval_detail(i2_id)
    assert "dependent_pinned_here=s_new" in out


def test_dependent_strategies_are_listed_in_decision_id_order(tmp_path):
    """D1 (順序、**codex plan r1 I9**): 表示順は**決定順 (最新承認の
    approval id 昇順)** であり、plugin 名の辞書順ではない。

    v1.2 のテストは各欄 1 件ずつしか置いておらず、実装が inventory の
    列挙順をそのまま返していても緑だった。**名前昇順と決定順が逆になる
    fixture** を作って差を出す: `z_first` を先に承認し、`a_second` を
    後に承認する → 決定順は `[z_first, a_second]`、名前順は
    `[a_second, z_first]`。"""
    shell, conn, plugins_root = _shell_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    # 承認順 = z_first → a_second (`_deploy_strategy` は approval 行を作る)
    _deploy_strategy(conn, plugins_root, "z_first", pins={"rsi": hashes["rsi"]})
    _deploy_strategy(conn, plugins_root, "a_second", pins={"rsi": hashes["rsi"]})
    i2_id, _i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")

    here, elsewhere = shell._dependent_strategies(
        indicator_name="rsi", candidate_hash=hashes["rsi"])

    assert here == ["z_first", "a_second"]       # 決定順 (名前順なら逆)
    assert elsewhere == []
    # 表示にも同じ順序で出る
    out = shell._approval_detail(i2_id)
    assert "dependent_pinned_elsewhere=z_first, a_second" in out
```

(`_deploy_strategy` が approval 行を `approved` で作ることを T6b の
Produces で保証する。作っていなければ `approvals.create` +
`apply_decision(status="approved")` をビルダに足すこと — 本テストは
**その id 順**を読む。)

`tests/plugin/test_switch_paths.py` に追記 (P2):

```python
def _seed_approved_in_sample_row(conn, *, content_hash, pair="USDJPY",
                                 trades=40, pf=1.5, avg_r=0.2):
    """[codex plan r1 C1] 「既承認候補の in_sample 行」を任意の
    `content_hash` で 1 本入れる (`find_matching_approved_metrics` の母集団)。

    `tests/loops/test_improve_loop_duplicate_metrics.py` の
    `_seed_approved_candidate_metrics` は `content_hash` を `"d"*64` に
    固定しているので流用できない。`br.save_harness_run` の引数は
    そちらから逐語転写する (着手時に
    `rg -n 'def save_harness_run' -A 12 src/agentic_fx/store/backtest_runs.py`
    で再確認)。"""
    from datetime import timedelta

    from agentic_fx.store import approvals, backtest_runs as br
    aid = approvals.create(conn, "plugin",
                           {"name": "rsi_pullback", "kind": "strategy",
                            "content_hash": content_hash}, fx.NOW)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t",
                             now=fx.NOW)
    br.save_harness_run(
        conn, scope="in_sample", plugin_ref="plugins/rsi_pullback",
        content_hash=content_hash, kind="strategy", pair=pair, timeframe="1h",
        source="dukascopy", base_interval="5m", params={},
        period=(fx.NOW - timedelta(days=90), fx.NOW),
        metrics={"trades": trades, "pf": pf, "win_rate": 0.5, "avg_r": avg_r,
                 "max_drawdown": 0.05, "total_pnl": 100.0, "evaluable": True,
                 "fallback_spread_used": False},
        settings_hash="h", core_commit="c", initial_balance=1_000_000.0,
        now=fx.NOW, variant="candidate")


def test_relock_creates_a_new_hash_that_never_collides_with_the_old_rows(
        tmp_path, monkeypatch):
    """P2 (r3 C1 シナリオの検算): S(pin I1) approved → I2 承認 → S 除外 →
    再ロック → hash 変化 → approval / signals / backtest_runs が新 hash で
    旧行と分離される。`find_matching_approved_metrics` は仕様どおり旧 hash
    を返す (重複検出 API — caller 側の無視は P5 で検証)。

    **gate double + 成績行の明示 seed が唯一の自己整合形** (codex plan r1
    C1): (a) `switch_env` は空 DB なので実 gate は `NoHistoryError`、
    (b) `fx.seed_history` で履歴を入れて実 gate を回すと、fixture の
    実測成績は **102 opens** であって `trades=40 / pf=1.5 / avg_r=0.2` には
    ならないので下の `find_matching_approved_metrics(...)` の逐語 assert が
    成立しない (`tmp/plan-indicator-wiring/probe_fixture.txt`)。
    本テストが見るのは **hash による行分離**であって成績の中身ではないので、
    gate は double にし、母集団の行は `_seed_approved_in_sample_row` で
    `old_hash` に対して明示的に入れる。"""
    from agentic_fx.store import backtest_runs as br_store
    from agentic_fx.store import signals as signals_store
    from tests.fixtures import indicator_wiring as fx
    conn, plugins_root = _switch_env(tmp_path)
    _install_gate_double(monkeypatch)
    fx.write_indicator(plugins_root, "rsi")
    i1 = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)["rsi"]
    old_dir = fx.write_rsi_pullback(plugins_root / "_human", pins={"rsi": i1})
    old_hash = plugin_loader.content_hash(old_dir)
    old_approval = plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    plugin_switch.approve_candidate(
        conn, old_approval, decided_by="human_cli", now=fx.NOW,
        plugins_root=plugins_root, settings=SETTINGS_FIXTURE)
    # 母集団: 旧 hash に対する承認済 in_sample 行 (gate double は
    # `backtest_runs` を書かないので明示的に入れる — codex plan r1 C1)
    _seed_approved_in_sample_row(conn, content_hash=old_hash)
    i2 = _bump_indicator_version(conn, plugins_root, "rsi", now=fx.NOW)
    # 再ロック (pin I1 -> I2) で hash が変わる
    new_dir = fx.write_rsi_pullback(plugins_root / "_human2", pins={"rsi": i2})
    new_hash = plugin_loader.content_hash(new_dir)
    plugin_switch.submit_candidate(
        conn, name="rsi_pullback", staging_dir=plugins_root / "_human2",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS_FIXTURE, now=fx.NOW)
    assert new_hash != old_hash
    # submit 2 本 + `_seed_approved_in_sample_row` が作る母集団行 1 本 = 3
    # (codex plan r1 C1: v1.2 は seed 行が無い前提で 2 を pin していた)。
    # **hash ごとに 1 行ずつ分離されている**ことを直接見る:
    hashes_in_approvals = [
        r[0] for r in conn.execute(
            "SELECT json_extract(payload_json,'$.content_hash') "
            "FROM approval_requests "
            "WHERE json_extract(payload_json,'$.name')='rsi_pullback' "
            "ORDER BY id")]
    assert old_hash in hashes_in_approvals and new_hash in hashes_in_approvals
    assert len(set(hashes_in_approvals)) == len(hashes_in_approvals)
    # signals は (plugin, content_hash, pair, timeframe, bar_ts) UNIQUE なので
    # 同じ bar_ts で 2 行挿入できる
    assert signals_store.add(conn, plugin="rsi_pullback", content_hash=old_hash,
                             pair="USDJPY", timeframe="1h", bar_ts=BAR_TS,
                             kind="strategy", payload={}, now=NOW) is not None
    assert signals_store.add(conn, plugin="rsi_pullback", content_hash=new_hash,
                             pair="USDJPY", timeframe="1h", bar_ts=BAR_TS,
                             kind="strategy", payload={}, now=NOW) is not None
    # opus r1 I7 是正: 現物は
    # `latest_in_sample_metrics(conn, content_hash, *, pair, variant, source,
    # base_interval)` で 3 引数が必須 (着手時に
    # `rg -n 'def latest_in_sample_metrics' -A 3 src/agentic_fx/store/backtest_runs.py`
    # で再取得)。直下の `find_matching_approved_metrics` は揃っているので
    # 転写時の取りこぼしだった。
    assert br_store.latest_in_sample_metrics(
        conn, new_hash, pair="USDJPY", variant="candidate",
        source="dukascopy", base_interval="5m") is None
    assert br_store.find_matching_approved_metrics(
        conn, pair="USDJPY", variant="candidate", source="dukascopy",
        base_interval="5m", trades=40, pf=1.5, avg_r=0.2) == old_hash
```

- [ ] **Step 4-8b: Run test to verify it fails**

Run: `uv run pytest tests/test_commands.py -k dependent tests/plugin/test_switch_paths.py -k relock_creates -q`
Expected: FAIL — `_approval_detail` に依存欄が無い

- [ ] **Step 4-8c: Write minimal implementation**

`src/agentic_fx/commands.py::_approval_detail` に追記
(`profitability_floor` 行の後、`_archive_line` の前):

```python
        # [indicator-consumption-wiring] §2.7 (codex r4 M1): indicator の
        # 承認詳細に依存 strategy を 2 欄で列挙する。**payload には入れない**
        # (表示時に `InventoryBuildResult` を逆引きする — payload は承認時点の
        # snapshot であり、承認待ちの間に依存関係が変わるため)。
        # (i) この候補の hash に pin 済み = `inventory.metas` のうち当該
        #     alias の pin が候補 `content_hash` と一致する strategy
        # (ii) 同名 indicator の別 hash に pin (承認すると外れる) =
        #     `phase1_metas` のうち同名 indicator への pin が候補 hash と不一致
        if payload.get("kind") == "indicator":
            here, elsewhere = self._dependent_strategies(
                indicator_name=payload.get("name"),
                candidate_hash=payload.get("content_hash"))
            lines.append(f"dependent_pinned_here={', '.join(here) or '-'}")
            lines.append(
                f"dependent_pinned_elsewhere={', '.join(elsewhere) or '-'}")
```

同クラスに helper を追加:

```python
    def _dependent_strategies(self, *, indicator_name, candidate_hash
                              ) -> tuple[list[str], list[str]]:
        """`_approval_detail` の 2 欄を作る。**決定順 (最新承認の approval
        `id` の昇順)** で並べる。

        **codex plan r1 I9 是正**: v1.2 の実装断片は inventory の列挙順を
        そのまま返しており、approval id を取得も sort もしていなかった
        (docstring だけが「決定順」と主張していた)。テストも各欄 1 件ずつ
        だったので順序違反を検出できなかった。**実際に id で並べる**。

        `approval_requests` に `name` 列は無い (`kind` / `payload_json` /
        `status` …、`src/agentic_fx/store/db.py:264-272`) ので、
        `json_extract(payload_json,'$.name')` で引く。

        inventory 構築に失敗した場合は両方空 (表示は fail-soft — 承認詳細の
        表示が inventory の不調で落ちない)。

        **opus r1 C4 ③ 是正**: `Commands` に `root` 属性は**無い**
        (`__init__` は `conn / state_store / broker / trade_loop / activity /
        log_dir / clock` 必須 + `plugins_root` / `settings` / `health_latch` /
        `improve_supervisor` / `policy_path` 任意)。plugin の root は
        **`self.plugins_root`** (既に `plugin` 系コマンドが使っている)。
        `plugins_root` / `settings` は任意引数なので `None` があり得る —
        その場合も両方空を返す (fail-soft)。
        """
        from agentic_fx.tools import plugin_loader as tools_plugin_loader
        if self.plugins_root is None or self.settings is None:
            return [], []
        try:
            result = tools_plugin_loader.approved_plugins(
                self.conn, self.plugins_root, settings=self.settings)
        except Exception:  # noqa: BLE001 — 表示は fail-soft
            return [], []

        def _pins_to(meta) -> list[str]:
            return [ref.pin for ref in meta.indicators
                    if ref.plugin == indicator_name and ref.pin is not None]

        # [codex plan r1 I9] 各 strategy 名の「最新承認 approval id」を
        # 1 クエリで引き、その昇順 = 決定順に並べる。承認行が無い名前
        # (まだ承認されていない配備物) は id を持たないので**末尾**に、
        # その中では名前昇順で安定させる。
        decided = {
            row["name"]: row["last_id"] for row in self.conn.execute(
                "SELECT json_extract(payload_json,'$.name') AS name, "
                "       MAX(id) AS last_id "
                "FROM approval_requests "
                "WHERE status='approved' "
                "  AND json_extract(payload_json,'$.kind')='strategy' "
                "GROUP BY name")
            if row["name"] is not None}

        def _in_decision_order(names: list[str]) -> list[str]:
            return sorted(names,
                          key=lambda n: (decided.get(n) is None,
                                         decided.get(n, 0), n))

        here = _in_decision_order(
            [m.name for m in result.inventory.metas
             if m.kind == "strategy" and candidate_hash in _pins_to(m)])
        elsewhere = _in_decision_order(
            [m.name for m in result.phase1_metas
             if m.kind == "strategy"
             and any(p != candidate_hash for p in _pins_to(m))])
        return here, elsewhere
```

- [ ] **Step 4-8d: Run test to verify it passes**

Run: `uv run pytest tests/test_commands.py tests/plugin -q && uv run pytest -q`
Expected: PASS

- [ ] **Step 4-8e: Commit**

```bash
git add src/agentic_fx/commands.py tests
git commit -m "feat(commands): list dependent strategies in indicator approval detail (D1/P2)"
```

**T4b 完了条件**:
- [ ] **A2**: submit 後の indicator 更新 → approve で
      `ValueError("indicator_unresolved:rsi:pin_mismatch")`、pending のまま、
      `.versions` に新版なし、symlink 不変
- [ ] **P2**: 再ロックで hash が変わり、approval / signals / `backtest_runs` の各行が
      新 hash で旧行と分離される。`find_matching_approved_metrics` は旧 hash を返す
- [ ] **P2'** (設計書 v1.3 §6 で置換): `_plugin_lock` の取得順序が
      `approve_candidate` の実行経路で常に**名前昇順・重複なし**であることを
      spy で pin する (順序が崩れる変異を検出) —
      `test_plugin_lock_order_for_approve_candidate_is_sorted_unique`。
      並行実行で S の approve 中に I2 の承認が待たされる事実 (相互排除の成立)
      は `test_dependency_lock_blocks_a_concurrent_indicator_approval`
      (スレッド 2 本 + 別 fd の `flock` 競合) が引き続き別途検証する
- [ ] **P2''**: 同一 indicator を 2 alias で参照する strategy の approve で lock 取得が
      1 回。bless の事前読取 → lock 間の差し替えは `candidate_changed`、approval 行 0、
      `.versions` / symlink 不変、全 lock 解放
- [ ] **P3 (payload)**: 三経路の `indicator_deps` が同形の plain object
- [ ] **P3'**: resolver 呼び出しが候補ごとに 1 回。in_sample session / holdout session /
      `GateOutcome.resolved` / payload の元が同一オブジェクト (`is`)
- [ ] **P4**: lock しただけの example コピーは `noop_copy_of:_examples/rsi_pullback`、
      正式な再ロックは noop にならず、再ロックなしの複製は `noop_copy_of:S`
- [ ] **P5**: 再ロックのみの再提出は `duplicate_metrics_of` で降格されない
      (再ロックでない一致は従来どおり降格する)。`ctx.inventory` が (本 task
      時点では常に) `None` のときも `_finalize_success` は
      `self._inventory_for_gate(conn)` へフォールバックして同じ判定ができる
      (`test_finalize_success_falls_back_to_inventory_for_gate_when_ctx_
      inventory_is_none`。codex plan r2 束3 Critical)
- [ ] **D1**: I2 承認前 = (ii) 欄に S、承認後も (ii) 欄に残る、S 再ロック後 = (i) 欄に S'。
      `_dependent_strategies` は `self.plugins_root` を読む (`self.root` は存在しない —
      opus r1 C4)。`plugins_root` / `settings` が `None` のときは両欄空 (fail-soft)
- [ ] **R2**: switched + pin 破れ → 旧 target へ戻り journal `reverted`、approval
      `pending`、新 version dir は `.versions` に残る、activity 逐語。
      pin が破れていなければ `decided` (この裏側は T6b の
      `test_stage_switched_journal_reconciles_to_decided_when_pin_intact` が据える)
- [ ] 段 0 変異 red:
      (c) `_plugin_locks` の `sorted(set(...))` を `list(names)` にする → P2'' が落ちる
      (d) `approve_candidate` の `require` 解決を `check` にする → A2 が落ちる
      (e) `find_noop_copy` の `is_relock_transition` 例外を削る →
      `test_relocked_copy_of_a_deployed_strategy_is_not_a_noop` が落ちる
      (f) reconcile の `_unresolved_after_switch` を `return None` に潰す → R2 が落ちる
      (g) `_finalize_success` の `inventory=` フォールバック式から
      `self._inventory_for_gate(conn)` 分岐を削り `ctx.inventory` を直渡しにする →
      `test_finalize_success_falls_back_to_inventory_for_gate_when_ctx_inventory_is_none`
      が `AttributeError` で落ちる (codex plan r2 束3 Critical)

---

## T5a: 改善 context の inventory 露出と RPC 予約 [improve-loop-exposure-1]

**対応**: 設計書 §2.9 全体、§2.1 の再ロック手順 (worker 側)、§4 の `improve_loop.py` /
`improve_run_context.py` / `improve_rpc_tools.py` / `mission_counters.py` /
`improve_staging_tools.py` / `mission_registry.py` / `improve_mission.md` /
`improve_context.py` 行。
**完了条件の受入 ID**: F4 (counters / RPC 部分) / P1 (改善経路) / P1' /
P3 (露出部分)。

> **opus r1 観点 7 で旧 T5 (6 Step / 約 1,200 行 / src 10 ファイル) を
> T5a (Step 5-1〜5-3) と T5b (Step 5-4〜5-6) に分割した。** 境界は
> 「露出面 (context / tool / counters) が確定したところ」。

**着手条件**: T4a・T4b・T6b が main にマージ済み。**worktree 並列不可**。

**Files:**
- (`src/agentic_fx/loops/improve_run_context.py` の 2 フィールド追加
  (`inventory` / `inventory_view`、互換既定値付き) は **T4b へ前倒し済み**
  — codex plan r2 束3 Critical。本 task は Modify しない。**Consumes** 側
  参照)
- Modify: `tests/fixtures/wiring_envs.py` (`synthetic_ctx` が新 2 フィールドを
  明示的に埋める。T6b は既にマージ済なのでここでは Modify — codex plan r1 C4)
- Modify: `src/agentic_fx/loops/improve_loop.py:396-410` (`ImproveRunContext` 構築)、
  `:819-850` (`_materialize_workspace`)、`:864-1000` (`run_backtest_handler`)、
  `:1330-1340` (`_run_plugin_gate`)、`:1402-1441` (`_build_approval_payload`)、
  `:2240-2300` (`commit` の gate)、`:2865-2915` (`_finalize_gate_failed`)
- Modify: `src/agentic_fx/tools/mission_counters.py:147-158`
- Modify: `src/agentic_fx/tools/improve_rpc_tools.py:117-162`
- Modify: `src/agentic_fx/tools/improve_staging_tools.py`
- Modify: `src/agentic_fx/tools/mission_registry.py:36-115`
- Modify: `src/agentic_fx/mission_worker.py:532-570`、`:692-696`
- Modify: `src/agentic_fx/runners/worker_runner.py:208-222`
- Modify: `src/agentic_fx/loops/prompts/improve_mission.md:40-80`
- Modify: `src/agentic_fx/loops/improve_context.py:81-92`
- Test: `tests/loops/test_improve_e2e.py`、`tests/tools/test_improve_rpc_tools.py`、
  `tests/tools/test_improve_staging_tools.py`、`tests/tools/test_mission_counters.py`、
  `tests/loops/test_improve_loop_source_snapshot.py`、
  `tests/integration/test_improve_forbidden_regression.py`

**Interfaces:**

- Consumes: T1 の `InventoryBuildResult` / `lock_config` / `resolve_indicator_deps` /
  `IndicatorResolutionError`、T3 の `build_intent_source(..., resolved)`、
  T4a の `run_kind_gate(..., inventory)` / `GateOutcome.resolved` / `.cpu_samples`、
  **T4b の `ImproveRunContext.inventory` / `.inventory_view` (2 フィールド、
  互換既定値付き — codex plan r2 束3 Critical で前倒し。本 task は
  この 2 フィールドへ実際に非空の値を書き込む「生成元」を配線するだけで、
  フィールド定義そのものは変更しない)**、
  T6b の `tests.fixtures.wiring_envs` (`improve_env` / `prepare_ctx` /
  `rpc_tooldefs` / `rpc_tools` / `loop_env`)。
  (`evaluate_strategy_adoption_gate(..., resolved, inventory)` /
  `find_noop_copy(..., examples_dir, inventory)` は **T5b** の Consumes)
- Produces:

```python
# src/agentic_fx/loops/improve_run_context.py — 定義そのものは T4b の Produces
# (2 フィールドは既に存在する)。本 task が変えるのは「誰が inventory / view
# に非空の値を書き込むか」だけ — `prepare()` が唯一の生成元になる。

# src/agentic_fx/tools/mission_counters.py
class MissionToolCounters:
    def release_backtest(self, name: str) -> None: ...
        # lock 内で backtest_calls[name] -= 1 (0 未満にしない)。
        # successful_backtests は触らない。

# src/agentic_fx/tools/improve_staging_tools.py
def build_improve_staging_tooldefs(*, staging_dir, source_snapshot_dir,
                                   counters=None, budget=None,
                                   inventory_view: dict | None = None
                                   ) -> list[ToolDef]: ...
# 追加 tool:
#   list_deployed_plugins() -> {"plugins": [{"name","kind","pairs","params",
#                                            "outputs","content_hash"}],
#                               "pin_broken_strategies": [{"name","alias","reason"}]}
#   lock_staging_deps(name) -> {"ok": true, "pins": {...}, "changed": bool,
#                               "diff": "..."} | {"error": "<fixed text>", ...}

# inventory_view の形 (JSON-safe、親が 1 回だけ生成)
# {"plugins": [...], "pin_broken_strategies": [...]}
```

### Step 5-1: `ImproveRunContext.inventory` と snapshot 材料 (P1')

- [ ] **Step 5-1a: Write the failing test**

`tests/loops/test_improve_loop_source_snapshot.py` に追記:

```python
def test_pin_broken_strategy_stays_in_the_snapshot_but_not_in_the_inventory(
        tmp_path):
    """P1': phase 2 で落ちた strategy は `_snapshot_src` に残り
    `read_plugin_source` で読めるが、inventory には出ない。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)         # tests.fixtures.wiring_envs (T6b)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _deploy_strategy(conn, plugins_root, "rsi_pullback",
                     pins={"rsi": "a" * 64})          # pin 破れ
    ctx = _prepare_ctx(loop, now=fx.NOW)

    snapshot = ctx.source_snapshot_dir
    assert (snapshot / "rsi_pullback" / "plugin.py").is_file()
    assert [p["name"] for p in ctx.inventory_view["plugins"]] == ["rsi"]
    assert ctx.inventory_view["pin_broken_strategies"] == [
        {"name": "rsi_pullback", "alias": "rsi", "reason": "pin_mismatch"}]
    assert [m.name for m in ctx.inventory.inventory.metas] == ["rsi"]
    assert sorted(m.name for m in ctx.inventory.phase1_metas) == \
        ["rsi", "rsi_pullback"]


def test_prompt_shows_the_number_of_pin_broken_strategies(tmp_path):
    """P1': prompt に「pin 破れで配備から外れている strategy: N 本 (名前)」。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _deploy_strategy(conn, plugins_root, "rsi_pullback",
                     pins={"rsi": "a" * 64})          # pin 破れ
    ctx = _prepare_ctx(loop, now=fx.NOW)
    rendered = loop._last_rendered_prompt
    # `_last_rendered_prompt` が無ければ `ImproveLoop._render_improve_mission_prompt`
    # の戻り値を `self._last_rendered_prompt` に保持する 1 行を同じコミットで足す
    # (テスト専用の観測面。本番挙動は変わらない)。
    assert "pin 破れ" in rendered and "rsi_pullback" in rendered


def test_inventory_view_is_generated_once_from_the_same_result(tmp_path):
    """P3: view は `ImproveRunContext.inventory` と同じ
    `InventoryBuildResult` から 1 回だけ生成される (prepare 後に live
    `plugins/` を差し替えても不変)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    before = json.dumps(ctx.inventory_view, sort_keys=True)
    fx.write_indicator(plugins_root, "adx")
    fx.deploy_approved(conn, plugins_root, ["adx"], now=fx.NOW)
    assert json.dumps(ctx.inventory_view, sort_keys=True) == before
```

- [ ] **Step 5-1b: Run test to verify it fails**

Run: `uv run pytest tests/loops/test_improve_loop_source_snapshot.py -k "pin_broken or inventory_view" -q`
Expected: FAIL — **codex plan r2 束3 Critical 是正**: `ImproveRunContext.inventory` /
`.inventory_view` フィールドは T4b で前倒し追加済みなので、もはや
`AttributeError: ... no attribute 'inventory'` にはならない。`prepare()` が
まだ実 inventory を書き込まない (本 Step の実装対象) ため、既定値のまま
`ctx.inventory is None` / `ctx.inventory_view == {}` になり、
`test_pin_broken_strategy_stays_in_the_snapshot_but_not_in_the_inventory` は
`ctx.inventory_view["plugins"]` で `KeyError: 'plugins'` に、
`test_prompt_shows_the_number_of_pin_broken_strategies` は prompt に
「pin 破れ」が出ない `AssertionError` になる (どちらも同じ根: `prepare()` が
`inventory=` / `inventory_view=` を渡していない)

- [ ] **Step 5-1c: Write minimal implementation**

**`src/agentic_fx/loops/improve_run_context.py` の 2 フィールドは T4b で
追加済みなので、本 Step では触らない** (codex plan r2 束3 Critical — 前倒し
の詳細と根拠は T4b Step 4-6c を参照)。本 Step がやるのは「誰が
`inventory=` / `inventory_view=` を渡すか」の配線だけ:

**`tests/fixtures/wiring_envs.py::synthetic_ctx` を同じコミットで更新する**
(codex plan r1 C4): `prepare_ctx` が成立しない環境での fallback なので、
既定値のままだと T5b の consumer (`ctx.inventory.inventory` を読む) が
`AttributeError` になる。**空の `InventoryBuildResult` と空 view を明示的に
渡す**:

```python
def synthetic_ctx(loop, conn, root):
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
    empty = InventoryBuildResult(
        inventory=ApprovedInventory(root=(root / "plugins").resolve(), metas=()),
        phase1_metas=(), resolved={}, rejected_strategies=())
    return ImproveRunContext(
        ...,                                   # 既存の 8 引数はそのまま
        inventory=empty,
        inventory_view={"plugins": [], "pin_broken_strategies": []})
```

**`_build_rpc_handlers` にも既定値を付ける** (codex plan r1 C4): 現行
`rg -c '_build_rpc_handlers\(' src tests` = **33 箇所** (src 2 = 定義 + `:402`、
tests 31)。必須引数にすると 31 箇所が `TypeError` になるので、

```python
    def _build_rpc_handlers(self, ledger, *, staging_dir,
                            inventory: "InventoryBuildResult | None" = None):
        # [codex plan r1 C4] 既定 `None` = 「解決できる依存が 1 本も無い」。
        # 依存を持つ候補は `not_found` → `started:false` (fail closed) に
        # なるので、既存 31 テストの意味は変わらない (どれも依存なし候補)。
```

とし、`prepare` (`:402`) だけが `inventory=inventory_result` を渡す。

- [ ] **Step 5-1a の追加テスト (codex plan r1 C4)**: 既定値を置いたことで
      「誰も inventory を渡さないまま緑」になる穴を塞ぐ。**実 `prepare` 経路が
      非空の inventory を持つこと**を pin する:

```python
def test_prepare_populates_a_non_empty_inventory(tmp_path):
    """codex plan r1 C4: `ImproveRunContext.inventory` は互換のため
    `None` 既定だが、**実 `prepare` 経路では必ず非空**であること。
    (既定値だけ足して `prepare` の更新を忘れる変異を検出する。)"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    assert ctx.inventory is not None
    assert [m.name for m in ctx.inventory.inventory.metas] == ["rsi"]
    assert ctx.inventory_view["plugins"][0]["name"] == "rsi"
    # handlers も同じ inventory を握っている (既定 None のまま作られていない)
    assert ctx.rpc_handlers["run_backtest"] is not None
```

`src/agentic_fx/loops/improve_loop.py::_materialize_workspace` を
`(staging_dir, source_snapshot_root, inventory_result)` を返す形にし、
`prepare` の `ImproveRunContext(...)` へ渡す:

```python
        plugins_dir = self._root / "plugins"
        inventory_result = approved_plugins(conn, plugins_dir,
                                            settings=self._settings)
        # [indicator-consumption-wiring] §2.1 / §2.3: snapshot の材料は
        # **第 1 相** (pin 破れ strategy を含む) — 再ロック経路
        # (`read_plugin_source` → staging へ複製 → `lock_staging_deps`)
        # のために残す。inventory としては admit しない。
        copy_source_snapshot(list(inventory_result.phase1_metas),
                            dest_root=source_snapshot_root,
                            plugin_lock=threading.Lock())
        return staging_dir, source_snapshot_root, inventory_result
```

同ファイルにモジュール関数を追加:

```python
def build_inventory_view(result) -> dict:
    """[indicator-consumption-wiring] §2.9(b): 子 worker の tool が読む
    JSON-safe な inventory。**snapshot ディレクトリを列挙しない** —
    phase 2 で落ちた strategy が inventory に混ざらないようにするため、
    最終 admit 済 plugin だけを載せる。遮断 8 との照合: 載るのは
    name / kind / pairs / params / outputs / content_hash のみ
    (成績・期間・段名は一切含まない)。"""
    return {
        "plugins": [
            {"name": m.name, "kind": m.kind, "pairs": list(m.pairs),
             "params": m.params,
             "outputs": (list(m.outputs) if m.outputs is not None else None),
             "content_hash": m.content_hash}
            for m in result.inventory.metas],
        "pin_broken_strategies": [
            {"name": r.name, "alias": r.alias, "reason": r.reason}
            for r in result.rejected_strategies],
    }
```

`prepare` を更新:

```python
            staging_dir, source_snapshot_dir, inventory_result = \
                self._materialize_workspace(conn, mission_id, allowed_ids)
            # (ledger / rpc_handlers の構築はそのまま。ただし
            #  `_build_rpc_handlers` には inventory を渡す — Step 5-4c)
            ctx = ImproveRunContext(
                mission_id=mission_id, run_id=run_id, staging_dir=staging_dir,
                source_snapshot_dir=source_snapshot_dir,
                allowed_backlog_ids=allowed_ids, slot_key=slot_key,
                ledger=ledger, rpc_handlers=rpc_handlers,
                inventory=inventory_result,
                inventory_view=build_inventory_view(inventory_result))
```

`_render_improve_mission_prompt` に pin 破れ表示を足す (テンプレートの
`current_inventory` セクション直後):

```python
        pin_broken = ctx.inventory_view["pin_broken_strategies"] if ctx else []
        pin_broken_line = (
            f"pin 破れで配備から外れている strategy: {len(pin_broken)} 本 "
            f"({', '.join(p['name'] for p in pin_broken)})"
            if pin_broken else "pin 破れで配備から外れている strategy: 0 本")
```

(`improve_mission.md` の「参照」節に `{pin_broken_strategies}` プレースホルダを
足し、ここでレンダする。)

`worker_runner.py:208-222` の `run_context_fields` に 1 キーを追加:

```python
                    "inventory_view": self._run_context.inventory_view,
```

- [ ] **Step 5-1d: Run test to verify it passes**

Run: `uv run pytest tests/loops -q`
Expected: PASS

- [ ] **Step 5-1e: Commit**

```bash
git add src/agentic_fx/loops src/agentic_fx/runners/worker_runner.py tests/loops
git commit -m "feat(improve): carry the mission inventory and its JSON-safe view (P1'/P3)"
```

### Step 5-2: `list_deployed_plugins` / `lock_staging_deps`

- [ ] **Step 5-2a: Write the failing test**

`tests/tools/test_improve_staging_tools.py` に追記:

```python
# --- [indicator-consumption-wiring] T5: 露出 tool ------------------------

_VIEW = {
    "plugins": [
        {"name": "rsi", "kind": "indicator", "pairs": [],
         "params": {"period": 14}, "outputs": ["rsi"], "content_hash": "a" * 64},
        {"name": "legacy", "kind": "indicator", "pairs": [],
         "params": {"period": 14}, "outputs": None, "content_hash": "b" * 64},
    ],
    "pin_broken_strategies": [
        {"name": "s_old", "alias": "rsi", "reason": "pin_mismatch"}],
}


def _tools(tmp_path, view=_VIEW):
    defs = improve_staging_tools.build_improve_staging_tooldefs(
        staging_dir=tmp_path / "staging",
        source_snapshot_dir=tmp_path / "snap", inventory_view=view)
    return {d.name: d.func for d in defs}


def _field_names(value, *, skip_keys=("params",)):
    """`value` 以下に現れる **辞書のキー名**を再帰的に集める。
    `skip_keys` に挙げたキーの**配下は降りない** (plugin 作者が決める
    自由な名前空間なので、遮断 8 の語彙表と衝突しうる)。"""
    names = set()
    if isinstance(value, dict):
        for k, v in value.items():
            names.add(k)
            if k in skip_keys:
                continue
            names |= _field_names(v, skip_keys=skip_keys)
    elif isinstance(value, list):
        for item in value:
            names |= _field_names(item, skip_keys=skip_keys)
    return names


def test_list_deployed_plugins_returns_the_view_verbatim(tmp_path):
    out = _tools(tmp_path)["list_deployed_plugins"]()
    assert out["plugins"] == _VIEW["plugins"]
    assert out["pin_broken_strategies"] == _VIEW["pin_broken_strategies"]
    # 遮断 8: 成績・期間・段の**フィールド名**が view に現れないこと。
    #
    # **codex plan r1 C5 是正**: v1.2 は `json.dumps(out)` の全文に
    # `"period"` が含まれないことを assert していたが、同じ `_VIEW` が
    # `params: {"period": 14}` を持っており **正しい出力でも必ず落ちる**
    # (判別力ゼロ)。設計書 §2.9b / §5 は **`params` の露出を明示的に許可**
    # しているので、これは遮断 8 との混同でもある。検査対象を
    # **トップレベルのフィールド名 (`params` 配下は除外)** に絞る。
    forbidden = {"pf", "avg_r", "win_rate", "max_drawdown", "total_pnl",
                 "trades", "in_sample", "holdout", "scope", "period",
                 "period_start", "period_end", "baseline", "metrics"}
    assert _field_names(out) & forbidden == set()
    # `params` 配下の `"period"` は許可されている (plugin 作者の名前空間)
    assert out["plugins"][0]["params"] == {"period": 14}


def test_list_deployed_plugins_marks_outputs_none_as_not_dependable(tmp_path):
    """U4: outputs なし = 依存先にできない — その事実が view から読める。"""
    out = _tools(tmp_path)["list_deployed_plugins"]()
    legacy = next(p for p in out["plugins"] if p["name"] == "legacy")
    assert legacy["outputs"] is None


def test_lock_staging_deps_writes_pins_from_the_view(tmp_path):
    """P1 (改善経路): view の content_hash を pin に書く。
    staging・examples は参照しない。"""
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["ok"] is True
    assert out["pins"] == {"rsi": "c" * 64}
    assert out["changed"] is True
    # ユーザー裁定 2026-09-14 ⑥: 返り値の content_hash は lock 後に disk から
    # 再計算した snapshot 値 (`check_candidate_snapshot` はこれを保証しない)。
    from agentic_fx.plugin.loader import content_hash as _content_hash
    assert out["content_hash"] == _content_hash(staging / "rsi_pullback")
    import yaml
    cfg = yaml.safe_load((staging / "rsi_pullback" / "config.yaml").read_text())
    assert cfg["indicators"]["rsi"]["pin"] == "c" * 64


def test_lock_staging_deps_is_idempotent(tmp_path):
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins={"rsi": "c" * 64})
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["ok"] is True and out["changed"] is False


def test_lock_staging_deps_refuses_unknown_dependency(tmp_path):
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    view = {"plugins": [], "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi" and out["reason"] == "not_found"
    assert out["available"] == []


def test_lock_staging_deps_refuses_outputs_undeclared_dependency(tmp_path):
    """codex plan r2 束4 Important 是正: `_tools(tmp_path)` の**既定引数**
    (`view=_VIEW`) に頼ると、`_VIEW` の中身が別の理由で変わったときに
    この受入がこっそり `not_found` へ後退しても誰も気づけない
    (`_VIEW` はモジュールレベル共有 fixture — `test_list_deployed_plugins_*`
    等、他の複数テストとも共用している)。**このテストだけが読む
    `inventory_view` を明示的にローカルで組み立て**、U4b
    (`outputs_undeclared`) 分岐に実際に到達することを自己完結で保証する。"""
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    d = fx.write_rsi_pullback(staging, pins=None)
    import yaml
    cfg = yaml.safe_load((d / "config.yaml").read_text())
    cfg["indicators"]["rsi"]["plugin"] = "legacy"
    (d / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    view = {"plugins": [
                {"name": "legacy", "kind": "indicator", "pairs": [],
                 "params": {"period": 14}, "outputs": None,
                 "content_hash": "b" * 64}],
            "pin_broken_strategies": []}
    out = _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    assert out["reason"] == "outputs_undeclared"


def test_lock_staging_deps_keeps_the_candidate_discoverable(tmp_path):
    from agentic_fx.plugin.loader import discover_one_with_reason
    from tests.fixtures import indicator_wiring as fx
    staging = tmp_path / "staging"
    fx.write_rsi_pullback(staging, pins=None)
    view = {"plugins": [{"name": "rsi", "kind": "indicator", "pairs": [],
                         "params": {"period": 14}, "outputs": ["rsi"],
                         "content_hash": "c" * 64}],
            "pin_broken_strategies": []}
    _tools(tmp_path, view)["lock_staging_deps"]("rsi_pullback")
    meta, reason = discover_one_with_reason(staging / "rsi_pullback",
                                            "rsi_pullback")
    assert reason is None and meta.indicators[0].pin == "c" * 64
```

`tests/integration/test_improve_forbidden_regression.py` に追記:

```python
def test_new_improve_tools_are_registered_and_not_forbidden():
    """P3: 2 tool が registry と規律文に載り、`IMPROVE_FORBIDDEN` と非交差。"""
    from agentic_fx.tools.signal_tools import IMPROVE_FORBIDDEN
    names = {"list_deployed_plugins", "lock_staging_deps"}
    assert names.isdisjoint(set(IMPROVE_FORBIDDEN))
    prompt = (Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
              / "loops" / "prompts" / "improve_mission.md").read_text()
    for name in names:
        assert name in prompt
```

- [ ] **Step 5-2b: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_improve_staging_tools.py -k "deployed or lock_staging" -q`
Expected: FAIL — `TypeError: build_improve_staging_tooldefs() got an unexpected
keyword argument 'inventory_view'`

- [ ] **Step 5-2c: Write minimal implementation**

`src/agentic_fx/tools/improve_staging_tools.py` の builder に `inventory_view` を足し、
2 つの tool を実装:

```python
def build_improve_staging_tooldefs(*, staging_dir: Path,
                                   source_snapshot_dir: Path,
                                   counters=None, budget=None,
                                   inventory_view: dict | None = None,
                                   ) -> list[ToolDef]:
    # (既存の _safe_join / _ALLOWED_REL / 既存 7 tool の定義はそのまま)
    view = inventory_view or {"plugins": [], "pin_broken_strategies": []}

    def list_deployed_plugins() -> dict:
        """[indicator-consumption-wiring] §2.9(b): **親が handshake に
        載せた `inventory_view` だけ**を読む (snapshot ディレクトリを
        列挙しない — phase 2 で落ちた strategy が混ざらないようにするため)。
        `outputs` が `null` の indicator は依存先にできない (U4)。"""
        return {"plugins": list(view["plugins"]),
                "pin_broken_strategies": list(view["pin_broken_strategies"])}

    def lock_staging_deps(name: str) -> dict:
        """候補 `config.yaml` の `indicators.<alias>.pin` を、`inventory_view`
        の `content_hash` で書き換える (ハーネスが書く — agent が 64 hex を
        写さない)。staging / examples は参照しない。書き換え後に
        `discover` を通ることを同じ tool 内で確認する。

        `outputs` 宣言なしの indicator は依存先にできないので
        `reason="outputs_undeclared"` で拒否する (U4)。"""
        candidate_dir = _safe_join(staging_dir, name)
        if candidate_dir is None or not candidate_dir.is_dir():
            return {"error": "not found",
                    "hint": "staging に無い名前です。list_staging で確認してください"}
        meta, reason = plugin_loader.discover_one_with_reason(candidate_dir, name)
        if meta is None:
            return {"error": f"loader_rejected: {reason}"}
        if meta.kind != "strategy":
            return {"error": "lock_staging_deps is only for kind=strategy "
                             "candidates", "candidate_kind": meta.kind}
        by_name = {p["name"]: p for p in view["plugins"]}
        pins: dict[str, str] = {}
        for ref in meta.indicators:
            dep = by_name.get(ref.plugin)
            if dep is None:
                return {"error": "indicator_unresolved", "alias": ref.alias,
                        "reason": "not_found",
                        "available": [p["name"] for p in view["plugins"]
                                      if p["kind"] == "indicator"]}
            if dep["kind"] != "indicator":
                return {"error": "indicator_unresolved", "alias": ref.alias,
                        "reason": "not_indicator",
                        "available": [p["name"] for p in view["plugins"]
                                      if p["kind"] == "indicator"]}
            if dep["outputs"] is None:
                return {"error": "indicator_unresolved", "alias": ref.alias,
                        "reason": "outputs_undeclared",
                        "available": [p["name"] for p in view["plugins"]
                                      if p["kind"] == "indicator"
                                      and p["outputs"] is not None]}
            pins[ref.alias] = dep["content_hash"]

        # YAML の書き換えは `plugin/resolve.lock_config` 1 箇所に閉じる
        # (人間 CLI の `afx plugin lock` と同じ関数 — 設計書 §2.3)。
        # snapshot 再取得 (ユーザー裁定 2026-09-14 ⑥): `check_candidate_snapshot`
        # はファイル 3 本の存在・属性しか見ないため、lock 後に無条件で
        # submit が通ると決め打ちしない。`lock_config` が書き込み後に
        # 再計算して返す `new_hash` を正とし、`discover_one_with_reason`
        # の再取得結果 (`relocked.content_hash`) と一致することを assert
        # する (どちらも書き込み後の disk を独立に読む)。
        before, after, new_hash = plugin_resolve.lock_config(candidate_dir, pins)
        relocked, relock_reason = plugin_loader.discover_one_with_reason(
            candidate_dir, name)
        if relocked is None:
            (candidate_dir / "config.yaml").write_text(before, encoding="utf-8")
            return {"error": f"loader_rejected_after_lock: {relock_reason}"}
        assert relocked.content_hash == new_hash, (
            "lock_config の snapshot 再取得値と discover の再取得値が食い違う"
            f" ({new_hash} != {relocked.content_hash})")
        return {"ok": True, "pins": pins, "changed": after != before,
                "content_hash": new_hash,
                "diff": "".join(difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile="config.yaml (before)",
                    tofile="config.yaml (after)")),
                "directive": "ロック後に run_plugin_tests と run_backtest を "
                             "再実行してから提出してください "
                             "(テストした artifact == 提出する artifact)"}
```

`return [...]` の ToolDef 一覧に 2 件を追加:

```python
        ToolDef(name="list_deployed_plugins",
                description="配備済 (承認済) plugin の一覧。strategy の "
                            "indicators: で依存に書けるのは kind=indicator かつ "
                            "outputs が null でないものだけ。",
                parameters={"type": "object", "properties": {}},
                func=list_deployed_plugins),
        ToolDef(name="lock_staging_deps",
                description="候補の indicators 依存を現在の配備版でロックする "
                            "(config.yaml の pin をハーネスが書く)。提出前に "
                            "必ず実行し、その後 self-test / backtest を再実行する。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                func=lock_staging_deps),
```

import を追加: `import difflib`、
`from agentic_fx.plugin import resolve as plugin_resolve`、
`from agentic_fx.plugin import loader as plugin_loader`。

`src/agentic_fx/tools/mission_registry.py` の `build_mission_registry` に
`inventory_view: dict | None = None` を足し、improve 分岐で
`build_improve_staging_tooldefs(..., inventory_view=inventory_view)` に渡す。

`src/agentic_fx/mission_worker.py::_build_improve_registry` に
`inventory_view: dict | None` を足して透通させ、`_run_improve_mission` が
`handshake.get("inventory_view")` を渡す。

`src/agentic_fx/loops/prompts/improve_mission.md` の規律 2 のツール列挙に
2 件を足し、規律を 1 項目追加 (設計書 §2.9(d) 逐語):

```markdown
7. **strategy は配備済 indicator を `indicators:` で宣言して使ってください**
   (`list_deployed_plugins()` で一覧。`outputs` が `null` の indicator は
   依存先にできません)。**提出前に `lock_staging_deps(name="<候補名>")` で
   依存の版をロックし、ロック後に `run_plugin_tests` と `run_backtest` を
   再実行してください** (テストした artifact == 提出する artifact)。
   系列が全 NaN のままなら warmup 不足の兆候です — `max_bars` は
   「依存 indicator の warmup + 自分の lookback」を覆う値を宣言してください。
   **無い指標は自前計算せず**、`不足指標: <名前と定義>` として
   `discoveries` に起票してください。
```

- [ ] **Step 5-2d: Run test to verify it passes**

Run: `uv run pytest tests/tools tests/integration -q`
Expected: PASS

- [ ] **Step 5-2e: Commit**

```bash
git add src/agentic_fx/tools src/agentic_fx/mission_worker.py \
        src/agentic_fx/loops/prompts/improve_mission.md tests
git commit -m "feat(improve): list_deployed_plugins + lock_staging_deps tools (P1/P3)"
```

### Step 5-3: `release_backtest` と `started:false` の予約解放 (F4)

- [ ] **Step 5-3a: Write the failing test**

`tests/tools/test_mission_counters.py` に追記:

```python
def test_release_backtest_decrements_and_never_goes_negative():
    c = MissionToolCounters(budget=_budget(max_backtests_per_candidate=2))
    assert c.reserve_backtest("cand", 2) is True
    assert c.backtest_calls["cand"] == 1
    c.release_backtest("cand")
    assert c.backtest_calls["cand"] == 0
    c.release_backtest("cand")
    assert c.backtest_calls["cand"] == 0      # 0 未満にしない
    c.release_backtest("never_reserved")
    assert c.backtest_calls["never_reserved"] == 0


def test_release_backtest_does_not_touch_successful_backtests():
    c = MissionToolCounters(budget=_budget(max_backtests_per_candidate=2))
    c.reserve_backtest("cand", 2)
    c.record_backtest_result("cand", ok=True)
    before = c.successful_backtests["cand"]
    c.release_backtest("cand")
    assert c.successful_backtests["cand"] == before
```

`tests/tools/test_improve_rpc_tools.py` に追記 (`_budget` は
`tests/tools/test_mission_counters.py:66` の `def _budget(**changes)` を
`from tests.tools.test_mission_counters import _budget` で取る。
`_rpc_tools` / `_rpc_tooldefs` はそれぞれ `tests.fixtures.wiring_envs.rpc_tools` /
`.rpc_tooldefs` の別名 import):

```python
def test_unresolved_backtest_releases_the_reservation(tmp_path):
    """F4 (予約・解放の部分): `{"started": false, ...}` で予約を戻す。

    **opus r1 I12 是正**: 旧案は `before_calls = dict(counters.backtest_calls)`
    → `assert dict(counters.backtest_calls) == before_calls` と書いていたが、
    `backtest_calls` は `defaultdict(int)` なので呼び出し前は `{}`、
    `reserve_backtest("cand", …)` で `{"cand": 1}`、`release_backtest("cand")`
    で `{"cand": 0}` になる。`{} != {"cand": 0}` なので **予約解放が正しく
    動いていてもこの assert は落ちる**し、逆変異 (M8: `release_backtest` を
    no-op) でも同じ理由で落ちる = 判別力ゼロだった。**キー単位の直接 assert**
    にする (段 0 の M8 の観測点も同じものに差し替えること —
    [[mutation-testing]] の「pin の観測点は probe で決める」)。

    **`_strip_forbidden` は denylist** (`{k: … for k, v in value.items()
    if k not in _FORBIDDEN_KEYS}`、着手時に
    `rg -n 'def _strip_forbidden' -A 12 src/agentic_fx/tools/improve_rpc_tools.py`
    で再確認する) なので、新規キー `started` / `alias` / `reason` /
    `available` はそのまま通る (opus r1 I11)。`_FORBIDDEN_KEYS` には
    `start` はあるが `started` は**別キー**で完全一致では当たらない。
    **src 側の変更は不要** — 下の 4 キーの pin がそれを固定する。
    """
    counters = MissionToolCounters(budget=_budget())
    handler_result = {"started": False, "error": "indicator_unresolved",
                      "alias": "rsi", "reason": "not_found",
                      "available": ["sma", "adx"]}
    tools = _rpc_tools(tmp_path, counters=counters,
                       run_backtest_handler=lambda args: handler_result)

    out = tools["run_backtest"](name="cand", pair="USDJPY")

    # `_strip_forbidden` (denylist) を通っても 4 キーが残る (opus r1 I11)
    assert out["started"] is False
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi"
    assert out["reason"] == "not_found"
    assert out["available"] == ["sma", "adx"]
    # 予約 → 解放で 0 に戻る (opus r1 I12: defaultdict 比較にしない)
    assert counters.backtest_calls["cand"] == 0
    assert counters.successful_backtests["cand"] == 0


def test_unresolved_backtest_counts_errors_and_streak_via_the_registry(tmp_path):
    """F4 (counters の残りのフィールド、**opus r1 I5 是正**)。

    設計書 §5 は「未解決 `run_backtest` は予算枠を消費しないが `errors` /
    refusal streak に計上」と書いているが、`errors` / `recoverable_refusal_streak`
    を増やすのは **`ToolRegistry` の `_notify_result` → `counters.record_tool_result`**
    だけ (`registry.py` の `if isinstance(result, dict) and "error" in result:`
    分岐、配線は `mission_registry.py` の `on_result=counters.record_tool_result`。
    着手時に `rg -n 'on_result' src/agentic_fx/tools/registry.py src/agentic_fx/tools/mission_registry.py`
    で再取得)。tooldef を**直接呼ぶ** `_rpc_tools` 経路は registry を通らない
    ので、上のテストだけでは受入が空振りする。**registry 経由で 1 本足す**。

    前提 (1 行で固定): `started:false` 応答には `"error"` キーが含まれる
    ので registry は `ok=False` と判定する。
    """
    from agentic_fx.tools.registry import ToolRegistry
    counters = MissionToolCounters(budget=_budget())
    defs = _rpc_tooldefs(tmp_path, counters=counters,
                         run_backtest_handler=lambda args: {
                             "started": False, "error": "indicator_unresolved",
                             "alias": "rsi", "reason": "not_found",
                             "available": ["sma", "adx"]})
    # `ToolRegistry.__init__` は **キーワード専用** (`on_execute` / `on_result`) で
    # tooldef は取らない。登録は `register_all`、実行は
    # `execute(name, arguments, allowed)` (`mission_registry.py` の構築行を
    # 逐語転写。着手時に `sed -n '20,70p' src/agentic_fx/tools/registry.py` と
    # `rg -n 'ToolRegistry(' src/agentic_fx/tools/mission_registry.py` で再取得)
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)
    before_errors = counters.errors
    before_total = counters.total_calls        # codex plan r1 I8

    registry.execute("run_backtest", {"name": "cand", "pair": "USDJPY"},
                     allowed=registry.names())

    assert counters.errors == before_errors + 1
    # 設計書 §6 F4 の「`max_tool_calls` +1」= tool 予算は消費される
    # (backtest 枠 `backtest_calls` だけが解放される)。**codex plan r1 I8**:
    # v1.2 はこの counter を 1 つも観測していなかった。`total_calls` の
    # 実フィールド名は着手時に
    # `rg -n 'total_calls|def record_call' -A 6 src/agentic_fx/tools/mission_counters.py`
    # で再取得すること (名前が違えば「max_tool_calls に計上される側の
    # 通算カウンタ」に読み替える)。
    assert counters.total_calls == before_total + 1
    # streak のキーは `(tool 名, "tool_error:" + error[:60])`
    # (`mission_counters.record_tool_result` の逐語。着手時に
    # `rg -n 'tool_error:' src/agentic_fx/tools/mission_counters.py` で再確認)
    assert counters.recoverable_refusal_streak[
        ("run_backtest", "tool_error:indicator_unresolved")] == 1
    assert counters.backtest_calls["cand"] == 0      # 予約は戻っている


def test_response_without_started_key_keeps_the_reservation(tmp_path):
    """F4: `started` キーが無い応答 (旧形式・RPC 失敗) では解放しない
    (fail closed — 予算は消費されたまま)。"""
    counters = MissionToolCounters(budget=_budget())
    tools = _rpc_tools(tmp_path, counters=counters,
                       run_backtest_handler=lambda args: {"error": "backtest_failed"})
    tools["run_backtest"](name="cand", pair="USDJPY")
    assert counters.backtest_calls["cand"] == 1


def test_started_true_keeps_the_reservation(tmp_path):
    counters = MissionToolCounters(budget=_budget())
    tools = _rpc_tools(tmp_path, counters=counters,
                       run_backtest_handler=lambda args: {
                           "started": True, "metrics": {"trades": 5}})
    tools["run_backtest"](name="cand", pair="USDJPY")
    assert counters.backtest_calls["cand"] == 1
    assert counters.successful_backtests["cand"] == 1
```

- [ ] **Step 5-3b: Run test to verify it fails**

Run: `uv run pytest tests/tools/test_mission_counters.py tests/tools/test_improve_rpc_tools.py -k "release or unresolved or started" -q`
Expected: FAIL — `AttributeError: 'MissionToolCounters' object has no attribute
'release_backtest'`

- [ ] **Step 5-3c: Write minimal implementation**

`src/agentic_fx/tools/mission_counters.py` に追加 (`reserve_backtest` の直後):

```python
    def release_backtest(self, name: str) -> None:
        """[indicator-consumption-wiring] §2.9(c): 親が backtest を
        **開始しなかった**とき (`{"started": false, ...}`) に予約を戻す。
        `reserve_backtest` と同じ lock 区間で減算し、0 未満にはしない
        (二重解放・未予約の解放を吸収する)。`successful_backtests` は
        触らない — 「成功した backtest の回数」の意味を変えない。"""
        with self._lock:
            if self.backtest_calls[name] > 0:
                self.backtest_calls[name] -= 1
```

`src/agentic_fx/tools/improve_rpc_tools.py::run_backtest` の handler 呼び出し直後:

```python
        result = run_backtest_handler({"name": name, "pair": pair})
        # [indicator-consumption-wiring] §2.9(c): 予約 (子) → 親 RPC →
        # **未開始なら解放**。`started` が明示的に False のときだけ戻す —
        # キーが無い応答 (旧形式・RPC 失敗) では戻さない (fail closed:
        # 予算は消費されたまま)。`error` キーがあるので registry の
        # `on_result` が `errors` と recoverable refusal streak に自動計上し、
        # 同じ未解決を繰り返す agent は既存規律で abort する。
        # `max_tool_calls` は常に +1 (`record_call` は registry 側)。
        if counters is not None and result.get("started") is False:
            counters.release_backtest(name)
        if counters is not None and result.get("started") is not False:
            backtest_ok = _is_successful_backtest(result)
            counters.record_backtest_result(name, ok=backtest_ok)
            if backtest_ok:
                counters.record_progress(name, "backtest_ok")
```

(既存の `if counters is not None:` ブロックを上の 2 分岐に置き換える。)

- [ ] **Step 5-3d: Run test to verify it passes**

Run: `uv run pytest tests/tools -q`
Expected: PASS

- [ ] **Step 5-3e: Commit**

```bash
git add src/agentic_fx/tools/mission_counters.py src/agentic_fx/tools/improve_rpc_tools.py tests/tools
git commit -m "feat(improve-rpc): release the backtest reservation when the parent did not start (F4)"
```

**T5a 完了条件**:
- [ ] **F4**: 未解決 RPC が `{"started": false, "error": "indicator_unresolved",
      "alias", "reason", "available"}` を返し (`_strip_forbidden` は denylist なので
      4 キーとも通る — opus r1 I11)、`backtest_calls[name] == 0` (予約 → 解放、
      **キー単位の直接 assert**。`dict(...)` 比較にしない — opus r1 I12)、
      `successful_backtests[name] == 0`。`errors` +1 と
      `recoverable_refusal_streak[("run_backtest", "tool_error:indicator_unresolved")]` +1、
      **`total_calls` +1** (= `max_tool_calls` は消費される。codex plan r1 I8) は
      **`ToolRegistry` 経由のテスト**で観測する (tooldef 直呼びでは registry を
      通らないので増えない — opus r1 I5)。
      **`last_result` 不変は T5b Step 5-4 で観測する** — 実 handler と実
      `improvement_backlog` 行が要るので T5a の counters テストでは書けない
      (`test_unresolved_run_backtest_leaves_the_backlog_last_result_untouched`、
      codex plan r1 I8)。
      staging 内 indicator は `available` に出ない。`started` キー無し応答では解放されない
- [ ] **P1 (改善経路)**: `lock_staging_deps` が view の `content_hash` を pin に書き、
      `content_hash` が変わり、書き換え後も `discover` を通る。同じ pin は `changed: false`、
      古い pin は上書き、未知/`outputs` なし依存は固定 reason で拒否
- [ ] **P1'**: phase 2 で落ちた strategy が `_snapshot_src` に残り `read_plugin_source` で
      読めるが `list_deployed_plugins` には出ず、prompt に「pin 破れ N 本 (名前)」が出る
- [ ] **P3 (改善経路の露出部分)**: `list_deployed_plugins` と prompt inventory が同じ
      view 由来 (prepare 後に live `plugins/` を差し替えても不変)、staging・examples を
      含まず、`IMPROVE_FORBIDDEN` と非交差
- [ ] 段 0 変異 red: (a) `release_backtest` を no-op にする →
      `test_unresolved_backtest_releases_the_reservation` の
      `backtest_calls["cand"] == 0` が落ちる (**旧案の `dict(...)` 比較では
      SURVIVED していた** — opus r1 I12)
      (b) `result.get("started") is False` を `result.get("started") is not True` にする
      → `test_response_without_started_key_keeps_the_reservation` が落ちる
      (d) `build_inventory_view` を snapshot ディレクトリ列挙に置き換える →
      `test_pin_broken_strategy_stays_in_the_snapshot_but_not_in_the_inventory` が落ちる

---

## T5b: handler の解決 / commit gate / activity / prompt [improve-loop-exposure-2]

**この task は opus r1 観点 7 で T5 から切り出した** (旧 T5 = 6 Step / 約 1,200 行 /
src 10 ファイル)。境界は「露出面 (context / tool / counters) が確定したところ」。

**対応**: 設計書 §2.9 の `run_backtest_handler` / commit gate、§4 の `improve_loop.py`
(`run_backtest_handler` / `commit` の gate / `_build_approval_payload` /
`_finalize_gate_failed` / `backtest_cpu` activity) / `improve_mission.md` /
`improve_context.py` 行。
**完了条件の受入 ID**: F5 / U4a (改善経路) / P3 (payload 部分) / P4 (改善経路) /
C1 (`backtest_cpu` activity)。

**着手条件**: T5a が main にマージ済み (`ImproveRunContext.inventory` /
`inventory_view` / `release_backtest` を Consumes)。T4b も必須
(`GateOutcome.resolved` / `.cpu_samples` / `find_noop_copy(..., inventory)`)。
**worktree 並列不可**。

**Files:**
- Modify: `src/agentic_fx/loops/improve_loop.py` (`run_backtest_handler` /
  `_run_plugin_gate` / `_build_approval_payload` / `commit` の gate /
  `_finalize_gate_failed` / `backtest_cpu` activity)
- Modify: `src/agentic_fx/loops/prompts/improve_mission.md`
- Modify: `src/agentic_fx/loops/improve_context.py`
- Test: `tests/loops/test_improve_e2e.py`、`tests/loops/test_improve_loop_finalize.py`、
  `tests/loops/test_improve_loop_plugin_gate.py`、
  `tests/integration/test_improve_forbidden_regression.py`

**Interfaces:**

- Consumes: T5a の `ImproveRunContext.inventory` / `.inventory_view` /
  `MissionToolCounters.release_backtest`、T4a の `GateOutcome.resolved` /
  `.verdict_kind` / `.cpu_samples`、T4b の `find_noop_copy(..., examples_dir, inventory)`、
  T3 の `build_intent_source(..., resolved)`、T6b の `wiring_envs`
  (`improve_env` / `improve_env_with_activity` / `prepare_ctx` / `activity_text` /
  `loop_env` / `completed_result` / `mission_for`)。
- Produces: 本 task は新しい公開シンボルを作らない (既存メソッドの挙動変更のみ)。

### Step 5-4: `run_backtest_handler` の解決と `started`

- [ ] **Step 5-4a: Write the failing test**

`tests/loops/test_improve_e2e.py` に追記:

```python
def test_run_backtest_handler_refuses_unresolved_dependency(tmp_path):
    """F4: 親は backtest を走らせず `{"started": false, ...}` を返す。
    staging 内 indicator は `available` に出ない。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    # staging に indicator 候補を置いても inventory には入らない
    fx.write_indicator(ctx.staging_dir, "adx")
    cand = fx.write_rsi_pullback(ctx.staging_dir, pins=None)
    import yaml
    cfg = yaml.safe_load((cand / "config.yaml").read_text())
    cfg["indicators"]["rsi"]["plugin"] = "adx"       # staging 内を指す
    (cand / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))

    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})

    assert out["started"] is False
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi" and out["reason"] == "not_found"
    assert out["available"] == ["rsi"]       # staging の adx は出ない
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


def test_unresolved_run_backtest_leaves_the_backlog_last_result_untouched(
        tmp_path):
    """F4 (`last_result` 不変、**codex plan r1 I8**)。

    設計書 §6 F4 は counters と並べて **`last_result` 不変**を要求している
    (遮断 8: 未解決の RPC は改善 worker に渡る文字列を 1 文字も動かさない)。
    v1.2 はこれを 1 箇所も観測していなかった。**実 DB (tmp) の
    `improvement_backlog` 行に見張り値を入れ、handler を回した後に
    同じ値のままであること**を pin する (`last_result` を書くのは
    mission の終端 (`_finalize_*`) だけ、という規律の回帰にもなる)。

    `last_result` を書き込む API 名は着手時に
    `rg -n "last_result=\\?" -B 4 src/agentic_fx/loops/improve_loop.py` と
    `rg -n 'def .*backlog' src/agentic_fx/store/*.py` で再取得すること。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": "a" * 64})  # pin 破れ
    conn.execute(
        "UPDATE improvement_backlog SET last_result='SENTINEL_UNCHANGED'")
    conn.commit()

    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})

    assert out["started"] is False
    rows = conn.execute("SELECT last_result FROM improvement_backlog").fetchall()
    assert rows, "見張り行が無い (backlog が空なら 1 行作ってから回すこと)"
    assert all(r["last_result"] == "SENTINEL_UNCHANGED" for r in rows)


def test_run_backtest_handler_accepts_unpinned_candidates(tmp_path):
    """探索中は `check` — pin 無しでも通る (提出時に `require` で落ちる)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins=None)
    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})
    assert out.get("started") is not False
    assert "metrics" in out


def test_run_backtest_handler_refuses_stale_pin_under_check(tmp_path):
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": "a" * 64})
    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})
    assert out["started"] is False and out["reason"] == "pin_mismatch"


def test_started_true_is_present_on_success(tmp_path):
    """F4 の裏: 成功応答にも `started` キーが載る (子が解放しない判定を
    キーの有無ではなく値で行えるように)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": hashes["rsi"]})
    out = ctx.rpc_handlers["run_backtest"](
        {"name": "rsi_pullback", "pair": "USDJPY"})
    assert out["started"] is True
```

- [ ] **Step 5-4b: Run test to verify it fails**

Run: `uv run pytest tests/loops/test_improve_e2e.py -k run_backtest_handler -q`
Expected: FAIL — `KeyError: 'started'` (handler が `started` を返さない)

- [ ] **Step 5-4c: Write minimal implementation**

`src/agentic_fx/loops/improve_loop.py::_build_rpc_handlers` の
`run_backtest_handler` を更新 (`content_hash_bytes` 照合の直後、
`build_intent_source` の前):

```python
            # [indicator-consumption-wiring] §2.9(c): 解決は**親でしか
            # できない** (`ImproveRunContext.inventory` は親にしかない)。
            # 予約は子で先に起きているので、ここで未解決なら backtest を
            # 走らせずに `started: false` を返し、**子が予約を戻す**。
            # 探索中なので `pin_mode="check"` (pin があれば一致を要求、
            # 無ければ通す)。`available` は inventory の indicator 名だけ
            # (staging・examples は含まない)。
            try:
                resolved = resolve_indicator_deps(
                    meta, inventory.inventory, settings=self._settings,
                    pin_mode="check")
            except IndicatorResolutionError as exc:
                return {
                    "started": False, "error": "indicator_unresolved",
                    "alias": exc.alias, "reason": exc.reason,
                    "available": sorted(
                        m.name for m in inventory.inventory.metas
                        if m.kind == "indicator" and m.outputs is not None),
                }
```

`_build_rpc_handlers` のシグネチャに `inventory` を足し、`prepare` が
`self._build_rpc_handlers(ledger, staging_dir=staging_dir,
inventory=inventory_result)` で渡す。

`build_intent_source(...)` に `resolved=resolved` を渡す (Step 3-1 の暫定
`ResolvedIndicatorSet.empty(...)` を置き換える)。

成功応答 (`_backtest_reply_from_save_kwargs`) に `started: True` を足す:

```python
def _backtest_reply_from_save_kwargs(save_kwargs, *, submission_blocked=None,
                                     started: bool = True) -> dict:
    reply = {...}                     # 現行のまま
    reply["started"] = started        # [indicator-consumption-wiring] §2.9(c)
    return reply
```

- [ ] **Step 5-4d: Run test to verify it passes**

Run: `uv run pytest tests/loops tests/tools -q`
Expected: PASS

- [ ] **Step 5-4e: Commit**

```bash
git add src/agentic_fx/loops/improve_loop.py tests/loops
git commit -m "feat(improve): resolve dependencies in the parent before running a backtest (F4)"
```

### Step 5-5: commit gate の未解決と `outputs_required` (F5 / U4a)

- [ ] **Step 5-5a: Write the failing test**

`tests/loops/test_improve_e2e.py` に追記:

```python
def test_commit_gate_reports_indicator_unresolved(tmp_path):
    """F5: `gate_failed reason=indicator_unresolved`、
    `last_result == "indicator_unresolved"` (**完全一致**)、gate 行 0、
    approval 行 0。alias/cause は activity にだけ出る (遮断 8)。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins=None)   # unpinned → require で落ちる
    result = _completed_result(_plugin_artifact("rsi_pullback", kind="strategy"))

    loop.commit(mission=_mission(ctx), ctx=ctx, result=result, now=fx.NOW)

    row = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["last_result"] == "indicator_unresolved"
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 1  # rsi のみ
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0
    text = _activity_text(activity)
    assert ("gate_failed mission=" in text
            and "reason=indicator_unresolved alias=rsi cause=unpinned" in text)


def test_last_result_never_carries_alias_or_reason(tmp_path):
    """遮断 8: `last_result` は固定文言のみ — alias も cause も混ぜない。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins=None)
    loop.commit(mission=_mission(ctx), ctx=ctx,
                result=_completed_result(
                    _plugin_artifact("rsi_pullback", kind="strategy")),
                now=fx.NOW)
    row = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "rsi" not in row["last_result"]
    assert "unpinned" not in row["last_result"]
    assert "holdout" not in row["last_result"]


def test_commit_gate_reports_outputs_required_for_indicator_without_outputs(
        tmp_path):
    """U4a (改善 commit gate): 固定文言 `outputs_required`、approval 行 0。"""
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    ctx = _prepare_ctx(loop, now=NOW)
    d = ctx.staging_dir / "legacy_ind"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(
        "def compute(df, params):\n    return {'rsi_14': 1.0}\n")
    (d / "config.yaml").write_text("kind: indicator\nparams:\n  period: 14\n")
    (d / "test_plugin.py").write_text("def test_x():\n    pass\n")
    result = _completed_result(_plugin_artifact("legacy_ind", kind="indicator"))

    loop.commit(mission=_mission(ctx), ctx=ctx, result=result, now=NOW)

    row = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["last_result"] == "outputs_required"
    assert conn.execute(
        "SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


def test_pinned_candidate_reaches_the_approval_payload_with_indicator_deps(
        tmp_path):
    """P3 (改善経路): 改善 commit の payload にも `indicator_deps` が載る。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": hashes["rsi"]})
    loop.commit(mission=_mission(ctx), ctx=ctx,
                result=_completed_result(
                    _plugin_artifact("rsi_pullback", kind="strategy")),
                now=fx.NOW)
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE "
        "json_extract(payload_json,'$.name')='rsi_pullback'"
    ).fetchone()["payload_json"])
    assert payload["indicator_deps"] == {
        "rsi": {"plugin": "rsi", "content_hash": hashes["rsi"],
                "params": {"period": fx.RSI_PERIOD}}}
```

- [ ] **Step 5-5b: Run test to verify it fails**

Run: `uv run pytest tests/loops/test_improve_e2e.py -k "indicator_unresolved or outputs_required or indicator_deps" -q`
Expected: FAIL — commit は `gate_failed:...` の形の `last_result` を書く

- [ ] **Step 5-5c: Write minimal implementation**

`src/agentic_fx/loops/improve_loop.py::commit` の plugin 分岐に 2 つの門を足す
(`assert_max_bars_within_limit` の直後、`kind` 判定の後):

```python
                # [indicator-consumption-wiring] U4a: kind=indicator の
                # `outputs` 宣言は新規承認で必須。固定文言のみ
                # (`last_result` にそのまま流れる — 遮断 8)。
                if kind == "indicator" and candidate_meta.outputs is None:
                    self._finalize_gate_failed(
                        conn, ctx=ctx, backlog_id=selection.backlog_id,
                        reason="outputs_required", now=now,
                        tool_calls=tool_calls)
                    return
                if kind == "strategy":
                    # [indicator-consumption-wiring] §2.8: 解決は commit
                    # gate でも `require` (提出物は必ず pinned)。**alias と
                    # cause は activity にだけ出す** — `last_result` は
                    # 固定文言 `indicator_unresolved` のみ (遮断 8)。
                    try:
                        resolved = resolve_indicator_deps(
                            candidate_meta, ctx.inventory.inventory,
                            settings=self._settings, pin_mode="require")
                    except IndicatorResolutionError as exc:
                        self._finalize_gate_failed(
                            conn, ctx=ctx, backlog_id=selection.backlog_id,
                            reason="indicator_unresolved", now=now,
                            tool_calls=tool_calls,
                            activity_extra=(f" alias={exc.alias or '-'} "
                                            f"cause={exc.reason}"))
                        return
```

`_run_strategy_gate` に `resolved` / `inventory` を渡す:

```python
                        strategy_verdict = self._run_strategy_gate(
                            conn, name=artifact["name"],
                            pairs=self._read_candidate_pairs(candidate_dir),
                            timeframe=self._read_candidate_timeframe(
                                candidate_dir),
                            content_hash=gate_verdict.content_hash, now=now,
                            meta=candidate_meta, kind=kind,
                            record_fn=gate_rows.append,
                            resolved=resolved, inventory=ctx.inventory)
```

(T3 Step 3-1c で入れた暫定の `ResolvedIndicatorSet.empty(...)` を
ここで本物に差し替える。)

`_run_strategy_gate` のシグネチャを更新し、`evaluate_strategy_adoption_gate`
へそのまま渡す。

`_finalize_gate_failed` に `activity_extra` を足す:

```python
    def _finalize_gate_failed(self, conn, *, ctx, backlog_id, reason, now,
                              gate_rows=(), tool_calls=None,
                              mission_outcome: str = "gate_failed",
                              report_detail: str = "",
                              activity_extra: str = "") -> None:
        """...(既存 docstring はそのまま)...

        [indicator-consumption-wiring] §2.8: `activity_extra` は activity 行
        にだけ足す補足 (`alias=<alias> cause=<reason>`)。**`last_result` /
        `report_detail` には絶対に流さない** — `reason` (固定文言) だけが
        `improvement_backlog.last_result` へ行く (遮断 8)。
        """
        self._delete_staging(ctx)
        floor_suffix = (
            f" {floor_settings_kv(self._settings.improve.gate)}"
            if mission_outcome == "unprofitable" else "")
        self._activity.write(
            Category.IMPROVE, "gate_failed",
            f"mission={ctx.mission_id} reason={reason}{floor_suffix}"
            f"{activity_extra}{_tool_calls_suffix(tool_calls)}")
```

`_build_approval_payload` に `indicator_deps` を足す
(`gate_metrics` から取る — `_run_strategy_gate` に渡した `resolved` を
`gate_metrics["resolved"]` として持ち回る):

```python
            # [indicator-consumption-wiring] §2.7: 三経路同形。
            # **`resolved` から作る** — ここで再解決しない (P3')。
            "indicator_deps": (gate_metrics["resolved"].pin_object()
                               if gate_metrics.get("resolved") is not None
                               else {}),
```

`commit` で `gate_metrics["resolved"] = resolved` を代入する (strategy 分岐のみ。
indicator 候補では未設定なので `{}` になる)。

`_run_plugin_gate` の `find_noop_copy` 呼び出しに `inventory=ctx.inventory` を渡す
(Step 4-6c の暫定 helper `_inventory_for_gate` を削除する)。**同じコミットで
`_finalize_success` の P5 呼び出し (Step 4-6c で足したフォールバック式) も
`inventory=ctx.inventory` の直渡しへ単純化する** — T5a 完了以降は
`prepare()` が常に非空の `ctx.inventory` を書き込むため、
`self._inventory_for_gate(conn)` フォールバック分岐は恒久的に到達不能な死に
コードになる (codex plan r2 束3 Critical の後始末)。
`test_finalize_success_falls_back_to_inventory_for_gate_when_ctx_inventory_is_none`
(T4b) は `_inventory_for_gate` ごと削除する。

- [ ] **Step 5-5d: Run test to verify it passes**

Run: `uv run pytest tests/loops -q`
Expected: PASS

- [ ] **Step 5-5e: Commit**

```bash
git add src/agentic_fx/loops/improve_loop.py tests/loops
git commit -m "feat(improve): fail the commit gate on unresolved deps and missing outputs (F5/U4a)"
```

### Step 5-6: `backtest_cpu` activity と prompt inventory

- [ ] **Step 5-6a: Write the failing test**

`tests/loops/test_improve_e2e.py` に追記:

```python
def test_backtest_cpu_activity_lines_are_verbatim(tmp_path):
    """C1 (改善経路): scope × pair ごとに 1 行、逐語形式。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": hashes["rsi"]})
    loop.commit(mission=_mission(ctx), ctx=ctx,
                result=_completed_result(
                    _plugin_artifact("rsi_pullback", kind="strategy")),
                now=fx.NOW)
    lines = [l for l in _activity_text(activity).splitlines() if "backtest_cpu" in l]
    assert lines, "no backtest_cpu activity written"
    assert any(
        f"backtest_cpu mission={ctx.mission_id} plugin=rsi_pullback "
        f"scope=in_sample pair=USDJPY deps=1 cpu_sec=" in l for l in lines)
    assert all("holdout_gate" not in l for l in lines)   # scope は holdout 表記


def test_backtest_cpu_is_written_with_null_when_the_session_died(
        tmp_path, monkeypatch):
    """C1: 例外終了でも `cpu_sec=null` で 1 行積む。"""
    from tests.fixtures import indicator_wiring as fx
    from agentic_fx.plugin import sandbox as plugin_sandbox
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    fx.seed_history(conn)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    fx.write_rsi_pullback(ctx.staging_dir, pins={"rsi": hashes["rsi"]})

    # graceful close の応答を殺して SIGKILL fallback に倒す
    original_close = plugin_sandbox.PluginSession.close

    def _hard_close(self):
        self._dead = True          # graceful close 経路を通さない
        original_close(self)

    monkeypatch.setattr(plugin_sandbox.PluginSession, "close", _hard_close)
    loop.commit(mission=_mission(ctx), ctx=ctx,
                result=_completed_result(
                    _plugin_artifact("rsi_pullback", kind="strategy")),
                now=fx.NOW)
    assert "cpu_sec=null" in _activity_text(activity)


def test_prompt_inventory_includes_params_outputs_and_hash(tmp_path):
    """P3 / §2.9(a): prompt の `current_inventory.approved_plugins` が
    indicator の params / outputs / content_hash を含む。"""
    from tests.fixtures import indicator_wiring as fx
    loop, conn, root = _improve_env(tmp_path)
    plugins_root = root / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    rendered = loop._last_rendered_prompt
    assert '"outputs"' in rendered or "outputs" in rendered
    assert "period" in rendered
    # 遮断 8: 成績・段名は出さない
    for forbidden in ("holdout", "pf=", "avg_r"):
        assert forbidden not in rendered
```

- [ ] **Step 5-6b: Run test to verify it fails**

Run: `uv run pytest tests/loops/test_improve_e2e.py -k "backtest_cpu or prompt_inventory" -q`
Expected: FAIL — `backtest_cpu` 行が 1 つも書かれない

- [ ] **Step 5-6c: Write minimal implementation**

`src/agentic_fx/loops/improve_loop.py::commit` の strategy 分岐で、
`_run_strategy_gate` の戻り (verdict) から activity を書く
(`strategy_verdict.evaluable` 判定の**前** — 落ちた候補でも CPU は記録する):

```python
                    # [indicator-consumption-wiring] §2.9(e): commit gate は
                    # adapter を `strategy_gate` の内部で生成・close するため、
                    # CPU 実測は `StrategyGateVerdict.cpu_samples` 経由でしか
                    # ここへ届かない (codex r5 I4)。scope × pair ごとに 1 行。
                    deps = len(candidate_meta.indicators)
                    for scope, pair, cpu_sec in strategy_verdict.cpu_samples:
                        self._activity.write(
                            Category.IMPROVE, "backtest_cpu",
                            f"mission={ctx.mission_id} "
                            f"plugin={artifact['name']} scope={scope} "
                            f"pair={pair} deps={deps} "
                            f"cpu_sec={'null' if cpu_sec is None else cpu_sec}")
```

`run_backtest_handler` 経路 (`_build_rpc_handlers`) は adapter を自分で持つので
直接読む (`intent_source.close()` の後):

```python
                finally:
                    intent_source.close()
                    cpu_sec = intent_source.cpu_sec
                    conn.close()
                self._activity.write(
                    Category.IMPROVE, "backtest_cpu",
                    f"mission={staging_dir.name} plugin={args['name']} "
                    f"scope=in_sample pair={args['pair']} "
                    f"deps={len(meta.indicators)} "
                    f"cpu_sec={'null' if cpu_sec is None else cpu_sec}")
```

`src/agentic_fx/loops/improve_context.py::_current_inventory` を snapshot 由来にする:

```python
def _current_inventory(conn, settings, root: Path, *,
                       inventory_view: dict | None = None) -> dict:
    """[indicator-consumption-wiring] §2.9(a): prompt の
    `current_inventory.approved_plugins` は **mission の inventory view**
    から作る (prepare 後に live `plugins/` を差し替えても不変、P3)。
    `inventory_view` が渡されない経路 (CLI 表示等) は従来どおり
    `approved_plugins` を呼ぶ。

    載せるのは name / kind / pairs / params / outputs / content_hash のみ —
    成績・期間・段名は載せない (遮断 8)。"""
    if inventory_view is not None:
        plugin_summaries = [
            {"name": p["name"], "kind": p["kind"], "pairs": p["pairs"],
             "params": p["params"], "outputs": p["outputs"],
             "content_hash": p["content_hash"]}
            for p in inventory_view["plugins"]]
    else:
        plugins = approved_plugins(conn, root / "plugins",
                                   settings=settings).inventory.metas
        plugin_summaries = [
            {"name": p.name, "kind": p.kind, "pairs": list(p.pairs),
             "params": p.params,
             "outputs": (list(p.outputs) if p.outputs is not None else None),
             "content_hash": p.content_hash} for p in plugins]
    ...  # news_sources / risk_gate は現行のまま
```

`build_improve_context(...)` に `inventory_view=None` を足して透通させ、
`ImproveLoop.prepare` が `ctx.inventory_view` を渡す。

- [ ] **Step 5-6d: Run test to verify it passes**

Run: `uv run pytest -q`
Expected: PASS (フルスイート)

- [ ] **Step 5-6e: Commit**

```bash
git add src/agentic_fx/loops tests/loops
git commit -m "feat(improve): backtest_cpu activity and richer prompt inventory (C1/P3)"
```

**T5b 完了条件**:
- [ ] **F5**: 改善 commit gate の未解決 (unpinned を含む) で
      `gate_failed reason=indicator_unresolved`、`last_result == "indicator_unresolved"`
      (**完全一致**、alias も cause も混ざらない)、gate 行 0、approval 行 0
- [ ] **U4a (改善経路)**: `outputs` なし indicator 候補は `last_result == "outputs_required"`、
      approval 行 0
- [ ] **F4 (handler 側)**: `run_backtest_handler` が未解決で backtest を走らせず
      `{"started": false, ...}` を返す。staging 内 indicator は `available` に出ない
- [ ] **P3 (payload)**: 改善 commit の payload にも `indicator_deps` が入り、
      submit / bless と同形
- [ ] **P4 (改善経路)**: `_run_plugin_gate` が `ctx.inventory` を `find_noop_copy` へ渡す
- [ ] **C1 (activity)**: `IMPROVE backtest_cpu mission=… plugin=… scope=… pair=… deps=N
      cpu_sec=<float|null>` が scope × pair ごとに 1 行 (逐語)。
      逐語 pin は `wiring_envs.activity_text(activity)` 経由で取る
      (`ActivityLog.read_text()` は存在しない — opus r1 C3)
- [ ] 段 0 変異 red:
      (c) `_finalize_gate_failed(reason="indicator_unresolved")` を
      `reason=f"indicator_unresolved:{exc.alias}"` にする →
      `test_last_result_never_carries_alias_or_reason` が落ちる
      (e) `backtest_cpu` の `cpu_sec` を常に `0.0` にする →
      `test_backtest_cpu_activity_lines_are_verbatim` の `cpu_sec=null` が落ちる

---

## プラン規約

- **設計を変えない。** 設計書 v1.4 (§0 の U1〜U6 + §8〜§14 の対応表) が正。設計書に無い
  判断が必要になったら**実装を止めて指揮者へ申告**する。設計レビューは 9 周 + opus 1 周で
  収束済みなので、同じ論点の蒸し返しには「設計書 §X で決着済み」と返して閉じる
- **逸脱は必ず申告する** ([[haiku-silently-adapts-report-deviations]])。プランの Step
  どおりに書けなかった箇所は、実装報告に「Step 番号 / 何を / なぜ」を明示すること。
  黙って回避した差分は検収で抜き取り検査する
- **逐語転写は機械 diff する** ([[transcription-must-be-machine-diffed]])。本プランの
  コードブロックを写した箇所は `git diff` を目視でなく `diff` で突き合わせること。
  「検証した」という自己申告だけで green を信じない
- **検収は削除行から読む** ([[spec-must-check-existing-guards]])。本束は既存の遮断
  (fail closed の `_reject` / `check_source` / hash 再検証 / lock / 遮断 8) と
  密に交差する。`git diff` の削除行に既存ガードが含まれていないかを最初に見る
- **未コミットの subagent 差分の上で `git checkout` しない**
  ([[no-git-checkout-over-uncommitted-subagent-work]])。変異確認の復元は
  `git stash` か patch で行う
- **一時ファイルは `tmp/`** に置く ([[temp-files-location]])。`rm` を使ってよいのは
  自分が同セッションで作った一時領域だけ ([[rm-allowed-directories]])
- **実 DB / 実 `plugins/` を触らない**。fixture は `tmp_path` のみ。`git status` と
  `data/` の mtime で検収する
- **fresh worktree でフルスイート**を最後に回す (残骸ゼロ、空 `logs/` の残留も無い)

## レビュー段

1. **段 0 (指揮者の変異スイープ)** — レビュー前に必ず回す。最優先 8 件 =
   - M1: `resolve_indicator_deps` の `pin_mode == "require"` 判定を反転 (R1 / F3')
   - M2: `approved_plugins` 第 2 相の reject を admit に潰す (F2 / P2)
   - M3: worker の `sub_df.copy(deep=True)` を `df.tail(...)` に戻す (V2)
   - M4: `PluginSession.__enter__` の indicator hash 再検証を削る (V3)
   - M5: `check_source` の `ast.Store` 分岐を削る (V2)
   - M6: `_plugin_locks` の `sorted(set(...))` を `list(names)` にする (P2'')
   - M7: `run_kind_gate` の `except IndicatorResolutionError` を `raise` に変える (F3)
   - M8: `release_backtest` を no-op にする (F4)。**観測点は
     `counters.backtest_calls["cand"] == 0` の直接 assert** —
     `dict(counters.backtest_calls) == before_calls` の形では正しい実装でも
     落ちるため判別力がゼロ (opus r1 I12、[[mutation-testing]] の
     「pin の観測点は probe で決める」)

   加えて**各 task の完了条件に書いた逆変異**をすべて回す。
   観測点は probe で決める (「落ちるはず」で済ませない — [[mutation-testing]])
2. **1 周目**: codex + ローカル LLM 3 本 (枠ゼロ、並列可)。
   **ブリーフに必ず含める材料**:
   - 設計書 v1.4 の §2.3 の root 表・§2.7 (identity とロック方式)・§2.8 (非送出規律)
   - Global Constraints の固定文言語彙一覧 (これを知らないレビュアーは
     「理由の分からないラベルは不親切」と逆方向の指摘を出す)
   - 遮断 8 のただし書き (`last_result` は 1 bit、alias/cause は activity のみ)
   - 「codex 設計レビュー 9 周 + opus 1 周は全件採用済み」の一文
   - codex は **terra/medium** を基本、`resolve.py` / `switch.py` の tx・lock・
     プロセス境界に触る周だけ **sol** ([[codex-operations]])
3. **2 周目**: `/code-review high` + codex + ローカル 3 本。有償 2 本
   (`/code-review` と sonnet) は並列にしない。`/code-review high` は指揮者から
   起動できないのでユーザーに打ってもらう ([[code-review-runs-on-session-model]])
4. **3 周目 (must-fix のみ)**: ブリーフ付き sonnet。2 周目の結果に応じて要否を判断
5. **レビューを投げる前に、メモリの `review-process` の「投げる前のチェックリスト」を
   読むこと** — 材料の作り方がレビュアーの成績を左右する

**レビュアーに特に見てほしい軸** (ブリーフに書く):

- **解決は本当に 1 箇所か** — `rg -n "resolve_indicator_deps\(" src` の全結果が
  composition root か resolver 自身であること。消費側 (adapter / session / gate /
  producer) が再解決していないこと (P3')
- **`InventoryBuildResult` への移行が全数か** — `rg -n "approved_plugins\(" src tests`
  の全結果。`len` / index / equality を含む list 利用が残っていないこと
- **fail closed の抜け** — 未解決が「hold の連続」「空 indicators で evaluate」
  「pin 無しで承認」に読み替わる経路が 1 つも無いこと
- **遮断 8** — `last_result` / prompt / RPC 応答 / `inventory_view` に holdout の数値・
  段名・pair・成績が 1 文字も出ないこと (`rg` で全数追跡)
- **同居実行の残余リスク** — deep copy / 一意名 import / グローバル状態 assert の
  3 面が揃っていること。`except` ハンドラが故障源を共有していないか
  ([[except-handler-shares-failure-source]])
- **共有リーダは層をまたぐ** ([[shared-reader-crosses-layer-boundaries]]) —
  `PluginMeta.indicators` / `.outputs` を読む箇所の全数 grep
- **テストの破壊力** ([[tests-touching-real-repo-resources]]) — 新規テストが実 DB /
  実 `plugins/` / `docs/examples` を書き換えていないこと
- **E2E が実物か** ([[test-fixtures-from-real-transcripts]]) — A1 / A1-b / C1 / V2 /
  V3 が実 worker + 実 sqlite で回っていること。モックが潰した次元は変異では取れない
- **配線そのもの** ([[verify-integration-not-just-units]]) — 「単体は緑だが誰からも
  呼ばれない」が残っていないか (特に `decision_sink` / `cpu_samples` /
  `inventory_view` / `release_backtest`)

## 完了条件 (束全体)

- [ ] T1 / T2 / T6a / T6b / T3 / T4a / T4b / T5a / T5b がすべて実装完了
- [ ] **設計書 §6 の受入 ID 32 件が全て pin として存在する** (下限なので追加は可。
      `S1'` は ID ではなく S1 内の観測点 — codex plan r1 M2):
      U4a / U4b / L1 / R1 / A1 / A1-b / A1' / A1'' / A2 / F1 / F2 / F3 / F3' / F4 / F5 /
      V1 / V2 / V3 / S1 / P1 / P1' / P2 / P2' / P2'' / P3 / P3' / P4 / P5 /
      D1 / R2 / C1 / N1
- [ ] 各 task の完了条件の逆変異がすべて RED
- [ ] **DB スキーマ変更ゼロ** (`git diff src/agentic_fx/store/db.py` が空)
- [ ] **`config/settings.yaml` / `.example` の差分ゼロ** (本束の上限はコード定数)
- [ ] **実 DB を書き換えていない** (`git status` と `data/` の mtime で確認)
- [ ] **実 `plugins/` を書き換えていない** (`docs/examples/plugins/` の更新は対象内)
- [ ] `rg -n "approved_plugins\(" src tests` の全結果が新契約
- [ ] `rg -n "indicators=None|\"indicators\": None" src` が 0 件
      (`worker.py` が `evaluate(df, indicators, None, params)` を渡すのは
      `signals` の位置 — N1 の pin で観測する)
- [ ] fresh worktree でフルスイート green (残骸ゼロ、空 `logs/` の残留も無い)
- [ ] `uv run pytest -m slow -q` (A1 / A1-b / decision sink) も green
- [ ] レビュー段の完了
- [ ] **実機 prerequisite は本束の完了条件ではない** (U6): 系列版 indicator 3 本
      (sma / rsi / adx) の `plugins/_human/` ひな形は fable が作り、submit → approve は
      ユーザーが行う。**実装完了後・validator が系列を通してから**着手する

## 運用観測 (受入ではない、A4 で)

prerequisite 3 indicator 配備後、deps=0 と deps=3 の同一 strategy で in_sample を完走し
`backtest_cpu` の差を記録する。目安 = deps=3 が 60 秒予算の 50% 未満 (設計書 §6 末尾)。

## 進捗表

| task | 状態 | commit | 備考 |
|---|---|---|---|
| T1 loader + resolve + approved_plugins 二相 | 未着手 | - | Step 1-1〜1-7 (7 Step)。受入 L1 / R1 / P1 (resolver 部分)。全 caller 移行を含む |
| T2 sandbox + worker の同居実行 | 未着手 | - | Step 2-1〜2-6 (5 Step: 旧 2-2/2-3 を統合 — opus r1 M8)。受入 V1 / V2 / V3 / S1 (末尾射影の観測点を含む) / C1 (sandbox 部分) / N1 |
| T6a examples + fixture + docs | 未着手 | - | Step 6-1〜6-4 (4 Step)。受入 A1 の fixture 部分。T2 後に worktree 並列可、**T3 前にマージ** |
| T6b `wiring_envs` (22 ビルダ + smoke) | 未着手 | - | Step 6b-1 (1 Step、22 ビルダ)。受入 ID なし (T4a 以降すべての基盤)。**T3 と並列可、T4a 着手前にマージ** |
| T3 composition root | 未着手 | - | Step 3-1〜3-5 (5 Step)。受入 F1 / F2 / A1' / A1'' / P1 (人間 CLI)。T6a の fixture を使う |
| T4a gate 判別子 + A1 E2E | 未着手 | - | Step 4-1〜4-3 (3 Step)。T3 + T6b 後。受入 F3 / F3' / A1 / A1-b / U4a / U4b (承認経路) / C1 (verdict)。A1 は実 worker で数分 |
| T4b lock / 再解決 / noop / reconcile | 未着手 | - | Step 4-4〜4-8 (5 Step)。受入 A2 / P2 / P2' / P2'' / P3 (payload) / P3' / P4 / P5 / D1 / R2 |
| T5a improve context 露出 + RPC 予約 | 未着手 | - | Step 5-1〜5-3 (3 Step)。受入 F4 (counters/RPC) / P1 (改善経路) / P1' / P3 (露出部分) |
| T5b handler / commit gate / activity | 未着手 | - | Step 5-4〜5-6 (3 Step)。受入 F5 / U4a (改善経路) / F4 (handler) / P3 (payload) / P4 (改善経路) / C1 (activity) |
| レビュー段0 (変異スイープ) | 未着手 | - | 最優先 8 件 (M1〜M8) + 各 task の逆変異 |
| レビュー1周目 (codex + ローカル3) | 未着手 | - | ブリーフに固定文言語彙と遮断 8 を含める |
| レビュー2周目 (`/code-review high` + codex + ローカル3) | 未着手 | - | ユーザーが打つ |
| レビュー3周目 (sonnet、要否判断) | 未着手 | - | |
| fresh worktree フルスイート (slow 込み) | 未着手 | - | 残骸ゼロ + `logs/` 未生成 |
| U6 prerequisite (fable ひな形 → ユーザー承認) | 未着手 | - | **本束の完了条件ではない**。実装完了後 |

> 指揮者裁定 (2026-09-14、設計書 v1.2 の §0 U1〜U6 を実装プランへ写す):
> U1 = 渡す indicator は strategy が config で宣言した依存のみ / U2 = indicator は
> 系列も返せる (スカラー返却は従来どおり有効) / U3 = 依存宣言は別名付き・params 上書き可 /
> U4 = `outputs` は新規承認 (submit / bless、kind=indicator) で必須、宣言なしの既存配備は
> `outputs: null` で standalone のみ可・依存先にできない (`outputs_undeclared`) /
> U5 = ロック時の `config.yaml` は全体を再シリアライズ (コメント・キー順は保持せず差分表示) /
> U6 = 実機 prerequisite の indicator 3 本は fable がひな形を作りユーザーが submit → approve。
> **蒸し返しの裁定は不要** — 設計レビュー codex 9 周 (r1〜r9、r9 は指摘 0) + opus 1 周は
> 全件採用済みで設計書 §8〜§14 に確定記録済み。

> ユーザー裁定 (2026-09-14、指揮者の既定選択 8 件の採否): 指揮者が「裁定不要」と
> 判断して先行実装していた既定選択 8 件のうち、**① と ⑥ は本裁定で変更**し、
> **②③④⑤⑦⑧ は変更なしで採用** (「ユーザー裁定 2026-09-14 採用」)。
>
> ① **validator の置き場 (裁定で変更)** = **`src/agentic_fx/core/plugin_contract.py`**
> (単一モジュール。指揮者の元案 `plugin/indicator_validate.py` は**採らない**)。
> 根拠: `core/contracts.py` の前例 (全レイヤー共有の契約型を置く場所は `core/`)、
> worker エントリが既に `agentic_fx.core.landlock` を import している、`core` は
> 上位層 (`plugin/sandbox.py` 等) に依存しない性格のモジュールを置く場所である
> (worker は `sandbox.py` を import しない既存規律を守るため、置き場自体を独立
> モジュールにする必要があるのは元案と同じ)。モジュール docstring に「親
> (`plugin/sandbox.py`) と sandbox worker (`plugin/worker.py`) の両方から
> import される。`agentic_fx.plugin.*` を import してはならない」を明記する
> (Step 2-1c)。プラン全体の `plugin/indicator_validate.py` /
> `agentic_fx.plugin.indicator_validate` の参照 (import 文・File Structure・
> Files・Interfaces・Step 本文・テストの import・変更履歴) は全数
> `core/plugin_contract.py` / `agentic_fx.core.plugin_contract` に置換済み
> (`grep -n "indicator_validate"` 残存 0)。テストファイルも
> `tests/core/test_plugin_contract.py` (`tests/core/test_contracts.py` と
> 同じ配置規約) に移す。
> ② **`PluginSession(resolved=None)` の既定** = indicator/signal
> セッション (`market_tools.run_plugin` / `signal_eval`) が `resolved` を持たないため
> `None` 既定にし、`kind == "strategy"` かつ `resolved is None` を `SandboxError` にする
> (F1 の `TypeError` は adapter の契約であってセッションの契約ではない)。
> **ユーザー裁定 2026-09-14 採用 (変更なし)** /
> ③ **`_KIND_PAYLOAD_KEYS["strategy"]`** = `("df", "params")` — `indicators`/`signals` は
> worker が組み立てるので wire に載せない (既存の `call()` は余分なキーを無視するため、
> `test_sandbox.py` の payload 縮小は挙動を変えない)。**ユーザー裁定 2026-09-14 採用
> (変更なし)** /
> ④ **`cpu_sec` の plugin error 後** =
> plugin コード自身の例外はセッションを `_dead` にしない既存契約があるため graceful
> close が成立し **float** が入る。`None` は SIGKILL fallback と worker 未起動の 2 経路のみ。
> **この解釈は「既定選択」ではなく指揮者へ申告済みの設計是正**であり、
> **設計書 v1.2 の §6 C1 行で本文が改訂された** (opus r1 I10)。Step 2-4a の
> `test_cpu_sec_is_float_after_plugin_error` が逐語で固定する。**ユーザー裁定
> 2026-09-14 採用 (変更なし)** /
> ⑤ **`same_modulo_pins` / `is_relock_transition` の引数型** = `Path` (plugin ディレクトリ)。
> `noop_gate` が既に Path ベースで比較しているため。**ユーザー裁定 2026-09-14 採用
> (変更なし)** /
> ⑥ **lock 後の候補の snapshot 検査 (裁定で代替)** = 指揮者の元案「`check_candidate_snapshot`
> は 3 ファイルの存在と属性しか見ない (`gate_pytest.py:57-117`) ため、`lock_config` の
> 書き換え後も submit は通る (追加の snapshot 更新は不要)」は**不採用**。**代替**:
> lock (`lock_config` / `afx plugin lock --from _human` / `lock_staging_deps`) の後は
> **必ず snapshot (content_hash) を取り直す** — `check_candidate_snapshot` が
> 「壊れるものはない」と主張する範囲 (ファイル 3 本の存在・属性) だけに依存しない。
> `lock_config` は書き込み**後**に disk を独立に再読して `content_hash()` を計算し直し、
> `(before_text, after_text, new_content_hash)` の 3-tuple を返す (Step 1-5)。呼び出し元
> 3 箇所は resolve 時点の hash を使い回さずこの `new_content_hash` を使う: T1 の
> `lock_config` 単体テスト (`new_hash == content_hash(...)` を独立 assert)、T3 の
> `_plugin_lock` (`afx plugin lock --from _human`、`new_hash` を `discover_one_with_reason`
> の再取得結果と突き合わせ assert)、T5a の `lock_staging_deps` (同様の assert + 応答
> `content_hash` フィールドをテストで pin)。3 箇所とも discover の再取得値と
> `lock_config` の `new_content_hash` が一致することを assert し、食い違えば
> `lock_config` か配線のバグとして即座に落ちるようにする /
> ⑦ **A1 / A1-b / decision sink の実行時間** = `pytest.mark.slow` を付け、
> `uv run pytest -m "not slow"` で日常ループから外せるようにする (検収では必ず回す)。
> **ユーザー裁定 2026-09-14 採用 (変更なし)** /
> ⑧ **`handshake_too_large` は正常入力から到達しない** — loader が通す最大は
> deps 8 × params 8 KiB (+ strategy 側 8 KiB の上書き) で handshake ~136 KB に
> しかならず、既定の `MAX_HANDSHAKE_BYTES = 262144` には届かない。よって
> R1 の境界テストは定数を monkeypatch して `>` 判定そのものを pin する形にする。
> **設計書 v1.1 §6 R1 は「同一 plugin の 8 alias × 大 default params で到達させる」と
> 書いていたが、この算術は成り立たない**。指揮者へ申告し、**設計書 v1.2 の §6 R1 行で
> 「定数 monkeypatch による境界試験。正常入力では到達不能 (8 alias × 8 KiB ≒ 136 KB
> < 256 KiB) であることを注記」へ改訂済み** (opus r1 I10)。**この上限は「将来 params
> 上限を緩めたときの最後の壁」として残す** — 実装から消してはならない。**ユーザー裁定
> 2026-09-14 採用 (変更なし)**。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-18 | v1.8 | **2 周目レビュー 3 レーンのトリアージと是正 (`cba93e4`..`85e3e7c`、14 commit)。** **`/code-review high` (sonnet、9 件)**: CR1 採用 — `switch._dependency_names` が gate 前に未検証の plugin 名を flock パスへ渡していた。probe で**指摘より重い**ことが判明 (`plugin: ../evil` → `plugins/evil.lock`、`plugin: /tmp/x` → `/tmp/x.lock` を実際に作成 = 任意パスへのファイル作成 sink)。source (`_dependency_names`: 非 mapping と `_PLUGIN_NAME_RE` 不一致を足さない) と sink (`_plugin_lock`: 正規形でない名前を `ValueError`、`mkdir` より前) の両方で閉じた。parse 失敗の `[name]` 縮退は現状維持 (submit/bless は `_run_full_gate` → `_discover_one` が、approve は payload の `content_hash` 照合が必ず止めることをテストで pin) / CR2 採用 (ユーザー承認、設計書 v1.6) — 再ロック時の質検査 skip を**自己一致除外**へ締め付け / CR3 採用 — tx 内の `is_relock_transition` を `noop_gate` と同じ except で守る / CR4 採用 — 承認詳細 (i) 欄を `phase1_metas` に (設計書 v1.6) / CR7 採用 — `outputs_required` を `approval.outputs_required_violation` に一本化 / **CR5・CR6・CR9 却下** (設計既定・`inventory_view` に `max_bars` が無い・性能のみ) — 却下根拠をコード側コメントと pin 1 本 (`run_backtest_handler` が backtest 時点で `over_max_bars_limit` を返す) に残した / **CR8 見送り** (1 周目是正の形は正しく動作、`finally` 復元は probe で健全を確認) / 未ランク 4 件のうち **flock 再入の docstring 主張は誤り**と実測 (別 ofd の `LOCK_EX` は `BlockingIOError`) — `sorted(set(...))` は spy 契約ではなく **自己デッドロック回避のために必須**、docstring 訂正 + 非再入性を pin。 **codex terra (2 件)**: X1 採用 — lock 後の rollback が第三者更新を旧内容で上書きしていた (失敗経路自身が競合相手の変更を破壊)。書き戻し前に現在値が自分の `after` と一致することを 確認する形にし、codex が名指ししていない `loader_rejected_after_lock` 分岐にも適用。実ファイル競合の回帰テスト 2 本 + `if True` 変異で両方 red を確認 / X2 採用 — `afx plugin lock` の docstring 文言。 **ローカル LLM 3 本 (9 チャンク × 3 = 27 走、全て `finish=stop` / `content_len>0`)**: 指摘 57 件 → 別々の穴として確定 2 件 (c01 系列 envelope のキー集合厳密一致 = muse+qwen の 独立 2 本、c06 候補 kind ガードの signal 欠落)、いずれも変異 SURVIVED → pin → 逆変異 red を 実測。`_indicator_result_to_wire` の list 分岐 NaN 漏れ (独立 3 本) は probe で**到達不能**と 判明したため N + 潜在的な罠として 1 行だけ安全側に揃えた。`outputs: []` 系の指摘 2 件は loader が空リストを reject するため**等価変異**。 **フルスイート 4177 passed** (途中 10 failed は CR2 の行番号 allowlist ずれ 1 件と CR7 の退化した `_discover_one` double 9 件の 2 原因、`8fe57fb` で 8 件・`85e3e7c` で残り 2 件を是正) | 2 周目レビュー材料 `tmp/review-20260918-iw-r2/{code-review.md,codex/,local/}`、トリアージ記録 `tmp/review-20260918-iw-r2/triage-r2.md` | `cba93e4`..`85e3e7c` |
| 2026-09-17 | v1.6 | Global Constraints の遮断 8 の項を改訂。「alias/cause は activity 行にだけ書く」が **RPC 応答も含む全 sink の規律**と読め、設計書 §2.9c (`{"started": false, "error": "indicator_unresolved", "alias", "reason", "available": [...]}`、codex 設計レビュー r4 C1 で確定) と矛盾していた。規律の適用先を `last_result` / `gate_failed` reason / 提案レポート本文に限定し、改善 RPC 応答は §2.9c のとおり alias/reason/available を返すと明記。sink 一覧 (6 経路) の参照先を設計書 §5 (v1.5) にした。見出しの版表記が v1.4 のまま v1.5 行が追記されていた drift も是正 | 1 周目 codex レビュー 束 2 [Critical] 「RPC 応答へ alias/cause が漏れる」= **却下** (設計判断済) だが、**プラン記述側の欠陥**として是正(指揮者裁定 2026-09-17) | - |
| 2026-09-15 | v1.4 | **codex プランレビュー r2 (2 周目、4 束並列。合計 Critical 2 / Important 3 / Minor 2、token 実測 = 4 束合計 約 350k) を全件是正。** **Critical**: 束2 — `SignalProducer.evaluate_due_plugins` の `resolved_by_hash: dict[str, ResolvedIndicatorSet]` (content_hash 単独キー) は、同一 content_hash・別名の 2 strategy が存在すると `InventoryBuildResult.resolved` (キーは `(name, content_hash)`) から dict comprehension で潰す際にどちらかが失われ、Global Constraints の既存 identity と P3' の「同一オブジェクト」不変条件に反する。`resolved_by_identity: dict[tuple[str, str], ResolvedIndicatorSet]` に改名しキーを `(meta.name, meta.content_hash)` に統一 (Step 3-2a/c、File Structure、T3 Interfaces Produces、完了条件 F2、段 0 変異 red を更新)。session cache は **`content_hash` 単独のまま維持**と判断 (根拠: content_hash は `plugin.py + config.yaml` のバイト列そのもの — 一致する 2 strategy は依存宣言も含めてバイト単位で同一なので、渡す `resolved` の identity は `(name, hash)` 単位に保ちつつ、subprocess 再利用の単位だけ content だけで 決めても矛盾しない。改名した subprocess 二重起動の回避にもなる)。P3' の回帰ケース `test_producer_uses_name_and_hash_identity_not_hash_alone` (同 hash・別 name の 2 strategy) を新設 / 束3 — T4b Step 4-6c の P5 (`_finalize_success` → `_check_duplicate_metrics_for_approval(inventory=ctx.inventory, ...)`) が、依存順で 後続の T5a Step 5-1 が所有する `ImproveRunContext.inventory` を先取りしており、本番 `AttributeError` になり得た。`ImproveRunContext.inventory` / `.inventory_view` の 2 フィールド追加 (v1.3 C4 の互換既定値付きの形はそのまま) を **T4b へ前倒し**し、T5a Step 5-1 からはフィールド追加を削除して「実 `prepare()` が非空の値を書き込む 配線」だけを Consumes する形に縮小 (T4b/T5a の Files・Interfaces・Produces・Consumes・依存グラフの所有範囲を更新)。**フィールドは前倒ししたが実配線は前倒ししていない**ため、T4b の時点では `ctx.inventory` は常に既定値 `None` — `_finalize_success`の P5 呼び出しは Step 4-6c で定義済みの暫定 helper `self._inventory_for_gate(conn)` へ フォールバックする形にし (`ctx.inventory if ... is not None else self._inventory_for_gate(conn)`)、T5b Step 5-5 (`_inventory_for_gate` 削除と同じコミット)でフォールバック式を `ctx.inventory` の直渡しへ単純化する後始末を明記。新規回帰テスト `test_finalize_success_falls_back_to_inventory_for_gate_when_ctx_inventory_is_none` を T4b Step 4-6a に追加 (T4b 完了条件 P5・段 0 変異 red にも反映)。 **Minor**: 束3 — `_check_duplicate_metrics_for_approval` の戻り値注釈を `str | None` から `_DuplicateDemotion | None` に訂正 (既存実装・呼び出し元の `if demotion is not None:` 分岐と整合) / 束1 — `PluginSession.__enter__` の kind 契約が片方向 (strategy は `resolved` 必須のみ検査) だったのを、`meta.kind != "strategy" and resolved is not None` も `SandboxError` にする双方向検査に強化し、単体テスト `test_indicator_session_with_resolved_is_rejected` を追加 (V3 完了条件・段 0 変異 red)。**Important**: 束1 — `sandbox._validate_indicator_result` が wire 形の再検証しかせず `meta.outputs` との一致を見ていなかった (worker を迂回/破損した応答の余分 key・欠落 key を親が受理し得た) のを `outputs: tuple[str, ...] | None` を受け、wire 復元後に `core.plugin_contract.validate_indicator_result(out, df_index=None, outputs=outputs)` を通す形に是正 (`PluginSession.call()` の呼び出し側も `outputs=self._meta.outputs` を 渡すよう更新)。テスト `test_standalone_indicator_response_rejects_extra_or_missing_outputs_keys` を追加 (V1 完了条件・段 0 変異 red) / 束1 — `test_every_approved_plugins_caller_consumes_the_inventory_result` が「直後の属性参照」しか検査しておらず、`x = approved_plugins(...)` の中間変数代入 (現行 4 caller のうち 2 本が実際にこの形) を経由した誤用をすり抜けていたのを、`Assign` の戻り値変数を追跡し後続の `.attr` 参照も同じ許可属性集合へ限定する 2 パス AST 検査に強化 / 束4 — `test_lock_staging_deps_refuses_outputs_undeclared_dependency` がモジュール共有の `_tools(tmp_path)` 既定引数 (`view=_VIEW`) に暗黙に 依存していたのを、`legacy` (`kind="indicator"`, `outputs=None`) を含む `inventory_view` をテスト自身が明示的に組み立てて渡す形に変更し、U4b (`outputs_undeclared`) 分岐への到達をこのテスト単体で自己完結して保証する形に 是正 | codex プランレビュー 2 周目 (4 束並列) `tmp/plan-indicator-wiring/r2/{codex,mat}-{1,2,3,4}.md` | - |
| 2026-09-15 | v1.3 | **codex プランレビュー r1 (Critical 5 / Important 10 / Minor 5 — ヘッダの「Minor 4」は本文の実数 5 と食い違っていたので 5 件として扱った) を全件是正。** **Critical**: C1 T4b の strategy submit テスト群に gate double を明示注入 (`switch_env` は空 DB、`submit_candidate` は実 `_run_full_gate` から実 backtest を回すので全ケースが `NoHistoryError` で落ちていた)。`wiring_envs.install_gate_double` を新設 (22 本目のビルダ)、T4b Interfaces に「strategy を submit/bless する全ケースで double か `seed_history` を明示的に選ぶ」規律と `_run_full_gate` 到達前に落ちる例外ケースを明記、`settings` を `SETTINGS_FIXTURE` に統一 / C2 T3 → T4a の契約破綻 (T3 が `run_kind_gate(..., resolved=, inventory=)` を呼ぶが T4a の最終シグネチャは `resolved` を受けず内部解決する) を **T4a への一本化**で解消 — T3 は `switch.py` を一切触らず `run_kind_gate` の**本体**だけが `ResolvedIndicatorSet.empty(meta.path.parent)` を渡す暫定になり、`_run_full_gate(..., plugins_root)` の引数追加と inventory 構築は T4a Step 4-2c が所有 (T3/T4a の Files・Interfaces・Produces・重複していた `_plugin_locks`/`find_noop_copy` を整理) / C3 `_check_duplicate_metrics_for_approval` を `(conn, payload, *, inventory, staging_dir)` に、`_deployed_dir_for(name, *, inventory)` / `_candidate_dir_for(payload, *, staging_dir)` に変更し (旧案は未束縛の `inventory` / `ctx` を読む = 本番 `NameError`、テストは helper を monkeypatch して隠していた)、caller 移行表 3 件と**実 helper を使う統合テスト** `test_relock_detection_uses_the_real_dir_helpers` を追加 / C4 `ImproveRunContext.inventory` / `.inventory_view` に互換既定値 (`None` / `field(default_factory=dict)`) を置いて現行 57 箇所の直接構築を壊さない形にし、`_build_rpc_handlers(..., inventory=None)` で現行 33 caller を維持、`wiring_envs.synthetic_ctx` を T5a の Files に足して空 inventory を明示、**実 `prepare` 経路だけが非空を持つ**ことを `test_prepare_populates_a_non_empty_inventory` で pin / C5 `list_deployed_plugins` の遮断 8 検査を JSON 全文の部分文字列から**フィールド名の再帰走査 (`params` 配下は除外)** に置換 (同じ `_VIEW` が `params: {"period": 14}` を持つため、正しい出力でも必ず落ちる判別力ゼロの assert だった。設計は params 露出を明示的に許可)。 **Important**: I1 `approved_plugins` の caller 表を実測で再生成 (17 → **19**)、全数性 AST テストを `src` + `tests` の双方走査に拡張 + 戻り値属性の検査テストを追加、期待 FAIL を 5 件に訂正 / I2 `build_intent_source` の caller 表を実測で再生成 (patch 12 → **15**、直接呼び出し 7 → **16**)、AST による `resolved=` 必須検査を追加、Step 3-1d の Run に `tests/loops tests/integration` を追加、commit 対象に `improve_loop.py` と非-plugin テストを追加 / I3 trade worker テストを実入口経由に — `mission_worker._build_trade_indicator_metas(conn, plugins_dir, settings)` を新設して helper を実物として呼び、`plugins_dir` / `settings` / `.inventory.metas` / indicator-only filtering を spy で観測 (旧案はテスト自身が fake を呼んで `build_mission_registry` に渡しており src を 1 行も実行していなかった) / I4 A2 fixture が live symlink の解決先 (= 承認済 I1 の version 実体) を改変していたのをやめ、`bump_indicator_version` だけで新版を作る形に / I5 P3 三経路同形テストに **bless payload** と `_build_approval_payload` を足して 3 payload を相互比較 (旧案は submit payload しか見ておらず bless からキーを落としても緑) / I6 P3' の spy 先を `plugin.resolve` モジュール属性から **call site の `approval.resolve_indicator_deps`** に訂正 (関数を直接 import するため旧案は spy が呼ばれず必ず赤)、両 scope の `is` 一致 pin を T4a Step 4-3 の実 gate E2E (`test_resolved_object_identity_across_both_scopes`) に新設 / I7 C1 の cpu_samples assert を `floor_mode="warn"` + scope 列 `== ["in_sample", "holdout"]` + float 型に (旧案は holdout 0 件でも緑) / I8 F4 に `total_calls` +1 (registry 経由) と `last_result` 不変 (T5b Step 5-4 の実 handler + `improvement_backlog` 見張り行) の観測を追加 / I9 D1 の「決定順 (id)」を実装 — `json_extract(payload_json,'$.name')` + `MAX(id)` で最新承認 id を引いて sort し、**名前順と決定順が逆になる fixture** (`z_first` → `a_second`) のテストを追加。 **I10 (= 設計書 v1.4)**: §6 S1 の「`get_indicators` **と依存の両方**で使える」を U4 / U4b に合わせて「standalone `get_indicators` のみ」に訂正。 **Minor**: M1 T6a の Files から `tests/fixtures/wiring_envs.py` を削除 (所有者は T6b 1 箇所) / M2 版表記を v1.3 (設計書 v1.4 準拠) に、task 数を 9 に、`S1'` を独立受入 ID から S1 内の観測点へ (受入 ID は設計書 §6 の 32 件)、ビルダ数を実数 22 に / M3 `cpu_sec` property の docstring を「正常終了・plugin error 後は float、`None` は SIGKILL fallback / worker 未起動の 2 経路のみ」に / M4 RSI warmup assert を `iloc[:14]` + `iloc[14]` の境界 2 点に (旧案は index 13 を見ておらず warmup が 1 本早く明ける変異を検出できなかった) / M5 handshake の `outputs` を辞書リテラルから `kind == "indicator"` 分岐へ移し、非 indicator では**キー自体が無い**ことを pin | codex プランレビュー 1 周目 `tmp/plan-indicator-wiring/codex-plan-r1.md` | - |
| 2026-09-14 | v1.2 | **ユーザー裁定 (2026-09-14、設計書 v1.3 準拠) を反映、10 件。** ①**置き場変更**: validator を `plugin/indicator_validate.py` から `src/agentic_fx/core/plugin_contract.py` へ (全数置換、`grep -n "indicator_validate"` 残存 0、モジュール docstring に「親 (`plugin/sandbox.py`) と sandbox worker の両方から import される・`agentic_fx.plugin.*` を import してはならない」を追加、テストも `tests/core/test_plugin_contract.py` へ) / ②③④⑤⑦⑧ = 変更なしで採用 (「ユーザー裁定 2026-09-14 採用」と注記) / ⑥**代替**: lock (`lock_config` / `afx plugin lock --from _human` / `lock_staging_deps`) の後は必ず snapshot (content_hash) を取り直す — `lock_config` の戻り値を `(before_text, after_text)` から `(before_text, after_text, new_content_hash)` に変更し (Step 1-5)、T1 単体テスト・T3 `_plugin_lock`・T5a `lock_staging_deps` の 3 箇所で `discover_one_with_reason` の再取得結果と assert 突き合わせるコードを追加。**申し送り F2**: 設計書 §6 F2 を「warning 1 行 (reason 込み)、producer の plugin 一覧に含まれない」に緩和 (session cache 未登録は producer 一覧に無いことの帰結なので別途観測しない)。Step 3-2 のテスト docstring・T3 完了条件を同文言に統一。**申し送り P2'**: 設計書 §6 P2' の「逆順で取っても deadlock しない (順序 pin)」を「`_plugin_lock` の取得順序が常に名前昇順・重複なしであることを spy で pin する」に置換。T4b Step 4-4 に `test_plugin_lock_order_for_approve_candidate_is_sorted_unique` (spy 形、`approve_candidate` の実経路で `_plugin_lock` を monkeypatch し `sorted(set(names))` と一致を assert) を新規追加し、既存の並行ブロッキングテストは相互排除の実測として役割を分離。設計書の変更履歴に v1.3 行、本プランの見出しを v1.2 / 設計書準拠 v1.3 に更新。「指揮者の既定選択」ブロックを「ユーザー裁定 (2026-09-14、指揮者の既定選択 8 件の採否)」ブロックに改題 | ユーザー裁定 (2026-09-14、プラン末尾「指揮者の既定選択」8 件 + 申し送り 2 件の確定) | - |
| 2026-09-14 | v1.1 | **着手前検証 (opus r1: Critical 6 / Important 12 / Minor 13) を全件是正。** **Critical**: C1 `prepare(conn=…)` → `wiring_envs.prepare_ctx(loop, now=)` (現物は `prepare(*, slot_key, now, on_ready)` → 3-tuple、15 箇所を置換) / C2 `improve_env` の `ImproveLoop(activity=, rag=)` 必須引数を追加 (`_FakeRag` は `tests/loops/conftest.py` から逐語転写) / C3 `ActivityLog.read_text()` は存在しない → `wiring_envs.activity_text` (tab 5 列の行形式を smoke test で固定、4 箇所を置換) / C4 `commands.Shell` → `commands.Commands` (必須 7 引数を `tests/test_commands.py` から転写)、`_dependent_strategies` の `self.root` → `self.plugins_root` (None は fail-soft) / C5 `_resolved()` に `pinned=True` / C6 `run_kind_gate(inventory=)` の既存 5 呼び出しの移行表を Step 4-1a に追加。 **Important**: I1 `build_intent_source` の patch 12 + 直接呼び出し 7 = 19 箇所の移行表を Step 3-1a に追加 (`**kwargs` 一律方針) / I2 移行全数性テストを正規表現 → AST (`ast.Call`) 走査に置換 (docstring 6 件の偽陽性を除去、期待 FAIL を 4 件に訂正) / I3 `wiring_envs` を T6b として独立 task 化 + 20 ビルダ全部に smoke test + Produces に逐語シグネチャ / I4 `_project_indicator_output` の入力契約を list に固定し fake を親再検証後の形へ / I5 F4 の `errors` / refusal streak は registry (`on_result`) 経由でしか増えないため registry 経由テストを追加 (`rpc_tooldefs` ビルダを新設) / I6 `improve_env` の conn factory を毎回新規接続に (handler の `conn.close()` でテスト conn が死ぬ) / I7 `latest_in_sample_metrics` に `variant`/`source`/`base_interval` を追加 / I8 `stage_switched_journal` の payload `content_hash` を新 version dir 実体から算出 + `advance_switch_journal(commit=True)` + T6b に reconcile→`decided` の smoke / I9 oracle の `df.empty: continue` を削除、`expected_eval_timestamps` の死に引数 `conn` を削除、adapter の sink 非呼び出し条件を逐語明記 / I10 設計書 §6 の C1 / R1 を **設計書 v1.2** で改訂 (指揮者へ申告済み) / I11 `_strip_forbidden` は denylist と実測確認 → src 変更不要、4 キーの pin テストへ / I12 `dict(counters.backtest_calls)` 比較 (正しい実装でも落ち、変異でも落ちる = 判別力ゼロ) を `backtest_calls["cand"] == 0` の直接 assert へ (段 0 M8 の観測点も同じに)。 **Minor M1〜M13**: `_current` の死に引数を `plugin_dir` に / `strip_pins` の `default=str` を比較専用と明記 / `_check_number` の型 allowlist (数値文字列拒否) / list 分岐の `pd.isna` 曖昧性 / Step 1-1c の `_check_json_safe` スタブを一意に確定 / `grid` 比較を in_sample 期間に + 週末非混入を直接 assert / テスト名 2 件の改名 / Step 2-2 と 2-3 を 1 Step に統合 / Step 4-3c の申し送りを 4-1c へ実際に移動 / 行番号再取得の指示を Global Constraints へ / oracle のメモ化 + `slow` マーク / T3 の Consumes から `wiring_envs` を除去 / `_unresolved_after_switch` の TOCTOU 窓をコメントで明記。 **task 分割 (観点 7)**: T6 → T6a / T6b、T4 → T4a / T4b、T5 → T5a / T5b の 9 task に。依存グラフと worktree 並列可否 (T3 ∥ T6b) を更新。 **fixture の実測 probe**: `tmp/plan-indicator-wiring/probe_fixture.py` で設計書 §6 の生成式を実行 — in_sample の 1h Wilder RSI(14) は **min 27.2159 / max 78.4162、long 52 + short 50 = 102 opens、先頭 14 本 NaN で index 14 から値**。`opens >= 30` (`EVALUABLE_MIN_TRADES`) を満たすため**設計書 §6 の生成式の是正は不要**。 **自己レビューで新規追記コードも現物照合**: `ToolRegistry` はキーワード専用 `__init__` + `register_all` + `execute(name, args, allowed)` (`call` は存在しない) / `approvals.get()` は存在しない (SQL で status を読む) / journal のテーブル名は `plugin_switch_journal` / `reconcile_switch_journals` は既に `settings` を取る、へ是正。 **T4a Step 4-2 が `wiring_envs.switch_env` を使うため T4a の着手条件に T6b を追加**し依存グラフを訂正 | プラン着手前検証 `tmp/plan-indicator-wiring/opus-plan-r1.md` (opus r1) | - |
| 2026-09-14 | v1 | 起案。設計書 v1.1 を実装プランへ写す。T1 (loader 新キー + `plugin/resolve.py` + `approved_plugins` 二相 + 全 caller 移行) / T2 (`core/plugin_contract.py` + worker の同居実行 + `PluginSession(resolved=)` の containment 検査 + graceful close/`cpu_sec` + `check_source` の共有状態遮断 + standalone 系列 wire) / T6 (`rsi_indicator` 系列化 + 新規 `rsi_pullback` + `tests/fixtures/indicator_wiring.py` の決定論 fixture と独立参照実装 + 設計書契約文) / T3 (adapter の `resolved` 必須 + `decision_sink` + service/producer/trade worker/人間 CLI の配線 + `afx plugin lock --from _human`) / T4 (`GateOutcome.verdict_kind="indicator_unresolved"` + `_run_full_gate` の固定 ValueError と `outputs_required` + A1/A1-b E2E + ロック集合と approve 時再解決と bless TOCTOU + payload `indicator_deps` + noop の pin 除去比較と再ロック例外 + 質検査除外 + reconcile の revert + 承認詳細 2 欄) / T5 (`ImproveRunContext.inventory`/`inventory_view` + `list_deployed_plugins`/`lock_staging_deps` + `release_backtest` と `started:false` + commit gate の解決 + `backtest_cpu` activity + prompt inventory) の 6 task に分割。実行順序 T1→T2→T6→T3→T4→T5、T6 のみ T2 後に worktree 並列可 (T3 前にマージ必須)。Global Constraints に固定文言語彙一覧・上限 4 種・遮断 8・実 DB 不可触・`run_kind_gate` 非送出・スキーマ変更ゼロを明記。レビュー段 (段 0 の最優先 8 件を含む)・完了条件 (受入 33 ID)・進捗表・指揮者の既定選択 8 件を追加。着手前検証で 10 件是正 — `handshake_too_large` の到達不能を定数 monkeypatch 境界に置換 / A1'' の `.versions` 直書きを正規再配備 (`_redeploy_rsi_variant`) に置換 / Step 3-1c の呼び出し元に `improve_loop._run_strategy_gate` を追加し `_validate_kind(resolved=None)` を任意化 / A1 を `floor_mode="warn"` にし T6 に `opens >= EVALUABLE_MIN_TRADES` の先行 pin を追加 / P2' の並行ロック待ちテストを新規追加 / 非 str キー YAML ケースを削除し `indicator_ref_duplicate_alias` 到達不能を L1 に明記 / `lock_config(candidate_dir, pins)` に変更し `lock_staging_deps` の YAML 書き換え重複を解消 / `deploy_approved` の kind を config.yaml 由来に / Step 4-3c の「実測で決める」記述を 4-1c の具体指示へ移動 / 残存 `...` を実コードに展開 | 設計書 `2026-09-14-indicator-consumption-wiring-design.md` v1.1 (実装着手可、ユーザー裁定 U1〜U6 反映済み) を writing-plans 規約の実装プランへ写す | - |
| 2026-09-15 | v1.4a | codex r3 (terra、束 2 / 束 3 のみ再投入、`tmp/plan-indicator-wiring/r2/codex-{2,3}-r3.md`): **指摘 0 / 0**。着手前検証を収束と判定 (opus r1 → codex r1 sol → codex r2 terra 4 束 → r3)。**実装着手** (subagent 駆動、worktree `tmp/wt/iw` branch `indicator-wiring`、T1 から) | 収束 | - |
| 2026-09-15 | v1.4b | 実装進捗: T1 `f79fdcf` / T2 `4a7f3c9` / T6a `a34128e` / T3 `1bc9356` / T6b `5717cac` → merge `b4cb110` (2461 passed + fixtures 26)。T6b の `install_gate_double` Produces を本文に合わせ `pytest_ok` に訂正。`slow` marker を pyproject に登録。段 0 申し送り: T2 逆変異 (a) は mutator を要素単位に、T6a (a) `max_bars` は A1 が担保 | 実装記録 | b4cb110 |
| 2026-09-16 | v1.5 | **実装 9 task 完了** (`indicator-wiring` `a6d98d4`): T4a `b66094a` (A1 decision sink vs oracle 1508/1508) / T4b `97e2006` / T5a `fd1ef58` / T5b `7dba0cc`〜`cce4ad8` + 仕上げ `49df622` `a6d98d4` (P4 identity pin 追加)。フルスイート `10 failed, 4118 passed` (失敗は既知 flake 10 件、個別緑)、`-m slow` 8 passed。束全体の完了条件充足。段 0 申し送り: T2 (a) は要素単位 mutator / T4b (d) はブロック無効化型 / T6a (a) は A1 が担保 / T5b 変異 (e) の観測点は `test_backtest_cpu_is_written_with_null_when_the_session_died` / T5a `c26f92b` が `tests/runners` を壊した件 (WIP `aa063a3` で是正) / `approved_plugins(` 56 箇所の目視。impl-report は `tmp/review-20260915-iw/impl-report-T*.md` (9 本) | 実装記録 | a6d98d4 |
| 2026-09-18 | v1.7 | **1 周目ローカル LLM 3 本のトリアージ完了** (muse-glimmer-30b-low / ornith-1.5-35b-nothink / qwen3.8-27b-Q5-nothink、17 チャンク × 3 = **51 走**、指摘 **142 件**)。判定内訳 Y 15 / N 82 / dup 19 / 等価 19 / ゼロ 4 / 裁定 3。重複を除いた**別々の穴 12 件を pin** (すべて変異で SURVIVED を実測 → pin 追加 → 逆変異 red → 復元 green)。**Y 由来の実装欠陥は 0** — 12 件すべてテスト側の穴だった。**裁定由来の是正 2**: `lock_staging_deps` (`c5177ee`) と `afx plugin lock` (`e9d496f`) の lock 後整合 (`lock_config` の `new_hash` vs `discover` 再取得値) を `assert` から明示チェックへ — `assert` は `python -O` で消えるため防御に数えられない (両者は同一の不変条件)。失敗時は各々の既存作法 (error 辞書 + `config.yaml` 書き戻し / stderr + `return 1`、`_human` は人間所有領域なので自動書き戻しはしない) に写像。**テスト改名 1**: `test_indicator_deps_is_absent_for_indicator_kind` → `..._is_empty_object_for_indicator_kind` (assert は `== {}` で名前と矛盾していた。契約「3 経路とも常にキーを載せ、依存が無ければ空オブジェクト」の側が正)、本プラン L8260 のテスト転写も追随。**モデル別**: Y は qwen 8 / ornith 4 / muse 3 だが qwen は 3258 秒と桁違いに重く、1 本 (c04) は反復退行 (`finish=stop` かつ `content_len>0` なので既存 2 指標では捕まらない)。**3 本合計 13 件の Critical はすべて N**、確定 12 件は Important 6 / Minor 6 — 重大度でソートしないこと。偽陽性の最大クラスは「テストが無い」型で、実際は**別ファイルにあった** (材料のチャンク分割の副作用) | 1 周目ローカル LLM レビューのトリアージ `tmp/review-20260917-iw-r1/triage-local.md` (材料と原本は同ディレクトリ `local/material/` と `triaged-originals/`) | `ff4c8cc`〜`e9d496f` (15 commit) |

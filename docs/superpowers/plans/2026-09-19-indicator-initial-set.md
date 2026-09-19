# [indicator-initial-set] 実装プラン v1.3 (設計書 = `docs/superpowers/specs/2026-09-19-indicator-initial-set-design.md` v1.3c 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (推奨) または superpowers:executing-plans で task ごとに実行すること。Step は
> チェックボックス (`- [ ]`) で追跡する。

**Goal:** 改善ループが戦略を作るとき `config.yaml` の `indicators:` で宣言できる
**標準指標の初期セット 9 本** (`sma` / `ema` / `rsi` / `macd` / `bollinger` / `atr` /
`adx` / `stochastic` / `ichimoku`) を `docs/examples/plugins/<名前>/` に用意し、人間が
1 本ずつ `afx plugin bless --from _human` で配備するための runbook を書く。
**配備用の新コマンド・新機構・LLM の自動配備経路は一切作らない。**

**Architecture:** 既存の plugin 契約の上に「中身」だけを載せる。各 plugin は
`plugin.py` / `config.yaml` / `test_plugin.py` の 3 ファイルで**完結**し、互いに
一切依存しない (共有モジュールを置く経路が無い — `loader._reject_unexpected_py_files`
が 4 本目の `.py` を拒否し、`sandbox.check_source` は相対 import を拒否する)。
したがって **T1〜T9 は完全に独立で並列実行できる**。正しさは各 plugin の
`test_plugin.py` (bless の pytest ゲートが毎回回す) と、repo 側の受入テスト
(`tests/`) の 2 層で担保する。

**Tech Stack:** Python 3.13 / uv / pytest / pandas / numpy / PyYAML。
**`src/` の変更はゼロ。DB スキーマ変更なし。`config/settings.yaml*` の変更なし。**

**Spec:** `docs/superpowers/specs/2026-09-19-indicator-initial-set-design.md` (v1.3a)。
**設計を変えない** — ユーザー裁定 R1〜R8 / R5a、指揮者裁定 D1〜D4、codex 設計レビュー
r1 (C0/I4/M2 全件採用) / r2 (C0/I3/M0 全件採用) / r3 (**指摘 0、収束**) は設計書
§0 / §8.1 / §8.2 / §8.3 に確定記録済みなので「裁定待ち」は無い。設計書に無い判断が
必要になったら**実装を止めて指揮者へ申告**すること。

## Global Constraints

- **実 DB (`data/agentic.db`) を読み書きしないこと**。repo 側テストは必ず `tmp_path` 上の
  sqlite を使う。`tests/conftest.py` の session ガードを無効化・迂回しない
  ([[tests-touching-real-repo-resources]])
- **実 `plugins/` ディレクトリを読み書きしないこと**。T10 の bless 実測は `tmp_path` 配下に
  作った plugins dir に対して行う (`tests/fixtures/wiring_envs.py` の流儀)
- **`src/` を 1 行も変更しない。** 本束が触るのは `docs/examples/plugins/` (新規 27 ファイル)、
  `docs/operations/` (新規 1 ファイル)、`tests/` (既存 1 ファイルへの追記 + 新規テスト) のみ
  (設計書 §7)。`src/` の修正が必要だと判断したら**実装を止めて指揮者へ申告**する
  (既知の 1 件 = `afx plugin bless` の `UnresolvedJournalError` 未捕捉は
  `[cli-bless-unresolved-journal]` として起票済み・本束では直さない)
- **plugin は 1 フォルダ 3 ファイルで完結する。** 4 本目の `.py` を置かない。
  `_int_param` / `_float_param` / `_true_range` は**使う plugin がそれぞれ逐語で持つ**
  (共有 import は構造的に不可能)。直すときは該当する全 plugin をまとめて直す
- **`check_source` で落ちる 3 形を使わない** (実測済み): `df.open` のような属性アクセス
  (`open` は denylist)、`to_frame` を含む `to_` 接頭辞のメソッド (許可は
  `to_dict`/`to_list`/`to_numpy`/`to_pydatetime` の 4 つのみ)、`getattr`。
  加えて `global` 文と**外部オブジェクトの属性への代入**も拒否される。
  **OHLCV 列は必ず `df["open"]` のように添字で取る**
- **params は「変換」ではなく「型の確認」** (設計書 §4)。`int(params.get(...))` を書かない。
  期間系は「`bool` でない `int`」かつ `>= 1`、`num_std` は「`bool` でない `int|float`」かつ
  有限かつ `> 0`。`macd` のみ `fast < slow` を要求、`ichimoku` の期間の順序は要求しない
- **例外文言の形 (逐語、9 本で統一)**: `params.<キー名> must be <期待>, got <値>` /
  `params.fast must be < params.slow, got fast=<n> slow=<n>`。**plugin 名・候補名を
  入れない**。`test_plugin.py` は `pytest.raises(ValueError, match=r"^params\.")` で
  **文言まで pin する** — `match` を外すと、検査を削る変異を入れても pandas 自身の
  `ValueError` でテストが緑のまま通る (変異スイープの実測)
- **`max_bars` は全 9 本で 400** (設計書 §3.2、`settings.plugin.max_bars_limit` 既定 1000 内)
- **docstring の必須項目** (設計書 §4): (1) warmup は自分の責務・足りない期間は NaN
  (2) 1d 足の epoch 錨 (3) `max_bars: 400` の意味と「依存する strategy は `max_bars` を
  400 以上に」 (4) 純関数であること (5) `rsi`/`adx` は値動きなしで中立値・`ichimoku` は
  lookahead 規約
- **遮断 8 に関わる新しい sink を作らない。** 9 本の docstring・test・config に成績
  (pf / avg_r / 勝率)・期間・段名 (`in_sample` / `holdout`)・pair・baseline 差分を
  **書かない** — `read_example_plugin` 経由で改善 agent に届く。指標の定義式・参照実装は
  秘密ではないので載せてよい
- **`docs/examples/plugins/{rsi_indicator,rsi_pullback,sma_cross}` を変更しない** (設計書 R7)
- **一時ファイルは `tmp/` か scratchpad**。`rm` を使ってよいのは自分が同セッションで作った
  一時領域だけ ([[rm-allowed-directories]])

## プラン規約

- **設計を変えない。** 設計書 v1.3a が正。設計書に無い判断が必要になったら**実装を止めて
  指揮者へ申告**する。設計レビューは 3 周で収束済み (r3 は指摘 0) なので、同じ論点の
  蒸し返しには「設計書 §X で決着済み」と返して閉じる
- **プラン記述自体が欠陥源になり得る** ([[plan-code-defects-not-implementer-defects]])。
  本プランのコードは**指揮者が scratchpad で実際に動かして green を確認してから**
  載せたもの (「着手前検証の記録」節に実測を記載) だが、それでも**プロジェクトの制約に
  反する記述を見つけたら、プランどおりの実装でも欠陥として申告**すること。黙って直さない
- **逸脱は必ず申告する** ([[haiku-silently-adapts-report-deviations]])。プランの Step
  どおりに書けなかった箇所は、実装報告に「Step 番号 / 何を / なぜ」を明示すること。
  **「食い違いなし」という報告は額面どおりに受け取られない** — 指揮者が同じコマンドを
  独立に再実行して照合する。報告に貼った出力は作業ログであって証拠ではない
- **逐語転写は機械 diff する** ([[transcription-must-be-machine-diffed]])。
  - **markdown のフェンス行 (` ```python ` / ` ``` `) をファイルに書かないこと。**
  - 書いた直後に `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" <file>` で
    自己検証すること (`.py` の 2 本とも)。
  - **各 task の最後に、プラン本文からコードブロックを機械抽出して `diff` を取り、
    その出力 (差分ゼロ) を報告に貼ること。** 抽出コマンドは各 task の Step に逐語で書いてある
  - 報告には**書いたファイルの絶対パスと `wc -l` の行数**を書くこと
- **変異テストの規律** ([[mutation-testing]])。
  - 各 task の逆変異リストは **4〜6 件 = 下限であって上限ではない**。思いついた変異は足してよい
  - **「落ちるはず」で済ませない。** 1 件ずつ実際に適用して pytest を回し、`FAILED` の
    テスト名を報告に貼る。本プランの表に「red になるべきテスト」を書いてあるので照合する
  - 適用 → 実測 → **必ず元に戻す** (`git checkout` は使わない。`cp` で退避して戻す)
  - **等価変異は正直に等価だと書く。** 本プランには 1 件記録がある (T3 の注記)
- **実 DB / 実 `plugins/` を触らない**。`git status` と `data/` の mtime で検収する
- **fresh worktree でフルスイート**を最後に回す (残骸ゼロ)

## File Structure

```
docs/examples/plugins/
  sma/         plugin.py  config.yaml  test_plugin.py     <- T1 (新規)
  ema/         plugin.py  config.yaml  test_plugin.py     <- T2 (新規)
  rsi/         plugin.py  config.yaml  test_plugin.py     <- T3 (新規)
  macd/        plugin.py  config.yaml  test_plugin.py     <- T4 (新規)
  bollinger/   plugin.py  config.yaml  test_plugin.py     <- T5 (新規)
  atr/         plugin.py  config.yaml  test_plugin.py     <- T6 (新規)
  adx/         plugin.py  config.yaml  test_plugin.py     <- T7 (新規)
  stochastic/  plugin.py  config.yaml  test_plugin.py     <- T8 (新規)
  ichimoku/    plugin.py  config.yaml  test_plugin.py     <- T9 (新規)
  rsi_indicator/ rsi_pullback/ sma_cross/                 <- 既存、触らない (R7)

docs/operations/
  indicator-initial-set-deploy-2026-09-19.md              <- T10 (新規、runbook)

tests/
  plugin/test_loader.py                                   <- T10 (既存、1 assert に 9 名を追記)
  plugin/test_indicator_initial_set.py                    <- T10 (新規、I2/I5/I6/I7/I8)
```

**触らないもの**: `src/` 配下すべて、`src/agentic_fx/loops/prompts/improve_mission.md`
(規律 7 の変更は不要 — 設計書 §7 の根拠)、`config/settings.yaml*`、`tests/` の既存
ファイル (`test_loader.py` の 1 assert への追記を除く)。

## 受入条件 (設計書 §6) と task の対応

| ID | 内容 | 担保する task / テスト |
|---|---|---|
| **I1** | 9 本すべてが `discover` を通る | **T10** — `tests/plugin/test_loader.py::test_discover_sample_plugins_directory_not_rejected` の包含集合に 9 名を追加 (**総数は pin しない**) |
| **I2** | 戻り値が indicator 契約を通る (キー集合完全一致 / index 一致 / Inf 不在) | **T1〜T9** の `test_outputs_match_declared_keys_and_index` (自己テスト) + **T10** の `test_all_nine_pass_the_indicator_validator` (`core.plugin_contract.validate_indicator_result` を直接通す) |
| **I3** | 独立参照実装との全行一致 (`rel/abs 1e-9`) | **T1〜T9** の `test_matches_reference_implementation_on_every_row` |
| **I4** | 固定 fixture における**全 160 行**の接頭辞一致 | **T1〜T9** の `test_prefix_consistency_on_every_row` |
| **I5** | 先頭依存の誤差が仕様化データ範囲で許容内 | **T10** の `test_head_dependence_within_tolerance` (4 値域 × 8 seed + スパイク + 退化 fixture) |
| **I6** | 9 本を順に bless できる | **T10** の `test_nine_indicators_bless_in_sequence` (tmp の plugins dir + tmp DB) |
| **I7** | 新 `rsi` を宣言した strategy が E2E で動く | **T10** の `test_strategy_declaring_new_rsi_runs_end_to_end` (+ `max_bars: 200` の保証外ケース) |
| **I8** | runbook が逐語再現できる ((a) 正常 (b) ゲート前失敗からの再開 (c) ゲート後失敗の収束) | **T10** の `test_runbook_*` 3 本 |
| **I9** | 入力不変・反復決定性 | **T1〜T9** の `test_input_frame_is_not_mutated` / `test_repeated_calls_are_deterministic` |

**抜けが無いことの確認**: I1〜I9 の 9 件すべてに担保する task が付いている。I2/I3/I4/I9 は
9 本 × 自己テストで 9 重に、I1/I5/I6/I7/I8 は T10 で 1 回ずつ観測する。

## task 依存図

```
          T0 (テンプレート定義 — コードは書かない、プラン内の規約)
           |
   +---+---+---+---+---+---+---+---+
   |   |   |   |   |   |   |   |   |
  T1  T2  T3  T4  T5  T6  T7  T8  T9     <- 9 本は完全に独立 / 並列実行可
  sma ema rsi macd boll atr adx sto ichi     (worktree 分離、依存は T0 のみ)
   |   |   |   |   |   |   |   |   |
   +---+---+---+---+---+---+---+---+
           |
          T10 (repo 側受入テスト I1/I2/I5/I6/I7/I8 + runbook)
```

- **T0 は文書 task** (本プランの T0 節を読むだけ。ファイルは作らない)。
- **T1〜T9 は互いに一切依存しない。** 別 worktree で同時に走らせてよい。
  **`subagent は指揮者の cwd を継承する`** ([[subagent-inherits-session-cwd]]) ので、
  並列 dispatch の前に cwd を repo root へ戻し、各 task には `git -C <worktree>` の形で
  コマンドを書いて渡すこと。
- **T10 は T1〜T9 の全マージ後**に 1 レーンで実行する。

---

## T0: 共通の土台 (テンプレート定義 — ファイルは作らない)

**この task は文書 task。** T1〜T9 の実装者は着手前にこの節を読むこと。ここで 1 回だけ
定義する規約を、各 task の逐語コードが実体化している。

### T0-1: `plugin.py` の形 (全 9 本共通)

1. **モジュール docstring** — 設計書 §4 の必須 5 項目 (+ `rsi`/`adx`/`ichimoku` は 6 項目め)。
2. `from __future__ import annotations` → 標準ライブラリ (`math`) → 空行 → `numpy` / `pandas`。
   **`math` は `bollinger` (`isfinite`/`sqrt`) だけが import する**。`numpy` は
   `atr` / `adx` (`np.maximum`) だけ。
3. (`rsi` / `adx` のみ) モジュール定数 `EPS = 1e-9`。
4. ヘルパ (`_int_param` / `_float_param` / `_true_range` / `_midpoint`) — **使う plugin だけが
   持つ**。共有 import は構造的に不可能 (Global Constraints)。
5. `def compute(df: pd.DataFrame, params: dict) -> dict:` — **モジュールトップレベル、
   位置引数名は `df, params` で完全一致、デコレータなし、必須 kwonly なし**
   (`loader._has_matching_function` がこの 4 点を AST で検証する)。

### T0-2: `config.yaml` の形 (全 9 本共通)

```yaml
kind: indicator
outputs: [<宣言キーをカンマ区切り>]
max_bars: 400
params:
  <キー>: <既定値>
```

**`timeframe` / `pairs` / `exit_mode` / `indicators` を書かない。**
`exit_mode` は kind=strategy 専用なので書くと `loader` に reject される。

### T0-3: `test_plugin.py` の骨格 (全 9 本共通、差分は参照実装と追加テストだけ)

**固定 fixture `_mkdf`** (全 9 本で逐語同一):

- 160 行 (= `ichimoku` の `senkou_b` warmup 51 + 余裕)、`freq="1h"`、UTC。
- `close = base + cumsum(N(0, base*0.002))`、`np.random.default_rng(seed)`。
- **極値を中間 (`n//2`) と末尾側 (`n-7`) の両方**に置く — 片側だけだと「系列全体の
  min/max で正規化する」型の未来参照を fixture 次第で見逃す (設計書 §6.2)。
- **bar 1 に決定論的な上げ** (`high[1] = high[0] + base*0.01`) — ここが平坦だと
  「`±DM[0]` を NaN でなく 0.0 にする」変異が fixture 次第で生き残る (変異スイープの実測)。
- **`open` は前バーの終値** (`close` と別系列) — `open == close` の fixture だと
  「`close` の代わりに `open` を読む」型の変異を検出できない (変異スイープの実測)。

**参照実装** (`_ref_*`、素朴なループ。**`plugin.py` から import・コピーしない**):

- `_ref_sma(values, period)` — 毎回スライスして `sum(window)/period`。
- `_ref_recursive(values, alpha, period)` — **`min_periods` は位置ではなく「非 NaN 観測数」で
  数える**。seed は最初の非 NaN 値 (`y_0 = x_0`)。位置で `i < period - 1` と書くと `atr` の
  warmup が 14 でなく 13 になり I3 が落ちる (設計書 §3.3)。
- `_ref_rolling(values, period, pick)` / `_ref_true_range(df)` / `_ref_std(values, period)`。
- **`rsi` / `adx` の参照実装は ε 規則も書き写す** — 同じ閾値 (`EPS = 1e-9`)、同じ基準
  (**その行自身の `|close|`**)、同じ判定順。参照実装は「別の書き方」であって
  「別の仕様」ではない (設計書 §3.3)。

**共通テスト 8 本** (全 9 本が持つ):

| テスト名 | 担保 |
|---|---|
| `test_outputs_match_declared_keys_and_index` | I2 |
| `test_matches_reference_implementation_on_every_row` | I3 |
| `test_prefix_consistency_on_every_row` | I4 (**全 160 行**。サンプル点だと「行 37 だけ未来を読む」型を素通りする — 実測) |
| `test_input_frame_is_not_mutated` | I9 (a) |
| `test_repeated_calls_are_deterministic` | I9 (b)。fresh な df で 2 回 + **同じ df で 2 回** |
| `test_warmup_boundary_is_pinned_on_both_sides` | warmup。**両側を pin する** |
| `test_short_frame_returns_all_declared_keys_as_all_nan` | 宣言キーを必ず全部返す |
| `test_invalid_params_raise_value_error` | params 型検証。**`match=r"^params\."` で文言まで pin** |
| `test_valid_param_override_changes_the_result` | 上書きが効いている |

`min_test_functions: 3` は改善ループ側のゲートで人間の bless には掛からないが、
9 本とも 12 本以上のテストを持つ。

### T0-4: params 検証の負例表 (全 9 本共通の形)

| 負例 | 期待 |
|---|---|
| `{"<期間>": 20.0}` (float) | `ValueError` (`^params\.`) — `int()` 変換しないので通らない |
| `{"<期間>": "20"}` (str) | 同上 |
| `{"<期間>": True}` (bool) | 同上 — `bool` は `int` の派生なので**先に**弾く |
| `{"<期間>": 0}` / `{"<期間>": -1}` | 同上 (`>= 1`) |
| `{"num_std": 0.0}` / `{"num_std": -2.0}` / `{"num_std": float("inf")}` | 同上 (`bollinger` のみ) |
| `{"fast": 26, "slow": 26}` / `{"fast": 30, "slow": 26}` | 同上 (`macd` のみ、`fast < slow`) |

### T0-5: red の作り方 (全 task 共通の手順)

**「テストが無い状態」でなく「stub がある状態」で red を作る。** `plugin.py` を置かずに
pytest を回すと `ImportError` (collection error) になり、**どの assert が守られているのかを
観測できない**。手順:

1. `config.yaml` と `test_plugin.py` を先に転写する。
2. `plugin.py` を**全出力キーを全 NaN で返す stub** にする (各 task の Step b に逐語がある)。
3. `pytest -q test_plugin.py` を回し、**assert-red** (collection は成功し、複数の
   `FAILED` が出る) ことを確認する。**この pytest 出力を逐語で報告に貼る。**
4. `plugin.py` を本実装へ差し替えて green にする。

---

## T1: `sma` — SMA (単純移動平均)

**出力**: `value` ／ **warmup (最初に値が入る 0 起点行)**: `value`=19 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/sma/` の 3 ファイルのみ。

**この task の要点**: 最も単純。ここで T0 の骨格を体得してから他へ進むとよい。

### Step 1-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/sma/` を作る
- [ ] `docs/examples/plugins/sma/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [value]
max_bars: 400
params:
  period: 20
```

- [ ] `docs/examples/plugins/sma/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""sma indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('value',)
WARMUP = {'value': 19}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_sma(values: list, period: int) -> list:
    """SMA を素朴なスライスで。先頭 period-1 個は NaN。"""
    out = []
    for i in range(len(values)):
        window = values[i - period + 1:i + 1]
        if i < period - 1 or any(_isnan(v) for v in window):
            out.append(float("nan"))
        else:
            out.append(sum(window) / float(period))
    return out


def _reference(df, params: dict) -> dict:
    period = int(params.get("period", 20))
    return {"value": _ref_sma(df["close"].tolist(), period)}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 20.0}, {'period': '20'}, {'period': True}, {'period': 0}, {'period': -1}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 20}, {'period': 20, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 20}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/sma/test_plugin.py`

### Step 1-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/sma/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "value": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/sma && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 1-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/sma/plugin.py` を**逐語**で差し替える:

```python
"""SMA (単純移動平均) indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**有限の窓しか見ない** (rolling) ので
   先頭依存そのものは無い — 同じ最終行を出すのに 400 本は要らない。
   400 に揃えてあるのは、9 本で値を 1 つにするため (設計書 §3.2 / 裁定
   D4): 指標ごとに散らすと、依存する strategy が小さく宣言したときに
   「一部の指標だけ静かにずれる」という気づきにくい部分的劣化になる。
   ただし窓 (下の `params`) より短い履歴では値は NaN のままである。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり、窓を満たせない期間は NaN のままになる
   (rolling の逐次更新に由来する丸めの差もわずかに残る)。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 20}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """単純移動平均 (SMA) を系列で返す。warmup (先頭 period-1 行) は NaN。"""
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    close = df["close"].astype(float)
    value = close.rolling(window=period, min_periods=period).mean()
    return {"value": value}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/sma/plugin.py`

### Step 1-d: **green** を確認する

- [ ] `cd docs/examples/plugins/sma && uv run pytest -q test_plugin.py`
      → **13 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/sma')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/sma'), 'sma')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 1-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-sma-1 | rolling -> ewm (平滑の種類を入れ替え) | `test_matches_reference_implementation_on_every_row` |
| M-sma-2 | min_periods を外す | `test_matches_reference_implementation_on_every_row`, `test_short_frame_returns_all_declared_keys_as_all_nan`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-sma-3 | period の既定を 20 -> 14 | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-sma-4 | close -> open | `test_matches_reference_implementation_on_every_row` |
| M-sma-5 | params の bool 除外を外す | `test_invalid_params_raise_value_error[params2]` |
| M-sma-6 | 未来参照 shift(-1) | `test_matches_reference_implementation_on_every_row`, `test_prefix_consistency_on_every_row`, `test_valid_param_override_changes_the_result` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 1-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T1: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T1: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/sma") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/sma && git commit`
      (メッセージ: `feat(indicator-initial-set): sma indicator plugin (T1)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T2: `ema` — EMA (指数移動平均)

**出力**: `value` ／ **warmup (最初に値が入る 0 起点行)**: `value`=19 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/ema/` の 3 ファイルのみ。

**この task の要点**: `span=period, adjust=False` (= alpha 2/(p+1))。Wilder (1/p) と混同しない。

### Step 2-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/ema/` を作る
- [ ] `docs/examples/plugins/ema/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [value]
max_bars: 400
params:
  period: 20
```

- [ ] `docs/examples/plugins/ema/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""ema indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('value',)
WARMUP = {'value': 19}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_recursive(values: list, alpha: float, period: int) -> list:
    """再帰平滑。**`min_periods` は位置ではなく「非 NaN 観測数」で数える**
    (設計書 §3.3) — 位置で `i < period - 1` と書くと `atr` / `adx` の warmup
    が 1 本早く明け、I3 が落ちる。seed は最初の非 NaN 値 (`y_0 = x_0`)。
    """
    out = []
    prev = None
    seen = 0
    for value in values:
        if _isnan(value):
            out.append(float("nan"))
            continue
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        seen += 1
        out.append(prev if seen >= period else float("nan"))
    return out


def _reference(df, params: dict) -> dict:
    period = int(params.get("period", 20))
    alpha = 2.0 / (period + 1.0)
    return {"value": _ref_recursive(df["close"].tolist(), alpha, period)}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 20.0}, {'period': '20'}, {'period': True}, {'period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 20}, {'period': 20, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 20}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/ema/test_plugin.py`

### Step 2-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/ema/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "value": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/ema && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 2-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/ema/plugin.py` を**逐語**で差し替える:

```python
"""EMA (指数移動平均) indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**再帰平滑** (前の行の値を使う) を
   含むので、履歴の先頭を切ると初期値 (seed) が変わり、その残差が最終行
   まで残る。再帰が線形なので残差は `|Δseed| × (1−α)^(本数−warmup)` で
   減衰し、400 本あればこの係数が 1e-13 の桁まで落ちる (設計書 §3.2 の
   上界式)。残差の大きさは入力の値域に比例するので、**「400 本なら必ず
   1e-6 以内」という普遍的な保証ではない**。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 20}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """指数移動平均 (EMA、alpha = 2/(period+1)) を系列で返す。

    再帰の初期値は `y_0 = x_0` (pandas `adjust=False` の既定、設計書 D1)。
    教科書流の「先頭 period 本の SMA を seed にする」は採らない — 配備済
    `rsi_indicator` と同じ再帰に揃えるため。
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    close = df["close"].astype(float)
    value = close.ewm(span=period, adjust=False, min_periods=period).mean()
    return {"value": value}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/ema/plugin.py`

### Step 2-d: **green** を確認する

- [ ] `cd docs/examples/plugins/ema && uv run pytest -q test_plugin.py`
      → **12 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/ema')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/ema'), 'ema')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 2-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-ema-1 | span -> alpha=1/p (EMA を Wilder に) | `test_matches_reference_implementation_on_every_row` |
| M-ema-2 | adjust=False -> True | `test_matches_reference_implementation_on_every_row` |
| M-ema-3 | min_periods を外す | `test_matches_reference_implementation_on_every_row`, `test_short_frame_returns_all_declared_keys_as_all_nan`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-ema-4 | period の下限検査を外す | `test_invalid_params_raise_value_error[params3]` |
| M-ema-5 | int 型検査を int() 変換に | `test_invalid_params_raise_value_error[params0]`, `test_invalid_params_raise_value_error[params1]`, `test_invalid_params_raise_value_error[params2]` |
| M-ema-6 | 未来参照 rolling(center=True) | `test_matches_reference_implementation_on_every_row`, `test_prefix_consistency_on_every_row`, `test_valid_param_override_changes_the_result` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 2-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T2: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T2: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/ema") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/ema && git commit`
      (メッセージ: `feat(indicator-initial-set): ema indicator plugin (T2)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T3: `rsi` — RSI (Wilder 平滑)

**出力**: `rsi` ／ **warmup (最初に値が入る 0 起点行)**: `rsi`=14 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/rsi/` の 3 ファイルのみ。

**この task の要点**: 退化規則 (ε) の**判定順**と、参照実装にも同じ ε 規則を書き写すこと。`|close|` は**その行自身の close** (`shift` も平均も使わない)。配備済 `rsi_indicator` とは横ばい相場で値が違う (設計書 §0 R5a)。

### Step 3-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/rsi/` を作る
- [ ] `docs/examples/plugins/rsi/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [rsi]
max_bars: 400
params:
  period: 14
```

- [ ] `docs/examples/plugins/rsi/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""rsi indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('rsi',)
WARMUP = {'rsi': 14}
EPS = 1e-9


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_recursive(values: list, alpha: float, period: int) -> list:
    """再帰平滑。**`min_periods` は位置ではなく「非 NaN 観測数」で数える**
    (設計書 §3.3) — 位置で `i < period - 1` と書くと `atr` / `adx` の warmup
    が 1 本早く明け、I3 が落ちる。seed は最初の非 NaN 値 (`y_0 = x_0`)。
    """
    out = []
    prev = None
    seen = 0
    for value in values:
        if _isnan(value):
            out.append(float("nan"))
            continue
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        seen += 1
        out.append(prev if seen >= period else float("nan"))
    return out


def _reference(df, params: dict) -> dict:
    """plugin.py とは別の書き方だが、**ε 規則は同じ閾値・同じ基準
    (その行の |close|)・同じ判定順 (1)->(2)->(3) で書き写す** (設計書 §3.3)。
    ここを素朴な式のままにすると閾値付近の行で I3 が落ちる。
    """
    period = int(params.get("period", 14))
    close = df["close"].tolist()
    gain = [float("nan")]
    loss = [float("nan")]
    for i in range(1, len(close)):
        delta = close[i] - close[i - 1]
        gain.append(delta if delta > 0.0 else 0.0)
        loss.append(-delta if delta < 0.0 else 0.0)
    alpha = 1.0 / period
    avg_gain = _ref_recursive(gain, alpha, period)
    avg_loss = _ref_recursive(loss, alpha, period)
    out = []
    for i in range(len(close)):
        if _isnan(avg_gain[i]) or _isnan(avg_loss[i]):
            out.append(float("nan"))
            continue
        threshold = EPS * abs(close[i])
        if avg_gain[i] + avg_loss[i] <= threshold:
            out.append(50.0)
        elif avg_loss[i] <= threshold:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + avg_gain[i] / avg_loss[i]))
    return {"rsi": out}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 14.0}, {'period': '14'}, {'period': True}, {'period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 14}, {'period': 14, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 14}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


def _decayed_flat_df(n_flat: int = 260, price: float = 150.0,
                     move: float = 1.0) -> pd.DataFrame:
    """1 本だけ上げたあと `n_flat` 本の完全横ばい。

    平滑量は指数的に減衰して**実質ゼロ**になるが、**厳密なゼロにはならない**
    — 厳密 `== 0` 判定では捕まらず、比を取る指標が「値動きが無いのに強い
    トレンド」という値を返す構成 (設計書 §3.2 (i-b) の反例)。相対 ε 規則が
    効いているかはこの fixture でしか観測できない (変異スイープの実測:
    完全横ばいだけの fixture では EPS=0 への変異が生き残る)。
    """
    closes = [price] * 5 + [price + move] * (n_flat + 1)
    values = np.array(closes, dtype="float64")
    index = pd.date_range("2026-01-01", periods=len(values), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(len(values))}, index=index)


def test_eps_rule_fires_after_the_averages_decay():
    """`avg_loss` が厳密 0 でも `avg_gain` が実質ゼロまで減衰した区間では
    **50 (中立)** を返す。EPS を 0 にすると (1) の分岐に到達せず 100 を返す
    ので、この fixture が ε 規則の唯一の観測点になる。"""
    rsi = compute(_decayed_flat_df(), {})["rsi"]
    assert float(rsi.iloc[-1]) == 50.0


# --- 退化規則 (ε) と既存 `rsi_indicator` との差異 ---------------------------

def _flat_df(n: int = 60, price: float = 150.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    values = np.full(n, price)
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(n)}, index=index)


def test_flat_market_is_neutral_not_one_hundred():
    """完全横ばい = 値動きなし -> **50** (中立)。配備済 `rsi_indicator` は
    同じ場面で 100 を返す (厳密 `== 0` 判定で「下げが無い」に倒すため) —
    両者を同じ strategy で混ぜて使わないこと (設計書 §0 R5a)。"""
    rsi = compute(_flat_df(), {})["rsi"]
    assert float(rsi.iloc[-1]) == 50.0


def test_monotonic_rise_is_one_hundred():
    """下げが一度も無く上げが有意 -> 100 (判定順 (2))。"""
    closes = [100.0 + i for i in range(40)]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=index)
    assert float(compute(df, {})["rsi"].iloc[-1]) == 100.0


def _epsilon_basis_df(wiggle: float = 1e-5, base: float = 150.0,
                      n_wiggle: int = 24, mult: float = 1000.0):
    """ε の基準が「その行自身の close」か「前の行の close」かを判別する fixture。

    構成: `base` の周りを `±wiggle` で `n_wiggle` 本だけ上下させ (Wilder 平滑の
    `avg_loss` を約 `4.0e-06` に落とす)、その次の 1 本で close を `mult` 倍する。
    その行 (`JUMP_ROW`) では

      EPS * |close[t-1]| = 1.5e-07  <  avg_loss = 4.0e-06  <  EPS * |close[t]| = 1.5e-04

    となり、**閾値の基準をどちらに取るかで判定順 (2) に入るかどうかが変わる**
    唯一の行になる (下側 26.7 倍・上側 37.5 倍の余裕)。

    **この構成でしか差が出ない**: `t` 行で基準が有意に変わるには close 自身が
    大きく動く必要があり、そのとき `avg_gain >= Δclose / period` なので比が
    極端になり、両者の差は高々 `100 * period * EPS` (= 1.4e-06) しかない。
    値の差で見ると `_same` の許容 (1e-07) 付近で脆いので、**判定順 (2) が返す
    リテラル `100.0` との厳密一致**で見る (基準を前行にすると
    `99.99999996261333` になり `== 100.0` が落ちる)。
    """
    closes = [base]
    for i in range(n_wiggle):
        closes.append(closes[-1] + (wiggle if i % 2 == 0 else -wiggle))
    closes.append(closes[-1] * mult)
    closes.append(closes[-1])
    values = np.array(closes, dtype="float64")
    index = pd.date_range("2026-01-01", periods=len(values), freq="1h",
                          tz="UTC")
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(len(values))}, index=index)


#: `_epsilon_basis_df` で基準が効く唯一の行 (`n_wiggle` 本の上下の次)。
JUMP_ROW = 25


def test_epsilon_threshold_uses_this_rows_close_not_the_previous_one():
    """ε の基準は **その行自身の `|close|`** (設計書 §3.2 (i-b) / docstring)。

    `EPS * close.abs()` を `EPS * close.shift(1).abs()` に変えると、この行の
    `avg_loss` (4.0e-06) が閾値 1.5e-07 を上回って判定順 (2) に入らなくなり、
    式どおりの `99.99999996261333` が返る。段 0 の変異スイープで、既存の
    fixture (ランダムウォーク / 完全横ばい / 減衰横ばい) では**全行が
    ビット一致**して生き残ることを実測したため、この観測点を足した。
    """
    rsi = compute(_epsilon_basis_df(), {})["rsi"]
    assert float(rsi.iloc[JUMP_ROW]) == 100.0


def test_eps_rule_does_not_fire_on_ordinary_data():
    """通常データで ε 規則が誤発火しないこと (設計書 §3.2 (i-b) の余裕 7e+03 倍)。"""
    rsi = compute(_mkdf(), {})["rsi"]
    assert 0.0 < float(rsi.iloc[-1]) < 100.0
    assert float(rsi.iloc[-1]) != 50.0
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/rsi/test_plugin.py`

### Step 3-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/rsi/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "rsi": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/rsi && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 3-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/rsi/plugin.py` を**逐語**で差し替える:

```python
"""RSI (Relative Strength Index、Wilder 平滑) indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は平滑量どうしの**比**を取り、その比が
   非線形な除算を通って次の段へ入る。したがって線形再帰の上界式は
   **使えず、400 本で一致するという普遍的な保証は書けない**
   (設計書 §3.2 (i-b))。保証の形は「設計書 §6 I5 が生成式と seed まで
   仕様化した fixture 集合での受入公差」であって、任意の入力に対する
   上界ではない。値動きの無い期間で比が退化する件は、下の
   「値動きの無い期間は中立値を返す」の項 (ε 規則) で扱う。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

5. **値動きの無い期間は中立値を返す** (設計書 §3.2 (i-b))。判定は
   `<= EPS * |close|` の相対 ε (EPS = 1e-9)。`|close|` は**その行自身の
   close**。strategy 側で「中立」と「板が動いていない」を読み分けたいなら
   `atr` を併せて宣言して自分で判定すること。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



EPS = 1e-9


#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 14}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """Wilder 平滑の RSI を系列で返す。

    **退化規則 (設計書 §3.2 (i-b))**: 値動きが実質ゼロの区間では
    `avg_gain` と `avg_loss` が同率で減衰し、その比が残るため「横ばいなのに
    RSI が 100」という誤った値になる。相対 ε で潰す。**判定順は固定**:

      (1) `avg_gain + avg_loss <= EPS * |close|`  -> 50.0 (値動きなし = 中立)
      (2) それ以外で `avg_loss <= EPS * |close|`  -> 100.0 (上げのみ)
      (3) それ以外は式どおり

    `|close|` は **その行自身の close** (shift も平均も使わない)。

    **配備済 `rsi_indicator` との差異**: あちらは厳密な `== 0` 判定で
    「下げが無い -> 100」しか持たない。横ばい相場で両者の値は一致しない
    (既存 100 / 本 plugin 50)。**両方を同じ strategy で混ぜて使わないこと。**
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    threshold = EPS * close.abs()
    rsi = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    rsi = rsi.where(avg_loss > threshold, 100.0)
    rsi = rsi.where(avg_gain + avg_loss > threshold, 50.0)
    rsi = rsi.where(~(avg_gain.isna() | avg_loss.isna()))
    return {"rsi": rsi}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/rsi/plugin.py`

### Step 3-d: **green** を確認する

- [ ] `cd docs/examples/plugins/rsi && uv run pytest -q test_plugin.py`
      → **16 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/rsi')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/rsi'), 'rsi')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 3-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-rsi-1 | Wilder alpha -> EMA span | `test_matches_reference_implementation_on_every_row` |
| M-rsi-2 | ε 規則の判定順を入れ替え | `test_eps_rule_fires_after_the_averages_decay`, `test_flat_market_is_neutral_not_one_hundred` |
| M-rsi-3 | EPS を 0 に (厳密判定へ戻す) | `test_eps_rule_fires_after_the_averages_decay` |
| M-rsi-4 | gain/loss を入れ替え | `test_matches_reference_implementation_on_every_row`, `test_monotonic_rise_is_one_hundred` |
| M-rsi-5 | warmup の NaN 復元を削る | `test_matches_reference_implementation_on_every_row`, `test_short_frame_returns_all_declared_keys_as_all_nan`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-rsi-6 | 退化規則 (1) を削る | `test_eps_rule_fires_after_the_averages_decay`, `test_flat_market_is_neutral_not_one_hundred` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 3-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T3: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T3: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/rsi") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/rsi && git commit`
      (メッセージ: `feat(indicator-initial-set): rsi indicator plugin (T3)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T4: `macd` — MACD

**出力**: `macd`, `signal`, `hist` ／ **warmup (最初に値が入る 0 起点行)**: `macd`=25, `signal`=33, `hist`=33 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/macd/` の 3 ファイルのみ。

**この task の要点**: `fast < slow` を `>=` で弾く (`>` ではない — `fast == slow` も退化形)。

### Step 4-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/macd/` を作る
- [ ] `docs/examples/plugins/macd/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [macd, signal, hist]
max_bars: 400
params:
  fast: 12
  slow: 26
  signal_period: 9
```

- [ ] `docs/examples/plugins/macd/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""macd indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('macd', 'signal', 'hist')
WARMUP = {'macd': 25, 'signal': 33, 'hist': 33}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_recursive(values: list, alpha: float, period: int) -> list:
    """再帰平滑。**`min_periods` は位置ではなく「非 NaN 観測数」で数える**
    (設計書 §3.3) — 位置で `i < period - 1` と書くと `atr` / `adx` の warmup
    が 1 本早く明け、I3 が落ちる。seed は最初の非 NaN 値 (`y_0 = x_0`)。
    """
    out = []
    prev = None
    seen = 0
    for value in values:
        if _isnan(value):
            out.append(float("nan"))
            continue
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        seen += 1
        out.append(prev if seen >= period else float("nan"))
    return out


def _reference(df, params: dict) -> dict:
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    signal_period = int(params.get("signal_period", 9))
    close = df["close"].tolist()
    fast_ema = _ref_recursive(close, 2.0 / (fast + 1.0), fast)
    slow_ema = _ref_recursive(close, 2.0 / (slow + 1.0), slow)
    macd = [float("nan") if _isnan(a) or _isnan(b) else a - b
            for a, b in zip(fast_ema, slow_ema)]
    signal = _ref_recursive(macd, 2.0 / (signal_period + 1.0), signal_period)
    hist = [float("nan") if _isnan(a) or _isnan(b) else a - b
            for a, b in zip(macd, signal)]
    return {"macd": macd, "signal": signal, "hist": hist}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'fast': 12.0}, {'slow': '26'}, {'signal_period': True}, {'fast': 0}, {'fast': 26, 'slow': 26}, {'fast': 30, 'slow': 26}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'fat': 12}, {'fast': 12, 'slow': 26, 'signal_period': 9, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`fast` のつもりで `fat` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'fast': 12, 'slow': 26, 'signal_period': 9}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'fast': 5, 'slow': 13, 'signal_period': 4})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


# --- fast < slow の要求 ------------------------------------------------------

def test_hist_equals_macd_minus_signal():
    out = compute(_mkdf(), {})
    tail = slice(33, None)
    difference = (out["macd"].iloc[tail] - out["signal"].iloc[tail]
                  - out["hist"].iloc[tail]).abs().max()
    assert float(difference) < 1e-12


def test_fast_equal_to_slow_is_rejected():
    """`fast == slow` は macd/hist が恒等的に 0 になる退化形 (設計書 §4)。"""
    with pytest.raises(ValueError):
        compute(_mkdf(n=60), {"fast": 26, "slow": 26})
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/macd/test_plugin.py`

### Step 4-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/macd/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "macd": nan_series,
            "signal": nan_series,
            "hist": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/macd && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 4-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/macd/plugin.py` を**逐語**で差し替える:

```python
"""MACD indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**再帰平滑** (前の行の値を使う) を
   含むので、履歴の先頭を切ると初期値 (seed) が変わり、その残差が最終行
   まで残る。再帰が線形なので残差は `|Δseed| × (1−α)^(本数−warmup)` で
   減衰し、400 本あればこの係数が 1e-13 の桁まで落ちる (設計書 §3.2 の
   上界式)。残差の大きさは入力の値域に比例するので、**「400 本なら必ず
   1e-6 以内」という普遍的な保証ではない**。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"fast": 12, "slow": 26, "signal_period": 9}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """MACD / signal / histogram を系列で返す。

    `fast < slow` を要求する (設計書 §4): `fast == slow` は macd と hist が
    恒等的に 0 になる退化形、`fast > slow` は全クロスの符号が反転して
    「macd が signal を上抜けたら買い」が静かに逆売買になる。
    """
    _reject_unknown_params(params)
    fast = _int_param(params, "fast", _DEFAULTS["fast"])
    slow = _int_param(params, "slow", _DEFAULTS["slow"])
    signal_period = _int_param(params, "signal_period",
                               _DEFAULTS["signal_period"])
    if fast >= slow:
        raise ValueError(
            f"params.fast must be < params.slow, got fast={fast} slow={slow}")
    close = df["close"].astype(float)
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=signal_period, adjust=False,
                      min_periods=signal_period).mean()
    return {"macd": macd, "signal": signal, "hist": macd - signal}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/macd/plugin.py`

### Step 4-d: **green** を確認する

- [ ] `cd docs/examples/plugins/macd && uv run pytest -q test_plugin.py`
      → **16 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/macd')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/macd'), 'macd')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 4-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-macd-1 | fast/slow を入れ替え | `test_matches_reference_implementation_on_every_row` |
| M-macd-2 | fast < slow を <= に | `test_fast_equal_to_slow_is_rejected`, `test_invalid_params_raise_value_error[params4]` |
| M-macd-3 | signal を macd でなく close から | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-macd-4 | hist の符号反転 | `test_hist_equals_macd_minus_signal`, `test_matches_reference_implementation_on_every_row` |
| M-macd-5 | signal の min_periods を外す | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-macd-6 | slow の既定 26 -> 12 | `test_hist_equals_macd_minus_signal`, `test_input_frame_is_not_mutated`, `test_matches_reference_implementation_on_every_row` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 4-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T4: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T4: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/macd") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/macd && git commit`
      (メッセージ: `feat(indicator-initial-set): macd indicator plugin (T4)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T5: `bollinger` — ボリンジャーバンド

**出力**: `upper`, `middle`, `lower` ／ **warmup (最初に値が入る 0 起点行)**: `upper`=19, `middle`=19, `lower`=19 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/bollinger/` の 3 ファイルのみ。

**この task の要点**: `ddof=0` (母標準偏差)。`num_std` は有限かつ `> 0`。

### Step 5-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/bollinger/` を作る
- [ ] `docs/examples/plugins/bollinger/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [upper, middle, lower]
max_bars: 400
params:
  period: 20
  num_std: 2.0
```

- [ ] `docs/examples/plugins/bollinger/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""bollinger indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('upper', 'middle', 'lower')
WARMUP = {'upper': 19, 'middle': 19, 'lower': 19}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_sma(values: list, period: int) -> list:
    """SMA を素朴なスライスで。先頭 period-1 個は NaN。"""
    out = []
    for i in range(len(values)):
        window = values[i - period + 1:i + 1]
        if i < period - 1 or any(_isnan(v) for v in window):
            out.append(float("nan"))
        else:
            out.append(sum(window) / float(period))
    return out


def _ref_std(values: list, period: int) -> list:
    """母標準偏差 (ddof=0) を素朴に。"""
    out = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(float("nan"))
            continue
        window = values[i - period + 1:i + 1]
        mean = sum(window) / float(period)
        out.append(math.sqrt(sum((v - mean) ** 2 for v in window) / float(period)))
    return out


def _reference(df, params: dict) -> dict:
    period = int(params.get("period", 20))
    num_std = float(params.get("num_std", 2.0))
    close = df["close"].tolist()
    middle = _ref_sma(close, period)
    sigma = _ref_std(close, period)
    upper = [float("nan") if _isnan(m) else m + num_std * s
             for m, s in zip(middle, sigma)]
    lower = [float("nan") if _isnan(m) else m - num_std * s
             for m, s in zip(middle, sigma)]
    return {"upper": upper, "middle": middle, "lower": lower}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 20.0}, {'period': 0}, {'num_std': '2.0'}, {'num_std': True}, {'num_std': 0.0}, {'num_std': -2.0}, {"num_std": float("inf")}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 20}, {'period': 20, 'num_std': 2.0, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 20, 'num_std': 2.0}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


# --- sigma == 0 の退化 -------------------------------------------------------

def test_zero_sigma_collapses_the_three_bands():
    n = 40
    values = np.full(n, 150.0)
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(n)}, index=index)
    out = compute(df, {})
    assert float(out["upper"].iloc[-1]) == float(out["middle"].iloc[-1])
    assert float(out["lower"].iloc[-1]) == float(out["middle"].iloc[-1])


def test_bands_are_ordered():
    out = compute(_mkdf(), {})
    tail = slice(19, None)
    assert bool((out["upper"].iloc[tail] >= out["middle"].iloc[tail]).all())
    assert bool((out["middle"].iloc[tail] >= out["lower"].iloc[tail]).all())
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/bollinger/test_plugin.py`

### Step 5-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/bollinger/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "upper": nan_series,
            "middle": nan_series,
            "lower": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/bollinger && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 5-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/bollinger/plugin.py` を**逐語**で差し替える:

```python
"""ボリンジャーバンド indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**有限の窓しか見ない** (rolling) ので
   先頭依存そのものは無い — 同じ最終行を出すのに 400 本は要らない。
   400 に揃えてあるのは、9 本で値を 1 つにするため (設計書 §3.2 / 裁定
   D4): 指標ごとに散らすと、依存する strategy が小さく宣言したときに
   「一部の指標だけ静かにずれる」という気づきにくい部分的劣化になる。
   ただし窓 (下の `params`) より短い履歴では値は NaN のままである。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり、窓を満たせない期間は NaN のままになる
   (rolling の逐次更新に由来する丸めの差もわずかに残る)。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import math

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 20, "num_std": 2.0}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def _float_param(params: dict, name: str, default: float) -> float:
    """有限かつ正の数であることを確認する (設計書 §4)。`bool` を先に弾く。"""
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"params.{name} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"params.{name} must be a finite number > 0, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """ボリンジャーバンドを系列で返す (母標準偏差 `ddof=0`)。

    sigma == 0 の行は除算を伴わないので `upper == middle == lower` になる
    — 異常ではない。`num_std` 自体は有限かつ `> 0` を要求する
    (0 は 3 本が同一系列になる退化形、負値は upper/lower の反転)。
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    num_std = _float_param(params, "num_std", _DEFAULTS["num_std"])
    close = df["close"].astype(float)
    middle = close.rolling(window=period, min_periods=period).mean()
    sigma = close.rolling(window=period, min_periods=period).std(ddof=0)
    return {"upper": middle + num_std * sigma,
            "middle": middle,
            "lower": middle - num_std * sigma}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/bollinger/plugin.py`

### Step 5-d: **green** を確認する

- [ ] `cd docs/examples/plugins/bollinger && uv run pytest -q test_plugin.py`
      → **17 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/bollinger')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/bollinger'), 'bollinger')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 5-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-bol-1 | ddof=0 -> ddof=1 | `test_matches_reference_implementation_on_every_row` |
| M-bol-2 | upper/lower の符号を入れ替え | `test_matches_reference_implementation_on_every_row` |
| M-bol-3 | num_std の既定 2.0 -> 1.0 | `test_matches_reference_implementation_on_every_row` |
| M-bol-4 | num_std > 0 の検査を >= 0 に | `test_invalid_params_raise_value_error[params4]` |
| M-bol-5 | num_std の有限検査を外す | `test_invalid_params_raise_value_error[params6]` |
| M-bol-6 | middle の min_periods を外す | `test_matches_reference_implementation_on_every_row`, `test_short_frame_returns_all_declared_keys_as_all_nan`, `test_warmup_boundary_is_pinned_on_both_sides` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 5-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T5: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T5: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/bollinger") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/bollinger && git commit`
      (メッセージ: `feat(indicator-initial-set): bollinger indicator plugin (T5)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T6: `atr` — ATR (Wilder 平滑)

**出力**: `atr` ／ **warmup (最初に値が入る 0 起点行)**: `atr`=14 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/atr/` の 3 ファイルのみ。

**この task の要点**: TR の行 0 は **NaN**。`pd.concat(...).max(axis=1)` は NaN を飛ばすので使わない。

### Step 6-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/atr/` を作る
- [ ] `docs/examples/plugins/atr/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [atr]
max_bars: 400
params:
  period: 14
```

- [ ] `docs/examples/plugins/atr/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""atr indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('atr',)
WARMUP = {'atr': 14}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_recursive(values: list, alpha: float, period: int) -> list:
    """再帰平滑。**`min_periods` は位置ではなく「非 NaN 観測数」で数える**
    (設計書 §3.3) — 位置で `i < period - 1` と書くと `atr` / `adx` の warmup
    が 1 本早く明け、I3 が落ちる。seed は最初の非 NaN 値 (`y_0 = x_0`)。
    """
    out = []
    prev = None
    seen = 0
    for value in values:
        if _isnan(value):
            out.append(float("nan"))
            continue
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        seen += 1
        out.append(prev if seen >= period else float("nan"))
    return out

def _ref_true_range(df) -> list:
    """TR の素朴実装。**行 0 は NaN** (前バーが無い)。"""
    high = df["high"].tolist()
    low = df["low"].tolist()
    close = df["close"].tolist()
    out = [float("nan")]
    for i in range(1, len(close)):
        out.append(max(high[i] - low[i],
                       abs(high[i] - close[i - 1]),
                       abs(low[i] - close[i - 1])))
    return out


def _reference(df, params: dict) -> dict:
    period = int(params.get("period", 14))
    return {"atr": _ref_recursive(_ref_true_range(df), 1.0 / period, period)}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 14.0}, {'period': '14'}, {'period': True}, {'period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 14}, {'period': 14, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 14}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/atr/test_plugin.py`

### Step 6-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/atr/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "atr": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/atr && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 6-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/atr/plugin.py` を**逐語**で差し替える:

```python
"""ATR (Average True Range、Wilder 平滑) indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**再帰平滑** (前の行の値を使う) を
   含むので、履歴の先頭を切ると初期値 (seed) が変わり、その残差が最終行
   まで残る。再帰が線形なので残差は `|Δseed| × (1−α)^(本数−warmup)` で
   減衰し、400 本あればこの係数が 1e-13 の桁まで落ちる (設計書 §3.2 の
   上界式)。残差の大きさは入力の値域に比例するので、**「400 本なら必ず
   1e-6 以内」という普遍的な保証ではない**。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 14}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def _true_range(df: "pd.DataFrame") -> "pd.Series":
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)。

    **行 0 は NaN** (前バーが無いので未確定)。`np.maximum` は NaN を伝播
    するのでこの性質が保たれる — `pd.concat([...], axis=1).max(axis=1)` は
    NaN を飛ばして `high - low` を返してしまうので使わない (設計書 §3.0)。
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    return pd.Series(
        np.maximum(np.maximum((high - low).to_numpy(),
                              (high - prev_close).abs().to_numpy()),
                   (low - prev_close).abs().to_numpy()),
        index=df.index)


def compute(df: pd.DataFrame, params: dict) -> dict:
    """Wilder 平滑の ATR を系列で返す。TR の行 0 は NaN なので、値が入るのは
    index `period` (= period+1 本目) から。"""
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    true_range = _true_range(df)
    atr = true_range.ewm(alpha=1.0 / period, adjust=False,
                         min_periods=period).mean()
    return {"atr": atr}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/atr/plugin.py`

### Step 6-d: **green** を確認する

- [ ] `cd docs/examples/plugins/atr && uv run pytest -q test_plugin.py`
      → **12 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/atr')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/atr'), 'atr')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 6-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-atr-1 | Wilder alpha -> EMA span | `test_matches_reference_implementation_on_every_row` |
| M-atr-2 | TR の行 0 を NaN でなく high-low に | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-atr-3 | shift(1) -> shift(-1) (未来参照) | `test_matches_reference_implementation_on_every_row`, `test_prefix_consistency_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-atr-4 | max -> min | `test_matches_reference_implementation_on_every_row` |
| M-atr-5 | min_periods を外す | `test_matches_reference_implementation_on_every_row`, `test_short_frame_returns_all_declared_keys_as_all_nan`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-atr-6 | period の既定 14 -> 20 | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 6-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T6: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T6: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/atr") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/atr && git commit`
      (メッセージ: `feat(indicator-initial-set): atr indicator plugin (T6)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T7: `adx` — ADX / +DI / -DI

**出力**: `adx`, `plus_di`, `minus_di` ／ **warmup (最初に値が入る 0 起点行)**: `adx`=27, `plus_di`=14, `minus_di`=14 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/adx/` の 3 ファイルのみ。

**この task の要点**: ε 判定を `±DI` を作る**前**に置くこと。`±DM` の行 0 は `mask` で **NaN** にする (0.0 のままだと Wilder の seed が変わる)。

### Step 7-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/adx/` を作る
- [ ] `docs/examples/plugins/adx/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [adx, plus_di, minus_di]
max_bars: 400
params:
  period: 14
```

- [ ] `docs/examples/plugins/adx/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""adx indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('adx', 'plus_di', 'minus_di')
WARMUP = {'adx': 27, 'plus_di': 14, 'minus_di': 14}
EPS = 1e-9


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_recursive(values: list, alpha: float, period: int) -> list:
    """再帰平滑。**`min_periods` は位置ではなく「非 NaN 観測数」で数える**
    (設計書 §3.3) — 位置で `i < period - 1` と書くと `atr` / `adx` の warmup
    が 1 本早く明け、I3 が落ちる。seed は最初の非 NaN 値 (`y_0 = x_0`)。
    """
    out = []
    prev = None
    seen = 0
    for value in values:
        if _isnan(value):
            out.append(float("nan"))
            continue
        prev = value if prev is None else alpha * value + (1.0 - alpha) * prev
        seen += 1
        out.append(prev if seen >= period else float("nan"))
    return out

def _ref_true_range(df) -> list:
    """TR の素朴実装。**行 0 は NaN** (前バーが無い)。"""
    high = df["high"].tolist()
    low = df["low"].tolist()
    close = df["close"].tolist()
    out = [float("nan")]
    for i in range(1, len(close)):
        out.append(max(high[i] - low[i],
                       abs(high[i] - close[i - 1]),
                       abs(low[i] - close[i - 1])))
    return out


def _reference(df, params: dict) -> dict:
    """ε 規則も plugin.py と同じ閾値・基準・順序で書き写す (設計書 §3.3)。"""
    period = int(params.get("period", 14))
    high = df["high"].tolist()
    low = df["low"].tolist()
    close = df["close"].tolist()
    plus_dm = [float("nan")]
    minus_dm = [float("nan")]
    for i in range(1, len(close)):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0.0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0.0) else 0.0)
    alpha = 1.0 / period
    atr = _ref_recursive(_ref_true_range(df), alpha, period)
    sm_plus = _ref_recursive(plus_dm, alpha, period)
    sm_minus = _ref_recursive(minus_dm, alpha, period)
    plus_di = []
    minus_di = []
    dx = []
    for i in range(len(close)):
        if _isnan(atr[i]) or _isnan(sm_plus[i]) or _isnan(sm_minus[i]):
            plus_di.append(float("nan"))
            minus_di.append(float("nan"))
            dx.append(float("nan"))
            continue
        if atr[i] <= EPS * abs(close[i]):
            pdi = 0.0
            mdi = 0.0
        else:
            pdi = 100.0 * sm_plus[i] / atr[i]
            mdi = 100.0 * sm_minus[i] / atr[i]
        plus_di.append(pdi)
        minus_di.append(mdi)
        denominator = pdi + mdi
        dx.append(0.0 if denominator == 0.0
                  else 100.0 * abs(pdi - mdi) / denominator)
    return {"adx": _ref_recursive(dx, alpha, period),
            "plus_di": plus_di, "minus_di": minus_di}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 14.0}, {'period': '14'}, {'period': True}, {'period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 14}, {'period': 14, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 14}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


def _decayed_flat_df(n_flat: int = 260, price: float = 150.0,
                     move: float = 1.0) -> pd.DataFrame:
    """1 本だけ上げたあと `n_flat` 本の完全横ばい。

    平滑量は指数的に減衰して**実質ゼロ**になるが、**厳密なゼロにはならない**
    — 厳密 `== 0` 判定では捕まらず、比を取る指標が「値動きが無いのに強い
    トレンド」という値を返す構成 (設計書 §3.2 (i-b) の反例)。相対 ε 規則が
    効いているかはこの fixture でしか観測できない (変異スイープの実測:
    完全横ばいだけの fixture では EPS=0 への変異が生き残る)。
    """
    closes = [price] * 5 + [price + move] * (n_flat + 1)
    values = np.array(closes, dtype="float64")
    index = pd.date_range("2026-01-01", periods=len(values), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(len(values))}, index=index)


def test_eps_rule_fires_after_the_atr_decays():
    """ATR が厳密 0 でなく実質ゼロまで減衰した区間でも +DI/-DI/ADX は 0。
    判定を `atr != 0.0` (厳密) に戻すと、`Wilder(+DM)` と `ATR` が同率で
    減衰して比が残り `plus_di` が 100 近くになる (設計書 §3.2 (i-b))。"""
    out = compute(_decayed_flat_df(), {})
    assert float(out["plus_di"].iloc[-1]) == 0.0
    assert float(out["minus_di"].iloc[-1]) == 0.0
    # `adx` は `Wilder(DX)` なので厳密 0 にはならない — ε 規則が効き始めるのは
    # ATR が閾値を割ってから (約 180 本後) で、そこから DX=0 が続いた本数ぶん
    # しか減衰しない (設計書 §3.2 (i-b) の「境界付近では ε をどう選んでも残る」)。
    # 規則が無ければここは 90 以上になるので、この上界で十分に判別できる。
    assert float(out["adx"].iloc[-1]) < 1.0


# --- 退化規則 (ε) -----------------------------------------------------------

def _flat_df(n: int = 60, price: float = 150.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    values = np.full(n, price)
    return pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(n)}, index=index)


def test_flat_market_yields_zero_di_and_zero_adx():
    """完全横ばいでは `atr <= EPS*|close|` が成立し +DI/-DI/ADX が 0 になる。
    規則が無いと `Wilder(+DM)` と `ATR` が同率で減衰して比が残り、「値動きが
    無いのに +DI が 100」という誤った値を返す (設計書 §3.2 (i-b))。"""
    out = compute(_flat_df(), {})
    assert float(out["plus_di"].iloc[-1]) == 0.0
    assert float(out["minus_di"].iloc[-1]) == 0.0
    assert float(out["adx"].iloc[-1]) == 0.0


def test_eps_rule_does_not_fire_on_ordinary_data():
    out = compute(_mkdf(), {})
    assert float(out["plus_di"].iloc[-1]) > 0.0
    assert float(out["minus_di"].iloc[-1]) > 0.0


def _symmetric_expansion_df(n: int = 60, price: float = 150.0,
                            step: float = 0.01) -> pd.DataFrame:
    """毎バー high が `+step`・low が `-step` で対称に広がる (close は不動)。

    すべての行で `up_move == down_move == step > 0` の**同着**になる。Wilder の
    規則は「同着なら +DM も -DM も 0」なので `plus_di == minus_di == 0`、
    したがって `dx = 0` / `adx = 0` になる。

    **`atr` は 0.94 まで育つので ε 規則 (`atr <= EPS*|close|` = 1.5e-07) は
    発火しない** — この pin は退化規則の言い換えではなく、同着規則そのものを
    見ている (恒真ではないことを段 0 で実測)。
    """
    high = np.array([price + step * (i + 1) for i in range(n)])
    low = np.array([price - step * (i + 1) for i in range(n)])
    close = np.full(n, price)
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close,
         "volume": np.ones(n)}, index=index)


def test_equal_up_and_down_move_yields_no_directional_movement():
    """`up_move == down_move` の同着では +DM / -DM とも 0 (Wilder の規則)。

    比較を `>` から `>=` に緩めると**同着ぶんが片側へ丸ごと入る**:
    段 0 の実測で `plus_dm` 側を `>=` にすると `plus_di` が 0.0 -> 1.0598、
    `adx` が 0.0 -> **100.0** (「方向性なし」が「最強のトレンド」に化ける)。
    `minus_dm` 側を `>=` にすると対称に `minus_di` が 1.0598 / `adx` 100.0。
    ランダムウォークの fixture では同着が測度 0 でしか起きないため、
    この構成でしか観測できない (M32 / M41 は既存の 16 テストを素通りした)。
    """
    out = compute(_symmetric_expansion_df(), {})
    assert float(out["plus_di"].iloc[-1]) == 0.0
    assert float(out["minus_di"].iloc[-1]) == 0.0
    assert float(out["adx"].iloc[-1]) == 0.0


def test_di_stays_within_bounds():
    out = compute(_mkdf(), {})
    for key in ("adx", "plus_di", "minus_di"):
        values = out[key].dropna()
        assert bool((values >= 0.0).all()) and bool((values <= 100.0).all()), key
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/adx/test_plugin.py`

### Step 7-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/adx/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "adx": nan_series,
            "plus_di": nan_series,
            "minus_di": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/adx && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 7-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/adx/plugin.py` を**逐語**で差し替える:

```python
"""ADX / +DI / -DI (Wilder 平滑) indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は平滑量どうしの**比**を取り、その比が
   非線形な除算を通って次の段へ入る。したがって線形再帰の上界式は
   **使えず、400 本で一致するという普遍的な保証は書けない**
   (設計書 §3.2 (i-b))。保証の形は「設計書 §6 I5 が生成式と seed まで
   仕様化した fixture 集合での受入公差」であって、任意の入力に対する
   上界ではない。値動きの無い期間で比が退化する件は、下の
   「値動きの無い期間は中立値を返す」の項 (ε 規則) で扱う。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

5. **値動きの無い期間は中立値を返す** (設計書 §3.2 (i-b))。判定は
   `<= EPS * |close|` の相対 ε (EPS = 1e-9)。`|close|` は**その行自身の
   close**。strategy 側で「中立」と「板が動いていない」を読み分けたいなら
   `atr` を併せて宣言して自分で判定すること。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd



EPS = 1e-9


#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 14}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def _true_range(df: "pd.DataFrame") -> "pd.Series":
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)。

    **行 0 は NaN** (前バーが無いので未確定)。`np.maximum` は NaN を伝播
    するのでこの性質が保たれる — `pd.concat([...], axis=1).max(axis=1)` は
    NaN を飛ばして `high - low` を返してしまうので使わない (設計書 §3.0)。
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    return pd.Series(
        np.maximum(np.maximum((high - low).to_numpy(),
                              (high - prev_close).abs().to_numpy()),
                   (low - prev_close).abs().to_numpy()),
        index=df.index)


def compute(df: pd.DataFrame, params: dict) -> dict:
    """ADX / +DI / -DI を系列で返す (すべて Wilder 平滑)。

    **退化規則 (設計書 §3.2 (i-b))**: 値動きが実質ゼロの区間では
    `Wilder(+DM)` と `ATR` が同率で減衰し比が残るため、「400 本まったく
    値動きが無いのに +DI が 100 (= 強い上昇トレンド)」という誤った値になる。
    `atr <= EPS * |close|` の行では `plus_di = minus_di = 0` に倒す
    (-> `dx = 0`)。`|close|` は **その行自身の close**。
    判定は `+/-DI` を作る**前**に行う。

    +DM / -DM の行 0 は NaN (前バーが無いので未確定)。
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    true_range = _true_range(df)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    plus_dm = plus_dm.mask(up_move.isna())
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    minus_dm = minus_dm.mask(down_move.isna())

    alpha = 1.0 / period
    atr = true_range.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    flat = atr <= EPS * close.abs()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False,
                                  min_periods=period).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False,
                                    min_periods=period).mean() / atr
    plus_di = plus_di.where(~flat, 0.0)
    minus_di = minus_di.where(~flat, 0.0)

    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    dx = dx.where(denominator != 0.0, 0.0)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/adx/plugin.py`

### Step 7-d: **green** を確認する

- [ ] `cd docs/examples/plugins/adx && uv run pytest -q test_plugin.py`
      → **16 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/adx')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/adx'), 'adx')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 7-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-adx-1 | ε 規則を削る (flat 判定なし) | `test_eps_rule_fires_after_the_atr_decays` |
| M-adx-2 | ε 判定を DI の後ろへ (順序入れ替え) | `test_eps_rule_fires_after_the_atr_decays` |
| M-adx-3 | +DM / -DM を入れ替え | `test_matches_reference_implementation_on_every_row` |
| M-adx-4 | DM の行 0 マスクを削る | `test_matches_reference_implementation_on_every_row` |
| M-adx-5 | Wilder alpha -> EMA span | `test_matches_reference_implementation_on_every_row` |
| M-adx-6 | dx のゼロ除算規則を削る | `test_eps_rule_fires_after_the_atr_decays`, `test_flat_market_yields_zero_di_and_zero_adx` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 7-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T7: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T7: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/adx") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/adx && git commit`
      (メッセージ: `feat(indicator-initial-set): adx indicator plugin (T7)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T8: `stochastic` — ストキャスティクス (slow)

**出力**: `k`, `d` ／ **warmup (最初に値が入る 0 起点行)**: `k`=15, `d`=17 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/stochastic/` の 3 ファイルのみ。

**この task の要点**: `k` は **raw %K の SMA** (slow)。`k_period: 1` で fast になる。

### Step 8-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/stochastic/` を作る
- [ ] `docs/examples/plugins/stochastic/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [k, d]
max_bars: 400
params:
  period: 14
  k_period: 3
  d_period: 3
```

- [ ] `docs/examples/plugins/stochastic/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""stochastic indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('k', 'd')
WARMUP = {'k': 15, 'd': 17}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_sma(values: list, period: int) -> list:
    """SMA を素朴なスライスで。先頭 period-1 個は NaN。"""
    out = []
    for i in range(len(values)):
        window = values[i - period + 1:i + 1]
        if i < period - 1 or any(_isnan(v) for v in window):
            out.append(float("nan"))
        else:
            out.append(sum(window) / float(period))
    return out

def _ref_rolling(values: list, period: int, pick) -> list:
    """rolling min / max を素朴なスライスで。"""
    out = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(float("nan"))
        else:
            out.append(pick(values[i - period + 1:i + 1]))
    return out


def _reference(df, params: dict) -> dict:
    period = int(params.get("period", 14))
    k_period = int(params.get("k_period", 3))
    d_period = int(params.get("d_period", 3))
    close = df["close"].tolist()
    highest = _ref_rolling(df["high"].tolist(), period, max)
    lowest = _ref_rolling(df["low"].tolist(), period, min)
    raw_k = []
    for i in range(len(close)):
        if _isnan(highest[i]) or _isnan(lowest[i]):
            raw_k.append(float("nan"))
            continue
        span = highest[i] - lowest[i]
        raw_k.append(50.0 if span == 0.0
                     else 100.0 * (close[i] - lowest[i]) / span)
    k = _ref_sma(raw_k, k_period)
    return {"k": k, "d": _ref_sma(k, d_period)}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'period': 14.0}, {'k_period': '3'}, {'d_period': True}, {'period': 0}, {'k_period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'perid': 14}, {'period': 14, 'k_period': 3, 'd_period': 3, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`period` のつもりで `perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'period': 14, 'k_period': 3, 'd_period': 3}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'period': 5, 'k_period': 1})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


# --- ゼロ除算 (HH == LL) と fast への切替 -----------------------------------

def test_flat_window_yields_fifty():
    """期間内が完全横ばい (HH == LL) の行は raw %K を 50 にする。"""
    n = 40
    values = np.full(n, 150.0)
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": values, "high": values, "low": values, "close": values,
         "volume": np.ones(n)}, index=index)
    out = compute(df, {})
    assert float(out["k"].iloc[-1]) == 50.0
    assert float(out["d"].iloc[-1]) == 50.0


def test_k_period_one_gives_fast_stochastic():
    """`k_period: 1` は raw %K そのもの = fast stochastic (設計書 D2)。"""
    df = _mkdf()
    fast = compute(df, {"k_period": 1})
    assert not bool(pd.isna(fast["k"].iloc[13]))
    assert bool(pd.isna(fast["k"].iloc[12]))
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/stochastic/test_plugin.py`

### Step 8-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/stochastic/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "k": nan_series,
            "d": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/stochastic && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 8-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/stochastic/plugin.py` を**逐語**で差し替える:

```python
"""ストキャスティクス (slow、%K / %D) indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**有限の窓しか見ない** (rolling) ので
   先頭依存そのものは無い — 同じ最終行を出すのに 400 本は要らない。
   400 に揃えてあるのは、9 本で値を 1 つにするため (設計書 §3.2 / 裁定
   D4): 指標ごとに散らすと、依存する strategy が小さく宣言したときに
   「一部の指標だけ静かにずれる」という気づきにくい部分的劣化になる。
   ただし窓 (下の `params`) より短い履歴では値は NaN のままである。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり、窓を満たせない期間は NaN のままになる
   (rolling の逐次更新に由来する丸めの差もわずかに残る)。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 14, "k_period": 3, "d_period": 3}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """slow stochastic の %K / %D を系列で返す (既定 14/3/3、設計書 D2)。

      raw %K = 100 * (close - LL(period)) / (HH(period) - LL(period))
      k      = SMA(k_period) of raw %K
      d      = SMA(d_period) of k

    `HH == LL` (期間内が完全な横ばい) の行は raw %K を 50.0 にする。
    `k_period: 1` を上書きすると fast stochastic になる (承認不要の調整)。
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    k_period = _int_param(params, "k_period", _DEFAULTS["k_period"])
    d_period = _int_param(params, "d_period", _DEFAULTS["d_period"])
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    highest = high.rolling(window=period, min_periods=period).max()
    lowest = low.rolling(window=period, min_periods=period).min()
    span = highest - lowest
    raw_k = 100.0 * (close - lowest) / span
    raw_k = raw_k.where(span != 0.0, 50.0)
    k = raw_k.rolling(window=k_period, min_periods=k_period).mean()
    d = k.rolling(window=d_period, min_periods=d_period).mean()
    return {"k": k, "d": d}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/stochastic/plugin.py`

### Step 8-d: **green** を確認する

- [ ] `cd docs/examples/plugins/stochastic && uv run pytest -q test_plugin.py`
      → **15 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/stochastic')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/stochastic'), 'stochastic')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 8-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-sto-1 | k を raw %K にする (slow -> fast) | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-sto-2 | d を raw から取る | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-sto-3 | HH/LL を入れ替え | `test_matches_reference_implementation_on_every_row` |
| M-sto-4 | span==0 の 50.0 規則を削る | `test_flat_window_yields_fifty` |
| M-sto-5 | rolling を center=True に (未来参照) | `test_flat_window_yields_fifty`, `test_matches_reference_implementation_on_every_row`, `test_prefix_consistency_on_every_row` |
| M-sto-6 | min_periods を外す | `test_matches_reference_implementation_on_every_row`, `test_warmup_boundary_is_pinned_on_both_sides` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 8-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T8: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T8: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/stochastic") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/stochastic && git commit`
      (メッセージ: `feat(indicator-initial-set): stochastic indicator plugin (T8)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T9: `ichimoku` — 一目均衡表

**出力**: `tenkan`, `kijun`, `senkou_a`, `senkou_b`, `chikou` ／ **warmup (最初に値が入る 0 起点行)**: `tenkan`=8, `kijun`=25, `senkou_a`=25, `senkou_b`=51, `chikou`=0 ／ **`max_bars`**: 400
**依存**: T0 のみ。他の指標 task と**並列実行可**。
**触るファイル**: `docs/examples/plugins/ichimoku/` の 3 ファイルのみ。

**この task の要点**: 先行/遅行スパンを **shift しない** (設計書 R5)。3 期間の順序制約は**付けない**。

### Step 9-a: `config.yaml` と `test_plugin.py` を転写する

- [ ] `docs/examples/plugins/ichimoku/` を作る
- [ ] `docs/examples/plugins/ichimoku/config.yaml` を**逐語**で作る:

```yaml
kind: indicator
outputs: [tenkan, kijun, senkou_a, senkou_b, chikou]
max_bars: 400
params:
  tenkan_period: 9
  kijun_period: 26
  senkou_b_period: 52
```

- [ ] `docs/examples/plugins/ichimoku/test_plugin.py` を**逐語**で作る
      (**フェンス行 ` ```python ` / ` ``` ` をファイルに書かないこと**):

```python
"""ichimoku indicator plugin の自己テスト ([indicator-initial-set] 設計書 §6)。

担保する受入条件: **I2** (outputs 完全一致 / index 一致 / Inf 不在)、
**I3** (独立参照実装との全行一致)、**I4** (固定 fixture における全行の
接頭辞一致)、**I9** (入力不変・反復決定性)、warmup 境界の両側 pin、
params の型検証。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと
(`to_frame` / `df.open` / `getattr` は使えない)。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ('tenkan', 'kijun', 'senkou_a', 'senkou_b', 'chikou')
WARMUP = {'tenkan': 8, 'kijun': 25, 'senkou_a': 25, 'senkou_b': 51, 'chikou': 0}


def _mkdf(n: int = 160, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    """設計書 §6 I4 の固定 fixture。160 行は `ichimoku` の `senkou_b` warmup
    51 + 余裕。**極値を中間と末尾側の両方に置く** — 片側だけだと「全体の
    min/max で正規化する」型の未来参照を fixture 次第で見逃す (§6.2)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if n >= 20:
        close[n // 2] += base * 0.05
        close[n - 7] -= base * 0.06
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    if n >= 2:
        # **bar 1 に決定論的な上げを置く** — ここが平坦だと `+DM[0]` を NaN に
        # するか 0.0 にするかが Wilder の seed に効かず、行 0 の扱いを潰す変異
        # (M-adx-4) が fixture 次第で生き残る (変異スイープの実測)。
        high[1] = high[0] + base * 0.01
    # **open は close と別の系列にする** (前バーの終値)。`open == close` の
    # fixture だと「close の代わりに open を読む」型の変異を検出できない
    # (M-sma-4 の実測)。
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
          "volume": np.ones(n)}, index=index)


def _same(got, want, *, rel: float = 1e-9, tol: float = 1e-9) -> bool:
    """NaN 同士は一致。数値は相対/絶対のどちらかを満たせば一致。"""
    got_nan = got is None or bool(pd.isna(got))
    want_nan = want is None or bool(pd.isna(want))
    if got_nan or want_nan:
        return got_nan and want_nan
    got = float(got)
    want = float(want)
    return abs(got - want) <= max(tol, rel * abs(want))


# --- 独立参照実装 (plugin.py とは別の書き方。plugin.py から import しない) ---

def _isnan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

def _ref_rolling(values: list, period: int, pick) -> list:
    """rolling min / max を素朴なスライスで。"""
    out = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(float("nan"))
        else:
            out.append(pick(values[i - period + 1:i + 1]))
    return out


def _ref_midpoint(df, window: int) -> list:
    highest = _ref_rolling(df["high"].tolist(), window, max)
    lowest = _ref_rolling(df["low"].tolist(), window, min)
    return [float("nan") if _isnan(h) else (h + l) / 2.0
            for h, l in zip(highest, lowest)]


def _reference(df, params: dict) -> dict:
    tenkan = _ref_midpoint(df, int(params.get("tenkan_period", 9)))
    kijun = _ref_midpoint(df, int(params.get("kijun_period", 26)))
    return {"tenkan": tenkan,
            "kijun": kijun,
            "senkou_a": [float("nan") if _isnan(t) or _isnan(k) else (t + k) / 2.0
                         for t, k in zip(tenkan, kijun)],
            "senkou_b": _ref_midpoint(df, int(params.get("senkou_b_period", 52))),
            "chikou": list(df["close"].tolist())}


# --- I2: 宣言キー集合 / index 一致 / Inf 不在 -------------------------------

def test_outputs_match_declared_keys_and_index():
    df = _mkdf()
    out = compute(df, {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        assert isinstance(out[key], pd.Series), key
        assert out[key].index.equals(df.index), key
        assert not bool(np.isinf(out[key].to_numpy(dtype="float64")).any()), key


# --- I3: 独立参照実装との全行一致 -------------------------------------------

def test_matches_reference_implementation_on_every_row():
    df = _mkdf()
    got = compute(df, {})
    want = _reference(df, {})
    for key in OUTPUTS:
        for i in range(len(df)):
            assert _same(got[key].iloc[i], want[key][i]), (key, i)


# --- I4: 固定 fixture における接頭辞一致 (全 160 行) ------------------------

def test_prefix_consistency_on_every_row():
    """`compute(df[:t+1])[key].iloc[-1] == compute(df)[key].iloc[t]` を
    **全行**で。サンプル点だけだと「サンプルされない行だけ未来を読む」実装
    (実測: 行 37 のみ書き換え) を素通りする (設計書 §6.2)。
    """
    df = _mkdf()
    full = compute(df, {})
    for t in range(len(df)):
        sub = compute(df.iloc[:t + 1], {})
        for key in OUTPUTS:
            assert _same(sub[key].iloc[-1], full[key].iloc[t]), (key, t)


# --- I9: 入力不変 / 反復決定性 ----------------------------------------------

def test_input_frame_is_not_mutated():
    """`check_source` は添字代入 `df["x"] = ...` を拒否しない (設計書 §4)。
    入力を壊さない規律の観測点はこのテストだけ。"""
    df = _mkdf()
    before = df.copy(deep=True)
    compute(df, {})
    assert df.equals(before)
    assert list(df.columns) == list(before.columns)
    assert df.index.equals(before.index)
    assert bool((df.dtypes == before.dtypes).all())


def test_repeated_calls_are_deterministic():
    """(1) fresh な df で 2 回 (2) **同じ df オブジェクトで 2 回** —
    (2) が module レベルの状態持ち越しを捕まえる。"""
    first = compute(_mkdf(), {})
    second = compute(_mkdf(), {})
    df = _mkdf()
    third = compute(df, {})
    fourth = compute(df, {})
    for key in OUTPUTS:
        for i in range(len(first[key])):
            assert _same(first[key].iloc[i], second[key].iloc[i]), ("fresh", key, i)
            assert _same(third[key].iloc[i], fourth[key].iloc[i]), ("same", key, i)


# --- warmup 境界 (両側を pin する) ------------------------------------------

def test_warmup_boundary_is_pinned_on_both_sides():
    """「N 行目まで NaN」だけでなく「N+1 行目に値が入る」も見る — 片側だけ
    だと warmup が 1 本早く/遅く明ける変異を検出できない。"""
    out = compute(_mkdf(), {})
    for key, first_valid in WARMUP.items():
        series = out[key]
        if first_valid > 0:
            assert bool(series.iloc[:first_valid].isna().all()), key
        assert not bool(pd.isna(series.iloc[first_valid])), key


def test_short_frame_returns_all_declared_keys_as_all_nan():
    """行が足りなくても「返さない」はできない — 宣言キーは必ず全部返す。"""
    out = compute(_mkdf(n=3), {})
    assert set(out) == set(OUTPUTS)
    for key in OUTPUTS:
        if WARMUP[key] >= 3:
            assert bool(out[key].isna().all()), key


# --- params の型検証 (変換ではなく型の確認) ---------------------------------

@pytest.mark.parametrize("params", [{'tenkan_period': 9.0}, {'kijun_period': '26'}, {'senkou_b_period': True}, {'tenkan_period': 0}])
def test_invalid_params_raise_value_error(params):
    """**文言まで pin する。** `match` が無いと、検査を外す変異を入れても
    pandas 自身が投げる `ValueError` (`span must satisfy: span >= 1` 等) で
    テストが緑のまま通ってしまう (変異スイープの実測: M-sma-5 / M-ema-4)。
    本 plugin の文言はすべて `params.<キー名> ...` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


@pytest.mark.parametrize("params", [{'tenkan_perid': 9}, {'tenkan_period': 9, 'kijun_period': 26, 'senkou_b_period': 52, 'unused': 1}])
def test_unknown_params_raise_value_error(params):
    """**未知の params キーは黙って無視しない。** strategy 側の params 上書き
    (R8 / U3) は承認不要なので、`tenkan_period` のつもりで `tenkan_perid` と綴りを誤ると
    現状は既定値のまま動き、backtest がその値を前提に結果を出す。文言は
    他の params 例外と同じく `params.` で始まる (設計書 §4)。
    """
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=60), params)


def test_declared_defaults_are_exposed_as_a_constant():
    """`_DEFAULTS` が `config.yaml` の `params` と突き合わせられる形で
    公開されていること (受入テストが値の表を重複して持たないため)。"""
    from plugin import _DEFAULTS, _KNOWN_PARAMS
    assert set(_DEFAULTS) == set(_KNOWN_PARAMS)
    assert _DEFAULTS == {'tenkan_period': 9, 'kijun_period': 26, 'senkou_b_period': 52}


def test_valid_param_override_changes_the_result():
    df = _mkdf()
    base = compute(df, {})
    other = compute(df, {'tenkan_period': 3, 'kijun_period': 7, 'senkou_b_period': 15})
    key = OUTPUTS[0]
    assert not _same(base[key].iloc[-1], other[key].iloc[-1])


# --- lookahead 禁止 (R5) ----------------------------------------------------

def test_spans_are_not_shifted_into_the_future():
    """`senkou_a` / `senkou_b` は「そのバー時点で確定した値」。未来へずらして
    いないことは (i) warmup 境界が shift 無しの位置にあること
    (ii) 全行の接頭辞一致 (`test_prefix_consistency_on_every_row`) で観測する。
    ここでは (i) を独立に pin する。"""
    out = compute(_mkdf(), {})
    assert bool(out["senkou_b"].iloc[:51].isna().all())
    assert not bool(pd.isna(out["senkou_b"].iloc[51]))
    assert bool(out["senkou_a"].iloc[:25].isna().all())
    assert not bool(pd.isna(out["senkou_a"].iloc[25]))


def test_chikou_is_the_current_close_not_shifted():
    """遅行スパンは「現在の終値」そのもの。`shift(-26)` を掛けない。"""
    df = _mkdf()
    chikou = compute(df, {})["chikou"]
    assert bool((chikou.to_numpy() == df["close"].to_numpy()).all())
    assert not bool(chikou.isna().any())
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/ichimoku/test_plugin.py`

### Step 9-b: stub を置いて **red** を確認する

- [ ] `docs/examples/plugins/ichimoku/plugin.py` を**この stub**にする:

```python
"""stub (red 確認用)。**このファイルは Step c で本実装に差し替える。**"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    nan_series = pd.Series(np.full(len(df), np.nan), index=df.index,
                           dtype="float64")
    return {
            "tenkan": nan_series,
            "kijun": nan_series,
            "senkou_a": nan_series,
            "senkou_b": nan_series,
            "chikou": nan_series,
    }
```

- [ ] `cd docs/examples/plugins/ichimoku && uv run pytest -q test_plugin.py` を回す
- [ ] **collection error ではなく assert-red** (複数の `FAILED`) になることを確認する
- [ ] **この pytest 出力を逐語で報告に貼る** (「red を確認した」という申告は証拠にならない)

### Step 9-c: `plugin.py` の本実装を転写する

- [ ] `docs/examples/plugins/ichimoku/plugin.py` を**逐語**で差し替える:

```python
"""一目均衡表 indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**有限の窓しか見ない** (rolling) ので
   先頭依存そのものは無い — 同じ最終行を出すのに 400 本は要らない。
   400 に揃えてあるのは、9 本で値を 1 つにするため (設計書 §3.2 / 裁定
   D4): 指標ごとに散らすと、依存する strategy が小さく宣言したときに
   「一部の指標だけ静かにずれる」という気づきにくい部分的劣化になる。
   ただし窓 (下の `params`) より短い履歴では値は NaN のままである。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり、窓を満たせない期間は NaN のままになる
   (rolling の逐次更新に由来する丸めの差もわずかに残る)。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

5. **先行/遅行スパンの lookahead 規約** — 下の `compute` の docstring を参照。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"tenkan_period": 9, "kijun_period": 26, "senkou_b_period": 52}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def _midpoint(df: pd.DataFrame, window: int) -> pd.Series:
    """(期間内の最高値 + 最安値) / 2。"""
    highest = df["high"].astype(float).rolling(
        window=window, min_periods=window).max()
    lowest = df["low"].astype(float).rolling(
        window=window, min_periods=window).min()
    return (highest + lowest) / 2.0


def compute(df: pd.DataFrame, params: dict) -> dict:
    """一目均衡表の 5 本を系列で返す (既定 9/26/52)。

    **lookahead 禁止 (設計書 R5)**: `senkou_a` / `senkou_b` に `shift(+26)`
    を掛けない。`chikou` に `shift(-26)` を掛けない。**返すのはすべて
    「そのバー時点で確定している値」**であり、「26 本先へ投影した雲」でも
    「26 本前へ遡らせた遅行線」でもない。

    雲との比較をしたい strategy は
    `indicators["ichi"]["senkou_a"].shift(kijun_period)` を自分で取ること。
    この plugin は未来の行に値を置かない (接頭辞一致 §6 I4 の担保でもある)。

    3 期間の順序 (`tenkan < kijun < senkou_b`) は**要求しない** — 式は任意の
    順序で well-defined で、符号反転も退化も起きない。順序を強制すると
    7/22/44 のような正当なパラメータ探索を塞ぐ (設計書 §4)。
    """
    _reject_unknown_params(params)
    tenkan_period = _int_param(params, "tenkan_period", _DEFAULTS["tenkan_period"])
    kijun_period = _int_param(params, "kijun_period", _DEFAULTS["kijun_period"])
    senkou_b_period = _int_param(params, "senkou_b_period",
                                 _DEFAULTS["senkou_b_period"])
    tenkan = _midpoint(df, tenkan_period)
    kijun = _midpoint(df, kijun_period)
    return {"tenkan": tenkan,
            "kijun": kijun,
            "senkou_a": (tenkan + kijun) / 2.0,
            "senkou_b": _midpoint(df, senkou_b_period),
            "chikou": df["close"].astype(float)}
```

- [ ] `python -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" docs/examples/plugins/ichimoku/plugin.py`

### Step 9-d: **green** を確認する

- [ ] `cd docs/examples/plugins/ichimoku && uv run pytest -q test_plugin.py`
      → **14 passed** になること (この本数を報告に書く)
- [ ] `check_source` が 2 本とも通ること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.sandbox import check_source
d = Path('docs/examples/plugins/ichimoku')
check_source(d / 'plugin.py')
check_source(d / 'test_plugin.py', extra_allowed=frozenset({'pytest', 'plugin'}))
print('check_source ok')"
```

- [ ] `discover` が通り `outputs` / `max_bars` が宣言どおりであること:

```
uv run python -c "
from pathlib import Path
from agentic_fx.plugin.loader import discover_one_with_reason
meta, reason = discover_one_with_reason(Path('docs/examples/plugins/ichimoku'), 'ichimoku')
print(meta.kind, list(meta.outputs), meta.max_bars, meta.params, reason)"
```

### Step 9-e: 逆変異 (**6 件 = 下限であって上限ではない**)

1 件ずつ `plugin.py` に適用 → `uv run pytest -q test_plugin.py --tb=no` → **元に戻す**。
`git checkout` は使わない (`cp plugin.py /tmp/...bak` で退避して戻す)。
**`FAILED` のテスト名を報告に貼り、下表と照合する。**

| # | 変異 | red になるべきテスト (指揮者の実測値) |
|---|---|---|
| M-ich-1 | senkou を未来へ shift (lookahead) | `test_matches_reference_implementation_on_every_row`, `test_prefix_consistency_on_every_row`, `test_valid_param_override_changes_the_result` |
| M-ich-2 | chikou を shift(-26) に (lookahead) | `test_chikou_is_the_current_close_not_shifted`, `test_matches_reference_implementation_on_every_row`, `test_prefix_consistency_on_every_row` |
| M-ich-3 | senkou_a を tenkan だけに | `test_matches_reference_implementation_on_every_row`, `test_spans_are_not_shifted_into_the_future`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-ich-4 | midpoint を平均に | `test_matches_reference_implementation_on_every_row` |
| M-ich-5 | senkou_b の既定 52 -> 26 | `test_matches_reference_implementation_on_every_row`, `test_spans_are_not_shifted_into_the_future`, `test_warmup_boundary_is_pinned_on_both_sides` |
| M-ich-6 | min_periods を外す (2 箇所とも) | `test_matches_reference_implementation_on_every_row`, `test_short_frame_returns_all_declared_keys_as_all_nan`, `test_spans_are_not_shifted_into_the_future` |

- [ ] 6 件すべてが KILLED になることを実測し、テスト名を報告に貼る
- [ ] 元のファイルに戻っていることを `diff` で確認する

### Step 9-f: 機械 diff と commit

- [ ] **プラン本文から抽出して `diff` を取る** (差分ゼロを報告に貼る):

```
PLAN=docs/superpowers/plans/2026-09-19-indicator-initial-set.md
uv run python - <<'EOF'
import re, pathlib, subprocess
pathlib.Path("tmp").mkdir(exist_ok=True)   # **`/tmp` 直下は使わない** (Global Constraints)
plan = pathlib.Path("docs/superpowers/plans/2026-09-19-indicator-initial-set.md").read_text()
# 見出し行 (行頭の "## T9: ") から次の "## " 見出しの直前までを節とする。
# **行頭アンカー (re.M) が要る** — この抽出スクリプト自身が節の中に
# 同じ文字列を含むため、素の split だと節が途中で切れる (実測)。
sec = re.search(r"^## T9: .*?(?=^## |\Z)", plan, re.S | re.M).group(0)
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
assert len(blocks) == 4, len(blocks)
# blocks[0]=config.yaml, blocks[1]=test_plugin.py, blocks[2]=stub, blocks[3]=plugin.py
for text, path in ((blocks[0], "config.yaml"), (blocks[1], "test_plugin.py"),
                   (blocks[3], "plugin.py")):
    want = pathlib.Path("tmp/expect_" + path)
    want.write_text(text)
    real = pathlib.Path("docs/examples/plugins/ichimoku") / path
    r = subprocess.run(["diff", str(want), str(real)], capture_output=True, text=True)
    print(path, "DIFF-ZERO" if r.returncode == 0 else "MISMATCH\n" + r.stdout)
EOF
```

- [ ] 各ファイルの絶対パスと `wc -l` を報告に書く
- [ ] `git add docs/examples/plugins/ichimoku && git commit`
      (メッセージ: `feat(indicator-initial-set): ichimoku indicator plugin (T9)`)
- [ ] **逸脱の申告** — 上の Step どおりに書けなかった箇所を「Step 番号 / 何を / なぜ」で全件

---

## T10: repo 側の受入テストと runbook

**依存**: T1〜T9 の全マージ後。1 レーンで直列実行する。
**触るファイル**: `tests/plugin/test_loader.py` (既存、1 assert に追記) /
`tests/plugin/test_indicator_initial_set.py` (新規) /
`docs/operations/indicator-initial-set-deploy-2026-09-19.md` (新規)。

> **着手前検証済み (v1.1、2026-09-19)**: v1.0 の T10 は「何を観測するか」の仕様までしか
> 書いておらず、**I6 / I7 / I8 の tmp 環境実測をしていなかった**。着手前検証で
> **Critical 4 / Important 8 / Minor 5** を検出し、全件を本節へ反映した
> (記録: `tmp/plan-indicator-initial-set/prevalidation-T10.md`)。
> **Step 10-i の完成ファイル (v1.1 時点 10 テスト = 10 passed / 41.7 秒、段 0 の是正で
> **12 テスト / 41.4 秒** に増えた。以下の記述は v1.1 当時の実測) は指揮者が実際に走らせて
> 確認したもの**。それでも**プロジェクトの制約に反する記述を見つけたら、プランどおりの
> 実装でも欠陥として申告**すること ([[plan-code-defects-not-implementer-defects]])。

### Step 10-a: I1 — `discover` の包含集合を広げる

- [ ] `rg -n 'test_discover_sample_plugins_directory_not_rejected' tests/plugin/test_loader.py`
      で現在の行番号を取得する (**本プランの行番号参照はドリフトしている前提で扱う**。
      着手前検証時点では `tests/plugin/test_loader.py:763-770`、広げる assert は **L770**)
- [ ] その関数の `assert {"rsi_indicator", "sma_cross"} <= names` を次の形に広げる:

```python
    assert {"rsi_indicator", "sma_cross", "sma", "ema", "rsi", "macd",
            "bollinger", "atr", "adx", "stochastic", "ichimoku"} <= names
```

- [ ] **総数 (`len(metas)`) は pin しない** — example が 1 本増えただけで落ちる脆いテストに
      なり、観測したい性質「9 本が reject されない」と一致しない (設計書 §6 I1 / r1 M1)
- [ ] 逆変異: どれか 1 本の `config.yaml` に未知キー (`timeframe: 1h`) を足すと**この**
      テストが red になることを実測する

### Step 10-b: I2 — validator を直接通す

`tests/plugin/test_indicator_initial_set.py` を新規作成する (**逐語は Step 10-i**)。
該当テストは `test_all_nine_pass_the_indicator_validator`。

- [ ] `docs/examples/plugins` を `discover` して **名前で** 9 本を取り出し
      (`_nine_metas()`)、各 `plugin.py` を `importlib` でロードして
      `compute(df, dict(meta.params))` の戻り値を
      `core.plugin_contract.validate_indicator_result(out, df_index=df.index,
      outputs=meta.outputs)` に通す (例外が出ないこと)
- [ ] **既存の `rsi_indicator` は `outputs` を宣言しているので混ざってよい**が、
      対象は「本束で足した 9 名」に限定して名前で選ぶこと (将来 example が増えても壊れない)
- [ ] **(着手前検証 C3 付随) `importlib` のモジュール名を 1 本ずつ変える**
      (`_iis_<名前>`)。9 本とも `plugin` という名前で `sys.modules` に入れると衝突し、
      **最後の 1 本の実装を 9 回検査するだけの恒真テスト**になる。I5 の
      `_last_row_deltas` も同じヘルパ (`_load_compute`) を通す
- [ ] **(着手前検証 M5) `docs/examples/plugins/` 配下で pytest を回さないこと。**
      `.gitignore` に入っているのは `__pycache__/` だけで **`.pytest_cache` は ignore されて
      いない** (`git check-ignore` で確認済み) ため、`tests/conftest.py:219-` の
      `_guard_repo_root_has_no_new_untracked_files` に当たる。`importlib` は
      `__pycache__` しか作らないので本テストは安全
- [ ] 逆変異: どれか 1 本の `compute` の戻り値から 1 キー落とすと red になること

### Step 10-c: I5 — 先頭依存の回帰 (**指揮者が実測済みのコードを転写する**)

fixture の生成式・値域・seed は設計書 §6 I5 の逐語仕様。**逐語コードは Step 10-i**
(`_spike_df` / `_degenerate_df` / `_last_row_deltas` と 3 本のテスト)。

- [ ] **ランダムウォーク 4 値域 (0.5 / 1.5 / 150 / 300) × seed 0〜7**: 末尾 400 本で
      計算した最終行と全 5000 本の最終行の差が、**0〜100 スケールの出力
      (`rsi` `k` `d` `adx` `plus_di` `minus_di`) は `abs < 1e-6`**、
      **価格スケールの出力は `abs < 1e-9 * 基準価格`**。
      指揮者の実測: **全キーの最大誤差 3.104e-10 / 違反 0** (0.45 秒)
- [ ] **スパイク fixture** (401 本目に基準価格の 60% = +90): 実測値は
      `ema` 0.0 / `macd` 2.56e-13 / `signal` 4.11e-13 / `hist` 1.55e-13 /
      `atr` 1.79e-12 / `rsi` 1.42e-10 / `adx` 3.40e-09 / `plus_di` 1.19e-10 /
      `minus_di` 2.73e-11 — 上界式 (設計書 §3.2 (i)) の内側。公差はランダムウォークと同じ
- [ ] **退化 fixture — 公差はランダムウォークの表ではなく設計書 §3.2 (i-b) の `1e-4` を
      全キーに適用する (着手前検証 C3)。** この fixture は `bollinger` の `upper` / `lower` に
      **1.08e-06** を出し、価格スケールの `1e-9 * 150` (= 1.5e-7) を**超える**。
      同じ公差表を退化 fixture にも当てると `bollinger` で必ず red になる。
      `1e-4` は全キーを覆う (最大は `adx` の **4.31e-06**)。
      指揮者の実測 (退化 fixture 601 行、全 19 キー):

```
value 1.7053e-13 / rsi 0 / macd 5.68434e-14 / signal 5.66504e-14 / hist 1.93055e-16 /
upper 1.08339e-06 / middle 0 / lower 1.08339e-06 / atr 6.03966e-14 / adx 4.31427e-06 /
plus_di 0 / minus_di 0 / k 0 / d 0 / tenkan 0 / kijun 0 / senkou_a 0 / senkou_b 0 / chikou 0
```

- [ ] そのうえで **`rsi` / `plus_di` / `minus_di` / `k` / `d` は `abs == 0.0` を個別に pin**
      する (ここが `EPS` 規則の唯一の観測点)。`adx` だけ `0.0 < abs < 1e-4`
- [ ] 逆変異: `rsi` / `adx` の `EPS` を 0 にすると**退化 fixture のテストだけ**が red に
      なることを実測する (ランダムウォーク / スパイクは緑のまま = 退化 fixture が唯一の観測点)

### Step 10-d: I6 — 9 本を順に bless する (tmp 環境)

該当テストは `test_nine_indicators_bless_in_sequence` (**逐語は Step 10-i**)。

- [ ] `tests/fixtures/wiring_envs.py:46-52` の `switch_env` と同じ流儀で `tmp_path/plugins` と
      tmp sqlite を用意する (`_bless_env`)。**実 `data/agentic.db` と実 `plugins/` に
      触れないこと** (Global Constraints)
- [ ] **(着手前検証 I-6) `.locks` は作らなくてよい** — `switch._plugin_lock`
      (`switch.py:806-807`) が `mkdir(parents=True, exist_ok=True)` する。
      `tests/plugin/test_switch_paths.py:46-47` の `env` は明示的に作っているが、
      どちらでも成立する (実測で `.locks/*.lock` が 9 本自動生成された)
- [ ] `docs/examples/plugins/<名前>` を `tmp_path/plugins/_human/<名前>` へ **`shutil.copytree`**
      でコピーする。**`__pycache__` / `.pytest_cache` を除外しない** — runbook の `cp -r` が
      巻き込んでも `check_candidate_snapshot` が無視する (**着手前検証 M2**: 実物は
      `gate_pytest.py:43` の `_IGNORED_DIR_NAMES` と `:46-55` の `_is_ignored_entry`。
      v1.0 の `gate_pytest.py:61-64` は誤り)。実測で両方入りのコピーが 9/9 通った
- [ ] 9 本を **1 本ずつ順に** `switch.bless_candidate(conn, name=..., human_dir=...,
      settings=..., now=..., decided_by=...)` に通し、**9 回とも `int` (approval_id) が
      返る**ことを assert する (シグネチャは `switch.py:1772-1777` と一致、実測 9/9)
- [ ] **pytest ゲートを double にしない。** ここはゲートを実物で回すことが受入そのもの。
      **`wiring_envs.install_gate_double` は strategy gate (実 backtest) 用で
      kind=indicator の bless には無関係**なので使わない。
      指揮者の実測: **9 本合計 11.7 秒** (1 本 1.22〜1.42 秒、`plugin.pytest_timeout_sec: 300`
      に対して桁で余裕)。Landlock 下でも `import pandas` と自己テストは落ちない
      (`gate_pytest` が `_SINGLE_THREAD_ENV` を子 env に入れるため —
      [[pytest-under-landlock-pitfalls]] の RLIMIT_AS × OpenBLAS は踏まない)
- [ ] **(着手前検証 I-1) `@pytest.mark.slow` を付ける。** `pyproject.toml:44` の
      `addopts = "-m 'not bench and not realbackend'"` は **slow を除外しない**ので、
      Step 10-h のフルスイートには残る (`pyproject.toml:42` の marker 定義が
      「既定で回る」と明記)。既存 `tests/plugin/test_indicator_wiring_e2e.py:21` も同じ形
- [ ] 同時に観測する: (i) `noop_gate.find_noop_copy` がこの経路に**無い**こと
      (`switch._run_full_gate` のゲート列に現れない — 設計書 §5.4/§5.5) — したがって
      「example の丸写し」で弾かれない (ii) `outputs_required` (`switch.py:970`) に掛からない
      (iii) `max_bars_limit` に掛からない (400 <= 1000)
- [ ] 9 本 bless 後に `tools.plugin_loader.approved_plugins(conn, tmp_path/"plugins",
      settings=...)` を呼び、**戻り値 `result` の `result.inventory.metas`** に 9 名が
      `outputs` 付き・`max_bars == 400` で並ぶことを assert する。
      **(着手前検証 I-2) `approved_plugins` は `InventoryBuildResult` を返す**
      (`tools/plugin_loader.py:41-59`) — それ自身に `.metas` は無い
- [ ] 逆変異: 1 本の `config.yaml` から `outputs:` 行を削ると、**その 1 本だけ**が
      `ValueError("outputs_required")` になり他の 8 本は配備されること (= 束ではない、
      設計書 §6.3)。指揮者の実測 (`atr` を削った場合):

```
blessed: ['sma', 'ema', 'rsi', 'macd', 'bollinger', 'adx', 'stochastic', 'ichimoku']
failed : [('atr', 'ValueError', 'outputs_required')]
inventory: ['adx', 'bollinger', 'ema', 'ichimoku', 'macd', 'rsi', 'sma', 'stochastic']
```

### Step 10-e: I7 — 新 `rsi` を宣言した strategy の E2E

該当テストは `test_strategy_declaring_new_rsi_runs_end_to_end` と
`test_strategy_with_max_bars_200_runs_but_is_outside_the_i5_guarantee`
(**逐語は Step 10-i**)。どちらも `@pytest.mark.slow`、実測 1.5 秒ずつ。

- [ ] tmp 環境に `rsi` を bless で配備したうえで、`rsi_pullback` 型の strategy を**別名**で作る
      (`config.yaml` に `indicators: {rsi: {plugin: rsi, params: {period: 14}}}`、
      **`max_bars: 400`**、`exit_mode: levels`、`timeframe` / `pairs` あり)
- [ ] **(着手前検証 C2) pin の書き込みは `agentic_fx.plugin.resolve.lock_config(candidate_dir,
      {alias: content_hash})` で行う** (`resolve.py:207-208`、戻り値
      `(before_text, after_text, new_content_hash)`)。人間 CLI の `afx plugin lock` と
      同じ関数で、YAML 書き換えの実装はここ 1 箇所に閉じている。
      **v1.0 が書いていた `lock_staging_deps` は使えない** —
      `tools/improve_staging_tools.py:116` の**内部クロージャ**で import できない。
      **`afx plugin lock --from _human` の CLI 自体も tmp では使えない**
      (init 済みリポジトリ root を要求する。実測 `rc=2`「初期化が完了していません」)
- [ ] `resolve_indicator_deps(meta, inventory, settings=..., pin_mode="require")` が通ること。
      **(着手前検証 I-3) 第 2 引数は `ApprovedInventory` (= `result.inventory`) で
      `InventoryBuildResult` ではない** (`resolve.py:161-162`)。
      未 pin だと `IndicatorResolutionError: indicator_unresolved:rsi:unpinned` (実測)
- [ ] 実 worker サブプロセスで **`PluginSession(meta, settings=SETTINGS.plugin,
      resolved=resolved)`** を起動し、`evaluate(df, indicators, None, params)` の
      `indicators["rsi"]["rsi"]` が **df と同じ index の系列**として届くことを assert
      (**モックで worker を潰さない** — [[test-fixtures-from-real-transcripts]])。
      **(着手前検証 C1) v1.0 の `PluginSession(kind="strategy", resolved=...)` は
      非実在シグネチャ**: 実物は `PluginSession(meta, *, settings: PluginSettings,
      resolved=None)` (`sandbox.py:330-331`) で **`kind=` は無く**、`settings` は
      `Settings` 全体ではなく **`Settings.plugin`**。既存の現物呼び出しは
      `tests/plugin/test_sandbox.py:944` ほか 6 箇所
- [ ] **`max_bars: 200` の保証外ケース**を別テストで: 同じ strategy を `max_bars: 200` で
      作ると**動く** (例外にならない) が、**I5 の保証範囲の外**であることを docstring に書く。
      値の一致は要求しない。**(着手前検証 I-4) 実物は
      `sub_df = df.tail(dep["max_bars"]).copy(deep=True)` (`worker.py:318`)** —
      indicator 自身の `max_bars` (= 400) で tail するが、strategy の `max_bars` が 200 なら
      df 自体が 200 本しかないので 200 本で計算される (`min(200, 400)` という式は実在しない)。
      その後 `worker.py:321-325` が `reindex(df.index)` を掛けるので、届く系列の長さは
      **strategy に渡した df と同じ**。指揮者の実測: 最終 `rsi` が
      `65.797529` (400 本) と `65.797530` (200 本) で食い違う

### Step 10-f: runbook を書く

`docs/operations/indicator-initial-set-deploy-2026-09-19.md` を新規作成する。

- [ ] **冒頭に**: 「**暫定**。[first-run-setup] (初回起動の対話ウィザード) が一括配備に
      置き換える。**恒久なのは『採用には人間の明示的確認が要る、LLM の自動配備経路は
      作らない』という規律のほう**」
- [ ] 手順 (0)〜(6) を逐語で:
  - **(0) 既存の配備名との衝突確認** — `ls -l plugins/` で `sma` / `ema` / `rsi` / `macd` /
    `bollinger` / `atr` / `adx` / `stochastic` / `ichimoku` が無いこと。既にあれば
    bless は**既存名の新版への切り替え**になり、その名前を pin している strategy が
    pin 破れで inventory から外れる (設計書 §8 D3)
  - **(1) コピー** — `cp -r docs/examples/plugins/<名前> plugins/_human/<名前>`
  - **(2) bless** — `afx plugin bless <名前> --from _human`
  - **(3) 結果の確認** — rc=0 なら stdout に `approval id=<N>`
    (`src/agentic_fx/backtest/cli.py:592`)。**rc=1 / traceback なら
    そこで停止**し、失敗が (A) ゲート前か (B) ゲート後かを判定して下の収束手順へ。
    **既に配備済の分は巻き戻さない** (設計書 §6.3)
  - **(4) 後片付け** — `rm -rf plugins/_human/<名前>` (bless は消さない。残すと後日の
    `afx plugin materialize <名前>` が `FileExistsError` になる)
  - **(5) 9 本ぶん繰り返す**
  - **(6) 最終確認** — service を再起動し、起動ログに 9 本が載ること
- [ ] **失敗の 2 系統 (設計書 §6.3) を逐語で転記する**:
  - **(A) ゲート前・ゲート中** (`check_source` / pytest / `max_bars` / `outputs_required`) —
    何も残らない。候補を直して同じ名前でやり直すだけ。
    **指揮者の実測** (5 本目の `test_plugin.py` をわざと落とした場合):
    `ValueError: plugin 'bollinger': test_plugin.py failed pytest gate (returncode=1)`、
    先行 4 本は配備済のまま、`.versions/bollinger` なし、未終端 journal なし、pending 0
  - **(B) ゲート後** (version 作成・history・symlink 切替) — journal / pending approval /
    `.versions/` が残り、**次の同名 bless は `UnresolvedJournalError` になる**。
    **この例外は `Exception` を直接継承していて (`switch.py:1572`) `ValueError` ではない**ため、
    CLI の `except (ValueError, SandboxError)` (`backtest/cli.py:590`) をすり抜け
    **Python traceback が出る** — 意味は「未終端 journal の検出」。
    **収束手順 5 ステップを設計書 §6.3 (B) から逐語で転記**
    (approval id は traceback のメッセージ `op_id=... approval_id=...` から読む /
    `afx> approval retry <id>` が `preparing` を終端させる唯一の手段 /
    **サービス再起動だけでは `preparing` は終端しない**)
  - **(着手前検証 C4) `approval retry <id>` の効き方は失敗した phase で変わる。**
    設計書 §6.3 (B) の手順 5 (「もう一度 bless して `UnresolvedJournalError` が出ない
    ことを確認する。そのまま成功すれば配備完了」) は**どちらでも正しく働く**ので、
    **手順 5 を必ず実行する形のまま**にする。指揮者の実測 (2026-09-19):

    | 失敗した phase | 症状 | `approval retry` 後 | 手順 5 の再 bless |
    |---|---|---|---|
    | `preparing` (版作成で失敗) | `.versions/` は増えていない | journal 終端 + **配備完了** | 確認になる (同内容なので切替は no-op) |
    | `versioned` / `recorded` (版はできた / history・切替直前) | **`.versions/<名前>/<hash>` が増えている** | journal 終端 + **配備完了** | 同上 |
    | `switched` (切替で失敗し live が新 target を指していない) | live が無い / 旧 target のまま | journal 終端・approval `approved` だが **配備されない** | **必須** |

    **`switched` の行は `src/` 側の観測事項**: `approve_candidate` の 0d は
    `phase == "switched"` を `_reverify_switched_journal` → `_finalize_decision` で
    閉じるだけで **`switch_live` を呼ばない** (`switch.py:1440-1454`) ため、
    「approval は approved なのに何も配備されていない」状態になり得る。
    **`[retry-switched-approves-without-deploy]` として指揮者へ申告済み** (本束では
    `src/` を直さない)。起動時 reconcile なら live target を見て巻き戻す
    (`test_switch_journal.py::test_switched_recovery_absent_old_kind_with_no_live_reverts`)
  - **`preparing` / `versioned` / `recorded` は再起動では終端しない**
    (`switch.py:180-183` が skip する) — ここが「`approval retry` が唯一の手段」の
    意味。**`switched` は再起動の reconcile が扱う**ので、この 2 文を混ぜないこと
- [ ] **strategy 作者向けの 1 行**: 「これらの indicator に依存する strategy は自分の
      `max_bars` を **400 以上**に宣言すること」(設計書 §3.2)
- [ ] **末尾に「任意の後片付け」節** (2026-09-19 ユーザー指示) — **実コードで確認した事実を
      反映すること**:
  - 新 `rsi` を配備したあと、**依存する strategy が無いことを確認したうえで**、配備済
    `rsi_indicator` / `rsi_wilder` を退役させてよい。**人間の判断であり本束の完了条件では
    ない。**
  - **ただし現時点で「symlink 配備された plugin を退役させる CLI 経路は存在しない」**
    (設計書 v1.3b §1 非スコープ、`[retire-symlink-deployed-plugin]` として起票)。
    **`afx plugin retire <名前>` は引数 1 個 (名前のみ)** だが、**「legacy plain live」
    — つまり `plugins/<名前>` が普通のディレクトリの場合にしか使えない**。bless で配備した
    ものは `.versions/` への symlink なので、`retire` は
    `ValueError("plugins/<名前> is not a plain directory (retire only applies to legacy
    plain live)")` で**拒否する** (`switch.py:1627-1629`)。未終端 journal があるときも
    `UnresolvedJournalError` で拒否する (`switch.py:1621-1625`)。
    **したがって `rsi_indicator` / `rsi_wilder` が bless / approve で配備されている場合、
    退役はできない — 新 `rsi` と併存させる**
  - **`retire` は依存 strategy を一切検査しない** (`switch.py:1605-1638` の `retire_plugin`
    全体に該当コードが無い — 着手前検証 I-8 で行番号を実物に合わせた)。
    退役させた indicator を pin している strategy は、次の `approved_plugins()` の第 2 相で
    `not_found` になり**黙って inventory から外れる** (pin 破れ)。**人間が事前に確認すること** —
    確認手段は対話シェルの `afx> approval <id>` の詳細に出る
    `dependent_pinned_here` / `dependent_pinned_elsewhere` の 2 欄 (`commands.py:394-400`)
  - **example 側の `docs/examples/plugins/rsi_indicator` は残す** — example 戦略
    `rsi_pullback` が依存しており、改善ループの `_examples` スナップショットと
    `tests/plugin/test_loader.py` / `tests/loops/test_improve_e2e.py` が参照している (R7)

### Step 10-g: I8 — runbook の逐語再現

該当テストは `test_runbook_normal_sequence_deploys_all_nine` /
`test_runbook_gate_failure_keeps_earlier_deployments_and_resumes` /
`test_runbook_post_gate_failure_converges_via_approval_retry` (**逐語は Step 10-i**)。
3 本とも `@pytest.mark.slow`、実測 11.5 / 12.0 / 2.7 秒。

- [ ] **(a) 正常系**: tmp 環境で runbook の手順 (1) コピー → (2) bless → (4) 後片付け →
      (5) 繰り返し、をそのまま実行し、9 本が配備され inventory に 9 本が現れること。
      コピーが `__pycache__` / `.pytest_cache` を巻き込んでも通ること (実測済み)
- [ ] **(b) ゲート前・ゲート中失敗からの再開 (§6.3 (A))**: 5 本目 (`bollinger`) の `_human`
      候補の `test_plugin.py` をわざと落ちるようにして bless を失敗させ、**4 本が配備済のまま
      残る**こと → 候補を直して 5 本目から再開すると**最終的に 9 本揃う**こと
- [ ] **(着手前検証 I-7) (b) の残骸 assert に「journal テーブルの行数が 0」を書かない。**
      失敗時点で `plugin_switch_journal` には**先行 4 本の終端済 journal が 4 行**残っている
      (指揮者の実測)。正しい観測点は 3 つ: `.versions/<失敗した名前>` が存在しない /
      `journal_store.get_open_by_name(conn, <名前>) is None` /
      `status='pending'` の approval が 0 件
- [ ] **(c) ゲート後失敗の収束 (§6.3 (B))**:
      **(着手前検証 C4) 故障注入は `switch.history_git.record_version` のモジュール属性
      差し替えで行う**
      (`monkeypatch.setattr(plugin_switch.history_git, "record_version", _fail_once)` —
      既存 `tests/plugin/test_reconcile.py:268,471` と同じ形)。`_advance_to_decided` は
      版作成 (`switch.py:1198`) → `phase="versioned"` (`:1205`) → history 記録 (`:1208`)
      → `phase="recorded"` (`:1212`) → symlink 切替 (`:1219`) の順に進むので、ここで
      落とすと **`.versions/<名前>/<hash>` は残り / journal は `versioned` / live symlink は
      無い**という §6.3 (B) の症状 (「`.versions/` が増えている」) がそのまま再現する。
      **`_advance_to_decided` 自体を差し替えるとこの位置は作れない** (v1.0 の記述)
- [ ] 観測の順序 (実測どおり):
      **`.versions/<名前>` + 未終端 journal (`phase == "versioned"`) + pending approval が
      残る** → **次の同名 bless が `UnresolvedJournalError`** (`ValueError` では**ない**ので
      CLI をすり抜ける。メッセージに `op_id=` と `approval_id=` が入る) →
      **`cmds.dispatch(f"approval retry {id}")`** で **journal の終端と配備の完了が同時に
      起きる** (手順を頭から冪等に流すため) → **手順 5 の再 bless** は
      「`UnresolvedJournalError` が出ないことの確認」として成功する (approval 行が 1 本
      増えるのは仕様)
- [ ] **retry の効き方は失敗 phase で変わる (Step 10-f の表)。`switched` の場合だけ
      retry では配備されず再 bless が必須**になる
      (`approve_candidate` の 0d が `switch_live` を呼ばない — `switch.py:1440-1454`、
      **`[retry-switched-approves-without-deploy]` として申告済み**)。
      **本テストは `versioned` の経路を観測する** (§6.3 (B) の症状に一致するため)
- [ ] **(着手前検証 I-5) 対話シェルの現物**: `agentic_fx.commands.Commands.dispatch(line)`
      (`commands.py:62`)、`approval retry <id>` 分岐は `commands.py:139-153`。
      `Commands` の組み方は `tests/fixtures/wiring_envs.py:193-222` の `shell_env(root)` が
      既存ビルダ。**`plugins_root` と `settings` の両方が必須** — どちらかが `None` だと
      `commands.py:143-144` が「未配線です」を返して**何もせず**、テストが黙って緑になる
- [ ] **状態遷移そのものは既存テストが pin 済み** — `tests/plugin/test_switch_journal.py`
      (`test_switched_recovery_*` / `test_interrupt_reverts_*`) と
      `tests/plugin/test_reconcile.py` を**参照**し、**再実装しない** (設計書 §6.1)。
      I8(c) が観測するのは「**runbook に書いた手順がそのまま通ること**」だけ。
      なお両ファイルには **bless 経路の故障注入は無い** (journal 行を直接作って reconcile を
      呼ぶ形) ので、注入点は上記の `switch_live` 差し替えを使うこと (着手前検証 C4)

### Step 10-h: フルスイートと commit

- [ ] `uv run pytest -q` をフルで回し、**既存テストの退行がゼロ**であること
      (特に `tests/plugin/test_loader.py` / `tests/loops/test_improve_loop_source_snapshot.py`
      / `tests/tools/test_improve_staging_tools.py` — `docs/examples/plugins` を列挙する側)
- [ ] 本ファイルは **12 テスト** で **実測 41.4 秒** (v1.1 時点は 10 テスト / 41.7 秒。
      段 0 で `test_all_nine_declare_max_bars_400` と
      `test_declared_params_match_each_plugins_own_defaults` の 2 本が増えた。どちらも
      `slow` ではなく 1 秒未満) (うち 9 本 bless を回す 3 本が 11.5 / 12.0 /
      11.5 秒)。`slow` は `addopts` で除外されないのでフルスイートに必ず含まれる
- [ ] `git status` で `data/` と `plugins/` に変更が無いことを確認する
- [ ] commit (`feat(indicator-initial-set): 受入テストと配備 runbook (T10)`)
- [ ] **逸脱の申告**を全件

### Step 10-i: `tests/plugin/test_indicator_initial_set.py` の逐語

**指揮者が実際に走らせて確認した完成ファイル** (着手前検証 2026-09-19 で 10 passed / 41.7 秒、
**段 0 の是正後 2026-09-19 で 12 passed / 41.4 秒**)。
`EXAMPLES` を scratchpad に向けた版で実測したので、**repo に置いたら
`EXAMPLES = _REPO / "docs" / "examples" / "plugins"` のままで走ること**を
必ず自分で確認すること。

```python
"""[indicator-initial-set] repo 側の受入テスト (I2 / I5 / I6 / I7 / I8)。

**実 DB (`data/agentic.db`) と実 `plugins/` には一切触れない** — 全て
`tmp_path` 配下に作る ([[tests-touching-real-repo-resources]])。
I1 は `tests/plugin/test_loader.py::test_discover_sample_plugins_directory_not_rejected`
が担保する (本ファイルには無い)。状態遷移そのものは
`tests/plugin/test_switch_journal.py` / `test_reconcile.py` が pin 済みなので
**再実装しない** — I8 が観測するのは「runbook に書いた手順がそのまま通ること」だけ
(設計書 §6.1)。
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import yaml

from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.plugin_contract import validate_indicator_result
from agentic_fx.entry import main
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.plugin.loader import discover, discover_one_with_reason
from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
from agentic_fx.plugin.sandbox import PluginSession
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store.state import StateStore
from agentic_fx.tools import plugin_loader
from tests.fixtures.wiring_envs import SETTINGS_FIXTURE as SETTINGS

_REPO = Path(__file__).resolve().parents[2]
EXAMPLES = _REPO / "docs" / "examples" / "plugins"

#: 本束で足した 9 名。**総数は pin しない** — 将来 example が増えても壊れない
#: よう、常に名前で選ぶ (設計書 §6 I1 / r1 M1)。
NINE = ("sma", "ema", "rsi", "macd", "bollinger", "atr", "adx",
        "stochastic", "ichimoku")

NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)

#: 0〜100 スケールの出力。残りは価格スケール (設計書 §6 I5)。
PCT_KEYS = frozenset({"rsi", "k", "d", "adx", "plus_di", "minus_di"})
PRICE_KEYS = frozenset({"value", "upper", "middle", "lower", "atr", "macd",
                        "signal", "hist", "tenkan", "kijun", "senkou_a",
                        "senkou_b", "chikou"})

#: I5 が観測する系列の完全な一覧 (`<plugin 名>.<出力キー>`)。**20 本ある** —
#: 出力キー名だけで束ねると `sma.value` と `ema.value` が衝突して
#: 片方 (dict の後勝ちで `sma`) が一度も観測されない。段 0 の実測では
#: `sma` の実装を `close.expanding(...).mean()` (先頭依存が最大の形) に
#: 差し替えても I5 の 3 テストが全て緑のまま通った。
DELTA_KEYS = frozenset(
    {f"{name}.{key}"
     for name, keys in (("sma", ("value",)), ("ema", ("value",)),
                        ("rsi", ("rsi",)),
                        ("macd", ("macd", "signal", "hist")),
                        ("bollinger", ("upper", "middle", "lower")),
                        ("atr", ("atr",)),
                        ("adx", ("adx", "plus_di", "minus_di")),
                        ("stochastic", ("k", "d")),
                        ("ichimoku", ("tenkan", "kijun", "senkou_a",
                                      "senkou_b", "chikou")))
     for key in keys})


def _tolerance(delta_key: str, base: float) -> float:
    """`<plugin 名>.<出力キー>` の出力キー側で値域を引く (設計書 §6 I5)。

    **どちらにも属さないキーは黙って価格スケール扱いにせず落とす**
    (/code-review #6)。`PRICE_KEYS` は定義だけされて誰も読んでいなかったので、
    新しい出力キーが足されたときに公差の分類漏れが `1e-9 * base` の
    フォールバックで静かに通ってしまう形だった。20 系列の全キーが 2 つの
    集合のどちらかに属することは `test_every_delta_key_is_classified` が
    独立に pin する。
    """
    key = delta_key.split(".", 1)[1]
    assert key in PCT_KEYS or key in PRICE_KEYS, delta_key
    return 1e-6 if key in PCT_KEYS else 1e-9 * base


def test_every_delta_key_is_classified():
    """I5 が観測する 20 系列の出力キーが、`PCT_KEYS` と `PRICE_KEYS` の
    **どちらか一方**に属すること (/code-review #6)。

    `_tolerance` の assert は「その呼び出しで使われたキー」しか見ないので、
    `DELTA_KEYS` 側だけを増やして分類を忘れた場合の観測点をここに置く。
    2 集合が交わらないことも同時に見る — 交わると `_tolerance` の分岐が
    `PCT_KEYS` 優先で静かに片方を選ぶ。
    """
    keys = {k.split(".", 1)[1] for k in DELTA_KEYS}
    assert not (PCT_KEYS & PRICE_KEYS), sorted(PCT_KEYS & PRICE_KEYS)
    assert keys <= (PCT_KEYS | PRICE_KEYS), sorted(keys - PCT_KEYS
                                                   - PRICE_KEYS)


# --- I5 の前提: 宣言 `max_bars` ---------------------------------------------

def test_all_nine_declare_max_bars_400(examples_copy):
    """9 本の `config.yaml` が `max_bars: 400` を宣言していること (設計書 §3.2 / D4)。

    **`_last_row_deltas` の結合だけでは足りない**ので独立に pin する。段 0 の実測:
    `ema` を `max_bars: 50` にすると結合した I5 が red になる
    (span=20 の EMA は 50 本では初期値の重みが `(19/21)**50` = 6.7e-03 残る) が、
    `sma` / `bollinger` / `stochastic` / `ichimoku` は窓が有限なので
    `tail(50)` と全長が最終行で厳密に一致し、**結合しても red にならない**。
    `test_nine_indicators_bless_in_sequence` も同じ値を見ているが、あちらは
    `slow` の bless 実走 (段 0 実測 41 秒) なのでここに 1 秒の観測点を置く。
    """
    for meta in _nine_metas(examples_copy):
        assert meta.max_bars == 400, (meta.name, meta.max_bars)

#: `__pycache__` / `.pytest_cache` は `check_candidate_snapshot` が無視する
#: (`gate_pytest.py:43,46-55`) ので**除外しない** — runbook の
#: `cp -r` がそのまま巻き込んでも通ることを I8(a) で観測する。

#: `examples_copy` の忠実性比較から外すディレクトリ名。`__pycache__` は
#: 過去の import が repo 側にだけ残していることがあり (`.gitignore` 済)、
#: `.pytest_cache` も同様に**コピー対象外**なので、比較の前に両側から落とす。
_COPY_IGNORED_DIRS = ("__pycache__", ".pytest_cache")


def _relative_file_hashes(root: Path) -> dict[str, str]:
    """`root` 配下の全ファイルを `{相対パス (posix): sha256}` で返す。

    `_COPY_IGNORED_DIRS` 配下は除外する。**ファイル名を列挙せず木を全走査
    する** — 「3 ファイルだけ一致」を見ると、将来 4 つ目のファイルを足した
    ときにコピー側に届いているかを誰も見なくなる。
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in _COPY_IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()).hexdigest()
    return out


@functools.lru_cache(maxsize=None)
def _nine_metas(root: Path) -> tuple:
    """`root` (= `examples_copy`) を discover して 9 名を名前で取り出す。

    **repo の `docs/examples/plugins` は読まない** (r2 codex C1)。metadata を
    repo から、`compute` をコピーから取ると**二重出所**になり、コピーが
    元と食い違っても (`ignore_patterns` を広げる / コピー元を変える)
    `co_filename` の「repo の外」判定だけでは何も落ちない。忠実性は
    `examples_copy` fixture が hash で観測し、出所の単一性はここで pin する。

    `lru_cache` は `discover` (ディレクトリ走査 + YAML 解析 + AST 検証 +
    ハッシュ) のモジュール中 ~46 回の再実行を 1 回に畳む (/code-review #1。
    指揮者の実測で非 slow 8 本が 1.13s -> 0.85s)。`root` は session スコープ
    fixture が返す不変の `Path`。**戻り値は tuple** — cache が同じ
    オブジェクトを配るので、呼び出し側が並べ替えられる list を渡さない。
    """
    metas = {m.name: m for m in discover(root) if m.name in NINE}
    assert sorted(metas) == sorted(NINE), sorted(metas)
    ordered = tuple(metas[n] for n in NINE)
    for meta in ordered:
        assert Path(meta.path).is_relative_to(root), (meta.name, meta.path)
        assert not Path(meta.path).is_relative_to(EXAMPLES), (meta.name,
                                                              meta.path)
    return ordered


def _load_module(name: str, plugin_py: Path):
    """`plugin.py` を **1 本ずつ別のモジュール名で** ロードする。

    9 本とも `plugin` という名前で `sys.modules` に入れると衝突し、
    最後の 1 本の実装を 9 回検査するだけの恒真テストになる。
    """
    spec = importlib.util.spec_from_file_location(f"_iis_{name}", plugin_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _load_compute(name: str, plugin_py: Path):
    """`_load_module` の `compute` だけを返す薄い包み。"""
    return _load_module(name, plugin_py).compute


def _typed(mapping) -> dict:
    """`{キー: (型名, 値)}`。**型のドリフトを潰さない**ための包み —
    `2 == 2.0` なので素の dict 比較では `bollinger` の `num_std` が
    `2.0` から `2` に落ちても気付けない (指揮者の実測: 変異 J は
    出力比較だけの版では緑のまま通った)。"""
    return {k: (type(v).__name__, v) for k, v in mapping.items()}


@pytest.fixture(scope="session")
def examples_copy(tmp_path_factory) -> Path:
    """`docs/examples/plugins` の **コピー** (session に 1 回)。

    I2 / I5 は `plugin.py` を `exec_module` で実際に import する。素の
    importlib は **ソースと同じディレクトリに `__pycache__` を書く** ので、
    repo の `docs/examples/plugins/<名前>/` を直接 import すると
    `tmp_path` の外の実資源を書き換える経路になる
    ([[tests-touching-real-repo-resources]])。bytecode を抑止する設定に
    頼るのではなく、**書き込み先が repo の外になる構造**を採る。
    `__pycache__` / `.pytest_cache` は複製しない (I8 の `_stage` は逆に
    **除外しない** — あちらは `cp -r` が巻き込んでも bless が通ることを
    観測するのが目的)。

    **コピーの忠実性をここで観測する (r2 codex C1)**。9 本それぞれについて
    「`_COPY_IGNORED_DIRS` を落とした後の相対パス集合とファイルごとの
    sha256」が repo 側と一致することを assert する。これが無いと、
    `ignore_patterns` を広げる / コピー元を取り違える類の変更で、I2 / I5 が
    **配備されるものとは別の artifact** を静かに検査する形になる
    (`ignore_patterns` に `*.yaml` を足す変異は、この assert を入れる前は
    受入テスト 8 本すべて緑のまま通った — 指揮者の実測)。assert を
    **fixture 本体に置く**ので、これを使う全テストが同時に fail closed になる。
    """
    dest = tmp_path_factory.mktemp("examples") / "plugins"
    shutil.copytree(EXAMPLES, dest,
                    ignore=shutil.ignore_patterns(*_COPY_IGNORED_DIRS))
    for name in NINE:
        want = _relative_file_hashes(EXAMPLES / name)
        got = _relative_file_hashes(dest / name)
        assert want, name          # 空同士の一致で通らないこと
        assert got == want, (name, sorted(set(want) ^ set(got)),
                             sorted(k for k in set(want) & set(got)
                                    if want[k] != got[k]))
    return dest


def test_loaded_plugins_never_come_from_the_repo_examples_directory(
        examples_copy):
    """I2 / I5 が実行する `compute` が **repo の外**から来ていること。

    この pin が無いと `_load_compute` の引数を `EXAMPLES / meta.name /
    "plugin.py"` に戻すだけで repo へ bytecode を書く形に静かに戻る
    (逆変異で実測)。

    **r2 codex C1 以後、`meta.path` 自体もコピー側を指す** (`_nine_metas` が
    `examples_copy` を discover する) ので、ここは「コピーが repo の外に
    ある」ことの pin であり、**コピーが元と同じ中身であること**は
    `examples_copy` fixture の hash 比較が、**metadata と実装の出所が
    1 つであること**は `_nine_metas` の 2 つの assert が担当する。3 つで
    1 組。
    """
    for meta in _nine_metas(examples_copy):
        compute = _load_compute(meta.name,
                                examples_copy / meta.name / "plugin.py")
        assert not Path(compute.__code__.co_filename).is_relative_to(
            EXAMPLES), (meta.name, compute.__code__.co_filename)


def _df(n: int, *, base: float = 150.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    return pd.DataFrame(
        {"open": np.concatenate([[close[0]], close[:-1]]),
         "high": close + sigma, "low": close - sigma, "close": close,
         "volume": np.ones(n)},
        index=pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"))


# --- I2: validator を直接通す ------------------------------------------------

def test_all_nine_pass_the_indicator_validator(examples_copy):
    """I2: 9 本の戻り値が `validate_indicator_result` を通る
    (キー集合完全一致 / index 一致 / Inf 不在)。

    **`docs/examples/plugins/` 配下で pytest を回さないこと** —
    `.pytest_cache` は `.gitignore` に無く、`tests/conftest.py` の
    untracked ガードに当たる。import も repo では行わない
    (`examples_copy` の注記)。
    """
    df = _df(300)
    for meta in _nine_metas(examples_copy):
        compute = _load_compute(meta.name,
                                examples_copy / meta.name / "plugin.py")
        out = compute(df, dict(meta.params))
        validate_indicator_result(out, df_index=df.index,
                                  outputs=meta.outputs)


def test_declared_params_match_each_plugins_own_defaults(examples_copy):
    """`config.yaml` の `params` が `plugin.py` の既定値と一致していること。

    **どちらのテストも片側しか見ていなかった**: 9 本の自己テストは
    `compute(df, {})` (= `plugin.py` の既定値) だけを、I2 は
    `compute(df, dict(meta.params))` (= `config.yaml` の宣言値) だけを通す。
    段 0 の実測で `sma` の `period: 20 -> 5`、`bollinger` の
    `num_std: 2.0 -> 3.0` がどちらも全テストを素通りした (M39 / M40)。
    宣言値は**実際に本番で使われる値**であり、ずれると「自己テストが緑の
    まま、配備された指標だけ別物」になる。

    値の表を手で持たずに**振る舞いで**比べる (どちらの向きのずれも捕まる)。

    **振る舞い比較だけでは足りない (r2 codex C2)**。出力に効かない差異
    (plugin が読まないキーが `config.yaml` に混じる / `2.0` が `2` に
    型落ちする) は両方の出力が同じなので緑のまま通る。そこで 2 段にした:

    1. **plugin 側が未知の params キーを拒否する**ようになったので
       (`_reject_unknown_params`)、`config.yaml` に余計なキーがあれば
       この下の `compute(df, dict(meta.params))` が `ValueError` で落ちる
       — 受入テスト側に「キー集合 ⊆ 既知キー」を書く必要がない
       (config に `unused: 1` を足す変異 K で実測)。
    2. **`_DEFAULTS` との辞書同値比較**を型込みで行う。値の表は
       `plugin.py` の `_DEFAULTS` に 1 箇所だけ置き、**受入テスト側に
       重複させない**。
    """
    df = _df(300)
    for meta in _nine_metas(examples_copy):
        mod = _load_module(meta.name,
                           examples_copy / meta.name / "plugin.py")
        assert set(mod._DEFAULTS) == set(mod._KNOWN_PARAMS), meta.name
        assert _typed(meta.params) == _typed(mod._DEFAULTS), (
            meta.name, _typed(meta.params), _typed(mod._DEFAULTS))
        compute = mod.compute
        declared = compute(df, dict(meta.params))
        builtin = compute(df, {})
        for key in meta.outputs:
            assert np.array_equal(
                declared[key].to_numpy(dtype="float64"),
                builtin[key].to_numpy(dtype="float64"),
                equal_nan=True), (meta.name, key, dict(meta.params))


# --- I5: 先頭依存の回帰 ------------------------------------------------------

def _spike_df(n=5000, base=150.0, spike=0.0, seed=1):
    """設計書 §6 I5 の fixture 生成式 (逐語)。`spike` は 401 本目 (= 400 本窓の
    ちょうど手前) に置く — `|Δseed|` を人為的に最大化する構成。"""
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if spike:
        close[n - 401] += spike
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2020-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": np.ones(n)}, index=index)


def _trend_df(n=5000, base=150.0, direction=1, noise=True, seed=2):
    """設計書 §6 I5 の fixture ②「明確な単調トレンド」の生成式 (逐語)。

    `drift = 基準価格 × 1e-4` / 本 — `n=5000` で基準価格の ±50% を動く
    (150 → 225 / 75)。**下降でも価格が 0 を跨がない**幅にしてある
    (跨ぐと `1e-9 × 基準価格` の絶対公差と `rsi` の相対 ε 基準
    (`<= 1e-9·|close|`) の意味がどちらも壊れる)。
    `noise=True` は drift + §6 I5 のランダムウォーク項、`noise=False` は
    **close が厳密に単調** (high / low の揺らぎだけ残す) — 後者は `rsi` を
    ε 規則②に突き当てる (上昇で 100.0、下降で 0.0 に張り付く)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    drift = base * 1e-4
    walk = np.cumsum(rng.normal(0.0, sigma, n)) if noise else np.zeros(n)
    close = base + direction * drift * np.arange(n) + walk
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2020-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": np.ones(n)}, index=index)


def _degenerate_df(n_pre=200, n_flat=400, base=150.0, spike=1.0, seed=0):
    """設計書 §3.2 (i-b) の反例: 通常データ -> DM/TR 1 本 -> 完全横ばい 400 本。"""
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = list(base + np.cumsum(rng.normal(0.0, sigma, n_pre)))
    high = [v + sigma / 2.0 for v in close]
    low = [v - sigma / 2.0 for v in close]
    top = close[-1] + spike
    close.append(top)
    high.append(top)
    low.append(close[-2])
    for _ in range(n_flat):
        close.append(top)
        high.append(top)
        low.append(top)
    open_ = [close[0]] + close[:-1]
    index = pd.date_range("2020-01-01", periods=len(close), freq="5min",
                          tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": [1.0] * len(close)},
                        index=index)


def _last_row_deltas(df: pd.DataFrame, examples_copy: Path) -> dict:
    """全 9 本について「**その plugin が宣言した `max_bars`** 本だけで計算した
    最終行」と「全 `len(df)` 本で計算した最終行」の差の絶対値を
    `{"<plugin 名>.<出力キー>": 値}` で返す。
    両方 NaN のキー (`ichimoku.chikou` 等) は 0.0 とみなす。

    **キーは plugin 名で修飾する** (`DELTA_KEYS` の注記) — 出力キー名だけだと
    `sma.value` が `ema.value` に上書きされて消える。

    **末尾の本数は `meta.max_bars` から取る** — 400 を引数既定値に固定すると、
    I5 が「宣言 `max_bars` の本数で保証が成り立つ」ではなく「400 本で
    成り立つ」しか観測せず、`config.yaml` の宣言を変えても何も red に
    ならない (段 0 の実測)。
    """
    out: dict[str, float] = {}
    for meta in _nine_metas(examples_copy):
        compute = _load_compute(meta.name,
                                examples_copy / meta.name / "plugin.py")
        tail = df.tail(meta.max_bars).copy(deep=True)
        full_res = compute(df, dict(meta.params))
        tail_res = compute(tail, dict(meta.params))
        for key, series in full_res.items():
            a = float(series.iloc[-1])
            b = float(tail_res[key].iloc[-1])
            out[f"{meta.name}.{key}"] = (
                0.0 if (np.isnan(a) and np.isnan(b)) else abs(a - b))
    assert set(out) == DELTA_KEYS, sorted(set(out))
    return out


def test_head_dependence_within_tolerance_on_random_walks(examples_copy):
    """I5 (ランダムウォーク): 4 値域 × seed 0〜7。
    0〜100 スケールは `< 1e-6`、価格スケールは `< 1e-9 * 基準価格`。
    指揮者の実測: 全キーの最大誤差 **3.104e-10 / 違反 0**。"""
    worst = 0.0
    for base in (0.5, 1.5, 150.0, 300.0):
        for seed in range(8):
            deltas = _last_row_deltas(_spike_df(base=base, seed=seed),
                                      examples_copy)
            for key, delta in deltas.items():
                tol = _tolerance(key, base)
                assert delta < tol, (base, seed, key, delta, tol)
                worst = max(worst, delta)
    assert worst < 1e-6, worst


def test_head_dependence_on_the_spike_fixture(examples_copy):
    """I5 (スパイク): 401 本目に基準価格の 60% (= +90) を置く。
    公差は上と同じ。指揮者の実測 (参考): `rsi` 1.42e-10 / `adx` 3.40e-09 /
    `macd` 2.56e-13 / `atr` 1.79e-12。"""
    base = 150.0
    deltas = _last_row_deltas(_spike_df(base=base, spike=base * 0.6, seed=1),
                              examples_copy)
    for key, delta in deltas.items():
        tol = _tolerance(key, base)
        assert delta < tol, (key, delta, tol)


def test_head_dependence_on_monotonic_trends(examples_copy):
    """I5 (単調トレンド): 設計書 §6 I5 の fixture ② — **ランダムウォークでは
    出ない「持続的な一方向の drift」での先頭依存**を見る。公差はランダム
    ウォークと同じキー別の表。

    4 象限 × 2 値域 = 8 系列: 上昇 / 下降 × `noise=True` (drift + 揺らぎ) /
    `noise=False` (close が厳密に単調) × 基準価格 1.5 / 150。

    指揮者の実測 (公差に対する最大比は `bollinger.upper` / `lower` の
    **0.4%**): `rsi` は `noise=True` で 1.01e-11、`noise=False` では
    **厳密に 0.0** (ε 規則②で 100.0 / 0.0 に張り付くため両側が一致する)。
    `adx` は 1.5e-11 〜 1.4e-10。**`noise=False` でも `adx` は飽和しない**
    (high / low の揺らぎが ±DM を両方立てるので、実測で +DI 25.4 / −DI 13.4
    / ADX 22.3、下降で +DI 18.4 / −DI 21.4 / ADX 26.4)。
    """
    for noise in (True, False):
        for direction in (1, -1):
            for base in (1.5, 150.0):
                deltas = _last_row_deltas(
                    _trend_df(base=base, direction=direction, noise=noise),
                    examples_copy)
                for key, delta in deltas.items():
                    tol = _tolerance(key, base)
                    assert delta < tol, (noise, direction, base, key,
                                         delta, tol)


def test_head_dependence_on_the_degenerate_fixture(examples_copy):
    """I5 (退化): 設計書 §3.2 (i-b) の反例。**公差はランダムウォークの表では
    なく §3.2 (i-b) の `1e-4` を全キーに適用する** — この fixture は
    `bollinger` の `upper` / `lower` に **1.08e-06** を出し、価格スケールの
    `1e-9 * base` (= 1.5e-7) を超える (指揮者の実測)。`1e-4` は全キーを覆う
    (最大は `adx` の **4.31e-06**)。

    比を取る指標 (`rsi` / `plus_di` / `minus_di`) と rolling の有限記憶
    (`k` / `d`) は**厳密に 0** になることを個別に pin する — ここが
    `EPS` 規則の唯一の観測点。"""
    deltas = _last_row_deltas(_degenerate_df(), examples_copy)
    for key, delta in deltas.items():
        assert delta < 1e-4, (key, delta)
    for key in ("rsi.rsi", "adx.plus_di", "adx.minus_di",
                "stochastic.k", "stochastic.d"):
        assert deltas[key] == 0.0, (key, deltas[key])
    assert 0.0 < deltas["adx.adx"] < 1e-4, deltas["adx.adx"]


# --- I6 / I8: bless の tmp 環境 ---------------------------------------------

def _bless_env(tmp_path: Path):
    """`(root, plugins_dir, conn)`。`tests/fixtures/wiring_envs.py:46-52` の
    `switch_env` と同じ流儀 (実 DB / 実 `plugins/` を触らない)。
    **`.locks` は作らなくてよい** — `switch._plugin_lock` (`switch.py:806-807`)
    が `mkdir(parents=True, exist_ok=True)` する
    (`tests/plugin/test_switch_paths.py:46-47` は明示的に作っているが、
    どちらでも成立する)。"""
    root = tmp_path
    plugins_dir = root / "plugins"
    (plugins_dir / "_human").mkdir(parents=True)
    (root / "logs").mkdir(exist_ok=True)
    conn = db_store.connect(root / "agentic.db")
    db_store.init_db(conn)
    return root, plugins_dir, conn


def _stage(plugins_dir: Path, name: str) -> Path:
    """runbook 手順 (1): `cp -r docs/examples/plugins/<名前> plugins/_human/<名前>`。
    **`__pycache__` / `.pytest_cache` を除外しない** — 巻き込んでも
    `check_candidate_snapshot` が無視する (`gate_pytest.py:43,46-55`)
    ことを実地で観測するため。"""
    dest = plugins_dir / "_human" / name
    shutil.copytree(EXAMPLES / name, dest)
    return dest


def _bless(conn, plugins_dir: Path, name: str) -> int:
    """runbook 手順 (2): `afx plugin bless <名前> --from _human` の中身
    (`backtest/cli.py:585-588` が呼ぶのと同じ引数)。"""
    return plugin_switch.bless_candidate(
        conn, name=name, human_dir=plugins_dir / "_human" / name,
        settings=SETTINGS, now=NOW, decided_by="human_cli")


def _inventory_names(conn, plugins_dir: Path) -> list[str]:
    """`approved_plugins` は **`InventoryBuildResult`** を返す
    (`tools/plugin_loader.py:41-59`) — 一覧は `result.inventory.metas`。"""
    result = plugin_loader.approved_plugins(conn, plugins_dir,
                                            settings=SETTINGS)
    return sorted(m.name for m in result.inventory.metas)


@pytest.mark.slow
def test_nine_indicators_bless_in_sequence(tmp_path):
    """I6: 9 本を 1 本ずつ順に bless でき、9 回とも `int` (approval_id) が返り、
    inventory に 9 名が `outputs` 付きで並ぶ。

    **pytest ゲートを double にしない** — ここはゲートを実物で回すことが
    受入そのもの。指揮者の実測で 9 本合計 **11.7 秒** (1 本 1.2〜1.4 秒、
    `plugin.pytest_timeout_sec: 300` に対して桁で余裕)、Landlock 下でも
    `import pandas` と自己テストは落ちない (`gate_pytest` が
    `_SINGLE_THREAD_ENV` を子 env に入れるため)。`slow` marker を付けるが、
    `pyproject.toml:44` の `addopts` は slow を除外しないのでフルスイートには残る。

    同時に観測していること: (i) `noop_gate.find_noop_copy` はこの経路に**無い**
    (`switch._run_full_gate` のゲート列に現れない) ので「example の丸写し」で
    弾かれない (ii) `outputs_required` に掛からない (iii) `max_bars_limit`
    に掛からない (400 <= 1000)。
    """
    _root, plugins_dir, conn = _bless_env(tmp_path)
    for name in NINE:
        _stage(plugins_dir, name)
        approval_id = _bless(conn, plugins_dir, name)
        assert isinstance(approval_id, int), (name, approval_id)
        assert (plugins_dir / name).is_symlink(), name

    result = plugin_loader.approved_plugins(conn, plugins_dir,
                                            settings=SETTINGS)
    by_name = {m.name: m for m in result.inventory.metas}
    assert sorted(by_name) == sorted(NINE)
    for name in NINE:
        assert by_name[name].kind == "indicator", name
        assert by_name[name].outputs, name
        assert by_name[name].max_bars == 400, name


# --- I7: 新 `rsi` を宣言した strategy の E2E ---------------------------------

_STRATEGY_PY = '''\
def evaluate(df, indicators, signals, params):
    rsi = indicators["rsi"]["rsi"]
    return {"action": "hold",
            "rationale": f"len={len(rsi)} nonnan={int(rsi.notna().sum())} "
                         f"last={float(rsi.iloc[-1]):.6f}"}
'''

_STRATEGY_TEST_PY = '''\
from plugin import evaluate


def test_evaluate_is_callable():
    assert callable(evaluate)
'''


def _write_strategy(base: Path, name: str, *, max_bars: int) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(_STRATEGY_PY)
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "strategy", "timeframe": "1h", "pairs": ["USDJPY"],
         "exit_mode": "levels", "max_bars": max_bars,
         "indicators": {"rsi": {"plugin": "rsi", "params": {"period": 14}}},
         "params": {}}, sort_keys=False))
    (d / "test_plugin.py").write_text(_STRATEGY_TEST_PY)
    return d


def _deploy_rsi_and_resolve(tmp_path: Path, *, max_bars: int):
    """新 `rsi` を bless で配備し、`max_bars` の strategy 候補に pin を書いて
    `pin_mode="require"` で解決する。戻り値 `(meta, resolved)`。

    pin の書き込みは **`plugin.resolve.lock_config`** で行う
    (`resolve.py:207-208`、人間 CLI の `afx plugin lock` と同じ関数)。
    `tools.improve_staging_tools` の `lock_staging_deps` は tooldef の
    **内部クロージャ**で import できず、`afx plugin lock` の CLI 自体は
    init 済みリポジトリ root を要求する (tmp では
    `初期化が完了していません` で rc=2)。
    """
    _root, plugins_dir, conn = _bless_env(tmp_path)
    _stage(plugins_dir, "rsi")
    _bless(conn, plugins_dir, "rsi")
    inventory = plugin_loader.approved_plugins(
        conn, plugins_dir, settings=SETTINGS).inventory
    rsi_meta = inventory.by_name("rsi")
    assert rsi_meta is not None and rsi_meta.max_bars == 400

    cand = _write_strategy(tmp_path / "cand", f"s{max_bars}",
                           max_bars=max_bars)
    lock_config(cand, {"rsi": rsi_meta.content_hash})
    meta, reason = discover_one_with_reason(cand, f"s{max_bars}")
    assert reason is None, reason
    # `resolve_indicator_deps` の第 2 引数は **`ApprovedInventory`**
    # (`InventoryBuildResult` ではない — `resolve.py:161-162`)。
    resolved = resolve_indicator_deps(meta, inventory, settings=SETTINGS,
                                      pin_mode="require")
    assert [(i.alias, i.plugin_name, i.pinned) for i in resolved.items] == [
        ("rsi", "rsi", True)]
    return meta, resolved


@pytest.mark.slow
def test_strategy_declaring_new_rsi_runs_end_to_end(tmp_path):
    """I7: 新 `rsi` を宣言した strategy を **実 worker サブプロセス**で回し、
    `indicators["rsi"]["rsi"]` が df と同じ index の系列として届く
    (**モックで worker を潰さない** — [[test-fixtures-from-real-transcripts]])。

    `PluginSession` のシグネチャは
    `PluginSession(meta, *, settings: PluginSettings, resolved=None)`
    (`sandbox.py:330-331`) — **`kind=` という引数は無く**、`settings` は
    `Settings` 全体ではなく **`Settings.plugin`**。
    """
    meta, resolved = _deploy_rsi_and_resolve(tmp_path, max_bars=400)
    df = _df(600).tail(400)
    with PluginSession(meta, settings=SETTINGS.plugin,
                       resolved=resolved) as session:
        out = session.call({"df": df, "params": {}})
    assert out["action"] == "hold"
    # worker は indicator 自身の `max_bars` で `df.tail(...)` したうえで
    # `reindex(df.index)` を掛ける (`worker.py:318,321-325`) ので、
    # strategy に渡した df と同じ長さで届く。
    assert f"len={len(df)}" in out["rationale"], out["rationale"]
    # warmup (period=14) の分だけ NaN が先頭に残る = 全 NaN でも全非 NaN でもない
    assert "nonnan=386" in out["rationale"], out["rationale"]


@pytest.mark.slow
def test_strategy_with_max_bars_200_runs_but_is_outside_the_i5_guarantee(
        tmp_path):
    """I7 (保証外ケース): 同じ strategy を `max_bars: 200` で作ると**動く**
    (例外にならない) が、**I5 の保証範囲の外**。

    worker は `sub_df = df.tail(dep["max_bars"])` (`worker.py:318`) と
    indicator 自身の `max_bars` (= 400) で tail するが、strategy の
    `max_bars` が 200 なら df 自体が 200 本しかないので 200 本で計算される。
    **値の一致は要求しない** — 指揮者の実測で最終 `rsi` は
    `65.797529` (400 本) と `65.797530` (200 本) で食い違う。
    """
    meta, resolved = _deploy_rsi_and_resolve(tmp_path, max_bars=200)
    df = _df(600).tail(200)
    with PluginSession(meta, settings=SETTINGS.plugin,
                       resolved=resolved) as session:
        out = session.call({"df": df, "params": {}})
    assert out["action"] == "hold"
    assert "len=200" in out["rationale"], out["rationale"]


# --- I8: runbook の逐語再現 --------------------------------------------------

@pytest.mark.slow
def test_runbook_normal_sequence_deploys_all_nine(tmp_path):
    """I8(a) 正常系: runbook の手順 (1) コピー → (2) bless → (4) 後片付け →
    (5) 9 本ぶん繰り返す、をそのまま実行する。コピーが `__pycache__` /
    `.pytest_cache` を巻き込んでも通る。"""
    _root, plugins_dir, conn = _bless_env(tmp_path)
    for name in NINE:
        _stage(plugins_dir, name)
        _bless(conn, plugins_dir, name)
        shutil.rmtree(plugins_dir / "_human" / name)   # 手順 (4)
    assert _inventory_names(conn, plugins_dir) == sorted(NINE)


@pytest.mark.slow
def test_runbook_gate_failure_keeps_earlier_deployments_and_resumes(tmp_path):
    """I8(b) ゲート前・ゲート中の失敗 (設計書 §6.3 (A)): 5 本目の候補の
    `test_plugin.py` をわざと落として bless を失敗させる。

    - **4 本は配備済のまま残る** (9 本は束ではない)
    - 失敗した 1 本には**何も残らない** — `.versions/<名前>` も、未終端 journal も、
      pending approval も。**「journal テーブルの行数が 0」を assert しては
      いけない** — 先行 4 本の**終端済** journal が 4 行残っている (指揮者の実測)
    - 候補を直して 5 本目から再開すると最終的に 9 本揃う
    """
    _root, plugins_dir, conn = _bless_env(tmp_path)
    broken = NINE[4]   # bollinger
    for name in NINE:
        _stage(plugins_dir, name)
        if name == broken:
            (plugins_dir / "_human" / name / "test_plugin.py").write_text(
                "def test_broken():\n    assert False\n")
            with pytest.raises(ValueError, match="failed pytest gate"):
                _bless(conn, plugins_dir, name)
            assert _inventory_names(conn, plugins_dir) == sorted(NINE[:4])
            assert not (plugins_dir / ".versions" / name).exists()
            assert journal_store.get_open_by_name(conn, name) is None
            assert conn.execute(
                "SELECT COUNT(*) c FROM approval_requests WHERE status='pending'"
            ).fetchone()["c"] == 0
            # 候補を直して同じ名前でやり直す (runbook (A))
            shutil.rmtree(plugins_dir / "_human" / name)
            _stage(plugins_dir, name)
        _bless(conn, plugins_dir, name)
        shutil.rmtree(plugins_dir / "_human" / name)
    assert _inventory_names(conn, plugins_dir) == sorted(NINE)


#: `_fail_record_version_once` が 1 回目に送出する文言。2 つのテストが
#: `match` / `in stderr` でこの文字列を見るので定数で持つ。
_INJECTED = "injected: after version dir, before symlink switch"


def _fail_record_version_once(monkeypatch) -> None:
    """`switch.history_git.record_version` を **1 回だけ** `OSError` にする。

    版ディレクトリ作成 (`switch.py:1198`) の**後**・symlink 切替
    (`:1219`) の**前**という §6.3 (B) の位置に故障を作るための注入で、
    差し替えはモジュール属性 (`test_reconcile.py:268,471` と同じ形)。
    `_advance_to_decided` 自体を差し替えるとこの位置は作れない。

    I8(c) と CLI (B) の 2 テストが逐語で同じブロックを持っていたので
    ここへ寄せた (/code-review #5)。**挙動は不変** — 2 回目以降は本物へ
    委譲するので、収束手順 (retry / 再 bless) はそのまま成功する。
    """
    real_record = plugin_switch.history_git.record_version
    calls = {"n": 0}

    def _fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(_INJECTED)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(plugin_switch.history_git, "record_version",
                        _fail_once)


@pytest.mark.slow
def test_runbook_post_gate_failure_converges_via_approval_retry(tmp_path,
                                                                monkeypatch):
    """I8(c) ゲート後の失敗 (設計書 §6.3 (B)): 版ディレクトリ作成後・symlink
    切替前で 1 回だけ失敗させ、runbook の収束手順 5 ステップが逐語で通ること
    を観測する。

    **故障注入は `switch.history_git.record_version` のモジュール属性差し替え**
    (`test_reconcile.py:268,471` と同じ形)。`_advance_to_decided` は
    版作成 (`switch.py:1198`) → `phase="versioned"` (`:1205`) →
    history 記録 (`:1208`) → `phase="recorded"` (`:1212`) → symlink 切替
    (`:1219`) の順に進むので、ここで落とすと **`.versions/<名前>/<hash>` は
    残り / journal は `versioned` / live symlink は無い**という §6.3 (B) の
    症状 (「`.versions/` が増えている」) がそのまま再現する。
    `_advance_to_decided` 自体を差し替えるとこの位置は作れない。

    **`approval retry` の効き方は失敗 phase で変わる (指揮者の実測、2026-09-19)**:

    | 失敗 phase | retry 後 | 再 bless |
    |---|---|---|
    | `preparing` (版作成で失敗) | journal 終端 + **配備完了** | 手順 5 の確認。同内容なので no-op |
    | `versioned` / `recorded` (history / 切替直前) | journal 終端 + **配備完了** | 同上 (本テストのケース) |
    | `switched` (切替で失敗し live が新 target でない) | journal 終端・approval `approved` だが **配備されない** (`approve_candidate` の 0d は `switched` を `_reverify_switched_journal` → `_finalize_decision` で閉じるだけで `switch_live` を呼ばない、`switch.py:1440-1454`) | **必須** |

    `switched` の行は **`[retry-switched-approves-without-deploy]` として指揮者へ
    申告済みの観測**であり、本テストの対象ではない (起動時 reconcile なら
    live target を見て `_revert_one` する — `test_switch_journal.py::
    test_switched_recovery_absent_old_kind_with_no_live_reverts`)。
    §6.3 (B) の手順 5 (「もう一度 bless して `UnresolvedJournalError` が出ない
    ことを確認する。そのまま成功すれば配備完了」) は **どちらの phase でも
    正しく働く**ので、runbook は手順 5 を必ず実行する形のままでよい。

    状態遷移そのものは `tests/plugin/test_switch_journal.py` /
    `test_reconcile.py` が pin 済みなので再実装しない (設計書 §6.1)。
    """
    root, plugins_dir, conn = _bless_env(tmp_path)
    _stage(plugins_dir, "rsi")

    _fail_record_version_once(monkeypatch)

    with pytest.raises(OSError, match="injected"):
        _bless(conn, plugins_dir, "rsi")

    # 残るもの: `.versions/` + 未終端 journal (phase=versioned) + pending approval。
    open_journal = journal_store.get_open_by_name(conn, "rsi")
    assert open_journal is not None
    assert open_journal["phase"] == "versioned"
    approval_id = open_journal["approval_id"]
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "pending"
    assert (plugins_dir / ".versions" / "rsi").is_dir()
    assert not (plugins_dir / "rsi").exists()
    assert _inventory_names(conn, plugins_dir) == []

    # 手順 2: 同じ bless をもう一度実行して `op_id` / `approval_id` を読む。
    # 例外は `UnresolvedJournalError` で **`ValueError` ではない**
    # (`switch.py:1572` は `Exception` を継承) ため、CLI の
    # `except (ValueError, SandboxError)` (`backtest/cli.py:590`) を
    # すり抜けて Python traceback が出る — runbook (B) の記述の根拠。
    with pytest.raises(plugin_switch.UnresolvedJournalError) as excinfo:
        _bless(conn, plugins_dir, "rsi")
    assert not isinstance(excinfo.value, ValueError)
    assert f"op_id={open_journal['op_id']}" in str(excinfo.value)
    assert f"approval_id={approval_id}" in str(excinfo.value)

    # 手順 3: 対話シェルの `afx> approval retry <id>`
    # (`commands.py:139-153`。`plugins_root` / `settings` が未配線だと
    # 何もせず文字列を返すだけなので、必ず両方渡す)。
    clock = FixedClock(NOW)
    cmds = Commands(
        conn=conn, state_store=StateStore(root / "state.json"),
        broker=PaperBroker(conn, SETTINGS, clock), trade_loop=MagicMock(),
        activity=ActivityLog(root / "logs" / "activity.log"),
        log_dir=root / "logs", clock=clock, health_latch=HealthLatch(),
        plugins_root=plugins_dir, settings=SETTINGS)
    assert cmds.dispatch(f"approval retry {approval_id}") == (
        f"approval #{approval_id} を再試行しました")

    # `versioned` からの retry は手順を頭から冪等に流すので、journal の終端と
    # **配備の完了**が同時に起きる。
    assert journal_store.get_open_by_name(conn, "rsi") is None
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "approved"
    assert (plugins_dir / "rsi").is_symlink()
    assert _inventory_names(conn, plugins_dir) == ["rsi"]

    # 手順 5: もう一度 bless して `UnresolvedJournalError` が出ないことを確認する。
    # 同内容なので切替は no-op だが、**approval 行は 1 本増える** (仕様)。
    second_id = _bless(conn, plugins_dir, "rsi")
    assert isinstance(second_id, int) and second_id != approval_id
    assert (plugins_dir / "rsi").is_symlink()
    assert _inventory_names(conn, plugins_dir) == ["rsi"]

    # 承認詳細に依存 strategy の 2 欄が出る (`commands.py:394-400`) —
    # runbook の「任意の後片付け」で人間が退役前に確認する手段。
    detail = cmds.dispatch(f"approval {second_id}")
    assert "dependent_pinned_here=" in detail
    assert "dependent_pinned_elsewhere=" in detail


# --- I8: runbook の CLI 境界 (`afx plugin bless <名前> --from _human`) --------

def _cli_env(tmp_path: Path) -> Path:
    """`afx` の CLI が要求する形の tmp root を作って返す。

    `backtest/cli.py:dispatch` は root を `Path.cwd()` として受け取り
    (`entry.py:23`)、**`ensure_initialized(root)`** (`service.py:75-79`、
    `data/state/app_state.json` の `initialized`) と
    **`config/settings.yaml`**、**`data/agentic.db`** を見る。上の
    `_bless_env` は DB を `root/agentic.db` に置くので**混ぜない** — CLI 用は
    このビルダで別に作る。

    `ensure_initialized` を monkeypatch で潰さない (`tests/backtest/test_cli.py`
    は潰している) — ここで観測したいのは「runbook の人間が打つコマンドが
    そのまま通ること」なので、初期化済みの状態も実物で用意する。
    `settings.yaml.example` は `SETTINGS_FIXTURE` の生成元そのもの
    (`tests/fixtures/wiring_envs.py:24`) なので `plugin.*` の値は上の
    テスト群と同じ。
    """
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / "config" / "settings.yaml.example",
                tmp_path / "config" / "settings.yaml")
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "plugins" / "_human").mkdir(parents=True, exist_ok=True)
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(
        initialized=True)
    conn = db_store.connect(tmp_path / "data" / "agentic.db")
    db_store.init_db(conn)
    conn.close()
    return tmp_path


@pytest.mark.slow
def test_runbook_cli_bless_succeeds_and_prints_approval_id(
        tmp_path, monkeypatch, capsys):
    """I8 (CLI 正常系): runbook 手順 (2) の
    `afx plugin bless <名前> --from _human` を **CLI の入口から**実行し、
    rc=0 と stdout の `approval id=<N>` を観測する。

    `entry.main` を **in-process** で呼ぶ (`tests/backtest/test_cli.py` の流儀)。
    subprocess にしないのは、(a) `main` が argparse → `dispatch` →
    `_plugin_bless` の CLI 境界そのものであり rc / stdout / stderr が
    同じであること、(b) 下の (B) が `record_version` の monkeypatch を
    同一プロセスに載せる必要があること、の 2 点による。
    `pyproject.toml:28` の `afx = "agentic_fx.entry:main"` が runbook の
    `afx ...` と同じ入口。
    """
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")

    rc = main(["plugin", "bless", "sma", "--from", "_human"])

    assert rc == 0
    out = capsys.readouterr().out
    assert re.search(r"^approval id=\d+$", out, re.M), out

    # **「symlink がある」だけでは足りない (r2 codex C3)** — 壊れた target を
    # 指していても通ってしまう。runbook の bless 成功は「live artifact が
    # 配備されたこと」なので、target の形 / 解決先の実在 / inventory から
    # metadata が引けることまで見る。target は相対 `.versions/<名前>/<hash>`
    # (`switch.py:1218-1219`)。
    live = root / "plugins" / "sma"
    assert live.is_symlink()
    target = live.readlink()
    assert target.parts[:2] == (".versions", "sma"), target
    assert len(target.parts) == 3, target
    assert re.fullmatch(r"[0-9a-f]{8,}", target.parts[2]), target
    resolved = (root / "plugins" / target).resolve()
    assert resolved.is_dir(), resolved
    assert (resolved / "plugin.py").is_file(), resolved
    assert (live / "config.yaml").is_file(), live

    conn = db_store.connect(root / "data" / "agentic.db")
    try:
        inv = plugin_loader.approved_plugins(
            conn, root / "plugins", settings=SETTINGS).inventory
    finally:
        conn.close()
    sma = inv.by_name("sma")
    assert sma is not None and sma.kind == "indicator", sma
    assert tuple(sma.outputs) == ("value",), sma.outputs


@pytest.mark.slow
def test_runbook_cli_bless_gate_failure_exits_1_with_error_on_stderr(
        tmp_path, monkeypatch, capsys):
    """I8 (CLI ゲート失敗、設計書 §6.3 (A)): 候補の `test_plugin.py` を壊すと
    `bless_candidate` が `ValueError` を投げ、`_plugin_bless` の
    `except (ValueError, SandboxError)` (`backtest/cli.py:591-593`) が
    **rc=1 + stderr の `エラー: `** に写像する。承認行も symlink も残らない。
    """
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    dest = root / "plugins" / "_human" / "sma"
    shutil.copytree(EXAMPLES / "sma", dest)
    (dest / "test_plugin.py").write_text(
        "def test_broken():\n    assert False\n")

    rc = main(["plugin", "bless", "sma", "--from", "_human"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "エラー: " in captured.err, captured.err
    assert "failed pytest gate" in captured.err, captured.err
    assert not (root / "plugins" / "sma").exists()
    conn = db_store.connect(root / "data" / "agentic.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM approval_requests").fetchone()["c"] == 0
    finally:
        conn.close()


@pytest.mark.slow
def test_runbook_cli_bless_after_post_gate_failure_raises_traceback(
        tmp_path, monkeypatch, capsys):
    """I8 (CLI ゲート後失敗、設計書 §6.3 (B)): 未終端 journal が残った状態で
    同名を再 bless すると、CLI から **`UnresolvedJournalError` が素通りして
    traceback になる** ([[cli-bless-unresolved-journal]])。

    **これは「現状をそのまま pin する」テストであり、望ましい姿ではない** —
    `UnresolvedJournalError` は `Exception` 直下 (`switch.py:1572`) なので
    `_plugin_bless` の `except (ValueError, SandboxError)` にも
    `dispatch` の `except (ValueError, KeyError, OSError, sqlite3.Error, ...)`
    にも掛からない。ticket [cli-bless-unresolved-journal] が直ったら
    (rc=1 + 収束手順の案内を stderr に出す形になるはず)、**このテストは
    その新しい振る舞いへ書き換える**こと。

    1 回目の失敗注入 (`record_version` の `OSError`) は逆に
    `dispatch` の `except OSError` に**捕まる**ので rc=1 になる — 同じ
    §6.3 (B) でも CLI から見える形が 1 回目と 2 回目で違うことを両方観測する。
    """
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")

    _fail_record_version_once(monkeypatch)

    rc = main(["plugin", "bless", "sma", "--from", "_human"])
    assert rc == 1
    assert "injected" in capsys.readouterr().err

    conn = db_store.connect(root / "data" / "agentic.db")
    try:
        open_journal = journal_store.get_open_by_name(conn, "sma")
        assert open_journal is not None and open_journal["phase"] == "versioned"
    finally:
        conn.close()

    with pytest.raises(plugin_switch.UnresolvedJournalError) as excinfo:
        main(["plugin", "bless", "sma", "--from", "_human"])
    assert f"op_id={open_journal['op_id']}" in str(excinfo.value)
```

**転写後の自己検証** (プラン規約):

```
python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" tests/plugin/test_indicator_initial_set.py
```

**機械抽出による照合** (プラン本文との差分ゼロを報告に貼ること):

```
sec = plan.split("### Step 10-i:")[1].split("\n**転写後の自己検証**")[0]
body = re.findall(r"^```python\n(.*?)^```$", sec, re.S | re.M)[0]
assert body == open("tests/plugin/test_indicator_initial_set.py").read()
```

---

## レビュー段

1. **段 0 (指揮者の変異スイープ)** — レビュー前に必ず回す。**本プランの T1〜T9 の
   Step e に書いた 54 件は指揮者が実測済み (54/54 KILLED)** なので、段 0 では
   **実装者の報告と指揮者の再実測を照合する** ([[haiku-silently-adapts-report-deviations]]
   — 報告は証拠にならない)。加えて T10 の逆変異 3 件を回す。
   **変異の適用と復元は必ず対で行い、復元漏れが後続の測定を汚さないようにする** —
   指揮者の scratchpad 検証でも 1 度これを踏んだ (「着手前検証の記録」§6)。
2. **1 周目** = codex + ローカル LLM 3 本 (並列)。
3. **2 周目** = `/code-review high` + codex + ローカル 3 本 (有償 2 本は並列にしない)。
4. **3 周目** = 必要に応じてブリーフ付き sonnet。

**レビュアーへの依頼文に必ず入れる**: 「**プロジェクトの制約に反するなら、プラン記述
どおりの実装でも欠陥として挙げてほしい**」([[plan-code-defects-not-implementer-defects]])。

## 完了条件 (束全体)

- [ ] `docs/examples/plugins/` に 9 本 × 3 ファイル = 27 ファイルが存在する
- [ ] 9 本の自己テストが **合計 131 passed** (指揮者の実測値。増える方向は可)
- [ ] `discover(docs/examples/plugins)` の結果に **9 名すべてが含まれる** (既存 3 + 新 9 = 12 本。
      **総数は pin しない** — I1 の方針と同じ。ここは束の完了時に 1 回見るだけの確認)
- [ ] 受入 I1〜I9 の 9 件すべてに緑のテストが対応している (対応表の抜けゼロ)
- [ ] 逆変異 54 件 + T10 の 3 件が KILLED
- [ ] `docs/operations/indicator-initial-set-deploy-2026-09-19.md` が存在し、I8 が通る
- [ ] `uv run pytest -q` フルスイートで退行ゼロ
- [ ] `git status` に `data/` / `plugins/` / `src/` の変更が無い
- [ ] fresh worktree でフルスイートを 1 回 (残骸ゼロ)

## 着手前検証の記録 (指揮者が scratchpad で実測、2026-09-19)

本プランの T1〜T9 の逐語コード (27 ファイル) は、**プランに載せる前に scratchpad で
実際に生成して動かし、green を確認したもの**。実測の内訳:

### 1. 静的ゲート

| 検査 | 結果 |
|---|---|
| `check_source(plugin.py)` × 9 | **9/9 PASS** |
| `check_source(test_plugin.py, extra_allowed={"pytest","plugin"})` × 9 | **9/9 PASS** |
| `loader.discover(<9 本のディレクトリ>)` | **9 本すべて admit**。`kind=indicator` / `max_bars=400` / `outputs` 宣言あり |
| `core.plugin_contract.validate_indicator_result` × 9 | **9/9 PASS** (キー集合・index・Inf/bool) |

### 2. 自己テスト

| plugin | passed | plugin | passed | plugin | passed |
|---|---|---|---|---|---|
| `sma` | 13 | `macd` | 16 | `adx` | 16 |
| `ema` | 12 | `bollinger` | 17 | `stochastic` | 15 |
| `rsi` | 16 | `atr` | 12 | `ichimoku` | 14 |

**合計 131 passed / 9 本合計 3.3 秒** (`pytest_timeout_sec: 300` に対して桁で余裕)。
うち **I4 の全 160 行の接頭辞検査**が 9 本合計 0.53 秒 (最悪 `adx` 0.224 秒)。

### 3. I5 (先頭依存) の実測

- ランダムウォーク **4 値域 (0.5 / 1.5 / 150 / 300) × seed 0〜7**、全 9 本の全出力キー:
  **最大誤差 3.104e-10 / 公差違反 0**。
- **401 本目に +90 のスパイク**: `ema` 0.0 / `macd` 2.56e-13 / `signal` 4.11e-13 /
  `hist` 1.55e-13 / `atr` 1.79e-12 / `rsi` 1.42e-10 / `adx` 3.40e-09 /
  `plus_di` 1.19e-10 / `minus_di` 2.73e-11。
- **退化 fixture** (200 本 → DM/TR 1 本 → 完全横ばい 400 本): `rsi` **0.0** /
  `adx` **4.31e-06** / `plus_di` 0.0 / `minus_di` 0.0 / `stochastic` 0.0。
  **`bollinger` の `upper` / `lower` は 1.08e-06** — 価格スケールの公差
  `1e-9 * 150` を超えるので、退化 fixture には設計書 §3.2 (i-b) の `1e-4` を全キーに
  適用する (T10 着手前検証 C3、2026-09-19 追記)。

### 4. 壊れた実装に対する red の実測 (`sma` の `test_plugin.py` を流用)

| 壊れた実装 | red になったテスト |
|---|---|
| **行 37 だけ**未来を読む (`value.iloc[37] = close.iloc[38]`) | `test_prefix_consistency_on_every_row` / `test_matches_reference_implementation_on_every_row` |
| 入力 df を添字代入で壊す (`df["close"] = close * 2.0`) | `test_input_frame_is_not_mutated` / `test_repeated_calls_are_deterministic` / 他 2 本 |
| module レベルの状態を持ち越す (`_SEEN.append(...)`) | `test_repeated_calls_are_deterministic` / 他 2 本 |
| `close.shift(-1)` を挟む | `test_prefix_consistency_on_every_row` / 他 2 本 |

**「行 37 だけ未来を読む」は 8 点サンプル検査では緑になる** (設計書 §6.2) — 全行検査が
必要な理由の実測。

### 5. 逆変異スイープ

**54 件 / 54 KILLED / 生存 0。** 初回は **9 件が生存**し、そのすべてが**テスト側の欠陥**
だった。修正内容 (T0-3 / T0-4 に反映済み):

| 生存した変異 | 原因 | 打った手 |
|---|---|---|
| `sma`: `close` → `open` | fixture が `open == close` だった | `_mkdf` の `open` を**前バーの終値**に変更 |
| `sma`/`ema`: params 検査を削る | pandas 自身の `ValueError` でテストが緑になっていた | `pytest.raises(..., match=r"^params\.")` で**文言まで pin** |
| `rsi`: `EPS` → 0 | 完全横ばい fixture では厳密 0 判定でも 50 になる | **減衰した横ばい** fixture (`_decayed_flat_df`) を追加 |
| `adx`: ε 規則を厳密 0 判定に戻す (2 件) | 同上 | 同上 |
| `adx`: `±DM` の行 0 マスクを削る | fixture の bar 1 が平坦で seed 差が出なかった | `_mkdf` の **bar 1 に決定論的な上げ**を置く |
| `ichimoku`: `min_periods` を外す | **変異定義の欠陥** (2 箇所あるのに 1 箇所しか置換していなかった) | 変異定義を修正 (両方置換) |

**等価変異 1 件 (正直に記録する)**: `rsi` / `adx` の `EPS * close.abs()` を
`EPS * close.shift(1).abs()` に変える変異は、**隣接バーの価格比が 1 に近い限り等価**で
自己テストでは殺せない。これは規約 (設計書 §3.1 の「`|close|` はその行自身の close」) と
docstring・参照実装の一致で担保する ([[mutation-testing]] の「リストは下限」)。

### 6. 検証手順そのものの落とし穴 (実測)

変異スイープの出力を `| grep` に通したところ、**SIGPIPE でスイープが途中終了し、変異を
適用したままのファイルが残って後続の測定を汚した** (`atr` の自己テストが一度 red になった)。
**変異の適用と復元は対で、パイプで打ち切られない形で回すこと。** 本プランの Step e は
「1 件ずつ適用 → 実測 → 必ず戻す → `diff` で確認」の順を明示している。

### 7. 機械抽出による再検証 (転写ずれの検出)

本プランを書き終えたあと、**プラン本文の ` ```python ` / ` ```yaml ` フェンスから 27 ファイルを
機械抽出して別ディレクトリに再展開し、§1〜§5 と同じ検証を全部もう一度回した**。抽出スクリプト
(各 task の Step f に載せたものと同じ正規表現):

```
sec = plan.split(f"## T{i}: `{name}` —")[1].split("\n---\n")[0]
blocks = re.findall(r"^```(?:python|yaml)\n(.*?)^```$", sec, re.S | re.M)
# blocks[0]=config.yaml  blocks[1]=test_plugin.py  blocks[2]=stub  blocks[3]=plugin.py
```

結果:

| 再検証 | 結果 |
|---|---|
| 生成元 (source of truth) との `diff -r` | **差分ゼロ (27 ファイルすべて一致)** |
| `check_source` 18 件 | **18/18 PASS** |
| `discover` | **9 本すべて admit** |
| `validate_indicator_result` 9 件 | **9/9 PASS** |
| 自己テスト | **131 passed / 3.3 秒** (§2 と同値) |
| 逆変異 54 件 | **54/54 KILLED** (§5 と同値) |
| I5 / 退化 fixture / 壊れた実装 4 種 | **§3 / §4 と数値まで同一** (最大誤差 3.104e-10、`adx` 退化 4.314e-06) |

**初回の抽出では 1 件だけ差分が出た** — `config.yaml` の末尾に空行が 1 行余分にあり、プラン側の
`.rstrip()` で落ちていた。生成元を直して再生成・再抽出し、差分ゼロにしてからこの記録を書いた。
**「プランに載せた文字列」と「検証した文字列」が同一であることを、目視ではなく `diff` で
確かめてある** ([[transcription-must-be-machine-diffed]])。

### 8. 設計書との食い違い (**v1.1 で解消済み** — 設計書 v1.3b へ反映)

| # | 箇所 | 実測 | 処置 (2026-09-19) |
|---|---|---|---|
| 1 | 設計書 §3.2 (i-b) の退化 fixture の `|Δadx|` = **5.2e-06** | 本プランの fixture 生成式では **4.31e-06** | **設計書 v1.3b で 4.31e-06 に差し替え**、「前段データの作り方に依存する観測値で、I5 の公差 1e-4 はこれを覆う」と注記。どちらも結論は不変 |
| 2 | 設計書 §6.3 は `afx plugin retire` に触れていない | `retire` は **plain live 専用**で symlink 配備 (= bless の結果) は**拒否**し、**依存 strategy を検査しない** | **設計書 v1.3b の R5a 近傍に 1〜2 文 + §1 非スコープに `[retire-symlink-deployed-plugin]` の起票文案**。Step 10-f の「任意の後片付け」も「退役できない/併存させる」に修正 |

### 9. T10 の着手前検証 (2026-09-19、**v1.1 の根拠**)

v1.0 の T10 は「何を観測するか」の仕様までしか書いておらず、**I6 / I7 / I8 の tmp 環境
実測をしていなかった**。着手前検証で **Critical 4 / Important 8 / Minor 5** を検出し、
全件を本節へ反映した (詳細: `tmp/plan-indicator-initial-set/prevalidation-T10.md`)。

**Critical 4 件**:

| # | v1.0 の記述 | 実物 |
|---|---|---|
| C1 | `PluginSession(kind="strategy", resolved=...)` | `PluginSession(meta, *, settings: PluginSettings, resolved=None)` (`sandbox.py:330-331`)。`kind=` は非実在、`settings` は `Settings.plugin` |
| C2 | 「`lock_staging_deps` 相当 / `afx plugin lock --from _human`」 | `lock_staging_deps` は tooldef の内部クロージャで import 不能 (`improve_staging_tools.py:116`)。CLI は init 済み root を要求し tmp では rc=2。正は `plugin.resolve.lock_config` (`resolve.py:207-208`) |
| C3 | I5 のテスト本体が無い + 退化 fixture の公差が未指定 | 退化 fixture で `bollinger` が **1.08e-06** を出し価格スケール公差 (1.5e-7) を超える。設計書 §3.2 (i-b) の `1e-4` を全キーに当てるのが正 |
| C4 | 「`_advance_to_decided` の途中を monkeypatch」「既存テストの monkeypatch に倣う」 | `_advance_to_decided` 自体を差し替えるとこの位置は作れない。正は `switch.history_git.record_version` のモジュール属性差し替え (phase=`versioned` = §6.3 (B) の症状)。加えて **`approval retry` の効き方は失敗 phase で変わる** — `preparing`/`versioned`/`recorded` は retry で配備まで完了し、`switched` だけ retry 後も未配備のまま `approved` になる (`[retry-switched-approves-without-deploy]` として申告) |

**Important 8 件**: I6 に `slow` marker の指示が無い (実測 9 本 11.7 秒) /
`approved_plugins` の戻り値は `InventoryBuildResult` /
`resolve_indicator_deps` の第 2 引数は `ApprovedInventory` /
`worker.py:318` に `min(200, 400)` という式は無い /
対話シェルの現物名 (`Commands.dispatch`) が書かれていない /
`.locks` の扱いが既存 2 流儀で割れている (どちらでも成立する) /
I8(b) の残骸 assert に「journal 0 行」を書くと red /
`retire_plugin` の行番号が 3 行ずれ (`1605-1641` → `1605-1638`)。

**Minor 5 件**: `_plugin_bless` は `agentic_fx.cli` ではなく `backtest/cli.py:557` /
`gate_pytest.py:61-64` → `:43,46-55` / `commands.py:395-400` → `:394-400` /
`.pytest_cache` は gitignore されていない / `approval id=<N>` の出力位置 (`cli.py:592`、変更不要)。

**T10 の実測** (全て tmp 環境。実 DB・実 `plugins/` には触れていない):

| 観測 | 結果 |
|---|---|
| I6: 9 本を実 pytest ゲート込みで順に bless | **9/9 成功、`int` 返り、合計 11.66 秒** (1 本 1.22〜1.42 秒)。Landlock 下で pandas import も自己テストも落ちない |
| I6 逆変異: `atr` の `outputs:` を削る | **その 1 本だけ `ValueError("outputs_required")`、他 8 本は配備** (束ではない) |
| I7: 新 `rsi` + strategy を実 worker で | `max_bars: 400` → `len=400 nonnan=386 last=65.797529` / `max_bars: 200` → `len=200 nonnan=186 last=65.797530` (**一致しない = 保証外**) |
| I8(a) 正常系 (`__pycache__` / `.pytest_cache` 込みコピー) | 9 本配備、inventory 9 名 |
| I8(b) 5 本目のゲート失敗 → 直して再開 | 失敗時点で 4 本配備済・残骸ゼロ、再開後 9 本揃う |
| I8(c) ゲート後失敗 → `approval retry` → 手順 5 の再 bless | 失敗 phase 別に実測: `preparing` / `versioned` は **retry で journal 終端 + 配備完了**、`switched` だけ **retry 後も未配備** (approval は `approved`)。いずれも次の同名 bless は `UnresolvedJournalError` で弾かれ、retry 後の再 bless で確実に配備される。受入テストは `versioned` を観測する |
| **`src/` の観測事項 (本束では直さない)** | `approve_candidate` の 0d は `phase == "switched"` を `_reverify_switched_journal` → `_finalize_decision` で閉じるだけで `switch_live` を呼ばない (`switch.py:1440-1454`) ため、**approval が `approved` なのに何も配備されていない**状態になり得る。**`[retry-switched-approves-without-deploy]` として指揮者へ申告** |
| 完成ファイル `tests/plugin/test_indicator_initial_set.py` (Step 10-i) | v1.1 時点 **10 passed / 41.7 秒** (最遅 3 本 = 12.0 / 11.5 / 11.5 秒)。**段 0 の是正後 v1.2 = 12 passed / 41.4 秒**。**1 周目の是正後 v1.3 = 17 passed / 45.6 秒** (X4 の `examples_copy` pin 1 本 + X2 の単調トレンド 1 本 + X3 の CLI 3 本) |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-19 | v1.0 | 起案。設計書 v1.3a を T0 (共通テンプレート) / T1〜T9 (指標 1 本ずつ、並列可) / T10 (repo 側受入テストと runbook) の 11 task へ分割。**T1〜T9 の逐語コード 27 ファイルは指揮者が scratchpad で生成・実行し 131 passed を確認済み**。逆変異 54 件を実測し 54/54 KILLED (初回 9 件生存 → すべてテスト側の欠陥として修正)。I5 の数値・壊れた実装の red・等価変異 1 件を「着手前検証の記録」に記載 | 設計書 v1.3a (codex 設計レビュー r3 で指摘 0、収束) | - |
| 2026-09-19 | v1.1 | **T10 の着手前検証** (Critical 4 / Important 8 / Minor 5) を全件反映。Step 10-b〜10-h を実物照合済みの記述へ改訂し、**Step 10-i に完成ファイル `tests/plugin/test_indicator_initial_set.py` の逐語 (実測 10 passed / 41.7 秒) を追加**。「着手前検証の記録 §8」の 2 件を設計書 v1.3b へ反映して閉じ、§9 に T10 の実測を追加。**T1〜T9 の節と行番号は一切動かしていない** (改訂は L4438 以降 + 冒頭 1 行の版表記のみ)。なお本文 L24 / L73 の「設計書 v1.3a」表記は、T10 より前の行を動かさない制約のため据え置き — **v1.3b は v1.3a に対する設計変更ゼロの改訂** (観測値の訂正 + 既知の欠落の起票) なので参照の妥当性は保たれる | T10 は起草者自身が「tmp 環境での bless 実測をしていない、最もプラン記述の欠陥を踏みやすい」と申告していた箇所 ([[plan-code-defects-not-implementer-defects]] 「着手前検証を必須工程にする」) | - |
| 2026-09-19 | v1.3 | **1 周目レビュー (codex terra 2 束、Critical 0 / Important 4) の是正を反映。4 件とも採用。** ①**X1 (束 1)**: 9 本のモジュール docstring の項目 3 が `max_bars: 400` の根拠を「再帰平滑の初期値依存を 1e-6 未満に抑えるため」と全 9 本で断定していた (設計書 §3.2 はこの上界式が使えるのは `ema` / `macd` / `atr` だけと明記)。**線形再帰 / 比を取る 2 本 (`rsi` `adx`) / 純 rolling 4 本の 3 類型に書き分け**、共通の「依存する strategy は `max_bars` を 400 以上に宣言すること」は全 9 本に残した。**T1〜T9 の `plugin.py` ブロックを同期**。②**X2 (束 2)**: 設計書 §6 I5 の fixture ②「明確な単調トレンド」が受入テストに無かった → `_trend_df` + `test_head_dependence_on_monotonic_trends` (8 系列) を追加、生成式を設計書 v1.3c に明記。③**X3 (束 2)**: I8 が `bless_candidate` を直接呼ぶだけで runbook の CLI (`afx plugin bless --from _human`) を実行していなかった → `_cli_env` + `entry.main` を in-process で叩く 3 ケース (正常系 rc=0 / `approval id=<N>`、ゲート失敗 rc=1 / stderr、未終端 journal での再 bless の traceback = [cli-bless-unresolved-journal] の現状 pin) を追加。runbook 手順 (6) は手動確認と明記。④**X4 (束 2)**: `_load_compute` が repo の `docs/examples/plugins/` を直接 `exec_module` しており `__pycache__` を実資源に書いていた (scratchpad の clean なコピーで再現) → session スコープの `examples_copy` fixture でコピーしてから import する形に変え、`co_filename` が repo 外であることを pin。**Step 10-i のブロックを同期**。プラン記載の抽出コマンドで T1〜T9 の 27 ファイル + Step 10-i = **28 ブロックすべて DIFF-ZERO** を再確認 | 1 周目レビュー `tmp/review-20260919-iis/r1/` (codex 2 束)、2026-09-19 指揮者裁定 | `5131961` / `c734f77` / `5798e05` / `c8eea63` |
| 2026-09-19 | v1.2 | **段 0 (指揮者の変異スイープ、レビュー前) の結果を反映。** **別個の変異 104 件** (実装者の 60 件とは別次元) を打ち、post-fix の再測定 7 件を合わせて 111 回測定した (ほかに語分割による無効測定 3 回を棄却)。**生存 9 件 → pin 4 本 / 是正 3 commit**、等価 5 件、プラン記述の欠陥 1 件 (⑤)。(a) **I5 が何も観測していなかった** — `_last_row_deltas` が出力キー名だけで束ねていたため `sma.value` が `ema.value` に上書きされ、9 本 20 系列のうち 19 本しか測っていなかった (`sma` を `expanding` = 先頭依存最大の実装に差し替えても 3 テストとも緑)。さらに末尾本数が `max_bars: int = 400` の**関数引数に固定**されており、`config.yaml` の宣言を変えても red にならなかった。キーを `<plugin 名>.<出力キー>` に修飾し、`df.tail(meta.max_bars)` を plugin ごとに取り、`test_all_nine_declare_max_bars_400` を追加 (窓が有限な 4 本は結合だけでは守れないため両方要る)。(b) `config.yaml` の `params` と `plugin.py` の既定値の乖離が**どちらのテストからも見えていなかった** (自己テストは `compute(df, {})`、I2 は `meta.params`) → `test_declared_params_match_each_plugins_own_defaults` を追加。(c) ADX の DM **同着規則** (`up_move > down_move` を `>=` に緩めると `adx` 0.0 → **100.0**) がランダムウォーク fixture では測度 0 で観測できず生存 → `_symmetric_expansion_df` を追加。(d) `rsi` の ε 基準 (「その行自身の `close`」) を判別する fixture を追加 (起草者の「等価」記録を訂正 — 価格比が 1 から大きく外れる行は作れる)。**Step 10-i / T3 / T7 のコードブロックを実ファイルへ同期し機械抽出で差分ゼロを再確認済み。** 併せて**実装者が報告した逆変異表の 5 件の誤りを訂正**: ① `rolling(window=p)` の `min_periods` を「外す」は pandas の既定が window なので**等価** (sma / bollinger / stochastic / ichimoku の 4 件)、さらに stochastic / ichimoku では `min_periods=1` を **`highest` か `lowest` の片側だけ**に当てても反対側の NaN が `highest - lowest` / `(highest + lowest)/2` を通って伝播するため**等価** (両側同時に当てて初めて KILLED) ② `ema` に `rolling(center=True)` は適用不可 ③ macd-6 は 9 本落ちる ④ bollinger-2 は 2 本落ちる ⑤ **Step 10-a の例示変異 `timeframe: 1h` は red にならない** — `timeframe` は loader の許可トップレベルキー (`plugin/loader.py:102-104`) なので「未知キー」ではない | 段 0 は 1 周目レビューの前提 ([[mutation-testing]] 3.6「変異を注入する主体は同時に 1 つだけ」)。生存の 4 類型のうち本束で出たのは「②生成側のキー集合が未検査」「③fixture の縮退で 2 経路が同じ答え」の 2 つ | `53f5b8c` / `2e56515` / `7cd0e6c` |

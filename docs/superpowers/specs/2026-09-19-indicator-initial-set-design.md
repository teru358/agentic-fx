# [indicator-initial-set] 設計書 v1.3e

束: 改善ループが戦略を作るときに `config.yaml` の `indicators:` で宣言できる **標準指標の初期セット (9 本)** を人間が用意する。成果物は `docs/examples/plugins/<名前>/` に置く plugin 3 点セットと、人間が配備するための runbook のみ。**新しいコマンド・新しい機構・新しい自動配備経路は一切作らない。**

前提束: [indicator-consumption-wiring] (`docs/superpowers/specs/2026-09-14-indicator-consumption-wiring-design.md` v1.6、main `3bb224c` で完了) — strategy が `indicators:` で宣言した配備済 indicator を worker 内で計算して `evaluate` に渡す配線と、`outputs` 宣言・pin ロック・resolver は既にある。**本束はその上に載せる「中身」だけを作る。**

## 0. 位置づけとユーザー裁定 (2026-09-19、設計 v0.1 承認済)

| # | 論点 | 裁定 |
|---|---|---|
| R1 | 目的 | 改善ループが戦略を作るとき `indicators:` で宣言できる**標準指標の初期セット**を用意する |
| R2 | 成果物と置き場 | 9 指標の plugin 3 点 (`plugin.py` / `config.yaml` / `test_plugin.py`) を `docs/examples/plugins/<名前>/` に置く。**配備用の新コマンド・新機構は作らない** |
| R3 | 配備手順 (暫定) | 人間が `docs/examples/plugins/<名前>` を `plugins/_human/<名前>` へコピー → `afx plugin bless <名前> --from _human` を 1 本ずつ。runbook を `docs/operations/` に置き「暫定、[first-run-setup] で置換」と明記。**恒久なのは「採用には人間の明示的確認が要る、LLM の自動配備経路は作らない」という規律** |
| R4 | 指標 9 種と出力 | `sma`/`ema` → `value` (20) ／ `rsi` → `rsi` (14, Wilder) ／ `macd` → `macd` `signal` `hist` (12/26/9) ／ `bollinger` → `upper` `middle` `lower` (20, 2.0σ) ／ `atr` → `atr` (14, Wilder) ／ `adx` → `adx` `plus_di` `minus_di` (14) ／ `stochastic` → `k` `d` (14/3/3) ／ `ichimoku` → `tenkan` `kijun` `senkou_a` `senkou_b` `chikou` (9/26/52)。**全出力は df と同じ index の `pd.Series`、足りない期間は NaN、`outputs` 宣言必須** |
| R5 | ichimoku の lookahead 禁止 | 先行スパンは未来へずらさず「現在バー時点で確定した値」を返す。遅行スパンは「現在の終値」をそのまま返す。雲との比較は strategy 側が `shift` で行う旨を docstring に明記 |
| R6 | 正しさの担保 | 各指標の自己テストに (i) 独立参照実装 (素朴なループ) との一致 (ii) 未来を読んでいないことの検査 (承認 v0.1 の「末尾切り詰め不変」は、設計レビュー r1 I2 を受けて**接頭辞因果性**へ強化した — §6.2。意図 (lookahead 禁止の観測) は不変で、検出力だけを上げる変更) (iii) warmup 期間が NaN (iv) 出力キー集合が `outputs` と完全一致・index 一致・Inf/bool なし。**ゲートとしての一般化 [indicator-reference-oracle-gate] は別束のまま** |
| R5a (r2 追記) | 新 `rsi` / `adx` の退化時の値 | **値動きが実質ゼロの区間では中立値を返す** (`rsi` → 50、`adx` の `plus_di`/`minus_di` → 0)。判定は相対 ε (`<= 1e-9·|close|`)。**既存 `rsi_indicator` は厳密 `== 0` 判定で「下げが無い → 100」**なので、横ばい相場で両者の値は一致しない。既存は R7 どおり触らないため、**同名の指標が 2 種類ある状態を許容する** (依存する strategy はどちらを宣言したかで挙動が変わる)。**2026-09-19 ユーザー見解: RSI は 0〜100 の指数で 50 が中立点なので、値動きなしで 50 を返す新定義の方が分かりやすい — 新 `rsi` の定義はこのまま進める。**<br>**v1.3b 追記 (実コードで確認)**: 配備済の `rsi_indicator` / `rsi_wilder` を**退役させる CLI 経路は現時点で存在しない** — `afx plugin retire` は `plugins/<名前>` が plain ディレクトリのときしか使えず、bless / approve で配備したもの (= `.versions/` への symlink) は `ValueError` で拒否する (`switch.py:1627-1629`)。**[retire-symlink-deployed-plugin] として起票 (§1 非スコープ)。本束では新 `rsi` と併存させる。** |
| R7 | 名前 | 短い一般名 (`sma` `ema` `rsi` …)。既存の配備済 `rsi_indicator` / `rsi_wilder` と example の `rsi_indicator` (example 戦略 `rsi_pullback` が依存) は**触らず残す** |
| R8 | 調整と改造 | 調整 = strategy 側の `indicators.<alias>.params` 上書き (承認不要、[wiring] U3)。改造 = `read_plugin_source` → staging 複製 → **別名** → 承認申請 |

## 1. スコープと非スコープ

**スコープ**: 9 本の plugin 3 点セットの新規追加 (`docs/examples/plugins/`)、その self-test、repo 側の回帰テストへの載せ方、人間配備 runbook 1 本。

**非スコープ (明示)**:
- **[indicator-reference-oracle-gate]** — 「参照実装との一致」をハーネス側のゲートとして一般化すること。本束では各 plugin の `test_plugin.py` の中の話に留める。
- **[multi-timeframe-indicator-deps]** — 複数足の依存宣言。9 本はいずれも `timeframe` を宣言せず、呼び出し側が渡した任意の足で動く。
- **[first-run-setup]** — 初回起動の対話ウィザードによる一括配備。本束の runbook はそれに置き換えられる暫定物。
- **配備の自動化** — `afx plugin bless-all` のようなコマンドも、改善ループからの自動配備経路も作らない。
- **`src/` の変更** — 本束は既存の契約・ゲート・CLI を 1 行も変えない (§5 / §7)。`tests/` は **§7 に挙げた既存 1 ファイル (`tests/plugin/test_loader.py`) への追記と、新規テストファイルの追加のみ** — 既存テストの意味を変える改変はしない (r1 M2)。
- **`afx plugin bless` の `UnresolvedJournalError` 未捕捉の修正** — 本束では `src/` を直さない。以下を別 ticket として起票する (文案、指揮者が起票):

  > **[cli-bless-unresolved-journal] `afx plugin bless` が未終端 journal を traceback で落とす**
  > `_plugin_bless` の except は `(ValueError, SandboxError)` のみ (`backtest/cli.py:589`)。`bless_candidate` が未終端 switch journal を検出して送出する `UnresolvedJournalError` は `Exception` 直系 (`plugin/switch.py:1572`) でこの集合に入らず、**利用者には Python traceback が出る**。`_plugin_retire` は同じ例外を捕捉している (`cli.py:614`) ので、bless 側だけが不揃い。処置案 = `_plugin_bless` (と `_plugin_submit`) の except に `plugin_switch.UnresolvedJournalError` を足し、「未終端 journal (op_id=...) が検出されました。収束手順は runbook 参照」の固定文言 + rc=1 にする。

- **[retire-symlink-deployed-plugin] symlink 配備された plugin を退役させる経路が無い** (v1.3b で起票。本束では直さない — `src/` を変更しないため)。以下を別 ticket として起票する (文案、指揮者が起票):

  > **[retire-symlink-deployed-plugin] `afx plugin retire` が bless / approve で配備した plugin を退役できない**
  > 事実 1: `switch.retire_plugin` (`plugin/switch.py:1605-1638`) は `plugins/<名前>` が**plain ディレクトリのときだけ**受け付け、symlink なら `ValueError("plugins/<名前> is not a plain directory (retire only applies to legacy plain live)")` で拒否する (`:1627-1629`)。承認回廊 (bless / approve) で配備されたものは必ず `.versions/<名前>/<artifact_hash>` への symlink なので、**正規の経路で配備した plugin は退役できない**。
  > 事実 2: `retire_plugin` は**依存 strategy を一切検査しない** (`:1605-1638` に該当コードが無い)。退役させた indicator を pin している strategy は、次の `approved_plugins()` の第 2 相で `not_found` になり**黙って inventory から外れる** (pin 破れ)。人間には何も表示されない。
  > 必要な機能: (a) symlink 配備の退役 (live symlink の除去 + 証跡。`.versions/` は残す) (b) 退役前に依存 strategy を表示して確認を取る (`Commands._dependent_strategies` (`commands.py:394-400`) の `dependent_pinned_here` / `dependent_pinned_elsewhere` が既にある — 同じ逆引きを `retire` の入口でも出す)。

- 収益性の検証 — 指標単体に収益性の評価軸は無い (`approval.run_kind_gate` の kind=indicator 分岐は `({}, True)` を返すだけ、`approval.py:220-221`)。

## 2. 動作目線の設計 (誰が何をどう扱うか)

### 2.1 作者 (実装 subagent)
9 本それぞれについて、リポジトリの `docs/examples/plugins/<名前>/` に 3 ファイルを新規作成する。既存の `rsi_indicator` を雛形とし、**モジュール docstring の作者向け注意 2 点 (warmup は自分の責務 / 1d 足のバケット境界は UTC 00:00 の epoch 錨) を全 9 本に踏襲する** (`docs/examples/plugins/rsi_indicator/plugin.py:12-29`)。作者は `plugins/` (実配備領域) にも DB にも触らない。

### 2.2 配備者 (人間)
runbook に従い、1 本ずつ手で配備する。1 本の手順は「`docs/examples/plugins/<名前>` を `plugins/_human/<名前>` へコピー → `afx plugin bless <名前> --from _human`」。bless は `switch.bless_candidate` → `switch._run_full_gate` を通り、候補ディレクトリの形の検査・`check_source`・`test_plugin.py` の pytest 実行・ゲート前後のハッシュ一致・`max_bars` 上限・`outputs` 宣言必須をすべて確かめてから、版ディレクトリを作って `plugins/<名前>` の symlink を切り替える。**人間が 9 回明示的にコマンドを打つことが唯一の採用経路**であり、これが R3 の「恒久な規律」の実体である。

### 2.3 消費者その 1 (改善 agent)
配備が済むと、改善 mission の `list_deployed_plugins()` の応答に 9 本が `{name, kind, pairs, params, outputs, content_hash}` の 6 キーで現れる (`improve_loop.build_inventory_view`)。agent は `indicators: {sma20: {plugin: sma, params: {period: 50}}}` のように別名と params 上書きで宣言し、`lock_staging_deps` で版をロックしてから提出する。9 本の**本文そのもの**は `read_example_plugin(name="sma")` でも読める (`docs/examples/plugins` のスナップショット `_snapshot_src/_examples/` 経由) ので、改造のときは「読んで → 別名で staging に書く」が成立する。

### 2.4 消費者その 2 (strategy worker)
strategy を評価するとき、worker は依存 indicator ごとに `df.tail(dep.max_bars)` を渡して `compute` を呼び、返った Series を strategy の全 index へ `reindex` してから `evaluate(df, indicators, None, params)` に渡す (`plugin/worker.py:316-325`)。**したがって indicator が実際に見る履歴は `min(strategy.max_bars, indicator.max_bars)` 本**であり、resolver は両者の大小を比較しない (意図的、[wiring] §2.3 codex r4 I3)。この事実が §3 の `max_bars` 選定と §6 の I5 の形を決める。

### 2.5 消費者その 3 (取引判断 loop)
`get_indicators` は配備済 indicator を standalone で呼び、系列の末尾値を射影する。9 本は `outputs` を宣言しているので、宣言キー集合との完全一致検査を毎回受ける。追加配線は不要。

## 3. 指標ごとの定義

### 3.0 全 9 本に共通する規約

- **入力**: ハーネスが渡す完成バーのみの OHLCV DataFrame (UTC 昇順 DatetimeIndex)。**入力に NaN は無いものとする** — NaN を含む入力に対する挙動は未規定とし、ただし**例外を送出してはならない** (validator が NaN を許容するので全 NaN で返ってよい)。
- **出力**: `outputs` に宣言したキーの `pd.Series` (df と同じ index、`float64`)。確定していない行は NaN。±Inf と bool は `core/plugin_contract.validate_indicator_result` が拒否するので、ゼロ除算は必ず §3 の規則で潰す。
- **平滑の種類は 2 つだけ**:
  - **SMA(p)** = 直近 p 本の単純平均。`rolling(window=p, min_periods=p).mean()`。
  - **再帰平滑 (α, seed=x₀)** = `y_t = α·x_t + (1−α)·y_{t−1}`、`y_0 = x_0`。`ewm(..., adjust=False, min_periods=p).mean()`。**EMA(p) は α = 2/(p+1)**、**Wilder(p) は α = 1/p**。
- **seed の裁定 (D1、§8)**: 再帰平滑の初期値は **`y_0 = x_0`** (pandas `adjust=False` の既定) とし、「先頭 p 本の SMA を seed にする」教科書流は採らない。理由: 既存の配備済 `rsi_indicator` がこの再帰であり (`rsi_indicator/plugin.py:54-57`)、§3.2 の `max_bars` 収束見積りもこの再帰を前提にしている。差は先頭側だけで、`min_periods=p` の NaN に覆われる区間を過ぎれば指数的に消える。
- **TR / ±DM の行 0**: `TR[0]` と `plus_dm[0]` / `minus_dm[0]` は **NaN** とする (前バーが無いので未確定)。`np.maximum` は NaN を伝播し、`pd.concat(...).max(axis=1)` は NaN を飛ばして `high-low` を返す — **後者は使わない** (warmup 本数が曖昧になる)。
- **`max_bars` は全 9 本で 400** (§3.2)。`settings.plugin.max_bars_limit` の既定 1000 以内 (`config/settings.yaml.example:129`)。
- **参照実装の書き方** (`test_plugin.py` の中): pandas のベクトル演算を一切使わず、`list[float]` に対する `for` ループで定義式をそのまま書き下す。`rolling` は毎回スライスして `sum(window)/p`、再帰平滑は `prev` を持ち回る 1 行。**`min_periods` は位置 (`i < p−1`) ではなく「非 NaN 観測数」で数える** — 詳細と、そこを間違えると `atr` / `adx` の warmup が 1 本ずれて I3 が落ちる理由は §3.3。plugin.py と**同じ関数を別の書き方で 2 度書く**のが目的なので、plugin.py からの import・コピーは禁止。

### 3.1 定義表

`p` は `params` の値。warmup 列は「値が入る最初の 0 起点行番号」(実測、§6 I3 の pin 対象)。

| plugin | outputs | 定義 | 既定 params | warmup (最初に値が入る行) | ゼロ除算 |
|---|---|---|---|---|---|
| `sma` | `value` | SMA(p) of close | `period: 20` | 19 | なし |
| `ema` | `value` | 再帰平滑 α=2/(p+1) of close | `period: 20` | 19 | なし |
| `rsi` | `rsi` | `delta = close.diff()`、`gain = max(delta,0)`、`loss = max(−delta,0)`、`avg_* = Wilder(p)`、`rsi = 100 − 100/(1 + avg_gain/avg_loss)` | `period: 14` | 14 **ε 規則 (§3.2 (i-b))**: ① `avg_gain + avg_loss <= ε·|close|` → **50.0** (値動きなし = 中立) ② それ以外で `avg_loss <= ε·|close|` → **100** (上げのみ) ③ 通常は式どおり。**判定順は ①→②→③ 固定**。<br>**既存 `rsi_indicator` との差異**: 既存は厳密 `==` 判定で「`avg_loss == 0` → 100」「両方 0 → 50」。新 `rsi` は**実質ゼロ**を ε で判定し、「上げのみ」と「値動きなし」を分ける。既存 `rsi_indicator` は触らない (R7) ので、**同じ RSI(14) でも横ばい相場で値が違い得る** (既存は 100 / 新は 50) |
| `macd` | `macd` `signal` `hist` | `macd = EMA(fast) − EMA(slow)`、`signal = 再帰平滑(α=2/(signal_period+1)) of macd`、`hist = macd − signal` | `fast: 12, slow: 26, signal_period: 9` | macd 25 / signal 33 / hist 33 | なし |
| `bollinger` | `upper` `middle` `lower` | `middle = SMA(p)`、`σ = rolling(p).std(ddof=0)` (母標準偏差)、`upper = middle + k·σ`、`lower = middle − k·σ` | `period: 20, num_std: 2.0` | 19 (3 本とも) | σ=0 は除算しないので `upper == middle == lower` (異常ではない)。`num_std` 自体は `> 0` を要求する (§4) |
| `atr` | `atr` | `TR = max(high−low, |high−prev_close|, |low−prev_close|)` (行 0 は NaN)、`atr = Wilder(p) of TR` | `period: 14` | 14 | なし |
| `adx` | `adx` `plus_di` `minus_di` | `up = high.diff()`、`dn = −low.diff()`、`plus_dm = up if (up>dn and up>0) else 0`、`minus_dm = dn if (dn>up and dn>0) else 0` (行 0 は両方 NaN)、`atr = Wilder(p) of TR`、`±DI = 100·Wilder(p) of ±DM / atr`、`dx = 100·|plus_di−minus_di|/(plus_di+minus_di)`、`adx = Wilder(p) of dx` | `period: 14` | plus_di 14 / minus_di 14 / adx **27** **ε 規則 (§3.2 (i-b))**: `atr <= 1e-9·|close|` → `plus_di = minus_di = 0.0` (→ `dx = 0.0`)。加えて `plus_di + minus_di == 0` → `dx = 0.0` |
| `stochastic` | `k` `d` | `hh = rolling(p).max(high)`、`ll = rolling(p).min(low)`、`raw = 100·(close−ll)/(hh−ll)`、**`k = SMA(k_period) of raw`**、`d = SMA(d_period) of k` (= 標準的な slow stochastic、裁定 D2) | `period: 14, k_period: 3, d_period: 3` | k 15 / d 17 | `hh == ll` → `raw = 50.0` |
| `ichimoku` | `tenkan` `kijun` `senkou_a` `senkou_b` `chikou` | `mid(n) = (rolling(n).max(high) + rolling(n).min(low))/2`、`tenkan = mid(tenkan_period)`、`kijun = mid(kijun_period)`、`senkou_a = (tenkan + kijun)/2`、`senkou_b = mid(senkou_b_period)`、`chikou = close` | `tenkan_period: 9, kijun_period: 26, senkou_b_period: 52` | tenkan 8 / kijun 25 / senkou_a 25 / senkou_b 51 / chikou 0 | なし |

**ichimoku の lookahead 禁止 (R5)**: `senkou_a` / `senkou_b` に `shift(+26)` を掛けない。`chikou` に `shift(−26)` を掛けない。**返すのはすべて「そのバー時点で確定している値」**であり、「26 本先へ投影した雲」でも「26 本前へ遡らせた遅行線」でもない。docstring に逐語で書く: *「雲との比較をしたい strategy は `indicators["ichi"]["senkou_a"].shift(kijun_period)` を自分で取ること。この plugin は未来の行に値を置かない (接頭辞因果性 §6 I4 の担保でもある)」*。

### 3.2 `max_bars` の決め方 — 全 9 本で 400

**400 は「普遍的な保証」ではない (r1 I3)。** 解析的上界 (i) と、仕様化したデータ範囲での回帰試験 (ii) の 2 段で根拠を置く。契約は OHLC の値域を制限していないので、任意の有効入力に対する 1e-6 保証は原理的に書けない。

#### (i) 解析的上界

**先頭依存 (head dependence)**: 再帰平滑は `y_0 = x_0` から始まるので、履歴の**先頭**を切ると seed が変わり、最終行まで残差が残る。線形再帰なので残差は厳密に

> **`|残差(最終行)| = |Δseed| · (1−α)^(N−p)`**  (N = 与えた本数、p = warmup)

`(1−α)^(N−p)` の値 (N = 400):

| 平滑 | α | `(1−α)^(N−p)` |
|---|---|---|
| Wilder p=14 (`atr`) | 1/14 | **3.77e-13** |
| EMA p=26 (`macd` の slow) | 2/27 | **3.16e-13** |
| EMA p=20 (`ema`) | 2/21 | 3.04e-17 |
| EMA p=12 (`macd` の fast) | 2/13 | 7.09e-29 |

**`|Δseed|` は入力の値域に比例する** — ここが「普遍的保証を書けない」理由の 1 つ。価格スケールの出力 (`ema` / `macd` / `atr`) では `|Δseed|` は最大で価格の変動幅のオーダー。

**この式が使えるのは `ema` / `macd` / `atr` だけ (r2 I1 で撤回)**。式の前提は「比較する 2 本の再帰平滑が seed 以外では**同一の入力列**を受ける」こと。**`rsi` と `adx` はこれを満たさない** — どちらも平滑量どうしの**比** (`avg_gain/avg_loss`、`Wilder(±DM)/ATR`) を取り、その比が非線形な除算を通って次の段へ入るため、内側の差が外側の入力列そのものを変えてしまう。v1.2 が書いていた `(13/14)^(N−2p) × 100 = 1.07e-10` という ADX の上界と「実測 1.1e-10 と一致する」という主張は**撤回する** (ランダムウォーク fixture での一致は偶然であり、下の反例が示すとおり N を増やしても収束しない構成が存在する)。

#### (i-b) 比を取る指標の退化 — `rsi` と `adx` に固有の構成

**反例 (codex r2 I1、実測で再現)**: 400 本窓のちょうど手前に上向きの DM/TR を 1 本だけ置き、**その後 400 本を `high == low == close` の完全な横ばい**にする。

- **長い履歴側**: 横ばい区間の TR も ±DM も 0 なので、`Wilder(+DM)` と `ATR` は**同率で**減衰する。比 `100·Wilder(+DM)/ATR` は減衰せず、`plus_di ≈ 100` が残り続ける。`rsi` も同様に `avg_gain/avg_loss` の比が残る。
- **切り詰め側 (末尾 400 本だけ)**: 全行が横ばいなので `ATR = 0` / `avg_gain = avg_loss = 0` になり、ゼロ除算規則で `DI = 0`・`rsi = 中立` になる。

実測 (基準価格 150、5m 足相当):

| 規則 | `|Δadx|` | `|Δrsi|` |
|---|---|---|
| **v1.2 の規則 (厳密 `== 0` 判定のみ)** | **22.37** | **11.82** |
| **本 spec の ε 規則 (ε = 1e-9)** | **4.31e-06** | **0.0** |

> **`|Δadx|` の数値について (v1.3b、2026-09-19)**: **4.31e-06 は実装プランの fixture 生成式 (プラン T0 / T10 Step 10-c の `_degenerate_df` を参照) での観測値**。v1.3a までは **5.2e-06** と書いていたが、これは起草時の ad hoc な fixture での値だった。**前段データの作り方 (通常データ区間の本数・σ・seed) に依存する観測値**であって導出値ではなく、**I5 の公差 `1e-4` はどちらも覆う**ので結論は不変。以降この節で `|Δadx|` の絶対値を引くときは、**プランの逐語 fixture を正とする**。

つまり「400 本なら 1e-10」は**この構成では成り立たない** (規則なしで 22)。9 本のうち影響を受けるのは **`rsi` と `adx` の 2 本だけ**: `stochastic` も比を取るが `rolling` の**有限記憶**なので先頭依存が無く、実測でも同じ fixture で Δk = Δd = **0.0**。`sma` / `ema` / `bollinger` / `macd` / `atr` / `ichimoku` は比を取らない。

**退化規則 (ε、相対で定義)** — §3.1 の表に反映済み:

> **`adx`**: `atr <= ε · |close|` の行では `plus_di = minus_di = 0`（→ `dx = 0`）
> **`rsi`**: ① `avg_gain + avg_loss <= ε · |close|` → `rsi = 50.0` ② それ以外で `avg_loss <= ε · |close|` → `rsi = 100.0` ③ それ以外は式どおり。**判定順 ①→②→③ 固定**
> **`ε = 1e-9`**

**`|close|` の意味を固定する (曖昧さを残さない)**: **その行自身の `close`** である。`close.shift(1)` でも区間平均でもない。選択自体は恣意的だが、**plugin 実装と参照実装 (§3.3) が同じものを使わなければ I3 が境界行で落ちる** — 閾値ちょうど付近 (`atr ≈ 1e-9·close`) の行では、どちらを使うかで分岐が反転し得る。参照実装も **ε 比較を同じ順序・同じ基準で**書くこと (`rsi` は ①→②→③、`adx` は `atr` 判定を `±DI` の計算より前に)。

**ε = 1e-9 の決め方** (実測):

| ε | 反例での `|Δadx|` | USDJPY 5m (`atr/close ≈ 6.95e-06`) の余裕 |
|---|---|---|
| 厳密 0 のみ | 22.37 | — |
| 1e-15 | 5.08 | 7.0e+09 倍 |
| **1e-12** | 5.2e-03 | 7.0e+06 倍 |
| **1e-9** | **5.2e-06** | **7.0e+03 倍** |
| 1e-6 | 4.9e-09 | **7 倍 — 誤発火の危険** |

> この掃引表は **ε どうしを相対比較するための表**なので、起草時の ad hoc fixture での値を
> そのまま残してある (同一条件で並べる方が意味がある)。**絶対値として引くべき数値は
> プランの逐語 fixture での 4.31e-06** (上の注記、v1.3b)。

ε を上げるほど反例での一致は良くなるが、**通常データでの誤発火余裕が減る**。**想定 TR 幅から合成した系列での `atr/close` 実測** (実データの取り込みではない — 括弧内が仮定した 1 本あたりの TR): USDJPY 5m (TR≈0.001) 6.95e-06 / USDJPY 1h (TR≈0.02) 1.23e-04 / EURUSD 5m (TR≈0.00008) 6.44e-05 / XAU 5m (TR≈0.3) 1.36e-04。**ε = 1e-6 は USDJPY 5m に対して 7 倍しか離れておらず**、閑散時間帯や祝日で容易に踏む。**ε = 1e-9 なら最悪でも 7.0e+03 倍の余裕**があり (`(avg_gain+avg_loss)/close` も同様に 3.2e+03 倍以上)、その ε で反例の `|Δrsi|` は 0、`|Δadx|` は **4.31e-06** (プランの逐語 fixture での観測値、v1.3b) に収まる。**ε = 1e-9 を採る。**

**ε 規則でも `adx` の一致は 1e-6 に届かない (正直に書く)**: 4.31e-06 が残るのは、長い履歴側で ε 規則が**途中から**発火する (ATR が徐々に減衰して閾値を割る) のに対し切り詰め側は最初から発火しているため、`Wilder(DX)` に入る列の**発火開始位置**がずれるから。横ばい本数を増やせば消える (flat=500 で 3.1e-06、flat=600 で 1.9e-09、flat=800 で 6.9e-16) が、**境界付近では ε をどう選んでも残る**。したがって:
- **`adx` の退化 fixture での公差だけは `abs < 1e-4`** とする (I5)。これは観測値であって導出値ではない。**公差 1e-4 は、前段データの作り方が変わっても (5.2e-06 でも 4.31e-06 でも) 覆う幅として選んである** (v1.3b)。
- ただし ε 規則には**受入試験のため以外の価値**がある: 規則が無いと、**400 本まったく値動きが無い市場で `plus_di ≈ 100`** (= 強い上昇トレンド) という意味的に誤った値を返す。ε 規則はこれを「中立」に倒す。

#### (ii) 仕様化したデータ範囲での実測

実測 A (合成ランダムウォーク、初値 150・σ=0.15/本、末尾 N 本で計算した最終行の値と全 5000 本で計算した値の差):

| N | ema20 | rsi14 | macd | atr14 | **adx14 (40 seed の最悪)** |
|---|---|---|---|---|---|
| 100 | 3.9e-06 | 4.2e-02 | 1.5e-05 | 4.2e-06 | — |
| 200 | 1.4e-09 | 1.1e-05 | 2.4e-07 | 3.1e-08 | — |
| 250 | 7.0e-12 | 8.7e-07 | 4.3e-09 | 6.1e-11 | **7.4e-06** |
| 300 | 2.8e-14 | 2.8e-08 | 4.5e-11 | 4.1e-11 | **1.6e-07** |
| 350 | — | — | — | — | **5.6e-09** |
| **400** | 0 | 1.4e-11 | 5.2e-14 | 1.2e-15 | **1.1e-10** |

**この表の `rsi` / `adx` 列は「この fixture での観測値」であって上界ではない** (r2 I1)。ランダムウォークでは N とともに減衰して見えるが、§3.2 (i-b) の退化構成では減衰しない。上界式で説明できるのは `ema` / `macd` / `atr` の 3 列だけ。

実測 B (**上界式の検証 — 400 本窓のちょうど手前 (401 本目) に +100 のスパイクを置く**。これは `|Δseed|` を人為的に最大化する構成。対象は上界式が成立する 3 本のみ):

| 系列 | ema20 | atr14 | macd |
|---|---|---|---|
| スパイクなし (価格 150 / σ0.2) | 0.0 | 1.06e-14 | 2.84e-14 |
| **401 本目に +100 のスパイク** | 0.0 | **1.99e-12** | **3.70e-13** |
| 価格 300 / σ0.4 | 0.0 | 1.01e-14 | — |

スパイクで `|Δseed|` を 100 に引き上げても残差は 2e-12 で、上界 `100 × 3.77e-13 = 3.8e-11` の内側に収まる。**価格スケールを 2 倍にしても残差は比例するだけ** (1e-14 オーダー) で、主要 FX ペアの値域 (0.5 〜 300 程度) では余裕がある。

純 rolling の 4 本 (`sma` / `bollinger` / `stochastic` / `ichimoku`) は原理的に先頭非依存だが、**厳密な bit 一致ではない**: pandas の `rolling(...).std()` は逐次更新アルゴリズムなので丸めの累積が履歴長に依存し、実測で ~3e-11 の差が残る (`sma.mean()` は ~3e-14)。これは「先頭依存」ではなく浮動小数の丸めであり、許容誤差の形 (§6 I5) を絶対等値でなく公差にする根拠である。

**選定: 全 9 本で `max_bars: 400`。** 理由:
1. 上界式が使える 3 本 (`ema` / `macd` / `atr`) は N=400 で 1e-13 台。`rsi` / `adx` には上界式が無い (§3.2 (i-b)) が、ε 規則の下で**仕様化した fixture 集合**では `rsi` 1.4e-11 / `adx` 1.1e-10、退化 fixture でも `rsi` 0.0 / `adx` **4.31e-06** (プランの逐語 fixture での観測値、v1.3b)。いずれも I5 の公差の内側。
2. **人間の配備者と strategy 作者が覚える数が 1 つで済む。** 指標ごとに 120/160/400 と散らすと、`max_bars: 200` の strategy が「`sma` は正確だが `adx` だけ静かに 7e-06 ずれる」という、テストで気づきにくい部分的劣化を生む (§2.4 の `min(...)` 規則のため)。
3. 400 は `max_bars_limit` 既定 1000 の 4 割で、resolver の `over_max_bars_limit` にも handshake 総量上限にも届かない。

**strategy 作者への帰結 (docstring と runbook に明記)**: *「これらの indicator に依存する strategy は自分の `max_bars` を依存先の `max_bars` 以上 (= 400 以上) に宣言すること。小さく宣言しても動くが、渡る履歴が短くなり値がわずかにずれる。」* 既存 example の `rsi_pullback` は `max_bars: 200` なので、新 `rsi` を宣言すると**動きはするが §6 I5 の保証範囲の外**になる (表の N=200 で 1.1e-05)。I7 ではこの点を明示して扱う。

### 3.3 参照実装の骨格 (`test_plugin.py` の中)

参照実装は「同じ式を 2 度、違う書き方で書く」ためのものなので、**pandas の集約 API を一切使わない**。使うのは `list` / `for` / `sum` / `len` / `float("nan")` / `math` だけ。3 つの基本部品で 9 本すべてが書ける:

- `_ref_sma(values, p)` — `i < p-1` は NaN、以降は `sum(values[i-p+1:i+1]) / p`。
- `_ref_recursive(values, alpha, p)` — **位置ではなく「非 NaN の観測数」で数える**。`prev` は**最初の非 NaN 値**で seed し、NaN の要素は `prev` を更新せず出力も NaN、非 NaN のたびに `prev = alpha*x + (1-alpha)*prev` してカウンタを 1 増やし、**カウンタが p 未満の間は出力 NaN**。これが pandas の `ewm(adjust=False, min_periods=p, ignore_na=False)` の意味論であり、**§3.1 で `atr` の warmup が 13 ではなく 14、`adx` が 27 になる理由**でもある (行 0 の TR / ±DM を NaN にしたぶん、確定が 1 本後ろへずれる)。位置で `i < p-1` と書くと 1 本早く明けて I3 が `atr` / `adx` の 2 本で落ちる。
- `_ref_rolling_minmax(values, p, op)` — `min`/`max` を素のスライスで取る。

**`rsi` / `adx` の参照実装は ε 規則も書き写す**: plugin.py と**同じ閾値 (`1e-9`)・同じ基準 (`その行の |close|`)・同じ判定順**で分岐を書く (§3.2 (i-b))。ここを「参照実装は素朴な式のまま」にすると、閾値付近の行で I3 が落ちる — 参照実装は「別の書き方」であって「別の仕様」ではない。

`_ref_std` (bollinger) だけは `sqrt(sum((x - mean)**2) / p)` を素で書く (`ddof=0`)。pandas の逐次更新アルゴリズムとは丸めの経路が違うので、ここが I3 の公差 (rel/abs 1e-9) を必要とする主因になる。

**参照実装を `test_plugin.py` に置くことの可否**: 置いてよい。`read_example_plugin(name="sma")` は改善 agent に 3 ファイルすべてを見せるので、参照実装も agent から読める。指標の定義式は秘密ではなく、むしろ agent が「この plugin が何を計算しているか」を曖昧さなく知れるのは望ましい。遮断 8 が守るのは成績・holdout・人間の判断根拠であって数式ではない (§5.3)。

## 4. 共通契約 (全 9 本の docstring と params 検証)

**モジュール docstring の必須 6 項目** (`rsi_indicator` の 2 項目を拡張。v1.3c で見出しの計数を本文の項目数に合わせた):
1. warmup はこの関数の責務。行が足りなくても「返さない」はできない (宣言 `outputs` と完全一致するキー集合が毎回要る)。足りない期間は NaN。消費側は `pd.isna` を見て hold する。
2. 1d 足のバケット境界は UTC 00:00 (epoch 錨) であり FX の取引日境界 (NY 17:00) ではない。この plugin は `timeframe` を宣言しないので任意の足で使われ得る。
3. **`max_bars: 400` の意味と、依存する strategy が `max_bars` を 400 以上に宣言すべき理由** (§3.2)。
4. **純関数であること** — I/O・乱数・実時計・グローバル状態の書き換えは禁止 (サンドボックスが AST で遮断する)。`df` と `params` を書き換えない。
5. (ichimoku のみ) 先行/遅行スパンの lookahead 規約 (§3.1)。
6. (`rsi` / `adx` のみ) **値動きの無い期間は中立値になる** — `rsi` は 50、`adx` の `plus_di`/`minus_di` は 0 (→ `adx` も 0 へ収束)。判定は `<= 1e-9·|close|` の相対 ε (§3.2 (i-b))。**strategy 側は「RSI が 50 付近」「DI が 0」を「中立」ではなく「板が動いていない」と読み分けたいなら、`atr` を併せて宣言して自分で判定すること。** 既存 `rsi_indicator` は同じ場面で 100 を返す — 両者を混ぜて使わない。

**既定値の出所は `plugin.py` の `_DEFAULTS` 1 箇所** (v1.3e): `_*_param(params, "period", _DEFAULTS["period"])` の形で引き、受入テストは `_DEFAULTS` と `config.yaml` の `params` を**型込みで**辞書同値比較する (`2 == 2.0` なので素の比較では `num_std: 2.0 -> 2` の型落ちを見逃す — 実測)。値の表を受入テスト側に重複させない。

**`config.yaml` の `params` は `plugin.py` の既定値と一致させる** (v1.3c で明文化、段 0 の実測を受けた指揮者裁定): 9 本の自己テストは `compute(df, {})` (= `plugin.py` の既定値) だけを、受入テスト I2 は `compute(df, dict(meta.params))` (= `config.yaml` の宣言値) だけを通すので、**両者がずれると「自己テストは緑のまま、配備された指標だけ別物」になる**。宣言値は実際に本番で使われる値である。観測は `tests/plugin/test_indicator_initial_set.py::test_declared_params_match_each_plugins_own_defaults` (値の表を持たず、両方の戻り値を全キーで比較する)。

**params の型と範囲の検証**: **例外を投げず、不正なら出力を全 NaN にする**方針は採らない。逆に、**不正な params は `ValueError` で即座に失敗させる**。理由: `params` は loader が JSON-safe 性しか見ない (`loader._check_json_safe`) ので、型・範囲の責任は plugin にある。全 NaN で黙って返すと、strategy 側は「warmup 不足」と区別できず、改善 agent は「系列が全 NaN → `max_bars` を増やす」という誤った申し送りに誘導される (規律 7 の文言がまさにそう指示している)。例外なら worker が `{"ok": false, "error": "ValueError: ..."}` として構造化報告し、self-test / `run_plugin_tests` / bless の pytest ゲートのいずれかで必ず露見する。

**型検査は「変換」ではなく「確認」で行う (r1 I1)**。`int(params.get("period", 14))` の形は使わない — `14.9` を `14` に、`"14"` を `14` に**黙って読み替えて**しまい、strategy が `params: {period: 14.9}` と上書きしたときに「14.9 期間」ではなく「14 期間」の指標が返る。上書きは承認不要 (R8/U3) なので、この読み替えは誰のレビューも通らずに本番へ届く。**元の値の型をそのまま確認し、違えば `ValueError`** とする。

各 plugin が冒頭で行う検証:

| 対象 | 規則 | 理由 |
|---|---|---|
| 期間系すべて (`period` / `fast` / `slow` / `signal_period` / `k_period` / `d_period` / `tenkan_period` / `kijun_period` / `senkou_b_period`) | 元値が **`bool` でない `int`** であること → **`>= 1`** | `bool` は `int` の派生なので先に弾く。`float` / `str` は変換せず拒否。下限は一律 1 (`period: 1` は「平滑なし」で数学的に定義でき、`rolling(1).std(ddof=0) == 0` も `alpha = 1/1 = 1` も破綻しない — 使い道の無さを理由に禁じない) |
| `macd` の `fast` / `slow` | 上に加えて **`fast < slow` を要求** | `fast == slow` は `macd` と `hist` が恒等的に 0 になる (動いているように見えて何も判定していない退化形)。`fast > slow` は**全クロスの符号が反転**し、「macd が signal を上抜けたら買い」が静かに逆売買になる。どちらも救う正当な用途が無い |
| `ichimoku` の 3 期間 | **順序関係 (`tenkan < kijun < senkou_b`) は要求しない** | 式は任意の順序で well-defined で、符号反転も退化も起きない。順序を強制すると 7/22/44 のような正当なパラメータ探索を塞ぐ (R8 の「調整は承認不要」と衝突する) |
| `stochastic` の `k_period` | `>= 1` のみ | **`k_period: 1` が fast stochastic** (raw %K そのもの) を与える正式な経路 (裁定 D2) |
| `bollinger` の `num_std` | 元値が **`bool` でない `int` または `float`**、`math.isfinite`、**`> 0`** | `0` は 3 本が同一系列になる退化形で、`bollinger` を宣言した意味が消える。負値は `upper < lower` の反転 |
| **`params` のキー集合そのもの (v1.3e、2 周目 codex C2)** | **既知キー以外が 1 つでもあれば `ValueError`**。既知キー集合は各 plugin が `_DEFAULTS` (既定値の表) を持ち `_KNOWN_PARAMS = frozenset(_DEFAULTS)` で導く。`compute` の冒頭で `set(params) - _KNOWN_PARAMS` を見る | strategy 側の params 上書きは**承認不要** (R8 / U3) なので、`period` のつもりで `perod: 10` と綴りを誤ると**黙って既定値のまま動き**、その値を前提にした backtest 結果が出る。承認・backtest・bless のどの経路でも fail closed になる方が正しい (上の「各消費者の振る舞い」表がそのまま当てはまる)。文言は `params.<キー> is not a known parameter, known: <既知キー>` — 他と同じく `params.` で始まり plugin 名を入れない |

**JSON 往復で型は保たれる**ので、この厳密な型検査は端から端まで成立する: YAML の `14` は `int`、`14.0` は `float` として loader を通り、worker は `json.loads(json.dumps(params))` で deep copy する (`worker.py:171-175`) — JSON は int と float を区別するので、`int` で書いた値が `compute` に `float` として届くことはない。

**共通 helper の骨格** (下の 2 関数を**各 plugin の `plugin.py` にそのまま持つ**):

```python
def _int_param(params: dict, name: str, default: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def _float_param(params: dict, name: str, default: float) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"params.{name} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"params.{name} must be a finite number > 0, got {value}")
    return value
```

**重複は避けられない (明示)**: plugin は 1 フォルダ 3 ファイルで完結しなければならず、共有モジュールを置く経路が無い — `loader._reject_unexpected_py_files` (`loader.py:519-539`) が 4 本目の `.py` を拒否し、`sandbox.check_source` は相対 import を拒否し allowlist は `math` / `statistics` / `numpy` / `pandas` / `__future__` だけ (`sandbox.py:118-119`, `:223-225`)。**9 本が同じ十数行を持つ**ことになるが、これは設計上の帰結であって重複の見落としではない。逐語同一にしておき、直すときは 9 本まとめて直す。

**例外文言の形は 9 本で統一する**: `params.<キー名> must be <期待>, got <値>`、相互関係は `params.fast must be < params.slow, got fast=26 slow=12`。plugin 名や候補名は入れない (ゲートの reject reason 語彙に混ぜない)。

**不正 params で `ValueError` を投げたとき、各消費者がどう振る舞うか (実コードで確認)**:

| 場面 | 起きること | 判定 |
|---|---|---|
| strategy worker の依存計算 | `worker.py:339-343` の `except Exception` が捕まえ `{"ok": false, "error": "ValueError: params.period must be ..."}` を 1 行で返す。`PluginSession.call` がこれを `SandboxError` に写像 (`sandbox.py:590-591`)。**セッションは死なず**次の call は通る (plugin 自身の例外は timeout/kill とは別分類) | 構造化報告 |
| bless / submit (承認回廊) | `strategy_adapter` は `SandboxError` を**捕まえず貫通させる**契約 (`strategy_adapter.py:41-43`)。`_run_full_gate` の `except SandboxError` が `ValueError(f"plugin {name!r}: {exc}")` へ写像し (`switch.py:1010-1012`)、`afx plugin bless` が `エラー: ...` を stderr へ出して **rc=1、承認行は作らない** | **fail closed** |
| 人間 CLI `afx backtest run --plugin` | `cli.py:447` の `except SandboxError` で rc=1 | **fail closed** |
| 改善ループの `run_backtest` / commit gate | gate verdict が不合格になり候補は承認申請にならない | **fail closed** |
| 本番 producer (live) | `signal_producer.py:194-199` が plugin 単位で捕まえ、warning を出して**その plugin の cursor を進めず次 tick で再試行**。signal 行は書かれない | **fail open だが無害** — 誤った値で発注することはなく、その strategy が黙って止まる (毎 tick warning が残る) |
| 取引判断 loop の `get_indicators` | `market_tools.py:88-93` が捕まえ、`plugin:<名前>` キーだけを落として warning。組み込み指標は必ず返る | **fail open** (LLM 向け参考情報なので設計どおり) |

**結論**: 承認・バックテストの全経路で fail closed、本番の 2 経路 (producer / trade LLM) は fail open だが**誤った値が発注判断に届く経路は無い** (値が出ないだけ)。全 NaN で黙って返す案を採らない理由はここにある — 全 NaN は「warmup 不足」と区別できず、改善 agent は規律 7 の文言に従って「`max_bars` を増やせ」という誤った申し送りを書く。



**loader が形を検証する箇所 (実物)** — 9 本はすべてこの範囲に収まる:

| 規則 | 実装 | 9 本での充足 |
|---|---|---|
| `config.yaml` の許可トップレベルキー = `{kind, params, timeframe, pairs, exit_mode, max_bars, indicators, outputs}` | `plugin/loader.py:102-104` | 使うのは `kind` / `outputs` / `params` / `max_bars` の 4 つだけ。`exit_mode` は kind=strategy 専用なので書かない (`loader.py:277-279` で reject される) |
| plugin 名の正規形 `^[a-z][a-z0-9_]{0,63}$` | `loader.py:142` | `sma` `ema` `rsi` `macd` `bollinger` `atr` `adx` `stochastic` `ichimoku` — 全 9 名が適合 |
| `outputs` の各要素 `^[a-z][a-z0-9_]{0,31}$`、非空・重複不可 | `loader.py:112`, `:339-358` | 全 15 出力名 (`value` `rsi` `macd` `signal` `hist` `upper` `middle` `lower` `atr` `adx` `plus_di` `minus_di` `k` `d` `tenkan` `kijun` `senkou_a` `senkou_b` `chikou`) が適合。下線・数字なしの短名のみ |
| `outputs` ≤ 32 / `params` の canonical JSON ≤ 8 KiB | `MAX_OUTPUTS` / `MAX_PARAMS_BYTES`, `loader.py:107-109` | 最大は `ichimoku` の 5 出力、`params` は最大 3 キーの整数 — 桁で余裕 |
| `outputs` は kind=indicator 専用・新規承認では必須 | `loader.py:340-342` / `approval.outputs_required_violation` (`approval.py:118`) | 全 9 本が宣言する |
| `max_bars` は int ≥ 1、承認時に `settings.plugin.max_bars_limit` 以下 | `loader.py:281-284` / `approval.assert_max_bars_within_limit` (`approval.py:101-115`) | 400 ≤ 1000 (既定) |
| `compute(df, params)` がモジュール直下に定義され、位置引数名が完全一致・デコレータなし・必須 kwonly なし | `loader._KIND_FUNCS` (`:93-97`) / `_has_matching_function` (`:371-411`) | 9 本とも `def compute(df: pd.DataFrame, params: dict) -> dict:` |
| フォルダ直下に規定 3 ファイル以外の `.py` を置かない (`conftest.py` 同梱の遮断) | `loader._reject_unexpected_py_files` (`:519-539`) | 3 ファイルのみ。`__pycache__/` はディレクトリなので対象外 |

**禁止事項 (サンドボックスの実測結果、§5)**: `df.open` のような属性アクセス (`open` は denylist)、`to_frame` を含む `to_` 接頭辞のメソッド (許可は `to_dict`/`to_list`/`to_numpy`/`to_pydatetime` の 4 つのみ)、`getattr`、`global`、外部オブジェクトの属性への代入。**OHLCV 列は必ず `df["open"]` のように添字で取る。**

**入力 df を書き換えない (r2 I2)**: `check_source` は**属性**代入 (`df.attrs = ...`) を拒否するが (`sandbox.py:202-214` の `isinstance(node.ctx, (ast.Store, ast.Del))`)、**添字代入 `df["tmp"] = ...` は拒否しない** (実測で PASS)。したがって「入力を壊さない」は AST では守られておらず、**作者の規律 + 受入条件 I9 (入力不変・反復決定性) が唯一の観測点**である。必要なら `df["close"].copy()` や新しい `pd.Series` を作る。

**ただし本番は既に守られている (誇張しない)**: 経路ごとに理由が違う — **依存 indicator** は strategy worker が `df.tail(dep.max_bars).copy(deep=True)` を渡すので deep copy (`worker.py:318`)、**standalone の indicator** は `fn(df, params)` に渡る df が `_wire_to_df` で**毎回 wire から再構築された新品** (`worker.py:331`)。どちらの経路でも **plugin が入力を壊しても他の plugin やハーネスには波及しない。** I9 は「本番の穴を塞ぐ」検査ではなく、**作者の規律を早い段階で観測可能にする**ための衛生検査であり、モジュールレベルの状態持ち越し (`global` 文なしで可変コンテナを書き換える形) を同時に捕まえる。

## 5. 遮断と安全 — 新しい sink も新しい入力経路も増えない

実物で確認した事実:

1. **9 本が使う pandas/numpy API はすべて `sandbox.check_source` を通る。** 代表実装 (`ewm(alpha=..., adjust=False, min_periods=...)` / `rolling(...).mean()/.std(ddof=0)/.max()/.min()` / `shift` / `diff` / `clip` / `where` / `mask` / `abs` / `astype` / `fillna` / `to_numpy` / `tolist` / `np.maximum` / `np.full` / `pd.concat` / `pd.Series` / `pd.isna` / `math.*`) を 1 ファイルに詰めた probe が PASS。参照実装を含む `test_plugin.py` 形 (`range`/`sum`/`math`/`pytest.approx`/`np.isclose`/`from plugin import compute`) も `check_source(..., extra_allowed={"pytest","plugin"})` で PASS。**落ちるのは `to_frame` / `df.open` / `getattr` の 3 形**で、いずれも §4 の禁止事項で回避済み。
2. **新しい入力経路は無い。** 9 本は `compute(df, params)` の既存契約だけを使い、`config.yaml` の許可キー (`kind` / `outputs` / `params` / `max_bars`) の範囲に収まる。ニュース・ネットワーク・DB・ファイルのいずれにも触れない。
3. **新しい sink は無い。** 本束は `src/` を 1 行も変えないので、遮断 8 (人間の判断根拠が改善プロンプトに漏れない) の sink 一覧 ([wiring] §5 の 6 経路) は不変。9 本の docstring と test にも成績・期間・段名は書かない (書くと `read_example_plugin` 経由で agent に届く)。**指標の定義式そのものは秘密ではない**ので、参照実装を `test_plugin.py` に置いて agent が読めることは問題ない。
4. **改善ループの noop ゲートは自動的に強くなる。** `find_noop_copy` は改善ループの commit gate からのみ呼ばれ (`improve_loop.py:1525` が唯一の生産呼び出し元、人間の bless 経路 `switch._run_full_gate` は呼ばない)、比較対象は `_snapshot_src/_examples/` (= `docs/examples/plugins` のコピー) と配備済スナップショットの両方である。**配備後は 9 本のどれを逐語コピーしても `noop_copy_of:` で落ちる** — R8 の「改造は別名 + 実質変更」が既存機構だけで強制される。新規に足すものは無い。
5. **人間の bless は noop 判定も self-test 本数下限 (`min_test_functions: 3`) も受けない。** どちらも改善ループ側のゲートである。したがって「example の丸写しを bless できない」という懸念は**成立しない** (§8 の当初懸念は解消)。self-test を 3 本より多く書くのは品質のためであって bless の要件ではない。

## 6. 受入条件

| ID | 条件 | 観測方法 |
|---|---|---|
| **I1** | 9 本すべてが discover を通る | 既存の `tests/plugin/test_loader.py::test_discover_sample_plugins_directory_not_rejected` が `discover(docs/examples/plugins)` を実行済み (現状 3 本すべてが通ることを実測: `rsi_indicator`(indicator) / `rsi_pullback`(strategy) / `sma_cross`(strategy))。その assert を **9 名の集合 `<= names`** へ拡張する。**総数の厳密 pin は置かない** — 将来 example が 1 本増えただけで落ちる脆いテストになり、観測したい性質 (9 本が reject されない) と一致しない |
| **I2** | 9 本の戻り値が indicator 契約を通る | 各 `compute` の戻り値を `core.plugin_contract.validate_indicator_result(result, df_index=df.index, outputs=<config の outputs>)` に通して例外なし。キー集合完全一致・index 一致・Inf/bool 不在はこの 1 本で同時に観測される |
| **I3** | 参照実装と一致 | 各 `test_plugin.py` で、素朴なループの参照実装と**全行**比較。公差は `rel=1e-9, abs=1e-9` の**併用** (bollinger / ichimoku / macd は価格スケール、rsi / k / d / adx / di は [0,100] スケールなので片方だけでは不足)。NaN 行は「両方 NaN」で一致とする |
| **I4** | **固定 fixture における接頭辞一致** (prefix consistency) | **160 行の固定 fixture の全行 `t` (0 〜 159) について**、各出力キーで `compute(df.iloc[:t+1])[key].iloc[-1]` と `compute(df)[key].iloc[t]` が一致 (NaN は NaN 同士)。公差 `rel/abs 1e-9` (実測では正しい実装は**差 0.0**)。160 行は `ichimoku` の `senkou_b` warmup 51 + 余裕。**サンプル点ではなく全行**である理由と所要時間は §6.2。fixture は極値を末尾側と中間の両方に置く。**「lookahead の一般的な検出」ではない** — この試験が保証するのは「この fixture のこの入力列に対して、各行の出力が自分より後の行に依存していない」ことだけ (限界は §6.2) |
| **I5** | 先頭依存の誤差が、**仕様化したデータ範囲**で許容内 | **普遍的保証ではなく、下記 fixture 集合に限る経験的回帰試験** (§3.2 の解析的上界と対にして読む。上界式が使えるのは `ema` / `macd` / `atr` だけで、`rsi` / `adx` には**上界が無い** — §3.2 (i-b))。fixture の生成式・seed・値域を spec で固定する (下記)。その範囲で、末尾 400 本だけで計算した最終行の値が長い履歴 (5000 本) の値と一致すること。**公差はどちらも絶対値で置く**: 0〜100 スケールの出力 (`rsi` `k` `d` `adx` `plus_di` `minus_di`) は **`abs < 1e-6`**、価格スケールの出力 (`value` `upper` `middle` `lower` `atr` `macd` `signal` `hist` `tenkan` `kijun` `senkou_a` `senkou_b` `chikou`) は **`abs < 1e-9 × fixture の基準価格`** (基準価格 300 なら 3e-7、0.5 なら 5e-10)。**相対公差を使わない理由**: 残差は `|Δseed| ∝ 入力価格` に比例し、**出力値の大きさには比例しない**。`macd` / `hist` / `atr` は 0 を跨ぐ・0 に近づくので、相対公差にすると値が 1e-6 のとき許容が 1e-15 になり、実測残差 (5e-14) で偽陽性になる (v1.1 で同じ罠を I5 から外した経緯がある)。基準価格に比例させた絶対公差は `ema` / `bollinger` / `ichimoku` では相対 1e-9 と一致し、0 近傍の出力でも壊れない。fixture 集合: ①ランダムウォーク (基準価格 0.5 / 1.5 / 150 / 300、seed 0〜7) ②**明確な単調トレンド** (v1.3c で生成式を明記 — 下記) ③**401 本目 (= 400 本窓のちょうど手前) に基準価格の 60% 相当のスパイクを置いた系列** ④**退化 fixture (r2 I1 の反例)**: 通常データ 200 本 → 上向きの DM/TR を 1 本 → **`high == low == close` の完全横ばい 400 本**。このケースの公差は `rsi` が **`abs == 0`**、`adx` だけ **`abs < 1e-4`** (§3.2 (i-b) の実測 **4.31e-06** に対する余裕。**導出値ではなく観測値**で、ε をどう選んでも境界付近では残る。400 本まったく値幅ゼロという入力は実 FX データには現れない)。**なお v1.3b 追記: この退化 fixture では価格スケールの出力にも `1e-9 × 基準価格` を超える差が出る** — プランの逐語 fixture での実測で `bollinger` の `upper` / `lower` が **1.08e-06** (基準価格 150 なら公差 1.5e-7)。**退化 fixture にはランダムウォーク用の公差表を当てず、`1e-4` を全キーに適用する** (全キーの最大は `adx` の 4.31e-06) — ③で上界式と実測が整合すること (§3.2 の実測 B) を同時に観測する。**保証の前提は「indicator に 400 行以上が渡ること」= `min(strategy.max_bars, 400) == 400`**。<br>**fixture の生成式 (逐語、§3.2 の実測 A/B もこの式で σ = 基準価格 × 0.13%)**: `σ = 基準価格 × 0.001〜0.003`、`close = 基準価格 + cumsum(N(0, σ))` (`np.random.default_rng(seed)`)、`high = close + |N(0, σ/2)|`、`low = close − |N(0, σ/2)|`、**`open[0] = close[0]` / `open[t] = close[t−1]` (t > 0)** (= 前足の終値。v1.3d で実装に合わせて訂正 — 旧版は `open = close` と書いていたが、`open == close` の fixture は「close の代わりに open を読む」型の変異を素通りさせる。9 本のどれも `open` を読まないので数値結果は不変)、`volume = 1.0`、index = `pd.date_range(..., freq="1h", tz="UTC")`<br>**②単調トレンドの生成式 (逐語、v1.3c)**: `drift = 基準価格 × 1e-4` / 本、`close = 基準価格 + 向き × drift × t (+ ①のランダムウォーク項)`、`high` / `low` / `open` / `volume` / index は①と同じ、`n = 5000`、`seed = 2`。**上昇 / 下降 × `noise` 有無 × 基準価格 1.5 / 150 の 8 系列**。`n × drift` を基準価格の 50% に取るのは、**下降側でも価格が 0 を跨がないため** (跨ぐと `1e-9 × 基準価格` の絶対公差も `rsi` / `adx` の相対 ε 基準 `<= 1e-9·|close|` も意味を失う)。`noise` 無しは `close` が厳密に単調になり、`rsi` が ε 規則②に当たって上昇 100.0 / 下降 0.0 に張り付く (両側一致で `|Δrsi| = 0`)。公差は①と同じキー別の表 (実測の最大比は `bollinger` の 0.4%) |
| **I6** | 9 本を順に bless できる | tmp の `plugins` ディレクトリと tmp の DB に対して、9 本を 1 本ずつ `bless_candidate` に通し 9 回とも approval_id が返ることを実測。**noop / 同一性で弾かれないこと**と、`outputs_required` / `max_bars_limit` / `check_source` / pytest ゲートをすべて通ることを同時に観測する。実 `data/agentic.db` と実 `plugins/` は使わない (`tests/fixtures/wiring_envs.py` の tmp 環境の作り方に倣う) |
| **I7** | 新 `rsi` を宣言した strategy が E2E で動く | `rsi_pullback` 型の strategy (別名、tmp 環境) が `indicators: {rsi: {plugin: rsi, params: {period: 14}}}` を宣言し、`max_bars: 400` で A1 相当の E2E (resolve → lock → worker 実行 → `evaluate` が系列を受け取る) が通る。**`max_bars: 200` でも動くが I5 の保証外**であることを別ケースで観測する (落ちないことの確認であって、値の一致は要求しない) |
| **I9** (r2 I2) | **入力不変** と **反復決定性** | (a) **入力不変**: `compute` の呼び出し前に `df.copy(deep=True)` を取り、呼び出し後に `df.equals(before)` / 列集合 / index / dtype がすべて同一。(b) **反復決定性**: ① **fresh な df を 2 つ作って 2 回呼び、出力が一致** ② **同じ df オブジェクトで 2 回呼び、出力が一致** — ② が module レベルの状態持ち越しを捕まえる (`global` 文が無くても可変コンテナへの `append` は `check_source` を通る)。実測: 値を壊す添字代入 / 列を足す添字代入は (a) で赤、module state を持つ実装は (b)② と I4 の両方で赤 |
| **I8** | runbook が逐語再現でき、**途中失敗からの再開**も成立する | **観測の範囲 (v1.3c で明記)**: 対象は runbook の手順 **(1)〜(5)** のみ。**手順 (6)「service を再起動して起動ログに 9 本が載ることを確認」は人間の手動確認であり、自動テストの範囲外**である (runbook にも同じ注記を置いた)。CLI 境界 (`afx plugin bless <名前> --from _human` の受理・rc・stdout / stderr) は `entry.main` を in-process で呼ぶ 3 ケースで観測する — 正常系の rc=0 と `approval id=<N>`、ゲート失敗の rc=1 と stderr の `エラー: `、未終端 journal での再 bless が **CLI から traceback になる現状** ([cli-bless-unresolved-journal] の pin。ticket が直ったらこのテストを新しい振る舞いへ書き換える)。(a) tmp 環境で runbook のコマンド列をそのまま実行し、9 本が配備され `list_deployed_plugins` 相当の inventory に 9 本が `outputs` 付きで現れる。**コピー手順が `__pycache__` を巻き込んでも `check_candidate_snapshot` が無視すること** (`gate_pytest.py:61-64` で確認済) と、**bless 後も `plugins/_human/<名前>` が残る**ことを runbook のクリーンアップ行で扱う。(b) **部分配備・ゲート前失敗 (§6.3 (A))**: k 本目 (例: 5 本目) の `_human` 候補をわざと壊して bless を rc=1 にし、**k−1 本が配備済のまま残る**ことと、候補を直して k 本目から再開すると**最終的に 9 本揃う**ことを観測する。(c) **ゲート後失敗 (§6.3 (B)、r2 I3)**: `_advance_to_decided` の途中 (version 作成後・symlink 切替前) を monkeypatch で 1 回だけ失敗させ、**未終端 journal と pending approval が残る**こと → **次の同名 bless が `UnresolvedJournalError` になる** こと → **runbook の収束手順を逐語で実行**すると解け、同名で再 bless して配備が完了することを観測する。**状態遷移そのものは既存テストが pin 済み** (`tests/plugin/test_switch_journal.py` / `tests/plugin/test_reconcile.py`) — I8(c) はそれを**再実装せず参照**し、**「runbook に書いた手順がそのまま通る」ことだけ**を観測する (§6.1) |

### 6.1 テストの置き場と、それぞれが誰に回されるか

テストは 2 系統あり、**回す主体と回る回数が違う**ので分けて置く。

| 系統 | 置き場 | 誰が回すか | 載せるもの |
|---|---|---|---|
| 自己テスト | `docs/examples/plugins/<名前>/test_plugin.py` | ① 開発中の `uv run pytest` ② 配備時の bless (`switch._run_full_gate` → `run_gate_pytest`、subprocess + Landlock、`settings.plugin.pytest_timeout_sec: 300`) ③ 改善 agent が複製・改造したとき `run_plugin_tests` | I3 (参照一致) / **I4 (全 160 行の接頭辞一致)** / **I9 (入力不変・反復決定性)** / warmup 境界の pin / ゼロ除算と ε 規則の分岐 / params 検証の分岐 |
| 回帰テスト | `tests/` | `uv run pytest` のみ | I1 / I2 / I5 / I6 / I7 / I8 |
| **既存テスト (参照のみ、新規に書かない)** | `tests/plugin/test_switch_journal.py` / `tests/plugin/test_reconcile.py` | `uv run pytest` | **journal の状態遷移そのもの** (switched → retry_approval / revert / unrecognized、preparing のスキップ、per-row 分離)。I8(c) はこれらを**再実装せず**、runbook の手順が通ることだけを見る |

自己テストは **Landlock 下の subprocess で走る**ので、ファイル I/O・ネットワークは使えず、`check_source(..., extra_allowed={"pytest","plugin"})` を通る必要がある (§5.1 で PASS を実測)。**回すたびに全 9 本ぶんが走る**ので、I5 の「5000 本 × 40 seed」のような重い比較は自己テストに置かず、repo 側の回帰テストに置く。自己テストには軽い版 (数百本 × 2〜3 seed) を置く。

**I4 を「全行」にしても自己テストに置ける (所要時間の実測)**: 160 行 fixture の全行接頭辞検査は 9 本**合計 0.53 秒** (最悪の `adx` 単体で 0.224 秒、`sma` 0.011 / `ema` 0.008 / `rsi` 0.087 / `macd` 0.023 / `bollinger` 0.030 / `atr` 0.031 / `stochastic` 0.058 / `ichimoku` 0.058)。各 plugin の自己テストは**自分の 1 本だけ**を回すので 1 回あたり最大 0.22 秒で、`pytest_timeout_sec: 300` に対して桁で余裕がある。**間引く必要は無いので分担の見直しもしない** (全行を自己テストに置く)。300 行に伸ばしても合計 0.70 秒。

### 6.2 I4 の形の根拠と、**この試験の限界** (実測)

**(1) 末尾切り詰め → 接頭辞一致 (r1 I2)**。旧稿の I4 (末尾を k 本落として残り行が不変) は、**その切り詰めで参照値が動く未来参照しか捕まえられない**。わざと未来を読む実装を書いて当てた実測:

| 壊れた実装 | 旧 I4 (末尾切り詰め) | 接頭辞一致・8 点サンプル | **I4 (全 160 行)** |
|---|---|---|---|
| (a) `close.shift(-1)` を挟む | 赤 | 赤 | 赤 |
| (b) `rolling(..., center=True)` | 赤 | 赤 | 赤 |
| (c) 系列全体の min/max で正規化 | **緑 (見逃し)** ※ | 赤 | 赤 |
| (d) 行 0 の出力にだけ `close.iloc[1]` を代入 | **緑 (見逃し)** | 赤 | 赤 |
| (e) **行 37 (サンプル点でない) にだけ `close.iloc[38]` を代入** | 緑 | **緑 (見逃し)** | **赤** |
| (f) module レベルの list に `append` して出力に混ぜる | — | 赤 | 赤 (I9(b)② でも赤) |
| 正しい SMA / rolling std / EMA / Wilder / ADX / Bollinger | 緑 | 緑 | 緑 (**差 0.0**) |

※ (c) は「極値が削除される末尾側に**も**ある」fixture では旧 I4 でも赤になるが、極値を中間にだけ置いた fixture では素通りした。fixture の作り方次第で結果が変わる試験は不変条件の観測になっていない。(d) は「行 0 だけ未来を読む」= 末尾をいくら落としても行 0 の値は変わらないので旧 I4 では**原理的に**検出できない。

**(2) サンプル点 → 全行 (r2 I2)**。(e) が示すとおり、8 点サンプルは**サンプルされなかった行だけが未来を読む**実装を素通りする。全行なら必ず赤になる。所要時間 (§6.1) が問題にならないので**間引かない**。

**正しい実装での差が 0.0 になる理由**: 接頭辞 `df[:t+1]` は全履歴と**先頭を共有する**ので、`ewm` の再帰も `rolling` の逐次更新も同じ順序で同じ浮動小数演算を辿る。先頭依存 (§3.2) の誤差はここには混ざらない — それを測るのは I5 の役目で、2 つの試験は測っている次元が違う。公差 1e-9 は pandas の実装差に対する安全率であって、丸めを吸収するためのものではない。

**(3) この試験で原理的に観測できないもの (r2 I2、限界の明記)**。I4 はブラックボックスの出力比較なので、**「未来を読むが、読んだ結果が出力に現れない」形は検出できない**:
- 未来値を読んだうえで**同じ値を返す**実装 (読み込み自体は無害)。
- **warmup の NaN 行だけが未来に依存し、依存した結果やはり NaN を返す**実装 (出力が NaN で同一なので挙動差が無い)。
- 入力の一部を読まずに済ませる等、**出力に痕跡が残らない**あらゆる内部動作。

これらは**出力が同一である以上、消費者 (strategy / trade LLM / バックテスト) から見て挙動差が無く無害**である。「動的な未使用 future-read をすべて排除したこと」はブラックボックス試験では証明できない — I4 が主張するのは **「この fixture のこの入力列に対して、各行の出力が自分より後の行に依存していない」** ことだけで、`lookahead を一般に検出する` とは書かない。静的な排除が要るなら [indicator-reference-oracle-gate] (別束) か AST 検査の領分である。

**warmup 境界の pin の形**は既存 `rsi_indicator/test_plugin.py:46-48` に倣う — 「N 行目までは NaN」だけでなく「N+1 行目には値が入る」も見る (片側だけだと warmup が 1 本早く/遅く明ける変異を検出できない)。§3.1 の warmup 列の数値がその境界。

**I1 の載せ方**: 既存の `test_discover_sample_plugins_directory_not_rejected` は `discover(docs/examples/plugins)` を回して `{"rsi_indicator", "sma_cross"} <= names` を見るだけなので、**9 名を集合に足すだけ**にする (現行と同じ包含形)。**総数は pin しない** — example が 1 本増えただけで落ちる脆いテストになり、観測したい性質「9 本が reject されない」と一致しない (§6 I1 と同じ方針、r1 M1)。これにより「3 ファイル構成を崩した」「`config.yaml` の未知キーを書いた」が repo の通常テストで即座に落ちる。

I3 / I4 / I9 は各 plugin の `test_plugin.py` の中 (= bless の pytest ゲートが毎回回す)。I1 / I2 / I5 / I6 / I7 / I8 は repo 側 `tests/` の回帰テスト。

### 6.3 部分配備 — 9 本は束ではない (r1 I4)、失敗の 2 系統 (r2 I3)

**9 本は互いに独立であり、束としての原子性は要求しない。** `bless_candidate` は候補 1 本ごとに版作成 → live symlink 切替まで完了する (`switch.py:1772`, `:1818`, `:1905`) ので、5 本目が失敗しても先の 4 本は**有効な配備としてそのまま残る**。9 本を束で巻き戻す機構は無く、作らない (R2「新機構を作らない」)。

**失敗は 2 系統に分かれる (r2 I3)。runbook はこれを区別する。**

#### (A) ゲート前・ゲート中の失敗 — 何も残らない

`_run_full_gate` (`switch.py:953-1059`) の中で落ちる形: 候補ディレクトリの形 (`check_candidate_snapshot`)、discover、`outputs_required`、`check_source`、**pytest**、ゲート前後のハッシュ一致、`max_bars_limit`。**approval 行も journal も version ディレクトリも作られる前**なので、`bless_candidate` は `ValueError` / `SandboxError` を投げ、CLI が `エラー: ...` を stderr へ出して **rc=1**。ディスクも DB も変わらない。

→ **候補 (`plugins/_human/<名前>`) を直して、同じ名前から bless をやり直すだけでよい。** 初期セットの配備でまず出会うのはこちら (params ミス・テスト失敗など)。

#### (B) ゲート後の失敗 — journal / pending approval / `.versions/` が残る

ゲートを通った後、`bless_candidate` は **approval 行 + `preparing` journal を 1 tx で commit してから** `_advance_to_decided` を呼ぶ (`switch.py:1890-1909`)。`_advance_to_decided` は版ディレクトリ作成 → history 記録 → symlink 切替 → 決定、と進む (`switch.py:1198-1240`)。**この途中の I/O 失敗・切替失敗・hash 再照合失敗では、`.versions/<名前>/<hash>` / pending approval / 未終端 journal の一部または全部が残り得る。**

このとき**同じ名前の次の bless は必ず拒否される** — `bless_candidate` は未終端 journal を検出して `UnresolvedJournalError` を送出する (`switch.py:1810-1816`)。つまり **(A) の「候補を直して同じ名前から再開」は (B) では成立しない。**

**さらに CLI は この例外を捕捉していない**: `_plugin_bless` の except は `(ValueError, SandboxError)` だけ (`backtest/cli.py:589`)、`UnresolvedJournalError` は `Exception` 直系 (`switch.py:1572`) なので**利用者には Python traceback が出る** (`_plugin_retire` は同じ例外を捕捉している `cli.py:614` ので bless 側だけが不揃い)。本束では `src/` を直さないので、**runbook に「この traceback が出たら意味は『未終端 journal の検出』であり、下の収束操作へ進む」と明記する**。恒久修正は §1 非スコープの ticket 文案。

**収束させる既存の手段 (実コードで確認。新設しない)**:

| 手段 | 入口 | 何をするか |
|---|---|---|
| **`afx> approval retry <id>`** (サービスの対話シェル) | `commands.py:139-151` → `switch.retry_approval` → `approve_candidate` | **`preparing` / `versioned` / `recorded` を終端させる唯一の手段。** P2/P3 入口が頭から冪等に再実行する |
| **サービス再起動** (起動時 reconcile) | `service.py:802` → `switch.reconcile_switch_journals` (**これが唯一の呼び出し元。CLI 入口は無い**) | 行ごとに収束規則を適用: **`phase != "switched"` (preparing/versioned/recorded) は何もせず skip** (`switch.py:180-183` — FS 効果がまだ無いので、完了は次の approve / approval retry に委ねる)。**`phase == "switched"`** は live symlink の指す先で分岐 — 新 target なら `retry_approval` で完遂、旧 target (または `absent`) なら `_revert_one` で巻き戻し、どちらでもなければ activity ERROR (`switch_reconcile_unrecognized_live_target`) を書いて**触らず人間待ち** |
| `force_revert_op_id` | `reconcile_switch_journals` の引数のみ。**CLI / シェルの入口は無い** (Python からしか呼べない) | 指定 op_id を phase に依らず `_revert_one` |

**重要 (runbook に逐語で書く)**: **サービスを再起動しただけでは `preparing` の行は終端しない** (上表の skip)。→ **収束の第一手は `afx> approval retry <id>`**。`<id>` は失敗した bless が (stdout へ出す前に落ちていても) `approval_requests` に残っているので、対話シェルの `approval <id>` / `status` で確認する。

**approval id の入手 (逐語性の要)**: ゲート後に落ちた bless は `approval id=<N>` を stdout へ出す前に死んでいる可能性がある。**対話シェルには pending approval を列挙するコマンドが無い** (`status` は orders/mission だけ `commands.py:298-315`、`log` は `logs/agentic.log` の tail `commands.py:493-500`)。そこで **`afx plugin bless <名前> --from _human` をもう一度実行する** — 未終端 journal が残っていれば `UnresolvedJournalError` の**メッセージ自身が `op_id=` と `approval_id=` を含む** (`switch.py:1813-1816` 逐語: `plugin '<名前>': an unresolved switch journal (op_id=..., approval_id=...) blocks bless — resolve it first (reconcile or approval retry)`)。これが id を知る正規の手段。
どうしても DB を直接見る必要があれば読み取り専用で:
`sqlite3 -readonly data/agentic.db "SELECT op_id, name, phase, approval_id FROM plugin_switch_journal WHERE phase NOT IN ('decided','reverted')"`

**(B) の runbook 手順 (逐語)**:
1. traceback または `エラー:` を見て、**ゲート後の失敗かを判定** (`.versions/<名前>/` が増えている / 同じ名前で bless し直すと `UnresolvedJournalError` → (B))。
2. **同じ bless をもう一度実行**し、`UnresolvedJournalError` のメッセージから `op_id` と `approval_id` を読み取る (上記)。
3. サービスの対話シェルで `approval <approval_id>` を見て status を確認し、`approval retry <approval_id>` を実行する。
4. それでも解けない (phase が `switched` で live が第三者に触られている等) 場合は**サービスを再起動**し、`logs/activity.log` の `switch_reverted` / `switch_reconcile_unrecognized_live_target` / `plugin_reconcile_failed` を確認する。`unrecognized` が出ていたら**自動収束しない** — 人間が `plugins/<名前>` の symlink の状態を確認して判断する。
5. 同じ bless をもう一度実行して `UnresolvedJournalError` が出ないこと (= 未終端 journal が無い) を確認する。そのまま成功すれば配備完了。

#### 共通の規則

| 場面 | 何をするか |
|---|---|
| bless が rc=0 (`approval id=<N>` を stdout) | 次の 1 本へ進む |
| bless が rc=1 / traceback | **そこで停止する。既に配備済の分は巻き戻さない** (それらは単体で妥当な配備であり、戻す方がかえって状態を壊す)。(A) か (B) かを判定して上の手順へ |
| 停止後 | 配備済の名前を確認 → ((B) なら先に収束操作) → 失敗した候補の `plugins/_human/<名前>` を直す → **同じ名前から再開**する。先に成功した分は再実行しない — 再 bless しても害は無いが承認行だけが無駄に増える (実コード: `version_store.create_version_dir` は同 `artifact_hash` の版があれば作り直さず既存を返す冪等実装 `version_store.py:77-85`、`switch_required` も `old_target == new_target` なら False `switch.py:1888`。一方 `approvals_store.create` は無条件に走る `switch.py:1891-1893`) |
| 9 本完了後 | `plugins/_human/<名前>` を 9 本ぶん片付ける (bless は消さない。残すと後日の `afx plugin materialize <名前>` が `FileExistsError` になる、`switch.py:1576-1581`) |

**配備済名の確認方法 (実コードで確認した事実)**: **`afx` には plugin 一覧のサブコマンドが無い** — `plugin` のサブコマンドは `submit` / `bless` / `materialize` / `lock` / `retire` の 5 つだけ (`backtest/cli.py:126-156`)。運用チャネルの `status` も plugin を出さない (`commands.py:298-315`)。したがって runbook が指示する確認は次の 2 つ:
1. 各 bless の **rc と stdout の `approval id=<N>`** (1 本ごとの一次証跡)。
2. `ls -l plugins/` — 配備済は `<名前> -> .versions/<名前>/<artifact_hash>` の symlink として見える (`switch.py` の切替形)。`_human` / `_retired` / `.versions` は先頭 `_`/`.` なので discover 対象外。

配備済かどうかの**最終的な正**は「approved 行 + hash 一致 + discover 通過」を見る `tools.plugin_loader.approved_plugins` であり、これは service 起動時と改善 mission 開始時に評価される。runbook は「service を再起動して起動ログに 9 本が載ることを確認する」を最終確認として置く。



## 7. 変更ファイル一覧

**新規 (27 ファイル)**: `docs/examples/plugins/{sma,ema,rsi,macd,bollinger,atr,adx,stochastic,ichimoku}/{plugin.py,config.yaml,test_plugin.py}`

**新規 (1 ファイル)**: `docs/operations/indicator-initial-set-deploy-2026-09-19.md` — 配備 runbook。冒頭に **「暫定。[first-run-setup] (初回起動の対話ウィザード) が一括配備に置き換える。恒久なのは『採用には人間の明示的確認が要る、LLM の自動配備経路は作らない』という規律のほう」** を明記。手順は (0) 既存の配備名との衝突確認 → (1) コピー → (2) `afx plugin bless <名前> --from _human` → (3) rc の確認 (**rc=1 / traceback ならそこで停止、既配備分は巻き戻さない**。失敗が (A) ゲート前か (B) ゲート後かを判定し、(B) なら先に収束操作 — §6.3) → (4) `plugins/_human/<名前>` の後始末 → (5) 9 本ぶん繰り返し → (6) service 再起動による最終確認。**§6.3 (B) の収束手順 5 ステップを逐語で転記する**こと (I8(c) がこの逐語性を観測する)。**strategy 作者向けに「依存する strategy は `max_bars` を 400 以上に」の 1 行も runbook に置く** (§3.2)。

**変更 (1 ファイル)**: `tests/plugin/test_loader.py` の `test_discover_sample_plugins_directory_not_rejected` の包含集合に 9 名を追加 (I1、総数の pin は置かない)。

**新規テスト (1〜2 ファイル)**: I2 / I5 / I6 / I7 / I8 を載せる repo 側テスト (I8(c) の故障注入は `tests/plugin/test_switch_journal.py` / `tests/plugin/test_reconcile.py` の monkeypatch の形に倣い、状態遷移の pin はそちらを参照して二重に作らない) (`tests/plugin/test_indicator_initial_set.py` 等)。

**変更しないもの (明示)**: `src/` 配下すべて、`src/agentic_fx/loops/prompts/improve_mission.md`、`config/settings.yaml*`、既存の `docs/examples/plugins/{rsi_indicator,rsi_pullback,sma_cross}`、`tests/` の既存ファイル (上記 1 ファイルへの追記を除く)。

**prompt (規律 7) を変えない根拠**: 規律 7 は「配備済 indicator を `list_deployed_plugins()` で見て `indicators:` で宣言せよ」「無い指標は自前計算せず `不足指標:` として起票せよ」と書いてある。初期セットが配備されると `list_deployed_plugins()` の応答が 9 本増えるだけで、**文言が指す動作は何も変わらない** (「無い指標」が減るだけ)。「初期セットの 9 本」を prompt に列挙すると、配備状況と prompt 文言の二重管理が生まれ、片方だけ古くなる drift の土台になる — inventory を唯一の正に保つ。

## 8. 要裁定事項

| # | 論点 | 選択肢 | 推奨と根拠 |
|---|---|---|---|
| **D1** | 再帰平滑 (EMA / Wilder) の初期値 | (a) `y_0 = x_0` (pandas `adjust=False` 既定) (b) 先頭 p 本の SMA を seed にする教科書流 | **(a) を推奨。** 既存の配備済 `rsi_indicator` がこの再帰であり (`rsi_indicator/plugin.py:54-57`)、揃えないと同じ「RSI(14)」が 2 種類できる。§3.2 の収束見積りも (a) 前提。参考: `tests/runners/m30_fixture.py:28` には (b) の言い回し (「seed with SMA then avg=(prev*(p−1)+cur)/p」) が残っているが、これは記録済みの LLM への**タスク文**であって設計裁定ではない — 過度に重く見る必要はない。(b) を採る場合は §3.2 の収束表を取り直す |
| **D2** | `stochastic` の `k` の定義 | (a) `k = SMA(k_period) of raw %K` (slow stochastic、標準的な「14/3/3」の読み) (b) `k = raw %K`、`d = SMA(3)` (fast stochastic) | **(a) を推奨。** 「14/3/3」という 3 数字の表記は慣例的に (period / %K smoothing / %D smoothing) を指し、(b) だと 3 つ目の数字が余る。承認済 v0.1 は出力キーが `k` `d` としか書いていないので、ここは裁定で確定させたい。warmup も変わる (a: k=15,d=17 / b: k=13,d=15) |
| **D3** | 実 `plugins/` に `sma` / `rsi` / `adx` が既に配備されていないか | (a) 未配備 → 本設計どおり新規 bless (b) 既に配備済 → bless は**既存名の新版への切り替え**になり、その名前を pin して配備されている strategy が全部 pin 破れで inventory から外れる | **配備前に人間が確認すること**を runbook の手順 (0) に入れる。[wiring] の U6 (2026-09-14 裁定) で「実機 prerequisite の indicator 3 本 (sma / rsi / adx) を fable がひな形として `plugins/_human/` に作りユーザーが submit → approve」と決まっており、**それが実施済みなら名前が衝突する**。**実装プランの進捗表 (`docs/superpowers/plans/2026-09-14-indicator-consumption-wiring.md:10958`) は U6 を「未着手 / 本束の完了条件ではない / 実装完了後」と記録している**ので、(a) 未配備の可能性が高い。ただし本調査は規約により実 `plugins/` を読んでいないため、最終確認は人間が行う。衝突していた場合の選択肢は (i) 既存を retire してから bless (ii) 初期セット側の名前を変える (iii) 既存版の上に新版として bless し、pin 破れた strategy を再ロックする — (iii) が既存機構どおりで推奨だが、影響本数の実機確認が先 |
| **D4** | `max_bars` を全 9 本で 400 に揃えるか、指標ごとに最小値を選ぶか | (a) 一律 400 (b) 純 rolling 系は 120〜160、再帰平滑系は 400 | **(a) を推奨** (§3.2 の理由 2)。(b) は転送量・CPU で僅かに有利だが、strategy の `max_bars` との `min()` 規則により「一部の依存だけ静かに劣化する」形を作る。反対意見があれば裁定を |

### 8.1 裁定結果 (2026-09-19、指揮者裁定。いずれも可逆な定義選択で、推奨どおり)

| # | 裁定 | 根拠 |
|---|---|---|
| D1 | **(a) `y_0 = x_0`** (pandas `ewm(adjust=False)`) | 配備済 `rsi_indicator` と同じ再帰に揃える。`max_bars: 400` では初期値の違いは 1e-10 未満に減衰するので、(b) との差は消費側から観測できない |
| D2 | **(a) slow stochastic** — `k = SMA(k_period) of raw %K`、`d = SMA(d_period) of k`。params は §3.1 のとおり `period: 14` / `k_period: 3` / `d_period: 3` | 「14/3/3」の慣例的な読み (MT5 の Stochastic の %K period / Slowing / %D period と同じ構成)。fast が要る場合は strategy が `k_period: 1` を上書きすれば得られる (承認不要、R8) |
| D3 | **(a) 未配備** — 指揮者が 2026-09-19 に実 `plugins/` の一覧 (`rsi_indicator` / `rsi_wilder` のみ) と実 DB の承認 payload 名 (read-only) を確認済。9 名との衝突なし。runbook の手順 (0) の確認は残す (将来の再実行に備える) | [wiring] U6 の prerequisite 3 本は未着手のまま本束に吸収する |
| D4 | **(a) 一律 `max_bars: 400`** | §3.2 の理由 2。`min(strategy.max_bars, indicator.max_bars)` 規則の下で一部の依存だけが静かに劣化する形を作らない |

### 8.2 設計レビュー r1 の処置 (codex terra、`tmp/design-indicator-initial-set/codex-design-r1.md`、C0 / I4 / M2)

**6 件とも採用** (2026-09-19 指揮者裁定)。

| r1 | 指摘 | 処置 |
|---|---|---|
| I1 | `int(params.get(...))` は型検証でなく変換で、`14.9` / `"14"` を黙って受理する | §4 を書き換え。**元値の型を確認する**形 (`bool` でない `int`) に。`_int_param` / `_float_param` の骨格を掲載し、9 本が同じ十数行を持つ理由 (1 フォルダ 3 ファイル制約) も明記。相互関係は `macd` の `fast < slow` のみ要求 (退化と符号反転を塞ぐ)、`ichimoku` の順序は**要求しない** (パラメータ探索を塞がない)。`num_std` は有限 `> 0`。**例外時の 6 経路の振る舞い**を実コードで確認して表に (承認・バックテストは fail closed、producer / `get_indicators` は fail open だが誤値が発注に届く経路なし) |
| I2 | I4 (末尾切り詰め不変) は任意の未来参照を検出しない | I4 を**接頭辞因果性**に置換。壊れた実装 4 種で実測し、旧 I4 が (c) 全体 min/max 正規化 (fixture 次第) と (d) 行 0 だけ未来参照を**見逃す**こと、新 I4 が 4 種すべてで赤になり正しい実装では誤差 0.0 になることを §6.2 に記録 |
| I3 | `max_bars=400` の 1e-6 は受入データ集合から導けない | §3.2 を (i) 解析的上界 `|Δseed|·(1−α)^(N−p)` と (ii) 仕様化データ範囲の回帰試験の 2 段に再構成。**※ このとき書いた ADX の入れ子上界 (1.07e-10) は v1.3 (r2 I1) で撤回済み** — 単段の線形再帰にしか上界式は成立しない。I5 は「普遍的保証ではなく回帰試験」と明記し、fixture の値域・生成式・seed・401 本目スパイクを仕様化 |
| I4 | 9 本の逐次 bless 失敗時の部分配備が未規定 | §6.3 を新設。**束としての原子性は要求しない**、失敗したらそこで停止し既配備分は巻き戻さない、同じ名前から再開、完了後に `_human` を片付ける。`afx` に plugin 一覧のサブコマンドが**無い**ことを実コードで確認し (`cli.py:126-156`)、確認方法を rc / `ls -l plugins/` / service 再起動ログの 3 つに具体化。I8 に部分配備からの再開ケース (b) を追加 |
| M1 | I1 の総数 pin 方針が §6 と §6.1 で矛盾 | §6.1 を「集合包含のみ、総数は pin しない」に統一 |
| M2 | §1 の「`tests/` を変更しない」が §7 と矛盾 | §1 を「`src/` は変更しない。`tests/` は §7 の既存 1 ファイルへの追記と新規ファイルのみ」に訂正 |

### 8.3 設計レビュー r2 の処置 (codex terra、`tmp/design-indicator-initial-set/codex-design-r2.md`、C0 / I3 / M0)

**3 件とも採用** (2026-09-19 指揮者裁定)。

| r2 | 指摘 | 処置 |
|---|---|---|
| I1 | ADX の解析的上界は成立しない (比が非線形な除算を通るので外側 Wilder の入力列自体が変わる)。反例 = 窓の直前に DM/TR 1 本 → 400 本の完全横ばい | **上界式を `ema` / `macd` / `atr` (単段の線形再帰) だけに限定し、ADX の上界と「実測と一致」の主張を撤回** (§3.2 (i))。反例を**実測で再現** (規則なしで `|Δadx|` = 22.37 / `|Δrsi|` = 11.82)。**同じ形が `rsi` にもある**ことを確認し、`stochastic` は rolling の有限記憶なので無関係 (Δ = 0.0) と全数確認。**相対 ε 規則**を §3.1 / §3.2 (i-b) に新設 (`adx`: `atr <= 1e-9·|close|` → DI = 0、`rsi`: `avg_gain + avg_loss <= 1e-9·|close|` → 50)。ε は実測で決定 (1e-6 は USDJPY 5m に 7 倍しか余裕が無く誤発火、1e-9 なら 7.0e+03 倍)。**ε でも `adx` は 1e-6 に届かない (v1.3a まで 5.2e-06 と記載 → v1.3b でプランの逐語 fixture での観測値 **4.31e-06** に訂正。いずれも公差 1e-4 の内側で対応内容は不変)** ことを正直に書き、I5 に退化 fixture ④ を追加して `adx` だけ公差 `1e-4` (観測値) とした。既存 `rsi_indicator` との値の差異を §0 R5a と §3.1 に明記 |
| I2 | I4 は 8 点サンプルで「lookahead の一般的な検出」にならない。純関数規約 (入力不変・反復決定性) が受入条件で観測されない。添字代入は `check_source` を通る | (a) I4 を**「固定 fixture における接頭辞一致」**に改題し、「一般的な検出」の表現を全削除。(b) **160 行の全行検査**に変更 — 所要時間を実測 (9 本合計 0.53 秒、最悪 `adx` 0.224 秒、`pytest_timeout_sec: 300` に桁で余裕) し**間引かない**と決定。「行 37 だけ未来を読む」実装でサンプル検査が緑・全行が赤になることを実測。(c) **I9 (入力不変・反復決定性) を新設**。添字代入が `check_source` を通る事実を §4 に記載し、**本番は `worker.py:318` の deep copy で既に守られている**ので I9 は衛生検査である、と誇張せず書いた。(d) 観測不能な形 (未来を読むが同じ値を返す / NaN 行だけ未来依存で NaN を返す) を §6.2 (3) に限界として明記 |
| I3 | 部分配備の再開規則が、ゲート後に失敗した bless の状態 (journal / pending approval / `.versions/`) を扱えていない。CLI は `UnresolvedJournalError` を捕捉しない | §6.3 を **(A) ゲート前・ゲート中 (何も残らない) / (B) ゲート後 (残る)** の 2 系統に分割。**収束手段を実コードで確認**: `reconcile_switch_journals` の呼び出し元は `service.py:802` (起動時) **のみ**で CLI 入口は無く、しかも **`preparing` 行は skip する** (`switch.py:180-183`) ので**再起動だけでは終端しない**。終端させる唯一の手段は対話シェルの **`afx> approval retry <id>`** (`commands.py:139-151`)。`force_revert_op_id` は引数のみで入口なし。(B) の 5 ステップ手順を逐語化し I8(c) を追加。**CLI の未捕捉 traceback は `src/` を直さず §1 非スコープに ticket 文案**として起票 |

### 8.4 設計レビュー後の変更 (実装レビュー 2 周目、2026-09-19)

| # | 変更 | 由来 | 状態 |
|---|---|---|---|
| **C2** | **9 本の plugin が未知の `params` キーを `ValueError` にする** (§4 の規則表に 1 行、既定値の単一出所 `_DEFAULTS` を明文化) | 実装レビュー 2 周目 codex terra Important (`tmp/review-20260919-iis/r2/codex/codex-1.md`)、2026-09-19 指揮者裁定で採用、**ユーザーへ提示済** | **設計変更 (締め付け方向)**。異論が出たら revert できるよう commit 列を分離 (plugin 9 本 + 自己テスト / 受入テスト / 本節) |

**C2 の影響の全数確認 (実コードで確認、2026-09-19)**: indicator の `compute` に `params` が届く経路は 2 つだけ。(i) strategy 依存 — `resolve._merge_params(dep.params, ref.params)` (`resolve.py:185`) が indicator の config params に strategy 側の上書きを 1 段重ねる。**未知キーはここからしか入らない** (= 本件の動機そのもの)。(ii) standalone — `market_tools.py:85` が `params: meta.params` を渡す (indicator 自身の config なので、config に余計なキーがある場合のみ。受入テストがそれを落とす)。**ハーネスが内部キーを足す経路は無い** — `worker.py:315,319` は `_deep_copy_json` で写すだけ、`sandbox.py:306-308` の handshake も `("df", "params")` のみ。既存 example (`rsi_indicator` / `sma_cross` / `rsi_pullback`) と `tests/fixtures/indicator_wiring.py` の rsi fixture は**本束の対象外なので変更しない**。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-19 | v1.3e | **設計変更 1 件 (締め付け方向、ユーザー提示済)。** **§4**: params の規則表に「**未知キーは `ValueError`**」を追加し、既定値の出所を `plugin.py` の `_DEFAULTS` 1 箇所に定める (受入テストは `config.yaml` の `params` と `_DEFAULTS` を**型込みで**比較)。**§8.4** を新設し、影響の全数確認 (params が `compute` に届く 2 経路と、ハーネスが内部キーを足さないこと) を実コードの行番号つきで記録。動機は「出力比較が弱い」ではなく**本番の穴** — strategy 側の params 上書きは承認不要なので、綴り誤りが黙って既定値で動くと、その値を前提にした backtest 結果が出る | 2 周目 codex terra Important (`tmp/review-20260919-iis/r2/codex/codex-1.md`)。2026-09-19 指揮者裁定で採用、ユーザーへ提示済。**異論が出たら revert できるよう commit 列を分離** | `a97b3bc` (plugin 9 本 + 自己テスト) / `e2214aa` (受入テスト) / 本行の docs commit |
| 2026-09-19 | v1.3d | **設計変更なし — 記述の訂正 1 件。** **§6 I5** の fixture 生成式①の `open` を `open = close` から **`open[0] = close[0]` / `open[t] = close[t−1]` (t > 0)** (前足の終値) へ訂正。実テスト (`_df` / `_spike_df` / `_trend_df` / `_degenerate_df`) と 9 本の自己テスト `_mkdf` はいずれも前足 close を入れており、**spec の文言だけが実態に追いついていなかった**。`open == close` の fixture は「close の代わりに open を読む」型の変異 (M-sma-4) を素通りさせるので、実態側が正しい。9 本のどれも `open` を読まないため数値結果は不変、**コードは 1 行も変えない** | 2 周目 codex terra Minor (`tmp/review-20260919-iis/r2/codex/codex-1.md`)。1 周目トリアージ §4-6 で「コードを spec に寄せる是正はしない」と申し送り済 | 本行の docs commit |
| 2026-09-19 | v1.3c | **1 周目レビュー (codex terra 2 束) の処置。設計変更ゼロ — 主張を狭める / 明文化のみ。** ①**§6 I5**: fixture ②「明確な単調トレンド」の**生成式・seed・値域・8 系列の組み合わせ**を逐語で明記 (受入テストに②が無く、持続的 drift での先頭依存を観測できていなかった — 束 2 Important)。②**§6 I8**: 観測の範囲を明記 — 対象は runbook 手順 (1)〜(5) のみで、**(6) の service 再起動とログ目視は手動確認・自動テストの範囲外**。あわせて CLI 境界 (`afx plugin bless --from _human` の rc / stdout / stderr / `UnresolvedJournalError` の traceback) を 観測対象として明記 (束 2 Important。runbook 側にも同じ注記)。③**§4**: docstring 必須項目の見出しが「5 項目」で本文が 6 項目だったのを訂正し、**`config.yaml` の `params` == `plugin.py` の既定値**を 1 行明文化 (段 0 が足した pin の裁定、指揮者裁定で採用済)。なお束 1 Important (9 本の docstring が `max_bars: 400` の根拠を過大に説明) は **plugin 側の記述を設計書 §3.2 に合わせる**是正で、設計書の変更を伴わない | codex 1 周目 `tmp/review-20260919-iis/r1/codex/codex-{1,2}.md` (Critical 0 / Important 4)、2026-09-19 指揮者裁定で 4 件とも採用 | `c734f77` (§6 I5) / `5798e05` (§6 I8) / 本行と §4 は直後の docs commit |
| 2026-09-19 | v1.3b | **観測値の訂正 + 既知の欠落の起票。設計変更は 1 件のみ (下記 ①後段、**指揮者裁定 2026-09-19 で恒久化** — 原因は完全横ばい区間の rolling 標準偏差 (真値 0) の丸め誤差が窓長に依存することで plugin ロジック由来ではなく、退化 fixture の目的は ε 規則の観測であって bollinger の精度ではない。通常 fixture の公差は不変)。** ①**§3.2 (i-b)**: 退化 fixture の `\|Δadx\|` を **5.2e-06 → 4.31e-06** に訂正 (本文・表・§6 I5・§8.3 対応表)。前段データの作り方に依存する**観測値**で、起草時の ad hoc fixture と実装プランの逐語 fixture (プラン T0 / T10 Step 10-c `_degenerate_df`) で値が変わる。**I5 の公差 1e-4 はどちらも覆う**ので結論は不変。ε 掃引表は ε どうしの相対比較なので起草時の値のまま残し、注記で「絶対値として引くのはプランの fixture での 4.31e-06」と明示。あわせて**同じ退化 fixture で `bollinger` の `upper`/`lower` が 1.08e-06 になる** (価格スケール公差 1.5e-7 を超える) ことを §6 I5 に追記し、**退化 fixture にはランダムウォーク用の公差表を当てず `1e-4` を全キーに適用する**とした。**これは受入条件 I5 の緩和なので「設計変更 1 件、暫定・指揮者裁定待ち」として申告する** — 原因は完全横ばい区間の rolling 標準偏差 (真値は厳密 0) が pandas の逐次更新で窓長に依存した丸め誤差を出すことで plugin ロジック由来ではなく、影響は退化 fixture 1 本のみ (ランダムウォーク / スパイクの公差は無変更、実測最大 3.104e-10)。②**§0 R5a / §1 非スコープ**: **配備済 `rsi_indicator` / `rsi_wilder` を退役させる CLI 経路は存在しない** (`retire_plugin` は plain 専用で symlink 配備を拒否、依存 strategy も検査しない) ことを実コードで確認して追記し、**[retire-symlink-deployed-plugin]** の起票文案を追加。本束では新 `rsi` と併存させる | T10 着手前検証 (2026-09-19、`tmp/plan-indicator-initial-set/prevalidation-T10.md`) の実測。プラン v1.0「着手前検証の記録 §8」で指揮者裁定待ちだった 2 件の処置 | — |
| 2026-09-19 | v1.3a | §0 R5a に**ユーザー見解**を 1 文追記 (0〜100 の尺度では 50 が中立点なので新定義の方が分かりやすい)。**設計変更なし — 既に確定している新 `rsi` の定義の根拠を補強しただけ** | 2026-09-19 ユーザー見解 | — |
| 2026-09-19 | v1.3 | 設計レビュー r2 の 3 件を全採用 (§8.3 に対応表)。**§3.2** ADX の解析的上界を撤回し上界式を `ema`/`macd`/`atr` に限定、比を取る指標 (`rsi`/`adx`) の退化を実測で再現 (規則なしで Δadx=22.4) して**相対 ε 規則 (1e-9)** を新設、**§3.1/§0 R5a** に既存 `rsi_indicator` との値の差異を明記、**§6 I4** を「固定 fixture の全 160 行の接頭辞一致」へ (主張を狭め + サンプル → 全行、所要 0.53s を実測)、**I9 (入力不変・反復決定性) を新設**、**§6.2 (3)** に観測不能な形の限界、**§6.3** を (A) ゲート前 / (B) ゲート後の 2 系統に分割し収束手段を実コードで確定 (+ I8(c))、**§1** に CLI の `UnresolvedJournalError` 未捕捉の ticket 文案 | codex terra 設計レビュー 2 周目 `tmp/design-indicator-initial-set/codex-design-r2.md` (Critical 0 / Important 3 / Minor 0)、2026-09-19 指揮者裁定で 3 件とも採用 | — |
| 2026-09-19 | v1.2 | 設計レビュー r1 の 6 件を全採用 (§8.2 に対応表)。**§4** params 検証を「変換」から「型の確認」へ書き換え + helper 骨格 + 例外時 6 経路の振る舞い表、**§6 I4** を末尾切り詰め不変 → **接頭辞因果性**へ置換 (+ §6.2 に壊れた実装 4 種の red 実測)、**§3.2** を解析的上界 + 仕様化データ範囲の 2 段に再構成し **I5** を回帰試験と明記、**§6.3** 部分配備の規則を新設 (+ I8 に再開ケース)、§6.1 の総数 pin 矛盾と §1 の `tests/` 記述を訂正 | codex terra 設計レビュー 1 周目 `tmp/design-indicator-initial-set/codex-design-r1.md` (Critical 0 / Important 4 / Minor 2)、2026-09-19 指揮者裁定で 6 件とも採用 | — |
| 2026-09-19 | v1.1 | §8.1 裁定結果 D1〜D4 を追記 (いずれも推奨どおり: `y_0 = x_0` / slow stochastic、fast は `k_period: 1` の上書きで得る / 9 名は未配備を確認 / `max_bars` 一律 400) | 指揮者裁定 (可逆な定義選択)。D3 は実 `plugins/` 一覧と実 DB の read-only 確認 | — |
| 2026-09-19 | v1.0 | 初稿。ユーザー承認済 設計 v0.1 (R1〜R8) をコードベースの実測で裏取りし spec 化。noop ゲートが人間 bless 経路に掛からないこと・9 指標の API が `check_source` を通ること・`max_bars` 400 の収束見積り・warmup 本数を実測で確定。要裁定 D1〜D4 を起票 | 設計 v0.1 承認 (2026-09-19)、[indicator-consumption-wiring] 完了後の次束 | - |

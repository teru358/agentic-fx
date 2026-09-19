# [indicator-initial-set] 設計書 v1.1

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
| R6 | 正しさの担保 | 各指標の自己テストに (i) 独立参照実装 (素朴なループ) との一致 (ii) 末尾切り詰め不変 (iii) warmup 期間が NaN (iv) 出力キー集合が `outputs` と完全一致・index 一致・Inf/bool なし。**ゲートとしての一般化 [indicator-reference-oracle-gate] は別束のまま** |
| R7 | 名前 | 短い一般名 (`sma` `ema` `rsi` …)。既存の配備済 `rsi_indicator` / `rsi_wilder` と example の `rsi_indicator` (example 戦略 `rsi_pullback` が依存) は**触らず残す** |
| R8 | 調整と改造 | 調整 = strategy 側の `indicators.<alias>.params` 上書き (承認不要、[wiring] U3)。改造 = `read_plugin_source` → staging 複製 → **別名** → 承認申請 |

## 1. スコープと非スコープ

**スコープ**: 9 本の plugin 3 点セットの新規追加 (`docs/examples/plugins/`)、その self-test、repo 側の回帰テストへの載せ方、人間配備 runbook 1 本。

**非スコープ (明示)**:
- **[indicator-reference-oracle-gate]** — 「参照実装との一致」をハーネス側のゲートとして一般化すること。本束では各 plugin の `test_plugin.py` の中の話に留める。
- **[multi-timeframe-indicator-deps]** — 複数足の依存宣言。9 本はいずれも `timeframe` を宣言せず、呼び出し側が渡した任意の足で動く。
- **[first-run-setup]** — 初回起動の対話ウィザードによる一括配備。本束の runbook はそれに置き換えられる暫定物。
- **配備の自動化** — `afx plugin bless-all` のようなコマンドも、改善ループからの自動配備経路も作らない。
- **`src/` と `tests/` の既存コードの変更** — 本束は既存の契約・ゲート・CLI を一切変えない (§5 / §7)。
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
- **参照実装の書き方** (`test_plugin.py` の中): pandas のベクトル演算を一切使わず、`list[float]` に対する `for` ループで定義式をそのまま書き下す。`rolling` は毎回スライスして `sum(window)/p`、再帰平滑は `prev` を持ち回る 1 行、`min_periods` は「先頭 p−1 個を `float("nan")` にする」で表現する。plugin.py と**同じ関数を別の書き方で 2 度書く**のが目的なので、plugin.py からの import・コピーは禁止。

### 3.1 定義表

`p` は `params` の値。warmup 列は「値が入る最初の 0 起点行番号」(実測、§6 I3 の pin 対象)。

| plugin | outputs | 定義 | 既定 params | warmup (最初に値が入る行) | ゼロ除算 |
|---|---|---|---|---|---|
| `sma` | `value` | SMA(p) of close | `period: 20` | 19 | なし |
| `ema` | `value` | 再帰平滑 α=2/(p+1) of close | `period: 20` | 19 | なし |
| `rsi` | `rsi` | `delta = close.diff()`、`gain = max(delta,0)`、`loss = max(−delta,0)`、`avg_* = Wilder(p)`、`rsi = 100 − 100/(1 + avg_gain/avg_loss)` | `period: 14` | 14 | `avg_loss == 0 かつ avg_gain > 0` → **100**、`avg_gain == avg_loss == 0` → **50** (既存 `rsi_indicator` と同一規則) |
| `macd` | `macd` `signal` `hist` | `macd = EMA(fast) − EMA(slow)`、`signal = 再帰平滑(α=2/(signal_period+1)) of macd`、`hist = macd − signal` | `fast: 12, slow: 26, signal_period: 9` | macd 25 / signal 33 / hist 33 | なし |
| `bollinger` | `upper` `middle` `lower` | `middle = SMA(p)`、`σ = rolling(p).std(ddof=0)` (母標準偏差)、`upper = middle + k·σ`、`lower = middle − k·σ` | `period: 20, num_std: 2.0` | 19 (3 本とも) | σ=0 は除算しないので `upper == middle == lower`。異常ではない |
| `atr` | `atr` | `TR = max(high−low, |high−prev_close|, |low−prev_close|)` (行 0 は NaN)、`atr = Wilder(p) of TR` | `period: 14` | 14 | なし |
| `adx` | `adx` `plus_di` `minus_di` | `up = high.diff()`、`dn = −low.diff()`、`plus_dm = up if (up>dn and up>0) else 0`、`minus_dm = dn if (dn>up and dn>0) else 0` (行 0 は両方 NaN)、`atr = Wilder(p) of TR`、`±DI = 100·Wilder(p) of ±DM / atr`、`dx = 100·|plus_di−minus_di|/(plus_di+minus_di)`、`adx = Wilder(p) of dx` | `period: 14` | plus_di 14 / minus_di 14 / adx **27** | `atr == 0` → `plus_di = minus_di = 0.0`。`plus_di + minus_di == 0` → `dx = 0.0` |
| `stochastic` | `k` `d` | `hh = rolling(p).max(high)`、`ll = rolling(p).min(low)`、`raw = 100·(close−ll)/(hh−ll)`、**`k = SMA(k_period) of raw`**、`d = SMA(d_period) of k` (= 標準的な slow stochastic、裁定 D2) | `period: 14, k_period: 3, d_period: 3` | k 15 / d 17 | `hh == ll` → `raw = 50.0` |
| `ichimoku` | `tenkan` `kijun` `senkou_a` `senkou_b` `chikou` | `mid(n) = (rolling(n).max(high) + rolling(n).min(low))/2`、`tenkan = mid(tenkan_period)`、`kijun = mid(kijun_period)`、`senkou_a = (tenkan + kijun)/2`、`senkou_b = mid(senkou_b_period)`、`chikou = close` | `tenkan_period: 9, kijun_period: 26, senkou_b_period: 52` | tenkan 8 / kijun 25 / senkou_a 25 / senkou_b 51 / chikou 0 | なし |

**ichimoku の lookahead 禁止 (R5)**: `senkou_a` / `senkou_b` に `shift(+26)` を掛けない。`chikou` に `shift(−26)` を掛けない。**返すのはすべて「そのバー時点で確定している値」**であり、「26 本先へ投影した雲」でも「26 本前へ遡らせた遅行線」でもない。docstring に逐語で書く: *「雲との比較をしたい strategy は `indicators["ichi"]["senkou_a"].shift(kijun_period)` を自分で取ること。この plugin は未来の行に値を置かない (末尾切り詰め不変の担保でもある)」*。

### 3.2 `max_bars` の決め方 — 全 9 本で 400

**先頭依存 (head dependence)**: 再帰平滑は `y_0 = x_0` から始まるので、履歴の**先頭**を切ると最終行の値が変わる。誤差は `|Δy_0|·(1−α)^(N−p)` のオーダーで減衰し、ADX はこの減衰が 2 段 (DI → DX → ADX) 入れ子になるため最も遅い。

実測 (合成ランダムウォーク、初値 150・σ=0.15/本、末尾 N 本で計算した最終行の値と全 5000 本で計算した値の差):

| N | ema20 | rsi14 | macd | atr14 | **adx14 (40 seed の最悪)** |
|---|---|---|---|---|---|
| 100 | 3.9e-06 | 4.2e-02 | 1.5e-05 | 4.2e-06 | — |
| 200 | 1.4e-09 | 1.1e-05 | 2.4e-07 | 3.1e-08 | — |
| 250 | 7.0e-12 | 8.7e-07 | 4.3e-09 | 6.1e-11 | **7.4e-06** |
| 300 | 2.8e-14 | 2.8e-08 | 4.5e-11 | 4.1e-11 | **1.6e-07** |
| 350 | — | — | — | — | **5.6e-09** |
| **400** | 0 | 1.4e-11 | 5.2e-14 | 1.2e-15 | **1.1e-10** |

純 rolling の 4 本 (`sma` / `bollinger` / `stochastic` / `ichimoku`) は原理的に先頭非依存だが、**厳密な bit 一致ではない**: pandas の `rolling(...).std()` は逐次更新アルゴリズムなので丸めの累積が履歴長に依存し、実測で ~3e-11 の差が残る (`sma.mean()` は ~3e-14)。これは「先頭依存」ではなく浮動小数の丸めであり、許容誤差の形 (§6 I5) を絶対等値でなく公差にする根拠である。

**選定: 全 9 本で `max_bars: 400`。** 理由:
1. 最も収束が遅い `adx` でも 1.1e-10 < 1e-6 に収まり、他は桁で余裕がある。
2. **人間の配備者と strategy 作者が覚える数が 1 つで済む。** 指標ごとに 120/160/400 と散らすと、`max_bars: 200` の strategy が「`sma` は正確だが `adx` だけ静かに 7e-06 ずれる」という、テストで気づきにくい部分的劣化を生む (§2.4 の `min(...)` 規則のため)。
3. 400 は `max_bars_limit` 既定 1000 の 4 割で、resolver の `over_max_bars_limit` にも handshake 総量上限にも届かない。

**strategy 作者への帰結 (docstring と runbook に明記)**: *「これらの indicator に依存する strategy は自分の `max_bars` を依存先の `max_bars` 以上 (= 400 以上) に宣言すること。小さく宣言しても動くが、渡る履歴が短くなり値がわずかにずれる。」* 既存 example の `rsi_pullback` は `max_bars: 200` なので、新 `rsi` を宣言すると**動きはするが §6 I5 の保証範囲の外**になる (表の N=200 で 1.1e-05)。I7 ではこの点を明示して扱う。

### 3.3 参照実装の骨格 (`test_plugin.py` の中)

参照実装は「同じ式を 2 度、違う書き方で書く」ためのものなので、**pandas の集約 API を一切使わない**。使うのは `list` / `for` / `sum` / `len` / `float("nan")` / `math` だけ。3 つの基本部品で 9 本すべてが書ける:

- `_ref_sma(values, p)` — `i < p-1` は NaN、以降は `sum(values[i-p+1:i+1]) / p`。
- `_ref_recursive(values, alpha, p)` — **位置ではなく「非 NaN の観測数」で数える**。`prev` は**最初の非 NaN 値**で seed し、NaN の要素は `prev` を更新せず出力も NaN、非 NaN のたびに `prev = alpha*x + (1-alpha)*prev` してカウンタを 1 増やし、**カウンタが p 未満の間は出力 NaN**。これが pandas の `ewm(adjust=False, min_periods=p, ignore_na=False)` の意味論であり、**§3.1 で `atr` の warmup が 13 ではなく 14、`adx` が 27 になる理由**でもある (行 0 の TR / ±DM を NaN にしたぶん、確定が 1 本後ろへずれる)。位置で `i < p-1` と書くと 1 本早く明けて I3 が `atr` / `adx` の 2 本で落ちる。
- `_ref_rolling_minmax(values, p, op)` — `min`/`max` を素のスライスで取る。

`_ref_std` (bollinger) だけは `sqrt(sum((x - mean)**2) / p)` を素で書く (`ddof=0`)。pandas の逐次更新アルゴリズムとは丸めの経路が違うので、ここが I3 の公差 (rel/abs 1e-9) を必要とする主因になる。

**参照実装を `test_plugin.py` に置くことの可否**: 置いてよい。`read_example_plugin(name="sma")` は改善 agent に 3 ファイルすべてを見せるので、参照実装も agent から読める。指標の定義式は秘密ではなく、むしろ agent が「この plugin が何を計算しているか」を曖昧さなく知れるのは望ましい。遮断 8 が守るのは成績・holdout・人間の判断根拠であって数式ではない (§5.3)。

## 4. 共通契約 (全 9 本の docstring と params 検証)

**モジュール docstring の必須 5 項目** (`rsi_indicator` の 2 項目を拡張):
1. warmup はこの関数の責務。行が足りなくても「返さない」はできない (宣言 `outputs` と完全一致するキー集合が毎回要る)。足りない期間は NaN。消費側は `pd.isna` を見て hold する。
2. 1d 足のバケット境界は UTC 00:00 (epoch 錨) であり FX の取引日境界 (NY 17:00) ではない。この plugin は `timeframe` を宣言しないので任意の足で使われ得る。
3. **`max_bars: 400` の意味と、依存する strategy が `max_bars` を 400 以上に宣言すべき理由** (§3.2)。
4. **純関数であること** — I/O・乱数・実時計・グローバル状態の書き換えは禁止 (サンドボックスが AST で遮断する)。`df` と `params` を書き換えない。
5. (ichimoku のみ) 先行/遅行スパンの lookahead 規約 (§3.1)。

**params の型と範囲の検証**: **例外を投げず、不正なら出力を全 NaN にする**方針は採らない。逆に、**不正な params は `ValueError` で即座に失敗させる**。理由: `params` は loader が JSON-safe 性しか見ない (`loader._check_json_safe`) ので、型・範囲の責任は plugin にある。全 NaN で黙って返すと、strategy 側は「warmup 不足」と区別できず、改善 agent は「系列が全 NaN → `max_bars` を増やす」という誤った申し送りに誘導される (規律 7 の文言がまさにそう指示している)。例外なら worker が `{"ok": false, "error": "ValueError: ..."}` として構造化報告し、self-test / `run_plugin_tests` / bless の pytest ゲートのいずれかで必ず露見する。

各 plugin は冒頭で共通の形の検証を行う:
- `period` 系は `int(params.get(...))` した結果が **1 以上** (`macd` は `fast < slow` も要求)。
- `bollinger` の `num_std` は **有限かつ 0 以上の float**。
- bool は int のサブクラスなので `isinstance(x, bool)` を先に弾く。

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
| **I4** | 末尾切り詰め不変 | `df` の末尾を k ∈ {1, 5, 17} 本落として再計算し、残った行の値が全出力キーで **bit 一致** (差が厳密に `0.0`。実測: ewm / wilder / rolling std のいずれも k=1/5/50 で 0.0 — 因果的な実装なら丸めの経路まで同じになる)。NaN 行は「両方 NaN」で一致とする。ichimoku の lookahead 禁止 (R5) はこのテストで観測される |
| **I5** | 先頭依存の誤差が許容内 | `max_bars`(=400) 本以上の履歴を与えたとき、末尾 400 本だけで計算した最終行の値が、長い履歴 (5000 本) で計算した値と **`pytest.approx(full, rel=1e-6, abs=1e-6)`** で一致 (= どちらかの公差を満たせば合格。`macd`/`hist` は 0 を跨ぎ、`dx`/`adx` は凪の区間で 0 に張り付くので、相対公差だけを課すと偽陽性になる)。系列は**ランダムウォークだけでなく、明確なトレンドを持つ系列を最低 1 本**含める (先頭依存の誤差は Δseed = 価格ドリフトに比例するため)。**保証の前提は「indicator に 400 行以上が渡ること」= `min(strategy.max_bars, 400) == 400`** — strategy が 400 未満を宣言した場合はこの保証の外であることをテストの docstring に書く |
| **I6** | 9 本を順に bless できる | tmp の `plugins` ディレクトリと tmp の DB に対して、9 本を 1 本ずつ `bless_candidate` に通し 9 回とも approval_id が返ることを実測。**noop / 同一性で弾かれないこと**と、`outputs_required` / `max_bars_limit` / `check_source` / pytest ゲートをすべて通ることを同時に観測する。実 `data/agentic.db` と実 `plugins/` は使わない (`tests/fixtures/wiring_envs.py` の tmp 環境の作り方に倣う) |
| **I7** | 新 `rsi` を宣言した strategy が E2E で動く | `rsi_pullback` 型の strategy (別名、tmp 環境) が `indicators: {rsi: {plugin: rsi, params: {period: 14}}}` を宣言し、`max_bars: 400` で A1 相当の E2E (resolve → lock → worker 実行 → `evaluate` が系列を受け取る) が通る。**`max_bars: 200` でも動くが I5 の保証外**であることを別ケースで観測する (落ちないことの確認であって、値の一致は要求しない) |
| **I8** | runbook が逐語再現できる | tmp 環境で runbook のコマンド列をそのまま実行し、9 本が配備され `list_deployed_plugins` 相当の inventory に 9 本が `outputs` 付きで現れる。**コピー手順が `__pycache__` を巻き込んでも `check_candidate_snapshot` が無視すること** (`gate_pytest.py:61-64` で確認済) と、**bless 後も `plugins/_human/<名前>` が残る**ことを runbook のクリーンアップ行で扱う |

### 6.1 テストの置き場と、それぞれが誰に回されるか

テストは 2 系統あり、**回す主体と回る回数が違う**ので分けて置く。

| 系統 | 置き場 | 誰が回すか | 載せるもの |
|---|---|---|---|
| 自己テスト | `docs/examples/plugins/<名前>/test_plugin.py` | ① 開発中の `uv run pytest` ② 配備時の bless (`switch._run_full_gate` → `run_gate_pytest`、subprocess + Landlock) ③ 改善 agent が複製・改造したとき `run_plugin_tests` | I3 (参照一致) / I4 (末尾切り詰め不変) / warmup 境界の pin / ゼロ除算の分岐 |
| 回帰テスト | `tests/` | `uv run pytest` のみ | I1 / I2 / I6 / I7 / I8 |

自己テストは **Landlock 下の subprocess で走る**ので、ファイル I/O・ネットワークは使えず、`check_source(..., extra_allowed={"pytest","plugin"})` を通る必要がある (§5.1 で PASS を実測)。**回すたびに全 9 本ぶんが走る**ので、I5 の「5000 本 × 40 seed」のような重い比較は自己テストに置かず、repo 側の回帰テストに置く。自己テストには軽い版 (数百本 × 2〜3 seed) を置く。

**warmup 境界の pin の形**は既存 `rsi_indicator/test_plugin.py:46-48` に倣う — 「N 行目までは NaN」だけでなく「N+1 行目には値が入る」も見る (片側だけだと warmup が 1 本早く/遅く明ける変異を検出できない)。§3.1 の warmup 列の数値がその境界。

**I1 の載せ方**: 既存の `test_discover_sample_plugins_directory_not_rejected` は `discover(docs/examples/plugins)` を回して `{"rsi_indicator", "sma_cross"} <= names` を見るだけなので、9 名を集合に足し、総数も pin する。これにより「3 ファイル構成を崩した」「`config.yaml` の未知キーを書いた」が repo の通常テストで即座に落ちる。

I3 / I4 は各 plugin の `test_plugin.py` の中 (= bless の pytest ゲートが毎回回す)。I1 / I2 / I5 / I6 / I7 / I8 は repo 側 `tests/` の回帰テスト。

## 7. 変更ファイル一覧

**新規 (27 ファイル)**: `docs/examples/plugins/{sma,ema,rsi,macd,bollinger,atr,adx,stochastic,ichimoku}/{plugin.py,config.yaml,test_plugin.py}`

**新規 (1 ファイル)**: `docs/operations/indicator-initial-set-deploy-2026-09-19.md` — 配備 runbook。冒頭に **「暫定。[first-run-setup] (初回起動の対話ウィザード) が一括配備に置き換える。恒久なのは『採用には人間の明示的確認が要る、LLM の自動配備経路は作らない』という規律のほう」** を明記。手順は (0) 既存の配備名との衝突確認 → (1) コピー → (2) `afx plugin bless <名前> --from _human` → (3) `plugins/_human/<名前>` の後始末 → (4) 9 本ぶん繰り返し → (5) 確認。

**変更 (1 ファイル)**: `tests/plugin/test_loader.py` の `test_discover_sample_plugins_directory_not_rejected` に 9 名を追加 (I1)。

**新規テスト (1〜2 ファイル)**: I2 / I6 / I7 / I8 を載せる repo 側テスト (`tests/plugin/test_indicator_initial_set.py` 等)。

**変更しないもの (明示)**: `src/` 配下すべて、`src/agentic_fx/loops/prompts/improve_mission.md`、`config/settings.yaml*`、既存の `docs/examples/plugins/{rsi_indicator,rsi_pullback,sma_cross}`。

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

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-19 | v1.1 | §8.1 裁定結果 D1〜D4 を追記 (いずれも推奨どおり: `y_0 = x_0` / slow stochastic、fast は `k_period: 1` の上書きで得る / 9 名は未配備を確認 / `max_bars` 一律 400) | 指揮者裁定 (可逆な定義選択)。D3 は実 `plugins/` 一覧と実 DB の read-only 確認 | — |
| 2026-09-19 | v1.0 | 初稿。ユーザー承認済 設計 v0.1 (R1〜R8) をコードベースの実測で裏取りし spec 化。noop ゲートが人間 bless 経路に掛からないこと・9 指標の API が `check_source` を通ること・`max_bars` 400 の収束見積り・warmup 本数を実測で確定。要裁定 D1〜D4 を起票 | 設計 v0.1 承認 (2026-09-19)、[indicator-consumption-wiring] 完了後の次束 | - |

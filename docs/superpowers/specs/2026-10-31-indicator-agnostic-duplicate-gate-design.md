# indicator 候補の銘柄非依存と重複を決定論 gate で検査する — 設計 C0

**版:** v0.6 C0 (裁定済、実装待ち。実測 [meas-indgate] 6 便を反映)
**チケット:** `indicator-symbol-agnostic-and-duplicate-gate` (中、機能)。`indicator-reference-oracle-gate` (低、参照値一致) は対象外だが、本束の fixture 実行経路を後で流用できる。
**正本の関係:** 改善ループの質は `2026-10-19-improve-loop-quality-design.md`、族表と注入面は `2026-10-24-improve-feedback-structure-design.md`、承認画面の事実欄は `2026-10-19-approval-content-design.md`、参照系列は `2026-10-25-reference-series-design.md`。いずれも変えず、indicator の gate に段を足す (族表の読み取り列だけ §4 で広げる)。多入力 indicator (`multi-input-indicator-relations`) は本束の後。

## 0. 目的と非目標

**目的:** 改善ループが提出する `kind: indicator` 候補を、承認に回す前に決定論コードで検査する。(a) 名前・param・docstring・文字列定数に銘柄名が入っていない、(b) 価格単位を要求する param が無い、(c) 本番と同じ形の入力で有限・非定数の出力を出し、宣言 outputs と返り値が一致する、(d) 価格を倍にしたとき出力が「不変」か「倍率比例」、volume を倍にしたとき「不変」に収まる (収まらなければ承認画面に warning)、(e) 配備済み indicator の出力集合をそのまま、または単調変換で再現する重複でない。あわせて、**indicator 候補が gate で一度も実行されていない** 現状 (§1) を正し、宣言 outputs と実際の返り値の不一致を配備時でなく gate で捕まえる。

**非目標:** indicator の「正しさ」(参照実装との値一致、別チケット) / 配備済み indicator の遡及検査・改名 (§6) / strategy 候補の検査の変更 / 多入力 indicator の入力束ね / 承認済み indicator の自動停止 / 銘柄依存の難読化防止 (本検査は偶発的な銘柄依存を捕る境界)。

### 0.1 前提の再検討

| チケットの前提 | 確かめたこと | 結論 |
|---|---|---|
| 名前に銘柄が入る = 中身が銘柄依存 | #77 `usoil_utc_day_open_return` の論理 (UTC 日初値からの戻り) は銘柄非依存で、名前だけが悪い。逆に `pip_size` を param に持つ候補は名前が中立でも壊れる | 2 つの欠陥は別物。**名前の衛生 (静的、reject)** と **スケール依存 (fixture 実行、warning)** を別の固定語で分ける |
| スケールの違う 2 銘柄分の fixture で回せば足りる | 2 つの独立 fixture は時刻・ノイズが違うので出力差の原因を切り分けられない | **同一 fixture の価格 4 列を k 倍、volume を倍**した系列との比較で、不変 / 比例 / どちらでもないを判定する |
| gate は候補を既に実行している | 実行しているのは候補自身の self-test (pytest) だけ。入力 DataFrame で `compute` を回す経路は本番 worker にしか無い (§1) | fixture 実行は **新設**。本番の `PluginSession` / `run_plugin` を再利用する (配線を試す。pytest ランナーとは別物) |
| 4096 本で回せば本番と同じ | 本番は依存宣言の `max_bars` 本 (配備済みは 200 / 400) だけを渡す。4096 本で動いても 400 本で退化する候補があり得る | 実行を 2 種に分ける。**契約 run** (本番形、`max_bars` 本) で outputs・有限・定数を判定、**解析 probe** (4096 本) でスケール・重複・類似を判定 |
| 指示 (prompt) で抑えられる | #76〜#80 の承認 5 本全部に銘柄名、既に「配備済みに同じ定義があれば別名で作らない」と書いてある (`improve_mission.md:116`) | 指示は 1 文足すだけ、判定は gate |
| 相関 0.99 超は reject できる (チケット (4)) | 全列 diff の Pearson で 0.99 超は 2 ペアだけ。1 本は exact (`bollinger.middle`-`sma`)、もう 1 本 (`downside_realized_variance_share`-`shock_asymmetry`) は同じ量 s = D²/(U²+D²) の単調変換で、情報として重複 (`downside_upside_ratio` も同じ s の 3 本目)。前便の `ema`-`rsi` 0.98 は混合正規化の見かけで Spearman では 0.05〜0.27。正当な別指標の最高は `garman_klass`-`yang_zhang` (diff Spearman 0.979)。**単調変換は Spearman で捕まる**が、重複の最小 0.9829 と正当の最大 0.9794 の余白は 0.0035、母数は 56 本 (§0.2) | 重複は 3 層: exact は常に reject、**単調変換**は新設 `indicator_monotone_duplicate` (mode warn / enforce、§3.5)、Pearson 0.98 は補助 warning。既定を warn にするか enforce にするかが §8 J4 |
| warmup 後は全部有限のはず | 時刻帯依存の列は設計上 NaN が多い (`xau_utc_open_gap_reversal` は契約 run で 9 本、`session_ranges` は 145〜153 本)。ギャップ無しの fixture では gap 系が全 NaN | 「NaN が 1 つでもあれば reject」は不可。全 NaN だけを reject し、有限が少ない列は `sparse_output` warning とし (§3.4)、fixture には足間ギャップを入れる |

### 0.2 実測 ([meas-indgate]、sonnet、2026-10-31、報告 `tmp/review-indgate/measure-report3.md`)

予備実測 (5m 末尾 4096 本): 時刻帯依存列の NaN と O(n²) plugin の長さ依存 (全系列で 83 秒) を確認し、判断足への集約と固定長 probe を採った。以下は本番形の条件。

条件: 配備済み kind=indicator 56 本 (出力 70 列 + スカラー 2)、合成 5m fixture 340 暦日 (週末の穴あり、足間ギャップ N(0, 0.02)・週明け N(0, 0.3)、seed 固定) を本番の `resample(df, "1h")` で 1h に集約した 5856 本の末尾 4096 本。

| 項目 | 結果 |
|---|---|
| 契約 run (1h 末尾 `max_bars` 本、400 が 48 本・200 が 8 本) | 例外 0、outputs 不一致 0、全 NaN 0、定数 0。有限本数の最小は `xau_utc_open_gap_reversal` 9 (日初足のみ)、`session_ranges` 145〜153。スカラー 2 本 (`rsi_indicator` / `rsi_wilder`) は 1 |
| 疎な列 (有限 30 未満) | `xau_utc_open_gap_reversal` 9、スカラー 2 本のみ (解析 run での本数は未計測) |
| スケール (4096 本、k=100 と 0.01、volume×100) | price: invariant 50 列 (うち volume 非不変 1) / proportional 20 列 / neither 0、2 つの k で一致。volume: 比例 0、neither 1 (`tick_volume_adjusted_move`) |
| Pearson (全列 diff、異 indicator 間、2307 ペア) | >0.95 6 / >0.98 2 / >0.99 2 / >0.999 1。0.99 超の 2 本は `bollinger.middle`-`sma` (exact) と `downside_realized_variance_share`-`shock_asymmetry` 0.9933 (同じ s の単調変換)。混合正規化 (proportional=差分・他は生) の `ema`-`rsi` 0.983 は見かけ (Spearman 生 0.048 / diff 0.269) |
| Spearman (4 便目、2378 / 2307 ペア) | 生 \|ρ\|: >0.98 51 / >0.99 23 / >0.999 6。diff \|ρ\|: >0.98 4 / >0.99 2 / >0.999 1。正当な別指標の高位: `garman_klass`-`yang_zhang` 生 0.9846 / diff 0.9794、`adx.minus_di`-`rsi` 0.9812 / 0.9621、`adx.plus_di`-`rsi` 0.9809 / 0.9595 |
| 生 ≥ 0.999 の交差 (5 便目、6 ペア) | 重複 4 ペア (生 ≥ 0.999 かつ diff ≥ 0.98、生 / diff): `bollinger.middle`-`sma` 1.0000 / 1.0000、`downside_upside_ratio`-`shock_asymmetry` 1.0000 / 0.9845、`downside_realized_variance_share`-`downside_upside_ratio` 0.9999 / 0.9829、`downside_realized_variance_share`-`shock_asymmetry` 0.9999 / 0.9984。生だけ ≥ 0.999 の 2 ペア (トレンド由来の見かけ): `ichimoku.chikou`-`session_ranges.london_low` 0.9999 / 0.9592、`ema`-`ichimoku.senkou_a` 0.9990 / 0.8421 は diff で落ちる。余白: diff の重複最小 0.9829 対 正当最大 0.9794 = 0.0035、閾値 0.98 からは 0.0006 (薄い)。単調変換は式を読んだ推測を含む |
| 別 seed 2 本 (6 便目、20261103 / 20261104) | 2 条件を満たすのは 3 seed とも同じ重複 4 ペア、正当ペアの該当 0。余白 (重複の diff 最小 − 正当の diff 最大) は 0.0034 / 0.0039 / 0.0025。seed 20261104 では `garman_klass`-`yang_zhang` の diff が 0.9811 で 0.98 を超え、diff 単独の条件だと誤検出する (生 0.9843 < 0.999 で外れる)。生 ≥ 0.999 との併用が前提 |
| allclose (rtol 1e-6、共通有限 mask) 一致 | `bollinger.middle` = `sma.value` のみ (共通 4077 点、有限 mask も完全一致) |
| 名前検査 (銘柄名 + 6 文字英字 pair の 3+3 通貨、英数混在は銘柄名のみ) | 銘柄名入り 4 本 = `us500_hourly_drift` / `usoil_utc_day_open_return` / `xau_hourly_volatility_surprise` / `xau_utc_open_gap_reversal` (#76〜#80 のうち配備済み分)。param キー・通貨コード単独・`jp225` のヒット 0 = 偽陽性 0 (5m 予備実測) |
| CPU (56 本 × 4 回) | 合計 15.97 秒 (1 回あたり約 4.0 秒)、中央値 0.9 ms、最大 3.38 秒 (`us500_hourly_drift`)。**sandbox を通さず importlib で直接呼んだ値**で `PluginSession` の起動費を含まない (T2 で実測、それまでは見積り) |

## 1. 現物の確認 (コード)

母数: 配備済み kind=indicator は 56 本 (2026-10-31 時点、pending の #78 は含まない)。銘柄名入りは 4 本。

| 項目 | 現物 |
|---|---|
| indicator の gate | `improve_loop._run_plugin_gate` (:2005): snapshot (:2010) → `check_source` (AST deny、`sandbox.py:224`) → `discover_one_with_reason` → `find_noop_copy` → self-test 数 → `run_gate_pytest` → 薄さ → hash 不変。`max_bars` 上限 (:3564)、kind、`outputs_required_violation` (:3592、`approval.py:118`) は gate の後の commit 本体にある。**fixture や実データで `compute` を回す段は無い**。backtest は strategy 専用 |
| outputs の実検査 | `core/plugin_contract.validate_indicator_result` (:63) は返り値 key と宣言 outputs の完全一致・数値性を見るが、呼び手は本番 worker (`worker.py:303,334`、`sandbox.py:1360`) だけ |
| 実行の隔離 | 本番の `PluginSession` (`sandbox.py:774`) と `run_plugin` (:1826) が Landlock・seccomp・RLIMIT_CPU を担う。`run_gate_pytest` (`gate_pytest.py`) は別のランナーで同一ではない |
| 重複・族 | `logic_identity.logic_hash_of` (:149) は数値リテラル・docstring を落とした AST の hash。`family_key` は indicator では `indicator:{name}@{tf}` で名前そのもの → 別名の indicator は段 F に掛からない。族表の読み取りは `candidate_archives.FAMILY_ATTEMPT_COLUMNS` (:105) の 3 列 (`family_key` / `outcome_class` / `created_at`) に限られる |
| 固定語 | gate の通知語彙は `improve_notice_codes.py` (`_PLUGIN_GATE_NOTICES` :121、`ALLOWED_PAIRS` :108、未知 code は `UnknownNoticeCode` :27) に閉じており、Discord の終了通知 reason_code にもなる。`last_result` は `_finalize_gate_failed` が reason をそのまま書くが、`project_last_result` (`improve_context.py:87`) は `gate_failed:<任意>` を語頭 `gate_failed` に畳む (:117)。配備済み名を通す前例は `observation:family_redirect:<配備済み名>` (:38,:101)。loader 系 (`outputs_bad_entry` 等) は `loader._validate_config` (:304〜388) にリテラル直書き、gate 系は `_PluginGateVerdict(code=...)` にリテラル。enum は無い |
| fixture | `tests/fixtures/indicator_wiring.py` は受入 test 専用の合成系列 (純正弦、USDJPY、週末の穴なし)。gate からは使われない |
| indicator の入力 | worker は strategy の依存宣言ごとに `source.tail(dep["max_bars"])` を渡す (`worker.py:297`)。indicator config は `kind / outputs / max_bars / params` のみで足を持たない。1h 集約は `datafeed/bars.py:79` `resample` (`_AGG` :15) |
| 承認 payload | 生成入口は 3 つ: 改善 mission (`improve_loop.py:2147` 付近)、`switch.submit_candidate` (:1377)、`switch.bless_candidate` (:2244)。収益床の floor_mode は submit = `enforce` (`switch.py:1398`)、bless = `warn` (`switch.py:2293`)。`_run_full_gate` は 4 要素固定の tuple (:1229)、gate の結果型は `GateOutcome` (`approval.py:234`) |
| 承認画面の warning | 汎用の欄は無い。`_approval_detail` (`commands.py:866`) に `floor_warning` / `_baseline_comparison_lines` / `approval_facts.render_facts_lines`。ops API は `service.py:377` の `_baseline_hash_warning` が近い形 |
| prompt | `loops/prompts/improve_mission.md:116` 「配備済みに同じ定義があれば別名で作らず再利用」。銘柄非依存の規則は無い |

## 2. 利用者から見た動き

**候補が落ちるとき。** indicator 候補が新段で落ちると、mission は `completed`、承認依頼は作らない。activity に `IMPROVE gate_failed mission=<id> reason=<固定語>` (固定語は §3.7)。親専有レポート `improve-<日付>-<mission>.md` に節「Indicator gate detail」(落ちた固定語、ヒットした token / param 名、重複なら相手の名前と列、相関値)。

**環境が原因で検査できないとき。** sandbox 不可・cache 生成不能・inventory 不全は、候補の良し悪しでなく環境の事象。分類は sandbox.py の既存規則に従う (load を送る前の失敗 = 環境、送った後 = 候補起因)。改善ループは承認を作らず mission を `failed` (retryable) にし、通知は既存 `NOTICE_SANDBOX_UNAVAILABLE`、backlog は strategy gate の `failed/sandbox_unavailable` と同じく open に戻す。bless だけ、候補自身の run が成功し配備済みの一部の cache 生成だけが失敗した場合に限り `duplicate_unavailable` の warning で進む (AC 11 と同じ条件)。候補自身の sandbox 実行不能・inventory 全損は submit / bless とも retryable failure。

**agent に見えるもの。** 落ちた理由は strategy の床と違い holdout 数値を含まないので、**固定語を agent に返す** (`last_result` = `observation:gate_failed:<固定語>`。現行は `gate_failed` に畳まれるので固定語を通す分岐を足す)。重複・単調変換の重複で落ちたときの相手名は `last_result` には載せず、次 mission の族表 (relation、§3.6) だけで返す (名前は inventory で既に見える語、`family_redirect` と同じ配備済み名 allowlist、§3.7)。次の mission の族表 (`family_attempts`) には indicator 行にも relation (「同等」「単調変換」「一部重複」「類似」+ 相手名) が残る。数値 (相関値・出力値)・候補名・列値・fixture の情報は agent に出さない。

**warning で通るとき。** `scale_dependent`・`indicator_monotone_duplicate` (mode warn のとき)・`indicator_similar`・`indicator_output_overlap`・`sparse_output`・`output_constant_suspect`・`legacy_symbol_name`・`indicator_redefined` の候補は承認に進み、承認依頼 payload に `gate_warnings` と `similar_to` (§3.6) が付く。CLI (`afx ctl approvals <id>`)・対話シェル・ops API+client・web の承認詳細に「警告: 価格または出来高のスケールに依存する出力 (別銘柄で挙動が変わる可能性)」「警告: 配備済み `ema` と類似」の行を出す (数値は親専有レポートにだけ)。Discord の提出通知には出さない。reject 語は終了通知の reason_code になるため、新 code を `improve_notice_codes.py` に足す (2026-10-25 裁定の固定語への追加、§4)。

**同名更新。** 配備中の同名 indicator がある候補は、重複比較の相手から自分の配備中の版を除く。承認待ち (pending) の indicator 候補は比較相手に加わる (repeat で同じ式の別名が並ぶ経路を閉じる)。名前検査は新名と同じだが、配備済みの銘柄名入り 4 本の同名更新だけは `legacy_symbol_name` warning で通す (§8 J1)。outputs 契約が変わる、旧版との類似が低い、全列を再定義している候補は `indicator_redefined` warning を承認者に出す (同名更新を別物の差し替えに使えないようにする、§3.5)。

**プロンプト。** indicator の節 (`improve_mission.md:116` の段落) に 1 文: 「indicator は銘柄を持たないので、名前・param・説明に銘柄名を書かず、価格の単位 (pips など) を param にしないでください。」

## 3. 規則

### 3.1 対象と順序

対象は改善 mission (週次・単発・repeat) の `kind: indicator` 候補と、人間回廊 (`submit` / `bless --from _human`)。人間回廊は既存の収益床と同じ 2 値に揃える (2026-09-12 の裁定、現行 `switch.py:1398` / `:2293`): **submit = enforce** (改善ループと同じ reject。submit は agent の staging 出力も受けるので、gate を通らなかった候補を素通しにしない)、**bless = warn** (政策判断にあたる検査 = 名前・param・スケール・類似・部分重複・mode warn の単調変換・`legacy_symbol_name` は reject せず warning。exact の `indicator_duplicate` は bless でも reject = §8 J5 の推奨)。**構造的に壊れた候補** (`fixture_run_failed`・`output_not_finite`・`output_constant`、候補起因の sandbox 失敗) は bless でも reject (fail closed。壊れた indicator の配備を許す前例は無い)。環境起因の sandbox 不可は retryable failure (承認を作らない)。

順序 (安いもの → 高いもの、落ちたら以降は回さない):
1. snapshot 検査 → `check_source` (AST deny)
2. loader → 契約検査 (`max_bars` 上限・kind・outputs 必須。現行の commit 本体の検査を前段に移す)
3. 名前検査 (§3.2) → param 検査 (§3.3)。loader が parse 済みの meta と source から取り、未検証の staging を親が直接 parse しない
4. noop コピー / self-test / hash 不変 (既存 pytest 段)
5. 契約 run (§3.4) → 解析 probe (§3.4) → スケール分類 (warning)
6. 重複・単調変換・類似 (§3.5)

### 3.2 名前検査 (`symbol_in_name`)

- 銘柄集合 = settings の `pairs` + `watch_symbols` (mission 開始時の snapshot、参照系列設計の `readable_symbols` と同じ源)。銘柄名側も同じ正規化を通す (小文字化、`.m` `.cash` などの接尾辞は `.` で切る)。
- 検査対象: `name`、`params` のキー (入れ子も再帰)、`params` の文字列値、`plugin.py` の全文字列定数と docstring (モジュール・関数)、`config.yaml` の全文字列値。
- 正規化: 小文字化、camelCase を境界で割る、`_` と空白・句読点で割る、英字⇄数字の境界でも割る。ただし分割前の語も token に残す (`us500` は `us500` / `us` / `500` の 3 つ)。1 文字 token の連続 (`x_a_u`) は連結して 1 token にする。部分文字列一致はしない。
- 銘柄 token = 銘柄名そのもの (`xauusd`, `us500`, `usoil`)、**6 文字英字の銘柄名を 3+3 に割った 2 語は、両半分が ISO 4217 の通貨 (+ XAU / XAG) に一致するときだけ** (`xau` / `usd` / `jpy` / `eur`。`copper` / `natgas` は割らず、`per` / `gas` で `volume_per_bar` を落とさない)、**別名表** (閉じた固定表を同モジュールに置く: xau→gold, xag→silver, usoil→oil/wti/crude, us500→spx/sp500, jp225→nikkei, jpy→yen, usd→dollar, eur→euro) と、銘柄集合から導いた別名。実測 (§0.2) で偽陽性 0、銘柄名入り 4 本は全部ヒット。
- いずれかが一致したら `symbol_in_name` で reject。位置づけは偶発的な銘柄依存を捕る境界で、難読化防止ではない (§6)。

### 3.3 param 検査 (`param_price_unit`)

`params` のキー (再帰、§3.2 の正規化後) について、token が `pip` / `pips` / `pipsize` / `ticksize` のいずれか、または隣接 token を `_` で繋いだ句が `pip_value` / `pip_size` / `tick_size` / `point_size` / `price_level` / `price_threshold` / `price_offset` / `price_distance` / `price_step` / `price_delta` / `price_unit` / `absolute_price` / `abs_price` / `min_price` / `max_price` のいずれかなら reject。bare の `point` / `points` と「`price` を含む」は使わない (`pivot_point` / `n_points` / `price_source` / `use_typical_price` のような銘柄非依存の param を落とすため)。値の既定 (例 0.05) は見ない (スケール依存は §3.4 が実測で見る)。語彙は閉じて 1 箇所に置く (§3.7)。

### 3.4 fixture 実行 (契約 run と解析 probe)

- **正規 fixture** は `src/agentic_fx/plugin/canonical_fixture.py` (tests/ ではない) が seed 固定で生成する: 5m 足、close = 150 + 2 sin(2πk/288) + 0.5 sin(2πk/2016) + seed 固定のランダムウォーク (σ 0.05)、open = 前足 close + N(0, 0.02) (週明け初足は N(0, 0.3))、high/low を足内で整合、volume は正の対数正規で UTC 時刻帯により水準が変わる、実 UTC timestamp と週末の穴 (金 21:00 〜 日 21:00 UTC、wine-mt5 流儀)。5m を本番と同じ集約関数で settings の判断足 (既定 1h) に揃える。実測の fixture (§0.2) はこの式。乱数分布は numpy の版で変わり得る (NEP 19) ので、fixture の実データ hash を cache key に含めて版違いを自動で無効にする。
- **実行経路**: 本番の `PluginSession` / `run_plugin` を再利用し (pytest ランナーとは別)、本番 worker と同じ入口 (`compute` → `validate_indicator_result`) を通す。gate 側に別のランナーを書かない。
- **契約 run**: 判断足の fixture の末尾 `max_bars` 本を 1 回渡す (本番 worker の `tail(max_bars)` と同形)。上限は本番と同じ (`sandbox_timeout_sec` 10 秒、出力 1 MiB、session CPU 60 秒)。判定:
  - 打ち切り・例外・`validate_indicator_result` 違反 → `fixture_run_failed` (`detail` = `cpu_limit` / `timeout` / `exception` / `outputs_mismatch`)。
  - 系列出力のうち**全列が全 NaN** → `output_not_finite` (構造的失敗はこれだけ)。有限が 1 本以上 30 本未満の列は `sparse_output` warning (日初足だけの指標は `max_bars` を小さく宣言すると 1h で数本になるが正当)。`min_finite_rows` のキーは持たない。時刻帯外が NaN の列 (`session_ranges`) は設計どおり通す。
  - 有限値の `nunique <= 1` かつ解析 probe でも定数 → `output_constant`。片方だけ定数なら warning `output_constant_suspect` (fixture 固有の退化があり得る。配備済み 56 本は両方でゼロ)。
  - スカラー出力 (現行契約で許される。配備済み 2 本) は有限検査と exact 判定だけ。定数・スケール・相関の対象外。
- **解析 probe**: 判断足 fixture の末尾 N=4096 本を、k=1 / 100 / 0.01 (price 4 列のみ倍、volume 不変) と volume×100 の 4 回。N=4096 は配備済み最大 `max_bars` 400 の 10 倍超で相関の自由度を確保でき、CPU 実測が 1 回約 4 秒に収まる値。
  - **失敗の分類**: probe の失敗・超過は候補起因として `fixture_run_failed` で reject (`detail` = `probe_timeout` / `probe_exception`)。壁時計上限 `improve.indicator_fixture_wall_clock_max_sec` (既定 60 秒) は 4 回の合計に掛け、session の CPU 上限とは別に probe 全体の上限とする (配備済み最大 3.4 秒 × 4 に対して余裕。本番で見ない長さで 20 倍遅い候補は欠陥とみなす)。
  - **出力サイズ**: 1 値は wire で約 20 byte、4096 本で 1 列約 80 KB。loader は 32 出力まで許す (`MAX_OUTPUTS`、`loader.py:109`) ので、probe は列を 1 call あたり 8 列以下 (約 640 KB) の列群に分けて回し、1 MiB に収める。
  - **各 k の検査**: 全 k の run で 契約 run と同じ全 NaN 検査を当て、どれかで全列が全 NaN になれば `output_not_finite` で reject (例: `log(close - 100)` は 150 では動くが k=0.01 では全 NaN。チケット (2) の「両スケールで有限」)。本数が減るだけなら `sparse_output`。
  - **スケール分類**: 出力列ごとに price は `invariant` (k=1 と allclose rtol 1e-6) / `proportional` (k 倍と allclose) / `neither`、volume は `invariant` / `neither` (volume 比例は設けない)。2 つの k で食い違えば `neither`。いずれかの列が `neither` なら **warning** `scale_dependent` を付けて承認へ進む (reject しない。チケット (2) で決定済み。対数収益のクリップなど正当な例があり得る)。k=1 の出力を cache し、重複・類似はこの出力で行う。
- **cache**: 配備済み indicator の k=1 出力 (列ごとの系列と分類) を親専有の `data/indicator_fixture_cache/` に置く。key = `<content_hash>-<fixture 実データ hash>-<tf>` (式や numpy の版で fixture が変われば旧 cache は自動で無効。旧世代は mission 終端で GC)。規律: directory 0700 / file 0600、`O_NOFOLLOW` + 通常ファイル + `nlink==1` の検査、temp + fsync + atomic rename、key ごとの lock、読出時に schema と content_hash を検証、sandbox の allowlist に入れない。無ければその場で同じ sandbox 経路で生成する (初回 56 本 × 4 回で約 16 秒 CPU と見積もる。sandbox 外の値で起動費は T2 で実測)。pending の indicator 候補の k=1 出力も同じ規律で cache する。
- cache や inventory が使えないときの扱いは §2。

### 3.5 重複と単調変換と類似

- 比較相手 = inventory の配備済み kind=indicator 全部 + 承認待ち (pending) の indicator 候補 (候補と同名の配備中の版は除く)。
- **exact / overlap は全出力に当てる**: 候補の `neither` 列・スカラー・疎な列も、k=1 の生の値で比較する (`neither` を外すのは相関系の比較だけ)。**exact の定義**: 有限 mask の完全一致 + 全評価点で対称 tolerance `|a-b| <= 1e-12 + 1e-6·max(|a|,|b|)` (詳細は下の exact の定義)。
- `indicator_duplicate` で **reject** (常時、mode に依らない) = 候補の**全出力列**が、同一の既存 plugin の出力集合に一対一で exact に再現される場合。
- 一部の列だけ exact なら `indicator_output_overlap` warning (例: `bollinger` は `middle` が `sma.value` と exact だが他の列が固有なので overlap)。
- **`indicator_monotone_duplicate`**: 候補の全系列列 (すべて `neither` でなく、共通有限点 30 以上) が、同一の既存 plugin の列と「生 Spearman の絶対値 ≥ 0.999 かつ 1 階差分の Spearman の絶対値 ≥ 0.98」で対応するとき。単調変換 (同じ量の言い換え) を捕る。実測の 4 ペアだけが満たす (§0.2)。`improve.indicator_duplicate_mode: warn | enforce` (既定 `warn`、mission 開始時の snapshot、baseline gate の shadow と同型) で、warn なら warning として承認画面と族表に、enforce なら reject。閾値 0.999 / 0.98 は定数で、config にしない。
- `indicator_similar` warning (補助): 共通の有限点が 30 以上の列対で、**全列を 1 階差分にした系列の |Pearson|** ≥ `improve.indicator_similar_corr` (既定 0.98)。符号反転も含める。混合正規化は使わない。共通有限点が 30 未満の疎な列は「有限点の時刻 + 値」の fingerprint で exact 判定だけを行う。
- 相関 0.99 超だけでは reject しない理由: 全列 diff の Pearson で 0.99 超は 2 ペアで、1 本は exact、もう 1 本は単調変換の本物の重複だが、その外側に正当な別指標が薄い余白で並ぶ (`garman_klass`-`yang_zhang` diff Spearman 0.9794)。相関の閾値より、順位の対応を 2 条件で見るほうが重複 4 ペアだけを分けられる。それでも余白は 3 seed で最小 0.0025・母数 56 で、偽棄却の率は未測定のため mode を設ける (§8 J4)。
- `similar_to` は kind の強い順 (duplicate > monotone > overlap > similar)、同じ kind は相関の高い順に 3 本まで (名前・列・kind)。数値は親専有レポートにだけ。候補内の自分の列同士は見ない。
- **同名更新の `indicator_redefined`**: 配備中の同名版と比べ、outputs 契約が変わる、または全列が旧版と低類似 (|corr| < 0.5) のとき warning。
- §3.4 で全 NaN の列・定数の列は相関系の比較に入らない。
- **承認時の再判定 (J6)**: `approve` は配備の直前に、候補の probe 出力 (提出 mission で保存した cache) を現 inventory (提出後に配備されたものを含む) に対して exact と単調変換だけ再判定する。新たに成立したら warning を更新し、enforce mode なら承認を拒否して再提出を求める (`approve` の失敗語 `duplicate_at_approve`)。候補の probe 出力が無い (旧 payload) なら再判定しない。

### 3.6 payload と族表の relation

承認依頼 payload に逐語で:
- `gate_warnings: list[str]` — §3.7 の warning 語彙のみ、重複なし、最大 8。
- `similar_to: list[{name, column, kind}]` — 最大 3、`name` は配備済み名、`column` は識別子、`kind` ∈ {`duplicate`, `monotone`, `overlap`, `similar`}。承認待ちの候補が相手のときは、承認者の画面にだけ候補の id を出し、agent 面には出さない。

生成入口 3 (改善 mission / `submit_candidate` / `bless_candidate`) が同じ形を作り、表示 4 面 (CLI / 対話シェル / ops API+client / web) が同じ語彙を読む。warning は `GateOutcome` に載せる (`_run_full_gate` の 4 要素 tuple は変えない)。キー無しの旧 payload と壊れた payload (型違い・語彙外) は fail-soft (表示しない、承認操作は妨げない)。

族表の relation: `candidate_archives` に `relation` 列 (JSON: `kind` ∈ {`duplicate`, `monotone`, `overlap`, `similar`}、`name` = 配備済み名のみ) を足す。`improve_context` の族表読み取りは 3 列 + `relation` に広げ、`project_last_result` と同じ配備済み名 allowlist を通して投影する。未配備の相手名は出さない。族表の行 `indicator (未配備) @<足>` は複数候補をまとめた行なので、relation が複数あるときは kind の強い順に 1 行へまとめ、相手名は 3 本まで。公開 schema (2026-10-24 設計の族表節) の改訂は本 spec §4 に書き、正本の改訂は実装時に指揮者が行う。

### 3.7 固定語と置き場、agent への配線

検査ロジックと token 化・禁止語の閉じた語彙は新モジュール `src/agentic_fx/plugin/indicator_gate.py` に置く。通知側の語彙 (`NoticeCode`・`ALLOWED_PAIRS`) は既存の `improve_notice_codes.py` に code を足す (新モジュールで二重定義しない)。生成側 (gate)・表示側・test は同じ語彙を import する:

- reject code: `symbol_in_name` / `param_price_unit` / `fixture_run_failed` / `output_not_finite` / `output_constant` / `indicator_duplicate` / `indicator_monotone_duplicate` (mode enforce のみ)
- warning: `scale_dependent` / `indicator_monotone_duplicate` (mode warn のみ) / `indicator_similar` / `indicator_output_overlap` / `sparse_output` / `output_constant_suspect` / `legacy_symbol_name` / `indicator_redefined` / `duplicate_unavailable` (bless のみ)
- `fixture_run_failed` の detail: `cpu_limit` / `timeout` / `exception` / `outputs_mismatch` / `probe_timeout` / `probe_exception`

`_PluginGateVerdict(code=...)` へは上記の値を渡す (loader 系の既存リテラルは触らない: §6)。`_finalize_gate_failed` の reason は固定語だけにし、ヒットした token や相関値は親専有レポートへ別経路で出す。`last_result` の逐語形は `observation:gate_failed:<固定語>` の 1 形だけ。relation (相手名) は `last_result` に載せず族表で投影する (単一値に 2 形を同居させない)。`project_last_result` に `family_redirect` と同じ配備済み名 allowlist の分岐を足す。

**relation を agent に返すこと** は `observation:family_redirect:<配備済み名>` の前例と同型で、返すのは配備済み名と kind だけ。数値・列値・fixture 情報は禁止 (AC 13)。残余: mission 回数を使って配備済みの出力を相関で探る probing は完全には防げない。監視は relation の mission あたり件数。

## 4. 配線

| 箇所 | 変更 |
|---|---|
| `plugin/indicator_gate.py` (新) | 検査ロジックの語彙、token 化・別名表、`check_name_and_params(meta, source) -> verdict`、列の分類・比較 (exact / overlap / monotone / similar / sparse) の純関数 |
| `plugin/canonical_fixture.py` (新) | seed 固定の生成、判断足への集約 (本番の `resample` を呼ぶ)、fixture 実データ hash |
| `plugin/sandbox.py` | indicator を fixture で回す入口 (`PluginSession` / `run_plugin` を再利用、`compute` → `validate_indicator_result`、契約 run は本番と同じ上限、probe は列群分割と壁時計上限)、cache 読み書き |
| `improve_notice_codes.py` | §3.7 の reject code を `_PLUGIN_GATE_NOTICES` / `ALLOWED_PAIRS` に追加、Discord 側 reason_code の同期、`UnknownNoticeCode` を起こさない test |
| `loops/improve_loop.py` | `_run_plugin_gate` に §3.1 の順で段を足す (indicator のみ)、`gate_warnings` / `similar_to` を payload に、レポート節、activity 固定語、`last_result` の固定語、prompt の 1 文、環境起因の retryable failure、`indicator_duplicate_mode` の mission 開始時 snapshot |
| `loops/improve_context.py` | `project_last_result` に新 gate 固定語を通す分岐、族表の relation 投影 (配備済み名 allowlist) |
| `plugin/switch.py` / `plugin/approval.py` | `submit_candidate` (enforce) / `bless_candidate` (warn) が同じ関数を呼ぶ、payload の同じ形、`GateOutcome` に warning、`gate_warnings` の検証 |
| `store/candidate_archives.py` | `relation` 列 (migration)、族表の読み取りを 3 列 + `relation` に広げる、数値・候補名の不在 pin |
| `config.py` / `config/settings.yaml.example` / 設定ロード test / fixture | 任意キー 3 つ: `improve.indicator_similar_corr` (0.98)、`improve.indicator_fixture_wall_clock_max_sec` (60)、`improve.indicator_duplicate_mode` (`warn`)。schema 版は上げない |
| `plugin/switch.py` (`approve`) | 配備直前の exact / 単調変換の再判定 (§3.5 J6)、失敗語 `duplicate_at_approve` |
| `commands.py` `_approval_detail` / `shell.py` / ops `service.py` DTO / `client.py` / web | `gate_warnings` と `similar_to` の表示 1 行 + 列名 |
| cache | `data/indicator_fixture_cache/` (親専有、gitignore 済の `data/` 配下)。配備 (`approve`) 時に新規分を先行生成してもよいが必須ではない |

## 5. 受入条件

1. 名前・param キー (入れ子)・param 文字列値・文字列定数・docstring・config の文字列値に銘柄名 (`xau_...`, `..._usdjpy`, `jpy_...`, `us500_...`、`xauUsd`、`x_a_u`、別名 `gold`) を含む候補は `symbol_in_name` で落ち、承認依頼が作られず、activity と親専有レポートに固定語が残る。test は銘柄集合を固定値 (現本番の 6 銘柄) で与え、配備済み 56 本に当てて、ヒットは銘柄名入りの 4 本だけ (偽陽性 0)。`volume_per_bar` / `copper_x` は通る。
2. `params` に `pip_size` / `tick_size` / `priceThreshold` / `price_threshold` を持つ候補は `param_price_unit` で落ちる。`period` / `window` / `alpha` は通る。
3. 宣言 outputs と返り値の key が違う候補は gate の `fixture_run_failed` (detail `outputs_mismatch`) で落ちる。bless でも落ちる。
4. 契約 run で全列が全 NaN になる候補 (4096 本なら動くが `max_bars` 本では全 NaN) は `output_not_finite`。有限が 1 本以上 30 本未満の列は `sparse_output` warning で通り、bless でも reject されない。`log(close - 100)` を使う候補は k=0.01 の run で全 NaN になり `output_not_finite`。配備済み 56 本は契約 run で全部通る。契約 run と probe の両方で定数を返す候補は `output_constant`、片方だけなら `output_constant_suspect` warning。
5. 32 出力の候補は probe で出力上限に当たらず通る。4096 本でだけ遅い候補 (O(n²)) は `fixture_run_failed` (detail `probe_timeout`) で落ち、gate 全体が上限を超えて止まらない。契約 run の上限は本番と同じ。
6. `close - open > 0.05` のような絶対閾値を持つ候補は通るが `scale_dependent` が付き、承認詳細に警告行と列名が出る。volume の絶対閾値の候補 (`tick_volume_adjusted_move` 相当) は「価格または出来高のスケールに依存」の文言。`atr` (proportional) と `rsi` (invariant) は warning なし。
7. 配備済み `rsi` と同じ式を別名で出した候補は `indicator_duplicate` で落ち、`last_result` は `observation:gate_failed:indicator_duplicate`、族表に `rsi` の名前が残る。`neither` 列だけの複製 (`tick_volume_adjusted_move` の書き直し)、スカラーを 1 本足した複製は exact 判定の対象になり overlap 以上に捕まる。`bollinger` に固有の列を足した候補は reject されず `indicator_output_overlap`。有限 mask が違うだけの系列は exact とみなされない。`ema` を別名で窓長だけ変えた候補は通り、`indicator_similar`・`similar_to` に `ema` が付く (`indicator-params-not-new-plugin` が入るまで開いたまま、§8 J4)。
8. 単調変換: `shock_asymmetry` と同じ s の単調変換 (`downside_realized_variance_share` 相当) を別名で出した候補は、mode warn で `indicator_monotone_duplicate` warning、enforce で reject。`garman_klass` に対する `yang_zhang` 相当は monotone にならない。mission 開始後に config を変えても同じ mission の mode は変わらない。
9. 同名更新 (例 `atr` の period 変更) は配備中の `atr` と比較されず、他の重複がなければ通る。outputs を変えた同名更新は `indicator_redefined`。配備済みの銘柄名入り 4 本の同名更新は、候補の銘柄ヒット集合 (場所 × token) が配備中の版のヒット集合に含まれるときだけ `legacy_symbol_name` warning で通る (docstring をそのまま残した修正は通り、docstring に新しい銘柄を足すと落ちる)。承認待ちの indicator と同じ式の別名候補は pending の相手として捕まり、相手名が未配備なら agent の `last_result` にも族表にも出ない。
10. cache が無い状態の mission で cache が生成され、2 回目の mission では sandbox 呼び出し spy が配備済み分について 0 回。activity に生成本数が出る。cache の directory / file の権限、symlink・hardlink・破損 file の拒否、sandbox と agent tool から読めないこと (canary) を test する。
11. 候補の run は成功し配備済み 1 本の cache 生成だけ失敗するケース: 改善ループは承認を作らず mission が `failed` (retryable、`NOTICE_SANDBOX_UNAVAILABLE`、backlog は open に戻る)、bless は `duplicate_unavailable` warning で進む。load を送る前の失敗は環境、送った後の失敗は候補起因 (reject)。
12. 人間回廊: `submit` は改善ループと同じ reject (名前・param も落ちる)、`bless` は名前・param・scale・類似・部分重複 (overlap)・mode warn の単調変換を warning として承認詳細に出し、`fixture_run_failed` / `output_not_finite` / `output_constant` と exact の `indicator_duplicate` は reject する (§8 J5 の推奨。J5 で bless の上書きを許す裁定なら warning に変える)。生成入口 3 つと表示 4 面すべてで `gate_warnings` と `similar_to` が同じ文言で出る。キー無しの旧 payload と壊れた payload は表示せず例外にならない。
13. agent 面: `last_result` と族表に固定語・kind・配備済み名のみ。相関値・出力値・fixture の式・fixture 固有の canary 値は、親専有レポートには出ても全 agent 面 (注入面・RPC 返却・族表) に無い。`relation` の投影は配備済み名 allowlist を通り、候補名は出ない。新 reject code は `ALLOWED_PAIRS` に入り、Discord 終了通知で `UnknownNoticeCode` を起こさない。
14. 意味的な pin: fixture の timestamp (週末境界 金 21:00 / 日 21:00 UTC を実例で assert)、先頭 / 末尾の close の範囲、volume 合計の範囲。byte 一致は求めない。fixture の式や numpy の版が変わって実データ hash が変わると cache が自動で無効になる。
15. 承認時の再判定: 候補 A (pending) と同じ式の候補 B が先に承認・配備された後、A を `approve` すると warn mode では重複の warning が更新されて承認詳細に出る。enforce mode では `duplicate_at_approve` で承認が拒否され、配備は起きない。候補の probe 出力が無い旧 payload は再判定を飛ばして従来どおり配備される。

## 6. 残余

- 配備済みの銘柄名入り 4 本 (と pending #78) は遡及しない。改名は人間の運用 (別チケット候補: `afx` の plugin 改名手順)。同名更新の例外 (§8 J1) は改名手順ができたら廃止する。
- 1 本の fixture では銘柄非依存を証明できない。発火しない絶対閾値、UTC session 固有の条件、丸め・clip の境界外は invariant / proportional に見えて通り得る。複数 seed・価格水準・周波数・価格オフセットを加えるのは将来。
- 名前・param 検査は偶発的な銘柄依存を捕る境界で、意図的な難読化 (生成した文字列、エンコード) は捕らない。
- 5m 原足での追加実行 (判断足を変える利用者) は本束では 1 足のみ。将来 `decision_timeframes` が複数になれば fixture も足ごと。
- loader 系の既存リテラル固定語の enum 化は範囲外 (別チケット候補)。
- 常時 reject は全出力の exact 再現だけ。単調変換は mode 次第 (既定 warn)。窓長を変えただけの同指標は warning (類似) に留まり、判断は承認者。族の概念 (同じ式で param だけ違う) は `indicator-params-not-new-plugin` の領分で、それが入るまで開いたまま (§8 J4)。列を 1 本足す・`min_periods` を変える・定数倍する複製は overlap / similar の warning に落ちる。
- 類似・単調変換の判定は合成 fixture 上の値で、実データ上の類似とは一致しない。単調変換の閾値は母数 56 本・余白 3 seed で最小 0.0025 で偽棄却率が未測定。diff 条件単独は seed により正当ペアを誤検出する (6 便目) ため 2 条件の併用が必須。周波数の違う fixture は未評価。
- relation oracle: mission 回数を使った probing は残る (§3.7)。時刻依存 (UTC 時刻帯) の indicator は fixture の timestamp に依存して正しく動く前提で、時刻帯分布が実データと違う分の偏りは残る。
- スカラー出力は有限検査と exact 判定だけで、スカラーの定数・相関は見ない。
- 環境起因の sandbox 不可の扱いが段で食い違う: 既存 pytest 段は `gate_failed/gate_sandbox_unavailable` (backlog を消費)、新段と strategy gate は `failed/sandbox_unavailable` (backlog を open に戻す)。本束では既存段を触らない。
- 承認待ち候補が相手のとき、承認時に再判定はしない (提出時点の比較のみ)。

## 7. 変更一覧と task 分割

| task | 内容 | 担当 | 行数 (src / test) |
|---|---|---|---|
| T0 | 実測 (本 C0 の §0.2、済 = [meas-indgate] 3 便) | sonnet | — |
| T1 | 純関数: token 化・別名表・名前/param 検査・列の分類・比較 (exact / overlap / monotone / similar / sparse)・fixture 生成 (`indicator_gate.py` / `canonical_fixture.py`)。変異 | codex (terra) | 約 250 / 350 |
| T2 | sandbox 入口 (`PluginSession` / `run_plugin` 再利用、probe の列群分割と壁時計上限) + cache (親専有・権限・lock・GC) + 実 sandbox 試験 + 起動費の実測 | sonnet (隔離境界) | 約 150 / 250 |
| T3 | `improve_loop` の段追加・契約検査の前段化・payload・レポート・activity・`last_result`・`improve_notice_codes.py`・prompt・settings (mode snapshot)、`switch.py` / `approval.py` (submit = enforce / bless = warn)、`relation` の永続化と族表の読み取り | codex (terra) | 約 250 / 350 |
| T4 | 表示 4 面 (CLI / shell / ops DTO+client / web)、結合試験 (AC 1〜15、実 sandbox・実 plugins の cache 生成) | sonnet | 約 100 / 250 |

依存 T1 → T2 → T3 → T4。レビューは codex 1 周目 → `/code-review high` → terra 閉鎖、新規の防御 (T1) は段 0 の変異スイープ。注入面の固定語・relation 漏れ (AC 13) は最終 diff でローカル LLM 3 本の候補。

## 8. 裁定 (利用者判断が要るもの)

**2026-10-31 ユーザー裁定済**: J1 = grandfather + `legacy_symbol_name` warning / J4 = 既定 warn / J5 = exact の `indicator_duplicate` は bless でも reject / J6 = approve 時に exact と単調変換を再判定。いずれも推奨どおり。

技術で決まるもの (閾値・token 化・fixture の式・順序・fail open の廃止・部分重複・スカラー・同名更新の全面変更・`scale_dependent` と `output_constant` の重さ・relation の返却) は入れない。J1 / J4 は sol の第二意見 (2026-10-31) で推奨に同意、J5 / J6 は sol の追加提案。

**J1 配備済みの銘柄名入り indicator の同名更新。** 推奨: 配備済み 4 本の**同名更新だけ**名前検査を免除し、`legacy_symbol_name` warning を出す (grandfather)。免除の規則は、候補の銘柄ヒット集合 (場所 × token) が配備中の版のヒット集合に含まれるときだけ (新名・新しい param・新しい銘柄参照は落ちる)。改名手順ができたら例外を廃止する。根拠: 名前は契約違反でも、名前のために安全修正まで不能にする必要は無く、承認者が warning を見て判断できる。代案: 凍結 (同名更新も `symbol_in_name` で落とし、新名で出し直す)。根拠: 例外を設けると gate に「配備済みなら許す」分岐が残り、新名の候補は重複検査で旧版と比べられる。利用者判断の理由: 承認済みのものを事実上凍結するか否かで、承認者はユーザー。

**J4 単調変換の重複 (`indicator_monotone_duplicate`) の既定 (チケット (4) の「相関 0.99 超で reject」からの変更を含む)。** 事実: 配備済み 56 本で本物の重複 4 ペア (exact 1、同じ s の単調変換 3) は生 Spearman ≥ 0.999 かつ diff Spearman ≥ 0.98 を同時に満たす唯一の組で、正当な別指標は外れる。ただし diff 側の余白は 3 seed で 0.0034 / 0.0039 / 0.0025 (閾値 0.98 から最小 0.0006)、diff 単独なら seed 1 本で正当ペアを誤検出、母数 56 で偽棄却の率は未測定。exact は常に reject、Pearson 0.98 の `indicator_similar` は常に warning。
- **A (推奨): 既定 warn。** 単調変換は warning として承認画面と族表に出す。根拠: 余白と母数では偽棄却が測れていない。実候補で warning の当たり外れを観測してから `enforce` に切り替える。
- **B: 既定 enforce。** 機械で止める (チケットの意図どおり)。偽棄却は人間回廊 bless (warn) で救える。根拠: 本物の重複が承認に回る手間を省く。
- いずれの案でも次の2点が残る。(1) チケットの「一致なら提出不可」から、部分重複 (overlap) を warning にした。(2) `ema` を窓長だけ変えた別名は通る (`indicator_similar` warning)。これは `indicator-params-not-new-plugin` (params で表せるものは新規として提出させない) の方針と衝突し、そのチケットが入るまで開いたまま。
利用者判断の理由: 閾値次第で正当な候補を落とす (または重複を見逃す) 受容で、チケットの仕様変更にあたる。

**J5 `bless` が上書きできる範囲 (sol の追加提案)。** 収益床の前例では bless は床を warn で越えられる。本設計で bless が warning で越えられるのは名前・param・スケール・類似・部分重複・mode warn の単調変換まで。推奨: exact の `indicator_duplicate` (全出力が配備済みと一致) は bless でも reject。根拠: 一致した複製は配備しても新しい情報が無く、人間が強行する理由が無い。前例 (床) は「実行できる候補の成績を人間が引き受ける」仕組みで、複製の配備とは性質が違う。代案: bless は exact も warning で越えられる (前例の単純拡張)。利用者判断の理由: 人間回廊の権限範囲で、既存の前例をどこまで広げるかは運用方針。

**J6 承認時に重複を再判定するか (sol の追加提案)。** 現案は提出時の inventory + pending に対して判定し、承認待ちの間に別候補が配備されても見直さない。推奨: `approve` 時に exact と単調変換だけを現 inventory に対して cache で再判定し、変化があれば承認画面の warning を更新する。enforce mode で exact / 単調変換が新たに成立したら承認を拒否し再提出を求める (warn mode では warning のみ)。根拠: cache があれば再判定は候補 1 本の probe 出力と配備済みの比較だけで秒単位、承認は「配備時の状態に対する承認」と解釈する方が二重配備を防ぐ。代案: 提出時 snapshot への承認とし、再判定しない (承認契約が単純)。利用者判断の理由: 承認が「提出時の候補への承認」か「配備時の状態への適合も含む」かは承認契約の選択。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-31 | v0.1 C0 | 初版 (指揮者承認前の清書、実測反映前) | チケット `indicator-symbol-agnostic-and-duplicate-gate` の設計化 | |
| 2026-10-31 | v0.2 C0 | 実測 2 便の反映: 相関 reject を廃止 (exact のみ reject、0.98 以上は `indicator_similar` warning)、probe を判断足 N=4096 固定長に、有限判定を本数下限 16 に、fixture に足間ギャップ、token 規則 (6 文字英字は 3+3) を確定 | 配備済み 56 本の総当たりで相関 0.99 超 80 ペア、時刻帯依存列の NaN、O(n²) plugin 83 秒 | |
| 2026-10-31 | v0.3 C0 | sol 1 周目の全件反映 (Critical 3 / Important 12 / Minor 4): 契約 run と解析 probe に分離し 1h 条件の実測 (3 便目) に差し替え、人間回廊は構造的失敗を fail closed、重複は全出力 exact のみ reject (部分一致は overlap warning)、fail open 廃止、下限 8 と sparse の fingerprint、token 化強化、volume probe、cache 規律、`relation` 永続化と payload schema、遮断 8 の例外記録、task を T1〜T4 に再分割。J1 推奨変更、J4 / J5 新設 | 設計レビュー 1 周目の指摘 | |
| 2026-10-31 | v0.4 C0 | 2 周目の全件反映 (Critical 2 / Important 10 / Minor 7)、J2 / J3 / J5 を技術確定に、J4 を 2 案に、人間回廊を submit=enforce / bless=warn に。単調変換の重複 (`indicator_monotone_duplicate`、mode warn / enforce) と Spearman 実測 (4・5 便目) を追加 | 設計レビュー 2 周目の指摘 | |
| 2026-10-31 | v0.5 C0 | 閉鎖確認の部分閉鎖 4 件: bless は exact 重複も reject (J5 推奨)、`duplicate_unavailable` の範囲を AC 11 の条件に限定、`last_result` は `observation:gate_failed:<固定語>` の 1 形 (relation は族表のみ)、AC の detail 表記を統一。6 便目 (別 seed 2 本) を §0.2 と J4 に。sol の第二意見で J5 / J6 を追加 | 閉鎖確認と第二意見 | |
| 2026-10-31 | v0.6 C0 | J1 / J4 / J5 / J6 をユーザー裁定済 (推奨どおり) に。J6 の配線 (approve 時の再判定、`duplicate_at_approve`、AC 15) を §3.5 / §4 / §5 に | ユーザー承認、実装着手 | |
| 2026-10-31 | v0.6.1 C0 | T1 レビューの追従: param 禁止語を閉じた句の表に (bare `point` と「`price` を含む」を廃止、§3.3)、exact は共通有限 1 以上・atol 1e-12・比較不能は不一致・exact 対は単調/類似から除外・warning は全部集める (§3.5) | /code-review high (T1) の #5 / #6 / #1 / #2 | |

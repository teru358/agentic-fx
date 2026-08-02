# Phase 2 プラン 7: plugin 機構 + signals + シグナル起動 実装プラン (v4 — codex R1 + opus R2 + codex R3 + ユーザー裁定 D1〜D5 反映、承認済み 2026-08-02)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** §6 plugin 機構 (indicator / signal / strategy・サンドボックス実行・承認フロー) と §5 strategy シグナルによる Mission 起動を、プラン 6 のバックテスト基盤の上に実装する。

**Architecture:** plugin は純関数としてサブプロセスサンドボックス (セッション型 IPC) で実行し、入力 DataFrame はハーネスが供給する。strategy の閉じた提案は `IntentSource` に変換してプラン 6 の `run_replay`/`run_in_sample` で評価し、承認は approval_requests (kind=plugin、コンテンツハッシュに対して) で行う。シグナル起動は scheduler の発火条件拡張 + TradeLoop の signal-aware lifecycle で実現し、`origin`/executor/Risk Gate は一切触らない。

**Tech Stack:** Python 3.12 / uv / pytest / pandas (既存依存) / SQLite / サブプロセス IPC (JSON 行)

**プラン規約 (プラン 6 と同一の SDD 運用):** task 完了ごとに停止してユーザー確認 / 全 task で sonnet + codex 並行レビュー + コントローラ独立検証 / レジャー: `.superpowers/sdd/<本プラン名>/progress.md`

## ⚠ spec 文言からの裁定差分 (プラン承認時に人間の明示確認が必要な 5 点)

| # | spec 文言 | 本プランの裁定 | 根拠 |
|---|---|---|---|
| D1 | §6: strategy の exit 表現は ①SL/TP 水準 ②`evaluate()` の毎バー exit 判定の 2 通り | **本プランは ①levels のみ**。`exit_mode: evaluate` はローダーが「未対応」として reject (fail closed)。② は将来プランへ起票 | ② には保有状態 (order_id 等) を IntentSource に注入する設計 = プラン 6 コアの契約変更が必要。中途半端な模倣 (ハーネス既定則の決済) は帰属規則を破る (codex R1 C1) |
| D2 | §5: 「適用範囲: エグジット先行、エントリーはバックテストの実証後に判断」 | **シグナル起動はオープンポジションまたは未約定指値が存在する場合のみ発火** (起動条件で実装)。ポジションゼロなら pending のまま次の cron が拾う。**承認の帰結 (opus R2 I4): 起動された Mission の判断範囲は制約しない — ポジション保有中のシグナル起動 Mission は新規エントリー intent も出せる。「保有中はシグナル経由の新規エントリーも許容する」ことの承認である — これを意図として認めるか?** | 判断範囲を狭める仕組みは §5 の Mission 統一設計に反する。起動条件なら 1 条件で spec の「エグジット先行」に忠実 (codex R1 I1) |
| D3 | §6: 「config.yaml の再承認免除は人間がローカルで明示的に編集した場合のみ」 | 免除の自動判別は作らない。**ハッシュ不一致 = 一律未承認 (fail closed)**。人間編集後は CLI `afx plugin bless <name>` (検証実行 + approval 作成 + 即 approved) の 1 コマンドで再承認 | 「人間の明示操作 1 回」という意図を、agent が迂回できない形で満たす (codex R1 I2)。**前提: 改善ループに汎用シェル/コマンド実行ツールを与えない** (プラン 9 引き継ぎに明記 — opus R2 M7) |
| D4 | §5 必須事項 5: 「排他スロットは non-blocking で取得し、取れなければ起動しない (鮮度を失った判断を後から実行しない)」 | **non-blocking 取得は本プランでは実装しない** — 現配線は tick 全体が core_lock (RLock) 下で走り `on_trade_mission` は同一スレッド再入のため、`acquire(blocking=False)` は恒久的に成功する no-op (opus R2 C1 で実証)。代替として **claim 時の鮮度ゲート** (`bar_ts` が `宣言 tf × plugin.signal_freshness_bars (既定 2)` より古い pending は claim せず `abandoned` + 通知) を入れる。**これは spec 必須事項 5 の完全代替ではなく「staleness を有界化する暫定緩和」である (codex R3 I2)**: 競合意味論 (ロックを取れなかったイベント駆動判断をその場で諦める) は満たさず、Mission 実行と競合した間に閉じたバケットのシグナルも鮮度窓内なら後から実行される (最大遅延 = freshness_bars × tf)。真の non-blocking はロックスコープが分離されるプラン 8 (worker 化) で実装する。**承認者への問い: この暫定緩和 (有界遅延の許容) + プラン 8 送りを認めるか?** | 恒久 no-op の「実装したふり」よりも、目的 (無期限に古い判断の排除) を検証可能な形で先に満たす。鮮度ゲートは滞留ドレイン問題 (D2 でポジションゼロ中に溜まった古いシグナルが起動枠を食い潰す — opus R2 I7) も同時に解決する |
| D5 | §5: 「扱う足は `datafeed.intervals` で決める (plugin とバックテストが使える)」 | **signal/strategy plugin の宣言 timeframe は `("15m", "1h", "4h", "1d")`** (= RESAMPLE_TIMEFRAMES の 1m 以外)。**承認バックテストの評価格子は plugin 宣言足に一致させる** (`eval_timeframe = meta.timeframe`、"1d" のみ "24h" に写像 — `runner._parse_timeframe` は `(\d+)(m\|h)` のみ受理、epoch 錨なので 24h と 1d の格子は同一)。5m/1m 特化 plugin のみ本プランでは不可 (1m は約定判定の足そのもの・5m は resample 供給面の拡張が要る) | ユーザー指摘 (2026-08-02): day ベースでは 15m は有効 — 当初案の 1h 固定評価格子では 15m plugin が 1/4 しか評価されず承認成績が本番と乖離する (opus R2 I3) が、**評価格子を宣言足に合わせれば乖離自体が消える**ため制限を 15m まで緩和。宣言 tf と評価格子の一致はテストで固定 (Task 6) |

## Global Constraints (spec 逐語 — 全 task の暗黙要件)

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ
- 発注・SL 変更・クローズ・資金保護は LLM に委ねない。決定論的コードで強制。drawdown kill switch は config で無効化不可
- **plugin はシグナルを出すだけで発注はしない** (§6)。シグナル起動は Risk Gate・sizing・kill switch・承認ゲートを一切迂回しない (§5)
- **`origin` は触らない** (§5)。executor の起動元検証は無変更
- **承認前の plugin はロードされない** (§6)。承認は内容ハッシュに対して行う
- **content_hash の算出規則は 1 箇所** (§5 必須事項 2): `sha256(b"plugin.py\0" + <plugin.py bytes> + b"\0config.yaml\0" + <config.yaml bytes>)`。承認ハッシュと signals の content_hash は**同一関数**
- **サンドボックスの threat model**: 目標は「LLM が善意で書いた plugin の事故 (誤 I/O・暴走) の防止」であり、**敵対的コードへの完全隔離ではない** — 最終防衛線は人間承認 (§6)。sandbox.py docstring に焼き込む
- **holdout 遮断 (§6 の 8 項目)**: 改善ループ向け面は期間・端点・件数を受け取らず返さない。`get_signals` の since は最大 lookback (既定 24h)。改善ループのツールセットに get_signals / bless を含めない
- **strategy は exit を省略できない** (§6)。ハーネスの既定決済規則で補わない (帰属規則)。本プランの exit は D1 のとおり levels のみ
- **バックテスト成績は実運用成績の予測値ではない (足切り専用)** — 結果表示・approval payload に焼き込む (§6)
- テストに実スリープ・実 HTTP・実 git・乱数・実時刻を混入させない。実 DB (`data/`) に触れない (例外: Task 2 の sandbox timeout 実測 1 秒上限のみ)
- **本番経路とテスト経路の乖離を作らない** (opus R2 の教訓): 本番が渡す now は非分格子 (`datetime.now(timezone.utc)`)・tick は core_lock 保持下 — この 2 前提を再現しないテストで「本番だけ死ぬ/本番だけ動かない」機能を緑にしない。該当 task のテストには**非分格子 now・実配線相当のロック状態**のケースを必ず含める
- uv / TDD / example 同期 / trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## ファイル構成 (新設・変更の全体マップ)

```
src/agentic_fx/plugin/            # ── plugin 機構 (新パッケージ) ──
├── __init__.py
├── loader.py        # discover + config 検証 + content_hash + 承認照合 (Task 1)
├── sandbox.py       # AST 検査 + セッション型サブプロセス実行 (Task 2)
├── worker.py        # 子プロセス側エントリ (Task 2)
├── signal_eval.py   # signal 検出精度評価 (Task 4)
├── strategy_adapter.py  # StrategyDecision → IntentSource (Task 5)
├── approval.py      # 検証実行 + approval 発行 + bless (Task 6)
└── signal_producer.py   # バケット進行検出 → signals 書き込み (Task 8)
src/agentic_fx/backtest/timeframes.py  # 1m → 上位足の読み取り時リサンプル (Task 0)
src/agentic_fx/store/signals.py        # signals CRUD (Task 7)
src/agentic_fx/store/missions.py       # set_trigger / signals_rate_ok (Task 8)
src/agentic_fx/store/backtest_runs.py  # latest_in_sample_metrics (Task 9)
src/agentic_fx/tools/plugin_loader.py  # 承認済み indicator 合成 (Task 3)
src/agentic_fx/tools/signal_tools.py   # get_signals (Task 9)
src/agentic_fx/loops/trade_loop.py     # signal-aware lifecycle (Task 8)
src/agentic_fx/core/scheduler.py       # cron/signal 分離 + signal 保守フック (Task 8)
src/agentic_fx/service.py              # producer/reclaim/ツール登録の配線 (Task 3/8/9)
docs/examples/plugins/{rsi_indicator,sma_cross}/   # サンプル (Task 1)
```

注: spec §11 は `tools/plugin_loader.py` に discovery を置くが、sandbox・評価・承認は「LLM に見せる薄い口」ではないため機構本体は `src/agentic_fx/plugin/` に置き、tools 層には合成の口のみを置く。依存方向 (`loops → tools → plugin → datafeed/store → core`) は §11 の一方向原則を維持。

---

### Task 0: 上位足供給 — 読み取り時リサンプル (プラン 6 申し送りブロッカーの解消)

**Files:**
- Create: `src/agentic_fx/backtest/timeframes.py`
- Modify: `src/agentic_fx/backtest/analysis.py` (`_load_returns` を 1m + resample 経由に変更)
- Test: `tests/backtest/test_timeframes.py`, `tests/backtest/test_analysis.py` (フィクスチャの 1m 化)

**設計判断 (ユーザー裁定 2026-08-02):** 導出保存ではなく**読み取り時リサンプル**。①導出保存は ohlcv の既存行不変契約と衝突 ②spec §5「細かい足から resample」と同一思想 ③既存 `datafeed.bars.resample` は `BAR_ANCHOR="epoch"` でバックテストのバケット錨と整合済み。

**Interfaces:**
- Consumes: `store/ohlcv` の 1m 行 (SQL 直読み・単一 source) / `datafeed.bars.resample` / `datafeed.bars.bars_to_df` / **`datafeed.bars.pandas_rule(interval)` (既存の interval→pandas freq 写像 — `_TF_RULE` を新設しない**。既存は `"1d"→"1D"` を意図的に採っており再複製は食い違いの温床 — opus R2 I6)
- Produces:
  - `RESAMPLE_TIMEFRAMES = ("1m", "15m", "1h", "4h", "1d")` — 本モジュールの許容 timeframe の正規列挙 (分析用に 15m を含む)。`PLUGIN_TIMEFRAMES = ("15m", "1h", "4h", "1d")` — **signal/strategy plugin の宣言 tf の正規列挙 (D5 — 1m を除く RESAMPLE_TIMEFRAMES)。将来の拡張点はこの定数 + RESAMPLE_TIMEFRAMES/TF_MINUTES + Task 7 の CASE 式の 3 箇所に集約されている (5m 追加は小 task 1 個で可能な設計)**。`TF_MINUTES = {"1m": 1, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}` / `floor_to_bucket(ts, timeframe) -> datetime` (UTC epoch 錨への切り下げ — Task 8 producer が使う)
  - `load_resampled_frame(conn, symbol, timeframe, *, source, since=None, until=None, max_bars=None) -> pd.DataFrame` — timeframe 列挙制。"1m" は resample せず返す。until は**排他**。**末尾の部分バケット (until 時点で終端未確定) は落とす** (先読み防止)。since/until は aware 必須。**`max_bars` 指定時は末尾 max_bars 本のみ返し、SQL 読み出しも `until - max_bars×tf×2` (安全係数 2 — 1m 欠損を許容) まで**に制限する (毎評価の全履歴読みを防ぐ — opus R2 I1/I9)
  - **since の契約 (opus R2 I2 — codex R1 I6 の floor 拡張は `index >= since` フィルタと打ち消し合い死んでいたため採用しない)**: 「**since 以降に開始する完成バケットのみ**を返す」。SQL は since から素直に読む。錨は epoch 固定でデータ開始位置に依存しないため、これで部分集約は生じない — この根拠を docstring に明記
  - **部分バケットの規律 (codex R1 I7)**: バケット内の 1m 欠損は「在る分だけの集約」— runner の `_aggregate_bucket`・§6「バー欠損を市場クローズとみなさない」と同一規則。docstring + テストで固定

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_resampled_1h_bucket_anchor_and_ohlc(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100 + i, h=100 + i + 0.5,
                    l=100 + i - 0.5, c=100 + i + 0.2) for i in range(90)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                              until=H + timedelta(minutes=90))
    assert len(df) == 1 and df.index[0].to_pydatetime() == H
    assert df.iloc[0]["open"] == 100 and df.iloc[0]["high"] == 159.5


def test_partial_tail_bucket_dropped_lookahead_guard(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(120)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                              until=H + timedelta(minutes=61))
    assert len(df) == 1


def test_since_returns_only_buckets_starting_at_or_after_since(tmp_path):
    """契約: since 以降に開始する完成バケットのみ (epoch 錨なので部分集約は生じない)。"""
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100 + i, h=100 + i + 0.5,
                    l=100 + i - 0.5, c=100 + i) for i in range(120)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                              since=H + timedelta(minutes=30),
                              until=H + timedelta(minutes=120))
    assert len(df) == 1 and df.iloc[0]["open"] == 160  # [H+1h,) のみ・欠け open なし


def test_max_bars_limits_result_and_sql_window(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(300)]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    # codex R3 M2: SQL 側の読み出し下限も観測する (tail() で誤魔化せないよう
    # set_trace_callback で発行 SQL のバインド値を記録し、bar_time >= の
    # パラメータが until - max_bars*tf*2 に一致することを assert する)
    seen_sql: list[str] = []
    conn.set_trace_callback(seen_sql.append)
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                              until=H + timedelta(minutes=300), max_bars=2)
    conn.set_trace_callback(None)
    assert len(df) == 2 and df.index[-1].to_pydatetime() == H + timedelta(hours=3)
    assert any((H + timedelta(minutes=300 - 2 * 60 * 2)).isoformat()
               in s for s in seen_sql)  # SQL 窓の下限が実際に渡っている


def test_intra_bucket_gap_aggregates_present_bars(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(60) if i != 30]
    ohlcv.import_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                              until=H + timedelta(minutes=60))
    assert len(df) == 1 and df.iloc[0]["volume"] == 59 * 10.0
```

- [ ] **Step 2: 実装 → green** (`pandas_rule` を使う — 新写像テーブル禁止)
- [ ] **Step 3: analysis.py の配線切替** — `_load_returns` を `load_resampled_frame` の close 列へ変更。**公開シグネチャと遮断契約 (MIN_COMMON_OBS / エラー固定コード / since 非配線 / 例外の非漏洩) は不変**。テストフィクスチャ `_series` を 1m 投入に書き換え (遮断系テストの意図は不変)。`coverage_report` の bars も resample 後の完成バー数へ
- [ ] **Step 4: コスト実測 (opus R2 I9)** — 2 銘柄 × 1 年相当の合成 1m で `corr_matrix` (1h) の実行時間を実測し報告書に記録。1 呼び出し数秒超なら resample 結果のメモ化 (conn/symbol/tf/source/until キー) を同 task 内で追加する判断材料とする (推測で入れない — 実測駆動)
- [ ] **Step 5: 全体 green + Commit** — `git commit -m "feat: 上位足の読み取り時リサンプル (分析面の生産者不在ブロッカー解消)"`

---

### Task 1: plugin 契約 + discovery + content_hash

**Files:**
- Create: `src/agentic_fx/plugin/__init__.py`, `src/agentic_fx/plugin/loader.py`
- Create: `docs/examples/plugins/rsi_indicator/`, `docs/examples/plugins/sma_cross/` (各 plugin.py + config.yaml + test_plugin.py)
- Modify: `src/agentic_fx/core/contracts.py` (Signal / StrategyDecision)、`.gitignore` (`plugins/`)
- Test: `tests/plugin/test_loader.py`

**Interfaces:**
- Produces:
  - `contracts.Signal` (frozen): `plugin, pair, timeframe, bar_ts: datetime, direction ("long"|"short"), strength: float (0..1), rationale: str, stop_loss: float | None = None, take_profit: float | None = None`
  - `contracts.StrategyDecision` (frozen): `action ("open"|"hold"), direction, entry_type ("market"|"limit"), limit_price, stop_loss, take_profit, rationale` — **action="open" は stop_loss 必須**。`"exit"` は語彙に含めない (D1 — 実行時 fail closed は Task 2)
  - `loader.PluginMeta` (frozen): `name, kind, path: Path, params: dict, timeframe: str | None, pairs: tuple[str, ...], max_bars: int, content_hash: str`
  - config.yaml スキーマ: `kind` (必須・列挙 3 値) / `params` (dict、既定 {}) / `timeframe` (signal/strategy 必須、**`timeframes.PLUGIN_TIMEFRAMES` の列挙制 — D5**) / `pairs` (signal/strategy 必須・非空 list。**discover は形式検証のみ** — settings.pairs 照合は消費側 (Task 6/8)) / `exit_mode` (strategy 必須、`"levels"` のみ受理 — `"evaluate"` は「未対応」reject。D1) / `max_bars` (必須ではない、既定 200、**1〜`settings.plugin.max_bars_limit` (既定 1000)** — plugin に渡す DataFrame の**末尾最大本数の上限宣言** (「常に同一の実 df 長」の保証ではない — source・欠損・履歴開始で実行時の行数は変わり得る。codex R3 M1)。承認時と本番で同じ上限値を使う — opus R2 I1。**df 長が不足するときの warmup 判断は plugin 側の責務** (len(df) を自ら検査し不足なら hold — サンプル plugin に実装例を置く)。上限照合は消費側)
  - `loader.content_hash(plugin_dir: Path) -> str` — spec 逐語規則。signals・承認はこの関数のみを使う
  - `loader.discover(plugins_dir: Path) -> list[PluginMeta]` — 1 plugin = 1 フォルダ (平坦)。3 ファイル欠落 skip + warning。config 不正はそのフォルダのみ reject + warning。kind 別 AST 検証: `compute(df, params)` / `detect(df, params)` / `evaluate(df, indicators, signals, params)` の関数存在 + 引数名一致

- [ ] **Step 1: 失敗するテストを書く** — ①content_hash の手計算照合 ②1 バイト変更でハッシュ変化 ③正常 plugin → PluginMeta (pairs/timeframe/exit_mode/max_bars) ④exit_mode: evaluate reject ⑤**timeframe: 5m の strategy が D5 で reject (15m は受理される)** ⑥kind 列挙外 / evaluate 欠落 / pairs 空 / max_bars 0 → reject ⑦3 ファイル欠けは skip。フィクスチャは tmp_path 生成
- [ ] **Step 2: 実装 → green**
- [ ] **Step 3: サンプル 2 個** — `rsi_indicator` (indicator) / `sma_cross` (strategy, 1h, levels, pairs: [USDJPY], max_bars: 200)。docs/examples は discover 対象外
- [ ] **Step 4: Commit** — `git commit -m "feat: plugin 契約 + discovery + content_hash (spec 逐語の単一算出規則)"`

---

### Task 2: サンドボックス実行 (AST 検査 + セッション型サブプロセス + resource limit)

**Files:**
- Create: `src/agentic_fx/plugin/sandbox.py`, `src/agentic_fx/plugin/worker.py`
- Test: `tests/plugin/test_sandbox.py`

**Interfaces:**
- Consumes: Task 1 `PluginMeta`
- Produces:
  - `sandbox.check_source(path: Path, *, extra_allowed: frozenset[str] = frozenset()) -> None` — AST 検査。**import allowlist**: `math`, `statistics`, `numpy`, `pandas` (+extra_allowed — test_plugin.py 用に `pytest`/`plugin`)。**denylist (codex R1 C6)**: `open/eval/exec/compile/__import__/input/breakpoint/globals/getattr/setattr/delattr/vars` の名前出現、`read_` 前綴り属性全て、`to_` 前綴り属性 (`to_dict/to_list/to_numpy/to_pydatetime` を除く)、`load/loads/loadtxt/genfromtxt/save/savetxt/savez/memmap/fromfile/tofile/pickle/unpickle`、`pandas.io`/`numpy.lib.npyio` の import、`global` 文。**構文名ベース検査であり動的呼び出しは防げない — threat model を docstring に明記**
  - **セッション型 IPC (opus R2 I1 の性能対応 — バックテストは同一 plugin を数千回評価するため 1 実行 1 プロセスでは成立しない)**: `sandbox.PluginSession(meta, *, settings)` — `__enter__` で worker サブプロセスを 1 個起動し、`call(payload: dict) -> dict` を stdin/stdout の **JSON 1 行ずつ**で繰り返せる。`__exit__`/timeout/上限超過で kill。`sandbox.run_plugin(meta, payload, *, timeout_sec, settings)` は「セッション 1 回だけ」の薄いラッパ (producer 等の単発評価用)
  - 子プロセス: `[sys.executable, "-m", "agentic_fx.plugin.worker", str(meta.path)]`。**env 最小化** (`PATH`/`PYTHONPATH=<src>` のみ — 構築関数を単体テストで直接検証)。cwd = plugin フォルダ、`start_new_session=True`。1 call の timeout (既定 10s)・stdout 上限 1MB。**戻り値の kind 別スキーマ検証**: indicator → `dict[str, 有限 float]` / signal → `{direction, strength, rationale, stop_loss?, take_profit?}` dict の list — **`bar_ts` キーは受理しない (存在したら SandboxError)**: bar_ts はハーネス (producer/signal_eval) が評価対象バケットから設定する監査キーであり、plugin 任意値にすると鮮度ゲート回避・誤 abandoned・dedupe 不成立が起きる (codex R3 I4) / strategy → StrategyDecision 互換 dict (`action` が open/hold 以外 — "exit" 含む — は SandboxError、open で stop_loss 欠落・非正値も SandboxError)
  - `worker.py`: 起動直後に `resource.setrlimit` (RLIMIT_CPU 累積 (セッション寿命に対する上限 — 既定 60s)・RLIMIT_AS 512MB・RLIMIT_NPROC) → `socket`/`urllib`/`http` を sys.modules にダミー登録して塞ぐ → plugin.py を 1 回 import → stdin の JSON 行ごとに指定関数を実行し結果を 1 行で返すループ
- settings 追加 (example + config.py 同期): `plugin:` — `sandbox_timeout_sec: 10` / `sandbox_session_cpu_sec: 60` / `sandbox_memory_mb: 512` / `sandbox_output_max_bytes: 1048576` / `max_bars_limit: 1000`

- [ ] **Step 1: 失敗するテストを書く** — ①正常 indicator の run_plugin ②セッションで同一プロセスの複数 call (worker の pid が変わらないことを payload echo で検証) ③`import os` reject ④`pd.read_csv`/`df.to_csv` reject・`to_numpy` は通る ⑤`pickle` reject ⑥無限ループが timeout 1s で SandboxError (実測上限 1 秒) ⑦action="exit" SandboxError ⑧stop_loss 無し open SandboxError ⑨env 構築関数に `AFX_*` 非伝播 (直接検証)
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: plugin サンドボックス (AST denylist + セッション型サブプロセス + resource limit)"`

---

### Task 3: indicator 検証パス + get_indicators 合成

**Files:**
- Create: `src/agentic_fx/tools/plugin_loader.py`
- Modify: `src/agentic_fx/tools/market_tools.py` (build 拡張) / **`src/agentic_fx/service.py` (build の src 側唯一の呼び出し元 — service.py:311。opus R2 M2。テスト側の呼び出し ~10 箇所も更新)**
- Test: `tests/tools/test_plugin_loader.py`

**Interfaces:**
- Consumes: Task 1 loader / Task 2 sandbox / `store/approvals`
- Produces:
  - `plugin_loader.approved_plugins(conn, plugins_dir: Path) -> list[PluginMeta]` — `kind='plugin'`・`status='approved'`・payload `content_hash` 一致のみ。不一致は未承認 + warning。再承認は bless (Task 6)。**反映は次回起動時 (hot reload しない — YAGNI)**
  - `market_tools.build(provider, econ, settings, *, indicator_plugins: list[PluginMeta] | None = None, sandbox_run=None)` — `get_indicators` に承認済み indicator の出力を `{"plugin:<name>": {...}}` 合成。SandboxError/timeout は当該キーを落とすだけ (組み込みは必ず返す — fail-open)。service.py での `approved_plugins` 結果の配線まで本 task
- pytest 実行 (テスト同梱検証) は Task 6 側 — 本 task は approvals.create + decide で承認状態を直接作る

- [ ] **Step 1: 失敗するテストを書く** — ①未承認は出ない ②承認 + 一致は出る ③承認後の編集で出ない ④get_indicators 合成 (fake sandbox_run) ⑤SandboxError でも組み込みは返る ⑥全呼び出し元更新 (全スイート green)
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: 承認済み indicator plugin の get_indicators 合成 (ハッシュ照合ロード)"`

---

### Task 4: signal 検出精度評価 (ラベル付きサンプル)

**Files:**
- Create: `src/agentic_fx/plugin/signal_eval.py`
- Test: `tests/plugin/test_signal_eval.py`

**設計 (spec §6「測り方は Phase 2 で定める」の確定):** plugin フォルダ同梱 `labels.json`: `{"bars": [[iso, o, h, l, c, v], ...], "expected": [{"bar_ts": iso, "direction": "long"}, ...]}`。ハーネスが DataFrame 化し sandbox で `detect` を実行、`(bar_ts, direction)` 完全一致で突合。

**Interfaces:**
- Produces: `signal_eval.evaluate_detection(meta, *, sandbox_run=None, settings) -> dict` — `{"precision", "recall", "tp", "fp", "fn", "n_labels"}`。labels.json 欠落・parse 不能・expected 空・kind != "signal" は ValueError (fail closed)

- [ ] **Step 1: 失敗するテストを書く** — fake sandbox_run「2 一致 + 1 余分」→ precision=recall=2/3, tp=2, fp=1, fn=1。labels.json 欠落 → ValueError
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: signal 検出精度評価 (ラベル付きサンプルの precision/recall)"`

---

### Task 5: strategy 評価アダプタ (plugin → IntentSource) + CLI --plugin

**Files:**
- Create: `src/agentic_fx/plugin/strategy_adapter.py`
- Modify: `src/agentic_fx/backtest/cli.py` (`--plugin`。**既存 `--proposal-file` は `required=True` — 必須解除 + 相互排他 (どちらか一方必須) の実装を含む** — opus R2 M6)
- Test: `tests/plugin/test_strategy_adapter.py`, `tests/backtest/test_cli.py` (追記)

**Interfaces:**
- Consumes: Task 0 `load_resampled_frame`/`TF_MINUTES` / Task 1 `PluginMeta` / Task 2 `PluginSession` / プラン 6 `IntentSource` / `holdout.run_in_sample` / `backtest_runs.save_human_run`
- Produces: `strategy_adapter.build_intent_source(meta, *, conn, pair: str, source, settings, session=None) -> IntentSource` (1 IntentSource = 1 pair。session は注入シーム — 既定は内部で `PluginSession` を生成・保持し、**バックテスト全体で 1 プロセス**を使い回す):
  1. **発火条件**: `closed_bar.ts + eval_tf 幅` が plugin 宣言 tf のバケット境界 (epoch 錨) に一致する tick のみ。**eval_tf 幅は `closed_bar.interval` から導出** (runner の `_aggregate_bucket` が interval=eval_timeframe を設定する — opus R2 M10。新引数を足さない)。乗らないバーは None
  2. `load_resampled_frame(conn, pair, meta.timeframe, source=source, until=closed_bar.ts + eval_tf 幅, max_bars=meta.max_bars)` — **max_bars は承認時・本番で同じ宣言値** (opus R2 I1)
  3. session で `evaluate(df, indicators=None, signals=None, params)` (indicators/signals 供給は将来拡張 — None 固定)
  4. `action="open"` → open intent dict (`{"action": "open", "pair", "direction", "entry_type", "horizon": "day", "stop_loss", "take_profit", "limit_price" (limit のみ), "expires_in": "4h" (limit のみ), "confidence": 0.5, "reasoning": rationale}`)。`"hold"` → None。**SandboxError は送出したまま貫通** (fail closed)
- CLI `--plugin <name>`: discover + `check_source` は通す (承認は要求しない — 手元評価は承認前が自然)。`--symbol` が meta.pairs 外なら rc=1。`save_human_run(plugin_ref=<plugins/name>, content_hash=..., kind="strategy")`

- [ ] **Step 1: 失敗するテストを書く** — ①1h 評価格子で 4h plugin は 4h 境界終端の bar のみ評価 (呼び出し記録) ②渡る df の末尾が評価時点より未来を含まない + **len(df) <= max_bars** ③open → intent dict 写像 ④SandboxError 貫通 ⑤統合: 1h 宣言 plugin + 5 時間 replay で intent が orders に到達 (セッション 1 プロセスであることも pid 記録で確認)
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: strategy 評価アダプタ (levels exit・max_bars 契約・セッション実行) + afx backtest run --plugin"`

---

### Task 6: 承認フロー (kind=plugin) + bless CLI

**Files:**
- Create: `src/agentic_fx/plugin/approval.py`
- Modify: `src/agentic_fx/backtest/cli.py` (`afx plugin submit <name>` / `afx plugin bless <name>` 配線)
- Test: `tests/plugin/test_approval.py`

**Interfaces:**
- Consumes: Task 1/2/4/5 + `holdout.run_in_sample` / `store/approvals`
- Produces:
  - `approval.submit_plugin(conn, meta, *, settings, now, pytest_runner=None, sandbox_run=None, run_in_sample_fn=None) -> int`:
    1. `check_source(plugin.py)` — 不合格 ValueError (request を作らない)
    2. `check_source(test_plugin.py, extra_allowed={"pytest", "plugin"})` → pytest 実行 (シーム経由)。**不合格は ValueError — approval_request にしない** (§6 品質ゲート)
    3. kind 別検証: indicator → 手順 2 のみ / signal → `evaluate_detection` / strategy → **`meta.pairs` の各 pair (settings.pairs 内であることをここで検証 — 外は ValueError) について、`run_in_sample(settings, history_conn=conn, symbol=pair, source="dukascopy", intent_source=build_intent_source(meta, conn=conn, pair=pair, source="dukascopy", settings=settings), eval_timeframe=<meta.timeframe ("1d" は "24h" に写像 — D5>, plugin_ref=<plugins/name>, content_hash=meta.content_hash, kind="strategy", now=now)`** (実シグネチャ逐語 — opus R2 M4。期間は run_in_sample が所有し submit_plugin は受け取らない。**評価格子 = 宣言足の一致は D5 の裁定 — 15m plugin が 1h 格子で 1/4 評価される乖離を作らない**)
    4. `approvals.create(kind="plugin", payload={"name", "kind", "content_hash", "test_file_hash" (sha256 監査値 — ロード時検証には使わない), "pytest": {...}, "metrics": <kind 別 — strategy は pair ごとの dict>, "evaluable": <strategy — trades>=30>, "eval_source": "dukascopy", "live_source": settings.plugin.producer_source, "note": "バックテスト成績は実運用成績の予測値ではない (足切り専用)"})` — **承認 source と本番 source の差異を payload で人間に見せる** (opus R2 I1)。evaluable=False でも request は作る (却下権も人間)
    5. **実行時間を報告書に実測記録** (opus R2 I1 の性能懸念 — セッション型で解消される見込みの実証)
  - `approval.bless(conn, meta, *, settings, now, ...) -> int` — D3: submit と同一の検証 → 成功時 `approvals.create` + 即 `decide(status="approved", decided_by="human_cli")`。CLI のみから呼ぶ (改善ループ非露出 — 回帰ピンは Task 9)
- holdout はスコープ外 (プラン 9 の採用ゲート)

- [ ] **Step 1: 失敗するテストを書く** — ①pytest 失敗で行ができない ②indicator 成功で行 + content_hash/test_file_hash ③strategy は pairs の各 pair で run_in_sample_fn が実シグネチャ相当の kwargs で呼ばれる (fake が kwargs 記録) ④pairs が settings.pairs 外 ValueError ⑤test_plugin.py の `import os` reject ⑥bless 成功で approved 行 / 検証失敗で何も作らない
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: plugin 承認フロー (pytest + kind 別検証 + bless 再承認 CLI)"`

---

### Task 7: signals テーブル + store

**Files:**
- Modify: `src/agentic_fx/store/db.py` (signals DDL + TABLE_NAMES 14)
- Create: `src/agentic_fx/store/signals.py`
- Test: `tests/store/test_signals.py`, `tests/store/test_db.py`

**Interfaces (用語: schema は 4 状態 pending/claimed/consumed/abandoned。§5「3 段階」は正常系遷移):**
- DDL (§12 逐語): `signals(id INTEGER PK, plugin TEXT NOT NULL, content_hash TEXT NOT NULL, pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','consumed','abandoned')), claimed_by_mission_id INTEGER, claimed_at TEXT, requeue_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE(plugin, content_hash, pair, timeframe, bar_ts))`
- `signals.add(conn, *, plugin, content_hash, pair, timeframe, bar_ts, kind, payload, now) -> int | None` (INSERT OR IGNORE)
- `signals.claim_oldest(conn, *, mission_id, now, freshness_bars: int | None) -> dict | None` — 原子的 UPDATE (影響行数で勝者)。bar_ts 古い順。**D4 の鮮度ゲートは行ごとの timeframe から SQL 内で cutoff を計算する** (codex R3 C1 — 単一 timedelta では 1h/4h/1d 混在の pending を正しく扱えない): timeframe は `PLUGIN_TIMEFRAMES` の列挙限定なので SQL の CASE 式 (`CASE timeframe WHEN '15m' THEN 15 WHEN '1h' THEN 60 WHEN '4h' THEN 240 WHEN '1d' THEN 1440 END × freshness_bars` 分) で表現できる。**CASE の分岐は PLUGIN_TIMEFRAMES と同期必須 — 実装時は「列挙に在るのに CASE に無い timeframe」を検出する同期テストを置くこと (将来 5m 等を足すときの唯一のハードコード点)**。**claim の UPDATE 自身の WHERE に鮮度条件 (`bar_ts >= now − cutoff`) を含める** — expire との 2 段の間に stale 行が現れても claim されない保証は claim 側にある (原子性は単一 UPDATE で成立)
- `signals.expire_stale(conn, *, now, freshness_bars) -> int` — 鮮度切れ pending の一括 `abandoned` 化 (同じ CASE 式)。claim とは独立の公開関数 (呼び出し側が通知に使う件数を返す)。claim 前に呼ぶが、呼び忘れ・競合があっても上記のとおり claim 側の鮮度条件が防波堤
- `signals.consume(conn, signal_id, *, mission_id, now)` — claimed かつ claimed_by 一致のみ
- `signals.requeue(conn, signal_id, *, now, max_requeue) -> str` — 上限超過は `abandoned` を返す
- `signals.reclaim_expired(conn, *, now, lease_min, max_requeue) -> list[str]` — **requeue と同一の上限判定を通す** (lease 回収は上限を迂回しない — codex R1 I5)。起動時 + 毎分 (配線は Task 8)
- `signals.pending_exists(conn) -> bool` / `signals.recent(conn, pair, *, since) -> list[dict]`
- settings 追加: `plugin.signal_requeue_max: 2` / `plugin.signal_lease_min: 15` / `plugin.signal_freshness_bars: 2` (宣言 tf の何バー分まで新鮮とみなすか — D4)

- [ ] **Step 1: 失敗するテストを書く** — ①重複キー 2 回目 None ②content_hash 違いは別行 ③claim_oldest が bar_ts 最古 ④2 連続 claim は別行 ⑤consume の mission_id 不一致拒否 ⑥requeue 上限超過 abandoned ⑦reclaim_expired: 期限内は不変・期限切れは requeue_count 増・上限超過 abandoned ⑧abandoned は claim 対象外 ⑨expire_stale: freshness 超過の pending が abandoned・以内は残る・claimed には触れない ⑩**1h/4h/1d 混在の pending で、1h の 3 バー前は stale・1d の 3 時間前は fresh — timeframe 別 cutoff が SQL 内で正しく効く (codex R3 C1 killer)** ⑪**expire_stale を呼ばずに claim_oldest だけ呼んでも stale 行は claim されない (claim 側の鮮度条件ピン)**
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: signals テーブル (4 状態・原子的 claim・鮮度ゲート・lease 回収も上限判定)"`

---

### Task 8: シグナル起動 (scheduler + TradeLoop lifecycle + producer)

**Files:**
- Modify: `src/agentic_fx/core/scheduler.py` — ①`_trade_mission_due` の cron/signal 分離 ②**`_last_trade` の更新箇所は `tick()` 内 (scheduler.py:189 — `_trade_mission_due` ではない。opus R2 M3) を `reason == "cron"` のみ更新に変更** ③**新コンストラクタフック `on_signal_maintenance: Callable[[datetime], None] | None = None` (既定 None = 機能無効・既存テスト互換)** — 開場ガード**内**・`_process_limit_fills` より**前**の新設位置で fail-open 呼び出し (producer + reclaim をまとめる保守フック。news/econ の data hook は開場判定より前にある — 「同じ位置」ではなく別の新位置であることを明記。opus R2 I8-1)、`signal_due_fn: Callable[[datetime], bool] | None = None` (既定 None)
- Modify: `src/agentic_fx/loops/trade_loop.py` — **signal-aware lifecycle。実コードの `_run_once_impl` は `_build_prompt` → `Mission` → `missions.start` の順であり、手順どおりにするには関数の再構成が必要 (opus R2 M1)。healthcheck (現在は先頭で `return None`) は claim より前に置く** (claim 済みで healthcheck 死亡 → requeue 漏れを構造的に防ぐ)
- Modify: `src/agentic_fx/store/missions.py` — `set_trigger(conn, mission_id, trigger)` / `signals_rate_ok(conn, now, settings) -> bool` (docstring: missions.trigger LIKE 'signal%' の行が DB 永続カウンタそのもの)
- Modify: `src/agentic_fx/service.py` — `on_signal_maintenance` の構築 (producer + reclaim_expired)・`signal_due_fn` の構築・起動時 reclaim 1 回
- Create: `src/agentic_fx/plugin/signal_producer.py`
- Test: `tests/core/test_scheduler_signal.py`, `tests/loops/test_trade_loop_signal.py`, `tests/plugin/test_signal_producer.py`

**Interfaces:**

*scheduler:*
- `_last_trade` → `_last_cron_trade` 改名 + tick 側の更新条件を `reason == "cron"` に限定 (§5 必須事項 4)
- `_trade_mission_due(now)`: ①cron (1h) → `"cron"` ②`signal_due_fn` が真 → **`"signal"` (plugin 名なし候補 — 実 plugin は claim 結果で確定。codex R1 I3)**
- `signal_due_fn` (service 側構築): `signals.pending_exists` AND `missions.signals_rate_ok` AND **D2 条件 (オープンポジション or pending_fill の orders 行が存在)**。レート制限は最短間隔 `signal_min_interval_min` (既定 10)・日次 `signal_daily_max` (既定 12)・境界 `trading_day_start`。**判定→起動の原子性は単一スレッド tick 前提 (プラン 8 で再検討 — docstring 明記)**

*TradeLoop (signal-aware lifecycle — §5 必須事項 1):*
- `trigger == "signal"` の実行手順: ①healthcheck (失敗なら claim 前に離脱) ②`missions.start(..., trigger="signal")` (**暫定値 "signal" — NULL 窓を作らない。§12 の「loop='trade' 以外は NULL」不変条件を保つ。opus R2 M5。`signals_rate_ok` のクエリは `LIKE 'signal%'` でこの暫定値も数える**) ③`signals.claim_oldest(mission_id=...)` — **claim 失敗なら missions を即 finalize (status="skipped") し LLM を起こさない** ④claim 成功 → `missions.set_trigger(mission_id, f"signal:{row['plugin']}")` ⑤プロンプトにシグナル行を注入して runner 実行 ⑥**consume/requeue の確定規則: 「プロンプトに実際に載せた時点で確定」(§5 表) は*どの Mission が確定できるか*を定め、「runner エラー・タイムアウト・パース失敗は pending へ」(§5 箇条書き) が*いつ確定するか*を定める — 順序は 注入 → runner → **パース成功の時点で `signals.consume`** → その後 `executor.handle_intent`。**executor の例外は requeue しない (consume 済み — 執行後の requeue は二重発注ハザード。opus R2 I5)**。runner 失敗・パース失敗は `signals.requeue` (+abandoned 通知)
- 例外時も finally で claimed のまま残さない (requeue — lease 回収を待たない)

*producer:*
- `signal_producer.evaluate_due_plugins(conn, *, plugins, now, source, sandbox_run, settings) -> int` — **バケット進行検出 (opus R2 C2 — 本番の now は非分格子のため境界一致判定は永久不成立)**: `(plugin, content_hash, pair)` ごとの**評価 cursor はメモリ辞書のみ** (テーブル追加なし)。各 tick で `floor = floor_to_bucket(now, meta.timeframe)` を計算し、**評価対象 = 「cursor より後かつ `floor` 未満に開始する確定バケット」のうち鮮度窓内 (`bar_ts >= now − tf × signal_freshness_bars`) のもの全件** (古い順)。評価が成功 (hold 含む) したバケットまで cursor を進める。**この規則が再起動・複数バケット停止・plugin 失敗の 3 つを同時に有界回復する (codex R3 I1)**: ①メモリ cursor 喪失 (再起動) 時は鮮度窓内の確定バケットを再評価する — hold だったバケットも再評価されるが冪等 (signals の UNIQUE dedupe が重複挿入を防ぎ、再評価回数は窓幅で有界) ②停止が窓を超えた分は評価しない (鮮度ゲートと同じ裁定 — 古い判断を今さら起こさない) ③sandbox 失敗時は cursor を進めず次 tick で再試行し、窓を出たら自然放棄。`until=バケット終端`、`max_bars=meta.max_bars`
- 対象: 承認済み signal/strategy × 宣言 pairs ∩ settings.pairs (外は warning スキップ)。写像: signal の `detect` → 各出力 → row (`kind="signal"`) / strategy の `evaluate` → **`action="open"` のみ** row 化 (`kind="strategy"`)、`"hold"` 非保存。**row の `bar_ts` は評価対象バケットの開始時刻をハーネスが設定する (plugin 出力からは受け取らない — Task 2 のスキーマ検証で reject。codex R3 I4)**。plugin 例外・timeout は当該 plugin×pair スキップ + warning (fail-open)
- **source は `settings.plugin.producer_source` (既定 "yfinance" — live キャッシュの主系列。承認時 "dukascopy" との差異は approval payload に記載済み — opus R2 I1)**
- service: `on_signal_maintenance = lambda now: (reclaim_expired(...), evaluate_due_plugins(...))` を組んで Scheduler へ。起動時 reclaim 1 回
- settings 追加: `plugin.signal_min_interval_min: 10` / `plugin.signal_daily_max: 12` / `plugin.producer_source: "yfinance"`

- [ ] **Step 1: 失敗するテストを書く** — scheduler/service: ①pending + ポジションありで "signal" ②ポジション・pending_fill ゼロで起動しない (D2) ③シグナル起動後も cron 締切不変 (2 tick) ④10 分以内・日次 12 回超過は起動しない・ロールオーバーでリセット ⑤on_signal_maintenance が開場中のみ呼ばれ、例外が tick を殺さない (fail-open)。trade_loop: ⑥claim → set_trigger → プロンプト搭載 (FakeRunner 受信 assert) → パース成功時 consumed ⑦runner/パース失敗で requeue ⑧**executor.handle_intent が例外でも consumed のまま (requeue されない — 二重発注ピン)** ⑨claim 失敗で skipped・LLM 不起動 ⑩healthcheck 失敗は claim 前離脱。producer: ⑪**非分格子 now (`12:00:03.412` 相当) でバケット進行時に 1 回だけ発火** (opus R2 C2 killer) ⑫同一バケット内の複数 tick で再評価しない ⑬4h plugin は 4h 進行のみ ⑭hold 非保存 + **hold 後の cursor 喪失 (producer 再生成) → 窓内バケットが再評価されるが signals 行は増えない (冪等回復 — codex R3 I1)** ⑮**2 バケット超の停止 → 鮮度窓内のみ catch-up・窓外は評価しない** ⑯**sandbox 失敗 → cursor が進まず次 tick で同一バケットを再試行** ⑰pairs 積集合外スキップ ⑱dedupe ⑲**row の bar_ts が評価バケット開始時刻に強制される (plugin 出力の bar_ts は Task 2 で reject 済み — aware UTC・未来値でないことを assert)**。鮮度: ⑳古い pending (freshness 超過) が claim 時に abandoned (D4)
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: シグナル起動 (claim lifecycle・バケット進行検出・鮮度ゲート・cron 締切分離)"`

---

### Task 9: get_signals ツール (取引判断 loop 専用)

**Files:**
- Create: `src/agentic_fx/tools/signal_tools.py`
- Modify: `src/agentic_fx/store/backtest_runs.py` (**`latest_in_sample_metrics(conn, content_hash) -> dict | None` 新設 — 既存 in_sample_view は content_hash 絞りがなく created_at も返さないため流用不可 (opus R2 M8)。METRIC_KEYS 白リスト濾過を通し、最新判定は id 降順**)
- Modify: **`src/agentic_fx/service.py` — `registry.register_all(signal_tools.build(...))` (欠くと起動時 `_assert_tools_registered` で RuntimeError — opus R2 I8-3)**
- Modify: **`src/agentic_fx/loops/trade_loop.py` — `_TRADE_TOOLS` への `get_signals` 追加 (allowed リストの実体はここ — service.py ではない。registry 登録だけでは Mission の tools list に載らず利用できない。codex R3 I3)**
- Test: `tests/tools/test_signal_tools.py`, `tests/store/test_backtest_runs.py` (追記), `tests/loops/` (Mission.tools への露出テスト)

**Interfaces:**
- Produces: `signal_tools.build(conn, settings) -> list[ToolDef]` — `get_signals(pair, since_hours=24)`:
  - ToolDef スキーマ `{"type": "integer", "minimum": 1, "maximum": <signals_max_lookback_hours>}` + 関数側でも int 検証 (bool 除外) + クランプ (二重防御)
  - 返り値: signals 行 list。strategy 行には `latest_in_sample_metrics(content_hash)` を添付 + **「バックテスト成績は実運用成績の予測値ではない」注記同梱**
  - **`_TRADE_TOOLS` 経由で ask Mission にも露出するのは意図的** (read-only 判断材料。lookback 上限で遮断は保たれる — opus R2 M11 の明記)
  - **改善ループ非露出の回帰ピン**: improve 系 allowed 定義に `get_signals`/`plugin bless` 系が現れないことを assert (プラン 9 実装後に実リストへ接続 — 引き継ぎ)
- settings 追加: `plugin.signals_max_lookback_hours: 24`

- [ ] **Step 1: 失敗するテストを書く** — ①since_hours=100 クランプ ②strategy 行に metrics + 注記 ③signal 行には付かない ④改善ループ非露出ピン ⑤bool 拒否 ⑥latest_in_sample_metrics が METRIC_KEYS 濾過・content_hash 絞り・最新行 ⑦service 起動 (build_app 相当のテスト) が register 済みで通る ⑧**trade / ask の FakeRunner が受け取る `Mission.tools` に `get_signals` が含まれる (allowed リスト配線のピン — codex R3 I3)**
- [ ] **Step 2: 実装 → green → Commit** — `git commit -m "feat: get_signals ツール (lookback 上限・strategy 成績添付・改善ループ非露出ピン)"`

---

### Task 10: E2E (承認済み strategy → シグナル → 前倒し Mission)

**Files:**
- Test: `tests/test_e2e_plugin_signal.py`

**シナリオ (決定的・実 DB/HTTP/スリープなし):**
1. tmp_path に strategy plugin (SMA クロス 1h・levels・pairs: [USDJPY]) → `submit_plugin` (pytest_runner fake・run_in_sample_fn fake) → `decide(approved)`
2. 1m 履歴投入 (クロス 1 回・市場オープン時間帯) + オープンポジション 1 件 (D2 条件)
3. `evaluate_due_plugins` を**非分格子の now** で呼ぶ (バケット進行検出の本番経路 — sandbox 実実行) → signals pending 1 件
4. scheduler tick (FakeClock) → `"signal"` → lifecycle: claim → set_trigger=`signal:<plugin>` → **プロンプト搭載を FakeRunner の受信プロンプトで assert** → intent → Risk Gate → ペーパー発注
5. assert: signals consumed / missions.trigger / orders 到達 / cron 締切不変

- [ ] **Step 1: E2E を書く → 実装済み部品のみで green** (新規 src なし — 配線欠陥は該当 task の fix 扱い)
- [ ] **Step 2: 全体 green + Commit** — `git commit -m "test: plugin シグナル起動 E2E (承認→バケット進行検出→claim lifecycle→発注)"`

---

## プラン完了条件

1. 未承認 plugin がロードされない / 承認後の編集はハッシュ不一致で未承認化 / bless のみが再承認経路 (Task 3/6)
2. シグナル起動が Risk Gate・承認ゲート・origin 検証を一切迂回しない — E2E で実証
3. signals lifecycle (claim/consume/requeue/lease/abandoned/鮮度ゲート) + **consume は executor 実行より前で確定し執行後 requeue しない** がテストで固定 (Task 7/8)
4. cron 締切分離・D2 起動条件・**producer が非分格子 now で発火する**ことがテストで固定 (Task 8)
5. get_signals の lookback 上限と改善ループ非露出がテストで固定 (Task 9)
6. 既存全テスト (1045) + 追加分 green。実 DB・実 HTTP・実 git・乱数・実時刻なし (sandbox timeout 1 秒のみ例外)
7. 分析面の生産者不在ブロッカー解消 (Task 0) + corr_matrix のコスト実測が報告書に記録されている

## プラン 8/9 への引き継ぎ

- exit 表現② (`exit_mode: evaluate`) は D1 によりスコープ外 — プラン 9 前の小改訂として起票
- **真の non-blocking スロット取得 (D4)** はロックスコープ分離 (worker 化) とセットでプラン 8 — 本プランの鮮度ゲートは目的の代替であり機構は未実装
- レート制限判定→起動の原子性は単一スレッド tick 前提 — worker 化時に再検討
- holdout_gate 実行・approval への holdout 添付はプラン 9 の採用ゲート
- `analysis_runs` の読み取り面遮断はプラン 8 の worker 権限境界 (プラン 6 申し送り継続)
- **改善ループに汎用シェル/コマンド実行ツールを与えない** (与えると bless / pytest 経由の承認迂回が可能になる — D3 の前提。プラン 9 のツールセット設計の制約)
- 改善ループ実装時、Task 9 の非露出ピンを実 allowed リストへ接続
- indicator plugin への watch 銘柄バー供給 (§6 注記) は本プランではやらない

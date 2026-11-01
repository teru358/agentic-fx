# 収益性フロア + reject reason 漏洩の是正 設計書 v1.6.3

束 [profitability-floor] + T0 [reject-reason-leak]。起案 2026-09-12、ユーザー裁定済 (U1〜U5)、codex 設計レビュー 1 周目 18 件 (v1.1) / 2 周目 5 件 (v1.2) / 3 周目 3 件 (v1.3) を全件反映。**v1.3 で実装着手可** (codex 収束判定)。v1.4 = 変更履歴のとおり実装レビューでの設計微修正。

正の参照元:
- 本体設計書 §6 (改善 loop・品質ゲート・遮断 8 項目・plugin 機構)
- `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` — §4.2 手順4 (戦略採用ゲート)、§4.3 (バックログ状態機械)、§7.1-1 ⑧ (遮断 8 項目の統合回帰)
- `docs/superpowers/specs/2026-09-12-approval-quality-design.md` §A — 再ルート (observation 降格) の雛形

実装プラン: `docs/superpowers/plans/2026-09-12-profitability-floor.md`

---

## 0. 前提を疑う

| 通説 | 一次証拠 | 帰結 |
|---|---|---|
| 「holdout が負けていれば gate で止まる」 | `plugin/strategy_gate.py:160-171` — holdout ループは `run_holdout(...)` を**戻り値を捨てて**呼ぶだけ。`StrategyGateVerdict` (`:29-35`) に holdout のフィールドが無い。holdout の `evaluable` は `backtest/metrics.py:141` が pair ごとに `trades >= 30` で計算して `metrics_json` に入れるのみで、**誰も読まない** | holdout は「記録されるだけ」。`evaluable=false` でも approval が作られる。approval #10 (`ema_rsi_pullback`、holdout trades 7 / `evaluable=false`) が承認キューに入ったのは仕様どおりの挙動 |
| 「`insufficient_trades` 終端が収益性も見ている」 | `strategy_gate.py:151-158` — 終端条件は `total_trades = Σ per_pair["trades"]` が `EVALUABLE_MIN_TRADES` (30) 未満のときだけ。pf / avg_r / total_pnl は一切参照しない | gate に**収益性の門は存在しない**。形式検査 + pytest + 標本数だけが門 |
| 「baseline との相対比較があるのに絶対閾値はおかしい」 | 2026-08-16 設計書 §4.2-4 は baseline を「live で D4-approved の同名 strategy を同じ期間で回した結果」と定義するが、実装の `approved_row is not None` 分岐 (`strategy_gate.py:173-178`) は **baseline の再生を一切行わず** `{"ref_plugin_ref":…, "variant":"baseline"}` というマーカー dict を返すだけ。`no_strategy` 分岐 (`:180-196`) が書く行は `trades=0, pf=None, avg_r=None, total_pnl=0.0` の定数。実 DB の `variant='baseline'` 行は **0 件** | **相対比較は今日存在しない**。比べる相手が無いのでフロアは絶対閾値でしか書けない。baseline 再生の未実装は別起票 [baseline-replay-unimplemented] (U4 裁定) |
| 「R8 が『負けている候補を落とす』のを禁じている」 | 2026-08-16 設計書 `:34` の R8 = 「失敗・却下・**標本不足**は観察として残し、試行回数と標本数を履歴に添える」。§4.2-4 逐語 = 「合計取引数 < 30 → 評価不能: 承認申請を出さず observation (**悪いとは記録しない** — R8)」 | R8 は**標本不足向けの裁定**。`evaluable=true` で pf<1 は「まだ分からない」ではなく「測った結果として負けている」ので射程外。backlog 終端を `observation` (= `list_open` に残り再挑戦可) にすることで R8 の精神 (1 回で課題を捨てない) は保つ — `rejected` にはしない |
| 「フロアの不合格理由は普通に `last_result` に書ける」 | `_finalize_gate_failed` は `last_result=reason` を**逐語・無サニタイズ**で書き (`improve_loop.py:2775-2776`)、`_history_table` (`:628-633`) がそれをプロンプトに描く (`improve_context.py:75,78` の `ib.last_result` 経由) | **フロアのラベル自体が遮断 8 の対象**。→ §4 の構築規則 |
| 「§A の再ルートが holdout フロアの前例になる」 | §A の `duplicate_metrics_of:<hash>` は in_sample の `(trades, pf, avg_r)` 一致で決まる (`improve_loop.py:1860-1869`) | §A は in_sample 由来。holdout フロアは遮断 8 に対する**新しい 1 bit** であり §A の再利用では正当化できない → U1 としてユーザー裁定を仰ぎ、**許容**の裁定を得た (§4) |
| 「submit は 1 本の corridor」 | `afx plugin submit --from _human` → `switch.submit_candidate` → `_run_full_gate` → `approval.run_kind_gate` → `evaluate_strategy_adoption_gate` (`cli.py:496`, `switch.py:779`)。一方 `afx plugin submit <name>` (live `plugins/` の既存 plugin) → `approval.submit_plugin` → `_validate_kind` → `_validate_strategy` (`cli.py:514`, `approval.py:118-158`) で、**共有ゲートを通らず holdout も回さない独自実装** | corridor は 2 本ある。後者に in_sample フロアだけを足すと「submit は課す」(U2) が入口によって偽になり、2026-08-16 設計書 `:388` の「submit / `submit|bless --from _human` の全経路に同じ規則」にも反する (codex C1)。→ **legacy submit は kind=strategy を拒否する** (§3 T1-e) |
| 「`_run_full_gate` の gate 行は台帳に正しく載る」 | `_run_full_gate` (`switch.py:738-784`) は `evaluate_strategy_adoption_gate` に `record_fn` を渡さないため、in-sample/holdout 行は `holdout._run_scope` の else 分岐 (`holdout.py:155-160`) でその場 commit され **`mission_outcome=None`** になる。`latest_in_sample_metrics` は `mission_outcome IS NULL OR 'approval'` を live 扱いする (`backtest_runs.py:231`) | **人間 corridor の gate 行は今日すでに live 指標へ混入している**。フロアで落ちた行・警告付き bless の行も NULL のまま残ってしまう → §3 T1-f で非コミット sink を導入し明示 outcome を付ける (codex I2) |

### 副次的に確認した事実 (設計に効くもの)

- `pf` は `gross_loss == 0` のとき `None` (`metrics.py:136`)。`pf < min_pf` を素で書くと `TypeError`。**`pf is None` は「負け取引ゼロ」なので PASS 側**に倒す。
- `pf >= 1` かつ `avg_r <= 0` は起こり得る。`pf` は金額比 (`gross_profit/gross_loss`)、`avg_r` は 1 取引のリスク額で正規化した平均 (`pnl / (|entry−SL| × qty × contract_size)`、`metrics.py:99-103`)。SL 幅・数量が取引ごとに違うため単調関係にない → **2 つの門は冗長ではない**。
- `evaluable` は pair ごとの値 (`metrics.py:141`)。in-sample の標本門は pair 合計で判定している (`strategy_gate.py:151`)。**holdout は pair ごとに `evaluable` を見る** (§3 T1-b、指揮者既定)。
- `kill_switch_latches` は実測で approval 候補が全件 `2` (pf 1.498 の最良候補も 2)。既定 0 の門にすると全滅 → 門にしない (U3)。
- `max_drawdown` は in_sample で 3.7%〜23%。閾値の根拠が無い → 門にしない (U3)。
- (§0〜§9 は v1.0〜v1.5 時点の歴史記述。DD の扱いだけ v1.6 で変わり、**現行規則は §10**: DD は in-sample のみ門 (既定 0.20)、`kill_switch_latches` は今も門にしない。)
- `backtest_runs.mission_outcome` は `TEXT` で CHECK 制約なし (`store/db.py:299,406`) → 新終端名の追加に migration 不要。`latest_in_sample_metrics` は `mission_outcome IS NULL OR 'approval'` で絞る (`backtest_runs.py:231`) ので新しい値は live 表示から自動的に外れる。
- `in_sample_view` の本番呼び出し元は **src に 0 件** (テストのみ)。`mission_outcome` 列を返すが改善 worker からは到達しない = フロア結果の side channel 漏洩は無い。
- `settings_snapshot_hash` は `{"risk":…, "backtest":…}` のみ (`backtest_runs.py:315-320`) → **`improve.gate` を緩めても `backtest_runs.settings_hash` は変わらない**。監査痕跡は別に作る (§3 T3)。

---

## 1. 実 DB の実態 (read-only、`file:data/agentic.db?mode=ro`)

### 1.1 gate 行の分布 (`backtest_runs` 全 35 行)

| scope | variant | mission_outcome | 件数 |
|---|---|---|---|
| in_sample | candidate | NULL (旧経路) | 4 |
| in_sample | candidate | approval | 12 |
| in_sample | candidate | report | 6 |
| in_sample | candidate | observation | 2 |
| in_sample | candidate | failed | 1 |
| in_sample | no_strategy | approval | 5 |
| holdout_gate | candidate | approval | 5 |
| **holdout_gate** | **baseline** | — | **0 件 (= 相対比較の不在)** |

`evaluable=true` の candidate in_sample 行 13 件のうち pf ≥ 1 は **4 件** (すべて同一の fast=10/slow=30 SMA、pf 1.473〜1.498)。残り 9 件は pf 0.49〜0.83。

### 1.2 approval_requests 10 件 — フロアで落ちるのはどれか

| # | name | kind | in_sample | holdout | 人間の判断 | in_sample 段 | holdout 段 |
|---|---|---|---|---|---|---|---|
| 1 | rsi_indicator | indicator | — | — | rejected | 対象外 | 対象外 |
| 2 | rsi_wilder | indicator | — | — | **approved** | 対象外 | 対象外 |
| 3 | rsi_indicator | indicator | — | — | **approved** | 対象外 | 対象外 |
| 4 | rsi_indicator | indicator | — | — | rejected | 対象外 | 対象外 |
| 5 | rsi_indicator_21 | indicator | — | — | rejected | 対象外 | 対象外 |
| 6 | sma_cross_usdjpy | strategy | trades 193 / pf 1.474 / avg_r +0.188 | trades 54 / pf 0.904 / avg_r −0.037 / eval **true** | rejected「holdout pf 0.90 avg_r 負」 | 通る | **落ちる** |
| 7 | sma_cross_usdjpy_10_30 | strategy | trades 194 / pf 1.498 / avg_r +0.196 | trades 53 / pf 0.805 / avg_r −0.070 / eval true | rejected「holdout pf 0.80 で負け」 | 通る | **落ちる** |
| 8 | sma_cross_v2 | strategy | 同上 (ビット一致) | 同上 | rejected「#7 と同一」 | 通る (§A が捕まえる) | 落ちる |
| 9 | sma_cross_usdjpy_opt | strategy | 同上 (ビット一致) | 同上 | rejected「#7 と同一」 | 通る (§A が捕まえる) | 落ちる |
| 10 | ema_rsi_pullback | strategy | trades 39 / pf **0.713** / avg_r **−0.121** | trades 7 / pf 0.590 / eval **false** | rejected「不採算、holdout 評価不能」 | **落ちる** | (到達しない) |

**要点**: in_sample 段だけでは 5 件の strategy approval のうち #10 の 1 件しか止まらない。人間の却下理由 5 件中 4 件が holdout を名指ししている (#6〜#9 = in_sample で勝ち holdout で負ける過剰適合)。**両段を入れると 5/5 が止まる** — これが U1 で holdout を門に入れた裁定の根拠。

### 1.3 T0 の汚染は現在も生きている

`tmp/decontam-20260912.py` は strategy 系 5 行 (backlog #27/#38/#39/#51/#60) を `rejected_by_human` に除染したが、**indicator 系の #1 / #3 を取り残していた**:

- backlog #1 `last_result='rejected:example の丸写しで改善ゼロ (人間裁定)'`
- backlog #3 `last_result='rejected:example 丸写し系の period 変更のみで改善なし (人間裁定)'`

現在プロンプトに注入される窓 (直近 50 run = run id 6〜55) にこの 2 行が **7 回**現れる (run 13/18/21/22/24/27 → backlog #1、run 31 → backlog #3)。スクリプトは kind 非依存に更新済、ユーザー実行待ち (U5)。

---

## 2. ユーザー裁定 (2026-09-12)

| 項目 | 裁定 |
|---|---|
| U1 holdout をフロアに入れるか | **入れる**。ラベルは固定文言 `unprofitable` のみ — 数値も「どちら側で落ちたか」も書かない。全数値は人間向けレポートにだけ出す |
| U2 人間経路 | **submit は課す** (raise)、**bless は警告のみ** (続行し、警告を残す) |
| U3 `max_drawdown` / `kill_switch_latches` | **門にしない** (2026-09-12 時点の裁定)。ただしレポートには出す。**現行 (v1.6〜): `max_drawdown` は §10 で in-sample のみ門に改訂、latches は不変** |
| U4 baseline 再生の未実装 | 別起票 [baseline-replay-unimplemented] (指揮者が tickets に記載) |
| U5 除染 | スクリプト更新済、ユーザー実行待ち |
| C1 legacy submit | `afx plugin submit <name>` (live plugin 回廊) は **kind=strategy のとき拒否**し `materialize` → `submit --from _human` を案内する。indicator/signal は従来どおり。共有ゲートへの統合・回廊廃止は別起票 [legacy-submit-corridor-bypasses-gate] |
| C2 bless の warn | `floor_mode` を evaluator まで渡す。`warn` では in_sample 不合格でも **holdout・baseline 収集まで完走**し失敗ビットを保持する (短絡は `enforce` のときだけ) |
| R2-I1 失敗種別の識別 | **`run_kind_gate` は常に `GateOutcome` を返す** (ゲート判定で raise しない)。判別子 `verdict_kind` (v1.4: `floor_failed` は削除) を持たせ、enforce の raise と失敗行 outcome の決定は `_run_full_gate` が**型付き結果から**行う。**例外メッセージによる分類は禁止** |
| R2-I2 submit の activity | `submit_candidate(..., activity: ActivityLog | None = None)` を追加し、`_plugin_submit` から bless と同じ `ActivityLog(root / "logs" / "activity.log")` を渡す |
| R2-I3 report 失敗時の INDEX | **共通 settle 契約を維持** — report 失敗分岐でも `_settle_ledger_after_commit(outcome="report_failed")` を通し、archive / INDEX も `report_failed` になる (INDEX 書込みを抑止する専用 API は作らない) |

### 指揮者の既定選択 (裁定不要と判断、既定として明記)

**holdout の収益性判定は、その holdout 行が `evaluable=true` のときだけ行う。** `evaluable=false` (標本不足) は R8 どおり「悪いとは言わない」= **フロアは通す**。config `improve.gate.require_holdout_evaluable: bool = False` で厳格化できる。

理由: `backtest.holdout_months=3` の現設定では低頻度戦略が構造的に 30 件に届かない (#10 は holdout trades 7)。holdout の標本不足を理由に落とすかは個別判断 (人間が approval payload を見て決める) であり、決定論で一律に落とすと低頻度戦略の評価経路が閉じる。

---

## 3. T1 [profitability-floor] — 収益性フロアを決定論で課す

### 誰が何をどう扱うか

戦略採用ゲート (`plugin/strategy_gate.py::evaluate_strategy_adoption_gate`) が **2 段**で収益性を見る。呼び出し元は `floor_mode` (`"enforce"` / `"warn"`) を渡す。

1. **in_sample 段**: 標本十分 (`total_trades >= 30`) を確認した直後、in-sample の pair ごとの成績で判定。
   - `floor_mode="enforce"` で不合格 → **holdout を回さずに** 即 return (落ちる候補に holdout の計算コストを払わない / holdout 情報の発生自体を無くす)。
   - `floor_mode="warn"` で不合格 → **失敗ビットを保持したまま holdout・baseline 収集まで完走**する (codex C2)。人間が bless で強行する候補にも「通常 submit と同じ全ゲート証跡」(2026-08-16 設計書 `:388`, `:611`) を残すため。`floor_detail` が約束する「in_sample + holdout の全数値」もこれで初めて生成できる。
2. **holdout 段**: holdout を pair ごとに回し、**返り値を捕捉して** (現状は捨てている `strategy_gate.py:160-171`) 同じ判定関数にかける。不合格なら `floor_reason` を立てる。

判定は `_check_profitability_floor` 1 箇所が所有する — in_sample 段 / holdout 段 / T2 の親側 RPC handler の **3 呼び出し元すべて**がこの関数を呼ぶ (複製実装を作らない。spy pin = §8 F9)。

`ImproveLoop.commit` は `floor_reason` が立っていたら既存の `_finalize_gate_failed` 経路に倒す。backlog は `observation` に落ち `list_open` に残るので次 mission が再挑戦できる (R8 の精神)。

### 判定規則 (逐語)

pair ごとに判定し、**1 pair でも不合格なら候補全体を落とす** (§A の多 pair 方針と同じ)。pair 合計の pf は算術的に合成できない (`gross_profit`/`gross_loss` が `METRIC_KEYS` に無い) ため合計ではなく pair ごとに見る。

```
_check_profitability_floor(per_pair: dict[str, dict], *, settings, scope: str) -> tuple[str, str]
  # 戻り値 (label, detail)。label は "" (合格) か "unprofitable" (固定文言)。
  # detail は人間向けレポート専用の全数値文字列 — label と絶対に混ぜない。
  g = settings.improve.gate
  for pair, m in per_pair.items():
      # ① strict holdout 判定は zero-trade shortcut より前 (codex I9)
      if scope == "holdout" and g.require_holdout_evaluable and not m.get("evaluable"):
          FAIL(pair, "holdout_not_evaluable")      # trades == 0 でも FAIL
      # ② 成績が無い pair は通常モードでは判定から除外
      if m["trades"] == 0:
          continue
      # ③ 既定: holdout の標本不足は「悪いとは言わない」(R8)
      if scope == "holdout" and not m.get("evaluable"):
          continue
      pf = m["pf"]
      if pf is not None and pf < g.min_pf:          # pf is None (gross_loss==0) は PASS
          FAIL(pair, f"pf={pf}")
      avg_r = m["avg_r"]
      if g.require_positive_avg_r and avg_r is not None and avg_r <= 0.0:
          FAIL(pair, f"avg_r={avg_r}")
  return ("", "")
```

- `min_pf` 既定 1.0 → `pf == 1.0` は PASS (`<` であって `<=` ではない)
- `require_positive_avg_r` 既定 True → `avg_r == 0.0` は FAIL
- `avg_r is None` は `trades == 0` のときだけ起こる (`metrics.py:123`) ので ② で吸収される
- **v1.5 までの規則 (歴史記述)**: `max_drawdown` / `kill_switch_latches` は判定式に一切現れない (U3)。極端値でも verdict は不変 (pin F10)
  - **現行規則 (v1.6〜、§10)**: 上の疑似コードは pf / avg_r / `holdout_not_evaluable` の部分で、DD は in-sample のときだけ §10.3 の判定が同じループで追加される (既定 `improve.gate.max_drawdown` 0.20)。holdout では DD は判定に現れない。`kill_switch_latches` は判定式に現れない (不変)
- **順序 ①→②→③ が契約** — ① を ② の後に置くと `require_holdout_evaluable=True` でも `trades=0` の pair が通ってしまう (codex I9)

### ラベルと数値の分離 (遮断 8)

| 出力先 | 内容 | 根拠 |
|---|---|---|
| `improvement_backlog.last_result` | **`unprofitable` (固定文言のみ、完全一致)** | プロンプトに注入される唯一の面 (§4) |
| `activity` の `gate_failed` 行 | `mission=<id> reason=unprofitable` (固定) + 適用閾値 (`min_pf=… require_positive_avg_r=… require_holdout_evaluable=…`) | activity は親専有だが `last_result` と文言を揃え「どちらを見ても同じ」状態を作る |
| 親専有レポート (`data/improve_reports/improve-YYYY-MM-DD-<mission_id>.md`) | **全数値** — in_sample / holdout の pair ごとの `trades / pf / win_rate / avg_r / max_drawdown / total_pnl / kill_switch_latches / evaluable`、落ちた段、落ちた pair、適用閾値 | 人間だけが読む。worker の rw に無い |
| `backtest_runs` | 既存どおり metrics 全数 + `mission_outcome='unprofitable'` | 監査台帳 |
| approval payload (`profitability_floor`) | 適用した閾値 snapshot — **approval 行を作る全終端 (改善ループ成功 / 人間 submit 成功 / bless 警告)** で (§3 T1-g、codex I3 + R2-M2)。**approval を作らない失敗終端は payload を持たないのでレポート / activity で監査する** | 緩めて通した候補の監査 |

### T1-a 判定関数と verdict (`plugin/strategy_gate.py`)

- `StrategyGateVerdict` に `floor_reason: str = ""` / `floor_detail: str = ""` を追加。**`evaluable` は流用しない** — `evaluable=False` は R8 の「標本不足」意味論を 2 箇所が読んでいる (`plugin/approval.py:216-221`、`loops/improve_loop.py:2179`)。
- `_check_profitability_floor(per_pair, *, settings, scope)` を新設 (上記逐語)。
- `evaluate_strategy_adoption_gate(..., floor_mode: Literal["enforce","warn"] = "enforce")` を追加。in_sample 段不合格時の挙動は `floor_mode` で分かれる (上記)。`warn` で完走したときも `floor_reason` は立てたまま返す。
- holdout ループは `holdout_per_pair[pair] = run_holdout(...)` で返り値を捕捉する (`run_holdout_gate` は metrics dict を返す — `holdout.py:199-219`)。

### T1-b 改善ループ側の再ルート (`loops/improve_loop.py`)

- `if not strategy_verdict.evaluable:` (`:2179`) の**直後**に `if strategy_verdict.floor_reason:` 分岐 → `_finalize_gate_failed(..., reason="unprofitable", mission_outcome="unprofitable", report_detail=strategy_verdict.floor_detail, gate_rows=tuple(gate_rows))` → `return`。改善ループは常に `floor_mode="enforce"`。
- **`_finalize_gate_failed` の outcome は branch-local に 1 つ決めて gate 行・ledger 行・settle の 3 箇所すべてへ同じ値を渡す** (codex I6)。現行は gate 行が引数、ledger が `"gate_failed"` 固定、settle が `"gate_failed"` 固定で分裂しうる。
  - 正常分岐: `outcome = mission_outcome` (既定 `"gate_failed"`、フロア経路は `"unprofitable"`) を `_persist_ledger_in_tx` / `_persist_gate_rows` / `_settle_ledger_after_commit` の 3 箇所へ。
  - **report 作成失敗分岐: `outcome = "report_failed"` に固定し、引数 `mission_outcome` で上書きしない** (既存の `report_failed` 意味を壊さない)。
  - 結果として archive INDEX の `status` もフロア経路では `unprofitable` になる (`_settle_ledger_after_commit` → `_write_archive_index_safe`)。§7 の終端表に反映済。

### T1-c 人間 corridor の `floor_mode` (U2)

- `approval.run_kind_gate(..., floor_mode=…)` は `floor_mode` を `evaluate_strategy_adoption_gate` へ転送し、**結果を `GateOutcome` として返す — ここでは raise しない**。`raise` するか続行するかは `_run_full_gate` が **`verdict_kind` と `floor_mode` から**決める (対応表は T1-d に一本化。この節に enforce/warn の挙動を重複記述しない)。
- `switch.submit_candidate` → `floor_mode="enforce"`。`switch.bless_candidate` → `floor_mode="warn"`。
- **`run_kind_gate` に `floor_mode` を渡す理由**は「判定を止めるため」ではなく「`warn` のとき evaluator に holdout・baseline まで完走させるため」(C2)。判定結果そのものは `floor_mode` に関わらず同じ `verdict_kind` になる。

### T1-d 名前付き結果型 (codex I4/I5 — tuple 拡張をやめる)

```
@dataclass(frozen=True)
class GateOutcome:                     # plugin/approval.py
    metrics: dict
    evaluable: bool
    verdict_kind: Literal["ok", "insufficient_trades", "floor"] = "ok"
    # (v1.4: `floor_failed` は冗長のため削除 — 判別子は `verdict_kind` のみ)
    floor_warning: str = ""            # "" | "unprofitable"
    floor_detail: str = ""             # 人間向け全数値 (payload / 表示用)
    insufficient_trades_reason: str = ""   # "insufficient_trades:<n>" (標本不足のときだけ)
    gate_rows: tuple[dict, ...] = ()   # T1-f の非コミット sink に積まれた行
```

**`run_kind_gate` はゲート判定で例外を投げない** (codex R2-I1)。標本不足も収益性不合格も `verdict_kind` に載せて**常に `GateOutcome` を返す**。**例外メッセージ (`"unprofitable"` の部分一致など) による分類は禁止** — 文言変更や候補名への混入で誤分類しうる。

| `verdict_kind` | 意味 | `_run_full_gate` の挙動 (enforce) | 失敗行の `mission_outcome` |
|---|---|---|---|
| `"ok"` | 全ゲート通過 (indicator / signal は常にこれ) | 続行 | (`approval`) |
| `"insufficient_trades"` | in-sample 合計 < 30 (現行 `approval.py:216-221` が `ValueError` にしていたもの) | `ValueError(f"plugin {name!r}: strategy not evaluable ({insufficient_trades_reason})")` | **`gate_failed`** |
| `"floor"` | 収益性フロア不合格 | `ValueError(f"plugin {name!r}: unprofitable (min_pf=… …)")` | **`unprofitable`** |

- `floor_mode="warn"` のときは `verdict_kind == "floor"` でも `_run_full_gate` は raise せず続行する (`floor_warning` / `floor_detail` を payload へ運ぶ)。`"insufficient_trades"` は `floor_mode` に関わらず raise する (U2 が緩和したのはフロアだけ)。
- `run_kind_gate` を**抜けてくる例外** (想定外。`holdout.NoHistoryError` / 履歴空の `ValueError` 等) は判別子を持たないので `_run_full_gate` が **`gate_failed`** として扱う。
- `approval.run_kind_gate` の戻り値を `tuple[dict, bool]` → `GateOutcome` に変更 (src の呼び出し元は `switch.py:779` のみ)。
- `switch._run_full_gate` の戻り値を `(meta, after_content, after_artifact, outcome: GateOutcome)` の **4 要素**に固定 (呼び出し元 `switch.py:818`, `:1470`)。spec/plan で要素数が食い違っていた v1 の矛盾を解消。
- **`bless_candidate` の戻り値は `int` (approval_id) のまま変えない** — 直接呼び出しが tests に 11 箇所あり (`tests/plugin/test_switch_paths.py:202,225,241,264,300,346,1201,1395,1456` / `test_approval_payload_common_contract.py:154` / `test_materialize_retire.py:212`)、v1 の「呼び出し元 1 箇所」は誤りだった (codex I5)。警告は次の 2 経路で取る:
  - 新設キーワード `on_floor_warning: Callable[[str, str], None] | None = None` (label, detail) を呼び出し元が渡す。`cli.py::_plugin_bless` はここで stderr 出力 + `activity.write` を行う。
  - approval payload の `floor_warning` / `floor_detail` / `profitability_floor` (永続側)。
- `commands.py::_approval_detail` に `floor_warning` / `floor_detail` / `profitability_floor` の表示を追加 (CLI が「詳細は `afx> approval <id>`」と案内するのに読めない状態を作らない)。

### T1-e legacy submit 回廊は strategy を拒否する (codex C1)

- `approval.submit_plugin` の冒頭 (検証ゲートより前) で `meta.kind == "strategy"` なら
  `ValueError("plugin <name>: この経路は strategy を受け付けません — `afx plugin materialize <name>` で候補を書き出し `afx plugin submit <name> --from _human` を使ってください (固定 holdout を含む共有ゲートを通すため)")` を送出する。**API 側で fail closed** にする (CLI だけの案内にしない)。
- `backtest/cli.py::_plugin_submit` は既存の `except ValueError` でそのまま stderr に出る (追加変更は不要だが、メッセージが案内文になっていることを pin する)。
- indicator / signal は従来どおりこの回廊を通れる。
- `_validate_kind` / `_validate_strategy` の strategy 分岐は**到達不能になる**。削除はせず (tests が直接使っている)、docstring に「`submit_plugin` は strategy を拒否するため、この分岐は直接呼び出し (テスト) からのみ到達する」と明記する。**この分岐にフロアは足さない** (v1 の「legacy corridor に in_sample 段だけ足す」は C1 により撤回)。
- 回廊そのものの統合・廃止は別起票 [legacy-submit-corridor-bypasses-gate]。

### T1-f 人間 corridor の gate 行に明示 outcome を付ける (codex I2)

- `switch._run_full_gate` が `rows: list[dict] = []` を作り `run_kind_gate(..., record_fn=rows.append)` → `evaluate_strategy_adoption_gate(record_fn=...)` へ渡す (この引数は既存。`record_fn` があれば `holdout._run_scope` は即時 commit しない)。捕捉した行は `GateOutcome.gate_rows` で返す。
- **保存の所有者は呼び出し元**:
  - `submit_candidate` / `bless_candidate`: `approvals_store.create` と**同じ tx** で `save_harness_run(conn, commit=False, mission_id=None, mission_outcome=<下表>, **row)` を全行ぶん書く。
  - `_run_full_gate` が `ValueError` を投げる分岐 (enforce のフロア不合格・標本不足・pytest 不合格等): **捕捉済みの行を短い専用 tx で保存してから raise する** (証跡を失わない)。
- 人間 corridor の `mission_outcome` 規約:

| 経路 | 結果 | `mission_outcome` |
|---|---|---|
| `submit_candidate` 成功 | approval 作成 | `approval` |
| `submit_candidate` フロア不合格 (enforce) | `ValueError` | `unprofitable` |
| `submit_candidate` 標本不足 (`verdict_kind="insufficient_trades"`) | `ValueError` | `gate_failed` |
| `submit_candidate` その他ゲート不合格 (pytest / hash / snapshot / 想定外例外) | `ValueError` | `gate_failed` |
| `bless_candidate` 成功 (フロア合格) | approval 作成 | `approval` |
| `bless_candidate` 成功 (フロア不合格・警告) | approval 作成 | **`approval`** (承認経路としては成立している。フロア不合格の事実は payload の `floor_warning` が持つ) |

- **`mission_outcome=None` の行を新たに作らない**ことが本項の目的 (`latest_in_sample_metrics` の live 絞りへの混入を止める)。既存の NULL 行 (実 DB に 4 件) の遡及修正は本束の範囲外。

### T1-g 閾値 snapshot の保存 (codex I3)

成功側にも監査を残す。`settings_snapshot_hash` は変更しない (既存の全 hash が変わり §A の dedup 母集団と既存行の identity に波及する)。

- 次の 3 つの payload 生成箇所に `"profitability_floor": {"min_pf": …, "require_positive_avg_r": …, "require_holdout_evaluable": …}` を追加する:
  - `loops/improve_loop.py::_build_approval_payload`
  - `plugin/switch.py::submit_candidate` の payload
  - `plugin/switch.py::bless_candidate` の payload
- approval を作らない終端の監査:
  - 改善ループのフロア不合格 → 親専有レポート本文 + `activity` の `gate_failed` 行 (T1-b)
  - 人間 submit のフロア不合格 → `activity.write(Category.APPROVAL, "submit_floor_rejected", f"name=… unprofitable min_pf=… require_positive_avg_r=… require_holdout_evaluable=…")` と `ValueError` メッセージ。**そのために `submit_candidate(..., activity: "ActivityLog | None" = None)` を追加し、`backtest/cli.py::_plugin_submit` から bless と同じ `ActivityLog(root / "logs" / "activity.log")` を渡す** (codex R2-I2 — 現行 `submit_candidate` は `activity` を受け取らず CLI も渡していないので、配線しないと実装者が `plugins_root.parent/logs` を関数内で推測する新しい所有規則を作ってしまう)
- `commands.py::_approval_detail` がこの snapshot を表示する (T1-d)。

### 終端名

- **backlog status** = `observation`。`rejected` にしない (§4.3 逐語「`rejected` は人間が `backlog reject` で閉じる用」)。
- **`backtest_runs.mission_outcome`** = 新設 `unprofitable`。CHECK 制約が無いので migration 不要 (`store/db.py:299,406`)。
  - in_sample 段で落ちたとき (enforce) は in_sample 行のみ。holdout 段 / warn 完走のときは in_sample 行 + holdout_gate 行の全 pair。
  - **§A の dedup 母集団には影響しない** — `find_matching_approved_metrics` は `mission_outcome='approval'` かつ `content_hash IN (approval_requests に載った hash)` で絞る (`backtest_runs.py:260-278`)。
  - `latest_in_sample_metrics` の live 絞り (`IS NULL OR 'approval'`) も自動的に除外する。
- **`improvement_runs.result`** = `report`。**`improvement_backlog.last_result`** = `unprofitable` (固定)。
- **archive INDEX の `status`** = `unprofitable` (T1-b の branch-local outcome 統一の帰結)。

---

## 4. 遮断 8 との整合 (明示的な例外としての記録)

ラベル (= `last_result` = プロンプト注入面) に入れてよいもの / ダメなもの:

| 情報 | 注入可否 | 理由 |
|---|---|---|
| in_sample の `trades` / `pf` / `avg_r` / `max_drawdown` | 可 | `run_backtest` RPC が同じ期間の同じ指標を agent に返している (2026-08-16 設計書 `:318`)。agent が既に知っている値 |
| holdout の数値 | **不可** | 遮断 8 逐語「holdout の指標」 |
| holdout の `evaluable` / pair ごとの合否 / 段名 (in_sample / holdout) | **不可** | 遮断 8 逐語「閾値別合否」 |
| 「holdout」という語そのもの | **不可** | 合否ビットの運搬になる |
| baseline との差分 | **不可** | 遮断 8 逐語「baseline 差分」 |
| **集約フロア不合格の 1 bit** (固定文言 `unprofitable`) | **可 — 明示的な例外** | 下記 |

### ただし書き (T0 で 2026-08-16 設計書 §7.1-1 ⑧ に逐語で追記する文案)

> **⑧ の明示的な例外: 集約フロア不合格の 1 bit** (2026-09-12 ユーザー裁定 U1、codex 設計レビュー I1)。
> 収益性フロア ([profitability-floor]) の不合格は、改善ループの backlog 履歴表に固定文言 `unprofitable` の **1 bit** として現れる。in_sample 段で落ちたか holdout 段で落ちたか、どの pair が落ちたか、数値はいずれも現れない。
> **これは「人間 reject と同程度の露出」ではない。** 人間 reject は一度限りの人間の判断だが、`unprofitable` は **agent が繰り返し問い合わせられる決定論的な二値 oracle** である。とくに RPC の `submission_blocked` が出なかった (= in_sample 段は通った) 候補の最終結果が `unprofitable` であれば、agent は「親専有段で落ちた」と推論できる。複数 mission にまたがる候補比較・二分探索によって、holdout に対する反復最適化は**原理的に可能**である。
> 本プロジェクトはこのリスクを**受容する脅威モデルとして明示的に記録する** — 収益性の決定論化 (人間の却下理由 5 件中 4 件が holdout 判定だった実測、[profitability-floor] 設計書 §1.2) の価値が、1 bit × mission 回数ぶんの反復可能性を上回るという裁定である。再評価のトリガ: 改善ループが `unprofitable` を受けた後に holdout 寄りへ体系的に収束する挙動が観測されたら、この例外を見直す。
> **テストが保証する範囲はこの 1 bit の外側に限る** — すなわち「holdout の数値・段名・pair 名・baseline 差分が注入面・RPC 返却・Mission 出力のどこにも現れない」。1 bit そのものの不在は検査しない (許容したため)。

### T2 のヒントは in_sample のみ

`run_backtest` RPC の返却に足す `submission_blocked` ヒントは **in_sample 判定の結果のみ**から導く。holdout は agent に見せない。holdout 段で落ちる候補は agent から見ると「ヒントは出なかったのに `unprofitable` になった」形になる — これが上記の受容した 1 bit の実体である。

---

## 5. T0 [reject-reason-leak] — 人間の却下理由をプロンプトから切る

### 誰が何をどう扱うか

人間が `afx> reject <id> <理由>` を打つと理由は 2 箇所に行く: ①`approval_requests.reason` (人間専用の台帳) ②`improvement_backlog.last_result` に `rejected:<理由>` として (`store/backlog.py:111` の `_OUTCOME_TABLE['rejected']`)。②は次 mission の改善履歴表に逐語で載る。**②を固定文言 `rejected_by_human` にし、理由は①だけに残す**。そのうえで①を人間が読める導線を足す。

### 変更点表

| file | 変更 |
|---|---|
| `store/backlog.py:111` | `"rejected": ("observation", "rejected:{reason}")` → `("observation", "rejected_by_human")` (`"{reason}" in template` 分岐は固定文言側を通るのでコード変更不要) |
| `store/approvals.py:103-107` | `rejected` 経路が `reason` を backlog へ渡さないことを docstring に明記。引数は残す (`approval_requests.reason` 列へは従来どおり書く) |
| `commands.py::_approval_detail` (`:345-366`) | `reason` / `decided_by` / `decided_at` の行を足す。**必須** — これが無いと人間の却下理由が write-only になる。T1-d の `floor_warning` / `floor_detail` / `profitability_floor` 表示と同じ task で入れる |
| `docs/.../2026-08-16-phase2-10-improve-loop-design.md` §7.1-1 ⑧ | §4 の「ただし書き」ブロックを**逐語で**追記 (要約・言い換え禁止) |
| `tests/store/test_backlog.py:197` | `("selected","rejected","observation","rejected:")` → `"rejected_by_human"` の完全一致 |
| `tests/store/test_approvals.py:150` | `== "rejected:not useful"` → `== "rejected_by_human"` |
| `tests/loops/test_floor_leak_guard.py` (**新規**) | 純粋な store / rendering の漏洩 pin をここに置く (codex I12 — `tests/integration/test_improve_forbidden_regression.py` はファイル全体に Landlock skip marker (`:62-63`) があり、Landlock 非対応環境で全 skip される) |
| 運用 (コミット対象外、U5) | `UPDATE improvement_backlog SET last_result='rejected_by_human' WHERE last_result LIKE 'rejected:%'` (backlog #1/#3)。実行はユーザー |

### 影響範囲の全数 (grep 済み)

`last_result` を**読む**箇所: `improve_context.py:75,102,109` / `improve_loop.py:629-633` (履歴表の描画) / `tests/` の assert 群 (上記 2 本のみが `rejected:` に依存)。`_backlog_table` (`improve_loop.py:636-644`) は `last_result` を描かない → **注入面は改善履歴表だけ**。approve 側は `approved:<id>` (`approvals.py:105-106`)、`expired`/`invalidated` は定数 → 漏洩なし。`mission_failed:` / `gate_failed:` / `observation:` は機械由来なので T0 の対象外。

---

## 6. T2 / T3

### T2 [floor-feedback-in-prompt] — 予算内で作り直させる

agent は `run_backtest` の返却で in_sample の `pf`/`avg_r` を見ている (遮断 7 = 集計指標のみなので正当)。今は「pf 0.713 で不採算」と自分で書きながら提出している (#10 の summary 逐語)。**提出が無駄になることを返却とプロンプトの両方で告げる**。

#### 導出は親側 1 箇所 (codex I7)

`submission_blocked` は **`loops/improve_loop.py::_build_rpc_handlers` の `run_backtest_handler`** で 1 回だけ導出する。この handler は `self._settings` を持っているので**新しい settings 配線を作らない** — `build_improve_rpc_tooldefs` / `build_rpc_handlers` / `tools/mission_registry.py` のシグネチャは変えない。

- 全ての経路 (in-process / RPC 越しの子 registry) は最終的にこの親 handler に到達する (`improve_loop.py:384`, `:1054`, `mission_registry.py:107` はいずれも `rpc_handlers["run_backtest"]` を受け取るだけ) ので、**導出点は一意**。
- handler の戻り値 dict (agent へ返る面) に足す。**`save_kwargs` には足さない** → 台帳の記録内容は痩せも太りもしない (`improve_rpc_tools.py:150-154` が `save_kwargs` を読む契約)。
- 子側 tooldef の `_strip_forbidden` は禁止キーのみを剥がすので `submission_blocked` はそのまま通る。`remaining_budget` の付与位置 (`:157-162`) とは独立。

形: `{"reason": "unprofitable", "detail": "pf=0.713 avg_r=-0.121 — この成績では親ゲートが承認申請を出さず observation になります"}`。**in_sample 段を通るときはキーを足さない**。判定は `_check_profitability_floor(..., scope="in_sample")` を呼ぶ (閾値のハードコード禁止)。**holdout を一切参照しない**。

#### 文言は settings からレンダする (codex I8)

閾値が config 可変なのに文言が `pf >= 1.0` 固定だと、設定変更後にゲートと説明が食い違う。

- `loops/prompts/improve_mission.md` 規律 4 の末尾に **単一 placeholder** `{profitability_floor_rule}` を置き、`_render_improve_mission_prompt` (`improve_loop.py:653-686`) の `render_map` で settings から文を組む (`.format()` は条件分岐できないため、文そのものを組み立てて渡す)。
  - `require_positive_avg_r=True`: 「`pf < {min_pf}` または `avg_r <= 0` の候補は提出しても承認申請になりません…」
  - `require_positive_avg_r=False`: **avg_r の条件を文から省く**
- 同様に `submission_blocked.detail` と `cli.py::_plugin_bless` の警告文、`ValueError` メッセージも settings の値を埋める。
- 規律 3 の「`observation` として理由を残し」に「**試したパラメータと得られた pf / avg_r を具体的に**」を足す。
- 規律 4 に「**`config.yaml` の `pairs` に宣言した全 pair を最低 1 回 `run_backtest` で確認してから提出する**」を足す (codex I10 — 候補は 1 pair の不合格で全体が落ちるため)。

### T3 [floor-config]

| file | 変更 |
|---|---|
| `config.py::ImproveGateSettings` (`:226-228`) | `min_pf: float = Field(ge=0.0, default=1.0)` / `require_positive_avg_r: bool = True` / `require_holdout_evaluable: bool = False` |
| `config.py::Settings` | **新規 `model_validator`**: `improve.tool_budget.max_backtests_per_candidate >= len(pairs)` (codex I10)。plugin の `pairs` は `_validate_strategy` / loader で `Settings.pairs` の部分集合に制限されるので、この上限で全 pair 1 回ぶんの枠を保証できる。違反は `ValueError` (fail closed) |
| `config/settings.yaml.example` (`:95-97`) | `improve.gate` に 3 キーをコメント付きで追記 |
| `config/settings.yaml` (gitignore) | **同期** (CLAUDE.md 規約) |

**予算の根拠 (codex I11 による訂正)**: v1 は「パラメータを変えた候補を別名で書けば枠は別」を予算整合の根拠にしていたが、これは候補ごと上限を名前変更で回避する運用を公式に勧める形になる。**撤回する**。基本は「**1 候補を最大 6 回まで編集して再試験する**」(同名候補の編集で `max_backtests_per_candidate` の枠内に収める)。複数候補を作る場合は mission 全体の `max_tool_calls=300` が律速であり、別途の評価は本束の範囲外。

**「drawdown kill switch は config で無効化不可」との関係**: 別物。あの制約は実運用の資金保護 (`risk.*`) が対象。収益性フロアは採用判断の品質基準であり、緩めても実資金は動かない (承認は人間の最終判断を通る)。

**監査**: `settings_snapshot_hash` は `improve.gate` を含まない (§0) ので、閾値 snapshot を approval payload に入れる (T1-g) + 不合格側はレポート / activity (T1-b)。`settings_snapshot_hash` 自体は変えない。

---

## 7. 全終端表 (フロア不合格候補の扱い)

| 終端 | 経路 | backlog status / `last_result` | `backtest_runs.mission_outcome` | ledger (`analysis_runs`) | archive / INDEX | report | `improvement_runs.result` | approval 行 | staging |
|---|---|---|---|---|---|---|---|---|---|
| 標本不足 (既存) | `_finalize_gate_failed` | observation / `insufficient_trades:<n>` | `gate_failed` | `gate_failed` | `gate_failed` | 書く | `report` | なし | 削除 |
| 形式/pytest 不合格 (既存) | `_finalize_gate_failed` | observation / `gate_failed:<reason>` | `gate_failed` | `gate_failed` | `gate_failed` | 書く | `report` | なし | 削除 |
| **フロア不合格 (in_sample 段、新)** | `_finalize_gate_failed(mission_outcome='unprofitable')` | observation / **`unprofitable`** | **`unprofitable`** (in_sample 行のみ) | **`unprofitable`** | **`unprofitable`** | 書く (全数値 + 適用閾値) | `report` | なし | 削除 |
| **フロア不合格 (holdout 段、新)** | 同上 | observation / **`unprofitable`** (in_sample 段と文字列同一) | **`unprofitable`** (in_sample + holdout_gate 行の全 pair) | **`unprofitable`** | **`unprofitable`** | 書く (in_sample + holdout 全数値) | `report` | なし | 削除 |
| **フロア不合格 + report 作成失敗 (新)** | `_finalize_gate_failed` の OSError 分岐 | observation / `report_failed:<safe_reason>` | **`report_failed`** (`unprofitable` で上書きしない) | **`report_failed`** | **`report_failed`** (共通 settle 契約を維持 — INDEX 書込みを抑止する専用 API は作らない。codex R2-I3) | 失敗 | `NULL` | なし | 削除 |
| 成績一致降格 (§A、既存) | `_finalize_success` → rollback → `_finalize_report_or_observation` | observation / `observation:duplicate_metrics_of:<hash> pair=<P>` | `observation` | `observation` | `observation` | 書かない | `NULL` | なし | 削除 |
| observation (agent 申告、既存) | `_finalize_report_or_observation` | observation / `observation:<safe_text(reason)>` | `observation` | `observation` | `observation` | 書かない | `NULL` | なし | 削除 |
| 承認申請 (既存) | `_finalize_success` | selected / `approval_pending:<id>` | `approval` | `approval` | `approval` (§B) | 任意 | `approval` | 作る (+`profitability_floor`) | **残す** |
| 却下 (人間、T0 後) | `apply_decision('rejected')` | observation / **`rejected_by_human`** | (変化なし) | — | — | — | — | 決定済み | — |
| 人間 submit 成功 (`--from _human`) | `submit_candidate` | — | **`approval`** (T1-f) | — | — | — | — | 作る (+`profitability_floor`) | — |
| 人間 submit フロア不合格 | `submit_candidate` → `ValueError` | — | **`unprofitable`** (短い専用 tx、T1-f) | — | — | — | — | 作らない | — |
| 人間 submit その他ゲート不合格 | 同上 | — | **`gate_failed`** | — | — | — | — | 作らない | — |
| **bless 警告 (U2、新)** | `bless_candidate(floor_mode="warn")` — holdout・baseline 完走 | — | **`approval`** (T1-f) | — | — | — | — | 作る (+`floor_warning` / `floor_detail` / `profitability_floor`) | — |
| legacy submit (strategy) | `approval.submit_plugin` → `ValueError` | — | **行を作らない** (ゲート前に拒否) | — | — | — | — | 作らない | — |

フロア不合格を `_finalize_gate_failed` に載せる副産物で**親専有レポートが書かれる** (§A の降格では書かれない) — 人間が「何がどれだけ負けたか」を後から読める。採用理由のひとつ。

---

## 8. 検証 (変異下限)

節番号は外部参照用に固定する。リストは下限。

### F1 in_sample 段の閾値境界

- F1-1 `pf == min_pf` → PASS。変異: `<=` → killer
- F1-2 `pf == min_pf - ε` → FAIL、`last_result == "unprofitable"` (完全一致)
- F1-3 `avg_r == 0.0` → FAIL。`avg_r == +ε` → PASS。変異: `< 0` → killer
- F1-4 **`trades > 0` かつ `pf is None` (gross_loss==0) かつ `avg_r > 0` → PASS** (codex M1: `trades=0` の fixture では zero-trade shortcut に吸収されて PF 分岐に到達しない)。変異: `None` を fail 側に倒す / 素で比較して `TypeError` → killer
- F1-5 `pf == 1.5` かつ `avg_r == -0.01` → FAIL (2 門が独立)。変異: `avg_r` 検査を落とす → killer
- F1-6 `require_positive_avg_r=False` で F1-5 が PASS
- F1-7 `min_pf=0.0` で pf 0.5 が PASS
- F1-8 **`floor_mode="enforce"` で in_sample 段が落ちたとき `run_holdout_gate` が呼ばれない** (spy で 0 回)。変異: 判定を holdout の後に置く → killer

### F2 holdout 段

- F2-1 in_sample 通過 (pf 1.5 / avg_r +0.2) かつ holdout `evaluable=true` / pf 0.8 → FAIL。変異: holdout の返り値を捨てる (現行実装) → killer
- F2-2 holdout pf `== min_pf` → PASS、`min_pf - ε` → FAIL
- F2-3 holdout `evaluable=true` / pf 1.2 / avg_r −0.01 → FAIL
- F2-4 **holdout `evaluable=false` (trades 7) / pf 0.59 → PASS** (既定、R8)。変異: `evaluable` を見ずに判定 → killer
- F2-5 `require_holdout_evaluable=True` で **(a) trades=7 / evaluable=false → FAIL**、**(b) trades=0 / evaluable=false → FAIL** (codex I9: strict 判定が zero-trade shortcut より前にある pin)。変異: 順序を入れ替える → (b) が killer
- F2-6 holdout 段で落ちたとき in_sample 行**と** holdout_gate 行の全 pair が `mission_outcome='unprofitable'`
- F2-7 **holdout 段で落ちた候補の `last_result` が in_sample 段で落ちた候補と文字列として同一** (`"unprofitable"`)。変異: ラベルに段名/pair/数値を足す → killer

### F3 多 pair

- F3-1 2 pair で 1 pair のみ FAIL → 候補全体が FAIL。変異: 「全 pair FAIL で落とす」→ killer
- F3-2 通常モードで `trades=0` の pair (`avg_r=None`) は判定から除外され他 pair で決まる。変異: `None` を FAIL 扱い → killer

### F4 改善ループ側の再ルート後の副作用

- F4-1 `backtest_runs` の行が `mission_outcome='unprofitable'`。変異: `None` → live 絞りを通る killer
- F4-2 `improvement_backlog.status == 'observation'` かつ `list_open` に含まれる。変異: `rejected` → R8 killer
- F4-3 `approval_requests` に行が増えない
- F4-4 親専有レポートが公開され (`report_state='published'`)、本文に in_sample / holdout の全数値 + 適用閾値 + 落ちた段 + 落ちた pair + `max_drawdown` + `kill_switch_latches` が含まれる
- F4-5 archive INDEX に `status='unprofitable'` の行が残る
- F4-6 staging が削除される / ledger が `PERSISTED`
- F4-7 既存 3 呼び出し (`insufficient_trades` / `gate_failed:*` / `backtest_data_unavailable`) の gate 行・ledger 行・INDEX が `gate_failed` のまま (引数化の後方互換)
- F4-8 §A の dedup 母集団に `unprofitable` 行が入らない
- F4-9 **gate 行・ledger 行・settle が同じ branch-local outcome** (`unprofitable`) を受ける (codex I6)。変異: ledger だけ `gate_failed` 固定に戻す → killer
- F4-10 **report 作成失敗分岐では gate 行・ledger 行・settle・archive INDEX の 4 点すべてが `report_failed`** (`unprofitable` で上書きしない)。ledger 状態は `PERSISTED` になる。変異: 引数をそのまま使う / INDEX だけ `unprofitable` になる → killer (codex R2-I3)

### F5 遮断 8 (**配置**: 純粋な store / rendering pin は新規 `tests/loops/test_floor_leak_guard.py` = Landlock skip なし。実プロセス依存のものだけ `test_improve_forbidden_regression.py`、codex I12)

- F5-1 人間が `reject <id> "holdout pf 0.80 で負け"` した後、レンダ済みプロンプト全文に `"holdout"` / `"0.80"` / 理由文字列が現れない。変異: `_OUTCOME_TABLE['rejected']` を戻す → killer
- F5-2 holdout 段でフロア不合格になった後、**(a) `last_result == "unprofitable"` の完全一致**、**(b) レンダ済みプロンプト全文に holdout 固有の canary 数値 (fixture が holdout にだけ入れた値) が無い**、**(c) `floor_detail` の文字列が無い**、**(d) 段名断片 (`"holdout"` / `"in_sample"` / `"scope="`) が無い** (codex I13)。**生の pair symbol の全文不在は要求しない** — prompt は直近成績・risk settings で pair 名を正当に含むため false positive になる
- F5-3 `approval_requests.reason` には理由が残っている
- F5-4 `afx> approval <id>` の出力に `reason=` / `floor_warning` / `floor_detail` / `profitability_floor` が現れる
- F5-5 `_history_table` が `last_result` をレンダする唯一の経路 (`_backlog_table` がレンダし始めたら落ちる assert)
- F5-6 `submission_blocked` に `"holdout"` の語・holdout の数値・期間端点が無い (既存 pin `"period_start" not in json.dumps(out)` を拡張)

### F6 人間 corridor (U2 / C1 / I2)

- F6-1 `switch.submit_candidate` (`--from _human`) でフロア不合格の strategy が `ValueError` になり approval 行が作られない。変異: フロアを改善ループ側だけに置く → killer
- F6-2 `switch.bless_candidate` でフロア不合格の strategy が **approval 行を作る** (警告のみ)。payload に `floor_warning == "unprofitable"` / `floor_detail` (全数値) / `profitability_floor` が入る。変異: bless も raise → killer
- F6-3 **`bless_candidate` の warn 経路で `run_holdout_gate` が pair ごとに呼ばれ、payload の `baseline` が非 null** (codex C2)。変異: warn でも in_sample 段で短絡する → killer
- F6-4 `on_floor_warning` コールバックが呼ばれる (`None` でも例外を出さない)。`cli.py::_plugin_bless` の終了コードは 0、stderr に警告
- F6-5 **`approval.submit_plugin(kind=strategy)` が `ValueError` で拒否され、メッセージに `materialize` と `--from _human` の案内が含まれる** (codex C1)。変異: 拒否を外す → killer
- F6-6 **in_sample 合格・holdout 不合格の候補が legacy submit 経路で approval にならない** (C1 の本質 pin。現行実装ではこれが approval になってしまう)
- F6-7 `bless_candidate` の既存 11 テスト呼び出しが **戻り値 `int` のまま**通る (後方互換 pin)
- F6-8 人間 corridor の gate 行に `mission_outcome` が付く: submit 成功 = `approval` / **submit フロア不合格 = `unprofitable`** / **submit 標本不足 = `gate_failed`** / submit その他不合格 = `gate_failed` / bless (警告含む) = `approval`。**`None` の行を新たに作らない** (codex I2)。**フロア不合格と標本不足の両方を別々に pin する** (codex R2-I1)。変異: `record_fn` を渡さず即時 commit に戻す / 判別子を無視して両方 `gate_failed` にする → killer
- F6-9 **`run_kind_gate` がゲート判定で例外を投げない** — 標本不足でも収益性不合格でも `GateOutcome` が返り、`verdict_kind` が `"insufficient_trades"` / `"floor"` になる (codex R2-I1)。変異: `run_kind_gate` 内で raise に戻す → killer
- F6-10 **`_run_full_gate` の分類が判別子由来**である pin — `floor_detail` / 候補名に `"unprofitable"` の文字列を混ぜた fixture でも標本不足は `gate_failed` に保存される。変異: 例外メッセージの部分一致で分類する → killer
- F6-11(a) **`submit_candidate` 層**: 人間 submit のフロア不合格で `activity` に `submit_floor_rejected` が書かれ、適用閾値 3 値 (`min_pf` / `require_positive_avg_r` / `require_holdout_evaluable`) が行に含まれる。`activity=None` でも例外を出さない (後方互換)。変異: `activity` 引数を外す / 閾値を書かない → killer
- F6-11(b) **CLI 層 (codex R3-I2)**: `switch.submit_candidate` を spy に差し替えて `afx plugin submit <name> --from _human` (= `backtest/cli.py::_plugin_submit` の `--from _human` 分岐) を実行し、**`activity=` に `ActivityLog(root / "logs" / "activity.log")` 相当 (= その `path` を持つ `ActivityLog` インスタンス) が渡っている**ことを直接 assert する。**(a) だけでは CLI 配線の欠落を検出できない** — (a) は `submit_candidate(activity=fake)` の直接呼び出しで満たせてしまう。変異: CLI が `activity=` を渡さない (現行実装のまま) → killer
- F6-12 **想定外例外の保存経路 (codex R3-M1)**: `run_kind_gate` の fake が `record_fn` に **1 行積んだ後**に contract 外の例外 (`RuntimeError` / `holdout.NoHistoryError` 等) を投げるケースで、**その行が `mission_outcome='gate_failed'` で保存され**、元例外が呼び出し元へ伝播する (または `_run_full_gate` の既存規約どおり `ValueError` へ正規化される — どちらでもよいが `unprofitable` には**ならない**)。変異: 想定外例外の経路で行を捨てる / `unprofitable` で保存する → killer

### F7 T2

- F7-1 `submission_blocked` が in_sample 段不合格のときだけ現れる。変異: 常に付ける / 付けない → killer
- F7-2 判定が T1 と同じ関数を呼ぶ (`min_pf` を変えたテストが効く)
- F7-3 **プロンプト文言が settings からレンダされる** (codex I8): `min_pf=1.5` で文言に `1.5` が出る / `require_positive_avg_r=False` で avg_r 条件が**文言から消える**。変異: 文言をハードコードに戻す → killer
- F7-4 `submission_blocked` が親 handler で 1 回だけ導出される (子 tooldef 層で再導出していない)。`save_kwargs` / ledger 行に `submission_blocked` が混ざらない
- F7-5 プロンプト規律に「宣言全 pair を最低 1 回 backtest」が含まれる

### F8 T3 config

- F8-1 既定値 pin: `min_pf == 1.0` / `require_positive_avg_r is True` / `require_holdout_evaluable is False`
- F8-2 `settings.yaml.example` と `ImproveGateSettings` のキー集合一致
- F8-3 `min_pf` に負値 → `ValidationError`
- F8-4 フロア不合格のレポート本文と `activity` の `gate_failed` 行に適用閾値が入る
- F8-5 **`max_backtests_per_candidate < len(pairs)` の settings が `ValidationError`** (codex I10)。境界: `==` は通る
- F8-6 approval payload の `profitability_floor` が 3 生成箇所すべてに入り、非既定値 (`min_pf=1.5`) がそのまま載る (codex I3)

### F9 単一所有者の spy pin (codex I14)

- F9-1 `_check_profitability_floor` の呼び出しを spy し、**3 呼び出し元** (evaluator の in_sample 段 / holdout 段 / 親側 `run_backtest_handler`) から呼ばれ、それぞれ `scope="in_sample"` / `"holdout"` / `"in_sample"` と**実 metrics dict** が渡ることを pin
- F9-2 段 0 変異に「helper を呼ばず同等ロジックをインラインで複製する」を追加 → F9-1 が killer

### F10 U3 の直接 pin (codex M2)

- F10-1 PF / avg_r が合格する fixture で `max_drawdown` を 0.99 に、`kill_switch_latches` を 999 に変えても **verdict が変わらない** (合格のまま)。変異: DD / latches 門を追加 → killer
  - v1.6 注記: DD の部分は §10.5 の 3・5 で置き換わる (latches の部分は不変。DD は holdout、または `max_drawdown: null` のとき不変)

### 段 0 (指揮者の変異スイープ) の最優先 6 件

F1-4 (`trades>0, pf=None`) / F1-8 (enforce で holdout を回さない) / F2-4 (holdout `evaluable=false` を通す) / F2-5(b) (strict 判定が zero-trade より前) / F5-2 (ラベルの holdout 漏洩) / F9-2 (helper を呼ばない複製)。

---

## 9. 残る未決・起票

| # | 内容 | 状態 |
|---|---|---|
| — | U1〜U5 / codex C1・C2 ほか 18 件 | **裁定済・反映済** (§2、v1.1) |
| R1 | legacy submit 回廊 (`approval.submit_plugin`) を共有ゲートへ統合するか廃止するか。本束では **strategy を拒否**して穴を閉じるだけ | 別起票 [legacy-submit-corridor-bypasses-gate] |
| R2 | baseline 再生の未実装 | 別起票 [baseline-replay-unimplemented] (U4) |
| R3 | `settings_snapshot_hash` が `improve.gate` を含まない構造 (本束は payload snapshot + レポート/activity で代替) | 記録のみ |
| R4 | 実 DB に既存する `mission_outcome=None` の in_sample 行 4 件 (人間 corridor 由来) の遡及ラベル付け | 本束の範囲外 (記録のみ) |
| R5 | 遮断 8 の明示的例外 (集約 1 bit) の再評価トリガ = 改善ループが `unprofitable` 受領後に holdout 寄りへ体系的に収束する挙動の観測 | 運用監視項目 |

---

## 10. 追補 v1.6 [floor-add-selectivity-drawdown-killswitch] — max DD と取引頻度を床に足す

チケット `floor-add-selectivity-drawdown-killswitch`。状態: C0 v1.6.2 (レビュー 2 周目反映済み、ユーザー承認前)。U3 / F10 を **DD だけ**部分改訂する。kill switch 回数は床に入れない (§10.1)。

### 10.0 目的

承認 #84 (us500_lagged_return_fx: pf 1.002 / avg_r 0.004 / trades 864 / max DD 26% / kill switch 55 回) は、床 (pf ≥ 1.0 かつ avg_r > 0) を形式上通って承認依頼になった。床が「運用上の危うさ」と「ほぼ毎バー建てる戦略」を見ていない。これを決定論で止める。止める対象は次の 2 つ。

- in-sample の max DD が深い候補 (既定で有効、上限 0.20)。
- in-sample の取引頻度が高すぎる候補 (キーだけ用意し、閾値は再実測後に別裁定。それまで無効)。

### 10.1 前提の再検討

元の床は意図して「pf と avg_r だけ」(U3、F10)。チケットは DD / kill switch 回数 / 建玉率 / 最小 trades の 4 条件を提案した。1 件 (#84) の観測で床を足すのは危ういので、実 DB で承認済み戦略と並べて前提を確かめた。

#### 実測 (実 DB、read-only、`backtest_runs` の `variant='candidate'`、直近 60 日)

in_sample 322 行 / holdout_gate 54 行 (いずれも trades > 0)。取引頻度は暫定の近似 (`trades / (期間日数 × 1440 × 5/7 / 足分)`)。分母は T1 の `eval_bars` で置き換える。

| scope | 指標 | min | median | p90 | max |
|---|---|---|---|---|---|
| in_sample | trades | 1 | 97 | 535 | 1065 |
| in_sample | max_drawdown | 0.000 | 0.056 | 0.173 | 0.417 |
| in_sample | kill_switch_latches | 0 | 5 | 46 | 77 |
| in_sample | 取引頻度 (近似) | 0.0001 | 0.013 | 0.069 | 0.468 |
| holdout | trades | 1 | 25 | 63 | 93 |
| holdout | max_drawdown | 0.002 | 0.028 | 0.041 | 0.088 |
| holdout | kill_switch_latches | 0 | 1 | 3 | 6 |
| holdout | 取引頻度 (近似) | 0.0006 | 0.016 | 0.040 | 0.208 |

現行の床を通った in-sample 候補は 110 件 (content_hash 単位、全 pair 合計 trades ≥ 30 かつ全 pair が pf ≥ 1.0 / avg_r > 0)。案ごとの不合格数:

| 案 | 不合格 | 備考 |
|---|---|---|
| max DD > 0.20 | 2 | #84 を含む |
| kill switch > 1 (チケット案) | 98 | 承認済み 11 本は全滅 |
| kill switch > 0 | 105 | |
| 取引頻度 > 0.5 (チケット案) | 0 | #84 (0.446) も通る |
| 取引頻度 > 0.25 | 2 | #84 を含む。承認済みの最大は 0.110 |
| pair 別 trades < 30 | 6 | 承認済みの最小は 39 |

人間承認済みの strategy 11 本 (承認 #21〜#74、9/20〜10/28) と #84:

| 承認 # | 戦略 | in-sample DD | 取引頻度 (近似) | latches | trades |
|---|---|---|---|---|---|
| 21 | sma_cross_10_30 | 0.038 | 0.029 | 3 | 201 |
| 23 | bb_ema_breakout | 0.133 | 0.038 | 19 | 277 |
| 24 | rsi_bb_reversion_200 | 0.039 | 0.005 | 2 | 39 |
| 25 | rsi6_bb200_rr2 | 0.034 | 0.006 | 3 | 42 |
| 32 | ichimoku_rsi_cloud_reentry | 0.049 | 0.033 | 7 | 250 |
| 39 | asym_shock_breakout | 0.150 | 0.093 | 58 | 710 |
| 65 | volume_close_pressure | 0.104 | 0.093 | 51 | 715 |
| 71〜74 | close_pressure_followthrough ほか | 0.08〜0.15 | 0.037〜0.110 | 20〜75 | 287〜851 |
| (84) | us500_lagged_return_fx (人間が reject) | **0.263** | **0.446** | 55 | 864 |

上の 2 表は直近 60 日窓の暫定近似で、戦略名は一部省略、取引頻度は週末だけ除いた概算である。**再現可能な根拠ではない**: `run_in_sample` (`backtest/holdout.py:180`) は現在時刻から in-sample 期間を再計算するため、承認当時と同じ replay にならない。J1 の根拠は T1 の manifest (§10.7) を正本とし、上の表は方向づけにしか使わない。

#### T1 の再測定は「元行の再現」ではなく「現行条件での新規 in-sample 実行」

元の backtest_runs 行を再現することは求めない。理由 (現物):

- `run_in_sample` は保存済みの `period_start/end` を使わない。`now` を正規化し `holdout_months` (現在の settings) から end を再計算し、start は現在の履歴 DB の最古バーから取る (`backtest/holdout.py:180` 付近)。履歴の追記や settings の変更で、同じ `now` を渡しても期間は元と一致しない。
- 元行に残る設定の痕跡は不可逆な `settings_hash` だけで、設定の snapshot は無い。`core_commit` は再測定時の HEAD になる (`store/backtest_runs.py:332`)。元の設定・実行コードの復元や一致確認の手順は作らない。

そこで T1 の再測定は、13 件 (承認済み 11 本 + #84 + DD 超過の 1 本) を**現在の runner・現在の settings・現在の履歴で in-sample だけ**新規に回し、`eval_bars` 付きの新しい backtest_runs 行を作る (variant は `human_custom` か新 variant。holdout には触れない)。J1 の根拠は、この新規実行の値 (`eval_bars` / trades / 頻度 / DD) である。元行の値は参考列として並べるだけで、一致は求めない。manifest の形は §10.7。

#### 設定値は strict float

`improve.gate` の 2 キーは bool・文字列・nan / inf を受け付けない。現行の `_Strict` (`config.py:33`) は `extra="forbid"` だけで型強制を止めないため、何もしなければ `"0.2"` が 0.2 に、`true` が 1.0 に受理され、`max_drawdown: true` で DD の門が実質無効 (1.0 = 上限なし) になる。2 キーは strict (`float | None`、`strict=True`、`allow_inf_nan=False`、範囲 (0, 1]) で定義し、bool・文字列・nan / inf は `ValidationError`。YAML の整数 `1` は float として可。

#### 疑った前提と結論

1. **「kill switch ≤ 1」は弁別力が無い。床に入れない。** 110 件中 98 件が落ち、承認済み 11 本は全滅。latches は取引数にほぼ比例して増え (backtest の日次 DD kill が頻繁に立つ)、#84 (55) は承認済みの 51〜75 と重なる。latches ÷ trades の比も #84 0.064 / 承認済み 0.081〜0.088 で逆転する。DD 条件とも分離できない。→ **floor に入れず、親専有レポートへの表示 (現状) を維持**。U3 のうち latches 部分と F10 の latches pin は不変。
2. **「建玉率 ≤ 0.5」は #84 を捕まえない。** 弁別するのは 0.25 付近。ただし「建玉率」は誤称で、実体は「判定機会のあったバーのうち何割で建てたか」= **取引頻度** `trades / eval_bars`。分母は評価されたバー数で、上の近似 (週末除外 5/7) は祝日・データ欠損で誤差が出る。`eval_bars` は metrics に無いので、**閾値は T1 の再実測 (§10.7) の後に決めた (J1: 0.25、§10.8)**。
3. **max DD は閾値の根拠が出来た。** U3 (2026-09-12) の根拠は「in_sample 3.7〜23%、閾値の根拠が無い」だった。今は 60 日の実測で、承認済み 11 本の最大 0.150、#84 が 0.263、床通過 110 件のうち 0.20 超は 2 件。→ **DD は U3 を部分改訂して床に入れる**。ただし reject 側の観測は #84 の 1 件で、0.20 は当てはめでなく判断である (§10.6 に残す)。ユーザー裁定 (2026-11-01): 既定 0.20 で有効、設定で変更可。
4. **pair 別 `min_trades` は床に入れない。** 収益性でなく評価可能性の条件で、閾値の前後で不連続 (29 は落ち 30 は通る)。標本不足は既存の合計 30 件ルール (`insufficient_trades`) が受け持つ。別ゲートの候補として §10.6 に残す。
5. **scope は in-sample のみ。** holdout は 92 日・trades 中央値 25 で DD / 頻度の統計力が無く (実測: holdout の DD 最大 0.088、頻度最大 0.208)、新しい合否ビットを holdout 段から運ぶことにもなる (遮断 8、§4)。
6. **agent に返す語・数値・閾値は増やさない (J2 裁定済、§10.8)。** 遮断契約は 1 bit (`unprofitable`)。条件別の語を agent に返すと 1 bit が条件ごとに増える (§4 の受容範囲外)。DD の値そのものは agent が in-sample の `metrics` で既に見ているが、そこへ閾値や条件節まで返すと「どの値をどちらへ動かせば通るか」を教える決定論的な oracle になり、条件語を伏せても数値と閾値で同じ漏れになる。条件別の理由は親専有レポートと人間面だけに出す。

### 10.2 利用者から見た動き

- **改善ループが床で落ちたとき**: 候補の DD が上限 (既定 0.20) を超えていれば、pf / avg_r が合格でも承認依頼は作られず `unprofitable` で終わる (backlog は `observation`、従来の床不合格と同じ終端)。Discord には従来どおり `gate_failed` / `unprofitable` が届く。
- **人間が見る面**:
  - 親専有レポート (`data/improve_reports/`) に、落ちた条件ごとの 1 行 (`floor_failed: USDJPY drawdown_too_deep (max_drawdown=0.263 > 0.2)`、書式は §10.4) と、pair ごとの `max_drawdown` / `eval_bars` / 取引頻度の表が付く。`eval_bars` が無い旧 metrics は「未計測」と出て、頻度は検査されない。
  - `afx plugin submit` は `ValueError` の文に、落ちた pair ごとの条件語・値・閾値 (`outcome.floor_detail`) を入れて拒否する。現状 `ValueError` は規則文と閾値 kv しか持たないので、これは新しい配線である (§10.4)。`afx plugin bless` は警告つきで承認依頼を作り、承認詳細の `floor_warning=unprofitable` / `floor_detail=…` に条件語が見える (payload は既に `floor_detail` を持つので追従は自動)。
  - `activity.log` の `submit_floor_rejected` / `gate_failed` 行は候補ごとの理由を載せない。出るのは従来の規則文と閾値 kv (5 キー) だけ。
  - Discord の床未達 pair 行への条件語 (`drawdown_too_deep` / `too_frequent` の 2 語だけ) の追加は T2 の末尾 sub-step (§10.7)。通知 code は増やさない。
- **agent に見えるもの**: `run_backtest` の `submission_blocked` が `{"reason": "unprofitable", "detail": …}`。DD だけで落ちたときの `detail` は数値・閾値・条件語を含まない固定文 (「この成績では親ゲートが承認申請を出さず observation になります」)。pf / avg_r で落ちたときの `detail` は従来どおり。プロンプトの床の規則文にも DD / 頻度は載らない (J2)。`last_result` は従来どおり `unprofitable` 固定。
- **既定の効き方**: キーを書いていない settings でも、DD 0.20 超の候補は今日から落ちる。許すなら `improve.gate.max_drawdown: null` か大きい値を書く。

### 10.3 規則

#### キー (`improve.gate`、任意キー、`schema_version` は上げない)

| キー | 型 | 意味 | 既定 |
|---|---|---|---|
| `max_drawdown` | strict float (0, 1] または null | in-sample の pair 別 `max_drawdown` がこれを**超えたら**不合格。null で無効 | **0.20** |
| `max_trades_per_eval_bar` | strict float (0, 1] または null | in-sample の pair 別 `trades / eval_bars` がこれを**超えたら**不合格。null で無効 | **0.25** (J1、2026-11-01 ユーザー裁定。null で無効) |

- 値ちょうどは合格 (`pf < min_pf` と同じ向き)。範囲外 (0 以下、1 超)、bool、文字列、nan / inf は `ValidationError` (strict、§10.1)。
- 取引頻度の上限が 1 なのは、1 評価バーで建てられるのが最大 1 件のため。
- 「drawdown kill switch は config で無効化不可」(`risk.*`) とは別物。これは採用判断の品質基準で、緩めても実資金は動かない (§6 T3 と同じ整理)。

#### 判定 (`_floor_fail_items`、`plugin/strategy_gate.py:89-123`)

- 判定場所は既存の 1 箇所のまま。①→②→③の skip 順 (`strategy_gate.py:104-114`) は変えない。③を通った pair について、同じループの中で pf / avg_r / DD / 頻度を**互いに独立に**判定して項目を足す。現行は pf 不合格で `continue` するため (:119)、そのまま DD / 頻度を後ろに足すと pf も落ちた pair の DD / 頻度が記録されない。これを避けるため `continue` を外す。ただし avg_r は従来どおり pf が合格のときだけ見る (pf と avg_r の排他は維持。agent 面の `detail` が変わらないようにするため)。
- 掛かるのは `scope == "in_sample"` だけ (holdout は掛けない)。`trades == 0` の pair は②で除外済みなので掛からない。
- **測定値の型**: DD と `eval_bars` は、`bool` を除いた有限の数値 (int / float、nan / inf でない) のときだけ評価する。それ以外 (キー無し、`None`、`True` / `False`、文字列、nan / inf) は「未計測」として skip する (通す)。頻度の分子 `trades` も、`bool` を除く**非負の整数** (`int`) のときだけ使う。それ以外 (`True`、`"864"`、`864.0`、負数、`None`) のときは**頻度の判定だけ**を「未計測」として skip する (pf / avg_r / DD は従来どおり判定)。`True / 1938` を 1 取引と読んだり、`"864" / 1938` が例外になったりしないようにする`backtest_runs` の読み側の白リスト (`store/backtest_runs.py:49`) は bool / str も通すため、旧行や手編集 metrics の `eval_bars=True` や `"1938"` でここが曖昧になったり例外になったりしないようにする。
- DD: `max_drawdown` キーが null でなく、測定値が有効で上限を超えたら不合格 (kind = `drawdown_too_deep`)。
- 頻度: `max_trades_per_eval_bar` が null でなく、`eval_bars` が有効で 1 以上、かつ `trades` が上の型で `trades / eval_bars` が上限を超えたら不合格 (kind = `too_frequent`)。**未計測の pair は skip し、親専有レポートに「未計測」と出す** (通す)。
- pf が不合格の pair でも DD / 頻度は判定して項目を足す (pf と DD が同時に落ちる pair は、両方の kind がレポートに出る)。
- 戻り値の各項目に **kind** (閉じた集合: `pf` / `avg_r` / `holdout_not_evaluable` / `drawdown_too_deep` / `too_frequent`) を持たせる。現行の `(pair, message)` の 2 要素 tuple を kind つきの名前付き型に広げ、呼び出し元 (`_check_profitability_floor`、`improve_loop._render_floor_report_detail`) と既存 test の 2 要素 unpack を同じ task で更新する。**文字列の前置一致で分類しない**。
- `floor_mode` は既存のまま: 改善ループと `submit` は enforce、`bless` は warn (落ちても警告つきで承認依頼を作る)。warn で in_sample が落ちた候補も holdout・baseline まで完走する (C2)。

#### 固定語は人間面だけ、agent は `unprofitable`

- `_check_profitability_floor(per_pair, *, settings, scope, audience="human")` に `audience` を足す。label は常に `""` か `"unprofitable"` (新条件でも同じ)。
  - `audience="human"` の `detail` は全 kind の理由 (条件語・値・閾値)。`GateOutcome.floor_detail` を通して `ValueError` 文 (submit)、bless の approval payload (`floor_detail`)、親専有レポートに使う。**`ValueError` 文への配線は新規**: `switch.py:1369-1370` は今 `floor_rule_text` と `floor_settings_kv` しか使わず `outcome.floor_detail` を読まないので、このままでは条件語は表示されない。`outcome.floor_detail` を文に足す。**activity (`submit_floor_rejected` / `gate_failed`) には使わない**: 行は従来の規則文と閾値 kv だけで、候補ごとの値や条件語は載せない。
  - `audience="agent"` の `detail` は kind が `pf` / `avg_r` の理由だけ。新条件だけで落ちたときは空文字になり、`run_backtest_handler` (`improve_loop.py:1703-1710`) が固定文 (数値なし) を `detail` にする。label は新条件だけで落ちても `unprofitable` を返すので、`submission_blocked` は出る。
- `run_backtest` の返却 `metrics` (`_backtest_reply_from_save_kwargs`、`improve_loop.py:255`) から **`eval_bars` を除く**。in-sample 期間の長さ (遮断 7: 期間端点を見せない) を足数から推定させないため。台帳 (`save_kwargs`) には残す。
- `floor_rule_text(audience="agent")` (`strategy_gate.py:281-320`) は新条件を含めない (`require_holdout_evaluable` と同じ扱い)。`audience="human"` は含める。

#### 閾値の記録

- `ImproveGateSettings.snapshot()` (`config.py:347-361`) を 3 キー → 5 キーに広げる (末尾に `max_drawdown` / `max_trades_per_eval_bar`)。`floor_settings_kv` (`strategy_gate.py:266-278`) はその順で出す。approval payload の `profitability_floor`、activity の `gate_failed` 行 (と `submit_floor_rejected` 行の kv)、レポートの `thresholds:` 行は `snapshot()` / `floor_settings_kv` が唯一の所有者のまま。

### 10.4 配線表

| ファイル | 変更 |
|---|---|
| `plugin/strategy_gate.py` | `_floor_fail_items` (:89-123) に 2 条件と kind。pf 不合格の `continue` (:119) を外し、pf / avg_r / DD / 頻度を独立に append (avg_r は pf 合格時のみ)。DD / `eval_bars` は bool 除外の有限数値だけ評価。`_check_profitability_floor` (:126-148) に `audience`。`floor_settings_kv` (:266) / `floor_rule_text` (:281) の追従 (human のみ新条件) |
| `config.py` | `ImproveGateSettings` (:335-361) に 2 キー (範囲検査)、`snapshot()` を 5 キーへ |
| `config/settings.yaml.example` | `improve.gate` (:110-117) に 2 キーをコメント付きで追記 (DD は既定 0.20 と無効化の書き方、頻度は null と「再実測後に決める」旨)。`tests/fixtures/settings_examples/v5.yaml` は example と byte 一致を保つので同じ追記。`config/settings.yaml` (gitignore) は指揮者が配備時に同期 |
| `backtest/runner.py` | `BacktestResult` (:136-147) に `eval_bars: int = 0`。`pending_proposal = intent_source(closed_bar)` (:476) を実行した回数を数え、`BacktestResult` (:500-507) に載せる |
| `backtest/metrics.py` | `compute_metrics` (:109-144) が trades 0 の分岐を含め両方で `eval_bars` を返す。`METRIC_KEYS` (:28-31) に追加 |
| `store/backtest_runs.py` | 保存は `metrics` dict をそのまま JSON 化。読み側の白リスト (`_filter_metrics` :52-55、`in_sample_view` :190、`latest_in_sample_metrics` :217) は `METRIC_KEYS` 経由なので自動で追従 (旧行はキー欠落のまま)。`_COMPARE_METRIC_KEYS` (`strategy_gate.py`) は広げない |
| `tests/backtest/golden/golden-v3.json` | 再生成。差分は metrics ブロックの `eval_bars` 1 キーの追加だけ |
| `loops/improve_loop.py` | `run_backtest_handler` (:1703-1710) が `audience="agent"` で固定文を組む。返却 `metrics` から `eval_bars` を除く (:255)。`_render_floor_report_detail` (:4563-4614) に条件別 1 行と `### floor_conditions` 表を、**最後の表の後ろに**追記。`_FLOOR_REPORT_METRIC_KEYS` (:4559) は 8 キーのまま |
| `loops/improve_loop.py` の `FloorPairRow` (:629) / `parse_floor_report` (:736-800) | `FloorPairRow` に `conditions: tuple[str, ...] = ()` を足す。`parse_floor_report` は最後の表の後ろから `### floor_conditions` を探して下記の形式で strict に読む。節が無ければ全 pair が空条件 (現状は末尾を未検査で捨てる。旧レポートと既存 fixture は同じ `FloorDetail` に読める) |
| `plugin/switch.py` | `ValueError` 文 (:1369-1370) に `outcome.floor_detail` を足す (**新規配線**。今は `floor_rule_text` と `floor_settings_kv` だけ)。`submit_floor_rejected` activity (:1366-1368) は変えない (規則文 + kv、候補ごとの detail は載せない)。payload `profitability_floor` (:1471, :2433) は `snapshot()` で追従、bless の `floor_detail` (:2432) は既に配線済みで human detail に追従 |
| `commands.py` (:918-931) | 承認詳細は `floor_detail` をそのまま出すので変更なし (表示確認の test のみ) |
| `ops/service.py` (`_floor_of` :960-997) と afx-ops (`gate/improve_render.py:244-252`) | **T2 の末尾 sub-step**: `_floor_of` は pair の dict に `conditions` を**空でないときだけ**足す (旧レポートは出力が byte 同一)。afx-ops の `_floor_lines` は `conditions` があれば pair 行の末尾に固定の日本語ラベルを付ける。kind は閉じた集合 `{drawdown_too_deep, too_frequent}` で、afx-ops は語一致 test で同期し、集合外の語は表示しない。通知 code (`NOTICE_UNPROFITABLE`、`improve_notice_codes.py:60`) は増やさない |
| `scripts/reevaluate_approvals.py` (:18-24, :52-70, :89-105) | 現状は `_GATE` が新既定 (DD 0.20) を継承する一方、`_metric` が `max_drawdown` を落とすので、DD を含む旧 payload でも DD 判定だけ黙って skip する。**DD を正規化して (bool 除外の有限数値、無ければ未計測) 新基準で再評価し、出力に旧 3 条件の判定と新基準の判定を別の列で並べる**。`_verdict` の `holdout_not_evaluable` 文字列一致は kind に置き換える。read-only は維持 |

#### `### floor_conditions` の形式 (親専有レポート、strict)

最後の表 (holdout 段があれば holdout 表、無ければ in_sample 表) の後ろに、見出し `### floor_conditions` を**ちょうど 1 回**置く。続けて空行、下の表、その後ろに条件別 1 行 (`floor_failed: …`) を並べる。この行は人間向けの補助文で、**parser は読まない・契約にも含めない**が、複数 pair でも一意に読めるよう書式を固定する (下記)。

```
### floor_conditions

| pair | max_drawdown | eval_bars | trades_per_eval_bar | failed_kinds |
|---|---|---|---|---|
| USDJPY | 0.263 | 1938 | 0.446 | drawdown_too_deep, too_frequent |

floor_failed: USDJPY drawdown_too_deep (max_drawdown=0.263 > 0.2)
floor_failed: USDJPY too_frequent (trades_per_eval_bar=0.446 > 0.25)
```

`floor_failed:` 行の書式: `floor_failed: <pair> <kind> (<名>=<値> > <閾値>)`。落ちた (pair, kind) ごとに 1 行。並びは表の pair 順、同じ pair 内は `drawdown_too_deep`、`too_frequent` の順。名は `max_drawdown` / `trades_per_eval_bar`。値は小数 3 桁 (`{:.3f}`)、閾値は設定値を `{:g}` で書く。pf / avg_r の不合格は従来の行のままで、この書式には入れない。

- 行は in_sample 表の pair と同じ順・同じ集合 (trades 0 の pair も含む)。列は上の 5 列固定。
- `max_drawdown` / `eval_bars` / `trades_per_eval_bar` は有限数値、または未計測のとき `未計測`。`failed_kinds` は閉じた集合 `drawdown_too_deep` / `too_frequent` をこの順に `, ` で連結、無ければ `-`。同じ pair の pf / avg_r の不合格は別の列 (`failed`) が持つので、ここには入れない。
- parser は、節が無ければ空条件。節があって形が崩れている (見出し 2 回、列違い、集合外の語、重複、行の pair が in_sample 表と食い違う、`failed_kinds` が空でない pair が in_sample 表の `failed` 列で False) ときは**全体を `None`** にする (部分出力しない)。`failed` 列の無い旧形式に節があるのも崩れとして扱う。
- 読んだ `failed_kinds` は in_sample 段の `FloorPairRow.conditions` に入る。holdout 段の行は常に空。

レポートの追記を最後の表の後ろに置くのは、`parse_floor_report` が `thresholds:` の直後から `### in_sample` を要求し、表の見出しを `_FLOOR_REPORT_METRIC_KEYS` で検査するため。途中に行を足す・列を増やすと、公開済みレポートも含めて `None` になり (`ops/service.py:979`)、web / Discord の床の数値が消える。

### 10.5 受入条件 (観測可能)

1. `improve.gate` の 2 キーを省いた settings が読め、`max_drawdown == 0.20` / `max_trades_per_eval_bar == 0.25`。範囲外 (0、負、1 超) は `ValidationError`、null は通る。**strict**: `max_drawdown` / `max_trades_per_eval_bar` に `true` / `false`、`"0.2"`、`.nan`、`.inf` を書くと `ValidationError` (`true` が 1.0 として DD の門を無効にしない)。整数 `1` は通る。example 追記後も v5.yaml と byte 一致。
2. 既定 0.20 で、#84 相当 (pf 1.002 / avg_r 0.004 / DD 0.263 / trades 864) の in-sample 行: 改善ループは承認依頼を作らず backlog `last_result` が `unprofitable` 完全一致、`submit` は `ValueError` で、その文に `drawdown_too_deep` と値 (`max_drawdown=0.263`) が入る (`outcome.floor_detail` の配線)、`bless` は承認依頼を作って `floor_warning == "unprofitable"` / `floor_detail` に `drawdown_too_deep`。
3. 承認済み 11 本相当 (DD ≤ 0.150) は通る。DD 0.20 ちょうどは通り、0.2000001 は落ちる。`max_drawdown: null` なら DD 0.99 でも通る。
4. 既定 (`max_trades_per_eval_bar: 0.25`) で `trades / eval_bars == 0.25` は通り、超えで落ちる。null を明示すると `eval_bars` が極端でも verdict 不変。`eval_bars` の無い metrics は通り、レポートに「未計測」が出る。DD と `eval_bars` が bool (`True`)、文字列 (`"1938"`)、nan / inf、`None` のときも例外にならず「未計測」として通る (bool は 1 とみなされない)。`trades` が bool (`True`)、文字列 (`"864"`)、float、負数、`None` のときも、頻度だけが「未計測」で skip され例外にならず、pf / avg_r / DD の判定は変わらない (`True` は 1 取引と評価されない)。
5. holdout 段には掛からない: holdout の `max_drawdown` / `eval_bars` / `trades` を極端値にしても verdict 不変 (F10 の分割、§8)。
6. agent 面: DD のみ不合格のとき `submission_blocked == {"reason": "unprofitable", "detail": <固定文と完全一致>}`。`detail` に数字・閾値・pair 名・`drawdown_too_deep` / `too_frequent`・「holdout」が無い。pf / avg_r 不合格の `detail` は従来の文のまま。`run_backtest` 返却の `metrics` に `eval_bars` が無い。`floor_rule_text(audience="agent")` と改善プロンプトに DD / 頻度の閾値・語が無い。`audience="human"` には現れる。`last_result`・activity の `gate_failed` 行の理由・INDEX に新語が無い (閾値 kv の `max_drawdown=` は除く)。`submit_floor_rejected` と `gate_failed` の activity 行は候補ごとの値・pair・条件語を持たず、規則文と閾値 kv だけである (DD で落ちた候補と pf で落ちた候補で行の形が変わらない)。
7. 親専有レポートに条件別 1 行と `### floor_conditions` 表 (max_drawdown / eval_bars / 取引頻度 / 落ちた条件) が出る。`_FLOOR_REPORT_METRIC_KEYS` は 8 キーのまま。`tests/fixtures/improve_reports` の既存レポートが無改変で同じ `FloorDetail` に読め (全 pair の `conditions` が空)、新形式のレポートも読める。形式は §10.4「`### floor_conditions` の形式」。
8. activity の `gate_failed` 行 (`floor_settings_kv`) と approval payload の `profitability_floor` が 5 キーになる。**3 キー固定の既存 pin を更新する**: `tests/plugin/test_strategy_gate_floor.py:261` (`floor_settings_kv` の順と書式)、`tests/loops/test_improve_prompt_objective.py:206` と `tests/loops/test_improve_loop_approval_and_ledger.py:52` (`profitability_floor` payload)、`tests/test_config.py:1024-1046` (F8-1 の既定値、F8-2 の example キー集合)、`tests/backtest/test_metrics.py:62-66` (trades 0 の返却 dict に `eval_bars`)、`tests/backtest/test_golden_diff_v2_v3.py:28` (classifier が追加 metric を `kill_switch_latches` 1 個と固定しているので、許容集合を `{kill_switch_latches, eval_bars}`、`added_metric_keys` を 2 に直し、`eval_bars` の型 int の変異 case を足す)、F6-11a / F8-6 / `test_improve_e2e` の閾値行。**旧 approval payload を使う fixture (3 キーの `profitability_floor`) は歴史データなので 3 キーのまま残す** (旧 payload が読めることの pin)。
9. 通知: 新条件の不合格は (`gate_failed`, `unprofitable`) で届き、`ALLOWED_PAIRS` が増えない。afx-ops の語表同期 test が緑。
10. **`eval_bars` は runner が `intent_source` を呼んだ回数と一致する**: 週末・市場クローズ・`closed_bar is None` を含む replay fixture で、`intent_source` の spy の呼び出し回数 == `BacktestResult.eval_bars` == `metrics["eval_bars"]`。trades 0 の run でも `eval_bars` が入る。`METRIC_KEYS` と `compute_metrics` の返却キーが一致 (既存の集合一致 test)。保存→`in_sample_view` の読み戻しで残り、旧行 (キー無し) は欠落のまま読める。
11. golden: `golden-v3.json` を再生成し、旧との機械 diff が metrics ブロックの `eval_bars` 1 キーの追加だけ。byte 一致 test が緑。
12. **T1 の新規 in-sample 実行と manifest**: 13 件それぞれに、現行条件で新規に回した in-sample 実行の manifest 項目があり (§10.7)、その行は `eval_bars` を持つ新しい backtest_runs 行である。manifest は新規実行の run id / `content_hash` / artifact の場所 / pair・timeframe / `source`・`base_interval` / 実行時 `now` と算出された start・end / `settings_hash` / `core_commit` を持つ。元行は参考列で、一致は求めない。再実行できなかった項目 (artifact 欠落、履歴不足) は理由つきで列挙し、J1 の根拠から外す。manifest と表は in-sample の値だけで、holdout の値・期間を含まない。実行は holdout の行を作らない。
13. 単一所有者 (F9): `_check_profitability_floor` の 3 呼び出し元 spy が `audience` も含めて通る。
14. **pf と DD の同時不合格**: pf < `min_pf` かつ DD が上限超の pair の fixture で、`_floor_fail_items` が両 kind を返し、親専有レポートの `failed_kinds` に `drawdown_too_deep` が出て、agent 面の `detail` は pf の理由だけである。pf と avg_r が両方落ちる pair は従来どおり pf だけが項目になる。
15. **`### floor_conditions` の strict 読み**: §10.4 の形式で、(a) 節なしの旧レポートは全 pair 空条件、(b) 正しい新形式は `conditions` が入る、(c) 見出し 2 回・列違い・集合外の語・重複・pair 不一致・`failed` 列と食い違う `failed_kinds` は全体 `None`。`ops/service._floor_of` は空条件の pair に `conditions` キーを出さない (旧レポートの出力が byte 同一)。afx-ops は 2 語の日本語ラベルを語一致 test で同期し、集合外の語を表示しない。
16. **`scripts/reevaluate_approvals.py`**: DD を含む旧 payload (DD 0.263) で、旧 3 条件の列は合格、新基準の列は DD 超過と出る。DD が無い・bool・非有限の payload は新基準でも未計測として skip する。read-only は維持。`tests/scripts/test_reevaluate_approvals.py` を更新。
17. 変異 (すべて pin が赤になること): (a) `>` を `>=` にする、(b) 新条件を判定する場所を呼ばず複製する、(c) in_sample 以外にも掛ける、(d) agent 面の `detail` に数値・条件語を入れる、(e) `eval_bars` を agent 返却 `metrics` に残す、(f) レポートの追記を最後の表の前に置く (parser pin)、(g) `eval_bars` 無しを不合格にする、(h) pf 不合格の `continue` を戻して DD / 頻度を判定しない、(i) bool / 文字列 / nan の `eval_bars` を数値として評価する、(j) `ValueError` 文に `floor_detail` を入れない、(k) `submit_floor_rejected` / `gate_failed` activity に候補ごとの detail を入れる、(l) `reevaluate_approvals` が `max_drawdown` を落とす、(m) bool / 文字列の `trades` を頻度の分子に使う、(n) 設定 2 キーの strict を外し bool / 文字列を受理する。

### 10.6 残余

- **選択バイアス**: 閾値は承認済み 11 本 (人間が選んだ生存者) と reject 1 件 (#84) の間に置いた。DD 0.20 が通る/落ちる境界は 1 件の観測に依存し、別の形の薄い戦略には効かないかもしれない。T1 の新規 in-sample 実行の 13 本で多少は確かめる (実行できなかった項目は数えない)。
- **pf 1.002 / avg_r 0.004 という薄さ**は床 (pf ≥ 1.0) の仕様どおりで、本節の対象外。`min_pf` を上げる案は別 (`min_pf` は既に任意キー)。
- **pair 別 `min_trades`** は評価可能性の条件で、別ゲートの候補 (収益性の床とは混ぜない)。実測では床通過 110 件中 6 件が pair 別 30 未満、承認済みの最小は 39。
- **kill switch 回数**は床に入れない。承認済み 11 本は latches 2〜75 で、取引数に比例する。選別の代理にならない限り、表示だけを続ける。
- **agent が理由を知らない**: DD だけで落ちた候補は、agent から見ると pf / avg_r が合格なのに `unprofitable` になる。mission 内の再試験 (上限 6 回) を無駄にしうる。これは遮断契約を 1 bit に保つ代償として受け入れる (J2 裁定済、§10.8)。
- **頻度の分母は評価バー全体**: 市場オープンで `closed_bar` が得られたバーだけを数える。戦略が内部で skip したバー (`skipped_bars_by_reason`) も分母に入る (判定機会はあったが戦略が見送ったバー)。

### 10.7 task 表

| # | task | 担当 | 触るもの | 規模 | 依存 |
|---|---|---|---|---|---|
| T1 | **eval_bars 計測 + 再実測**: `BacktestResult` / `compute_metrics` / `METRIC_KEYS` / golden 契約 (§10.5 の 10・11)。再実測は承認済み 11 本 + #84 + DD で落ちるもう 1 本の計 13 本を、現行条件で in-sample だけ新規に回す **driver** と、その **manifest** (下記) を成果物にする (元行の再現は求めない、§10.1)。manifest の新規実行の値 (`eval_bars` / trades / 頻度 / DD) の表で J1 の頻度閾値を提示する | codex terra | `backtest/{runner,metrics}.py`、golden、test、`scripts/` の driver | 約 260 / 330 | なし |
| T2 | **床 / 設定 / レポート / 漏洩 pin**: 2 キー、`_floor_fail_items` の 2 条件と kind、`audience`、agent 面の固定文、`eval_bars` の返却除外、`snapshot()` 5 キー、example / v5.yaml、`ValueError` への `floor_detail` 配線、レポート追記と parser (`FloorPairRow.conditions`)、`scripts/reevaluate_approvals.py` の新基準化、3 キー固定 pin の更新 (§10.5 の 8)、§10.5 の 1〜9・13〜17。末尾 sub-step: ops / Discord の条件語 (肥大したら T3 に切る。床の動作は不変) | codex terra | `config.py`、`plugin/strategy_gate.py`、`plugin/switch.py`、`loops/improve_loop.py`、`config/settings.yaml.example`、`tests/fixtures/settings_examples/v5.yaml`、`scripts/reevaluate_approvals.py`、(末尾) `ops/service.py`・afx-ops | 約 220 / 400 | T1 |

判定 (pytest) は指揮者が回す。実 kernel / systemd は触らないので sonnet は不要。T1 の再実測は in-sample の replay を回し、backtest_runs に新規行を足すだけで、plugin・approval・backlog を変更しない。

#### T1 の manifest (J1 の根拠表)

13 件それぞれについて、次を 1 項目として列挙する。成果物は `tmp/` 配下 (gitignore、戦略名と hash を含むためコミットしない)。driver は `scripts/` に置く。plugin と履歴 DB は read-only で開き、書くのは新規の in-sample 行だけ。

- 識別: 戦略名、`content_hash`、immutable な artifact の場所 (`plugins/_archive` 等) とその hash、承認 # (#84 と DD で落ちるもう 1 本は元の backtest_runs の id)。
- 新規実行の条件: 新しい backtest_runs 行の run id、pair 集合と timeframe、履歴源 (`source`) と `base_interval`、**実行時の `now` と、そこから `run_in_sample` が算出した start / end** (算出結果を記録する。入力として指定しない)、現在の `settings_hash`、現在の `core_commit`。
- 結果 (新規実行): `eval_bars` / trades / 頻度 / DD。J1 の根拠はこの値。
- 参考列 (元行): 元の in_sample 行の `trades` / `max_drawdown` / `created_at`。並べるだけで、一致の判定はしない。
- 除外: artifact 欠落、履歴の不足など再実行できない理由。除外した項目は J1 の根拠に使わない。

出力は in-sample だけで、holdout の値・期間は含めない。

### 10.8 裁定

**裁定済み**

- DD の既定は **0.20 で有効、設定で変更可** (2026-11-01 ユーザー)。
- **J2 agent に DD の条件を見せるか: 見せない** (裁定済 2026-11-01、指揮者: 既存遮断契約の維持)。agent は in-sample の DD 値を既に見るが、閾値や条件節まで返すと値をどちらへ動かせば通るかを教える決定論的 oracle になる。条件語を伏せても数値と閾値で同じ漏れで、1 bit (`unprofitable`) の維持ではなく遮断契約の変更になる。旧推奨 (A: DD の数値と閾値だけの条件節を agent に見せる) は採らない。A を採るなら実装の小変更でなく、§4 の脅威モデルと §10.2 / §10.5 の 6 を改訂する利用者裁定として扱う。頻度の閾値と条件語も同じく見せない。

**指揮者の既定 (裁定不要と判断)**

- kill switch 回数は床に入れない (report-only 維持、U3 の latches 部分と F10 の latches pin は不変)。
- scope は in-sample のみ。pair 別 `min_trades` は床に入れない。
- 通知 code は増やさず `unprofitable` に束ねる。

- **J1 取引頻度の閾値: 0.25 で有効、設定で変更可** (2026-11-01 ユーザー裁定)。T1 の新規 in-sample 実行 13 本 (除外 0) で、承認済み 11 本は 0.005〜0.113 (最大 #72)、#84 は 0.447 (864 / 1931)。0.25 は承認済み最大の約 2.2 倍、#84 の約 0.56 倍。落ちる側の観測は #84 の 1 件だけ (13 本目は #84 の lock 前の同一戦略)。承認済みは 1h 足、#84 は 4h 足で分母が違う。

**未裁定**: なし。

---

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案 + ユーザー裁定反映。T0 (reject reason 漏洩) / T1 (収益性フロア 2 段: in_sample + holdout、ラベルは `unprofitable` 固定) / T2 (RPC ヒント + プロンプト、in_sample のみ) / T3 (config 3 キー) の 4 task。U1 = holdout も門に (固定文言 1 bit は許容、遮断 8 にただし書き追記) / U2 = submit は raise・bless は警告のみ / U3 = DD・latches は門にせずレポートに出す / U4 = baseline 再生は別起票 / U5 = 除染はユーザー実行。指揮者既定 = holdout は `evaluable=true` のときだけ判定 (`require_holdout_evaluable=False`) | 実機観測 `tmp/a4-run16-codex-20260912.md` / `tmp/approval-20260912b.md` (approval #10 = mission 80)、実 DB read-only 調査 (`backtest_runs` 35 行 / `approval_requests` 10 件 / `improvement_backlog` 64 行)、ユーザー裁定 2026-09-12 | - |
| 2026-09-12 | v1.1 | codex 設計レビュー 1 周目 18 件 (Critical 2 / Important 14 / Minor 2) を全件反映。C1 = legacy submit (`approval.submit_plugin`) は kind=strategy を拒否し materialize → `--from _human` を案内 (回廊統合は別起票) / C2 = `floor_mode` を evaluator まで渡し `warn` では in_sample 不合格でも holdout・baseline 完走 / I1 = 遮断 8 の例外を「人間 reject と同程度」ではなく「反復可能な二値 oracle を受容する脅威モデル」として逐語記録 (テスト保証は数値・段名・pair・baseline 差分の不在に限定) / I2 = `_run_full_gate` に非コミット sink、人間回廊の gate 行に明示 outcome (`approval` / `unprofitable` / `gate_failed`) / I3 = approval payload 3 箇所に `profitability_floor` snapshot / I4・I5 = tuple 拡張を撤回し `GateOutcome` dataclass、`bless_candidate` は `int` 戻りを維持して `on_floor_warning` コールバックで警告を渡す (tests 11 箇所を壊さない)、`_approval_detail` に floor 表示 / I6 = `_finalize_gate_failed` の gate 行・ledger 行・settle に同じ branch-local outcome、report 失敗時は `report_failed` を上書きしない / I7 = `submission_blocked` は親 `run_backtest_handler` で 1 回だけ導出 (builder/registry のシグネチャは変えない) / I8 = プロンプト・CLI 文言を settings からレンダ / I9 = strict holdout 判定を zero-trade shortcut より前 / I10・I11 = 「別名で予算再取得」を撤回し `max_backtests_per_candidate >= len(pairs)` validator + 全 pair backtest 規律 / I12 = 純粋漏洩 pin を Landlock skip の無い新規モジュールへ / I13 = F5-2 を完全一致 + canary + `floor_detail` + 段名断片に限定 / I14 = `_check_profitability_floor` の 3 呼び出し元 spy pin + 「helper を呼ばない」変異 / M1 = F1-4 を `trades>0, pf=None, avg_r>0` / M2 = DD・latches 極端値で verdict 不変の pin | codex 設計レビュー 1 周目 `tmp/design-profitability-floor/codex-design-r1.md` (gpt-5.6-sol、spec/plan v1 = 3ab0b06)、指揮者裁定 2026-09-12 (全件採用) | - |
| 2026-09-12 | v1.2 | codex 設計レビュー 2 周目 5 件 (Important 3 / Minor 2) を全件反映。R2-I1 = `run_kind_gate` はゲート判定で例外を投げず**常に `GateOutcome` を返す**契約に固定、判別子 `verdict_kind` (`ok`/`insufficient_trades`/`floor`) + `floor_failed` を追加し enforce の raise と失敗行 outcome の決定は `_run_full_gate` が型付き結果から行う、**例外メッセージによる分類は禁止**と明記 (標本不足 = `gate_failed` / フロア = `unprofitable` を別々に pin、F6-9〜F6-11) / R2-I2 = `submit_candidate(..., activity=None)` を追加し `_plugin_submit` から `ActivityLog(root/"logs"/"activity.log")` を渡す配線を明記 (F6-11) / R2-I3 = 共通 settle 契約を維持し終端表「フロア不合格 + report 作成失敗」の archive / INDEX を `report_failed` に訂正、F4-10 を gate 行・ledger 行・settle・INDEX の 4 点 + ledger `PERSISTED` に拡張 / R2-M2 = 閾値 snapshot の出力先を「approval を作る全終端 (成功・bless 警告)」に限定し、approval を作らない失敗はレポート / activity で監査と明記 | codex 設計レビュー 2 周目 `tmp/design-profitability-floor/codex-design-r2.md` (gpt-5.6-sol、v1.1 = cbed926)、指揮者裁定 2026-09-12 (全件採用) | - |
| 2026-09-12 | v1.3 | codex 設計レビュー 3 周目 3 件 (Important 2 / Minor 1) を全件反映。**v1.3 で実装着手可** (codex 収束判定)。R3-I1 = T1-c に残っていた旧契約「`run_kind_gate(..., floor_mode="enforce")` が `floor_reason` 時に `ValueError`」を削除し「`run_kind_gate` は `GateOutcome` を返し raise しない / `_run_full_gate` が `verdict_kind` と `floor_mode` から決める」に統一 (enforce/warn の対応表は T1-d に一本化、`floor_mode` を evaluator へ渡す理由 = C2 の完走のためと明記。同旨の残存を grep で 0 件確認) / R3-I2 = F6-11 を (a) `submit_candidate` 層 (イベント・閾値 3 値・`activity=None` 後方互換) と (b) **CLI 層 spy pin** (`_plugin_submit --from _human` が `ActivityLog(root/"logs"/"activity.log")` 相当を `activity=` に渡す) に二分 — (a) だけでは CLI 配線欠落を検出できない / R3-M1 = F6-12 新設 (`run_kind_gate` の fake が `record_fn` に 1 行積んだ後に contract 外例外を投げるケースで、その行が `gate_failed` で保存され元例外が伝播 / 規定 `ValueError` へ変換され、`unprofitable` にならない) | codex 設計レビュー 3 周目 `tmp/design-profitability-floor/codex-design-r3.md` (gpt-5.6-sol、v1.2 = 8ef4694)、指揮者裁定 2026-09-12 (全件採用) | - |
| 2026-09-13 | v1.4 | 実装レビューでの設計微修正: `GateOutcome.floor_failed` を削除し判別子は `verdict_kind` のみ (冗長) / 閾値 snapshot は `ImproveGateSettings.snapshot()` が所有 / フロア規則の文言は `strategy_gate.floor_rule_text(gate, audience="agent"|"human")` が所有し、**agent 向けには `require_holdout_evaluable` 条件を出さない** (遮断 8、§4)、human 向け (CLI 警告・ValueError・activity) には出す / `run_kind_gate(sink=)` で呼び出し元が行 sink を渡せる | `/code-review high` 2 周目 8 件 (`tmp/review-20260912-pf/code-review-r2.md`) の裁定 | 684fb31 |
| 2026-09-13 | v1.5 | §4 ただし書きの**再評価を実施 (ユーザー指示)、例外は維持**。根拠: run17〜19 (#81〜#84) で `holdout` の語・数値・段名は prompt に 0 件、run19 観測 D (#84 が #83 とビット一致の候補を再提出) は「holdout 寄りへの体系的収束」ではなく無差別な再現 — 1 bit は agent 自筆 note (#74「提出条件を満たした」) に負けて学習に使われなかった。是正は note 衛生 / 再提出抑止の側 ([floor-path-skips-duplicate-metrics] 裁定へ)、質検査母集団の拡張は偽陽性再導入のため採らない。追加観測点: `unprofitable` 後の候補がパラメータ空間で単調変化しているか (二分探索の形) | `tmp/floor-dup-ticket/{materials,reevaluation}.md` | - |
| 2026-11-01 | v1.6 | §10 追加: 床に in-sample の max DD 上限 (`improve.gate.max_drawdown` 既定 0.20、有効、null で無効) と取引頻度上限 (`max_trades_per_eval_bar` 既定 null、`eval_bars` 計測後に別裁定) を任意キーで追加。**U3 / F10 の部分改訂** (DD のみ。kill switch 回数は床に入れず report-only、latches の不変 pin は残す)。pair 別 `min_trades` は床に入れない。agent 面は `unprofitable` 1 語のまま、条件別の語は親専有レポートと人間面だけ。レポートの追記は `parse_floor_report` を壊さないよう最後の表の後ろ。2 task (T1 計測 → T2 床) | #84 (承認依頼、人間が reject) の実測 (DD 0.263、頻度 0.446)、直近 60 日の実 DB 分布 (床通過 110 件、承認済み 11 本)、sol の前提確認、ユーザー裁定 2026-11-01 (DD 既定 0.20 有効) | - |
| 2026-11-01 | v1.6.1 | §10 に床拡張レビュー 1 周目 (Critical 1 / Important 5 / Minor 1) を全件反映。T1 は 13 件の read-only manifest + 再測定 driver を成果物にし、非再現は J1 の根拠から外す (§10.1 / §10.7) / `_floor_fail_items` は pf 不合格の `continue` を外して pf・DD・頻度を独立に append (avg_r は pf 合格時のみで agent 面の `detail` は不変) / `ValueError` に `floor_detail` を配線、`submit_floor_rejected` と `gate_failed` activity は規則文と閾値 kv だけ (§10.3 と §10.5 の 6 の矛盾を解消) / `### floor_conditions` の strict 形式と `FloorPairRow.conditions`、`_floor_of` / afx-ops は閉じた 2 語 (§10.4) / 3 キー固定 pin の更新一覧 (§10.5 の 8) / `reevaluate_approvals.py` は DD を正規化して新基準で再評価し旧 3 条件の列と並べる / DD・`eval_bars` は bool 除外の有限数値だけ評価、他は未計測で skip / **J2 は裁定済: DD の条件語・閾値を agent に見せない** | `tmp/review-floor/r1-terra.md` (terra)、`r1-rulings.md` (指揮者裁定 2026-11-01、全件採用) | - |
| 2026-11-01 | v1.6.2 | §10 に床拡張レビュー 2 周目 (Critical 1 / Important 2 / Minor 3) を全件反映。**T1 の再測定を「元行の再現」から「現行条件での新規 in-sample 実行」に再定義** (`run_in_sample` が now と現在の settings から期間を再計算する事実を §10.1 に、元行との一致は求めず参考列にする。manifest は新規実行の条件を記録、§10.5 の 12・§10.7) / 頻度の分子 `trades` も bool 除外の非負整数に限定し、不正値は頻度だけ未計測 skip (§10.3、§10.5 の 4・17m) / 設定 2 キーは strict float (bool・文字列・nan / inf は ValidationError、`_Strict` が型強制を止めない事実を §10.1 に、§10.5 の 1・17n) / `floor_failed:` 行の書式を固定し parser は読まない補助文と明記 (§10.4) / 文書タイトルを v1.6.2 に / §0・§2・§3 の「DD は門にしない」を歴史記述と現行規則 (§10) に分けて書き直し | `tmp/review-floor/r2-sol.md` (sol)、`rev2-brief.md` (指揮者裁定 2026-11-01、全件採用) | - |
| 2026-11-01 | v1.6.3 | J1 裁定: `max_trades_per_eval_bar` 既定 0.25 で有効 (設定可)。T1 再実測 13 本の分布を §10.8 に。`get_signals` (tools/signal_tools.py:147) も `eval_bars` の除外対象に加える (T2) | T1 の再実測、ユーザー裁定 | - |

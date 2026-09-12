# 収益性フロア + reject reason 漏洩の是正 設計書 v1.1

束 [profitability-floor] + T0 [reject-reason-leak]。起案 2026-09-12、ユーザー裁定済 (U1〜U5)、codex 設計レビュー 1 周目 18 件を全件反映 (v1.1)。

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
| U3 `max_drawdown` / `kill_switch_latches` | **門にしない**。ただしレポートには出す |
| U4 baseline 再生の未実装 | 別起票 [baseline-replay-unimplemented] (指揮者が tickets に記載) |
| U5 除染 | スクリプト更新済、ユーザー実行待ち |
| C1 legacy submit | `afx plugin submit <name>` (live plugin 回廊) は **kind=strategy のとき拒否**し `materialize` → `submit --from _human` を案内する。indicator/signal は従来どおり。共有ゲートへの統合・回廊廃止は別起票 [legacy-submit-corridor-bypasses-gate] |
| C2 bless の warn | `floor_mode` を evaluator まで渡す。`warn` では in_sample 不合格でも **holdout・baseline 収集まで完走**し失敗ビットを保持する (短絡は `enforce` のときだけ) |

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
- **`max_drawdown` / `kill_switch_latches` は判定式に一切現れない** (U3)。極端値でも verdict は不変 (pin F10)
- **順序 ①→②→③ が契約** — ① を ② の後に置くと `require_holdout_evaluable=True` でも `trades=0` の pair が通ってしまう (codex I9)

### ラベルと数値の分離 (遮断 8)

| 出力先 | 内容 | 根拠 |
|---|---|---|
| `improvement_backlog.last_result` | **`unprofitable` (固定文言のみ、完全一致)** | プロンプトに注入される唯一の面 (§4) |
| `activity` の `gate_failed` 行 | `mission=<id> reason=unprofitable` (固定) + 適用閾値 (`min_pf=… require_positive_avg_r=… require_holdout_evaluable=…`) | activity は親専有だが `last_result` と文言を揃え「どちらを見ても同じ」状態を作る |
| 親専有レポート (`data/improve_reports/improve-YYYY-MM-DD-<mission_id>.md`) | **全数値** — in_sample / holdout の pair ごとの `trades / pf / win_rate / avg_r / max_drawdown / total_pnl / kill_switch_latches / evaluable`、落ちた段、落ちた pair、適用閾値 | 人間だけが読む。worker の rw に無い |
| `backtest_runs` | 既存どおり metrics 全数 + `mission_outcome='unprofitable'` | 監査台帳 |
| approval payload (`profitability_floor`) | 適用した閾値 snapshot — **成功・警告・失敗の全終端**で (§3 T1-g、codex I3) | 緩めて通した候補の監査 |

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

- `approval.run_kind_gate(..., floor_mode="enforce")` → `evaluate_strategy_adoption_gate` へ転送。`enforce` で `floor_reason` が立っていたら `ValueError(f"plugin {meta.name!r}: unprofitable")`。`warn` は続行し結果型に載せる。
- `switch.submit_candidate` → `floor_mode="enforce"`。`switch.bless_candidate` → `floor_mode="warn"`。

### T1-d 名前付き結果型 (codex I4/I5 — tuple 拡張をやめる)

```
@dataclass(frozen=True)
class GateOutcome:                     # plugin/approval.py
    metrics: dict
    evaluable: bool
    floor_warning: str = ""            # "" | "unprofitable"
    floor_detail: str = ""             # 人間向け全数値 (payload / 表示用)
    gate_rows: tuple[dict, ...] = ()   # T1-f の非コミット sink に積まれた行
```

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
| `submit_candidate` その他ゲート不合格 (標本不足 / pytest / hash) | `ValueError` | `gate_failed` |
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
  - 人間 submit のフロア不合格 → `activity.write(Category.APPROVAL, "submit_floor_rejected", f"name=… unprofitable min_pf=… …")` と `ValueError` メッセージ
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
| **フロア不合格 + report 作成失敗 (新)** | `_finalize_gate_failed` の OSError 分岐 | observation / `report_failed:<safe_reason>` | **`report_failed`** (`unprofitable` で上書きしない) | **`report_failed`** | — | 失敗 | `NULL` | なし | 削除 |
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
- F4-10 **report 作成失敗分岐では gate 行・ledger 行とも `report_failed`** (`unprofitable` で上書きしない)。変異: 引数をそのまま使う → killer

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
- F6-8 人間 corridor の gate 行に `mission_outcome` が付く: submit 成功 = `approval` / submit フロア不合格 = `unprofitable` / submit その他不合格 = `gate_failed` / bless (警告含む) = `approval`。**`None` の行を新たに作らない** (codex I2)。変異: `record_fn` を渡さず即時 commit に戻す → killer

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

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案 + ユーザー裁定反映。T0 (reject reason 漏洩) / T1 (収益性フロア 2 段: in_sample + holdout、ラベルは `unprofitable` 固定) / T2 (RPC ヒント + プロンプト、in_sample のみ) / T3 (config 3 キー) の 4 task。U1 = holdout も門に (固定文言 1 bit は許容、遮断 8 にただし書き追記) / U2 = submit は raise・bless は警告のみ / U3 = DD・latches は門にせずレポートに出す / U4 = baseline 再生は別起票 / U5 = 除染はユーザー実行。指揮者既定 = holdout は `evaluable=true` のときだけ判定 (`require_holdout_evaluable=False`) | 実機観測 `tmp/a4-run16-codex-20260912.md` / `tmp/approval-20260912b.md` (approval #10 = mission 80)、実 DB read-only 調査 (`backtest_runs` 35 行 / `approval_requests` 10 件 / `improvement_backlog` 64 行)、ユーザー裁定 2026-09-12 | - |
| 2026-09-12 | v1.1 | codex 設計レビュー 1 周目 18 件 (Critical 2 / Important 14 / Minor 2) を全件反映。C1 = legacy submit (`approval.submit_plugin`) は kind=strategy を拒否し materialize → `--from _human` を案内 (回廊統合は別起票) / C2 = `floor_mode` を evaluator まで渡し `warn` では in_sample 不合格でも holdout・baseline 完走 / I1 = 遮断 8 の例外を「人間 reject と同程度」ではなく「反復可能な二値 oracle を受容する脅威モデル」として逐語記録 (テスト保証は数値・段名・pair・baseline 差分の不在に限定) / I2 = `_run_full_gate` に非コミット sink、人間回廊の gate 行に明示 outcome (`approval` / `unprofitable` / `gate_failed`) / I3 = approval payload 3 箇所に `profitability_floor` snapshot / I4・I5 = tuple 拡張を撤回し `GateOutcome` dataclass、`bless_candidate` は `int` 戻りを維持して `on_floor_warning` コールバックで警告を渡す (tests 11 箇所を壊さない)、`_approval_detail` に floor 表示 / I6 = `_finalize_gate_failed` の gate 行・ledger 行・settle に同じ branch-local outcome、report 失敗時は `report_failed` を上書きしない / I7 = `submission_blocked` は親 `run_backtest_handler` で 1 回だけ導出 (builder/registry のシグネチャは変えない) / I8 = プロンプト・CLI 文言を settings からレンダ / I9 = strict holdout 判定を zero-trade shortcut より前 / I10・I11 = 「別名で予算再取得」を撤回し `max_backtests_per_candidate >= len(pairs)` validator + 全 pair backtest 規律 / I12 = 純粋漏洩 pin を Landlock skip の無い新規モジュールへ / I13 = F5-2 を完全一致 + canary + `floor_detail` + 段名断片に限定 / I14 = `_check_profitability_floor` の 3 呼び出し元 spy pin + 「helper を呼ばない」変異 / M1 = F1-4 を `trades>0, pf=None, avg_r>0` / M2 = DD・latches 極端値で verdict 不変の pin | codex 設計レビュー 1 周目 `tmp/design-profitability-floor/codex-design-r1.md` (gpt-5.6-sol、spec/plan v1 = 3ab0b06)、指揮者裁定 2026-09-12 (全件採用) | - |

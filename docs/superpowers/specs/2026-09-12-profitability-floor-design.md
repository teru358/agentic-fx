# 収益性フロア + reject reason 漏洩の是正 設計書 v1

束 [profitability-floor] + T0 [reject-reason-leak]。起案 2026-09-12、ユーザー裁定済 (U1〜U5)。

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
| 「submit は 1 本の corridor」 | `afx plugin submit --from _human` → `switch.submit_candidate` → `_run_full_gate` → `approval.run_kind_gate` → `evaluate_strategy_adoption_gate` (`cli.py:496`, `switch.py:779`)。一方 `afx plugin submit <name>` (live `plugins/` の既存 plugin) → `approval.submit_plugin` → `_validate_kind` → `_validate_strategy` (`cli.py:514`, `approval.py:118-158`) で、**共有ゲートを通らず holdout も回さない独自実装** | corridor は 2 本ある。後者は本束以前から holdout を持たないので、フロアも **in_sample 段だけ**を課す (§3 T1-d) |

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

### 指揮者の既定選択 (裁定不要と判断、既定として明記)

**holdout の収益性判定は、その holdout 行が `evaluable=true` のときだけ行う。** `evaluable=false` (標本不足) は R8 どおり「悪いとは言わない」= **フロアは通す**。config `improve.gate.require_holdout_evaluable: bool = False` で厳格化できる。

理由: `backtest.holdout_months=3` の現設定では低頻度戦略が構造的に 30 件に届かない (#10 は holdout trades 7)。holdout の標本不足を理由に落とすかは個別判断 (人間が approval payload を見て決める) であり、決定論で一律に落とすと低頻度戦略の評価経路が閉じる。

---

## 3. T1 [profitability-floor] — 収益性フロアを決定論で課す

### 誰が何をどう扱うか

戦略採用ゲート (`plugin/strategy_gate.py::evaluate_strategy_adoption_gate`) が **2 段**で収益性を見る。

1. **in_sample 段**: 標本十分 (`total_trades >= 30`) を確認した直後、in-sample の pair ごとの成績で判定。不合格なら **holdout を回さずに** 即 `floor_reason` を立てて return (落ちる候補に holdout の計算コストを払わない / holdout 情報の発生自体を無くす)。
2. **holdout 段**: in_sample 段を通過した候補について holdout を pair ごとに回し、**返り値を捕捉して** (現状は捨てている) 同じ判定関数にかける。`evaluable=false` の holdout 行は判定から除外する (既定、§2)。不合格なら `floor_reason` を立てる (holdout 行は既に `record_fn` に積まれているので、台帳行としては残る)。

判定は `_check_profitability_floor` 1 箇所が所有する (T2 の RPC ヒントも同じ関数を呼ぶ — 2 つの導出がズレない。§A/CR7 と同じ規律)。

`ImproveLoop.commit` は `floor_reason` が立っていたら既存の `_finalize_gate_failed` 経路に倒す。backlog は `observation` に落ち `list_open` に残るので次 mission が再挑戦できる (R8 の精神)。

### 判定規則 (逐語)

pair ごとに判定し、**1 pair でも不合格なら候補全体を落とす** (§A の多 pair 方針と同じ — `_check_duplicate_metrics_for_approval` は「いずれか 1 pair 一致で候補全体を降格」)。pair 合計の pf は算術的に合成できない (`gross_profit`/`gross_loss` が `METRIC_KEYS` に無い) ため、合計ではなく pair ごとに見る。

```
_check_profitability_floor(per_pair: dict[str, dict], *, settings, scope: str) -> str
  # 戻り値: "" (合格) または "unprofitable" (不合格。固定文言)
  # detail は別に返す (レポート用、ラベルには使わない) → 実装は
  #   (label: str, detail: str) のタプルを返す形にする
  g = settings.improve.gate
  for pair, m in per_pair.items():
      trades = m["trades"]
      if trades == 0:                       # 成績が無い pair は判定から除外
          continue
      if scope == "holdout" and not m.get("evaluable"):
          if g.require_holdout_evaluable:
              FAIL(pair, "holdout_not_evaluable")
          continue                          # 既定: 標本不足は通す (R8)
      pf = m["pf"]
      if pf is not None and pf < g.min_pf:   # pf is None (gross_loss==0) は PASS
          FAIL(pair, f"pf={pf}")
      avg_r = m["avg_r"]
      if g.require_positive_avg_r and avg_r is not None and avg_r <= 0.0:
          FAIL(pair, f"avg_r={avg_r}")
  return ("", "")
```

- `min_pf` 既定 1.0 → `pf == 1.0` は PASS (`<` であって `<=` ではない)
- `require_positive_avg_r` 既定 True → `avg_r == 0.0` は FAIL
- `avg_r is None` は `trades == 0` のときだけ起こる (`metrics.py:123`) ので上の `continue` で吸収される

### ラベルと数値の分離 (遮断 8)

| 出力先 | 内容 | 根拠 |
|---|---|---|
| `improvement_backlog.last_result` | **`unprofitable` (固定文言のみ)** | プロンプトに注入される唯一の面 (§4) |
| `activity` の `gate_failed` 行 | **`mission=<id> reason=unprofitable`** (固定) + 適用閾値 (`min_pf=1.0 require_positive_avg_r=True`) | activity は親専有だが、`last_result` と文言を揃えて「どちらを見ても同じ」状態を作る (将来 activity が注入面になったときの安全余裕) |
| 親専有レポート (`data/improve_reports/improve-YYYY-MM-DD-<mission_id>.md`) | **全数値** — in_sample / holdout の pair ごとの `trades / pf / win_rate / avg_r / max_drawdown / total_pnl / kill_switch_latches / evaluable`、落ちた pair と落ちた段、適用閾値 | 人間だけが読む。worker の rw に無い (§2.1) |
| `backtest_runs` | 既存どおり metrics 全数 (`mission_outcome='unprofitable'`) | 監査台帳 |

### 変更点表

| file | 変更 |
|---|---|
| `plugin/strategy_gate.py` | (a) `StrategyGateVerdict` に `floor_reason: str = ""` と `floor_detail: str = ""` を追加。**`evaluable` は流用しない** — `evaluable=False` は R8 の「標本不足」意味論を 2 箇所が読んでいる (`plugin/approval.py:216-221` が `"strategy not evaluable (insufficient trades)"` で raise、`improve_loop.py:2179` が `observation_reason` を `insufficient_trades:` として扱う)。(b) `_check_profitability_floor(per_pair, *, settings, scope)` を新設。(c) in-sample ループ直後・`evaluable` 判定の直後に in_sample 段を呼び、不合格なら `StrategyGateVerdict(evaluable=True, floor_reason="unprofitable", floor_detail=…)` を即 return (**holdout ループに入らない**)。(d) holdout ループの `run_holdout(...)` の**返り値を pair ごとに捕捉**し (`holdout_per_pair[pair] = run_holdout(...)`)、ループ後に holdout 段を呼ぶ。不合格なら同じ形の verdict を return (`candidate_metrics` は in_sample を載せる — レポートが両方を読めるように `floor_detail` に holdout 側も文字列化して入れる) |
| `loops/improve_loop.py` (`:2179` の直後) | `if strategy_verdict.floor_reason:` 分岐を追加 → `self._finalize_gate_failed(conn, ctx=ctx, backlog_id=selection.backlog_id, reason=strategy_verdict.floor_reason, now=now, gate_rows=tuple(gate_rows), tool_calls=tool_calls, mission_outcome="unprofitable", report_detail=strategy_verdict.floor_detail)` → `return` |
| `loops/improve_loop.py::_finalize_gate_failed` | (a) `mission_outcome: str = "gate_failed"` を引数化 (既定は現行値のまま — 既存 3 呼び出しの挙動不変)。(b) `report_detail: str = ""` を追加し、レポート本文 (`body_md`) の後段に全数値を書く。(c) `activity` の `reason=` は引数 `reason` のまま (= `unprofitable` 固定) だが、適用閾値を 1 行足す。`last_result` は `reason` 逐語 = `unprofitable` |
| `plugin/approval.py::run_kind_gate` | `floor_mode: Literal["enforce", "warn"] = "enforce"` を追加。`verdict.floor_reason` が立っていたら enforce → `ValueError(f"plugin {meta.name!r}: unprofitable")`、warn → 続行し `(metrics, evaluable, floor_warning)` を返せるよう戻り値を拡張 (下記) |
| `plugin/approval.py::_validate_strategy` | legacy corridor (`submit_plugin`) 用に **in_sample 段のみ**のフロアを追加 (この corridor は holdout を回さない)。`_check_profitability_floor(per_pair_metrics, settings=settings, scope="in_sample")` が立ったら `ValueError(f"plugin {meta.name!r}: unprofitable")` |
| `plugin/switch.py::_run_full_gate` | `floor_mode` を引数に足して `approval.run_kind_gate` へ転送。戻り値を `(meta, content, artifact, metrics, evaluable, floor_warning)` に拡張 |
| `plugin/switch.py::submit_candidate` | `floor_mode="enforce"` (既定)。`_run_full_gate` の戻り値 6 要素化に追随 |
| `plugin/switch.py::bless_candidate` | `floor_mode="warn"`。`floor_warning` が立っていたら payload に `"floor_warning": "unprofitable"` と `"floor_detail": <全数値>` を足し、`activity: ActivityLog | None = None` 引数 (既存 `reconcile` と同じパターン、`switch.py:122,140`) で `Category.APPROVAL, "bless_floor_warning"` を書く |
| `backtest/cli.py::_plugin_bless` | `ActivityLog(root / "logs" / "activity.log")` を構築して `bless_candidate(..., activity=activity)` に渡す (`cli.py:556` に既存の構築例がある)。`floor_warning` が立っていたら stderr に「警告: この候補は収益性フロア (pf<1 または avg_r<=0) を満たしません。人間裁定で承認申請を作成しました。詳細は `afx> approval <id>`」を出す (終了コードは 0 のまま) |
| `store/backlog.py::_OUTCOME_TABLE` | **変更なし**。フロアは approval 決定 (`apply_approval_outcome`) を通らず `finish_improve_mission(backlog_transition=…)` を使う経路なので無関係 |

### 終端名

- **backlog status** = `observation` (既存 `_finalize_gate_failed` と同じ)。`rejected` にしない — §4.3 逐語「`rejected` は人間が `backlog reject` で閉じる用」を崩さない。
- **`backtest_runs.mission_outcome`** = 新設 `unprofitable`。`gate_failed` に混ぜると「形式/pytest で落ちた」と「測って負けた」が台帳上区別できなくなり、次の設計の材料を失う。CHECK 制約が無いので migration 不要。
  - **in_sample 行と holdout_gate 行の両方に付く** — `_persist_gate_rows` は `gate_rows` 全件に同じ `mission_outcome` を書く (`improve_loop.py:1799-1812`)。holdout 段で落ちたときは in_sample 行 (全 pair) + holdout_gate 行 (全 pair) が揃って `unprofitable` になる。in_sample 段で落ちたときは in_sample 行のみ (holdout を回していない)。
  - **§A の dedup 母集団には影響しない**。`find_matching_approved_metrics` は `mission_outcome='approval'` かつ `content_hash IN (approval_requests に載った hash)` で絞る (`backtest_runs.py:260-278`) ので、`unprofitable` 行は母集団に入らない。フロアで落ちた候補の成績が将来の候補を誤って重複判定することは無い。
  - `latest_in_sample_metrics` の live 絞り (`IS NULL OR 'approval'`、`:231`) も自動的に除外する。
- **`improvement_runs.result`** = `report` (レポートを書くので §4.2 手順7 の値規約どおり)。
- **`improvement_backlog.last_result`** = `unprofitable` (固定)。

---

## 4. 遮断 8 との整合 (構築規則 + ただし書きの追記)

ラベル (= `last_result` = プロンプト注入面) に入れてよいもの / ダメなもの:

| 情報 | 注入可否 | 理由 |
|---|---|---|
| in_sample の `trades` / `pf` / `avg_r` / `max_drawdown` | 可 | `run_backtest` RPC が同じ期間の同じ指標を agent に返している (2026-08-16 設計書 `:318`)。agent が既に知っている値 |
| holdout の数値 | **不可** | 遮断 8 逐語「holdout の指標」 |
| holdout の `evaluable` / 閾値合否 | **不可** | 遮断 8 逐語「**閾値別合否**」 |
| 「holdout」という語そのもの | **不可** | 合否ビットの運搬になる |
| baseline との差分 | **不可** | 遮断 8 逐語「baseline 差分」 |
| 「フロアで落ちた」という 1 bit (固定文言 `unprofitable`) | **可 (U1 裁定)** | 下記 |

### ただし書き (T0 で 2026-08-16 設計書 §7.1-1 ⑧ に追記する)

> **フロアの 1 bit は人間 reject と同じ露出量として許容する** (2026-09-12 ユーザー裁定 U1)。改善ループの backlog 履歴表には、従来から「人間に却下された」という 1 bit が `rejected:<理由>` として注入されていた (T0 [reject-reason-leak] で `rejected_by_human` の固定文言に是正)。収益性フロアの不合格も同様に `unprofitable` の固定文言 1 件だけを注入する — **in_sample 段で落ちたか holdout 段で落ちたかを区別せず、数値も pair 名も付けない**。遮断 8 が防いでいるのは「agent が holdout に対して反復最適化すること」であり、固定文言 1 bit では最適化の勾配 (どのパラメータが holdout を改善したか) が作れない。数値・段・pair は親専有レポートと `backtest_runs` にだけ残す。

この追記により、遮断 8 の統合回帰 (`tests/integration/test_improve_forbidden_regression.py` の ⑧) は「holdout の指標・baseline 差分・**閾値別合否の内容**がどこにも現れない」を検査する形に精密化される (固定文言 1 bit は許容、その中身は不可)。

### T2 のヒントは in_sample のみ

`run_backtest` RPC の返却に足す `submission_blocked` ヒントは **in_sample 判定の結果のみ**から導く。holdout は agent に見せない (遮断 8)。holdout 段で落ちる候補は、agent から見ると「ヒントは出なかったのに `unprofitable` になった」という形になる — これは許容された 1 bit の範囲内。

---

## 5. T0 [reject-reason-leak] — 人間の却下理由をプロンプトから切る

### 誰が何をどう扱うか

人間が `afx> reject <id> <理由>` を打つと理由は 2 箇所に行く: ①`approval_requests.reason` (人間専用の台帳) ②`improvement_backlog.last_result` に `rejected:<理由>` として (`store/backlog.py:111` の `_OUTCOME_TABLE['rejected']`)。②は次 mission の改善履歴表に逐語で載る。**②を固定文言 `rejected_by_human` にし、理由は①だけに残す**。そのうえで①を人間が読める導線を足す (現在 `afx> approval <id>` は reason を表示しない)。

### 変更点表

| file | 変更 |
|---|---|
| `store/backlog.py:111` | `"rejected": ("observation", "rejected:{reason}")` → `("observation", "rejected_by_human")` (テンプレートから `{reason}` が消えるので `apply_approval_outcome` の `"{reason}" in template` 分岐は固定文言側を通る — コード変更不要) |
| `store/approvals.py:103-107` | `apply_approval_outcome(reason=…)` に渡す `(reason or "")` は使われなくなる。**引数は残す** (`expired`/`invalidated` と API を揃える) が、`rejected` 経路が reason を無視することを docstring に明記 |
| `commands.py::_approval_detail` (`:345-366`) | 出力に `reason=<approval_requests.reason>` / `decided_by` / `decided_at` の行を足す。**T0 の必須項目** — これが無いと人間の却下理由がシェルから読めない write-only 情報になる |
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` §7.1-1 ⑧ | §4 の「ただし書き」を追記 (フロアの 1 bit と人間 reject の 1 bit を許容、内容は不可) |
| `tests/store/test_backlog.py:197` | パラメータ `("selected","rejected","observation","rejected:")` を `"rejected_by_human"` の完全一致へ |
| `tests/store/test_approvals.py:150` | `assert row["last_result"] == "rejected:not useful"` → `== "rejected_by_human"` |
| `tests/integration/test_improve_forbidden_regression.py` | 遮断 8 (⑧) に §7 の pin を追加 |
| 運用 (コミット対象外、U5) | `UPDATE improvement_backlog SET last_result='rejected_by_human' WHERE last_result LIKE 'rejected:%'` (backlog #1/#3)。実行はユーザー |

### 影響範囲の全数 (grep 済み)

`last_result` を**読む**箇所: `improve_context.py:75,102,109` (履歴表 + backlog items/notes) / `improve_loop.py:629-633` (履歴表の描画) / `tests/` の assert 群 (上記 2 本のみが `rejected:` に依存)。
`_backlog_table` (`improve_loop.py:636-644`) は `id / idea / status / attempts / assigned` のみを描き `last_result` を描かない → **注入面は改善履歴表だけ**。
approve 側は `reason=str(approval_id)` を渡して `approved:<id>` になる (`approvals.py:105-106`) → 人間文字列の漏洩なし。
`expired` / `invalidated` はテンプレートに `{reason}` を持たない定数 → 漏洩なし。
`mission_failed:{reason}` / `gate_failed:{reason}` / `observation:{reason}` は**機械由来**の文字列 (人間の自由入力ではない) ので T0 の対象外。

---

## 6. T2 / T3

### T2 [floor-feedback-in-prompt] — 予算内で作り直させる

agent は `run_backtest` の返却で in_sample の `pf`/`avg_r` を見ている (`improve_rpc_tools.py:142-162`、遮断 7 = 集計指標のみなので正当)。今は「pf 0.713 で不採算」と自分で書きながら提出している (#10 の summary 逐語)。**提出が無駄になることを返却とプロンプトの両方で告げる**。

| file | 変更 |
|---|---|
| `tools/improve_rpc_tools.py::run_backtest` | `remaining_budget` を足している箇所 (`:157-162`) の隣で、`_strip_forbidden` 済み response に `submission_blocked` を足す: `{"reason": "unprofitable", "detail": "pf=0.713 avg_r=-0.121 — この成績では親ゲートが承認申請を出さず observation になります"}`。**成績が in_sample 段を通るときはキーを足さない**。判定は T1 の `_check_profitability_floor(…, scope="in_sample")` を**同じ関数で**呼ぶ。holdout は一切参照しない |
| `loops/prompts/improve_mission.md` 規律 4 末尾 | 下記文言を追記 |
| `loops/prompts/improve_mission.md` 規律 3 | 「`observation` として理由を残し」に「**試したパラメータと得られた pf / avg_r を具体的に**」を足す (次回の出発点になる) |

文言案 (規律 4 の末尾):

```
   - **`pf < 1.0` または `avg_r <= 0` の候補は提出しても承認申請になりません**
     (親の決定論ゲートが `unprofitable` として observation に落とします)。
     予算内でパラメータ・フィルタ・エントリ条件を変えて `run_backtest` を
     やり直し、`pf >= 1.0` かつ `avg_r > 0` を満たした候補だけを提出して
     ください。予算を使い切っても満たせなければ**提出せず** `observation`
     として、試したパラメータ群とそれぞれの pf / avg_r を理由に書いて
     ください (次回の出発点になります)。
```

予算との整合: `max_backtests_per_candidate=6` は**候補ごと** (`counters.backtest_calls[name]`)。パラメータを変えた候補を別名で書けば枠は別。`max_writes=30` / `max_tool_calls=300` の範囲で 3〜4 候補 × backtest 数回は収まる。追加 config は不要。

### T3 [floor-config]

| file | 変更 |
|---|---|
| `config.py::ImproveGateSettings` (`:226-228`) | `min_pf: float = Field(ge=0.0, default=1.0)` / `require_positive_avg_r: bool = True` / `require_holdout_evaluable: bool = False` を追加 |
| `config/settings.yaml.example` (`:95-97`) | `improve.gate` に 3 キーをコメント付きで追記 |
| `config/settings.yaml` (gitignore) | **同期** (CLAUDE.md 規約) |

**「drawdown kill switch は config で無効化不可」との関係**: 別物。あの制約は**実運用の資金保護** (`risk.*` の kill switch) が対象で、無効化できないことが資金を守る。収益性フロアは**採用判断の品質基準**であり、緩めても実資金は動かない (承認は人間の最終判断を通る)。緩められることが運用上必要でもある (新 pair / 低頻度戦略の暫定評価)。

**監査の穴と対処**: `settings_snapshot_hash` は `improve.gate` を含まない (§0) ので、閾値を緩めても `backtest_runs.settings_hash` は変わらない。`settings_snapshot_hash` に `improve` を足す案は既存の全 hash を変え §A の dedup 母集団や既存行の identity に波及するため採らない。代わりに **適用した閾値をレポート本文と activity の `gate_failed` 行に逐語で書く** (T1 の変更点表)。

---

## 7. 全終端表 (フロア不合格候補の扱い)

§A の表形式に合わせる。「フロア不合格」2 行が新設分。

| 終端 | 経路 | backlog status / `last_result` | `backtest_runs.mission_outcome` | 台帳 (`analysis_runs`) | archive / INDEX | report ファイル | `improvement_runs.result` | approval 行 | staging |
|---|---|---|---|---|---|---|---|---|---|
| 標本不足 (既存) | `_finalize_gate_failed` | observation / `insufficient_trades:<n>` | `gate_failed` | `gate_failed` | `status='gate_failed'` | 書く | `report` | なし | 削除 |
| 形式/pytest 不合格 (既存) | `_finalize_gate_failed` | observation / `gate_failed:<reason>` | `gate_failed` | `gate_failed` | `status='gate_failed'` | 書く | `report` | なし | 削除 |
| **フロア不合格 (in_sample 段、新)** | `_finalize_gate_failed(mission_outcome='unprofitable')` | observation / **`unprofitable`** | **`unprofitable`** (in_sample 行のみ — holdout は回していない) | `gate_failed` | `status='gate_failed'` | **書く** (全数値 + 適用閾値) | `report` | なし | 削除 |
| **フロア不合格 (holdout 段、新)** | 同上 | observation / **`unprofitable`** (in_sample 段と**区別できない**) | **`unprofitable`** (in_sample 行 + holdout_gate 行の全 pair) | `gate_failed` | `status='gate_failed'` | **書く** (in_sample + holdout 全数値) | `report` | なし | 削除 |
| 成績一致降格 (§A、既存) | `_finalize_success` → rollback → `_finalize_report_or_observation` | observation / `observation:duplicate_metrics_of:<hash> pair=<P>` | `observation` | `observation` | `status='observation'` | 書かない | `NULL` | なし | 削除 |
| observation (agent 申告、既存) | `_finalize_report_or_observation` | observation / `observation:<safe_text(reason)>` | `observation` | `observation` | `status='observation'` | 書かない | `NULL` | なし | 削除 |
| 承認申請 (既存) | `_finalize_success` | selected / `approval_pending:<id>` | `approval` | `approval` | `status='approval'` (§B) | 任意 | `approval` | 作る | **残す** |
| 却下 (人間、T0 後) | `apply_decision('rejected')` | observation / **`rejected_by_human`** | (変化なし) | — | — | — | — | 決定済み | — |
| bless 警告 (U2、新) | `bless_candidate(floor_mode="warn")` | — (backlog を持たない) | `approval` (通常の承認経路) | — | — | — | — | **作る** (payload に `floor_warning`) | — |

フロア不合格を `_finalize_gate_failed` に載せる副産物で、**親専有レポートが書かれる** (§A の降格では書かれない)。人間が「何がどれだけ負けたか」を後から読める導線がこちらにはある — 採用理由のひとつ。

---

## 8. 検証 (変異下限)

節番号は外部参照用に固定する。リストは下限。

### F1 in_sample 段の閾値境界

- F1-1 `pf == min_pf` ちょうど → PASS。変異: `<=` に変える → killer
- F1-2 `pf == min_pf - ε` → FAIL、`last_result == "unprofitable"` (完全一致)
- F1-3 `avg_r == 0.0` → FAIL。`avg_r == +ε` → PASS。変異: `< 0` に変える → killer
- F1-4 **`pf is None` (gross_loss==0) → PASS**。変異: `None` を fail 側に倒す / 素で比較して `TypeError` → killer (実 DB の `no_strategy` 行が `pf=None`)
- F1-5 `pf == 1.5` かつ `avg_r == -0.01` → FAIL (2 門が独立である pin)。変異: `avg_r` 検査を落とす → killer
- F1-6 `require_positive_avg_r=False` で F1-5 が PASS
- F1-7 `min_pf=0.0` で pf 0.5 が PASS (config が効く pin)
- F1-8 in_sample 段で落ちたとき **`run_holdout_gate` が呼ばれない** (monkeypatch で呼び出し回数 0)。変異: 判定を holdout の後に置く → killer

### F2 holdout 段

- F2-1 in_sample pf 1.5 / avg_r +0.2 (通過) かつ holdout `evaluable=true` / pf 0.8 → **FAIL**、`last_result == "unprofitable"`。変異: holdout の返り値を捨てる (現行実装) → killer
- F2-2 holdout pf == `min_pf` ちょうど → PASS。`min_pf - ε` → FAIL
- F2-3 holdout `evaluable=true` / pf 1.2 / avg_r −0.01 → FAIL (holdout でも avg_r 門が効く)
- F2-4 **holdout `evaluable=false` (trades 7) / pf 0.59 → PASS** (既定。R8)。変異: `evaluable` を見ずに判定 → killer (#10 型の低頻度戦略が一律で落ちる)
- F2-5 `require_holdout_evaluable=True` で F2-4 が FAIL
- F2-6 holdout 段で落ちたとき `backtest_runs` の in_sample 行**と** holdout_gate 行の全 pair が `mission_outcome='unprofitable'`
- F2-7 holdout 段で落ちた候補の `last_result` が **in_sample 段で落ちた候補と文字列として同一** (`"unprofitable"`)。変異: ラベルに段名/pair/数値を足す → killer

### F3 多 pair

- F3-1 2 pair で 1 pair のみ FAIL → **候補全体が FAIL**。変異: 「全 pair FAIL で落とす」に変える → killer
- F3-2 `trades=0` の pair (`avg_r=None`) は判定から除外され、他 pair の成績で決まる。変異: `None` を FAIL 扱い → 低頻度多 pair 戦略が不当に落ちる killer

### F4 再ルート後の副作用

- F4-1 `backtest_runs` の行が `mission_outcome='unprofitable'`。変異: `None` で保存 → `latest_in_sample_metrics` の live 絞りを通ってしまう killer (既存 CR1 と同型)
- F4-2 `improvement_backlog.status == 'observation'` かつ `list_open` に含まれる (再挑戦可)。変異: `rejected` → R8 killer
- F4-3 `approval_requests` に行が増えない
- F4-4 親専有レポートが公開され (`report_state='published'`)、本文に **in_sample / holdout の全数値 + 適用閾値 + 落ちた段 + 落ちた pair + `max_drawdown` + `kill_switch_latches`** が含まれる (U3: 門にしないがレポートには出す)。変異: 閾値を書かない → F8-4 killer / `kill_switch_latches` を書かない → U3 killer
- F4-5 `plugins/_archive/INDEX.md` に 1 行残る
- F4-6 staging が削除される / 台帳が `PERSISTED`
- F4-7 既存 3 呼び出し (`insufficient_trades` / `gate_failed:*` / `backtest_data_unavailable`) の `mission_outcome` が `gate_failed` のまま (引数化の後方互換 pin)
- F4-8 **§A の dedup 母集団に `unprofitable` 行が入らない** — フロアで落ちた候補と同成績の次候補が `duplicate_metrics_of` にならないこと

### F5 遮断 8 (統合回帰 `tests/integration/test_improve_forbidden_regression.py` ⑧ に追加)

- F5-1 人間が `reject <id> "holdout pf 0.80 で負け"` した後、次 mission の**レンダ済みプロンプト全文**に `"holdout"` / `"0.80"` / 理由文字列が現れない。変異: `_OUTCOME_TABLE['rejected']` を `rejected:{reason}` に戻す → killer
- F5-2 holdout 段でフロア不合格になった後のレンダ済みプロンプト全文に `"holdout"` / holdout の数値 / 段名 / pair 名が現れない (現れるのは `unprofitable` のみ)。変異: ラベルに holdout pf を入れる / `floor_detail` を `last_result` に渡す → killer
- F5-3 `approval_requests.reason` には理由が**残っている** (人間の台帳は失わない)
- F5-4 `afx> approval <id>` の出力に `reason=` が現れる (T0 の導線 pin)
- F5-5 `_history_table` が `last_result` をレンダする唯一の経路であることの pin (`_backlog_table` が `last_result` をレンダし始めたら落ちる assert)
- F5-6 `submission_blocked` (T2) に `"holdout"` の語・holdout の数値・期間端点が無い (既存 pin `"period_start" not in json.dumps(out)` を拡張)

### F6 共有 corridor (U2)

- F6-1 `switch.submit_candidate` (`--from _human`) でフロア不合格の strategy が `ValueError` になり approval 行が作られない。変異: フロアを `improve_loop.commit` 側だけに置く → killer
- F6-2 `switch.bless_candidate` でフロア不合格の strategy が **approval 行を作る** (警告のみ)。payload に `floor_warning == "unprofitable"` と `floor_detail` (全数値) が入る。変異: bless も raise にする → killer
- F6-3 `bless_candidate` が `activity` に `bless_floor_warning` を書く (`activity=None` でも例外を出さない)
- F6-4 `afx plugin bless --from _human` の終了コードが 0、stderr に警告が出る
- F6-5 `approval.submit_plugin` (legacy corridor) で in_sample フロア不合格が `ValueError`。変異: legacy corridor を素通しにする → killer
- F6-6 legacy corridor は holdout を回さない (既存挙動の pin — フロア追加で holdout を呼び始めないこと)

### F7 T2

- F7-1 `submission_blocked` が in_sample 段不合格のときだけ現れる。変異: 常に付ける / 付けない → killer
- F7-2 判定が T1 と**同じ関数**を呼ぶ。変異: RPC 側に閾値をハードコード → `min_pf` を変えたテストが落ちる killer
- F7-3 プロンプト文言 pin: `improve_mission.md` に `pf >= 1.0` と `unprofitable` の語が含まれる

### F8 T3 config

- F8-1 既定値 pin: `min_pf == 1.0` / `require_positive_avg_r is True` / `require_holdout_evaluable is False`
- F8-2 `settings.yaml.example` と `ImproveGateSettings` のキー集合一致
- F8-3 `min_pf` に負値 → `ValidationError` (`ge=0.0`)
- F8-4 フロア不合格のレポート本文と `activity` の `gate_failed` 行に**適用した閾値**が入る (`settings_snapshot_hash` が `improve.gate` を含まないため唯一の監査痕跡)。変異: 閾値を書かない → killer

### 段 0 (指揮者の変異スイープ) の最優先 4 件

F1-4 (`pf is None`) / F1-8 (in_sample 段で holdout を回さない) / F2-4 (holdout `evaluable=false` を通す) / F5-2 (ラベルの holdout 漏洩)。

---

## 9. 残る未決

| # | 内容 | 状態 |
|---|---|---|
| — | U1〜U5 | **裁定済** (§2) |
| R1 | `approval.submit_plugin` (legacy corridor) は共有ゲートを通らず holdout も回さない。本束では in_sample 段のみを課す。この corridor を `evaluate_strategy_adoption_gate` に統合するか廃止するかは別起票候補 | 起票候補 (指揮者判断) |
| R2 | baseline 再生の未実装 [baseline-replay-unimplemented] | 別起票 (U4 裁定、指揮者が tickets に記載) |
| R3 | `settings_snapshot_hash` が `improve.gate` を含まない構造 (本束はレポート/activity で代替) | 記録のみ |

---

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案 + ユーザー裁定反映。T0 (reject reason 漏洩) / T1 (収益性フロア 2 段: in_sample + holdout、ラベルは `unprofitable` 固定) / T2 (RPC ヒント + プロンプト、in_sample のみ) / T3 (config 3 キー) の 4 task。U1 = holdout も門に (固定文言 1 bit は許容、遮断 8 にただし書き追記) / U2 = submit は raise・bless は警告のみ / U3 = DD・latches は門にせずレポートに出す / U4 = baseline 再生は別起票 / U5 = 除染はユーザー実行。指揮者既定 = holdout は `evaluable=true` のときだけ判定 (`require_holdout_evaluable=False`) | 実機観測 `tmp/a4-run16-codex-20260912.md` / `tmp/approval-20260912b.md` (approval #10 = mission 80)、実 DB read-only 調査 (`backtest_runs` 35 行 / `approval_requests` 10 件 / `improvement_backlog` 64 行)、ユーザー裁定 2026-09-12 | - |

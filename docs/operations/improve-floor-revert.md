# 改善候補の収益性フロアを戻す

この手順は、改善候補の採用判定を次の基準へ戻す運用です。実行は daemon を動かしている repository root で行います。

```text
min_pf: 1.0
require_positive_avg_r: true
require_holdout_evaluable: true
```

`improve.backend = codex` にする案 A のもう一つの一時設定は、この手順の対象外です。必要になったときに別途決めます。

## 0. 保存済み承認を読む

設定・DB・配備を変えずに、承認時の payload を新基準で表示します。省略時は `approved` の全件を対象にします。

```bash
.venv/bin/python scripts/reevaluate_approvals.py --db data/agentic.db
.venv/bin/python scripts/reevaluate_approvals.py --db data/agentic.db --ids 21,24,25
```

表の列は、承認 id、戦略名と版（content hash の先頭）、in-sample の pf / avg_r、holdout の pf / 取引数、判定です。`PF 不足` は pf または avg_r が基準外、`holdout 評価不能` は holdout が30取引未満、`両方` は両方です。値を保存していない古い payload は `評価不能 (値なし)` と表示します。この表示は情報提示だけで、承認や配備を変更しません。

#21、#24、#25 は各行について人間が個別に keep または disable を決めます。自動で解除してはいけません。

- keep: 操作はせず、文書末尾の実施記録に決定を書きます。
- disable: 対象名を確認してから実行します。

  ```bash
  afx plugin disable <name> --revoke
  ```

## 1. 3値を一組で変更する

`config/settings.yaml` の `improve.gate` を、3値すべて同時に次の形へ変更します。1値だけを変更しません。

```diff
 improve:
   gate:
     min_pf: 1.0
     require_positive_avg_r: true
-    require_holdout_evaluable: false
+    require_holdout_evaluable: true
```

## 2. 設定を検証する

```bash
afx check
```

fatal が出た場合は再起動せず、設定を直してから同じ確認をやり直します。

## 3. daemon を再起動する

```bash
systemctl --user restart afx
systemctl --user status afx --no-pager
```

## 4. 稼働中設定の確認面

現在の実装には、`min_pf`、`require_positive_avg_r`、`require_holdout_evaluable` を daemon の active snapshot として表示する `afx ctl` コマンドも、起動 journal の記録行もありません。`afx ctl status` はこの3値を表示しません。

したがって、この3値だけを daemon 内部から直接確認する面は未提供です。ここで `config/settings.yaml` の再読だけを active snapshot の確認として扱ってはいけません。再起動の成否を確認し、次の承認 payload を実効確認面として確認します。

```bash
systemctl --user status afx --no-pager
afx ctl status
```

## 5. 次の承認 payload を確認する

次に作られた承認を一覧から選び、詳細の `profitability_floor` を確認します。

```bash
afx ctl approvals list
afx ctl approval <id>
```

詳細に次の3値が保存されていることを確認します。

```text
min_pf=1.0
require_positive_avg_r=true
require_holdout_evaluable=true
```

## 6. 元へ戻す

戻す必要があるときは、手順 1 で3値を一組として戻し、手順 2〜5 を同じ順で行います。案 A に戻す値は次です。

```text
min_pf: 1.0
require_positive_avg_r: true
require_holdout_evaluable: false
```

## 挙動の確認

`require_holdout_evaluable=true` で holdout が評価不能の場合、内部の床判定は `unprofitable` になります。改善 agent に見える結果は、取引数不足や比較不能を含む非提出と同じ中立な `not_submitted` に畳まれます。holdout 評価不能が収益性不良を意味するわけではないため、agent 面の語だけで理由を判断せず、この手順の表と承認詳細を人間が確認します。

## 実施記録

| 日時 | 承認 id | 戦略名と版 | keep / disable | 実施者 | 根拠・補足 |
|---|---:|---|---|---|---|
| 2026-10-22 | 21 | sma_cross_10_30@9d8f8336 | disable | 利用者 | holdout 48 取引で pf 0.76。評価可能な標本で新基準を下回る |
| 2026-10-22 | 24 | rsi_bb_reversion_200@a8fe63c3 | keep (期限付き) | 利用者 | holdout 4 取引で評価不能 (不合格ではなく材料不足)。RSI+BB 族の更新先を 1 つ残す。期限 = 次の観測期の終わり、または最初の同名更新まで。期限内に新基準を通る同名更新が出なければ再裁定する |
| 2026-10-22 | 25 | rsi6_bb200_rr2@17fc289f | disable | 利用者 | holdout 4 取引で評価不能。#24 と同じ仮説族 (主差分は SL/TP) で、両方残すと同族候補の誘導先が名前順に依存する |
| 2026-10-22 | 23 | bb_ema_breakout@e92668d5 | keep (裁定対象外) | 指揮者 | 新基準に合格 (holdout pf 1.0004 / 45 取引)。結果を見た後に安全余裕を足す後付け基準は作らない。境界値として次回の再評価で見る |

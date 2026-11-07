# 履歴の修復のあとに、配備中の strategy を再評価する

`afx history repair-time` などで履歴が修復されると、それより前の履歴で評価・承認された配備中の strategy は、承認の根拠 (in_sample / holdout) が古いままになります。この手順は、今の履歴で評価し直し、配備を続けるか止めるかを人間が決める運用です。実行は daemon を動かしている repository root で行います。daemon は動かしたままでよく、評価の間も配備は変わりません。

再評価は改善の試行には数えません。評価の規則 (期間・収益性の床・gate) は変わらず、修復前の記録 (`backtest_runs` と承認済みの行) は書き換えません。indicator は対象外です。

## 1. 依頼を作る

```bash
afx plugin reevaluate --all-deployed
```

配備中の strategy を名前順に評価し、承認の依頼を 1 本ずつ作ります。1 本につき数分かかります。確認の問いに答えるか、`--yes` を付けます。1 本だけ評価するなら `afx plugin reevaluate <name>` です。

## 2. 結果表を読む

1 行は `名前  状態  #承認 id  (理由)  説明` です。

| 状態 | 意味 | することは |
|---|---|---|
| `created` | 承認の依頼を作った | 3. へ |
| `existing` | 同じ評価の依頼が既にある (2 回目の実行など) | 3. へ。評価はやり直していない |
| `unevaluable` | 評価できず、依頼は作っていない (理由は括弧の中) | 5. へ |
| `failed` | 評価が失敗した。他の strategy は止まらない | もう一度実行する。続くときは `afx ctl log` を読む |
| `changed_during_evaluation` | 評価中に配備・履歴が変わったため何も書いていない | もう一度実行する |
| `skipped` | 配備中の strategy でない (indicator、旧式の plugin、承認の無い entry など) | `--all-deployed` では何もしなくてよい |

終了コードは、`created` と `existing` 以外が 1 本でもあれば非 0 です。`--all-deployed` では、配備中の strategy でないことによる `skipped` (`not_strategy` `plain_plugin` `not_approved` `not_deployed`) は数えません。名前で指したときは数えます。

## 3. 承認の中身を見る

```bash
afx ctl approval list
afx ctl approval show <id>
```

見出しは `再評価 (前回の承認 #<id>)` です。次を確かめます。

- コード変更はありません (配備中の版そのものの評価です)。
- pair ごとの in_sample / holdout が、前回 → 今回で並びます (pf / trades / avg_r / max_drawdown)。前回の値には出所が付き、承認時の記録、`run #<id>`、不明のどれかです。不明は、前回の値を特定できなかったことを表します。
- 評価時点は、前回の承認日時と今回の評価時点が利用者の表示 tz で出ます。期間が違う比較なので、値の差はそのまま優劣ではありません。

web の承認ページと Discord の承認カードにも同じ内容が出ます。

## 4. 承認か却下か

- **承認** (`afx ctl approve <id>`): 承認の根拠だけを新しい行に差し替えます。配備 (symlink・版・journal) は変わりません。同名の別候補の承認待ちには影響しません。
- **却下** (`afx ctl reject <id>`): **その strategy の配備を停止します**。`afx plugin disable` と同じ処理で、新しい signal と OPEN が止まり、待機中の signal は取り下げられ、symlink が外れ、`plugin_disabled` が通知されます。shadow の観測と open position の保護 (SL・決済) は続き、強制決済はしません。確認の文にも同じことが出ます。

却下の結果は次のどれかで返ります。

| 結果の文言 | 意味 | することは |
|---|---|---|
| 却下し、配備を停止しました | 完了 | なし |
| 却下しました。取引は止まりましたが symlink を外せていません | 外す操作が失敗 | `afx plugin disable <name>` で完了する |
| 却下しました。取引は止まりましたが plugins/<name> が想定と違うため外していません | live が評価した版と違う | `plugins/<name>` の状態を確認する |
| 依頼が古いため無効にしました。配備は変えていません | 依頼のあとに切替・取消・停止などがあった | 再評価をやり直す。配備を止めるなら `afx plugin disable <name>` |
| 履歴が変わったため決定していません | 依頼のあとに履歴が修復された | 再評価をやり直す。配備を止めるなら `afx plugin disable <name>` |

未完の切替 journal や配備操作があると、却下は止まって pending のままです。その場合も、配備を止めるなら `afx plugin disable <name>` を使います。

## 5. `unevaluable` の行

評価できない版 (データセット未設定、参照履歴の不足、取引数の不足など) には依頼を作りません。古い根拠のまま配備が続くので、行に並ぶ `止めるには: afx plugin disable <name>` で止めるかどうかを人間が決めます。取引数の不足のように評価の行が残るものは、履歴が増えてからもう一度実行します。この扱いは、実際に評価不能が出たときに裁定します。

## 6. 再評価のやり直し

依頼は、履歴の epoch・最新の決定・配備世代が評価時と同じ間だけ有効です。変わった依頼は承認も却下も「古い」として無効になるので、もう一度 `afx plugin reevaluate --all-deployed` を実行します。変わっていない依頼は `existing` と表示され、評価はやり直しません。

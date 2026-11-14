# 評価用履歴の import (`afx history import`) の運用

評価用履歴 (`ohlcv_history`) を MT5 または Dukascopy から取り込む手順です。契約の正本は `docs/superpowers/specs/2026-11-14-history-status-and-import-conflicts-design.md` (C2.2) です。履歴の穴埋めのあとに承認を再評価する手順は `docs/operations/reevaluate-deployed-after-history-repair.md` §7 にあります。

## 1. 配備の順序: 再起動してから import する

新しい衝突の記録 (`history_conflicts`) と共通の import 経路は、コードを更新して **daemon を再起動した後**に使います。

1. コードを更新し、`afx ctl` で daemon を再起動する。起動時に `history_conflicts` 表が作られる (既存 DB への追加は冪等で、再起動を重ねても変わらない)。
2. `afx status` の `evaluation history` で状態を確かめる。
3. daemon を止め、開場中に `afx history import ...` を実行する。
4. 終わったら daemon を戻す。

旧い daemon が動いたまま、新しい import だけを先に走らせてはいけません。取込と time repair は、DB と同じ directory の `history-mutation.lock` で互いに待たずに排他され、取れなければ `database_busy` で何も書かずに終わります。この lock は履歴を書く処理どうしだけを排他し、daemon の通常の取引 DB 書込みは止めません。

## 2. 終了コードと結果の読み方

| rc | 結果 | 意味 |
|---:|---|---|
| 0 | `ok` | 範囲の処理が完了し、衝突はない |
| 1 | `partial` / `failed` | 取得・DB などの失敗。`partial` は一部の batch だけ確定済み、`failed` は確定 0 件 |
| 2 | 引数不正 | 何も書いていない |
| 3 | `completed_with_conflicts` | 完走した。既存値と違う入力は既存値を残したまま `history_conflicts` に記録した |
| 4 | `conflict_circuit_open` | 遮断器が作動した。その系列は履歴・衝突記録とも 0 件 |

複数の系列があるときの rc は、技術失敗 (1) が遮断 (4) より、遮断が衝突 (3) より優先されます。

### Dukascopy の rc の変化と取得の原子性

- 衝突を含んで完走したとき、Dukascopy の rc は従来の **0 から 3** に変わりました (MT5 と同じ共通の経路です)。rc を見て 0 を成功と決めているスクリプトは、3 を「完走・衝突あり」として扱うよう直します。
- 取得・復号・検証のどこかが失敗したとき、その系列は **0 件** になります (旧: 取得できた hour までを部分的に確定)。1 系列の全時間を取得・検証し終えてから書くためです。失敗は `reason=throttled` (429/503) や `fetch_failed` などの固定語で表示されます。

## 3. 失敗したとき: 再実行

- `failed`: 何も書いていません。元の `--from` から取り直す。
- `partial`: 確定済みの batch は残っています。`remaining=[...)` が未処理の範囲ですが、**元の同じ範囲を最初から再実行**します。確定済みの行は unchanged、既知の衝突は再観測として数えるため、重複して入りません。
- `time_rule_unverified`: bridge が時刻規則を検査できていません (閉場中の 503 など)。開場中にもう一度実行する。
- `database_busy`: 別の履歴 writer (time repair など) が動いている。終わってから再実行する。
- `stage_expired`: 取得してから書き始めるまでが長すぎた。再実行する。

遮断 (rc 4) の再実行は別です。次節を読んでからにします。

## 4. 遮断器の閾値の読み方

遮断の表示は次の行で出ます。

```
conflict_circuit_open source=... symbol=... interval=... N_overlap=<n> N_test=<n> C_new=<n> C_known=<n> new_ratio=<%> threshold=1% (...) omitted=<n> persisted=false
```

- `N_overlap`: 既存と重なった本数 (`unchanged + C_new + C_known`)。
- `N_test`: 遮断の分母。**`unchanged + C_new`**。既知の異値 (`C_known`) は入れません。
- `C_new`: 新規に見つかった異値。既知の異値を再観測しただけのものは `C_known` で、件数遮断にも割合にも数えません。
- 遮断の規則: **`C_new > 20` または (`C_new >= 2` かつ `C_new` が `N_test` の 1% を超える)**。1% ちょうどは通ります。`C_new` が 1 件だけの孤立した衝突は割合に掛けず、記録して続行します。`N_test=0` の割合は「算出不能」と出ます。
- `persisted=false` は、この系列の履歴にも衝突記録にも書いていないという意味です。表示は先頭 5 件で、残りは `omitted`。
- 遮断は強制続行できません。閾値は設計値で、実分布から推定した統計閾値ではありません。

遮断を見たら、系統的なずれ (時刻規則・取得元の差・broker 側の改訂) を疑います。bridge から同じ範囲を取り直して値を比べ、原因を確かめます。遮断を避けるために範囲を分けて再実行しても、日窓ごとに分母を薄めることはできません。

## 5. 実測 (合成 fixture、本番 DB は使わない)

合成 DB (一時 directory、WAL、1 分足の 1 系列に 10 万行を投入済み) で、共通 core の分類と batch 確定を測りました。同じ測定を 3 回行い、ばらつきは 1 ms 未満でした。他の作業が同じ機械で動いている状態の値で、scheduler の並走・実ディスクの fsync・最大構成 (複数 pair × 足) は含みません。受入の性能試験 (scheduler writer との並走) の代わりにはなりません。

| 操作 | 値 |
|---|---|
| 5,000 行の新規追記 (write lock の保持時間 = BEGIN から commit まで) | 約 9 ms |
| 5,000 行の重複 (全て unchanged) を確定 | 約 13 ms |
| 5,000 行の重複、うち 20 件が異値の確定 (衝突記録の upsert を含む) | 約 13 ms |
| 5,000 行の分類 (既存との照合、write lock なし) | 約 27〜37 ms |
| 120 行の重複が全て異値の分類 (遮断) | 約 2 ms |

分類の結果の例: 5,000 行の重複で `N_overlap=5000 N_test=5000 C_new=0` (通過)。5,000 行の重複のうち 20 件が異値だと `C_new=20 N_test=5000` (件数 20 は超えず、割合 0.4% で通過)。直近 120 本の全てが異値だと `N_test=120 C_new=120` で遮断されます。

batch の目標は 50 ms 以内です。測定値はその範囲に収まりましたが、実機 (本番 DB の規模、daemon の書込みとの並走) では別に測ります。結果は人間専用の配備記録に残し、未測の最大時間を「短い」とだけ書かないでください。

## 6. 公開する変更の要約

- 衝突は上書きせず記録し、非衝突行の確定を続ける (MT5・Dukascopy 共通)。
- Dukascopy は衝突を含む完走で rc 3、取得失敗は当該系列 0 件。
- 系統的なずれは遮断器で系列ごとに 0 件、rc 4。
- 部分確定したら元の範囲を最初から再実行する。

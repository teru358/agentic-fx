# Kill switch 評価

## 不一致裁定

## 凍結母数

## 三分類

## 初回結果

## 履歴再現 a

## 履歴再現 b

## 履歴再現 c

## 初回測定 (run2、正解表の凍結直後)

- 正解表: `oracle/kill-switch-a.json` (A、188 行) と `oracle/kill-switch-b.json` (B、138 行、repo 外の隔離 checkout で作成) を第三者の裁定案と指揮者の抜取り照合で確定し `oracle/kill-switch-frozen.json` (221 行、critical 161、G_lit 105 / G_reach 116) に凍結した。sha256: A `798c080f…`、B `49d1d2e5…`、frozen `ae6911f0…`。採用前に確定した裁定: guard_version は評価用履歴の guard の版で kill switch と無関係のため母数から外し頁の resource からも外した / 採用比較 metric の表示行は critical にしない / event の読み手は producer 判定に使うものだけ critical / daemon の Notifier は critical にしない。
- 機械セル (G_lit critical): 母数 78、TP 37、FN 41、U 0。FN の主因は抽出器の実装が設計と食い違っていたこと — module 定数を `<module>.NAME` で記録していない (§2.2)、SQL・JSON path の文字列中の table 名・key を照合していない (§2.3)、dataclass の field 宣言を拾っていない。
- 頁セル (G_lit ∪ G_reach critical): 母数 161、TP 64、FN 97、U 0。頁の被覆宣言 106 件に対し正解表 221 行。
- 判定: **初回は不合格** (§4.3 の critical FN=0 を満たさない)。抽出器を設計の形へ合わせ、頁を手動補完して再測定する。再測定の点はこの初回の点と並べて記録する。
- 実測: extract 367 file / 5.9 MB / 0.82 秒 (一回)。

## 正解表の訂正 (run4 の後)

- `src/agentic_fx/store/state.py::StateStore._marker` × `marker:reset_marker` の class を G_lit → G_reach に訂正。理由: この symbol は literal を持たず定数名 `_MARKER_SUFFIX` を参照するだけで、literal は `<module>._MARKER_SUFFIX` 側にある (G_lit の定義「宣言 literal を持つ site」に当たらない)。出力を見た後の訂正なので記録する。役割: 指揮者。frozen sha256: `ae6911f0f7b3…` → `b929322136ed…`。
- 訂正前の点 (run4): 機械セル critical 母数 78 / TP 77 / FN 1 / U 0、頁セル critical 母数 161 / TP 64 / FN 97 / U 0。

## 抽出器の是正の経過 (設計の形に合わせたもの、正解表は見せていない)

| run | 変更 | 機械 critical FN | 頁 critical FN |
|---|---|---|---|
| run2 | 初回 | 41 / 78 | 97 / 161 |
| run3 | module 定数 `<module>.NAME`、文字列中の識別子境界の一致、dataclass field、marker literal の明示 | 4 / 78 | 97 / 161 |
| run4 | class 定数 `Class.NAME`、正解 symbol の入れ子を同じ symbol として数える、marker literal を suffix 文字列に | 1 / 78 | 97 / 161 |

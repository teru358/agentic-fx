---
id: improve-backtest-untradable-pair-and-skip-readability
title: 改善の backtest が取引できない pair を黙って 0 取引にし、参照の除外の読み方も伝わらない
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-08
closed: 2026-11-09
related: [backtest-worker-cpu-budget-shrinks-with-timeframe, improve-run-summary-for-humans]
---
# [improve-backtest-untradable-pair-and-skip-readability] 改善の backtest が取引できない pair を黙って 0 取引にし、参照の除外の読み方も伝わらない

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

2026-11-08 の手動の改善 #3377・#3382 (EURUSD 主軸 + US500 参照) が 0 取引・評価不能になり、agent は参照系列の欠損が原因と申告した。実際は EURUSD が settings.pairs に無く、open の意図がすべて not_tradable_symbol で却下されていた (参照で除外は 1.2%)。run_backtest の応答に却下理由も評価した足数も出ないため、agent が誤って帰属する。参照の除外自体は正しい挙動 (参照市場の休場・warmup) だが、USDJPY 主軸では参照の履歴が無い先頭 952 足 (12.2%) が評価区間に入っている。調査: tmp/inv-refgap/report.md

## 原因

## 処置案・裁定

2026-11-09 利用者承認 (部分 A から着手、B・C は様子を見てから設計)。
- A (この束): `run_backtest` の入口で `settings.pairs` に無い pair を backtest を走らせずに返す (`error: pair_not_tradable`、hint に取引できる pair の一覧と「他の symbol は参照 (symbol:) としてのみ使える」)。既存の `pair_not_declared_by_plugin` と同じ作り。改善 mission の prompt に「候補の pairs に書けるのは取引 pair だけ」を足す。gate の理由語は足さない。
- B (後で設計): 応答に評価した足の数・除外の割合と意味・0 取引の却下理由の内訳 (遮断 7 との関係を確認)。
- C (後で設計): 評価区間の始点を参照系列の履歴に合わせる (遮断 1、比較可能性)。

## 修正内容

- 2026-11-09: A (取引できない pair を run_backtest の入口と gate の前で pair_not_tradable で止め、prompt に取引 pair を明記) と B1 (prompt に skipped_bars_by_reason の 4 語の判定条件と、除外・提案なし・前提不足・未決済を区別する一文を足し、段落を test で固定) を入れた。B2 (却下件数の公開) は評価回数のカウンタになり境界の探索に使えるため見送り、B3 (足数・割合を出さない) を維持、C (評価区間の始点) は improve-eval-window-reference-availability に分けた。設計 spec 2026-11-09-improve-backtest-skip-readability-design.md (C1、sol 1 周)、実装レビュー sol 1 周 (Important 1・Minor 1 是正)、判定 tests/loops ほか 2613 緑

## 経緯

- 2026-11-08: 起票。
- 2026-11-09: 状態: 実装中 — 部分 A に着手 (処置案に記載)
- 2026-11-09: 状態: 設計待ち — 部分 A を main に merge: run_backtest の入口で settings.pairs 外の pair を pair_not_tradable と hint で返す (backtest を走らせない)、宣言した pairs に取引できない pair を含む strategy の候補を契約違反 pair_not_tradable で gate の評価の前に落とす (通知の理由語を 1 つ足した)、prompt に取引 pair を明記。レビュー codex sol 2 周 (Important 1 是正、Minor 2 是正)。残り: B (応答で除外の読み方を伝える) と C (評価区間の始点を参照系列の履歴に合わせる) は様子を見てから設計
- 2026-11-09: 状態: 実装中 — B・C の設計 C1 (spec 2026-11-09-improve-backtest-skip-readability-design.md、sol 1 周、利用者承認): B1 (prompt に除外の 4 語の判定条件と共通の一文) を実装、B2 (却下件数を応答に出す) は評価回数のカウンタになり境界の探索に使えるため見送り、B3 (足数・割合を出さない) を維持、C は improve-eval-window-reference-availability に分ける
- 2026-11-09: 是正内容 — A (取引できない pair を run_backtest の入口と gate の前で pair_not_tradable で止め、prompt に取引 pair を明記) と B1 (prompt に skipped_bars_by_reason の 4 語の判定条件と、除外・提案なし・前提不足・未決済を区別する一文を足し、段落を test で固定) を入れた。B2 (却下件数の公開) は評価回数のカウンタになり境界の探索に使えるため見送り、B3 (足数・割合を出さない) を維持、C (評価区間の始点) は improve-eval-window-reference-availability に分けた。設計 spec 2026-11-09-improve-backtest-skip-readability-design.md (C1、sol 1 周)、実装レビュー sol 1 周 (Important 1・Minor 1 是正)、判定 tests/loops ほか 2613 緑

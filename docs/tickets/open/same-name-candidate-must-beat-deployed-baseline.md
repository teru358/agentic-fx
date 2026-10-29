---
id: same-name-candidate-must-beat-deployed-baseline
title: 同名の戦略候補は配備中の版と同条件で backtest し、改善していなければ gate で却下する
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-29
closed: null
related: [weekly-improve-rebacktests-deployed-strategies, improve-objective-filtered-signal-quality, plugin-naming-lifecycle-policy]
---
# [same-name-candidate-must-beat-deployed-baseline] 同名の戦略候補は配備中の版と同条件で backtest し、改善していなければ gate で却下する

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

配備中の同名戦略がある候補 (更新) でも、gate は絶対の床 (PF >= 1.0、avg_r > 0、取引数、holdout 評価可能) しか見ず、配備中の版との比較はしない (strategy_gate.py は baseline の参照を payload に付けるだけ、承認画面はコード差分のみ)。全体設計 2026-07-25 §441 の「baseline との比較値を承認依頼に添付」は未実装で、収益性の床の設計 (2026-09-12) が「相対比較の不在」と記録。方針 (ユーザー 2026-10-29): 候補と配備中の版を同じ期間・同じデータ源・同じ足で backtest し、結果が改善していなければ gate で却下する (人間承認に回さない)。設計で決めること: (a) 改善の定義 (holdout の PF / avg_r / 取引数のどれを、どれだけの差で、in_sample も見るか) = ユーザー裁定、(b) 配備中の版の backtest は候補と同じ mission 内で gate が回す (保存済みの古い成績とは比べない)、(c) 配備中の版が評価不能 (取引数不足など) のときの扱い、(d) 却下の固定 reason と activity・Discord 通知、backlog の last_result、(e) backtest 予算 (候補ごと最大回数) への影響、(f) 再ロック (pin だけの更新) は比較の対象外、(g) 新名の候補 (同名なし) は従来どおり床のみ。同名更新 = 同じ仮説の改良 (plugin-naming-lifecycle-policy) と整合。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-29: 起票。

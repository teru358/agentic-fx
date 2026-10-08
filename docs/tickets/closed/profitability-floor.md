---
id: profitability-floor
status: 是正済
priority: 高
opened: 2026-09-13
closed: 2026-09-12
related: []
backfilled: true
source_section: 未完了
---
# [profitability-floor]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

[profitability-floor] **完結 cd19457 (2026-09-13、spec v1.4 / plan v1.9、実機未観測)** — 旧: (高、2026-09-12 ユーザー承認、順序 = test-hygiene の後・backtest-dedup-cache の前)** gate に PF 門が無く `evaluable` は in_sample trades≥30 のみ、holdout の戻り値は未使用 (opus 実機観測) → 不採算候補 (#10: pf 0.713) が承認キューに並ぶ。設計 = ① 決定論フロア: in_sample `pf<1.0` or `avg_r<=0` or holdout `evaluable=false` → approval を作らず observation 再ルート (`unprofitable:pf=… avg_r=…`、台帳/archive は残す、§A 再ルート経路を流用) ② プロンプト: pf<1/avg_r<0 なら予算内でパラメータ・フィルタを変えて再 backtest、改善しなければ提出せず observation で試行内容を残す ③ config `improve.gate.min_pf` (既定 1.0) / `require_positive_avg_r`。前提確認: holdout evaluable=false で approval が作られた経路 (insufficient_trades が in_sample 限定か)。spec/plan は docs/superpowers に起こす

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


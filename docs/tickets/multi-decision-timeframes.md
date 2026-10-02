---
id: multi-decision-timeframes
title: 判断足を複数持つ構想 (将来拡張)
status: 設計待ち
priority: 低
opened: 2026-09-21
closed: null
related: [multi-timeframe-indicator-deps]
backfilled: true
source_section: 完了ログ
---
# [multi-decision-timeframes]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[multi-decision-timeframes]** (2026-09-21 ユーザー構想、着手は優先 ticket の後 = 初回設定ウィザード前後が目安): 基準 tick を最小足にし、各 tick で確定した足の集合ごとに発注可否を判断、建玉は足ごとの帳簿で管理する。設計項目: 帳簿ごとのリスク枠と合計上限 (足を増やすと黙ってリスクが増えないこと) / **両建て可否は設定項目に利用者が記述し責任は利用者が持つ (2026-09-21 裁定。既定は不可、口座の margin mode は食い違い警告にだけ使う)** / 同時に確定した複数足を mission 1 本で判断するか足ごとに分けるか / 既存 `horizon` (day/swing) との整理。前提: A2 束 B が `decision_timeframes` (list、当面 1 要素に制限) と足タグを集合前提で入れておく (`tmp/design-a2/RESUME.md`)。strategy の複数足指標 [multi-timeframe-indicator-deps] とは別物。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 判断足を複数持つ構想 (将来拡張) — 仕分け (2026-10-02、現物で成立を確認): 足ごとの帳簿とリスク枠、両建て可否の設計が未着手。初回設定ウィザード前後に着手。 根拠: decision_timeframes の複数足対応は未着手 (2026-09-21 構想、優先 ticket の後)。

---
id: plugin-locks-accumulate
status: 是正済
priority: 低
opened: 2026-09-12
closed: 2026-09-12
related: [sweep-empty-dirs]
backfilled: true
source_section: 未完了
---
# [plugin-locks-accumulate]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[plugin-locks-accumulate] **是正済 test-hygiene T2 (2026-09-12、startup sweep ⑧、flock 確認付き、name 正規形検証)** — 旧: (低) `plugins/.locks/<name>.lock` が名前ごとに蓄積 (reject 止まりの `rsi_indicator_21.lock` も残存)、sweep に掃除経路なし。[sweep-empty-dirs] と束で (再発 2026-09-12: reject 4 件で 3 → 7 件、`tmp/approval-20260912.md`)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


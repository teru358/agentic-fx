---
id: abandoned-signal-observability
status: 設計待ち
priority: 未設定
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [abandoned-signal-observability]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[abandoned-signal-observability] 起票 2026-09-27 (B-2 論点、観測待ち)**: 建玉ゼロの間は signal 起動 mission を起こさない規則 (D2) のため、strategy signal が claim されず abandoned で終端し得る (2026-09-24 に初観測)。段 c で cron mission が最古 pending を購読するようになったので、9/28 開場観測で abandoned が減るか・残る場合の activity の読みやすさを確認する

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


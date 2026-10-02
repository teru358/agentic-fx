---
id: abandoned-signal-observability
title: 建玉ゼロ中の signal が abandoned で終わる件の観測
status: 設計待ち
priority: 低
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [abandoned-signal-observability]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[abandoned-signal-observability] 起票 2026-09-27 (B-2 論点、観測待ち)**: 建玉ゼロの間は signal 起動 mission を起こさない規則 (D2) のため、strategy signal が claim されず abandoned で終端し得る (2026-09-24 に初観測)。段 c で cron mission が最古 pending を購読するようになったので、9/28 開場観測で abandoned が減るか・残る場合の activity の読みやすさを確認する

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 建玉ゼロ中の signal が abandoned で終わる件の観測 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): cron mission が最古 pending を購読するようになり、減るかを観測待ち。 根拠: 9/28 開場後の観測結果が必要。abandoned の発生数と activity の読みやすさを実機で見る。

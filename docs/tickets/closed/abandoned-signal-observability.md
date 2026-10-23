---
id: abandoned-signal-observability
title: 建玉ゼロ中の signal が abandoned で終わる件の観測
status: 是正済
priority: 低
opened: 2026-09-27
closed: 2026-10-23
related: []
backfilled: true
source_section: 未完了
---
# [abandoned-signal-observability]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[abandoned-signal-observability] 起票 2026-09-27 (B-2 論点、観測待ち)**: 建玉ゼロの間は signal 起動 mission を起こさない規則 (D2) のため、strategy signal が claim されず abandoned で終端し得る (2026-09-24 に初観測)。段 c で cron mission が最古 pending を購読するようになったので、9/28 開場観測で abandoned が減るか・残る場合の activity の読みやすさを確認する

## 修正内容

- 2026-10-23: 観測完了: cron mission が最古 pending を購読する形 (9/28) 以降、実 DB で 2026-10 の signals は consumed 38 / abandoned 0。trade-cron-hybrid-mode で挙動を変えるときはその設計内で再観測

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 建玉ゼロ中の signal が abandoned で終わる件の観測 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): cron mission が最古 pending を購読するようになり、減るかを観測待ち。 根拠: 9/28 開場後の観測結果が必要。abandoned の発生数と activity の読みやすさを実機で見る。
- 2026-10-23: 是正内容 — 観測完了: cron mission が最古 pending を購読する形 (9/28) 以降、実 DB で 2026-10 の signals は consumed 38 / abandoned 0。trade-cron-hybrid-mode で挙動を変えるときはその設計内で再観測

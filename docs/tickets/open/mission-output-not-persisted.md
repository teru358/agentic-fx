---
id: mission-output-not-persisted
title: mission の出力と transcript が DB に残らない疑い
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [mission-output-not-persisted]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[mission-output-not-persisted] missions.output_json NULL / transcript_json `[]` (m34〜m42 全部) — 既存の観測性負債

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: mission の出力と transcript が DB に残らない疑い — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 現行コードは終端時に書く。最近の mission 行を見れば決まる。 根拠: missions.py:39 の finish は output_json と transcript_json を書く。m34〜m42 の NULL が今も出るかは DB 実測が要る。

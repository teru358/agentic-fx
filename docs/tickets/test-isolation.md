---
id: test-isolation
title: 特定 4 ファイルの部分実行で loops の fixture が見えなくなる
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [test-isolation]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[test-isolation] tests/loops/conftest.py の loop_min/loop_full が特定 4 ファイル同時の部分実行で不可視化し 18 本消える (全スイートでは発生せず)。verified-local-round1.md (束 F) 付録参照。原因未特定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 特定 4 ファイルの部分実行で loops の fixture が見えなくなる — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 全スイートでは発生せず、原因未特定のまま。 根拠: tests/loops/conftest.py:58,63 に loop_min/loop_full、4 ファイル部分実行での消失は再現手順が本文に無く未確認。部分実行で再現すれば原因が決まる。

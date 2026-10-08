---
id: legacy-d-stage0-e
title: switch の resume 経路が symlink 版ディレクトリを resolve せず受理
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-d-stage0-e]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[D-stage0→束E] switch.py resume 経路が .resolve() 無しで symlink 版ディレクトリを受理 (fresh 経路と非対称)。stage0-bundle-D.md 特例節参照。束 E 2 周目で是正

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: switch の resume 経路が symlink 版ディレクトリを resolve せず受理 — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): fresh 経路との非対称が残っているかは未確認。 根拠: switch.py:344 は .resolve() 付きだが、本文の resume 経路 (_reverify_switched_journal) との対応は個別に読まないと決まらない。

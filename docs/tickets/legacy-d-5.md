---
id: legacy-d-5
title: 旧 submit 回廊が strategy の gate を素通りする問題は是正済
status: 是正済
priority: 未設定
opened: null
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [legacy-d-5]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[D-5] afx plugin submit → submit_plugin → _validate_strategy の旧回廊が §8.1-41 fail closed を素通り (閾値未満でも approval 行が作れる)。検収 acceptance-task10.md (d) 項。Task 11 の 11d 系として是正要

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。legacy submit_plugin は strategy を受け付けず共通 gate 経由に限定 (7d7546b)。 根拠: 7d7546b。plugin/approval.py:412 submit_plugin が strategy を拒否する (旧回廊が gate を素通りする穴は塞がれた)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 旧 submit 回廊が strategy の gate を素通りする問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。legacy submit_plugin は strategy を受け付けず共通 gate 経由に限定 (7d7546b)。 根拠: 7d7546b。plugin/approval.py:412 submit_plugin が strategy を拒否する (旧回廊が gate を素通りする穴は塞がれた)。

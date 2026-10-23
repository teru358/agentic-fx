---
id: backlog-status-update-is-two-step-and-races-selection
title: backlog reject/reopen/note が SELECT 後の更新で選択と競合する
status: 是正済
priority: 中
opened: 2026-10-03
closed: 2026-10-23
related: []
---
# [backlog-status-update-is-two-step-and-races-selection] backlog reject/reopen/note が SELECT 後の更新で選択と競合する

**状態**: 是正済 / **優先**: 中

## 現象

commands.py:263-276 は状態を SELECT してから set_status する 2 段。間に改善 loop の select_for_mission が走ると、selected に変わった行を reject してしまう等の競合が起きる。

## 原因

## 処置案・裁定

状態の検査と更新を 1 文の条件付き UPDATE (WHERE id=? AND status IN (...)) にする。操作 API の T2 で実装し、シェルも同じ関数を使う。

## 修正内容

- 2026-10-23: store/backlog.py transition() が条件付き UPDATE (SELECT 後の更新ではない) になり、選択との競合が構造的に消えた

## 経緯

- 2026-10-03: 起票。
- 2026-10-23: 是正内容 — store/backlog.py transition() が条件付き UPDATE (SELECT 後の更新ではない) になり、選択との競合が構造的に消えた

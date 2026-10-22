---
id: ctl-status-shows-active-gate-floor
title: afx ctl status に稼働中の収益性フロア 3 値を出す
status: 設計待ち
priority: 低
opened: 2026-10-22
closed: null
related: []
---
# [ctl-status-shows-active-gate-floor] afx ctl status に稼働中の収益性フロア 3 値を出す

**状態**: 設計待ち / **優先**: 低

## 現象

docs/operations/improve-floor-revert.md 手順 4: daemon が起動時に読んだ improve.gate の min_pf / require_positive_avg_r / require_holdout_evaluable を daemon 内部から確認する面が無い (`afx ctl status` も起動 journal も出さない)。現状は再起動の成否と次の承認 payload の profitability_floor で代用している。status に 1 行足すだけでよい。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-22: 起票。

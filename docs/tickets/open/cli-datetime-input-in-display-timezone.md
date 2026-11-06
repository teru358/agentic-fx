---
id: cli-datetime-input-in-display-timezone
title: CLI の日時入力 (--from / --to) を表示 tz で解釈する
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-11-06
closed: null
related: []
---
# [cli-datetime-input-in-display-timezone] CLI の日時入力 (--from / --to) を表示 tz で解釈する

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

afx history の --from / --to は UTC として解釈し、明示した offset も捨てる (src/agentic_fx/backtest/cli.py:72-79)。利用者の原則「利用者が入力する日時は利用者の tz」(表示 tz 設計 2026-11-06 §0) と食い違う。表示 tz 設計では非目標として外した

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-06: 起票。

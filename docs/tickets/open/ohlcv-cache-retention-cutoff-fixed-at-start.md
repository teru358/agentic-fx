---
id: ohlcv-cache-retention-cutoff-fixed-at-start
title: cache の保持期間 cutoff が起動時刻で固定され再起動まで進まない
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-03
closed: null
related: []
---
# [ohlcv-cache-retention-cutoff-fixed-at-start] cache の保持期間 cutoff が起動時刻で固定され再起動まで進まない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

cache の保持期間の cutoff が daemon の起動時刻 − 保持日数で固定され、再起動まで進まない (src/agentic_fx/service.py:1650-1655、src/agentic_fx/store/ohlcv.py:62-75,204-218)。docstring「毎 tick 呼ぶことで日次増分に追いつく」と食い違う。誤り行が 1 か月残った原因。設計 docs/superpowers/specs/2026-11-02-utc-time-rules-dst-design.md の §0 参照

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-03: 起票。

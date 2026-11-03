---
id: time-repair-generalize-beyond-known-fingerprint
title: repair と cache 誤り行検出を他の利用者の履歴へ一般化する
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-03
closed: null
related: [market-hours-fixed-21utc-ignores-dst, mt5-bridge-applies-current-offset-to-all-history]
---
# [time-repair-generalize-beyond-known-fingerprint] repair と cache 誤り行検出を他の利用者の履歴へ一般化する

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

v1 の repair と cache の誤り行検出はこの DB の既知 fingerprint と定数の鍵集合に限る。他の利用者の +3 固定履歴は exit 3 で直らない。取り込み時期の記録か bridge との全量照合で一般化する案。設計 docs/superpowers/specs/2026-11-02-utc-time-rules-dst-design.md §3.4・§6

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-03: 起票。

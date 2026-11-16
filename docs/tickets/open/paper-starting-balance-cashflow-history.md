---
id: paper-starting-balance-cashflow-history
title: paper.starting_balance の変更が cashflow 履歴に残らない
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-15
closed: null
related: [killswitch-reset-hwm-rebase]
---
# [paper-starting-balance-cashflow-history] paper.starting_balance の変更が cashflow 履歴に残らない

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

paper.starting_balance を設定で変えても account_snapshots の cashflow に記録されず、過去にその変更が無かったことを証明できない。HWM 付け直しの台帳 (kill_switch_hwm_rebases) は rebase の累積だけを意味し、入出金調整済みの総損失を表さない。実口座 broker の導入前に入出金の正本とあわせて設計する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-15: 起票。

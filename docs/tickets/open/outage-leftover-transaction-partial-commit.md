---
id: outage-leftover-transaction-partial-commit
title: 停止判定の rollback 失敗で残った中途の書き込みが、次の足の保存で確定し得る
status: 設計待ち
priority: 低
opened: 2026-10-02
closed: null
related: [outage-stalled-on-broker-daily-rollover-gap]
---
# [outage-leftover-transaction-partial-commit] 停止判定の rollback 失敗で残った中途の書き込みが、次の足の保存で確定し得る

**状態**: 設計待ち / **優先**: 低

## 現象

停止判定が自分の transaction の rollback に失敗すると、中途の書き込み (state だけ・gap だけ等) が接続に残る。次の tick は停止判定より先に足の保存が同じ接続で commit するので、その中途の書き込みが確定する可能性がある。rollback の失敗自体が入出力エラー級の稀な事象。

## 原因

## 処置案・裁定

tick の lock 区間の先頭 (足の保存の前) で、停止判定に残骸の片付けをさせる。

## 修正内容

## 経緯

- 2026-10-02: 起票。

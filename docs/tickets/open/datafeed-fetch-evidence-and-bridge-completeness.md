---
id: datafeed-fetch-evidence-and-bridge-completeness
title: 価格取得の証跡と bridge の過去区間の完全性保証 (tick 無しの証明)
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-11-03
closed: null
related: [econ-event-actuals-and-reaction-stats]
---
# [datafeed-fetch-evidence-and-bridge-completeness] 価格取得の証跡と bridge の過去区間の完全性保証 (tick 無しの証明)

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

開場中に足が無い minute が tick 無しか障害欠損かを区別する情報が無い。econ 設計 v0.10.2 は安全側に全部を障害欠損として扱い、取得の証跡 (datafeed_fetch_evidence) と no_tick_flat を v1 から外した

## 原因

## 処置案・裁定

bridge が (1) 現在の terminal 接続と (2) 要求した過去区間の完全性 (その区間に tick が無かったこと) を返せるようにし、その時点から ingest が取得の証跡を記録し、証明できた minute だけ flat にする。今から証跡を貯めても遡って flat にはできない (econ 設計 §6)

## 修正内容

## 経緯

- 2026-11-03: 起票。

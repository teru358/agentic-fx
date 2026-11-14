---
id: improve-admission-host-leaks-across-tests
title: tests/service の後で improve admission の test が落ちる
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-14
closed: null
related: []
---
# [improve-admission-host-leaks-across-tests] tests/service の後で improve admission の test が落ちる

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

tests/service の後に tests/core/test_improve_admission.py を同じ process で回すと test_disabled_startup_reads_only が落ちる (current_host() が None でなく MissionCgroupHost を返す)。単独では 59 件とも緑。main (a58dc4bb) で `pytest tests/service tests/core/test_improve_admission.py -p no:randomly` により 10 秒で再現。tests/service のどこかが improve_admission の module global `_HOST` を設定したまま後始末しない。どの test が残すかを特定し、fixture で戻すか、admission 側で test 用の reset を用意する。束での判定に赤を 1 件足すので、全体判定の読み違いの元になる。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。

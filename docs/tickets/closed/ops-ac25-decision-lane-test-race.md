---
id: ops-ac25-decision-lane-test-race
title: ops ac25 決定レーン試験が順序窓で不安定
status: 見送り
priority: 低
opened: 2026-10-10
closed: 2026-10-23
related: []
---
# [ops-ac25-decision-lane-test-race] ops ac25 決定レーン試験が順序窓で不安定

**状態**: 見送り / **優先**: 低

## 現象

tests/ops/test_ac09_25_26_decision_lane.py::test_ac25_job_completes_without_polling_and_duplicate_is_in_progress が無変異でも 3 回中 1 回落ちる。approval が rejected になる前に job が done に達しない順序窓。fixture は build_app を通らない。main でも再現 (2026-10-10 観測)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-10: 起票。
- 2026-10-23: 見送り理由 — ops-decision-lane-ac25-timing-flake と重複。そちらに統合

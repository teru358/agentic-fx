---
id: ops-decision-lane-ac25-timing-flake
title: tests/ops の ac25 が負荷下で timing flake
status: 設計待ち
priority: 低
opened: 2026-10-13
closed: null
related: []
---
# [ops-decision-lane-ac25-timing-flake] tests/ops の ac25 が負荷下で timing flake

**状態**: 設計待ち / **優先**: 低

## 現象

tests/ops/test_ac09_25_26_decision_lane.py::test_ac25_job_completes_without_polling_and_duplicate_is_in_progress が、並列に別 pytest が走っている・連続実行のとき assert 'running' == 'done' で落ちる (sleep 0.02 の待ちで job 完了を仮定)。単独では 3/3 通る。2026-10-13 の first-run 判定で 3 回観測

## 原因

job 完了を固定 sleep で待っている。CPU 負荷で完了が遅れると state が running のまま

## 処置案・裁定

完了を条件待ち (deadline つき poll か event) に変える。他の sleep 固定待ちの test も grep して同じ形に

## 修正内容

## 経緯

- 2026-10-13: 起票。

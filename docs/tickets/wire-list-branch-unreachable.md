---
id: wire-list-branch-unreachable
title: 到達しない list 分岐が読み手を惑わす
status: 実装待ち
priority: 低
opened: 2026-09-18
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [wire-list-branch-unreachable]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[wire-list-branch-unreachable] 起票 2026-09-18 (同 T-2): `worker._indicator_result_to_wire` の `list` 分岐は唯一の呼び出し元が `df_index` 非 None のため到達不能 (validator が必ず `pd.Series` へ畳む)。`0ab6ce6` で NaN 安全にはした。削除するか到達経路を作るか未決 (ローカル 3 本が独立に引っかかった = 読み手を惑わす)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 到達しない list 分岐が読み手を惑わす — 仕分け (2026-10-02、現物で成立を確認): 削除するか到達経路を作るか未決 (整理)。 根拠: plugin/worker.py:253 の list 分岐は唯一の呼び出し元 (worker.py:337) から到達しない。

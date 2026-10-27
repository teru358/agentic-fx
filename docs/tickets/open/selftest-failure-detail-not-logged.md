---
id: selftest-failure-detail-not-logged
title: improve 隔離の自己試験が落ちた理由がどこにも残らない
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-10-27
closed: null
related: [improve-gpt6-models-fail-isolated-selftest]
---
# [selftest-failure-detail-not-logged] improve 隔離の自己試験が落ちた理由がどこにも残らない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

起動時の improve 隔離 admission で自己試験が不合格になると、技術ログには `admitted=no reason=runtime_fingerprint_selftest_failed` だけが出る。不合格の条件 (echo 未到達、MCP の initialize / tools/list / list_staging 呼び出しの欠落、grace 超過、CLI 子プロセス未観測、子プロセス未回収など、loops/verify_backend.py が組む detail) は ProbeOutcome.detail (core/improve_canary.py の run_selftest) に入るが、runtime_fingerprint の admission で捨てられ、ログにも activity にも残らない。失敗の理由が読めないので原因を絞れない (2026-10-27、gpt-6-luna で再現)。是正案: 不合格時に detail (固定語の組立てで 200 字上限) を技術ログに 1 行出す。秘密や自由文が入らないことを確認する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。

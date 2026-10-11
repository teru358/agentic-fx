---
id: full-suite-from-repo-root-hangs-with-live-data
title: repo root から full suite を回すと実 data/ を掴んで固まる
status: 設計待ち
priority: 中
opened: 2026-10-11
closed: null
related: [rpc-dispatcher-join-budget-test-flaky-under-load]
---
# [full-suite-from-repo-root-hangs-with-live-data] repo root から full suite を回すと実 data/ を掴んで固まる

**状態**: 設計待ち / **優先**: 中

## 現象

2026-10-11、稼働中 afx がある repo root で uv run pytest (not realbackend) を回すと 2 回連続で約 77% 付近で停止 (futex 待ち)。pytest process 内に tokio-rt-worker 4960 本、sqlx-sqlite-wor 169 本、同一 socket の dup fd が 1000 本超。worktree (data/ 無し) では同じ suite が 5 分で完走。chroma (RAG) や相対 path で実 data/ を開く test が runtime / fd を閉じずに溜めていると見られる。conftest の session ガードで実 data/ を封じ、資源を閉じる fixture を正にする。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。

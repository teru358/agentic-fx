---
id: improve-handler-unexpected-exception-text-reaches-agent
title: 改善 mission の handler で想定外例外の文面が agent に届く
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [improve-handler-unexpected-exception-text-reaches-agent] 改善 mission の handler で想定外例外の文面が agent に届く

**状態**: 設計待ち / **優先**: 中

## 現象

handler の try の外で起きた例外 (improve_loop.py:1096 の RuntimeError を含む) は worker_runner.py:358 の str(e) のまま agent への応答になる。backtest の失敗は固定の分類と hint に揃えたが、この経路は対象外で残っている。テストで固定されているのは 1 経路 (test_run_backtest_handler_rejects_non_strategy_candidate) だけ。

## 原因

固定応答への写像が run_backtest の SandboxError 経路に限られている。

## 処置案・裁定

handler 全体で、想定外例外は固定の error と hint に写し、原文は activity とログだけに残す。意図して文面を返している経路 (no_history_for_symbol、pair_not_declared_by_plugin、indicator_unresolved) は agent が直せる情報なので残すかを個別に決める。

## 修正内容

## 経緯

- 2026-10-02: 起票。

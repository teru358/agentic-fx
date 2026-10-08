---
id: gate-pytest-tail-stored-in-last-result
title: gate の pytest 出力末尾が次回の改善 mission の材料になる
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [gate-pytest-tail-stored-in-last-result] gate の pytest 出力末尾が次回の改善 mission の材料になる

**状態**: 設計待ち / **優先**: 中

## 現象

_finalize_gate_failed が gate_failed:pytest failed: {stdout_tail} と str(exc) を last_result にそのまま保存する。候補 plugin 自身のテスト出力なので、候補が任意の文字列を次回 mission の prompt に流し込める。test_improve_loop_finalize は「流れること」を期待値として固定している。

## 原因

自分のテスト失敗を次回に直せるよう意図した可能性がある (未確認)。人間の reject reason は固定文言 + 除染に揃えたが、この経路は同じ扱いになっていない。

## 処置案・裁定

まず意図を確認する。残すなら長さの上限と無害化 (制御文字、holdout 数値の遮断規律との照合) を掛ける。残さないなら固定の分類に置き換え、原文は activity だけに残す。

## 修正内容

## 経緯

- 2026-10-02: 起票。

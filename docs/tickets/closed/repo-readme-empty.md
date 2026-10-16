---
id: repo-readme-empty
title: README.md が 0 バイト
status: 是正済
priority: 中
opened: 2026-10-02
closed: 2026-10-16
related: []
---
# [repo-readme-empty] README.md が 0 バイト

**状態**: 是正済 / **優先**: 中

## 現象

公開リポジトリの入口が空。

## 原因

## 処置案・裁定

最小の入口 (何であるか、前提、初期設定コマンド、安全上の原則、bridge への誘導) を書く。初期設定ウィザードの実装と同時でよい。

## 修正内容

- 2026-10-16: first-run T8 で README を書いた (入口: 前提、clone → uv sync → afx init、reset、afx ctl、設計書へのリンク)。main 2733093

## 経緯

- 2026-10-02: 起票。
- 2026-10-16: 是正内容 — first-run T8 で README を書いた (入口: 前提、clone → uv sync → afx init、reset、afx ctl、設計書へのリンク)。main 2733093

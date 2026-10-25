---
id: seccomp-chmod-eperm-breaks-codex-rollout
title: seccomp の chmod/fchmod EPERM で codex の rollout 書込みが失敗する
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: [real-cli-selftest-should-exercise-tools-call]
kind: 不具合
---
# [seccomp-chmod-eperm-breaks-codex-rollout] seccomp の chmod/fchmod EPERM で codex の rollout 書込みが失敗する

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

隔離下の codex 0.162.1 は起動時に PATH alias 作成と rollout (session jsonl) 書込みで Operation not permitted (os error 1) を出す (自己試験・実 mission とも)。seccomp improve-cli/1 の表で chmod / fchmod が EPERM のため (Rust std の set_permissions)。codex は WARNING を出して続行するので非致命だが、rollout は残らず、将来の版で fatal 化し得る。閉鎖案: 自分の rw 領域内の chmod / fchmod を許す (Landlock が領域を限るので危険は小さい) か、拒否を残すなら spec §12 の残余として明記。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与

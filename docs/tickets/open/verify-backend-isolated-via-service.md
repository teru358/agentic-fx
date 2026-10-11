---
id: verify-backend-isolated-via-service
title: 隔離付きの backend 疎通確認を service 経由 (ops API / afx ctl) で提供する
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: [real-cli-selftest-should-exercise-tools-call]
---
# [verify-backend-isolated-via-service] 隔離付きの backend 疎通確認を service 経由 (ops API / afx ctl) で提供する

**状態**: 設計待ち / **優先**: 低

## 現象

運用者の shell で叩く afx improve verify-backend は service の mission cgroup host が無いため、CLI 隔離の有効化後は OPERATOR_SHELL_NO_ISOLATION で追加隔離 (seccomp / cgroup) なしの疎通確認だけを行う (有効化前と同じ、outer Landlock + rlimit のみ)。隔離適合の確認は service 起動時の admission (journal の 1 行) に限られ、運用者が任意の backend を隔離付きで確かめる手段が無い。閉鎖案: ops API に verify job を足し afx ctl から呼ぶ (ウィザードの LLM 接続確認もこれを使う)。同 uid の露出 (/proc、Yama 依存の process memory) は有効化前と同等で悪化ではない。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。

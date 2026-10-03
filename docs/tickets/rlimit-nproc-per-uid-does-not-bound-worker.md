---
id: rlimit-nproc-per-uid-does-not-bound-worker
title: RLIMIT_NPROC は uid 全体の値で worker の子プロセス数を縛れない
status: 設計待ち
priority: 低
opened: 2026-10-03
closed: null
related: []
---
# [rlimit-nproc-per-uid-does-not-bound-worker] RLIMIT_NPROC は uid 全体の値で worker の子プロセス数を縛れない

**状態**: 設計待ち / **優先**: 低

## 現象

plugin worker の RLIMIT_NPROC=512 は uid 全体のプロセス・スレッド数と比較される。この host では uid のスレッド数が 1022 あり、seccomp と無関係にスレッド生成が EAGAIN になる一方、NPROC を設定しても plugin の fork を縛れない (失敗も無視される)。plugin worker Landlock の設計中に確認 (2026-10-03、tmp/design-plugin-landlock/measure/)。

## 原因

## 処置案・裁定

子プロセスの生成は seccomp で拒否する (plugin worker Landlock の束)。NPROC は「worker 自身のスレッド生成を阻害しない値」に見直すか、設定しない。uid 全体の数が多い host で worker が起動できない事象の検知と文面を足す。

## 修正内容

## 経緯

- 2026-10-03: 起票。

---
id: sandbox-escape-via-user-daemons-unmeasured
title: 改善 agent の檻からの脱出 (D-Bus / screen 経由) を測らずに塞ぐ
status: 是正済
priority: 高
opened: 2026-10-03
closed: 2026-10-23
related: []
---
# [sandbox-escape-via-user-daemons-unmeasured] sandbox から他デーモン (D-Bus / screen) 経由で抜けられるかが未実測

**状態**: 是正済 / **優先**: 高

## 現象

Landlock は pathname UDS の connect を仲介しない (実測)。sandbox 内の改善 agent (CLI backend は /usr/bin を exec できる) が /run/user/<uid>/bus 経由で systemd --user に一時 unit を起動させる、または screen のセッション socket に届いて現行運用の afx> に approve を打ち込む、という経路が成り立つ可能性がある。成り立てば Landlock 文脈の「鍵が読めない」防御が崩れる。

## 原因

## 処置案・裁定

操作 API の実装前に実測する (2026-10-03 裁定)。測り方は tmp/design-ops-api/C0.md §12.3。成り立つなら、Landlock の scope 規則 (abstract UDS) と network 規則、/run/user の allowlist からの除外、screen 廃止 (daemon 化) の優先度を上げる。

## 修正内容

- 2026-10-23: 2026-10-04 裁定 (案 1: 実測せず塞ぐ) のとおり、spec 2026-10-07-improve-cli-seccomp v1.2 を実装し 5528416c で改善 CLI の隔離を有効化 (ISOLATION_ENABLED、admission 通過必須の fail closed)。seccomp improve-cli/1 (core/seccomp_cli_policy.py socket 条件) で AF_UNIX は EACCES、INET は TCP stream のみ、connect は core/connect_supervisor.py が代行。人手の実測は escape-probe-manual-measurement に残す

## 経緯

- 2026-10-03: 起票。
- 2026-10-04: 題名: 改善 agent の檻からの脱出 (D-Bus / screen 経由) を測らずに塞ぐ — 2026-10-04 ユーザー裁定 (案 1): 実測は安全分類器に止められたため行わない。読み取りで得た事実 (改善 worker の檻は /usr/bin を exec 可、session bus の socket は同 uid で接続可、Landlock は UNIX socket の connect を仲介しない) から「成り立つ」前提で塞ぐ。塞ぎ方 = 改善 mission worker (mission_worker._bootstrap_improve_profile) に seccomp を足して UNIX socket と localhost TCP の接続を拒否する (plugin worker 用の core/seccomp を再利用)。確認は無害な形 (各 backend claude / codex / opencode が強化後の檻で起動・動作するか) だけ。着手は plugin worker 隔離の main 投入後、操作 API の実装と並行可。
- 2026-10-09: 状態: 実装中 — 2026-10-09: spec improve-cli-seccomp v1.2 をユーザー承認、第 1 波 (A1/A2/T2/T3a/T4a/T4b/T5) を worktree 並列で実装中。T2 を main 投入
- 2026-10-23: 是正内容 — 2026-10-04 裁定 (案 1: 実測せず塞ぐ) のとおり、spec 2026-10-07-improve-cli-seccomp v1.2 を実装し 5528416c で改善 CLI の隔離を有効化 (ISOLATION_ENABLED、admission 通過必須の fail closed)。seccomp improve-cli/1 (core/seccomp_cli_policy.py socket 条件) で AF_UNIX は EACCES、INET は TCP stream のみ、connect は core/connect_supervisor.py が代行。人手の実測は escape-probe-manual-measurement に残す

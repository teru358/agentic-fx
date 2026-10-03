---
id: sandbox-escape-via-user-daemons-unmeasured
title: sandbox から他デーモン (D-Bus / screen) 経由で抜けられるかが未実測
status: 設計待ち
priority: 高
opened: 2026-10-03
closed: null
related: []
---
# [sandbox-escape-via-user-daemons-unmeasured] sandbox から他デーモン (D-Bus / screen) 経由で抜けられるかが未実測

**状態**: 設計待ち / **優先**: 高

## 現象

Landlock は pathname UDS の connect を仲介しない (実測)。sandbox 内の改善 agent (CLI backend は /usr/bin を exec できる) が /run/user/<uid>/bus 経由で systemd --user に一時 unit を起動させる、または screen のセッション socket に届いて現行運用の afx> に approve を打ち込む、という経路が成り立つ可能性がある。成り立てば Landlock 文脈の「鍵が読めない」防御が崩れる。

## 原因

## 処置案・裁定

操作 API の実装前に実測する (2026-10-03 裁定)。測り方は tmp/design-ops-api/C0.md §12.3。成り立つなら、Landlock の scope 規則 (abstract UDS) と network 規則、/run/user の allowlist からの除外、screen 廃止 (daemon 化) の優先度を上げる。

## 修正内容

## 経緯

- 2026-10-03: 起票。

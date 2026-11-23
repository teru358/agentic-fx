---
id: test-pidfd-leak-after-plugin-suite
title: tests/plugin の後の同一プロセスで pidfd が残り connect_supervisor 系が赤
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-23
closed: null
related: []
---
# [test-pidfd-leak-after-plugin-suite] tests/plugin の後の同一プロセスで pidfd が残り connect_supervisor 系が赤

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

全 test を 1 プロセスで回し、tests/plugin の後に connect_supervisor を使う test (egress の proxy_handoff_supervisor・pins、core の mission_cleanup 等) を回すと、pidfd が 1 本残っているため 93 件が赤になる (2026-11-21 観測: 5 failed / 46 errors)。main でも再現し、本番 daemon の pidfd は 0 本。test 間の干渉で、本番の不具合ではない。判定は dir ごとの束に分けて回すことで避けている。直すには、tests/plugin のどの test が pidfd を閉じずに残すかを特定し、fixture の後始末で閉じる

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-23: 起票。

---
id: test-service-app-leaks-fds
title: tests/test_service_app.py が fd を 1100 超残し後続 test に波及する
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-10-26
closed: null
related: [full-suite-from-repo-root-hangs-with-live-data]
---
# [test-service-app-leaks-fds] tests/test_service_app.py が fd を 1100 超残し後続 test に波及する

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

tests/test_service_app.py を同じ process で先に回すと fd が 1100 超残り、後続の ops_setup/test_interfaces が select.select の ValueError (fd>=1024) で落ちていた (2026-10-26、Discord 通知の最終判定で発見、main でも再現)。interfaces 側は poll に変えて堅牢化済み。fd 漏れ自体は未対処。

## 原因

## 処置案・裁定

どの fixture / service 構築が fd を閉じていないかを組で二分して特定し、teardown で閉じる。

## 修正内容

## 経緯

- 2026-10-26: 起票。
- 2026-10-29: 関連: [full-suite-from-repo-root-hangs-with-live-data] — 2026-10-29 棚卸し: 関連を束ねる先に張る

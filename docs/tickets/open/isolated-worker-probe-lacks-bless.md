---
id: isolated-worker-probe-lacks-bless
title: 実隔離 worker の deploy 境界 probe に plugin bless が無い
status: 設計待ち
priority: 低
opened: 2026-10-19
closed: null
related: [deployed-plugin-has-no-disable-or-rollback]
---
# [isolated-worker-probe-lacks-bless] 実隔離 worker の deploy 境界 probe に plugin bless が無い

**状態**: 設計待ち / **優先**: 低

## 現象

tests/integration/test_improve_worker_deploy_boundary.py の CLI probe は disable / rollback / retire / revoke を実隔離で実測するが、人間 CLI 専用の plugin bless (即時承認) が無い。bless は registry 側 IMPROVE_FORBIDDEN で pin 済みなので遮断は成立しているが、実隔離 CLI 経路での実測は無い。正の対照 (隔離なしで成功) には plugins/_human/<name> の有効な候補 fixture が要る。versions は読み取り専用 (pdo spec AC-30) で対象外。pdo T8 閉鎖確認 (codex) の指摘。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-19: 起票。

---
id: ops-probe-child-env-drops-xdg-config-home
title: afx ops の probe 子 process に XDG_CONFIG_HOME が渡らない
status: 設計待ち
priority: 中
opened: 2026-10-24
closed: null
related: [web-lan-access-bind-allow-auth]
kind: 不具合
---
# [ops-probe-child-env-drops-xdg-config-home] afx ops の probe 子 process に XDG_CONFIG_HOME が渡らない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

ops_setup/probe.py の run_probe は子 process (afx-ops probe) の env を {"PATH": os.defpath} だけにする。afx-ops は XDG_CONFIG_HOME を読み、無ければ pwd の home を見るので、利用者が既定外の XDG_CONFIG_HOME を使っていると wizard の書込み先 (本体 feature_config は環境変数を読む) と probe の読み先がずれる。本番の systemd unit の env との整合も含めて要確認。web-lan T2 統合 test は probe を env 付きで直接呼んで回避 (2026-10-24)。main 既存の挙動

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-24: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与

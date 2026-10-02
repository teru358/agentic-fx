---
id: bridge-order-endpoints-open-on-lan-by-default
title: bridge の発注系 endpoint が既定で LAN に開く
status: 設計待ち
priority: 高
opened: 2026-10-02
closed: null
related: []
---
# [bridge-order-endpoints-open-on-lan-by-default] bridge の発注系 endpoint が既定で LAN に開く

**状態**: 設計待ち / **優先**: 高

## 現象

mt5_bridge の BRIDGE_HOST 既定は 0.0.0.0 (config.py:70)、API キー未設定は LAN trust モード (server.py:174)。POST /order、/positions/{ticket}/modify、/close、/admin/halt、/admin/resume がある (server.py:352,431,452,487,499)。DRY_RUN の既定は true (config.py:73) なので既定のままなら実発注にはならないが、公開リポジトリの既定として不適。作者の環境も 0.0.0.0:8812 で待受 (2026-10-02 確認、キー設定の有無は未確認)。

## 原因

## 処置案・裁定

既定の待受を 127.0.0.1 にする。127.0.0.1 以外で待ち受けるときは API キー必須 (無ければ起動拒否)。発注系 endpoint は DRY_RUN=false のときキー必須。作者環境のキー設定を確認する。

## 修正内容

## 経緯

- 2026-10-02: 起票。

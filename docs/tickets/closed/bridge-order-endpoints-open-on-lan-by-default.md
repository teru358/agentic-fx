---
id: bridge-order-endpoints-open-on-lan-by-default
title: bridge の発注系 endpoint が既定で LAN に開く
status: 是正済
priority: 高
opened: 2026-10-02
closed: 2026-10-03
related: []
---
# [bridge-order-endpoints-open-on-lan-by-default] bridge の発注系 endpoint が既定で LAN に開く

**状態**: 是正済 / **優先**: 高

## 現象

mt5_bridge の BRIDGE_HOST 既定は 0.0.0.0 (config.py:70)、API キー未設定は LAN trust モード (server.py:174)。POST /order、/positions/{ticket}/modify、/close、/admin/halt、/admin/resume がある (server.py:352,431,452,487,499)。DRY_RUN の既定は true (config.py:73) なので既定のままなら実発注にはならないが、公開リポジトリの既定として不適。作者の環境も 0.0.0.0:8812 で待受 (2026-10-02 確認、キー設定の有無は未確認)。

## 原因

## 処置案・裁定

既定の待受を 127.0.0.1 にする。127.0.0.1 以外で待ち受けるときは API キー必須 (無ければ起動拒否)。発注系 endpoint は DRY_RUN=false のときキー必須。作者環境のキー設定を確認する。

## 修正内容

- 2026-10-03: 既定待受を 127.0.0.1 に。loopback 以外の host はキー必須 (無ければ起動拒否)、ホスト名はキー必須、localhost は 127.0.0.1 に正規化。発注系 5 endpoint は DRY_RUN に関係なくキー必須 (403)。キー未設定時は ASGI middleware で接続元が loopback の IP でなければ全経路 403、proxy header を読まない、docs 系を無効化。段 0 変異 42 本、codex 2 周、実 ASGI 経路のテスト 189 passed。main 投入 2026-10-03。

## 経緯

- 2026-10-02: 起票。
- 2026-10-03: 状態: 実装中 — 2026-10-03 着手 (並行実装、Landlock 設計の収束待ちの間)。
- 2026-10-03: 是正内容 — 既定待受を 127.0.0.1 に。loopback 以外の host はキー必須 (無ければ起動拒否)、ホスト名はキー必須、localhost は 127.0.0.1 に正規化。発注系 5 endpoint は DRY_RUN に関係なくキー必須 (403)。キー未設定時は ASGI middleware で接続元が loopback の IP でなければ全経路 403、proxy header を読まない、docs 系を無効化。段 0 変異 42 本、codex 2 周、実 ASGI 経路のテスト 189 passed。main 投入 2026-10-03。

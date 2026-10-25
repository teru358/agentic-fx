---
id: test-init-guard-order-dependent-on-fetchers
title: offline init test が test_fetchers の後では赤になる (順序依存)
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-10-25
closed: null
related: []
---
# [test-init-guard-order-dependent-on-fetchers] offline init test が test_fetchers の後では赤になる (順序依存)

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

tests/datafeed/test_fetchers.py を同じ process で先に回すと tests/test_init_and_guard.py::test_init_completes_offline_with_unreachable_bridge が 'mt5: JSONDecodeError' で失敗する。単独では緑。main (6b554d4f) でも同じ組で再現 (news T1 の判定で発見、2026-10-25)。

## 原因

未調査。test_fetchers 側が httpx の transport か mt5 クライアントのモジュール状態 (monkeypatch の autouse 残留か module キャッシュ) を残している疑い。

## 処置案・裁定

組で二分して残留元を特定し、fixture で閉じる。メモリ monkeypatch-over-autouse-patch-leaks-mock の既知パターンと照合。

## 修正内容

## 経緯

- 2026-10-25: 起票。

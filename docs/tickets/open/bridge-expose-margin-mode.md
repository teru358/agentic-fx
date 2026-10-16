---
id: bridge-expose-margin-mode
title: bridge の /account に margin_mode (netting / hedging) を出す
status: 設計待ち
priority: 低
opened: 2026-10-16
closed: null
related: [per-strategy-virtual-books]
---
# [bridge-expose-margin-mode] bridge の /account に margin_mode (netting / hedging) を出す

**状態**: 設計待ち / **優先**: 低

## 現象

mt5_bridge の /account は currency / leverage / trade_mode を返すが MT5 の account_info().margin_mode (0 netting / 1 exchange / 2 hedging) を出さない。両建て可否 (per-strategy-virtual-books の前提条件 1) の判断に要る。1 項目の追加と afx 側の表示 (afx ctl status か afx check)。bridge は Wine 上の Windows Python で動く (mt5_bridge/、本機で再起動が要る)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-16: 起票。

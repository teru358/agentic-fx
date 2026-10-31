---
id: plugin-ast-open-attribute-false-positive
title: plugin AST 検査が df.open (OHLCV 列の属性参照) を reject する
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-10-31
closed: null
related: []
---
# [plugin-ast-open-attribute-false-positive] plugin AST 検査が df.open (OHLCV 列の属性参照) を reject する

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

improve mission 2656 (2026-10-31) が gate_failed: test_plugin.py: attribute open is not allowed。sandbox.py の _DENY_NAMES (open / eval / exec …) が ast.Attribute の attr にも適用されるため、DataFrame の open 列を属性で読む df.open が builtin open() と同じ理由で落ちる。agent には理由が伝わらず試行 1 回が無駄になる。案: (a) 改善 prompt と例に「列は df["open"] で読む (属性 .open は不可)」を 1 文、(b) または Attribute では open を許す (call でない属性参照に限る) — 境界コードなので (a) を先に。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。

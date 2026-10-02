---
id: runner-trade-backend-error-wording-mismatch
title: runner.trade の backend の検証文言が受け付け方と食い違う
status: 設計待ち
priority: 低
opened: 2026-10-02
closed: null
related: []
---
# [runner-trade-backend-error-wording-mismatch] runner.trade の backend の検証文言が受け付け方と食い違う

**状態**: 設計待ち / **優先**: 低

## 現象

config.py:115 のパターンは 4 種を受けてから 2 種を拒否する。example のコメントは local | claude で現物と一致。文言だけの不一致。

## 原因

## 処置案・裁定

パターンを 2 種に絞るか、拒否の文面を揃える。

## 修正内容

## 経緯

- 2026-10-02: 起票。

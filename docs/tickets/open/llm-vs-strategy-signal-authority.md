---
id: llm-vs-strategy-signal-authority
title: LLM と戦略シグナルの役割分担 (シグナルの最終審査員にするか)
status: 裁定待ち
priority: 中
kind: 機能
opened: 2026-10-30
closed: null
related: [econ-event-hold-window-deterministic, econ-event-actuals-and-reaction-stats]
---
# [llm-vs-strategy-signal-authority] LLM と戦略シグナルの役割分担 (シグナルの最終審査員にするか)

**状態**: 裁定待ち / **優先**: 中 / **種別**: 機能

## 現象

戦略 plugin の signal が LLM の裁量で潰されている。policy の managed-policy id=1「get_signals の方向と自分のテクニカル判断が一致するかを reasoning に書く」により、2026-10-30 の hold の過半は「signal と自分の見立てが不一致なので不採用」。指標の回避帯を絞っても、LLM がシグナルを裁量で却下する構造は残る。決めること: LLM を (a) シグナルの最終審査員にする (現状)、(b) シグナルに従い、回避帯と資金規則 (決定論) だけで止める、(c) 中間 (シグナルの採否は LLM だが却下理由を固定語で分類し、却下率を監視する)。製品方針なのでユーザー裁定。改善ループの評価 (戦略の成績) と live の成績が乖離する原因にもなる。設計前にブレスト (ユーザー要望) と sol の前提確認

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-30: 起票。
- 2026-10-30: 状態: 裁定待ち — ブレスト → sol 前提確認 → 設計 C0 の順 (3 件を一束で)

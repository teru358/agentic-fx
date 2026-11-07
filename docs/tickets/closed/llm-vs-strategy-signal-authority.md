---
id: llm-vs-strategy-signal-authority
title: LLM と戦略シグナルの役割分担 (シグナルの最終審査員にするか)
status: 是正済
priority: 中
kind: 機能
opened: 2026-10-30
closed: 2026-11-07
related: [econ-event-hold-window-deterministic, econ-event-actuals-and-reaction-stats]
---
# [llm-vs-strategy-signal-authority] LLM と戦略シグナルの役割分担 (シグナルの最終審査員にするか)

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

戦略 plugin の signal が LLM の裁量で潰されている。policy の managed-policy id=1「get_signals の方向と自分のテクニカル判断が一致するかを reasoning に書く」により、2026-10-30 の hold の過半は「signal と自分の見立てが不一致なので不採用」。指標の回避帯を絞っても、LLM がシグナルを裁量で却下する構造は残る。決めること: LLM を (a) シグナルの最終審査員にする (現状)、(b) シグナルに従い、回避帯と資金規則 (決定論) だけで止める、(c) 中間 (シグナルの採否は LLM だが却下理由を固定語で分類し、却下率を監視する)。製品方針なのでユーザー裁定。改善ループの評価 (戦略の成績) と live の成績が乖離する原因にもなる。設計前にブレスト (ユーザー要望) と sol の前提確認

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-07: 束 1 (設計 docs/superpowers/specs/2026-11-01-econ-window-signal-authority-shadow-design.md v0.10.4) を 2026-11-07 に本番配備 (main c0aa9bb0)。回避帯の決定論的判定と裁量 OPEN の帯内拒否、strategy signal の専用採否 mission (accept 既定、reject は news_shock / opposite_position の固定 code)、accept / reject の shadow 成績と 1m slice の退避 worker、status / web / 日次 report。merge 前必須 job 3 本 (Claude 実往復・履歴 DB 再生成 970/3/57/1,033・実ディスク bench) 合格、J3 の方針既定文を登録し旧 1 行目を削除

## 経緯

- 2026-10-30: 起票。
- 2026-10-30: 状態: 裁定待ち — ブレスト → sol 前提確認 → 設計 C0 の順 (3 件を一束で)
- 2026-11-01: 状態: 実装待ち — 設計 C0 v0.9 (docs/superpowers/specs/2026-11-01-econ-window-signal-authority-shadow-design.md) を 8 周のレビューで清書、J1〜J3 をユーザーが推奨どおり承認。3 件は束 1 (回避帯・採否契約・shadow、T1a〜T5) と束 2 (反応統計、T1〜T4) で実装。actual 取得は対象外 (源に無い)
- 2026-11-07: 是正内容 — 束 1 (設計 docs/superpowers/specs/2026-11-01-econ-window-signal-authority-shadow-design.md v0.10.4) を 2026-11-07 に本番配備 (main c0aa9bb0)。回避帯の決定論的判定と裁量 OPEN の帯内拒否、strategy signal の専用採否 mission (accept 既定、reject は news_shock / opposite_position の固定 code)、accept / reject の shadow 成績と 1m slice の退避 worker、status / web / 日次 report。merge 前必須 job 3 本 (Claude 実往復・履歴 DB 再生成 970/3/57/1,033・実ディスク bench) 合格、J3 の方針既定文を登録し旧 1 行目を削除

---
id: econ-event-hold-window-deterministic
title: 指標前の様子見を tool 側で重要イベントと回避帯に絞る
status: 裁定待ち
priority: 中
kind: 機能
opened: 2026-10-30
closed: null
related: [econ-event-actuals-and-reaction-stats, llm-vs-strategy-signal-authority]
---
# [econ-event-hold-window-deterministic] 指標前の様子見を tool 側で重要イベントと回避帯に絞る

**状態**: 裁定待ち / **優先**: 中 / **種別**: 機能

## 現象

取引判断の hold が指標を理由に多発 (2026-10-16〜30 で hold 944 / open 3、イベントを理由に含む payload 838)。原因: trade_mission.md の原則「経済指標発表の直前は新規エントリーを避ける」に時間幅・重要度・通貨の定義が無く、get_econ_calendar が 24 時間分を通貨・重要度の絞り込みなしで返す (10/27〜11/1 は 101 件、importance 3 が 27 件)。LLM は importance 2 や無関係通貨 (CAD GDP) まで 1.5〜2 時間前から回避している。対策: tool 側で決定論的に、対象 pair の通貨 × importance 3 × 発表前 N 分〜後 M 分の回避帯だけを返す (帯の外は空)。N / M と importance 2 を含めるかは settings (既定は安全側)。プロンプトの文言は回避帯の意味に合わせて直す。設計前にブレスト (ユーザー要望) と sol の前提確認

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-30: 起票。
- 2026-10-30: 状態: 裁定待ち — ブレスト → sol 前提確認 → 設計 C0 の順 (3 件を一束で)

---
id: trade-backend-opencode-single-local-path
title: 取引判断にも opencode backend を許し、完全ローカル運用を opencode 1 本に
status: 設計待ち
priority: 中
opened: 2026-10-14
closed: null
related: [first-run-setup, wizard-stage4-menu-per-role]
---
# [trade-backend-opencode-single-local-path] 取引判断にも opencode backend を許し、完全ローカル運用を opencode 1 本に

**状態**: 設計待ち / **優先**: 中

## 現象

完全ローカル運用の手引きが「改善 = opencode」「取引 = OpenAI 互換 endpoint 直結 (LocalRunner)」の 2 本立てで手間 (2026-10-14 ユーザー指摘)。方針: trade.backend に opencode を許し、取引 profile (MCP シムは claude と同じ、opencode の edit/write/bash 無効、出力は TradeIntent JSON、隔離は改善側を流用) を OpencodeRunner に与える。afx は opencode から bin とモデル id (opencode models) だけを受け、provider は opencode 側に閉じる。要裁定 (設計レビューで): context_limit は opencode 設定 (limit.context) から読むか聞くか / 隔離の egress 許可先 (provider の host) はウィザードで聞いて settings に書く。実測条件: 毎時 mission の opencode 起動コスト、ローカルモデルの TradeIntent JSON 成功率が LocalRunner と同等以上。クリア後に LocalRunner を任意経路に格下げし手引きを 1 本化、削除は数週間の運用後に判断。順序: 受入完了後に設計 1 周 → 実装

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。

---
id: cli-surface-principle-and-reorg
title: afx / afx ctl の置き場の原則を定義し、コマンド面を整理する
status: 設計待ち
priority: 中
opened: 2026-10-15
closed: null
related: [news-source-cli-and-429-backoff, deployed-plugin-has-no-disable-or-rollback]
---
# [cli-surface-principle-and-reorg] afx / afx ctl の置き場の原則を定義し、コマンド面を整理する

**状態**: 設計待ち / **優先**: 中

## 現象

原則 (2026-10-15 ユーザー方針): afx ctl = 稼働中の daemon に対する操作 (操作 API 経由、認証・監査つき、bot / web と同じ操作面。daemon の生きた状態 = mission の排他スロット・承認キュー・kill switch・データ状態を読む・動かす・回復する: status/log/activity/events/jobs、approval/approve/reject/retry、ask/improve/backlog add/policy add、killswitch/data resume/reflect retry)。最上位 afx <sub> = daemon を介さない運用操作 (設定・鍵・資材を人間が整える、オフラインの検査・分析。daemon 停止中でも実行でき、稼働中は flock か次周期反映で共存: init/check/keys/ops、plugin/news/history、backtest/analyze、status)。仕分け規則「daemon の生きた状態が要るなら ctl、要らないなら最上位」。整理項目: (1) `afx improve verify-backend` を `afx check` 側へ (daemon 不要の検査。`afx improve` 群は廃止、旧名は 1 版だけ案内つきで残す)、(2) `afx ctl improve add` → `afx ctl backlog add` に改名 (別名は残す) + `backlog list`、(3) `afx ctl policy show` / `policy remove <n>`、(4) `afx plugin list` (配備済み・pending・版)、(5) オフラインの `afx status` (setup.json・未完手順・unit の有無・直近の活動を記録と DB から直接表示。daemon 停止中の診断とウィザードの起動後確認に使う。stream-svr のリセット後に afx ctl status が鍵不在で何も見せられなかった)、(6) README / 手順書 / ops-api 文書に原則の節を 1 つ。ウィザード対話の束とは分け、afx news の束の後に

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-15: 起票。

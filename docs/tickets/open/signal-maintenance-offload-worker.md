---
id: signal-maintenance-offload-worker
title: signal maintenance を tick から別 worker へ切り出す
status: 設計待ち
priority: 低
opened: 2026-10-16
closed: null
related: [live-signal-eval-blocks-protection-tick, deployed-plugin-has-no-disable-or-rollback]
---
# [signal-maintenance-offload-worker] signal maintenance を tick から別 worker へ切り出す

**状態**: 設計待ち / **優先**: 低

## 現象

plugin deploy ops 設計 (2026-10-16) で signal maintenance (reclaim / expire / sandbox 評価 / 再検証 / INSERT) は Scheduler.tick() の遅延 callable として core_lock の外へ出し、保護 tick が core_lock 待ちで止まる問題は解消する。ただし callable は tick スレッド上で同期実行されるため、評価が長い (失敗 plugin は 10〜30 秒、複数本・catch-up で 60 秒超) と次 tick の開始が遅れる部分は残る (tick 周期 60 秒)。

## 原因

遅延 callable は lock 保持の問題だけを解く。tick スレッドの占有そのものは別 worker 化しないと消えない。plugin deploy ops 設計へ worker lifecycle まで入れると範囲が過大になるため分離 (設計レビュー 5 周目の判定)。

## 処置案・裁定

別 worker thread (at-most-one、coalescing)、失敗 plugin の再試行間隔 (backoff)、1 回の評価の時間上限、専用 DB 接続 (conn_signal を引き継ぐ)、shutdown 順序。着手は plugin deploy ops 実装の後、実害 (tick 遅延の実測) が出てから。

## 修正内容

## 経緯

- 2026-10-16: 起票。

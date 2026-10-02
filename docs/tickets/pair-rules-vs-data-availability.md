---
id: pair-rules-vs-data-availability
title: データの無い通貨ペアを agent に見せてしまう余地がある
status: 設計待ち
priority: 低
opened: 2026-09-03
closed: null
related: [dukascopy-likely-blacklisted]
backfilled: true
source_section: 未完了
---
# [pair-rules-vs-data-availability]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[pair-rules-vs-data-availability] **裁定 2026-09-03: MT5 bridge 前倒し** (ユーザー承認: ① MT5 import + UTC 検証 / ② `backtest.eval_source` 設定キー統合 / ③ holdout_months 3→1 を個人設定で一時変更、深い履歴が入ったら 3 に戻す)。①② は完了 (完了ログ参照)。旧状況: ohlcv_history 空 × Dukascopy 遮断 ([[dukascopy-likely-blacklisted]])。**残**: pair_rules とデータ実在の突合 (データの無い pair を agent に見せない)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: データの無い通貨ペアを agent に見せてしまう余地がある — 仕分け (2026-10-02、現物で成立を確認): MT5 import と eval_source 統合は完了済み。pair_rules とデータ実在の突合のみ残る。 根拠: config.py:700 の検査は pairs と pair_rules の対応だけで、データ実在との突合は無い。

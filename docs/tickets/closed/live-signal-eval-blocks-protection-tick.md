---
id: live-signal-eval-blocks-protection-tick
title: 失敗する plugin の signal 計算が毎 tick 10〜30 秒 lock を保持する
status: 是正済
priority: 中
opened: null
closed: 2026-10-18
related: [signal-maintenance-offload-worker, deployed-plugin-has-no-disable-or-rollback]
backfilled: true
source_section: 完了ログ
---
# [live-signal-eval-blocks-protection-tick]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[live-signal-eval-blocks-protection-tick]** (重要度: 高、同上、未実測): signal maintenance は scheduler tick 内の同期 hook。例外は握るが、「完了はするが重い」plugin が多数あると資金保護の tick を遅らせる。watchdog は scheduler の生存だけを見ており busy 時間を見ない。束 B で wall 遅延を評価。

## 修正内容

- 2026-10-18: plugin deploy ops T6 (main 0724a46) で signal maintenance を Scheduler.tick() の遅延 callable にし core_lock の外で実行、専用接続 conn_signal、producer / OPEN の name lock を core_lock と循環しない順序に固定 (AC-34 計装 + 実 Scheduler の event 順序 test)。tick スレッド上の同期実行による次 tick 遅延は signal-maintenance-offload-worker に分離済み

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 優先: 中、題名: 失敗する plugin の signal 計算が毎 tick 10〜30 秒 lock を保持する — 実測 (2026-10-02): signal の計算は同じ tick の SL/TP 走査の後に走るので、同じ tick の資金保護は遅らせない (主張の半分は否定)。遅れるのは次の tick と、発注・クローズの確定を待つ処理。直列で plugin 数に比例 (1 秒 × 5 本 = 5.8 秒)。失敗した plugin は毎 tick 再試行し、timeout で 10 秒・読み込み時の停止で 30 秒を保持する。監視は tick の所要時間を見ない。現在 (1 本・1h・1 ペア) は 0.16 秒で実害なし。複数の戦略を動かす前に対処する。最小対処 = 失敗の再試行に間隔 + 1 tick あたりの時間の上限
- 2026-10-16: plugin deploy ops 設計 (docs/superpowers/specs/2026-10-16-plugin-deploy-ops-design.md) で signal maintenance を Scheduler.tick() の遅延 callable にして core_lock の外で実行する → 保護 tick が signal 計算の lock 待ちで止まる部分は解消 (部分解消)。tick スレッド上の同期実行による次 tick 遅延は signal-maintenance-offload-worker に分離。本チケットは deploy ops 実装 (T6) の完了で閉じる。
- 2026-10-16: 関連: [signal-maintenance-offload-worker, deployed-plugin-has-no-disable-or-rollback] — signal-maintenance-offload-worker を related に追加
- 2026-10-18: 是正内容 — plugin deploy ops T6 (main 0724a46) で signal maintenance を Scheduler.tick() の遅延 callable にし core_lock の外で実行、専用接続 conn_signal、producer / OPEN の name lock を core_lock と循環しない順序に固定 (AC-34 計装 + 実 Scheduler の event 順序 test)。tick スレッド上の同期実行による次 tick 遅延は signal-maintenance-offload-worker に分離済み

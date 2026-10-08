---
id: outage-stalled-on-broker-daily-rollover-gap
title: 静かな市場で 1m 足が欠けると degraded になる (restricted 状態を追加)
status: 是正済
priority: 高
opened: 2026-09-29
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [outage-stalled-on-broker-daily-rollover-gap]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[outage-stalled-on-broker-daily-rollover-gap] 起票 2026-09-29 (重要度: 高、実機 9/28 21:07 UTC、C0 下書き = `tmp/design-rollover-gap/C0.md`、実測: 欠落は 21:01〜21:08 に離散・最大 4 連続 → 連続 N 本許容案)**: MT5 (OANDA Japan、サーバ UTC+3) は日次ロールオーバー 21:00 UTC 前後に数分ティックが無く、1m 足 21:05・21:06 が bridge にも存在しない (broker 側の欠落)。1m の停滞式 (watermark 21:04 → 期限 21:06:30) が 21:07:08 に `degraded` (epoch 2、15m は健全)、21:09:09 に `recovered_awaiting_resume`、以後 **人間の `data resume` 待ちで 9/29 の欧州〜東京セッション 12 時間以上 cron mission 0 本**。週明け (`ed2de56` で是正) とは別の穴。処置候補 = (a) 日次ロールオーバー窓 (21:00〜21:05 UTC 程度、実測で幅を決める) を market_hours の「既知の無ティック時間」として停滞式・gap 検査から除く (b) 1m の停滞判定に欠落本数の許容 (例: 3 本) を持たせる (c) **建玉ゼロなら自動で ready に戻す** (spec A2-3 §3.1 `ready_confirm_ticks`、段 c) — 2 日連続で「一過性の穴 → 終日停止」になった以上 (c) の優先度は高い。A2-3c 残段の順序見直し (ユーザー裁定 9/29: ① に前倒し)。**分割 (astra 9/29)**: (c) 建玉ゼロの自動復帰 = A2-3c-lite として先行 (`tmp/design-a2/a23c-lite/C0.md`)。窓の除外 (a)(b) は「今が窓内か」ではなく**欠落している足の開始時刻**で判定する規則が要り、`_closed_minutes` は足幅刻みの推定値なので 1h 足で 5 分窓を過大評価し得る → 別設計 (source/足種を明示した「欠落を許容する足」の規則を停滞・gap・連続性検査で共有、0=無効、上限 5 分)

## 修正内容

- 2026-10-02: main af64a11..1079ecb。建玉も未約定指値も無いときの停滞は restricted (新規リスク停止、datafeed.outage.flat_stall_max_sec 既定 1800 秒、健全 3 tick で ready) に留め、建玉あり・取得失敗・空の応答・期限超過は degraded。state と gap は 1 transaction、発注は consume 前・入口・submit 直前で state を再検査、停止判定の保存失敗時も新規リスクを止める。spec docs/superpowers/specs/2026-10-01-quiet-market-restricted-state-design.md v1.2。レビュー: 設計 sol 2 周、実装は変異 66 本 (pin 10)・ローカル LLM 3 本・/code-review high・sol 2 周 + terra 1 周。フルスイート 4992 passed (既知 flake 1)。実データ 3 本 (9/29・9/13・9/30) の遷移を受入テストで固定。実機受入は再起動後のロールオーバーで確認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-01: 状態: 実装中、題名: 静かな市場で 1m 足が欠けると degraded になる (restricted 状態を追加) — spec docs/superpowers/specs/2026-10-01-quiet-market-restricted-state-design.md v1.0。原因は broker が無 tick の分の足を作らないこと (bridge・取得の失敗ではない、60 日実測で USDJPY 96 分・他銘柄は時間帯を問わず)。時間帯規則・固定銘柄群・気配は使わず、建玉なしの停滞は restricted (新規リスク停止、上限 30 分)、建玉ありは現行どおり degraded
- 2026-10-02: 是正内容 — main af64a11..1079ecb。建玉も未約定指値も無いときの停滞は restricted (新規リスク停止、datafeed.outage.flat_stall_max_sec 既定 1800 秒、健全 3 tick で ready) に留め、建玉あり・取得失敗・空の応答・期限超過は degraded。state と gap は 1 transaction、発注は consume 前・入口・submit 直前で state を再検査、停止判定の保存失敗時も新規リスクを止める。spec docs/superpowers/specs/2026-10-01-quiet-market-restricted-state-design.md v1.2。レビュー: 設計 sol 2 周、実装は変異 66 本 (pin 10)・ローカル LLM 3 本・/code-review high・sol 2 周 + terra 1 周。フルスイート 4992 passed (既知 flake 1)。実データ 3 本 (9/29・9/13・9/30) の遷移を受入テストで固定。実機受入は再起動後のロールオーバーで確認

---
id: trade-timeout-on-startup
title: 停止による打ち切りの trade mission が timeout と記録される
status: 設計待ち
priority: 中
opened: 2026-09-28
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [trade-timeout-on-startup]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[trade-timeout-on-startup] 起動直後の cron trade mission が stop 時に timeout で終わる (m41/m43)。 **再観測 2026-09-28: 再起動時に走行中だった cron mission #209 が `status=timeout` (64 秒) で記録された。停止による打ち切りと本物の timeout が区別できないので終端理由を分ける (`stopped` 等) か activity に理由を書く。**improve 並走時の GPU swap と起動直後の停止の両方で起きる — trade 側の timeout / 起動直後の発火抑止を見直す候補

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 中、題名: 停止による打ち切りの trade mission が timeout と記録される — 仕分け (2026-10-02、現物で成立を確認): 再起動時 #209 が 64 秒で timeout、終端理由の分離か activity への理由記載が要る。 根拠: missions の終端 status に stop 由来の区別が無い (store/db.py の CHECK と trade_loop に stopped 無し)。停止での打ち切りが timeout と記録される。

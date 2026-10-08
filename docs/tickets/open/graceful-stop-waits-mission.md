---
id: graceful-stop-waits-mission
status: 設計待ち
priority: 低
opened: null
closed: null
related: [selftest-loop-no-cutoff, mission46-selftest-loop, llama-swap-contention]
backfilled: true
source_section: 未完了
---
# [graceful-stop-waits-mission]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[graceful-stop-waits-mission] (低) graceful stop が走行中 improve mission の timeout (最大 60 分) を待つ (m53 で 17.5 分)。[selftest-loop-no-cutoff] self-test 修正ループ (同一テスト書き換え ≈25 往復) の早期打ち切り無し ([mission46-selftest-loop] と同根)。[llama-swap-contention] trade と improve の取り合い (#54 timeout、継続観測)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


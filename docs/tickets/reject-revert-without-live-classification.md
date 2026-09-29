---
id: reject-revert-without-live-classification
status: 設計待ち
priority: 低
opened: 2026-09-20
closed: null
related: [switch-ops-hardening, switch-ops-hardening]
backfilled: true
source_section: 未完了
---
# [reject-revert-without-live-classification]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[reject-revert-without-live-classification] 起票 2026-09-20 (重要度: 低〜中、[switch-ops-hardening] 1 周目 codex terra/medium の指摘)**: `reject_candidate` (`switch.py:1866-1875`) は自分の未完 journal があると lock 内で `_revert_one` を呼ぶが、live を分類しない (`classify_live` を経由しない)。reject の直前に第三者が live を foreign symlink に差し替えていた場合、それを上書きして旧状態に戻しうる (reconcile には [switch-ops-hardening] で分類ガードを入れたが reject には無い)。出所: `tmp/review-20260920-soh/r1/codex-triage.md`。該当行は `5f93827e` (2026-08-25) 由来で束の diff 範囲外。設計書 v1.6a の IV-6 に「例外 2 つ目」として明記済。論点: reject で foreign を検出したときの決め (reject 自体は成立させ journal を foreign_waiting 相当で残すか、reject を pending 留置にするか) は設計判断を伴うので別束

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。


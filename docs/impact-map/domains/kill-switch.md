---
{"id":"kill-switch","owner_role":"conductor","resources":[],"anchors":[],"related":[],"tests":[]}
---
# 責務 / 入口 / resource / 主な逸脱

試行前 (opsalert merge 後に記入)。

## F（anchor、観測 revision、参考行、test と検証状態）

試行前 (opsalert merge 後に記入)。

## K（ID、入口×遷移、前提、副作用、障害時、正本、F/test）

試行前 (opsalert merge 後に記入)。ID と題名の正本は[承認済み設計 §3.3、§3.5](../../superpowers/specs/2026-11-14-code-impact-map-design.md#33-守る契約確定と未実装を分ける)。

| ID | 題名 |
|---|---|
| KS-LATCH | 自動ラッチ |
| KS-REPEAT | ラッチの繰返し |
| KS-NOTICE | guard 通知 |
| KS-RESET-OPS | ops からの解除 |
| KS-RESET-SHELL | shell からの解除 |
| KS-RECONCILE | marker の確認 |
| KS-START | 起動時の回復 |
| KS-RESET-UI | 解除時の表示 |
| KS-REBASE | HWM の手動 rebase |
| KS-REPLAY | replay の解除と補償 |
| GS-CONSUME | event 配送 |
| GS-VIEW | status と activity の表示 |
| GS-LANE | lane の再入防止 |
| KS-NOTICE-CODES | 通知の固定 code |
| GS-PENDING | 通知待ちの表示 |
| GS-ALTERNATE | 代替通知の表示 |

## D（F/K、公開 ticket、解消条件・残余リスク）

試行前 (opsalert merge 後に記入)。

## 影響先と検証記録

試行前 (opsalert merge 後に記入)。

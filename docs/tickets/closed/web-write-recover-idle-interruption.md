---
id: web-write-recover-idle-interruption
title: 有効でない web の鍵と write の回復が中断に弱い
status: 是正済
priority: 中
kind: 不具合
opened: 2026-11-10
closed: 2026-11-15
related: [web-plugin-improve-hub]
---
# [web-write-recover-idle-interruption] 有効でない web の鍵と write の回復が中断に弱い

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象

afx ops web enable --write / --no-write は、web が有効でない (disabled・未 enable) 間に書込みの鍵と ops.toml の write が食い違っていると、停止と instance lock の下で両者を揃える (web_write.py の _recover_idle、stage file は作らない)。この回復は途中で止まると戻れない: 停止の後・整合の前に止まると、停止前に afx が動いていたことを覚えていないため、再実行しても afx を起こさない。設定を揃えた後・marker を外す前、または marker を外した後・起動の前に止まると、再実行は鍵と設定が整合済みなので not_enabled で終わり、marker の削除と afx の起動に届かない。直し方の案: stage file とは別の durable な回復記録に、回復操作・目標・停止前の afx の状態を残し、再実行では整合済みの拒否より先にそれを処理して、marker の解消と必要な起動まで終えてから記録を消す。あわせて、入口 × 状態の test の表 (tests/ops_setup/test_web_write_switch.py) に、enable の再開の staged・enrolled・started、切替の各段、init の明示行、未 enable の --no-write、この回復の中断点を足す。web 段 B1 の 4 段目の閉鎖確認 4 (sol) の残り

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-15: 有効でない web の write 回復に、停止前の afx の状態だけを持つ回復の記録 (data/ops/web.write-recovery.json、{version, afx_before_stop}) を足した。再実行は記録を先に処理し、鍵と設定の整合、marker の解消、元が稼働なら afx の起動を終えてから記録を消す。記録が残る間は他の setup 操作と init を write_switch_pending で止め、壊れた記録は write_recovery_invalid、遷移中の afx は afx_state_transient で記録を作らない。元が停止で手で稼働中の afx は止めない。直接 afx keys 操作との競合は保証の外。設計 2026-11-15-web-write-recover-idle-design.md (C0.5、レビュー 5 周)、実装レビューは sol 1 周目 (3 件採用)・/code-review high (8 件中 4 採用)・sol 閉鎖 0 件。判定は tests/ops_setup tests/setup tests/ops 6155 緑、status・entry・cli ほか 525 緑

## 経緯

- 2026-11-10: 起票。
- 2026-11-15: 状態: 実装中 — 回復記録 (data/ops/web.write-recovery.json) による再開を実装。中断点・手動操作・入口・wizard の test と変異で確認
- 2026-11-15: 是正内容 — 有効でない web の write 回復に、停止前の afx の状態だけを持つ回復の記録 (data/ops/web.write-recovery.json、{version, afx_before_stop}) を足した。再実行は記録を先に処理し、鍵と設定の整合、marker の解消、元が稼働なら afx の起動を終えてから記録を消す。記録が残る間は他の setup 操作と init を write_switch_pending で止め、壊れた記録は write_recovery_invalid、遷移中の afx は afx_state_transient で記録を作らない。元が停止で手で稼働中の afx は止めない。直接 afx keys 操作との競合は保証の外。設計 2026-11-15-web-write-recover-idle-design.md (C0.5、レビュー 5 周)、実装レビューは sol 1 周目 (3 件採用)・/code-review high (8 件中 4 採用)・sol 閉鎖 0 件。判定は tests/ops_setup tests/setup tests/ops 6155 緑、status・entry・cli ほか 525 緑

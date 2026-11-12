---
id: web-write-recover-idle-interruption
title: 有効でない web の鍵と write の回復が中断に弱い
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-10
closed: null
related: [web-plugin-improve-hub]
---
# [web-write-recover-idle-interruption] 有効でない web の鍵と write の回復が中断に弱い

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

afx ops web enable --write / --no-write は、web が有効でない (disabled・未 enable) 間に書込みの鍵と ops.toml の write が食い違っていると、停止と instance lock の下で両者を揃える (web_write.py の _recover_idle、stage file は作らない)。この回復は途中で止まると戻れない: 停止の後・整合の前に止まると、停止前に afx が動いていたことを覚えていないため、再実行しても afx を起こさない。設定を揃えた後・marker を外す前、または marker を外した後・起動の前に止まると、再実行は鍵と設定が整合済みなので not_enabled で終わり、marker の削除と afx の起動に届かない。直し方の案: stage file とは別の durable な回復記録に、回復操作・目標・停止前の afx の状態を残し、再実行では整合済みの拒否より先にそれを処理して、marker の解消と必要な起動まで終えてから記録を消す。あわせて、入口 × 状態の test の表 (tests/ops_setup/test_web_write_switch.py) に、enable の再開の staged・enrolled・started、切替の各段、init の明示行、未 enable の --no-write、この回復の中断点を足す。web 段 B1 の 4 段目の閉鎖確認 4 (sol) の残り

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-10: 起票。

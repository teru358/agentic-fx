---
id: ctl-improve-return-on-accept
title: afx ctl improve は受理で返り、完了を待たない
status: 設計待ち
priority: 低
opened: 2026-10-25
closed: null
related: [cli-surface-principle-and-reorg]
---
# [ctl-improve-return-on-accept] afx ctl improve は受理で返り、完了を待たない

**状態**: 設計待ち / **優先**: 低

## 現象

`afx ctl improve` が job 受理後に完了を 45 秒待ち、「待ち時間の上限に達しました… state=running」で終わる (2026-10-25 実機)。改善 mission は分単位〜時間単位なので client 側の待ちは意味がなく、ユーザー裁定: 受理後の待機は不要、「job <id> を受理しました」(状態確認コマンドの案内つき) で shell を返してよい。approve / reject / retry の待ち (45 秒) は対象外。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。

---
id: escape-probe-manual-measurement
title: 改善 agent の檻からの脱出可否を人手で実測する
status: 裁定待ち
priority: 中
opened: 2026-10-04
closed: null
related: [sandbox-escape-via-user-daemons-unmeasured]
---
# [escape-probe-manual-measurement] 改善 agent の檻からの脱出可否を人手で実測する

**状態**: 裁定待ち / **優先**: 中

## 現象

親チケット sandbox-escape-via-user-daemons-unmeasured は『測らずに塞ぐ』方針 (2026-10-04 案 1)。実測そのものは agent (subagent も指揮者も) が安全分類器に止められて実行できない。塞ぐ前の事実の記録と、塞いだ後の効きの確認のために、人手での実測を別チケットとして残す。

## 原因

稼働中のデーモン (session bus / systemd --user / screen) を相手にする脱出試験は agent が行えない。

## 処置案・裁定

利用者が手で行う。詳細な手順は着手時に詰める (安全分類器の都合で agent が手順書を書けないため、骨子のみ: 改善 worker と同じ Landlock を掛けた子から、同 uid のデーモン socket への到達可否を、使い捨て対象に対してだけ測る。稼働中の afx には触れない)。塞いだ後は、塞ぎが効いて到達不能になったことの確認に使う。

## 修正内容

## 経緯

- 2026-10-04: 起票。

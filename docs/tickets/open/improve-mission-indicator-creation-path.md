---
id: improve-mission-indicator-creation-path
title: 改善 mission に indicator 新設の経路を開く (規律 7 と方針の衝突)
status: 実装中
priority: 中
opened: 2026-10-25
closed: null
related: [improve-objective-filtered-signal-quality, improve-run-summary-for-humans]
---
# [improve-mission-indicator-creation-path] 改善 mission に indicator 新設の経路を開く (規律 7 と方針の衝突)

**状態**: 実装中 / **優先**: 中

## 現象

ユーザー方針「必要な指標が無ければ kind=indicator の plugin を新設して検証に使う」を 3 件入れて improve を 3 回回しても、候補は既存 12 指標の strategy だけで新指標は作られなかった (2026-10-25)。原因: `loops/prompts/improve_mission.md` の規律 7「無い指標は自前計算せず `不足指標:` として discoveries に起票」が方針より強く読まれ、加えて open の課題がすべて strategy 形で indicator を作る課題が無い。設計上は artifact `kind: indicator` を提出でき (提出床 PF は strategy だけ、indicator は self-test → 人間承認 → 配備 → 後続 mission の strategy が依存)、入口の文言と導線が無いだけ。案: (1) 規律 7 を「strategy の中で自前計算しない。不足指標は indicator plugin として候補にできる (同 mission で strategy と同時提出は不可、承認後に別 mission で使う)」に改める、(2) 族表 / backlog 表に未配備 indicator の作成経路 (`不足指標` 起票 → 次 mission で indicator 候補) を明示、(3) ユーザー方針節を規律より前に置くか「方針は規律の範囲で優先」と書く、(4) indicator 候補の最終出力例と self-test の要件を prompt に 1 段落。改善周の実測 (AC-20 と同じ観測枝) で、indicator 候補が 1 件以上出ることを確認する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: 状態: 実装中 — 2026-10-25: feedback spec v0.4 §9.5 で設計、prompt と pin test を実装 (terra)、判定 tests/loops + integration + 直下 2766 green。AC-32 (方針文だけで indicator 候補が出るか) は次の手動 improve と 10/31 週次で観測。本番反映は次の daemon 再起動 (prompt は起動時に読む)

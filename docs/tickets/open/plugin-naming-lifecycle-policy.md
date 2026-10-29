---
id: plugin-naming-lifecycle-policy
title: plugin の命名・引退・保有上限の方針 (同名更新か新名作成か)
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-27
closed: null
related: [approval-base-identity-cas, indicator-update-dependent-relock-flow]
---
# [plugin-naming-lifecycle-policy] plugin の命名・引退・保有上限の方針 (同名更新か新名作成か)

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

改善で戦略・指標を同名で更新するか新名で作るかの方針が無い。利用者から「新しく作るほうが影響が少なく版管理の必要性が薄れる」との提起 (2026-10-27)。sol の評価: 新名を原則にするのは不向き (duplicate_logic / family_redirect が同名更新へ誘導、全 admitted strategy が signal 評価対象で増殖は runtime・検証量を増やす)。推奨は「同一仮説・互換修正は同名更新、意味・outputs・依存契約が変わるもの・並行比較するものは新名」。新名方式でも hash 固定と版管理は要る (承認 identity・signal 監査・rollback が依存)。引退 (retire) と inventory 上限も含めて決める。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
- 2026-10-29: 優先: 中 — 2026-10-29 ユーザー方針 (裁定候補、設計 C0 で確定): (1) 名前の線引き = 使う指標 plugin の集合。同じ集合上の閾値・params・論理の変更は同名の更新 (配備版との比較 gate の対象)。指標を入れ替える・足す・減らすなら新しい名前で、承認依頼に derived_from: <元の名前>@<hash> を記録し承認画面に元の成績を参考として並べる (却下根拠には使わない)。close_pressure_followthrough #72→#74 (reversal_capture_ratio を rolling_wick_asymmetry に入れ替え) はこの線では新名側。(2) 週ごとの棚卸し: 週次改善の冒頭で未使用の指標 (配備中の戦略が参照していないもの、2026-10-29 時点で 50 本中 41 本) と成績の悪い戦略 (weekly-improve-rebacktests-deployed-strategies の前方成績) を Discord に「停止の提案」として通知する。停止 (disable) / 引退 (retire) は人間が afx で行い、自動では停止しない。削除はしない (版は .versions/ に残る)。停止済みの名前は改善 prompt に出して作り直しを抑止する。(3) 指標は params 付き (EMA の期間 7 / 14 は同じ plugin) なので、既存の指標の params で表せるものは新規の指標として提出させない (prompt の規律 + noop_gate / 同一性検査の拡張)。

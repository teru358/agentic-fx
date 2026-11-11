---
id: improve-eval-window-reference-availability
title: 参照系列を宣言した候補の評価区間に、参照の履歴が無い先頭の足が入る
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-09
closed: null
related: [improve-backtest-untradable-pair-and-skip-readability]
---
# [improve-eval-window-reference-availability] 参照系列を宣言した候補の評価区間に、参照の履歴が無い先頭の足が入る

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

評価区間の始点は主 pair の最古足だけで決まり、宣言した参照系列の履歴の開始を見ない。参照の履歴が主 pair より遅く始まると、先頭の足が missing で除外される (USDJPY 主軸 + US500 参照で先頭 12.2%)。基準線はその区間も評価するので、候補と基準線の判断の機会が揃わない。始点を候補ごとに切り詰めると、候補が参照の選び方で期間を間接に選べる・頻度の床の分母 eval_bars が変わる・4h/1d の完全な bucket と確定足の規則で raw の最古足からは使えない・source/base_interval の扱い・再評価で同じ始点が出ない、の問題がある。ハーネスが事前に固定する共通の評価区間 (raw 履歴の区間・判断できる区間・頻度の分母・候補と基準線の比較区間) として設計する。経緯は spec 2026-11-09-improve-backtest-skip-readability-design.md の §1 C

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-09: 起票。

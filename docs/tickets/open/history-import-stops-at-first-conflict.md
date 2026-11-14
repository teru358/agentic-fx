---
id: history-import-stops-at-first-conflict
title: history import は食い違う足 1 本で partial 停止し後続を取り込まない
status: 実装中
priority: 中
kind: 不具合
opened: 2026-11-13
closed: null
related: [live-confirmed-bars-into-history]
---
# [history-import-stops-at-first-conflict] history import は食い違う足 1 本で partial 停止し後続を取り込まない

**状態**: 実装中 / **優先**: 中 / **種別**: 不具合

## 現象

afx history import --source mt5 は既存行と OHLC が食い違う足に当たると partial として止まり、それ以降の足を取り込まない。2026-11-11 に参照 4 銘柄で 11-03 13:45 の 1 本が食い違い、11-04 以降が取り込まれず「端末に足が無い」と誤認した (2026-11-13 に食い違いの後から取り直して解消)。出力は partial inserted=0 unchanged=N conflicted=1 で、止まった位置と残りの範囲が示されない。直し方の案: 食い違いは既存行を残して記録し、後続の取込みは続ける。止まる場合は止まった足と未取込の範囲を表示する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。
- 2026-11-14: 状態: 実装中 — 設計 C2.1 を 2026-11-14 に利用者承認 (docs/superpowers/specs/2026-11-14-history-status-and-import-conflicts-design.md)。afx status は locked-by: unknown のときだけ評価用履歴を mode=ro で読み、他の保持者は従来の 6 行。参照節は watch_symbols を正本に「holdout 区間の 0 本日」として出し、ready なら next を出さない。import は衝突した足の既存値を残して history_conflicts に記録し続行、衝突が 1% 超かつ 2 件以上、または 20 件超ならその系列を 0 件にする遮断器。書込みは batch 単位で確定し再実行で続きを取る。同期 (段 2・3) はこの書込み契約を使う。

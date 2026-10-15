---
id: wizard-bridge-check-plain-http-auth-required
title: ウィザードの bridge 検査は平文 http の鍵つき bridge で必ず auth_required
status: 裁定待ち
priority: 中
opened: 2026-10-14
closed: null
related: [first-run-setup]
---
# [wizard-bridge-check-plain-http-auth-required] ウィザードの bridge 検査は平文 http の鍵つき bridge で必ず auth_required

**状態**: 裁定待ち / **優先**: 中

## 現象

段 3 分岐 1 で URL が平文 http (非 loopback) のとき、ウィザードは鍵を送らない (是正の裁定) が、検査は鍵が要る endpoint まで叩くので、API キーを有効にした LAN の bridge は必ず auth_required になる。案内文「afx 側の .env に MT5_BRIDGE_API_KEY を書いてから再試行」は、再試行しても鍵を送らないので誤誘導。回避は [2]「使う予定・未準備」で進む (daemon 本体は .env の鍵を送るので起動後は動く)。要裁定: (a) private アドレス (RFC1918) の平文 http には鍵を送る、(b) 鍵を送らないまま /health 200 + auth_required を「鍵は daemon が送る」として price: unverified (reason 固定語) で進める、のどちらか。2026-10-14 stream-svr 受入 1 回目で発見

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。
- 2026-10-14: 状態: 裁定待ち — 受入 1 回目の回避は [2] で進む
- 2026-10-15: 優先: 中 — 受入 2 回目 (2026-10-15): [2] planned で進めた後、daemon は .env の鍵で bridge を読み afx ctl status は data: ready なのに、未完手順には「MT5 bridge はまだつながっていません」が残る。pending_steps が記録の bridge_plan=planned だけを見て daemon の実観測 (data_state=ready) を見ていない。是正時は「record が planned でも data_state が ready なら出さない」を足す

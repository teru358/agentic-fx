---
id: ops-web-502-stuck-cause-unknown
title: web が Service unavailable (502) を返し続けた原因が未確定
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-10-30
closed: null
related: [ops-web-head-request-loop-error]
---
# [ops-web-502-stuck-cause-unknown] web が Service unavailable (502) を返し続けた原因が未確定

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

2026-10-29 22:14 以降 (推定) から 10-30 19:41 の afx-ops@web 再起動まで、認証付き GET が 502 を 0.8 ms で返し続けた。再起動で復旧。当時の web は 502 の理由を stderr に出さなかったので記録が無い。slot 帳簿の漏れ・interrupt の残留・MAX_CONNECTIONS は再現試験 (3 KB 順次 60 回、100 KB 並列 400 本、RST 300 本) で否定。残る候補は daemon 呼出しの admission 失敗 (lock device / pidfd) か daemon の非 200。ops-web-head-request-loop-error の是正で web_upstream reason= / status= / shape= / route= の固定語が stderr に残るようになったので、次に起きたら journalctl --user -u afx-ops@web で web_upstream 行を見て原因を確定する

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-30: 起票。

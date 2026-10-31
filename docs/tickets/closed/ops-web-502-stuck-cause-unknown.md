---
id: ops-web-502-stuck-cause-unknown
title: web が Service unavailable (502) を返し続けた原因が未確定
status: 是正済
priority: 高
kind: 不具合
opened: 2026-10-30
closed: 2026-10-31
related: [ops-web-head-request-loop-error]
---
# [ops-web-502-stuck-cause-unknown] web が Service unavailable (502) を返し続けた原因が未確定

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

2026-10-29 22:14 以降 (推定) から 10-30 19:41 の afx-ops@web 再起動まで、認証付き GET が 502 を 0.8 ms で返し続けた。再起動で復旧。当時の web は 502 の理由を stderr に出さなかったので記録が無い。slot 帳簿の漏れ・interrupt の残留・MAX_CONNECTIONS は再現試験 (3 KB 順次 60 回、100 KB 並列 400 本、RST 300 本) で否定。残る候補は daemon 呼出しの admission 失敗 (lock device / pidfd) か daemon の非 200。ops-web-head-request-loop-error の是正で web_upstream reason= / status= / shape= / route= の固定語が stderr に残るようになったので、次に起きたら journalctl --user -u afx-ops@web で web_upstream 行を見て原因を確定する

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-31: Landlock を掛ける前に /proc/<pid> と /proc/locks を O_PATH で開いたまま保持 (規則はその fd に付ける)、admission 失敗の stderr 行に step と errno を添える (閉じた語彙)、fdinfo の ENOMEM 系は pidfd_transient。レビュー: codex sol r1 + /code-review high + codex terra 閉鎖。受入: 配備後にユーザーの drop_caches=2 で web / bot が壊れないことを確認 (2026-10-31)

## 経緯

- 2026-10-30: 起票。
- 2026-10-31: 状態: 実装待ち、優先: 高、種別: 不具合 — 原因確定 (2026-10-31): Landlock の path 規則は procfs の inode に付くため、/proc/<pid> と /proc/locks の dentry がキャッシュ回収 (メモリ逼迫、drop_caches=2) で捨てられて作り直されると規則が死ぬ。以後 fdinfo / locks の open が EACCES になり、admission が ops_requires_so_peerpidfd / not_lock_holder に固定される。規則は追加できないので再起動まで直らない。drop_caches=2 で本番 web / bot が 3 回とも 2 秒以内に壊れ、O_PATH で 2 つの path を開いたまま持つ pin 付きプロセスは生き残った (pin 無しは EACCES)。是正: 起動時に /proc/<pid> と /proc/locks を O_PATH で開いたまま保持、観測語に errno と段を添える、pidfd_pid の ENOMEM 系は transient に。
- 2026-10-31: 是正内容 — Landlock を掛ける前に /proc/<pid> と /proc/locks を O_PATH で開いたまま保持 (規則はその fd に付ける)、admission 失敗の stderr 行に step と errno を添える (閉じた語彙)、fdinfo の ENOMEM 系は pidfd_transient。レビュー: codex sol r1 + /code-review high + codex terra 閉鎖。受入: 配備後にユーザーの drop_caches=2 で web / bot が壊れないことを確認 (2026-10-31)

---
id: unit-placement-explain-missing-user-session
title: unit 設置で systemctl --user が使えない理由 (su でセッション無し) を文面に出す
status: 実装中
priority: 低
opened: 2026-10-14
closed: null
related: [first-run-setup]
---
# [unit-placement-explain-missing-user-session] unit 設置で systemctl --user が使えない理由 (su でセッション無し) を文面に出す

**状態**: 実装中 / **優先**: 低

## 現象

su - <user> や sudo -u で起動した端末では logind のセッションが無く XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS が無いため systemctl --user が bus に接続できず、段 13 は「systemd のユーザーサービスが使えません。手で起動するには…」で unit を置かずに終わる。文面が原因 (環境変数が無い = 直接ログインしていない) を言わないので利用者は環境の問題だと分からない。是正: systemctl_available の失敗時に XDG_RUNTIME_DIR の有無を見て「ユーザーセッションの環境変数が無い。su ではなく <user> で直接ログインして afx init を再実行」と出す (reason 固定語を 1 つ追加)。手順書の前提に「対象ユーザーで直接ログイン (su 不可)」を 1 行。また unit の記録が unit: not_installed だけで reason が落ちているなら reason_code も記録に写す。2026-10-14 stream-svr 受入 1 回目で発見 (手動の fallback 文の uv run main.py も README の uv run afx と揃える)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。
- 2026-10-14: 状態: 実装待ち — 原因確定 (2026-10-14 ユーザー申告): ウィザードは su でユーザーを切り替えた端末から起動していた。stream-svr で XDG_RUNTIME_DIR を外すと systemctl --user show-environment が「Failed to connect to bus」で失敗することを再現済み
- 2026-10-15: 状態: 実装中 — 束 fr-fix2 (spec v1.8、ブリーフ tmp/review-20261011-sc/fr-fix2-1.md / fr-fix2-2.md)

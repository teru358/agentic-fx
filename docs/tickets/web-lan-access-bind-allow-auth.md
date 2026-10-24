---
id: web-lan-access-bind-allow-auth
title: 同梱 web を LAN から見られるようにする (bind / allow / auth の 3 設定)
status: 実装中
priority: 中
opened: 2026-10-23
closed: null
related: [ops-ui, web-ui-charts-equity-indicators]
---
# [web-lan-access-bind-allow-auth] 同梱 web を LAN から見られるようにする (bind / allow / auth の 3 設定)

**状態**: 実装中 / **優先**: 中

## 現象

同梱の読み取り専用 web は 127.0.0.1 固定・認証なし (afx-ops spec v1.6 §3.3、§12 裁定 1)。afx をサーバーで運用して別 PC から確認したいとき、nginx や ssh 転送を利用者に要求するのは労力がかかりすぎる (2026-10-23 ユーザー)。裁定 (2026-10-23): ある程度のセキュリティは用意し、運用は利用者の自己責任。localhost のみ / LAN 内 / ホワイトリスト / 簡易認証 の運用形を提供する。設計骨子 (sol 第二意見 2 回を反映): ops.toml の web 節に bind (既定 127.0.0.1、LAN は本機の具体的 IPv4 のみ、0.0.0.0 と IPv6 は初版不可)、allow (接続元 IPv4 / CIDR の許可リスト、非 loopback では必須、無制限は 0.0.0.0/0 の明示値 + 固定警告)、auth (none | basic、basic のとき secret web.password 必須)。allow は accept 直後に照合し外れは黙って close。評価順 allow → parse → Host 完全一致 → auth → route。rate limit は固定 (送信元 5 回/60 秒、全体 100 回/60 秒、429)。ウィザードは既定 localhost、LAN 時の既定は許可 IP だけ、候補列挙は ip -j addr。残余 (平文 HTTP、DHCP 変動、侵害済み LAN 端末、TLS / IPv6 / reverse proxy) は利用者責任として文書化。amendment spec を正本にし旧 spec の §3.3 / §12 / 設定 schema / wizard / probe / O-11 / AC-10 を置換。規模 M、T1 (bind + allow + Host + probe) → T2 (auth + rate limit + ウィザード)

## 原因

## 処置案・裁定

正本は設計書 `docs/superpowers/specs/2026-10-23-web-lan-access-design.md` (v0.9、2026-10-24 収束)。task 表は同 §10.2。

## 修正内容

## 経緯

- 2026-10-23: 起票。
- 2026-10-23: 状態: 設計待ち — C0 を sol に下書きさせ、sol レビュー → ユーザー確認の順
- 2026-10-24: 状態: 実装待ち — 設計書 docs/superpowers/specs/2026-10-23-web-lan-access-design.md v0.9 で収束 (設計レビュー 6 周 + 差分閉鎖確認 3 周、ユーザー裁定 C' = enable 時の要約 + 1 問、afx に触れない reconfigure は T2、コマンド階層 afx ops <feature> <verb>、reconfigure + 別名 reconf)。T1 (bind + allow + Host + probe + 残骸検査、afx-ops と ops_setup writer) → T2 (auth + rate limit + ウィザード + reconfigure) の順で実装
- 2026-10-24: 状態: 実装中 — T1 (bind + allow + Host 検査 + probe + bounded interface 列挙 + none の secret 残骸検査 + 本体 writer の web 4 key) を実装。codex r1 Medium 3 / /code-review high 8 件 / 閉鎖確認 2 周を是正して merge。設計書 v0.9a (接続単位 errno 11 種を続行、accept_failed に rc 74、reply 形不一致は 502)。判定: afx-ops 2716 / 本体 10717 + 1054 + 398 green。残り T2 (Basic 認証、rate limit、ウィザード、--ask、status、reconfigure、運用文書)

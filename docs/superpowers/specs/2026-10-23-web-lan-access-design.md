# 同梱 web の公開範囲設計

**版:** v0.4
**対象:** チケット `web-lan-access-bind-allow-auth`
**位置づけ:** afx-ops spec v1.6 の web 公開範囲を置き換える amendment spec である。

## 0. 目的、範囲、決定

同梱 web は現在 `127.0.0.1` 固定で待ち受ける。
サーバー上の afx を別 PC から確認するには、利用者が nginx 又は SSH 転送を別途用意する必要がある。
本設計は読み取り専用という境界を保ったまま、具体的な LAN IPv4 での待受、接続元の制限、Basic 認証を製品内に用意する。

ユーザー裁定の原文は「ある程度のセキュリティは用意し、運用は利用者の自己責任。localhost のみ / LAN 内 / ホワイトリスト / 簡易認証 の運用形を提供する。」である。
この裁定は 2026-10-23 に行われた。
本 spec は旧 spec `2026-10-09-afx-ops-design.md` の §12 裁定 1 を置換する。

| 項目 | 決定 |
|---|---|
| 設定軸 | `bind`、`allow`、`auth` の 3 軸を独立した明示設定として扱う。 |
| 既定 | `bind = "127.0.0.1"`、`allow` 省略、`auth = "none"` とする。 |
| LAN 待受 | 本機の up 状態の interface に実在する具体的な数値 IPv4 だけを許す。 |
| LAN の接続元 | 非 loopback では `allow` を必須にする。無制限は省略でなく `allow = ["0.0.0.0/0"]` と明示する。 |
| 認証 | `auth = "none" | "basic"` とする。`basic` は固定ユーザー名 `afx` と secret `web.password` を使う。 |
| 拒否順 | `allow → 有界 parse → Host → auth → route / daemon` とする。 |
| 既存の安全境界 | GET / HEAD の読み取り専用 route、`ops-web` principal、隔離後の新規 INET socket 拒否を維持する。 |
| 実装の分割 | T1 は bind、allow、Host、probe、config、writer に加え、`auth = "none"` の secret dir / 未知 entry / 残骸 password 検査を実装する。T2 は `auth = "basic"` の password 読取りと比較、rate limit、ウィザード、無制限 LAN、status、運用文書を実装する。 |

範囲外は次のとおりである。

- `0.0.0.0` への bind。
- IPv6 の bind、allow、接続元。
- TLS 終端と証明書管理。
- reverse proxy と proxy header。
- firewall の設定と検証。
- cookie session、login route、logout route。

## 1. 用語

| 用語 | 定義 |
|---|---|
| loopback | IPv4 loopback 範囲 `127.0.0.0/8` である。既定値は `127.0.0.1` である。 |
| 非 loopback | loopback と `0.0.0.0` を除く数値 IPv4 である。待受には本機の up 状態の interface に実在するアドレスだけを使える。 |
| bind | listener が待ち受ける単一のローカル IPv4 である。接続元の制限ではない。 |
| allow | TCP peer の IPv4 を照合する IPv4 network の集合である。HTTP header は照合に使わない。 |
| unrestricted | `allow = ["0.0.0.0/0"]` を明示した状態である。`allow` の省略又は空 list を意味しない。 |
| auth | HTTP Basic 認証を使うかを表す列挙値である。secret file の有無から推測しない。 |
| web principal | daemon API の `ops-web` principal である。読み取り scope だけを持つ。 |

代表する運用形は次の 5 つである。
loopback では §2.3 の範囲で allow 又は auth を追加してもよい。

| 運用形 | `bind` | `allow` | `auth` |
|---|---|---|---|
| localhost | `127.0.0.1` | 省略 | `none` |
| LAN 無制限 | 本機の具体的な非 loopback IPv4 | `["0.0.0.0/0"]` | `none` |
| ホワイトリスト | 本機の具体的な非 loopback IPv4 | 1 件以上の限定 IPv4 / CIDR | `none` |
| 簡易認証 | 本機の具体的な非 loopback IPv4 | `["0.0.0.0/0"]` | `basic` |
| 両方 | 本機の具体的な非 loopback IPv4 | 1 件以上の限定 IPv4 / CIDR | `basic` |

## 2. 設定

### 2.1 `ops.toml` の grammar

web feature 固有の `ops.toml` は既存の `port` に加えて、同じ top-level に `bind`、`allow`、`auth` を持つ。
個別の設定 file と `[web]` table は増やさない。

| key | 型と既定 | grammar |
|---|---|---|
| `port` | integer、既存 key | production では `1..65535` とする。`0` は引き続き拒否する。 |
| `bind` | string、既定 `127.0.0.1` | 十進 dotted-quad の数値 IPv4 だけを許す。hostname、IPv6、IPv4-mapped IPv6、`0.0.0.0`、余分な空白、先頭ゼロを含む表記を拒否する。非 loopback は本機の up 状態の interface に実在する unicast address だけを許し、scope は `global` 又は `link` とする。 |
| `allow` | string の list、loopback では省略可 | 各要素は canonical な数値 IPv4 又は canonical な IPv4 CIDR とする。単一 IPv4 は内部で `/32` として扱う。host bit を持つ CIDR、空文字、空 list、重複 network、IPv6、IPv4-mapped IPv6、余分な空白を拒否する。`0.0.0.0/0` は singleton list `allow = ["0.0.0.0/0"]` の場合だけ受理し、他の network と混在する list は `config_invalid` で拒否する。非 loopback では key 自体を必須にする。無制限はこの singleton の明示値だけで表す。 |
| `auth` | string、既定 `none` | `none` 又は `basic` だけを許す。大文字小文字の別表記は許さない。 |

`bind` と CIDR は `ipaddress.IPv4Address` / `IPv4Network(..., strict=True)` と再文字列化した結果が入力と一致する形を canonical とする。
単一 IPv4 の allow 要素は CIDR でないため、そのまま受理して内部で `/32` に正規化する。
writer は list の順序を保ち、読み直したときに同じ意味の値を返す。

`port` は待受 port と Host 検査の port の両方に使う。
`bind` の省略は旧い port-only 設定を安全な localhost 設定として読めるようにする。
`auth` の省略も旧い設定を `none` として読めるようにする。
非 loopback では `allow` の省略を既定化しない。

### 2.2 password secret

設定収集が返す `FeaturePlan` は、許可する secret 名の集合と、その起動で必須な secret 名の集合を別 field に持つ。
web で許可する名前は常に `{"web.password"}` に固定し、必須集合は grammar と組み合わせを検証済みの `auth` からだけ導く。
`auth = "basic"` なら必須集合を `{"web.password"}`、`auth = "none"` なら空集合とする。
secret の有無から `auth` 又は必須集合を推測しない。

`auth = "none"` では secret dir の不在を許可する。
secret dir が存在する場合は owner 所有の mode `0700` を検査し、その検査済み dir fd から `web.password` の有無を確認する。
`web.password` 以外の entry は許可せず `secret_invalid` とする。
`web.password` は owner 所有の通常 file、mode `0600` ちょうど、size 16〜128 byte とし、`O_NOFOLLOW` で一度だけ読む。
symlink、他 uid、mode 違反、非通常 file、size 違反、read 失敗を `secret_invalid` で拒否する。
検査済み dir fd と password fd は listener の pre-bind、不要 fd 整理、Landlock 適用のいずれよりも前に閉じる。
secret dir は隔離後の Landlock 規則へ加えない。

`auth = "none"` で安全な `web.password` が存在する場合は、固定 reason `secret_unexpected` で起動と probe を拒否する。
残骸 file を黙って無視すると、利用者が認証済みだと思ったまま無認証で公開する事故が起こるためである。
secret の存在だけで `auth` を `basic` に変えることもしない。

password は 16〜128 byte の visible ASCII (`0x21..0x7e`) とし、`:` を許可する。
space、tab、改行、他の C0 / DEL、非 ASCII、16 byte 未満、128 byte 超を拒否する。
生成値は CSPRNG の random 16 byte を padding なし base64url で表した 22 byte とする。

### 2.3 組み合わせの妥当性

この表は §2.4 の決定木そのものであり、上から順に評価して最初に一致した 1 行だけを最終結果とする。表外の override は設けない。
軸は `artifact ∈ {T1,T2}`、`bind ∈ {loopback,nonloop-local,nonloop-absent,lookup-failed}`、`allow ∈ {省略,空,限定,unrestricted,mixed-unrestricted}`、`auth ∈ {none,basic}`、`secret ∈ {無し,安全,不安全}` である。
`nonloop-local` は列挙に成功して bind が存在する状態、`nonloop-absent` は列挙に成功して bind が存在しない状態、`lookup-failed` は §5.1 の列挙規則で局所性を決定できない状態を表す。
`unrestricted` は `0.0.0.0/0` だけの singleton、`mixed-unrestricted` は `0.0.0.0/0` と他 network が混在する grammar 違反を表す。
`secret = 無し` は secret dir 不在又は安全な空 dir、`安全` は安全な dir に grammar を満たす `web.password` が 1 個だけある状態、`不安全` は dir 自体、password 又は未知 entry のいずれかが安全性検査に違反する状態を表す。
`任意` と集合表記を含む行はその軸の全値を被覆する。先行行を除外して読むため行同士は重ならず、上記 2 × 4 × 5 × 2 × 3 の全直積は必ずただ 1 個の最終結果へ到達する。

| 段 | artifact | bind | allow | auth | secret | 最終結果 |
|---:|---|---|---|---|---|---|
| 1 | 任意 | 任意 | 空又は mixed-unrestricted | 任意 | 任意 | `config_invalid` |
| 2 | 任意 | nonloop-local / nonloop-absent / lookup-failed | 省略 | 任意 | 任意 | `allow_required` |
| 3 | T1 | 任意 | unrestricted | 任意 | 任意 | `unrestricted_not_supported` |
| 4 | T1 | 任意 | 省略又は限定 | `basic` | 任意 | `auth_not_supported` |
| 5 | 任意 | lookup-failed | 限定又は unrestricted | 任意 | 任意 | `interface_lookup_failed` |
| 6 | 任意 | nonloop-absent | 限定又は unrestricted | 任意 | 任意 | `bind_not_local` |
| 7 | T1 / T2 | loopback | 省略又は限定、T2 では unrestricted も含む | `none` | 不安全 | `secret_invalid` |
| 7 | T1 / T2 | nonloop-local | 限定、T2 では unrestricted も含む | `none` | 不安全 | `secret_invalid` |
| 7 | T2 | loopback | 省略、限定又は unrestricted | `basic` | 不安全 | `secret_invalid` |
| 7 | T2 | nonloop-local | 限定又は unrestricted | `basic` | 不安全 | `secret_invalid` |
| 8 | T2 | loopback | 省略、限定又は unrestricted | `basic` | 無し | `secret_missing` |
| 8 | T2 | nonloop-local | 限定又は unrestricted | `basic` | 無し | `secret_missing` |
| 8 | T1 / T2 | loopback | 省略又は限定、T2 では unrestricted も含む | `none` | 安全 | `secret_unexpected` |
| 8 | T1 / T2 | nonloop-local | 限定、T2 では unrestricted も含む | `none` | 安全 | `secret_unexpected` |
| 9 | T1 / T2 | loopback | 省略又は限定、T2 では unrestricted も含む | `none` | 無し | 許可 |
| 9 | T1 / T2 | nonloop-local | 限定、T2 では unrestricted も含む | `none` | 無し | 許可 |
| 9 | T2 | loopback | 省略、限定又は unrestricted | `basic` | 安全 | 許可 |
| 9 | T2 | nonloop-local | 限定又は unrestricted | `basic` | 安全 | 許可 |

AC-12 の期待値 fixture は、手書きで reason を重ねず、この表の軸と先行一致規則から機械生成する。

loopback で allow を省略した場合、内部 allow は `127.0.0.0/8` とする。

allow と auth は loopback でも任意に強化できる。
非 loopback では allow と auth のどちらか一方だけを必須にするのではなく、allow を常に必須にする。
auth は allow の代わりではない。

### 2.4 固定 reason

外部へ返す web 固有 reason と rc は次の集合に固定する。
設定値、IP、CIDR、path、header、secret、例外文は reason の後ろへ付けない。

| reason | rc | 条件 |
|---|---:|---|
| `config_invalid` | 71 | TOML、型、未知 key、`port`、`bind`、`allow`、`auth` の grammar が不正である。`0.0.0.0/0` と他 network の混在を含む。 |
| `allow_required` | 71 | 非 loopback で `allow` key が無い。 |
| `unrestricted_not_supported` | 71 | T1 単独版で `allow = ["0.0.0.0/0"]` を使おうとした。 |
| `auth_not_supported` | 71 | T1 単独版で `auth = "basic"` を使おうとした。 |
| `interface_lookup_failed` | 71 | §5.1 の stdlib interface 列挙で `if_nameindex()`、flags 取得、address 取得の想定外 errno、又は address 取得済み interface の netmask 取得 / 検査に失敗し、bind の局所性を判定できない。IPv4 未設定を表す固定 errno は失敗に含めない。 |
| `bind_not_local` | 71 | 数値 IPv4 としては正しい非 loopback bind が、本機の up 状態の interface の global / link unicast IPv4 に存在しない。 |
| `secret_invalid` | 71 | 存在する secret dir 又は `web.password` の owner、mode、型、symlink、size、byte grammar、open / read 検査に失敗したか、許可外の secret 名が存在する。 |
| `secret_missing` | 71 | `auth = "basic"` だが安全な `web.password` が無い。secret dir 不在も含む。 |
| `secret_unexpected` | 71 | `auth = "none"` で安全な `web.password` が存在する。 |
| `listener_invalid` | 71 | production 起動の bind / listen 又は listener 自己検査に失敗した。 |
| `probe_bind_failed` | 73 | probe の実 bind / listen 又は `getsockname()` 一致に失敗した。 |
| `accept_exhausted` | 72 | `EMFILE` / `ENFILE` が accept syscall 成功を挟まず 200 回連続したため listener を閉じる。既存契約を維持する。 |

複数の不正が同時にある場合は、次の決定木で最初に該当した reason だけを返す。

1. 既存契約の config dir 不在 / unsafe を `config_dir_missing` / `config_dir_invalid` とする。
2. TOML と各 key の単独 grammar 違反を `config_invalid` とする。
3. 非 loopback の allow key 欠落を `allow_required` とする。
4. T1 では unrestricted、basic の順に capability を検査し、それぞれ `unrestricted_not_supported`、`auth_not_supported` とする。
5. §5.1 の stdlib interface 列挙自体の失敗を `interface_lookup_failed`、列挙成功後の不在を `bind_not_local` とする。loopback は列挙しない。
6. 存在する secret dir / entry の安全性と password grammar を先に検査し、違反を `secret_invalid` とする。その後に検証済み `auth` に対する欠落を `secret_missing`、余剰を `secret_unexpected` とする。
7. production listener の作成 / 自己検査を `listener_invalid`、probe の実 bind / listen / 自己検査を `probe_bind_failed` とする。

起動と probe は 1〜6 を同じ関数と順序で評価するため、同じ入力には同じ固定 reason を返す。probe だけが最後の実 bind 失敗を `probe_bind_failed` とする。

T1 専用の capability 2 reason は T2 で到達不能になる。
既存の `config_dir_missing`、`config_dir_invalid`、`startup_internal_error` は rc 71 と従来の意味を維持する。
web の secret 検査には汎用の `secrets_failed` を返さず、上表の 3 reason を使う。

## 3. 接続の処理順

### 3.1 allow

`AcceptLoop.accept_ready()` は readiness callback 1 回につき最大 64 接続だけを accept し、残りは次の event-loop tick へ譲る。
readiness 開始時に accept queue 上で許可接続より前に `Q` 件あるなら、その許可接続は最大 `ceil((Q + 1) / 64)` 回の readiness callback で runtime callback へ渡す。
accept 処理と同時に ready になった deadline callback は次の event-loop tick までに実行できなければならない。
accept 直後に accepted socket の `getpeername()` を呼ぶ。
返った peer が 2 要素の IPv4 tuple であることを確認する。
peer IPv4 を正規化済み allow network と照合する。
loopback で allow key を省略した場合は内部 allow `127.0.0.0/8` と照合する。

照合に外れた接続は応答を送らずに close する。
`getpeername()` の失敗又は想定外の形も応答を送らずに close する。
この経路では要求 byte を読まない。
この経路では `_Slot`、aiohttp protocol、transport、worker future を生成しない。
この経路では daemon 呼出しを 0 回にする。

allow を通った接続だけを既存の接続枠上限へ渡す。
現在の枠上限は 16 接続である (`afx-ops/afx_ops/web/runtime.py:33`)。
allow 外の接続はこの 16 枠を消費しない。

accept callback の契約は `(socket, canonical_peer_ipv4)` とする。
runtime は canonical peer IPv4 を `_Slot` の生成時に不変値として保存する。
auth と rate limit はこの値だけを使い、socket、proxy header、Host その他の HTTP header から peer を再導出しない。

peer reject と peer lookup failure はそれぞれ専用 counter だけを増やす。
callback error、`EMFILE` その他の accept error の連続回数には含めず、listener を誤って停止させない。

既存の fd 枯渇契約も維持する。
accept syscall が `EMFILE` 又は `ENFILE` を返したら reader を同期的に解除し、50 ms 後に 1 回だけ再登録する。
accept syscall の成功時点で、後続の peer lookup、allow 判定又は runtime callback の成否にかかわらず fd 枯渇の連続回数を 0 に戻す。
成功を 1 本も挟まず `EMFILE` / `ENFILE` が 200 回連続した場合は reader を解除したまま listener を閉じ、固定 reason `accept_exhausted` を 1 行だけ出して rc 72 で process を終了する。

64 本目の accept syscall が成功した場合は、まず連続回数を reset し、その接続の引渡し又は close を完了してから callback を返す。公平性のため 65 本目の accept syscall は同じ callback では発行しない。
次 tick の callback が直ちに `EMFILE` / `ENFILE` になった場合は、その callback で reader を解除してから 50 ms timer を登録する。64 本上限による次 tick への譲渡は backoff reader を再登録せず、timer 満了時だけ再登録する。
したがって 64 境界と fd 枯渇が連続しても、reader は二重登録されず、成功した 64 本目による reset と次の resource error の 1 回目がこの順で観測される。

### 3.2 parse と Host

allow 通過後は既存の request line 2048 byte、header 8192 byte、body 65536 byte、header 64 件、接続 30 秒の上限で parse する。
GET と HEAD だけを route へ進める。
body、Transfer-Encoding、不正な Content-Length、既存の拒否対象は従来どおり拒否する。

全 bind で `Host` header をちょうど 1 個要求する。
非 loopback の値は設定 IPv4 と一致し、loopback の値は設定 IPv4 又は ASCII case-insensitive の `localhost` と一致しなければならない。
port は設定 port と一致する `:<port>` を要求し、設定 port が 80 の場合だけ省略を許す。
数値 IPv4 の表記は設定 `bind` と byte-for-byte で一致させる。
Host の grammar は wire 全体ではなく、HTTP parser が返す field-value に対して適用する。header name と `:` の後及び行末の外側 OWS は parser が除去するため、その wire 表記の完全一致は要求しない。
parser 後の field-value 内の空白、他の hostname、別 port、複数 Host を許さない。
欠落、重複、値違いは GET / HEAD とも 400 とし、daemon を呼ばない。

### 3.3 Basic 認証

Host を通過した後、route 解決と daemon 呼出しより前に認証する。
`auth = "none"` では Authorization を認証には使わない。
`auth = "basic"` では Authorization をちょうど 1 個要求する。
scheme は ASCII case-insensitive の `Basic` とする。
Authorization の wire field-value は `Basic`、ちょうど 1 個の SP (`0x20`)、token の順だけを許す。tab、2 個以上の SP、scheme 又は token の前後の OWS を拒否するため、protocol 層はこの field-value を OWS 正規化前の bytes として認証器へ渡す。
token は最大 176 ASCII byte とし、RFC 4648 の標準 Base64 alphabet と padding を strict mode で decode する。decode 後に標準 Base64 で再 encode した bytes が元の token と一致する場合だけ canonical とする。非 canonical pad bit、余分な padding、URL-safe alphabet を拒否する。
decode 後の credential は最大 132 byte とする。
credential は byte 列 `b"afx:" + password` と完全一致しなければならない。
password 内の `:` はそのまま password の一部であり、最初の `:` より後を再分割しない。

rate limit 中でない各試行は、正解 `b"afx:" + password` 全体と decoded credential 全体をそれぞれ SHA-256 の 32 byte digest にする。
欠落、重複、scheme 違い、壊れた Base64、区切り無しも固定の dummy 入力から 32 byte digest を作る。
decoded 上限超過も同じ固定 dummy 経路へ入れる。
全試行は `hmac.compare_digest()` を 32 byte 対 32 byte でちょうど 1 回行う。
password 又は credential の可変長 bytes を直接 `compare_digest()` に渡さない。

欠落、重複、scheme 違い、壊れた Base64、decoded 上限超過、username 違い、password 違いは同じ認証失敗として扱う。
通常の認証失敗は 401 を返す。
401 は `WWW-Authenticate: Basic realm="afx"` を必ず持つ。
外部応答、journal、counter の key から失敗形を区別できないようにする。

HEAD の 401 と 429 は本文を持たない。
その `Content-Length` は同じ条件の GET error 本文の長さと一致させる。
この規則は既存の全 error 応答の HEAD 契約 (`afx-ops/afx_ops/web/request.py:217`) を維持する。

### 3.4 rate limit

認証失敗だけを monotonic clock の rolling 60 秒窓へ記録する。
送信元 IPv4 ごとの上限は 5 回である。
process 全体の上限は 100 回である。
最初の 5 回又は 100 回は比較後に 401 を返す。
同じ窓の 6 回目又は全体の 101 回目からは、認証比較をせず 429 を返す。
最古の失敗から 60.000 秒以上経過した時点で、その失敗を窓から除く。
成功しても窓内の失敗履歴を消さない。

各試行では送信元表を lookup する前に、global queue と全実 IPv4 entry の期限切れ時刻を prune し、空になった実 IPv4 entry を回収する。
各送信元 queue の保持長は最大 5、global queue は最大 100 とする。
429 にした試行はどの queue にも加えない。
global queue が既に上限なら送信元 entry の lookup / allocation より先に 429 とし、新しい entry を作らない。この分岐では既存 entry も評価しない。

429 は整数秒の `Retry-After` を持つ。
global 先行 429 の値は global queue の最古の失敗が期限切れになるまでの秒数だけから計算する。
global が満杯でない場合の送信元 429 は、その送信元 entry の最古の失敗が期限切れになるまでの秒数だけから計算する。
いずれも切り上げた整数で、最小 1、最大 60 とする。同じ送信元の窓も同時に超過していても、global 先行分岐では送信元側との最大値を取らない。

送信元表は実 IPv4 ごとに最大 100 entry とし、overflow bucket は設けない。
各 entry は 60 秒窓の失敗時刻だけを持つ。
entry を作るのは認証比較後に失敗を global queue へ追加する経路だけであるため、非空 entry 数は global queue の失敗数以下となり、100 を超えない。global が満杯なら前段で返すため、101 個目の entry を作らない。
失敗時刻が全て期限切れになった実 IPv4 entry は削除できる。
表の entry 数、各送信元の保持数、全体の保持数は固定上限を超えない。

429 でも daemon 呼出しは 0 回である。
rate limit の key に `X-Forwarded-For`、`Forwarded`、Host その他の header を使わない。

### 3.5 route と記録

allow、parse、Host、auth を全て通った要求だけを既存の route 解決へ渡す。
web principal は引き続き読み取り専用である。
認証失敗を daemon activity 又は event に書かない。

method 不許可の 405 は POST 等の GET / HEAD 以外で観測する。HEAD 自体は許可 method なので 405 へは到達しない。
protocol、request adapter、route dispatch 又は renderer 自身の想定外例外は 500 とする。
daemon client の transport 例外 / timeout、daemon の非 200 reply、code 付き reply、又は期待 schema でない backend reply は 502 とする。
この境界より前に確定した 400 / 401 / 404 / 413 / 429 は内部例外へ読み替えない。
HEAD で到達し得る error status は 400 / 401 / 404 / 413 / 429 / 500 / 502 とし、いずれも同条件の GET と同じ Content-Length、body 0 とする。

peer / auth の security event counter と journal は次の 4 種だけに固定する。

| counter | journal 文 |
|---|---|
| `web_peer_rejected_total` | `afx-ops: web_peer_rejected` |
| `web_peer_lookup_failed_total` | `afx-ops: web_peer_lookup_failed` |
| `web_auth_failed_total` | `afx-ops: web_auth_failed` |
| `web_auth_limited_total` | `afx-ops: web_auth_limited` |

この 4 種の journal は各種につき process lifetime の先頭 10 件だけを出し、11 件目以後は counter だけを増やす。
accept loop の運用 event は security event と別に、次の集合へ固定する。

| counter | 増分 | journal 文と sampling |
|---|---|---|
| `accept_resource_error_total` | `EMFILE` / `ENFILE` ごとに 1 | `afx-ops: accept_backoff`。連続 episode の先頭だけ、かつ process lifetime で最大 10 行。errno は付けない。 |
| `accept_callback_failed_total` | runtime callback の例外ごとに 1 | `afx-ops: accept_callback_failed`。process lifetime の先頭 10 件だけ。例外型・例外文は付けない。 |
| `accept_exhausted_total` | 連続 200 回到達時に 1 | `afx-ops: accept_exhausted` を sampling 枠と独立にちょうど 1 行出して rc 72 で終了する。 |

全 journal 文に IP、port、Host、Authorization、username、password、secret、回数、errno、例外型、例外文を足さない。
aiohttp の access log は引き続き無効にする (`afx-ops/afx_ops/web/runtime.py:54`)。

## 4. 隔離との関係

隔離前の `prebind_listener()` は固定 `127.0.0.1` ではなく、検証済み `bind` と `port` を受け取る。
listener は引き続き隔離前に `socket(AF_INET)`、bind、listen を完了する。
この時点では accept しない。

`check_listener()` は `getsockname()` の address が設定の `bind` と一致することを検査する。
port も設定値と一致することを検査する。
`SO_TYPE`、`SO_ACCEPTCONN`、close-on-exec の検査は維持する (`afx-ops/afx_ops/isolation/accept_loop.py:56`)。

隔離後に処理を開始する順序は変えない。
隔離前に backlog へ入った接続も、Landlock、seccomp、`PR_SET_DUMPABLE=0`、fd 自己検査、server 認証、READY 完了後に初めて accept する。

web の seccomp は新規 `socket(AF_INET, ...)` と `socket(AF_INET6, ...)` を `EACCES` で拒否する (`afx-ops/afx_ops/isolation/profiles.py:113`)。
既存の pre-bind listener に対する trace 固定の `accept4` だけを許す (`afx-ops/afx_ops/isolation/profiles.py:220`)。
この契約は LAN bind 後も緩めない。

Landlock は socket の bind address、accept、peer address を仲介しない。
Landlock は pathname UDS の connect も仲介しないという現行契約を維持する。
したがって LAN 公開の境界は pre-bind、allow、Host、auth、seccomp の組み合わせで作る。

設定収集、secret dir / file の検査と password の読み込みを完了して `FeaturePlan` を固定した後、secret 関連 fd を全て閉じてから listener を pre-bind する。
fd 整理と隔離の自己検査時点で保持を許す fd は `{0, 1, 2, listener, notify}` だけとし、secret dir fd、password fd、config fd、interface 列挙用 socket fd を残さない。
`READY=1` の送信後は既存契約どおり notify fd も閉じる。

## 5. probe と enable / ウィザード

### 5.1 probe

`afx-ops probe web` は state dir と stage file を作らない。
stdout は成功時も失敗時も空とする。
stderr は失敗時に `afx-ops: <固定 reason>` の 1 行だけとする。
secret、設定値、IP、CIDR、path、例外文を出さない。

interface 列挙は Linux 前提の stdlib だけの 1 実装を `afx-ops/afx_ops/interfaces.py` に置き、probe、production 起動時の局所性検査、`afx-ops interfaces` が共用する。本体ウィザードは package を直接 import せず、後述の bounded subprocess 契約だけを通じて同じ結果を使う。
`socket.if_nameindex()` の各 interface について一時的な `AF_INET/SOCK_DGRAM|SOCK_CLOEXEC` socket から最初に `fcntl.ioctl(SIOCGIFFLAGS)` を呼ぶ。flags 取得失敗は列挙失敗、`IFF_UP` でない interface は address を問い合わせず skip とする。
up interface には `SIOCGIFADDR` を行う。IPv4 未設定だけを表す errno は `EADDRNOTAVAIL` と `EAFNOSUPPORT` に固定して skip し、それ以外の errno (`ENODEV` を含む) は列挙失敗とする。実装と試験はこの集合を同じ定数から使い、集合を暗黙に広げない。
address を取得できた interface だけに `SIOCGIFNETMASK` を行う。その失敗、想定外 errno、又は netmask 検査失敗は列挙失敗とする。
各 ioctl buffer と interface 名の長さを固定し、列挙 socket は結果を固定した直後に閉じ、隔離へ持ち込まない。
`if_nameindex()` 又は上記で列挙失敗とした操作が失敗した場合は、部分結果を使わず固定 reason `interface_lookup_failed` で停止する。外部 command、netlink の別実装、手入力 fallback は使わない。

scope は kernel の文字列に依存せず address 範囲から判定する。`169.254.0.0/16` は `link`、`127.0.0.0/8` は `loopback`、それ以外の unicast IPv4 は `global` とし、multicast、unspecified、broadcast は候補にしない。
netmask は連続した canonical mask であることを検査し、不正なら列挙失敗とする。
候補は `(scope の順 global, link, ifindex, address)` で安定 sort し、重複 address は最小 ifindex の 1 件に正規化する。

`afx-ops interfaces` は本体と afx-ops の独立 package 境界を保つ列挙専用 command とする。絶対 path の executable を使い、argv はちょうど `(<resolved-afx-ops>, "interfaces")`、stdin は閉じ、secret を argv、env、stdin に渡さず、config / state / secret dir を読まず書かない。5 秒で timeout し、stdout は 65536 byte を上限として超過時点で打ち切る。成功契約は rc 0、stderr 空、stdout が UTF-8 の改行 1 個で終わる次の versioned JSON object 1 個である。

```json
{"interfaces":[{"address":"192.168.1.10","ifindex":2,"scope":"global"}],"version":1}
```

top-level key は `interfaces` と `version` だけ、entry key は `address`、`ifindex`、`scope` だけとする。`version` は整数 1、`ifindex` は 1 以上の整数、`address` は canonical な非 loopback unicast IPv4、`scope` は `global` 又は `link` とし、entry は前記の安定 sort 済みで address 重複を許さない。列挙失敗時は rc 71、stdout 空、stderr `afx-ops: interface_lookup_failed\n` だけとする。本体は nonzero rc、signal、timeout、stdout 上限超過、stderr 契約違反、UTF-8 / JSON / schema / sort 違反を全て固定 reason `interface_lookup_failed` に写像し、子 process の出力や例外文を追加しない。この subprocess は probe と同じ有界呼出し helper を使う。

probe は §2.4 の決定木と同じ順序で次を検査する。

1. config dir と TOML を読み、`port`、`bind`、`allow`、`auth` の全単独 grammar を検査する。
2. 非 loopback の allow key 必須条件を検査する。
3. T1 artifact では unrestricted、basic の順に capability を検査する。T2 ではこの段を通過する。
4. 非 loopback bind が上記共通列挙結果の global / link unicast IPv4 として実在することを検査する。列挙不能は `interface_lookup_failed`、列挙成功後の不在は `bind_not_local` とする。
5. secret dir が存在すれば owner / mode を検査し、検査済み dir fd から許可外 entry の不在と `web.password` の owner、通常 file、mode `0600`、size、byte grammar を検査する。unsafe は auth にかかわらず `secret_invalid` とする。
6. 検証済み auth に従い、`basic` の欠落を `secret_missing`、`none` の安全な残骸を `secret_unexpected` とする。`none` の secret dir 不在は許可する。
7. 指定 address と port へ実際に bind + listen する。
8. `getsockname()` が指定 address と port に一致することを検査して close する。

probe は listener を accept しない。
probe は password の正否をネットワーク経由で試さない。
実 bind / listen 又は `getsockname()` 不一致は `probe_bind_failed` とする。

### 5.2 enable / reconfigure / 対話ウィザード

`afx ops enable web` は `--non-interactive` と `--reconfigure` を受け付ける。
`--non-interactive` は stdin / stdout の TTY 判定より常に優先する。
`--reconfigure` は enabled の実物照合後の no-op 経路へ入らず、完全な既存設定でも port、bind、allow、auth と、basic の password を保持するか置換するかを目標値として再質問する。
非対話の `--reconfigure` は完全な目標 `ops.toml` を指す `--target-config <path>` を必須とし、現在値から目標を推測しない。目標が basic で安全な既存 password を保持しない場合は、別の明示 password input file も必須とする。いずれかが無ければ何も書かず rc 2 `setup_input_required` とする。

既存 stage schema の operation に `reconfigure` を追加する。
有限な stage 列は `prepared → secret-staged → stopped → secret-committed → config-committed → probed → started → ready` とする。active 経路は `ready` の事後処理、inactive 経路は `probed` の事後処理だけが stage を既存の `operation = enable, stage = enabled` へ置き換える。
reconfigure 開始時に terminal `enable/enabled` record の全 field を基底 snapshot `enable_record` object として reconfigure stage file へ durable 保存し、最初の外部変更より前に afx と ops-web の `ActiveState` をそれぞれ reconfigure 固有の `reconfigure_afx_before_stop`、`reconfigure_ops_before_stop` に一度だけ記録する。`enable_record` は現行 terminal schema で再検証し、再開時に上書きせず、内部の既存 `afx_before_stop` と reconfigure 固有 field を混同しない。
reconfigure stage は `current_config_digest`、`target_config_digest`、`secret_action = keep|install|remove|absent`、`material_committed` boolean、`reconfigure_afx_before_stop`、`reconfigure_ops_before_stop`、`enable_record` と、secret を含まない固定 field だけを持つ。`material_committed` は最初の live secret rename / unlink + directory fsync、又は secret が no-op なら live config rename + directory fsync の完了後にだけ true にする。
crash が live 変更と stage 更新の間に入った場合は、`secret_action`、secret staging / live file の有無、live config digest を照合して commit 済みを先に検出し、`material_committed = true` を durable にしてから再開する。実物が commit 済みなら古い false を根拠に rollback しない。
目標 `ops.toml` 全文は secret を含まない mode `0600` の durable staging file に書き、file fsync、atomic rename、親 directory fsync を済ませてから `prepared` を記録する。各再開時に staging file の SHA-256 が `target_config_digest` と一致することを検査する。
live `ops.toml` の更新時は staging file 自体を消費せず、そこから同一 directory の一時 file を作って fsync、atomic rename、directory fsync する。これにより config rename 後に crash しても目標全文を digest からではなく staging file から再利用できる。

目標 basic の新 password は `prepared` の後、mode `0600` の固定名 secret staging file に durable 保存し、stage / log には内容も digest も書かず `secret-staged` に進む。書込み途中の crash は `prepared` から安全な完成 file を検査して進み、不在 / unsafe なら live material 変更前の rollback を行って入力を再要求する。再開時は staging file の有無と live `web.password` の有無で atomic rename の完了を判定する。
目標 none の場合、現在 auth が none であっても安全な残骸 `web.password` があれば `secret_action = remove` とし、unit 停止後に unlink、secret directory fsync を必ず行う。unsafe な dir、password 又は未知 entry は変更前に `secret_invalid` で停止し、自動修復しない。

各 stage の durable 事後条件と再開動作は次のとおりである。`旧` と `目標` はそれぞれ記録済み digest と完全一致することを意味する。

| stage | unit 状態 | live config | staged config | 期待する live secret | 再開動作 |
|---|---|---|---|---|---|
| `prepared` | 変更前と同じ | 旧 | 目標、digest 一致 | 旧状態。secret staging はまだ不要 | `install` なら password staging を durable 保存して `secret-staged` にする。それ以外は事後条件を検査して `secret-staged` にする。失敗は必ず rollback する。 |
| `secret-staged` | 変更前と同じ | 旧 | 目標、digest 一致 | 旧状態。`install` だけ安全な secret staging が存在 | `ops-web` を停止して停止確認し、`stopped` を durable にする。停止又は stage 書込み失敗は必ず rollback する。 |
| `stopped` | inactive / failed | 旧 | 目標、digest 一致 | 旧状態 | 最初の live material 変更を行う。`remove` は unlink + dir fsync、`install` は secret staging の atomic rename + dir fsync、`keep/absent` は現状態を検査し、`secret-committed` にする。 |
| `secret-committed` | inactive / failed | 旧、又は crash が rename 後なら目標 | 目標、digest 一致 | 目標 auth に必要な状態 | live config が旧なら staging 全文を atomic 保存する。既に目標 digest なら再保存せず、directory fsync を確認して `config-committed` にする。 |
| `config-committed` | inactive / failed | 目標 | 目標、digest 一致 | 目標 auth に必要な状態 | 共通 validator と probe を実行する。失敗はこの stage と停止状態を保ち、次回同じ reconfigure が forward recovery する。 |
| `probed` | inactive / failed | 目標 | 目標、digest 一致 | 目標 auth に必要な状態 | `reconfigure_afx_before_stop` が active / activating / reloading なら `ops-web` を起動して `started` へ進む。afx がそれ以外なら ops-web を起動せず terminal record を finalize する。 |
| `started` | active / activating | 目標 | 目標、digest 一致 | 目標 auth に必要な状態 | server 認証と `READY=1` を確認する。失敗時は unit を停止して `probed` に戻し、forward recovery する。 |
| `ready` | active | 目標 | 目標、digest 一致又は cleanup 済み | 目標 auth に必要な状態 | staging file を unlink + directory fsync し、stage を `enable/enabled` へ atomic 保存する。cleanup 後の crash は live config digest と ready unit を照合して finalize する。active 経路だけがこの stage を通る。 |

afx が開始時に inactive / failed なら、`probed` の事後条件を確認した後も afx と ops-web を起動しない。staging file を unlink + directory fsync して直接 `enable/enabled` を atomic 保存し、terminal record は `ops_active = false` のまま status の `ops=starts_with_afx` と既存 `Wants=` を維持する。次回 afx 起動時に既存の `Wants=` で ops-web が起動する。
afx が開始時に active / activating / reloading だった場合だけ `probed → started → ready` で ops-web を start し、server 認証と `READY=1` を要求して `ops_active = true` で finalize する。
どちらの finalize も基底の terminal record から `principal`、`registered`、`artifacts`、`wants`、`afx_before_stop`、`committed` と未知でない既存 schema field を保持し、reconfigure 固有 field と failure だけを除去する。`principal`、`artifacts`、`wants` を空値で再生成してはならない。

stage 名にかかわらず `material_committed = false` で live secret / live config の最初の material 変更より前に失敗した場合は rollback を必須とする。staging file と secret staging を unlink + 各 directory fsync し、`reconfigure_ops_before_stop` が active / activating / reloading で、かつ `reconfigure_afx_before_stop` も active / activating / reloading の場合だけ旧設定で ops-web を再起動して ready を確認する。開始時に afx が inactive なら afx / ops を起動せず、`ops_active=false` / `ops=starts_with_afx` の terminal record へ戻す。rollback 自体に失敗した場合は reconfigure stage と固定 failure を残して unit を停止し、enabled と表示しない。
最初の live secret rename / unlink 又は live config rename のいずれかを directory fsync した後は rollback せず、上表の目標へだけ forward recovery する。

`basic → none` と `none + 安全な残骸 → none` は password unlink、secret directory fsync、目標 config 保存の順とする。unlink 後、config 保存前の crash は旧 config に対して `secret_missing` 又は `secret_unexpected` になり得るが、production は停止したまま stage と staging fileから none へ再開する。
`none → basic` は password atomic 保存、secret directory fsync、目標 config 保存の順とする。secret 保存後、config 保存前の crash は旧 config に対して `secret_unexpected` になるが、production は停止したまま stage と staging file から basic へ再開する。

手置きの unsafe な secret dir / `web.password` を見つけた場合は `secret_invalid` で停止し、削除、chmod、chown、置換をしない。
回復手順は、利用者が所有者・mode・型・内容を直すか、unit 停止中に対象を退避して `--reconfigure` を再実行することだけを固定文で案内し、path や内容は表示しない。

初回 enable の対話時は不足している設定だけを次の順で尋ねる。`--reconfigure` は前記のとおり既存設定が完全でも同じ順で目標値を再質問する。

1. 「自機のみ」又は「LAN」を尋ねる。
2. 既定は「自機のみ」とする。
3. LAN なら待受 address を候補から選ばせる。
4. LAN なら誰が見られるかを尋ねる。
5. 必要なら allow の IPv4 / CIDR を入力させる。
6. 必要なら password を生成するか利用者が決めるかを尋ねる。
7. 目標が `auth = "none"` で安全な既存 `web.password` がある場合は、`secret_action=remove` を提示し、利用者が `none` を明示的に再選択した場合だけ削除する。
8. 設定と secret を atomic に保存して probe を実行する。

LAN の候補列挙は §5.1 の `afx-ops interfaces` bounded subprocess だけを使い、その afx-ops 内部の stdlib 共通実装が返す global と link の非 loopback unicast IPv4 を候補にする。本体から `afx_ops.interfaces` を import しない。
候補は `(scope の順 global, link, ifindex, address)` で安定 sort する。
1 候補ならそれを既定値にし、複数候補なら利用者の明示選択を必須にする。
候補が無ければ `bind_not_local`、列挙 command が固定失敗を返すか bounded subprocess 契約に違反したら `interface_lookup_failed` で停止する。対話 / 非対話とも手入力 fallback は設けない。
非対話の初回 enable では、既存設定が grammar と組み合わせを満たして完全なら候補 UI を実行せず、probe 内で同じ列挙実装を使う。不足なら file を一切書かず rc 2 `setup_input_required` とする。

「誰が見られるか」の選択肢は次の 4 個に固定する。

| 選択肢 | 既定 | 書く設定 |
|---|---|---|
| 許可 IP だけ | LAN での既定 | `allow` に非空の限定 list、`auth = "none"` |
| パスワード |  | `allow = ["0.0.0.0/0"]`、`auth = "basic"` |
| 両方 |  | `allow` に非空の限定 list、`auth = "basic"` |
| この待受アドレスへ到達できる全 IPv4 |  | `allow = ["0.0.0.0/0"]`、`auth = "none"` |

「許可 IP だけ」と「両方」は非空の allow 入力まで完了条件にする。
空回答を unrestricted と解釈しない。

「この待受アドレスへ到達できる全 IPv4」では、確認前に固定の 1 行 `LAN、VPN、port-forward 等を含め、このアドレスへ到達できる全 IPv4 source が戦略の diff を読めます` を表示する。
この 1 行の後で明示確認を要求する。
確認しなければ設定を書かない。

生成 password は CSPRNG の random 16 byte を padding なし base64url にする。
端末へ表示するのは生成直後の 1 回だけとする。
ログ、stage、ウィザードの記録、再実行時の表示へ残さない。
利用者が自分で password を決める選択肢も用意する。
`web.password` は同じ directory 内の一時 file へ mode `0600` で書き、fsync、atomic rename、directory fsync の順で保存する。

初回 enable は、既存設定が grammar と組み合わせの検査を満たし、probe が成功するなら再質問しない。`--reconfigure` はこの無再質問規則の例外である。
既存 secret の内容を表示しない。
初回 enable で `auth = "none"` と安全な残骸 `web.password` が組み合わさる場合、自動削除も自動 basic 化もしない。対話では auth の選択へ戻し、利用者が `none` を再選択して `secret_action=remove` を明示した場合だけ `web.password` を unlink して secret directory を fsync する。EOF、取消し、又は明示選択なしでは何も削除しない。非対話では明示 `secret_action=remove` 入力が無ければ何も変更せず `setup_input_required` とする。
初回 enable では probe が失敗した場合だけ、回復可能な固定 reason に対応する必要項目を再質問する。reconfigure は live material 変更前に目標候補を同じ validator で検査して修正させ、`prepared` 後は目標を変更しない。
`config_invalid` / `allow_required` は該当設定、`bind_not_local` / `probe_bind_failed` は列挙済み候補からの bind 再選択、`secret_missing` / `secret_invalid` / `secret_unexpected` は auth と secret だけを対象にし、無関係な値を再質問しない。
`interface_lookup_failed` は同じ列挙実装を再試行又は手入力で迂回せず、その試行を直ちに終了する。
初回 enable の probe と、reconfigure の material 変更前の目標検査は初回を含め最大 3 回とする。
3 回目も失敗した場合は最後の固定 reason を返す。初回 enable は設定と利用者が明示した secret 操作を durable 保存済みだが、probe は stage を作らないため enable stage は未作成のままとする。reconfigure の `config-committed` 以後に probe が失敗した場合は同じ目標のまま該当 `operation = reconfigure` stage と停止状態を保持し、次回は forward recovery だけを行う。

allow 入力の空回答は再質問し、unrestricted 又は取消しと解釈しない。
全質問で EOF 又は明示取消しを受けた場合は以後書き込まず rc 1 `setup_aborted` とする。

非対話では port-only の旧設定を安全な localhost として扱える。
非対話で LAN を選ぶ場合は `bind`、非空の `allow`、`auth` を全て明示する。非対話 reconfigure は常に `--target-config` の完全な目標設定を要求する。
不足があれば何も書かず rc 2 `setup_input_required` で fail closed とする。
無制限 LAN は `allow = ["0.0.0.0/0"]` が明示されていなければ成立しない。

### 5.3 status

`afx ops status web` は既存の stage / unit 行に続けて公開範囲を 1 行だけ出す。
形式は `web-access: bind=<IPv4>:<port> allow=<restricted|unrestricted|loopback-default> auth=<none|basic>` に固定する。
`restricted` は `0.0.0.0/0` 以外の allow がある状態を表す。
`unrestricted` は明示値 `0.0.0.0/0` を表す。
`loopback-default` は loopback bind かつ allow 省略を表す。
この行に allow の個別 IP、password、secret の存在 path を出さない。

`operation = reconfigure` の間、既存の stage / unit 行は `web: reconfigure <stage> unit=<active-state> result=<result>` と表示し、`enabled` という語を出さない。
`stopped`、`secret-committed`、`config-committed`、`probed` の unit は `inactive` 又は `failed` でなければ stage mismatch として停止する。
probe / restart 失敗時も reconfigure stage と固定 failure を表示し、公開範囲 1 行は live config ではなく digest 検証済みの目標 staging config から表示する。
通常の `operation = enable, stage = enabled` だけを enabled と表示する。停止中は `unit=inactive` 又は `unit=failed` をそのまま表示し、旧設定が enabled であるかのように見せない。
reconfigure を inactive 経路で完了した terminal record は既存形式どおり `enable enabled unit=inactive result=success afx_before_enable=<保存値> ops=starts_with_afx` と表示する。

## 6. 旧 spec の置換

本 spec と旧 spec が矛盾する場合、下表の範囲では本 spec を正本とする。
下表にない旧 spec の読み取り専用 principal、route、隔離、enable 状態機械は維持する。

| 旧 spec の箇所 | 置換内容 | 本 spec |
|---|---|---|
| 行 13、51、224、独立 package / subprocess | **維持する。**本体と afx-ops は互いの package を import せず、絶対 path の `afx-ops probe ...` と `afx-ops interfaces` という bounded subprocess 契約だけで結合する。 | §5.1 |
| 行 17、要点 | 「web は loopback の read-only 表示」を、設定済み bind / allow / auth の read-only 表示へ置き換える。web が承認権限を持たない点は維持する。 | §0、§3 |
| 行 33、§1.2 境界 | `ops-web` が扱う「UI 認証 secret」を、`auth = "basic"` のときだけ必須な `web.password` として具体化する。 | §2.2、§4 |
| 行 36、§1.2 主体 | 「localhost の利用者」を「設定した待受アドレスへ到達し、allow / Host / auth を通った利用者」に置き換える。 | §1、§3 |
| §3.1.1 `ops.toml` | web の `port` に `bind`、`allow`、`auth` を追加する。 | §2 |
| §3.3 web | loopback 固定・認証なしを、検証済み bind、allow、Host、任意 Basic 認証へ置き換える。 | §1〜§4 |
| 旧 §5.1 行 132、web secret | 「web は secret を持たない」を、「web は auth に応じて `web.password` を隔離前だけ読む。secret dir は Landlock 規則へ加えない」に置き換える。 | §2.2、§4 |
| §5 自己隔離 | loopback pre-bind を設定 address の pre-bind に置き換える。隔離後の新規 INET socket 拒否は維持する。 | §4 |
| §6.1 enable | staged の web 設定・secret・probe 検査、status の公開範囲表示、`operation = reconfigure` と有限 stage 列を追加する。既存の `enable / disable` stage と enabled 実物照合は維持し、明示 reconfigure だけは enabled no-op を迂回する。 | §5 |
| §7 初回ウィザード | web の最小質問と loopback probe を、公開範囲質問、`secret_action=remove` の明示導線、`afx-ops interfaces`、新 probe へ置き換える。 | §5 |
| §8 O-11 | 「認証済み loopback read-only」を「設定済み公開範囲の allow / Host / auth を通った read-only」に置き換える。 | §3、§7 |
| 行 226、§7 probe rc | web の config / interface / secret reason を rc 71 に、実 bind failure を rc 73 に追加し、固定 reason の優先順位を本 spec の決定木へ置き換える。 | §2.4、§5.1 |
| §9 AC-10 | `127.0.0.1` 固定・認証なしの観測を、本 spec の AC-1〜AC-40 へ置き換える。 | §9 |
| 行 281、§10 T7 | 実装済みの「認証なしのローカル限定」を、本 spec の T1 / T2 で段階的に置き換える。 | §10.2 |
| §11 残余リスク | web の port 衝突だけでなく LAN 公開の残余を追加する。 | §12 |
| §12 裁定 1 | 2026-10-09 の「初版は 127.0.0.1・認証なし」を、2026-10-23 のユーザー裁定で置き換える。 | §0、§8 |

旧 spec の変更履歴行 326 と 331 は履歴事実なので書き換えない。
両行に記録された「ローカル限定・認証なし」は当時の契約であり、現行契約ではない。

## 7. 遮断と秘密

password は認証比較にだけ使う。
password を daemon へ送らない。
password を daemon activity、event、web 応答、journal、access log、例外文、probe 出力、status、stage file、ウィザードの記録へ入れない。
Authorization header も同じ出力先へ入れない。

allow 又は auth の失敗では daemon client を呼ばない。
失敗を daemon activity に書くための route 又は scope を追加しない。
web principal は引き続き読み取り専用とする。

秘密を含み得る aiohttp の既定 error log と access log は無効のままにする。
想定外例外は型名又は固定 reason だけを journal に出す。
IP、Host、Authorization、decoded credential、secret bytes を例外へ連結しない。

## 8. 裁定の記録

### 8.1 ユーザー裁定 (2026-10-23、確定)

| # | 問い | 決定 | 根拠 |
|---|---|---|---|
| 1 | nginx や SSH 転送を要求せず LAN から見られるようにするか | 製品内で提供する。 | 別 PC から確認する日常運用の労力を下げる。 |
| 2 | 安全策と利用者責任をどう分けるか | ある程度のセキュリティを製品が用意し、最終的な運用は利用者責任とする。 | 読み取り専用でも戦略 diff を表示するため、無保護を既定にしない。 |
| 3 | 提供する運用形 | localhost、LAN、ホワイトリスト、簡易認証を提供する。 | bind、allow、auth の組み合わせで表現する。 |
| 4 | auth の表現 | secret の有無でなく `auth = "none" | "basic"` を明示する。 | 残骸 file による意図しない有効化又は無効化を防ぐ。 |
| 5 | 非 loopback の allow | 必須にする。無制限は `0.0.0.0/0` の明示値にする。 | 省略ミスと機密性の明示放棄を区別する。 |
| 6 | T1 単独公開 | localhost 又は非空 allow 付き LAN に限定する。無制限 LAN と `auth = "basic"` は T2 まで拒否する。 | 明示選択、警告、rate limit、secret lifecycle が揃う前に広く公開しない。 |

### 8.2 閉じた確認事項 (決定と根拠)

| # | 決定 | 根拠 |
|---|---|---|
| 1 | Host は全 bind でちょうど 1 個とする。非 loopback は設定 IPv4、loopback は設定 IPv4 又は case-insensitive `localhost` を許し、port 80 だけ省略を許す。 | bind ごとの差を減らしつつ、標準 port の通常表記と localhost 利用を保つ。Host 検査は §12 のとおり defense-in-depth である。 |
| 2 | password は 16〜128 byte の visible ASCII とし `:` を許す。空白、改行、非 ASCII は拒否する。生成値は random 16 byte の padding なし base64url とし、decoded credential は 132 byte 上限、正解 `b"afx:" + password` 全体との固定長 hash 比較にする。 | 最低 128 bit の生成 entropy、明確な file grammar、有界 parse、可変長比較の回避を同時に満たす。 |
| 3 | 待受候補は up interface に実在する unicast IPv4 の scope `global` / `link` を許す。scope は address 範囲で決め、列挙できない場合の手入力 fallback は設けない。 | probe / production と同じ候補集合に限定し、選べたのに起動不能となる導線を作らない。 |
| 4 | Authorization は `Basic` + 1 SP + RFC 4648 標準 Base64 の canonical token だけを許し、tab、2 SP、外側 OWS、余分 padding、非 canonical pad bit を拒否する。 | 認証 header の受理範囲を parser の寛容さや実装差から独立させる。 |
| 5 | interface 列挙は Linux の `socket.if_nameindex()` と `fcntl.ioctl()` (`SIOCGIFFLAGS` / `SIOCGIFADDR` / `SIOCGIFNETMASK`) による afx-ops 内の 1 実装を使う。probe / production は package 内で共用し、本体 wizard は `afx-ops interfaces` bounded subprocess だけで利用する。 | 外部 command への意味依存と異なる列挙結果を排除しつつ、本体と afx-ops が互いを import しない独立 package 原則を保つ。 |

残りの確認事項はない。

### 8.3 問いにしない確認事項

- `auth = "none"` で安全な `web.password` が残っていれば無視せず `secret_unexpected`、unsafe なら `secret_invalid` で拒否する。
- allow 外と peer 取得失敗は 403 を返さず黙って close する。
- allow は接続枠と protocol の生成前に判定する。
- 評価順は `allow → parse → Host → auth → route` とする。
- Basic の username は `afx` に固定する。
- cookie session は作らない。
- rate limit の値は設定 key にせず固定する。
- password と Authorization は daemon、ログ、activity、event へ渡さない。
- `0.0.0.0` bind、IPv6、TLS、reverse proxy、firewall は本 spec に含めない。

## 9. 受入条件

上限 `L` の境界試験は `L-1`、`L`、`L+1` を同じ観測点で行う。
時刻試験は fake monotonic clock を使い、59.999 秒、60.000 秒、60.001 秒を観測する。
mock の単体試験だけでなく、listener、accepted fd、実 process、実 seccomp を使う試験を該当 AC に含める。

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | `bind` の省略、`127.0.0.1`、up interface の global / link unicast IPv4 を受理する。hostname、先頭ゼロ、空白付き、`0.0.0.0`、IPv6、IPv4-mapped IPv6、interface に無い IPv4 を拒否する。stdlib 共通列挙の `if_nameindex()` / 各 ioctl 失敗と、列挙成功後の不在を区別し、scope を address 範囲から判定する。 | config / wizard / probe / production の同じ列挙 fixture、rc 71 と `interface_lookup_failed` / `bind_not_local`。 |
| AC-2 | 指定した非 loopback IPv4 と port に実 bind + listen し、`getsockname()` が両方に完全一致する。別 address 又は別 port の listener を `listener_invalid` にする。 | 実 listener の `getsockname()` と起動 rc。 |
| AC-3 | allow の単一 IPv4、canonical CIDR、singleton `allow = ["0.0.0.0/0"]` を受理する。非 canonical CIDR、空 list、重複、IPv6、IPv4-mapped IPv6、空白付き、host bit 付き CIDR、`0.0.0.0/0` と他 network の混在を `config_invalid` で拒否する。 | config reader と writer の再読取り、混在 list の順序違い。 |
| AC-4 | 非 loopback で allow key が無いと `allow_required` になる。loopback では allow 省略、限定 allow、`0.0.0.0/0` がそれぞれ契約どおり動く。 | 起動 rc と固定 reason。 |
| AC-5 | allow 外の接続と `getpeername()` 失敗は要求受信 0 byte、接続枠 0、protocol 生成 0、daemon 呼出し 0 で黙って close する。callback は canonical peer IPv4 を渡し、`_Slot`、auth、rate limit がその不変値だけを使う。 | instrument した reader、slot、protocol factory、peer source、fake daemon の回数。 |
| AC-6 | allow 内の接続だけが接続枠と protocol へ到達する。loopback allow 省略は `127.0.0.0/8` となる。allow 外の接続を 16 本以上張っても許可接続の 16 枠を消費しない。 | 実 accepted fd と slot 数の L-1 / L / L+1。 |
| AC-7 | 非 loopback の Host 欠落、重複、設定 IPv4 / port との値違い、hostname、許されない port 省略を GET / HEAD とも 400 にする。port 80 だけ省略を許す。parser が除く外側 OWS の wire 差は同じ field-value として扱い、parser 後の内部空白は拒否する。 | parser が返す field-value、HTTP 応答、HEAD body 0、daemon 呼出し 0 / 1。 |
| AC-8 | loopback の Host をちょうど 1 個にし、設定 IPv4 と case-insensitive `localhost`、port 80 省略だけを §3.2 どおり許す。Host の外側 OWS と内部空白の観測点を AC-7 と同じにする。 | 欠落、1 個、重複、値違い、大小文字、OWS、port 80 / 非 80 の GET / HEAD fixture。 |
| AC-9 | 隔離前に接続して backlog に入れた connection が隔離前に handler、protocol、daemon へ届かず、隔離と READY 後にだけ処理される。 | phase ごとの callback 回数。 |
| AC-10 | LAN bind 後も隔離 process の新規 AF_INET / AF_INET6 socket、bind、listen が拒否される。trace 固定の `accept4` だけで実要求が通る。 | 実 seccomp process と syscall trace。 |
| AC-11 | probe が bind、stdlib 共通 interface 列挙、allow、auth、secret、実 bind / listen、`getsockname()` を決定木順に検査する。成功と全失敗で state dir、stage file、listener 残骸を作らず、起動と probe の 1〜6 は同じ reason になる。 | probe 前後の filesystem / fd snapshot、rc、stdout / stderr、production との対照表。 |
| AC-12 | §2.3 の artifact / bind 4 値 / allow 5 値 / auth / secret の全直積を表だけから機械生成し、config、probe、production が各 fixture で同じ最終 reason 1 個だけを返す。T1 の `auth=none`、mixed-unrestricted、lookup-failed / nonloop-absent も完了条件にし、複合不正は表の最初の行だけを返す。表外 override は fixture generator に持たない。 | 生成 fixture 数と被覆証明、表の行 ID、3 入口の reason 対照。 |
| AC-13 | T1 / T2 とも `auth = "none"` は secret dir 不在を許可する。安全な残骸を `secret_unexpected`、symlink、未知 entry を含む unsafe な状態を `secret_invalid` で拒否し、secret の存在だけでは auth が有効にならない。 | T1 / T2 artifact の probe と production の rc 71 と固定 reason。 |
| AC-14 | username `afx` と正しい password だけが通る。別 username と正しい password、`afx` と誤 password は同じ失敗になる。 | 200 / 401 と daemon 呼出し 1 / 0。 |
| AC-15 | Authorization の欠落、重複、scheme 違い、区切り無し、tab、2 SP、前後 OWS、壊れた Base64、URL-safe alphabet、余分 padding、非 canonical pad bit、username 違い、password 違い、decoded 132 byte 超が同じ 401、同じ `WWW-Authenticate: Basic realm="afx"`、同じ固定 journal 文になる。canonical token だけが decode 後の再 encode と一致して通る。 | raw field-value、status、header、body、counter、journal。 |
| AC-16 | rate limit 中でない認証試行が、成功、誤り、壊れた入力を含め常に 32 byte 対 32 byteの `compare_digest` を 1 回だけ呼ぶ。rate limit 中は 0 回になる。 | spy した引数長と呼出し回数。 |
| AC-17 | 401 の GET は固定 error 本文を持つ。HEAD は本文 0 byteで GET と同じ Content-Length を持つ。 | GET / HEAD の status、body、Content-Length。 |
| AC-18 | 同じ送信元の失敗 4 / 5 / 6 回目が 401 / 401 / 429 になる。60 秒境界で古い失敗が契約どおり外れる。窓内 429 は比較と daemon 呼出しが 0 回で、queue に加わらない。 | fake clock、status、compare 回数、daemon 回数、queue 長。 |
| AC-19 | 全体の失敗 99 / 100 / 101 回目が 401 / 401 / 429 になる。異なる送信元でも全体窓を共有し、global 先行 429 は送信元 entry の lookup / allocation と per-source 評価を行わない。 | fake clock、全体 counter、送信元表の lookup / allocation 回数。 |
| AC-20 | 429 が 1..60 の Retry-After を持つ。global と送信元が同時に満杯で、global の残り 1 秒、送信元の残り 50 秒なら global だけから `1` を返す。global が空いている送信元 429 は送信元だけから計算する。HEAD の本文は空で GET と同じ Content-Length を持つ。 | 両窓の直積、窓境界の GET / HEAD 応答。 |
| AC-21 | 各試行の lookup 前に global と全送信元 queue を prune し、送信元 queue 長が 5、global が 100、実 IPv4 map が 100 entry を超えない。100 個の異なる送信元が各 1 回失敗して global が満杯になった後、101 個目の送信元は lookup / allocation されず、新 entry を作らない。期限切れ entry は次の lookup 前に回収でき、overflow bucket は存在しない。 | 99 / 100 / 101 送信元、59.999 / 60.000 / 60.001 秒、queue 長、map / 状態 object / allocation 数。 |
| AC-22 | allow、Host、auth、rate limit の各失敗で daemon 呼出しが 0 回になる。成功した GET / HEAD だけが既存 read-only method を呼ぶ。protocol / adapter / renderer の内部例外は 500、daemon transport 例外、timeout、非 200、code 付き又は schema 不正 response は 502 に固定する。 | 404、POST の 405、500、502 と fake daemon の method 別回数。 |
| AC-23 | journal、例外、probe、status、activity、event に IP、Host、Authorization、username、password、secret canary が無い。daemon activity と event の件数も増えない。 | 各出力と保存先の個別 canary 検査。 |
| AC-24 | 初回 enable の完全な既存設定は候補 UI も再質問も行わず、不足設定だけを順に聞く。ただし `auth=none` と安全な残骸には none の再選択による明示 `secret_action=remove` を要求し、unlink + directory fsync の後だけ進む。3 回の probe 失敗後は設定を保存済みでも enable stage が存在しない。`--reconfigure` は完全でも目標値を再質問する。明示 `--non-interactive` は TTY より優先し、reconfigure では完全な `--target-config`、必要なら password input file を要求する。不足時は何も書かず rc 2 `setup_input_required` になる。 | IO fake の質問列、通常 / reconfigure、TTY 直積、target file、unlink / fsync、stage 不在、writer 呼出し回数。 |
| AC-25 | `afx_ops.interfaces` の 1 実装が `socket.if_nameindex()` と `fcntl.ioctl()` の `SIOCGIFFLAGS` / `SIOCGIFADDR` / `SIOCGIFNETMASK` だけを使い、wizard は `afx-ops interfaces` subprocess 経由、probe / production は package 内呼出しで同じ fixture を共用する。本体から `afx_ops` を import しない。flags 失敗は列挙失敗、down interface は skip、up だが IPv4 を持たず固定 errno を返す interface は skip、address 取得後の netmask 失敗と想定外 errno は `interface_lookup_failed` とする。IPv6-only / up、down、対象 IPv4 ありの混在でも対象を返す。address 範囲から global / link / loopback を分類し `(scope, ifindex, address)` で安定 sort する。command の JSON schema、65536 byte 上限、5 秒 timeout、rc / stderr 違反を本体が固定 reason に写像し、候補 0 は `bind_not_local` になる。 | syscall / ioctl spy、errno 集合、混在 fixture、schema / stdout / timeout 境界、3 入口の同一結果、import pin、IO transcript。 |
| AC-26 | unrestricted の選択肢と警告が §5.2 の固定文であり、明示確認の後だけ `allow = ["0.0.0.0/0"]` を書く。未確認、EOF、取消しでは書かず、EOF / 取消しは rc 1 `setup_aborted` になる。限定 allow の空回答は再質問する。 | IO transcript と ops.toml。 |
| AC-27 | 生成 password が random 16 byte の padding なし base64url であり、初回だけ表示され、再実行、probe、status、stage、ログに現れない。 | deterministic RNG fixture、22 byte grammar、出力 canary。 |
| AC-28 | live config、目標 config staging、secret の atomic write が crash 前後に旧版又は新版の完全な file だけを残す。目標 staging は全文と digest が一致し、`web.password` と secret staging は常に mode `0600` である。 | rename / fsync fault injection、digest、mode、directory durability。 |
| AC-29 | `feature_config.py` の writer が port、bind、allow、auth を書き、再読取りで scalar 型、list 順序、値を保持する。単一 IPv4 allow の `/32` 意味も保持する。 | write → collect → write → read の round-trip。 |
| AC-30 | `afx ops status web` が固定形式の公開範囲 1 行を出し、限定 allow の個別 IP と password を出さない。reconfigure 中は `reconfigure <stage>` と実 unit 状態を出して `enabled` と表示せず、停止 stage は inactive / failed、公開範囲は検証済み staging config を表示する。 | localhost、restricted、unrestricted、basic、全 reconfigure stage の出力全文。 |
| AC-31 | T1 単独版は localhost 又は非空の限定 allow 付き LAN だけを起動する。singleton `0.0.0.0/0` と `auth = "basic"` は優先順位どおり `unrestricted_not_supported` / `auth_not_supported`、`0.0.0.0/0` と他 network の混在は先に `config_invalid` で拒否し、production と probe が同じ固定 reason / rc 71 を返す。 | T1 artifact の production 起動と probe、mixed-unrestricted の順序違いの直積。 |
| AC-32 | `0.0.0.0` bind、IPv6 bind / allow、proxy header による peer 上書き、新しい変更 route が存在しない。 | schema、route 一覧、`_Slot` の peer 判定入力の pin。 |
| AC-33 | T1 / T2 の `FeaturePlan` の許可 secret 名は常に `{"web.password"}` で、必須集合は検証済み auth だけから決まる。T1 の none でも dir 不在を許可し、存在時は検査済み dir fd から残骸と未知 entry を判定する。 | T1 / T2 plan object、openat spy、auth / dir / entry 直積。 |
| AC-34 | 手入力 password は 16〜128 byte の visible ASCII (`:` を含む) だけを受理し、space、改行、非 ASCII と境界外を `secret_invalid` にする。Basic は `b"afx:" + password` 全体を hash し、password 中の `:` を正しく認証する。 | 15 / 16 / 128 / 129 byte、文字 class、複数 colon、digest 入力 spy。 |
| AC-35 | readiness 1 回の accept は 64 以下で残りを次 tick に譲る。`EMFILE` / `ENFILE` は reader 解除後 50 ms backoff、連続 200 回で listener close、`accept_exhausted`、rc 72 になる。accept syscall が 1 本成功すれば allow / callback 結果にかかわらず連続回数を reset する。64 本目成功の reset、次 tick の EMFILE、reader 解除、timer だけによる resume の順を固定する。accept 3 counter は全 event を数え、backoff は episode 先頭かつ最大 10 行、callback failure は最大 10 行、exhausted はちょうど 1 行で、固定文以外を出さない。 | 63 / 64 / 65 境界 × EMFILE、実 listener flood、fake loop の reader / timer / tick 順、counter、10 / 11 件 sampling、listener、rc、journal 全文。 |
| AC-36 | 隔離の fd 自己検査時点の保持 fd は `{0,1,2,listener,notify}` だけで、secret dir / password / config / interface 列挙 socket fd が無い。READY 後は notify も閉じる。 | `/proc/self/fd` と syscall trace の phase 別 snapshot。 |
| AC-37 | `operation = reconfigure` の有限 stage が schema で検証され、目標 `ops.toml` 全文、元の terminal `enable_record`、`reconfigure_afx_before_stop` / `reconfigure_ops_before_stop` を durable に保持する。`basic → none` と `none + 安全な残骸 → none` は停止、password unlink、dir fsync、config 保存の順で、material 変更前の全失敗は rollback、以後は forward recovery する。開始時に afx が inactive なら probe 後も afx / ops を起動せず、`ops_active=false` / `ops=starts_with_afx` で terminal へ戻る。 | afx / ops の active-state 直積、全 stage × 各 syscall 前後の fault injection、staging 全文 / digest、stage / unit / status / fsync 順。 |
| AC-38 | `none → basic` は停止、secret atomic 保存、dir fsync、staging 全文から config 保存の順で進む。各 stage が unit、live / staged config、secret の事後条件を照合する。afx が開始時に active だった場合だけ start → server 認証 → READY を要求する。両 finalize 経路の `enable/enabled` record は元の `principal` / `registered` / `artifacts` / `wants` / `afx_before_stop` / `committed` を保持し、active 経路は `ops_active=true`、inactive 経路は `ops_active=false` にする。unsafe な手置き secret は `secret_invalid` で無変更とし、material 変更前は必ず rollback、以後は password を再生成せず forward recovery する。 | 全 stage × fault injection、active / inactive finalize record 全文、canary、mode、digest、unit / status / recovery 文。 |
| AC-39 | request line / header / body の malformed と上限境界が既存契約の 400 / 413 になり、Host / auth より先に止まる。405 は POST 等で観測する。HEAD は実際に到達する 400 / 401 / 404 / 413 / 429 / 500 / 502 について body 0 と GET と同じ Content-Length を検査する。protocol 内部例外は 500、daemon / backend 障害は 502 にする。 | 各 parser 上限の L-1 / L / L+1、POST 405、到達可能な全 HEAD status、daemon 回数。 |
| AC-40 | 下記の有限な mutation ID を 1 個ずつ有効にし、対応 AC が少なくとも 1 個失敗する。T1 完了時は T1 集合だけ、T2 完了時は T1 + T2 集合を実行する。 | mutation ID、対応 AC、意図した failure の CI 記録。 |

AC-40 の mutation 集合は次に固定する。各 mutation は表の反転だけを行い、複数条件を同時に変えない。

| 集合 | mutation ID | 反転する条件 | 検出する AC |
|---|---|---|---|
| T1 | `M-T1-BIND-CANON` | 先頭ゼロ付き bind を受理する。 | AC-1 |
| T1 | `M-T1-BIND-LOCAL` | 列挙成功後に存在しない bind を受理する。 | AC-1、AC-12 |
| T1 | `M-T1-IFACE-DIVERGE` | wizard だけ権威的列挙と異なる候補を使う。 | AC-1、AC-11、AC-25 |
| T1 | `M-T1-ALLOW-MISSING` | 非 loopback の allow 省略を通す。 | AC-4、AC-31 |
| T1 | `M-T1-ALLOW-UNRESTRICTED-MIX` | `0.0.0.0/0` と限定 network の混在を限定 allow として通す。 | AC-3、AC-12、AC-31 |
| T1 | `M-T1-CAP-UNRESTRICTED` | T1 で singleton unrestricted を通す。 | AC-12、AC-31 |
| T1 | `M-T1-CAP-BASIC` | T1 で basic を通す。 | AC-12、AC-31 |
| T1 | `M-T1-NONE-RESIDUE` | `auth=none` の安全な password 残骸を無視する。 | AC-12、AC-13、AC-33 |
| T1 | `M-T1-UNKNOWN-SECRET` | 許可外 secret entry を通す。 | AC-13、AC-33 |
| T1 | `M-T1-ALLOW-LATE` | allow 判定を slot 生成後へ移す。 | AC-5、AC-6 |
| T1 | `M-T1-ALLOW-PEER-HEADER` | allow source を accepted peer でなく proxy header から導出する。 | AC-5、AC-32 |
| T1 | `M-T1-HOST-MISSING` | Host 欠落を通す。 | AC-7、AC-8 |
| T1 | `M-T1-HOST-DUPLICATE` | Host 重複を通す。 | AC-7、AC-8 |
| T1 | `M-T1-HOST-MISMATCH` | 設定 bind と異なる Host を通す。 | AC-7、AC-8 |
| T1 | `M-T1-PRE-READY-ACCEPT` | READY 前に backlog を処理する。 | AC-9 |
| T1 | `M-T1-SECCOMP-INET` | 隔離後の新規 AF_INET socket を許す。 | AC-10、AC-32 |
| T1 | `M-T1-LISTENER-ADDRESS` | `getsockname()` の address 不一致を見逃す。 | AC-2、AC-11 |
| T1 | `M-T1-LISTENER-PORT` | `getsockname()` の port 不一致を見逃す。 | AC-2、AC-11 |
| T1 | `M-T1-ACCEPT-64` | readiness 1 回で 65 本目を accept する。 | AC-35 |
| T1 | `M-T1-EMFILE-RESET` | accept syscall 成功後も EMFILE 連続回数を reset しない。 | AC-35 |
| T1 | `M-T1-SECRET-FD` | secret fd を隔離へ残す。 | AC-36 |
| T1 | `M-T1-INTERFACE-FD` | interface 列挙 socket fd を隔離へ残す。 | AC-36 |
| T1 | `M-T1-STATUS-500-502` | protocol 内部例外を 502 にする。 | AC-22、AC-39 |
| T2 | `M-T2-BASIC-2SP` | Basic と token 間の 2 SP を受理する。 | AC-15 |
| T2 | `M-T2-BASIC-TAB` | Basic と token 間の tab を受理する。 | AC-15 |
| T2 | `M-T2-BASIC-OWS` | token 後方の OWS を受理する。 | AC-15 |
| T2 | `M-T2-BASIC-BASE64` | URL-safe Base64 alphabet を受理する。 | AC-15 |
| T2 | `M-T2-COMPARE-LENGTH` | credential を可変長のまま比較する。 | AC-16、AC-34 |
| T2 | `M-T2-SOURCE-LIMIT` | 送信元 6 回目を 401 にする。 | AC-18 |
| T2 | `M-T2-GLOBAL-LIMIT` | global 101 回目を 401 にする。 | AC-19 |
| T2 | `M-T2-GLOBAL-RETRY` | global 先行 429 で per-source の Retry-After を使う。 | AC-19、AC-20 |
| T2 | `M-T2-RATE-PEER-HEADER` | rate key を accepted peer でなく proxy header から導出する。 | AC-5、AC-18、AC-32 |
| T2 | `M-T2-MAP-101` | global 満杯時に 101 個目の source entry を割り当てる。 | AC-19、AC-21 |
| T2 | `M-T2-PASSWORD-OUTPUT` | password を status へ含める。 | AC-23、AC-27、AC-30 |
| T2 | `M-T2-AUTHORIZATION-OUTPUT` | Authorization を journal へ含める。 | AC-23 |
| T2 | `M-T2-UNRESTRICTED-CONFIRM` | 明示確認なしで unrestricted を保存する。 | AC-26 |
| T2 | `M-T2-RECONFIG-STAGING` | 目標 config 全文を durable 保存せず digest だけを残す。 | AC-28、AC-37、AC-38 |
| T2 | `M-T2-RECONFIG-ROLLBACK` | material 変更前の失敗で rollback しない。 | AC-37、AC-38 |
| T2 | `M-T2-NONE-RESIDUE-UNLINK` | `none + 安全な残骸 → none` で unlink を省く。 | AC-13、AC-24、AC-37 |
| T2 | `M-T2-NONE-RESIDUE-FSYNC` | 残骸 unlink 後の directory fsync を省く。 | AC-24、AC-28、AC-37 |
| T2 | `M-T2-RECONFIG-STATUS` | reconfigure の停止 stage を enabled と表示する。 | AC-30、AC-37 |
| T2 | `M-T2-RECONFIG-INACTIVE-START` | 開始時に afx が inactive でも ops-web を start する。 | AC-37、AC-38 |
| T2 | `M-T2-RECONFIG-FINAL-RECORD` | finalize 時に元の `wants` を空 list へ置換する。 | AC-38 |
| T2 | `M-T2-HEAD-401` | HEAD 401 の Content-Length を 0 にする。 | AC-17、AC-39 |
| T2 | `M-T2-HEAD-413` | HEAD 413 の Content-Length を 0 にする。 | AC-39 |
| T2 | `M-T2-HEAD-429` | HEAD 429 の Content-Length を 0 にする。 | AC-20、AC-39 |
| T2 | `M-T2-HEAD-500` | HEAD 500 の Content-Length を 0 にする。 | AC-39 |
| T2 | `M-T2-HEAD-502` | HEAD 502 の Content-Length を 0 にする。 | AC-39 |

## 10. 変更ファイルと task 表

### 10.1 変更ファイル (予定)

| 領域 | ファイル | 変更 |
|---|---|---|
| 設定 | `afx-ops/afx_ops/config.py` | bind、allow、auth の grammar、組み合わせ、固定 reason を追加する。 |
| interface | `afx-ops/afx_ops/interfaces.py`、`afx-ops/afx_ops/cli.py` | stdlib / ioctl の権威的な列挙を 1 実装だけ置き、afx-ops 内の probe / production と `afx-ops interfaces` から共用する。固定 JSON schema と bounded command を追加する。 |
| pre-bind / allow | `afx-ops/afx_ops/isolation/accept_loop.py` | 設定 address の bind と listener 検査、accept 直後の peer allow を追加する。 |
| secret / 起動 | `afx-ops/afx_ops/isolation/startup.py` | T1 で none の secret dir / 未知 entry / 残骸検査、T2 で basic の `web.password` 読取り、設定 bind の pre-bind を追加する。 |
| seccomp | `afx-ops/afx_ops/isolation/profiles.py` | 緩和せず、現行の AF_INET / AF_INET6 拒否と accept4 だけを pin する。 |
| HTTP | `afx-ops/afx_ops/web/{runtime,request,headers}.py` | allow 配線、Host、Basic、rate limit、401 / 429 / HEAD、固定 counter / journal を追加する。 |
| probe | `afx-ops/afx_ops/probe.py` | 共通 interface 列挙を呼び、allow、auth / secret、実 bind / listen / getsockname を検査する。 |
| CLI | `src/agentic_fx/cli.py` | `afx ops enable` に `--non-interactive`、`--reconfigure` を追加する。 |
| wizard / writer | `src/agentic_fx/ops_setup/{interactive,feature_config}.py` と bounded subprocess helper | `afx-ops interfaces` 経由の候補列挙、質問、初回 none 残骸の明示 remove、atomic secret、目標 config staging、round-trip、通常 enable の無再質問と reconfigure の再質問を追加する。本体から afx_ops を import しない。 |
| stage / status | `src/agentic_fx/ops_setup/stages.py` | `operation = reconfigure` と有限 stage、開始時 afx / ops 状態と旧 terminal record の durable 保存、active / inactive 分岐、公開範囲と進行中 / 停止状態の固定表示を追加する。 |
| test | `afx-ops/tests/{isolation,web}/`、`afx-ops/tests/test_{config,probe}.py`、`tests/ops_setup/` | §9 の T1 / T2 観測点、境界値、実 process を追加する。 |
| 運用文書 | afx-ops の運用文書 | 平文 HTTP、到達範囲、password の reconfigure 手順、残余リスクを記載する。 |

### 10.2 task 表

| task | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|
| T1 bind + allow | config と writer、stdlib interface 列挙と `afx-ops interfaces` 契約、設定 address の pre-bind / getsockname、accept 直後の allow、全 bind の Host 検査、probe、seccomp pin、`auth=none` の secret dir / 未知 entry / 残骸 password 検査と固定許可 secret 名を実装する。単独公開は localhost 又は非空の限定 allow 付き LAN に限る。singleton `0.0.0.0/0` は `unrestricted_not_supported`、mixed-unrestricted は `config_invalid`、`auth = "basic"` は `auth_not_supported` で拒否する。 | AC-1〜AC-13 の T1 / none 部分、AC-25 の列挙 command、AC-29、AC-31〜AC-33、AC-35、AC-36、AC-39 の 400 / 404 / 413 / 405 / 500 / 502 部分、AC-40 の T1 集合。allow 外の受信 0、fairness、EMFILE、accept journal、protocol 0、daemon 0、残骸 secret、実 bind、実 seccomp を含む。T2 の Basic、rate limit、wizard、reconfigure の完成は要求しない。 | M |
| T2 auth + 運用導線 | `auth=basic` の password 読取り / 比較、100 entry 上限の rate limit、本体から `afx-ops interfaces` を呼ぶウィザード、初回残骸の明示 remove、active / inactive を保存する durable reconfigure、無制限 LAN の解禁、status、運用文書を実装する。T1 の none 残骸検査は維持する。 | AC-12 の T2 / basic 部分、AC-14〜AC-28、AC-30、AC-34、AC-37〜AC-39 の追加 status、AC-40 の T1 + T2 集合。Authorization の全失敗形、401 / 413 / 429 / HEAD、rate limit、bounded discovery、停止中 afx の reconfigure、crash recovery、canary、password 一度表示を含む。 | M |

T1 は T2 より先に統合できる。
T1 の LAN 公開は allow-only の限定公開であり、無制限公開ではない。
T2 は T1 の allow と Host の境界を緩めない。

## 11. 後続と非目標

- TLS 終端と証明書の生成又は更新。
- IPv6 の bind、allow、rate-limit key。
- `0.0.0.0` bind と複数 interface の同時 listener。
- reverse proxy の信頼設定と `Forwarded` / `X-Forwarded-For` の解釈。
- firewall、nftables、cloud security group の自動設定。
- cookie session、明示 logout、session rotation。
- password の live reload。
- hostname 又は mDNS 名での bind と Host 許可。
- allow の DNS 名、interface 名、動的 group。
- 認証失敗を daemon の監査記録へ保存する機能。
- rate-limit 状態の process 間永続化。

## 12. 残余リスク

| 点 | 利用者責任として残ること |
|---|---|
| 平文 HTTP / 能動的 LAN 中間者 | 同じ経路を観測又は改変できる者は Basic credential と表示内容を盗聴・改変できる。ARP spoofing 等の能動的中間者も防げず、Basic は暗号化でも server 認証でもない。 |
| IP allowlist | IP spoofing、ARP spoofing、DHCP の払い出し変更、NAT により、端末の人間又は機器 identity を保証できない。 |
| 到達範囲 | allow 内の侵害済み端末は到達できる。unrestricted では、LAN、VPN、port-forward 等を含め、このアドレスへ到達できる全 IPv4 source が戦略の diff を読めます。host firewall、router、container、cloud network の実効範囲を製品は検証しない。 |
| allow 外洪水 | 64 accept / tick と早期 close で公平性と資源消費を有界化するが、回線、kernel backlog、CPU を飽和させる DoS は防げない。 |
| slowloris | 接続枠 16 と 30 秒 timeout は被害を制限するが、allow 内 source が枠を占有して一時的に正規要求を妨げられる。 |
| global rate limit | 分散又は共有 source の失敗により global 100 件窓へ達すると、正しい利用者も最大 60 秒 lockout される。 |
| 分散総当たり | 送信元別 / global の固定窓は試行速度を制限するが、時間を空けた攻撃、複数 process / host、credential の別経路漏えいを防がない。 |
| rate-limit の再起動 reset | rate-limit は process 内状態であり、通常再起動や `accept_exhausted` 後の systemd 再起動で履歴が消える。永続化は今回の非目標であり、再起動後は新しい送信元別 5 回 / global 100 回の窓が始まる。 |
| 生成 password の初回表示 | 生成値は一度だけ表示するため、その時点の画面共有、端末 scrollback、肩越しの観察から漏えいし得る。製品は端末側の記録を消去できない。 |
| Host 検査 | DNS rebinding や誤転送への defense-in-depth であり、network reachability、allow、auth、TLS の代替ではない。 |
| Basic logout | browser が credential を保持するため、確実な logout 操作を提供できない。browser を閉じても保持が続く場合がある。 |
| bind address の変化 | DHCP 等で設定 address が失われると、probe 又は次回起動が `bind_not_local` / `probe_bind_failed` で止まる。自動で別 address へ広げない。 |
| read-only の限界 | 変更権限は無くても、損益、稼働状態、ログ、承認詳細、戦略 diff は機密情報である。 |

## 13. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-23 | v0.1 | C0 下書きを新規作成。bind、allow、auth、接続順、隔離、probe、ウィザード、AC、T1 / T2 を定義した。 | チケットのユーザー裁定と第二意見 2 回目の是正 3 点を amendment spec に固定するため。 | — |
| 2026-10-23 | v0.2 | 固定 reason 決定木、FeaturePlan / secret fd、durable reconfigure、Host / password / bind scope、accept fairness、rate-limit queue、discovery / 非対話契約、旧 spec 置換、AC-1〜AC-40、残余リスクを確定した。 | 設計レビュー 1 周目 sol、High 5 / Medium 8 を全採用。 | — |
| 2026-10-23 | v0.3 | 一意な組み合わせ表、目標 config 全文を使う reconfigure recovery、T1 の none secret 検査、EMFILE 契約、stdlib interface 列挙、global 先行 Retry-After、Host / Basic wire grammar、観測可能な status / mutation 集合を確定した。 | 設計レビュー 2 周目 sol、High 3 / Medium 5 を全採用し、1 周目の未閉鎖 7 件 (#1、#3、#5、#7、#8、#10、#12) を閉鎖。 | — |
| 2026-10-23 | v0.4 | mixed-unrestricted の拒否、表だけで完結する reason oracle、`afx-ops interfaces` subprocess 契約、停止中 afx の reconfigure、100 entry rate map、初回残骸削除導線、HEAD 413、accept journal、ioctl skip、旧 spec secret 置換、再起動 reset リスク、1 変異 1 ID を確定した。 | 設計レビュー 3 周目 sol、High 3 / Medium 7 / Low 1 を全採用し、2 周目の未閉鎖 4 件 (#1、#2、#5、#8) を閉鎖。 | — |

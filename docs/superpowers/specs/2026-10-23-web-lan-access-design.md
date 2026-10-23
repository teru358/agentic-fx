# 同梱 web の公開範囲設計

**版:** v0.2
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
| 実装の分割 | T1 は bind、allow、Host、probe、config、writer を実装する。T2 は auth、rate limit、ウィザード、無制限 LAN、status、運用文書を実装する。 |

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
| `allow` | string の list、loopback では省略可 | 各要素は canonical な数値 IPv4 又は canonical な IPv4 CIDR とする。単一 IPv4 は内部で `/32` として扱う。host bit を持つ CIDR、空文字、空 list、重複 network、IPv6、IPv4-mapped IPv6、余分な空白を拒否する。非 loopback では key 自体を必須にする。無制限は唯一 `0.0.0.0/0` の明示値で表す。 |
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

| bind | allow | auth | secret | 結果 |
|---|---|---|---|---|
| loopback | 省略 | `none` | 無し | 許可する。既定形である。 |
| loopback | 非空 | `none` | 無し | 許可し、loopback peer に allow を適用する。 |
| loopback | 省略又は非空 | `basic` | 安全な `web.password` 有り | 許可する。 |
| loopback | 任意 | `none` | `web.password` 有り | `secret_unexpected` で拒否する。 |
| 任意 | 任意 | `basic` | `web.password` 無し | `secret_missing` で拒否する。 |
| 非 loopback | allow key 無し | 任意 | 任意 | `allow_required` で拒否する。 |
| 非 loopback | 空 list | 任意 | 任意 | `config_invalid` で拒否する。 |
| 非 loopback | 非空 | `none` | 無し | 許可する。`0.0.0.0/0` も明示値として許可する。 |
| 非 loopback | 非空 | `basic` | 安全な `web.password` 有り | 許可する。 |

loopback で allow を省略した場合、内部 allow は `127.0.0.0/8` とする。

allow と auth は loopback でも任意に強化できる。
非 loopback では allow と auth のどちらか一方だけを必須にするのではなく、allow を常に必須にする。
auth は allow の代わりではない。

### 2.4 固定 reason

外部へ返す web 固有 reason と rc は次の集合に固定する。
設定値、IP、CIDR、path、header、secret、例外文は reason の後ろへ付けない。

| reason | rc | 条件 |
|---|---:|---|
| `config_invalid` | 71 | TOML、型、未知 key、`port`、`bind`、`allow`、`auth` の grammar が不正である。 |
| `allow_required` | 71 | 非 loopback で `allow` key が無い。 |
| `unrestricted_not_supported` | 71 | T1 単独版で `allow = ["0.0.0.0/0"]` を使おうとした。 |
| `auth_not_supported` | 71 | T1 単独版で `auth = "basic"` を使おうとした。 |
| `interface_lookup_failed` | 71 | interface 列挙の起動、timeout、非 0、出力上限、JSON 又は必要 field の解釈に失敗し、bind の局所性を判定できない。 |
| `bind_not_local` | 71 | 数値 IPv4 としては正しい非 loopback bind が、本機の up 状態の interface の global / link unicast IPv4 に存在しない。 |
| `secret_invalid` | 71 | 存在する secret dir 又は `web.password` の owner、mode、型、symlink、size、byte grammar、open / read 検査に失敗したか、許可外の secret 名が存在する。 |
| `secret_missing` | 71 | `auth = "basic"` だが安全な `web.password` が無い。secret dir 不在も含む。 |
| `secret_unexpected` | 71 | `auth = "none"` で安全な `web.password` が存在する。 |
| `listener_invalid` | 71 | production 起動の bind / listen 又は listener 自己検査に失敗した。 |
| `probe_bind_failed` | 73 | probe の実 bind / listen 又は `getsockname()` 一致に失敗した。 |

複数の不正が同時にある場合は、次の決定木で最初に該当した reason だけを返す。

1. 既存契約の config dir 不在 / unsafe を `config_dir_missing` / `config_dir_invalid` とする。
2. TOML と各 key の単独 grammar 違反を `config_invalid` とする。
3. 非 loopback の allow key 欠落を `allow_required` とする。
4. T1 では unrestricted、basic の順に capability を検査し、それぞれ `unrestricted_not_supported`、`auth_not_supported` とする。
5. interface 列挙自体の失敗を `interface_lookup_failed`、列挙成功後の不在を `bind_not_local` とする。loopback は列挙しない。
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

### 3.2 parse と Host

allow 通過後は既存の request line 2048 byte、header 8192 byte、body 65536 byte、header 64 件、接続 30 秒の上限で parse する。
GET と HEAD だけを route へ進める。
body、Transfer-Encoding、不正な Content-Length、既存の拒否対象は従来どおり拒否する。

全 bind で `Host` header をちょうど 1 個要求する。
非 loopback の値は設定 IPv4 と一致し、loopback の値は設定 IPv4 又は ASCII case-insensitive の `localhost` と一致しなければならない。
port は設定 port と一致する `:<port>` を要求し、設定 port が 80 の場合だけ省略を許す。
数値 IPv4 の表記は設定 `bind` と byte-for-byte で一致させる。
空白の正規化、他の hostname、別 port、複数 Host を許さない。
欠落、重複、値違いは GET / HEAD とも 400 とし、daemon を呼ばない。

### 3.3 Basic 認証

Host を通過した後、route 解決と daemon 呼出しより前に認証する。
`auth = "none"` では Authorization を認証には使わない。
`auth = "basic"` では Authorization をちょうど 1 個要求する。
scheme は ASCII case-insensitive の `Basic` とする。
token は最大 176 ASCII byte とし、strict Base64 で decode する。
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

各試行では送信元表を lookup する前に、global queue、全実 IPv4 entry、overflow bucket の期限切れ時刻を prune し、空になった実 IPv4 entry を回収する。
通常の送信元 queue と overflow queue の保持長はそれぞれ最大 5、global queue は最大 100 とする。
429 にした試行はどの queue にも加えない。
global queue が既に上限なら送信元 entry の lookup / allocation より先に 429 とし、新しい entry を作らない。

429 は整数秒の `Retry-After` を持つ。
値は該当する送信元窓又は全体窓が次に 1 件空くまでの秒数を切り上げた値とし、最小 1、最大 60 とする。
送信元と全体の両方が超過していれば、大きい方を使う。

送信元表は実 IPv4 ごとに最大 1024 entry とする。
表が満杯の間に現れた未登録の送信元は、全て 1 個の overflow bucket を共有する。
overflow bucket は rate limit 上「1 送信元相当」であり、そこへ写像された全 IPv4 が同じ 5 件の queue と Retry-After を共有する。
各 entry と overflow bucket は 60 秒窓の失敗時刻だけを持つ。
失敗時刻が全て期限切れになった実 IPv4 entry は削除できる。
表の entry 数、各送信元の保持数、全体の保持数は固定上限を超えない。

429 でも daemon 呼出しは 0 回である。
rate limit の key に `X-Forwarded-For`、`Forwarded`、Host その他の header を使わない。

### 3.5 route と記録

allow、parse、Host、auth を全て通った要求だけを既存の route 解決へ渡す。
web principal は引き続き読み取り専用である。
認証失敗を daemon activity 又は event に書かない。

process 内 counter は `web_peer_rejected_total`、`web_peer_lookup_failed_total`、`web_auth_failed_total`、`web_auth_limited_total` の 4 個に固定する。
journal へ出す文は `afx-ops: web_peer_rejected`、`afx-ops: web_peer_lookup_failed`、`afx-ops: web_auth_failed`、`afx-ops: web_auth_limited` の 4 種だけとする。
journal は期間ごとの集約又は固定上限の bounded sampling とし、allow 外 flood、lookup failure、認証失敗の件数に比例して無制限に書かない。
journal 文には IP、port、Host、Authorization、username、password、secret、回数、例外文を足さない。
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
fd 整理と隔離の自己検査時点で保持を許す fd は `{0, 1, 2, listener, notify}` だけとし、secret dir fd、password fd、config fd、interface 列挙用 pipe を残さない。
`READY=1` の送信後は既存契約どおり notify fd も閉じる。

## 5. probe と enable / ウィザード

### 5.1 probe

`afx-ops probe web` は state dir と stage file を作らない。
stdout は成功時も失敗時も空とする。
stderr は失敗時に `afx-ops: <固定 reason>` の 1 行だけとする。
secret、設定値、IP、CIDR、path、例外文を出さない。

probe は §2.4 の決定木と同じ順序で次を検査する。

1. config dir と TOML を読み、`port`、`bind`、`allow`、`auth` の全単独 grammar を検査する。
2. 非 loopback の allow key 必須条件を検査する。
3. T1 artifact では unrestricted、basic の順に capability を検査する。T2 ではこの段を通過する。
4. 非 loopback bind が本機の up 状態の interface の global / link unicast IPv4 として実在することを検査する。列挙不能は `interface_lookup_failed`、列挙成功後の不在は `bind_not_local` とする。
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
`--reconfigure` は enabled 中の web にも使え、次の durable 遷移を行う。
状態列は `enabled → ops-web 停止 → 設定変更 → probe → 再起動 → ready → enabled` に固定する。

1. setup lock を取得し、旧設定の digest、目標設定の digest、phase を secret を含めず reconfigure marker に atomic 保存する。
2. `ops-web` を停止し、停止を確認する。ここまでの失敗は marker を除去して旧設定のまま再起動する rollback を許す。
3. §5.2 の auth 遷移順で設定と secret を変更する。最初の material 変更を directory fsync した後は旧状態へ自動 rollback せず、marker から新状態へ forward recovery する。
4. probe を実行する。失敗時は unit を停止した `staged/reconfigure` に保ち、enabled と表示しない。
5. probe 成功後に `ops-web` を再起動し、server 認証と `READY=1` を確認してから marker を除去し、最後に `enabled` を durable にする。

`basic → none` は `web.password` を unlink して secret directory を fsync した後、`auth = "none"` を含む `ops.toml` を一時 file + fsync + atomic rename + directory fsync で保存する。
unlink 後、設定 rename 前の crash は `auth = "basic"` + password 無しとなり `secret_missing` で閉じる。
この commit point 後は失われた password を推測又は再生成して rollback せず、再実行が `auth = "none"` への forward recovery を完了する。

`none → basic` は安全な `web.password` を一時 file + fsync + atomic rename + secret directory fsync で先に保存し、その後 `auth = "basic"` を含む `ops.toml` を atomic 保存する。
secret 保存後、設定 rename 前の crash は `auth = "none"` + password 有りとなり `secret_unexpected` で閉じる。
この commit point 後は password を自動削除して rollback せず、再実行が `auth = "basic"` への forward recovery を完了する。

手置きの unsafe な secret dir / `web.password` を見つけた場合は `secret_invalid` で停止し、削除、chmod、chown、置換をしない。
回復手順は、利用者が所有者・mode・型・内容を直すか、unit 停止中に対象を退避して `--reconfigure` を再実行することだけを固定文で案内し、path や内容は表示しない。

対話時は不足している設定だけを次の順で尋ねる。

1. 「自機のみ」又は「LAN」を尋ねる。
2. 既定は「自機のみ」とする。
3. LAN なら待受 address を候補から選ばせる。
4. LAN なら誰が見られるかを尋ねる。
5. 必要なら allow の IPv4 / CIDR を入力させる。
6. 必要なら password を生成するか利用者が決めるかを尋ねる。
7. 設定と secret を atomic に保存して probe を実行する。

LAN の候補列挙は `ip -j addr show up` を引数配列で呼ぶ。
shell は使わない。
短い timeout と固定の stdout / stderr byte 上限を設ける。
JSON のうち `scope = "global"` の非 loopback IPv4 だけを候補にする。
候補は `(ifindex, address)` で安定 sort する。
1 候補ならそれを既定値にし、複数候補なら利用者の明示選択を必須にする。
候補が無い場合は canonical な数値 IPv4 を手入力させる。
手入力は up interface に実在する unicast IPv4 であれば scope `global` 又は `link` を許し、probe で実在を再確認する。

`ip` 不在、timeout、非 0 終了、stdout 上限超過、壊れた JSON 又は必要 field の欠落は discovery 失敗とする。
対話時は固定警告 `interface discovery failed; enter a local IPv4 manually` を 1 回だけ出して手入力へ進む。
非対話では、既存設定が grammar と組み合わせを満たして完全なら discovery 自体を実行せず probe へ進み、不足なら file を一切書かず rc 2 `setup_input_required` とする。

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

既存設定が grammar と組み合わせの検査を満たし、probe が成功するなら再質問しない。
既存 secret の内容を表示しない。
probe が失敗した場合だけ、固定 reason に対応する必要項目を再質問する。
`config_invalid` / `allow_required` は該当設定、`interface_lookup_failed` / `bind_not_local` / `probe_bind_failed` は bind、`secret_missing` / `secret_invalid` / `secret_unexpected` は auth と secret だけを対象にし、無関係な値を再質問しない。
probe は初回を含め最大 3 回とする。
3 回目も失敗した場合は最後の固定 reason を返し、初回 enable は `staged`、reconfigure は `staged/reconfigure` のまま unit を停止して fail closed にする。

allow 入力の空回答は再質問し、unrestricted 又は取消しと解釈しない。
全質問で EOF 又は明示取消しを受けた場合は以後書き込まず rc 1 `setup_aborted` とする。

非対話では port-only の旧設定を安全な localhost として扱える。
非対話で LAN を選ぶ場合は `bind`、非空の `allow`、`auth` を全て明示する。
不足があれば何も書かず rc 2 `setup_input_required` で fail closed とする。
無制限 LAN は `allow = ["0.0.0.0/0"]` が明示されていなければ成立しない。

### 5.3 status

`afx ops status web` は既存の stage / unit 行に続けて公開範囲を 1 行だけ出す。
形式は `web-access: bind=<IPv4>:<port> allow=<restricted|unrestricted|loopback-default> auth=<none|basic>` に固定する。
`restricted` は `0.0.0.0/0` 以外の allow がある状態を表す。
`unrestricted` は明示値 `0.0.0.0/0` を表す。
`loopback-default` は loopback bind かつ allow 省略を表す。
この行に allow の個別 IP、password、secret の存在 path を出さない。

## 6. 旧 spec の置換

本 spec と旧 spec が矛盾する場合、下表の範囲では本 spec を正本とする。
下表にない旧 spec の読み取り専用 principal、route、隔離、enable 状態機械は維持する。

| 旧 spec の箇所 | 置換内容 | 本 spec |
|---|---|---|
| 行 17、要点 | 「web は loopback の read-only 表示」を、設定済み bind / allow / auth の read-only 表示へ置き換える。web が承認権限を持たない点は維持する。 | §0、§3 |
| 行 33、§1.2 境界 | `ops-web` が扱う「UI 認証 secret」を、`auth = "basic"` のときだけ必須な `web.password` として具体化する。 | §2.2、§4 |
| 行 36、§1.2 主体 | 「localhost の利用者」を「設定した待受アドレスへ到達し、allow / Host / auth を通った利用者」に置き換える。 | §1、§3 |
| §3.1.1 `ops.toml` | web の `port` に `bind`、`allow`、`auth` を追加する。 | §2 |
| §3.3 web | loopback 固定・認証なしを、検証済み bind、allow、Host、任意 Basic 認証へ置き換える。 | §1〜§4 |
| §5 自己隔離 | loopback pre-bind を設定 address の pre-bind に置き換える。隔離後の新規 INET socket 拒否は維持する。 | §4 |
| §6.1 enable | staged の web 設定・secret・probe 検査と status の公開範囲表示を追加する。 | §5 |
| §7 初回ウィザード | web の最小質問と loopback probe を、4 段の公開範囲質問と新 probe へ置き換える。 | §5 |
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
| 3 | 手入力 bind は up interface に実在する unicast IPv4 なら scope `global` / `link` を許し、自動候補は `global` だけにする。 | link-local の明示運用は妨げず、自動提示は一般的な LAN address に限定する。 |

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
| AC-1 | `bind` の省略、`127.0.0.1`、up interface の global / link unicast IPv4 を受理する。hostname、先頭ゼロ、空白付き、`0.0.0.0`、IPv6、IPv4-mapped IPv6、interface に無い IPv4 を拒否する。列挙失敗と列挙成功後の不在を区別する。 | config / probe / production の rc 71 と `interface_lookup_failed` / `bind_not_local`。 |
| AC-2 | 指定した非 loopback IPv4 と port に実 bind + listen し、`getsockname()` が両方に完全一致する。別 address 又は別 port の listener を `listener_invalid` にする。 | 実 listener の `getsockname()` と起動 rc。 |
| AC-3 | allow の単一 IPv4、canonical CIDR、`0.0.0.0/0` を受理する。非 canonical CIDR、空 list、重複、IPv6、IPv4-mapped IPv6、空白付き、host bit 付き CIDR を拒否する。 | config reader と writer の再読取り。 |
| AC-4 | 非 loopback で allow key が無いと `allow_required` になる。loopback では allow 省略、限定 allow、`0.0.0.0/0` がそれぞれ契約どおり動く。 | 起動 rc と固定 reason。 |
| AC-5 | allow 外の接続と `getpeername()` 失敗は要求受信 0 byte、接続枠 0、protocol 生成 0、daemon 呼出し 0 で黙って close する。callback は canonical peer IPv4 を渡し、`_Slot`、auth、rate limit がその不変値だけを使う。 | instrument した reader、slot、protocol factory、peer source、fake daemon の回数。 |
| AC-6 | allow 内の接続だけが接続枠と protocol へ到達する。loopback allow 省略は `127.0.0.0/8` となる。allow 外の接続を 16 本以上張っても許可接続の 16 枠を消費しない。 | 実 accepted fd と slot 数の L-1 / L / L+1。 |
| AC-7 | 非 loopback の Host 欠落、重複、設定 IPv4 / port との値違い、hostname、許されない port 省略を GET / HEAD とも 400 にする。port 80 だけ省略を許し、正しい 1 個だけを通す。 | HTTP 応答、HEAD body 0、daemon 呼出し 0 / 1。 |
| AC-8 | loopback の Host をちょうど 1 個にし、設定 IPv4 と case-insensitive `localhost`、port 80 省略だけを §3.2 どおり許す。 | 欠落、1 個、重複、値違い、大小文字、port 80 / 非 80 の GET / HEAD fixture。 |
| AC-9 | 隔離前に接続して backlog に入れた connection が隔離前に handler、protocol、daemon へ届かず、隔離と READY 後にだけ処理される。 | phase ごとの callback 回数。 |
| AC-10 | LAN bind 後も隔離 process の新規 AF_INET / AF_INET6 socket、bind、listen が拒否される。trace 固定の `accept4` だけで実要求が通る。 | 実 seccomp process と syscall trace。 |
| AC-11 | probe が bind、interface、allow、auth、secret、実 bind / listen、`getsockname()` を決定木順に検査する。成功と全失敗で state dir、stage file、listener 残骸を作らず、起動と probe の 1〜6 は同じ reason になる。 | probe 前後の filesystem snapshot、rc、stdout / stderr、production との対照表。 |
| AC-12 | `auth = none/basic`、secret dir 不在/安全/unsafe、`web.password` 無し/安全/owner 違い/mode 違い/symlink/非通常 file/size・byte grammar 違反の直積が §2.3 と §2.4 の一意な reason になる。複合不正も決定木の最初の reason だけを返す。 | config、probe、production の直積 fixtureと優先順位 fixture。 |
| AC-13 | `auth = "none"` は secret dir 不在を許可する。安全な残骸を `secret_unexpected`、symlink を含む unsafe な残骸を `secret_invalid` で拒否し、secret の存在だけでは auth が有効にならない。 | probe と production の rc 71 と固定 reason。 |
| AC-14 | username `afx` と正しい password だけが通る。別 username と正しい password、`afx` と誤 password は同じ失敗になる。 | 200 / 401 と daemon 呼出し 1 / 0。 |
| AC-15 | Authorization の欠落、重複、scheme 違い、壊れた Base64、区切り無し、username 違い、password 違い、decoded 132 byte 超が同じ 401、同じ `WWW-Authenticate: Basic realm="afx"`、同じ固定 journal 文になる。 | status、header、body、counter、journal。 |
| AC-16 | rate limit 中でない認証試行が、成功、誤り、壊れた入力を含め常に 32 byte 対 32 byteの `compare_digest` を 1 回だけ呼ぶ。rate limit 中は 0 回になる。 | spy した引数長と呼出し回数。 |
| AC-17 | 401 の GET は固定 error 本文を持つ。HEAD は本文 0 byteで GET と同じ Content-Length を持つ。 | GET / HEAD の status、body、Content-Length。 |
| AC-18 | 同じ送信元の失敗 4 / 5 / 6 回目が 401 / 401 / 429 になる。60 秒境界で古い失敗が契約どおり外れる。窓内 429 は比較と daemon 呼出しが 0 回で、queue に加わらない。 | fake clock、status、compare 回数、daemon 回数、queue 長。 |
| AC-19 | 全体の失敗 99 / 100 / 101 回目が 401 / 401 / 429 になる。異なる送信元でも全体窓を共有し、global 先行 429 は送信元 entry を割り当てない。 | fake clock、全体 counter、送信元表の allocation 回数。 |
| AC-20 | 429 が 1..60 の Retry-After を持つ。HEAD の本文は空で GET と同じ Content-Length を持つ。 | 窓境界の GET / HEAD 応答。 |
| AC-21 | 各試行の lookup 前に全 queue を prune し、通常 / overflow queue 長が 5、global が 100 を超えない。送信元表は 1024 entry を超えず、1025 個目以降は「1 送信元相当」の overflow bucket と queue を共有する。期限切れ entry は lookup 前に回収でき、global 先行時は entry を割り当てない。 | 1023 / 1024 / 1025 送信元、59.999 / 60.000 / 60.001 秒、queue 長、状態 object / allocation 数。 |
| AC-22 | allow、Host、auth、rate limit の各失敗で daemon 呼出しが 0 回になる。成功した GET / HEAD だけが既存 read-only method を呼び、route / backend の 404 / 405 / 500 / 502 を既存契約どおり返す。 | 全 status と fake daemon の method 別回数。 |
| AC-23 | journal、例外、probe、status、activity、event に IP、Host、Authorization、username、password、secret canary が無い。daemon activity と event の件数も増えない。 | 各出力と保存先の個別 canary 検査。 |
| AC-24 | 既存の完全な設定ではウィザードが discovery も再質問も行わない。不足設定だけを順に聞く。明示 `--non-interactive` は TTY より優先し、不足時は何も書かず rc 2 `setup_input_required` になる。 | IO / subprocess fake の質問列、TTY 直積、writer 呼出し回数。 |
| AC-25 | `ip -j addr show up` を shell なしの引数配列、timeout、出力上限付きで呼ぶ。scope global の非 loopback IPv4 を `(ifindex, address)` で安定 sort し、1 候補を既定、複数候補を明示選択、候補なしを手入力にする。ip 不在 / timeout / 非 0 / 上限超過 / 壊れた JSON / 必要 field 欠落は固定警告後に手入力へ進む。 | subprocess spy、候補数、失敗種別ごとの JSON / process fixture、IO transcript。 |
| AC-26 | unrestricted の選択肢と警告が §5.2 の固定文であり、明示確認の後だけ `allow = ["0.0.0.0/0"]` を書く。未確認、EOF、取消しでは書かず、EOF / 取消しは rc 1 `setup_aborted` になる。限定 allow の空回答は再質問する。 | IO transcript と ops.toml。 |
| AC-27 | 生成 password が random 16 byte の padding なし base64url であり、初回だけ表示され、再実行、probe、status、stage、ログに現れない。 | deterministic RNG fixture、22 byte grammar、出力 canary。 |
| AC-28 | config と secret の atomic write が crash 前後に旧版又は新版の完全な file だけを残す。`web.password` は常に mode `0600` である。 | rename / fsync fault injection と mode。 |
| AC-29 | `feature_config.py` の writer が port、bind、allow、auth を書き、再読取りで scalar 型、list 順序、値を保持する。単一 IPv4 allow の `/32` 意味も保持する。 | write → collect → write → read の round-trip。 |
| AC-30 | `afx ops status web` が固定形式の公開範囲 1 行を出し、限定 allow の個別 IP と password を出さない。 | localhost、restricted、unrestricted、basic の出力全文。 |
| AC-31 | T1 単独版は localhost 又は非空の限定 allow 付き LAN だけを起動する。`0.0.0.0/0` と `auth = "basic"` は優先順位どおり `unrestricted_not_supported` / `auth_not_supported` で拒否し、production と probe が同じ固定 reason / rc 71 を返す。 | T1 artifact の production 起動と probe の直積。 |
| AC-32 | `0.0.0.0` bind、IPv6 bind / allow、proxy header による peer 上書き、新しい変更 route が存在しない。 | schema、route 一覧、`_Slot` の peer 判定入力の pin。 |
| AC-33 | `FeaturePlan` の許可 secret 名は常に `{"web.password"}` で、必須集合は検証済み auth だけから決まる。none の dir 不在を許可し、存在時は検査済み dir fd から残骸と未知 entry を判定する。 | plan object、openat spy、auth / dir / entry 直積。 |
| AC-34 | 手入力 password は 16〜128 byte の visible ASCII (`:` を含む) だけを受理し、space、改行、非 ASCII と境界外を `secret_invalid` にする。Basic は `b"afx:" + password` 全体を hash し、password 中の `:` を正しく認証する。 | 15 / 16 / 128 / 129 byte、文字 class、複数 colon、digest 入力 spy。 |
| AC-35 | readiness 1 回の accept は 64 以下で残りを次 tick に譲る。allow 外 flood 中でも同時に ready の deadline callback は次 tick まで、前方に `Q` 件ある 1 本の allow 内要求は最大 `ceil((Q + 1) / 64)` readiness callback で進む。peer reject / lookup failure は専用 counter だけを増やし、callback / EMFILE 連続回数を増やさない。journal 出力量は集約又は sampling 上限内である。 | 実 listener flood、fake loop の tick / callback 順、counter、listener 生存、journal 行数。 |
| AC-36 | 隔離の fd 自己検査時点の保持 fd は `{0,1,2,listener,notify}` だけで、secret dir / password / config / discovery pipe fd が無い。READY 後は notify も閉じる。 | `/proc/self/fd` と syscall trace の phase 別 snapshot。 |
| AC-37 | enabled の `basic → none` は停止、password unlink、dir fsync、config rename の順で、各 crash point は旧 enabled、`secret_missing` で停止、又は新 enabled のいずれかになる。material 変更前だけ rollback し、以後は marker から forward recovery する。 | 各 syscall 前後の fault injection、stage / unit / reason / fsync 順。 |
| AC-38 | enabled の `none → basic` は停止、secret atomic 保存、dir fsync、config rename の順で、各 crash point は旧 enabled、`secret_unexpected` で停止、又は新 enabled のいずれかになる。unsafe な手置き secret は `secret_invalid` で無変更とし、material 変更前だけ rollback、以後は forward recovery する。 | 各 syscall 前後の fault injection、canary、mode、stage / unit / recovery 文。 |
| AC-39 | request line / header / body の malformed と上限境界が既存契約の 400 / 413 になり、Host / auth より先に止まる。GET / HEAD の 400 / 401 / 404 / 405 / 429 / 500 / 502 は既存の HEAD body / Content-Length 契約を守る。 | 各 parser 上限の L-1 / L / L+1、全 status の GET / HEAD 応答、daemon 回数。 |
| AC-40 | §0〜§7 の各安全条件を 1 個だけ反転する mutation を用意し、各 mutation が AC-1〜AC-39 の少なくとも 1 個を必ず落とす。T1 の bind / allow / Host / probe / listener / seccomp / peer 不変値 / capability reason は各単独 mutation を含む。 | mutation 一覧、対応 AC、意図した failure の CI 記録。 |

## 10. 変更ファイルと task 表

### 10.1 変更ファイル (予定)

| 領域 | ファイル | 変更 |
|---|---|---|
| 設定 | `afx-ops/afx_ops/config.py` | bind、allow、auth の grammar、組み合わせ、固定 reason を追加する。 |
| pre-bind / allow | `afx-ops/afx_ops/isolation/accept_loop.py` | 設定 address の bind と listener 検査、accept 直後の peer allow を追加する。 |
| secret / 起動 | `afx-ops/afx_ops/isolation/startup.py` | `web.password` の条件付き検査と読取り、設定 bind の pre-bind を追加する。 |
| seccomp | `afx-ops/afx_ops/isolation/profiles.py` | 緩和せず、現行の AF_INET / AF_INET6 拒否と accept4 だけを pin する。 |
| HTTP | `afx-ops/afx_ops/web/{runtime,request,headers}.py` | allow 配線、Host、Basic、rate limit、401 / 429 / HEAD、固定 counter / journal を追加する。 |
| probe | `afx-ops/afx_ops/probe.py` | interface、allow、auth / secret、実 bind / listen / getsockname を検査する。 |
| CLI | `src/agentic_fx/cli.py` | `afx ops enable` に `--non-interactive`、`--reconfigure` を追加する。 |
| wizard / writer | `src/agentic_fx/ops_setup/{interactive,feature_config}.py` | 質問、候補列挙、atomic secret、durable reconfigure、round-trip、無再質問を追加する。 |
| status | `src/agentic_fx/ops_setup/stages.py` | 公開範囲の固定 1 行を追加する。 |
| test | `afx-ops/tests/{isolation,web}/`、`afx-ops/tests/test_{config,probe}.py`、`tests/ops_setup/` | §9 の T1 / T2 観測点、境界値、実 process を追加する。 |
| 運用文書 | afx-ops の運用文書 | 平文 HTTP、到達範囲、password の reconfigure 手順、残余リスクを記載する。 |

### 10.2 task 表

| task | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|
| T1 bind + allow | config と writer、設定 address の pre-bind / getsockname、accept 直後の allow、全 bind の Host 検査、probe、seccomp pin を実装する。単独公開は localhost 又は非空の限定 allow 付き LAN に限る。`0.0.0.0/0` と `auth = "basic"` はそれぞれ `unrestricted_not_supported` / `auth_not_supported` で拒否する。 | AC-1〜AC-11、AC-29、AC-31、AC-32、AC-35、AC-36、AC-39、AC-40。allow 外の受信 0、fairness、protocol 0、daemon 0、隔離前 connection の未処理、実 bind、実 seccomp、単独 mutation を含む。 | M |
| T2 auth + 運用導線 | Basic、条件付き secret、固定長 digest、rate limit、ウィザード、durable reconfigure、無制限 LAN の解禁、status、運用文書を実装する。 | AC-12〜AC-40。auth / secret の直積、Authorization の全失敗形、401 / 429 / HEAD、rate limit の queue / 時刻 / 状態数境界、discovery、crash recovery、canary、無再質問、password 一度表示を含む。 | M |

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

## 12. 残余リスク

| 点 | 利用者責任として残ること |
|---|---|
| 平文 HTTP / 能動的 LAN 中間者 | 同じ経路を観測又は改変できる者は Basic credential と表示内容を盗聴・改変できる。ARP spoofing 等の能動的中間者も防げず、Basic は暗号化でも server 認証でもない。 |
| IP allowlist | IP spoofing、ARP spoofing、DHCP の払い出し変更、NAT により、端末の人間又は機器 identity を保証できない。 |
| 到達範囲 | allow 内の侵害済み端末は到達できる。unrestricted では、LAN、VPN、port-forward 等を含め、このアドレスへ到達できる全 IPv4 source が戦略の diff を読めます。host firewall、router、container、cloud network の実効範囲を製品は検証しない。 |
| allow 外洪水 | 64 accept / tick と早期 close で公平性と資源消費を有界化するが、回線、kernel backlog、CPU を飽和させる DoS は防げない。 |
| slowloris | 接続枠 16 と 30 秒 timeout は被害を制限するが、allow 内 source が枠を占有して一時的に正規要求を妨げられる。 |
| global rate limit | 分散又は共有 source の失敗により global 100 件窓へ達すると、正しい利用者も最大 60 秒 lockout される。 |
| overflow bucket | 1024 entry 超の source は 1 送信元相当の bucket を共有するため、ある source の失敗が別 source を 429 にする巻き添えがある。 |
| 分散総当たり | 送信元別 / global の固定窓は試行速度を制限するが、時間を空けた攻撃、複数 process / host、credential の別経路漏えいを防がない。 |
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

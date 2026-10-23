# 同梱 web の公開範囲設計

**版:** v0.1 (C0 下書き)
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
| `bind` | string、既定 `127.0.0.1` | 十進 dotted-quad の数値 IPv4 だけを許す。hostname、IPv6、IPv4-mapped IPv6、`0.0.0.0`、余分な空白、先頭ゼロを含む表記を拒否する。非 loopback は本機の up 状態の interface に実在する具体的なアドレスだけを許す。 |
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

`auth = "basic"` のときだけ、feature の secret dir に `web.password` を要求する。
secret dir は owner 所有の mode `0700` とする。
`web.password` は owner 所有の通常 file とし、mode `0600` ちょうどを要求する。
symlink、他 uid の file、mode 違反、読み取り上限超過を拒否する。
secret は隔離前に検査済み dir fd から `O_NOFOLLOW` で一度だけ読む。
secret dir は隔離後の Landlock 規則へ加えない。

`auth = "none"` で `web.password` が存在する場合は、固定 reason `secret_unexpected` で起動と probe を拒否する。
残骸 file を黙って無視すると、利用者が認証済みだと思ったまま無認証で公開する事故が起こるためである。
secret の存在だけで `auth` を `basic` に変えることもしない。

password の byte grammar は §8.2 の確認事項である。
生成 password は少なくとも 128 bit の乱数を持つ ASCII 表現とする。

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

allow と auth は loopback でも任意に強化できる。
非 loopback では allow と auth のどちらか一方だけを必須にするのではなく、allow を常に必須にする。
auth は allow の代わりではない。

### 2.4 固定 reason

外部へ返す reason は次の集合に固定する。
設定値、IP、CIDR、path、header、secret、例外文は reason の後ろへ付けない。

| reason | 条件 |
|---|---|
| `config_invalid` | TOML、型、未知 key、`port`、`bind`、`allow`、`auth` の grammar が不正である。 |
| `bind_not_local` | 数値 IPv4 としては正しい非 loopback bind が、本機の up 状態の interface に存在しない。 |
| `allow_required` | 非 loopback で `allow` key が無い。 |
| `secret_missing` | `auth = "basic"` だが `web.password` が無い。 |
| `secret_unexpected` | `auth = "none"` だが `web.password` が存在する。 |
| `secrets_failed` | secret dir 又は file の所有者、mode、型、symlink、size、read の検査に失敗した。 |
| `listener_invalid` | production 起動の bind / listen 又は listener 自己検査に失敗した。 |
| `probe_bind_failed` | probe の実 bind / listen 又は `getsockname()` 一致に失敗した。 |
| `unrestricted_not_supported` | T1 単独版で `allow = ["0.0.0.0/0"]` を使おうとした。 |
| `auth_not_supported` | T1 単独版で `auth = "basic"` を使おうとした。 |

T1 専用の末尾 2 reason は T2 で到達不能になる。
既存の `config_dir_missing`、`config_dir_invalid`、`startup_internal_error` は従来の意味を維持する。

## 3. 接続の処理順

### 3.1 allow

現在の `AcceptLoop.accept_ready()` は `accept()` が返した peer address を捨て、直ちに callback を呼ぶ (`afx-ops/afx_ops/isolation/accept_loop.py:100`)。
変更後は accept 直後に accepted socket の `getpeername()` を呼ぶ。
返った peer が 2 要素の IPv4 tuple であることを確認する。
peer IPv4 を正規化済み allow network と照合する。

照合に外れた接続は応答を送らずに close する。
`getpeername()` の失敗又は想定外の形も応答を送らずに close する。
この経路では要求 byte を読まない。
この経路では `_Slot`、aiohttp protocol、transport、worker future を生成しない。
この経路では daemon 呼出しを 0 回にする。

allow を通った接続だけを既存の接続枠上限へ渡す。
現在の枠上限は 16 接続である (`afx-ops/afx_ops/web/runtime.py:33`)。
allow 外の接続はこの 16 枠を消費しない。

### 3.2 parse と Host

allow 通過後は既存の request line 2048 byte、header 8192 byte、body 65536 byte、header 64 件、接続 30 秒の上限で parse する。
GET と HEAD だけを route へ進める。
body、Transfer-Encoding、不正な Content-Length、既存の拒否対象は従来どおり拒否する。

非 loopback bind では `Host` header をちょうど 1 個要求する。
値は ASCII の `<bind>:<port>` と byte-for-byte で完全一致させる。
空白の正規化、hostname、port 省略、別表記、複数 Host を許さない。
欠落、重複、値違いは 400 とし、daemon を呼ばない。

loopback bind の Host は現行契約を維持する。
現行契約は Host が 1 個以上存在することだけを要求し、値と重複を拒否しない (`afx-ops/afx_ops/web/request.py:103`)。
この例外を統一するかは §8.2 の確認事項である。

### 3.3 Basic 認証

Host を通過した後、route 解決と daemon 呼出しより前に認証する。
`auth = "none"` では Authorization を認証には使わない。
`auth = "basic"` では Authorization をちょうど 1 個要求する。
scheme は ASCII case-insensitive の `Basic` とする。
token は strict Base64 で decode する。
decode 後の credential は固定ユーザー名 `afx` と password の組でなければならない。

rate limit 中でない各試行は、正しい credential と入力 credential をそれぞれ SHA-256 の 32 byte digest にする。
欠落、重複、scheme 違い、壊れた Base64、区切り無しも固定の dummy 入力から 32 byte digest を作る。
全試行は `hmac.compare_digest()` を 32 byte 対 32 byte でちょうど 1 回行う。
password 又は credential の可変長 bytes を直接 `compare_digest()` に渡さない。

欠落、重複、scheme 違い、壊れた Base64、username 違い、password 違いは同じ認証失敗として扱う。
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

429 は整数秒の `Retry-After` を持つ。
値は該当する送信元窓又は全体窓が次に 1 件空くまでの秒数を切り上げた値とし、最小 1、最大 60 とする。
送信元と全体の両方が超過していれば、大きい方を使う。

送信元表は実 IPv4 ごとに最大 1024 entry とする。
表が満杯の間に現れた未登録の送信元は、全て 1 個の overflow bucket を共有する。
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

## 5. probe と enable / ウィザード

### 5.1 probe

`afx-ops probe web` は state dir と stage file を作らない。
stdout は成功時も失敗時も空とする。
stderr は失敗時に `afx-ops: <固定 reason>` の 1 行だけとする。
secret、設定値、IP、CIDR、path、例外文を出さない。

probe は次を順に検査する。

1. `bind` が canonical な数値 IPv4 であり、`0.0.0.0` と IPv6 でないことを検査する。
2. 非 loopback bind が本機の up 状態の interface に実在することを検査する。
3. `allow` の型、非空、IPv4 / CIDR、canonical 表記、非 loopback での必須条件を検査する。
4. `auth` の列挙値を検査する。
5. `auth = "basic"` なら `web.password` の存在、owner、通常 file、mode `0600`、size を検査する。
6. `auth = "none"` なら `web.password` が存在しないことを検査する。
7. 指定 address と port へ実際に bind + listen する。
8. `getsockname()` が指定 address と port に一致することを検査して close する。

probe は listener を accept しない。
probe は password の正否をネットワーク経由で試さない。
実 bind / listen 又は `getsockname()` 不一致は `probe_bind_failed` とする。

### 5.2 対話ウィザード

`afx ops enable web` は不足している設定だけを次の順で尋ねる。

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
候補が無い場合だけ、canonical な数値 IPv4 を手入力させる。
手入力も probe で本機 interface の実在を確認する。

「誰が見られるか」の選択肢は次の 4 個に固定する。

| 選択肢 | 既定 | 書く設定 |
|---|---|---|
| 許可 IP だけ | LAN での既定 | `allow` に非空の限定 list、`auth = "none"` |
| パスワード |  | `allow = ["0.0.0.0/0"]`、`auth = "basic"` |
| 両方 |  | `allow` に非空の限定 list、`auth = "basic"` |
| LAN 内の全機器 |  | `allow = ["0.0.0.0/0"]`、`auth = "none"` |

「許可 IP だけ」と「両方」は非空の allow 入力まで完了条件にする。
空回答を unrestricted と解釈しない。

「LAN 内の全機器」では、確認前に固定の 1 行 `LAN 内の全機器が戦略の diff を読めます。運用は利用者の責任です` を表示する。
この 1 行の後で明示確認を要求する。
確認しなければ設定を書かない。

生成 password は CSPRNG から 128 bit 以上の entropy を得る。
端末へ表示するのは生成直後の 1 回だけとする。
ログ、stage、ウィザードの記録、再実行時の表示へ残さない。
利用者が自分で password を決める選択肢も用意する。
`web.password` は同じ directory 内の一時 file へ mode `0600` で書き、fsync、atomic rename、directory fsync の順で保存する。

既存設定が grammar と組み合わせの検査を満たし、probe が成功するなら再質問しない。
既存 secret の内容を表示しない。
probe が失敗した場合だけ、既存の再質問契約へ入る。

非対話では port-only の旧設定を安全な localhost として扱える。
非対話で LAN を選ぶ場合は `bind`、非空の `allow`、`auth` を全て明示する。
不足があれば `setup_input_required` で fail closed とする。
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
| §3.1.1 `ops.toml` | web の `port` に `bind`、`allow`、`auth` を追加する。 | §2 |
| §3.3 web | loopback 固定・認証なしを、検証済み bind、allow、Host、任意 Basic 認証へ置き換える。 | §1〜§4 |
| §5 自己隔離 | loopback pre-bind を設定 address の pre-bind に置き換える。隔離後の新規 INET socket 拒否は維持する。 | §4 |
| §6.1 enable | staged の web 設定・secret・probe 検査と status の公開範囲表示を追加する。 | §5 |
| §7 初回ウィザード | web の最小質問と loopback probe を、4 段の公開範囲質問と新 probe へ置き換える。 | §5 |
| §8 O-11 | 「認証済み loopback read-only」を「設定済み公開範囲の allow / Host / auth を通った read-only」に置き換える。 | §3、§7 |
| §9 AC-10 | `127.0.0.1` 固定・認証なしの観測を、本 spec の AC-1〜AC-26 へ置き換える。 | §9 |
| §11 残余リスク | web の port 衝突だけでなく LAN 公開の残余を追加する。 | §12 |
| §12 裁定 1 | 2026-10-09 の「初版は 127.0.0.1・認証なし」を、2026-10-23 のユーザー裁定で置き換える。 | §0、§8 |

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

### 8.2 確認事項

次の点は C0 で判断に迷った事項である。
指揮者とユーザーの裁定前に実装へ固定しない。

1. loopback の Host を現行どおり「1 個以上あれば値と重複を問わない」とするか、LAN と同じく「ちょうど 1 個の `127.x.x.x:<port>`」へ強化するか。
2. 利用者指定 password の byte grammar、最小長、最大長、許可文字をどこまで固定するか。
3. 手入力 bind に link-local 又は scope が global でない up interface の IPv4 を許すか。

### 8.3 問いにしない確認事項

- `auth = "none"` で `web.password` が残っていれば無視せず `secret_unexpected` で拒否する。
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
| AC-1 | `bind` の省略、`127.0.0.1`、本機の具体的な非 loopback IPv4を受理する。hostname、先頭ゼロ、空白付き、`0.0.0.0`、IPv6、IPv4-mapped IPv6、interface に無い IPv4 を拒否する。 | config / probe / production の rc と固定 reason。 |
| AC-2 | 指定した非 loopback IPv4 と port に実 bind + listen し、`getsockname()` が両方に完全一致する。別 address 又は別 port の listener を `listener_invalid` にする。 | 実 listener の `getsockname()` と起動 rc。 |
| AC-3 | allow の単一 IPv4、canonical CIDR、`0.0.0.0/0` を受理する。非 canonical CIDR、空 list、重複、IPv6、IPv4-mapped IPv6、空白付き、host bit 付き CIDR を拒否する。 | config reader と writer の再読取り。 |
| AC-4 | 非 loopback で allow key が無いと `allow_required` になる。loopback では allow 省略、限定 allow、`0.0.0.0/0` がそれぞれ契約どおり動く。 | 起動 rc と固定 reason。 |
| AC-5 | allow 外の接続と `getpeername()` 失敗は要求受信 0 byte、接続枠 0、protocol 生成 0、daemon 呼出し 0 で黙って close する。 | instrument した reader、slot、protocol factory、fake daemon の回数。 |
| AC-6 | allow 内の接続だけが接続枠と protocol へ到達する。allow 外の接続を 16 本以上張っても許可接続の 16 枠を消費しない。 | 実 accepted fd と slot 数の L-1 / L / L+1。 |
| AC-7 | 非 loopback の Host 欠落、重複、`<bind>:<port>` との値違い、hostname、port 省略を 400 にし、正しい 1 個だけを通す。 | HTTP 応答と daemon 呼出し 0 / 1。 |
| AC-8 | loopback の Host が §3.2 の確定契約どおり動く。 | 欠落、1 個、重複、値違いの fixture。 |
| AC-9 | 隔離前に接続して backlog に入れた connection が隔離前に handler、protocol、daemon へ届かず、隔離と READY 後にだけ処理される。 | phase ごとの callback 回数。 |
| AC-10 | LAN bind 後も隔離 process の新規 AF_INET / AF_INET6 socket、bind、listen が拒否される。trace 固定の `accept4` だけで実要求が通る。 | 実 seccomp process と syscall trace。 |
| AC-11 | probe が bind、interface、allow、auth、secret、実 bind / listen、`getsockname()` を検査する。成功と全失敗で state dir、stage file、listener 残骸を作らない。 | probe 前後の filesystem snapshot、rc、stdout / stderr。 |
| AC-12 | `auth = none/basic` と `web.password` 無し/安全/owner 違い/mode 違い/symlink/非通常 file の全組み合わせが §2.3 と固定 reason のとおりになる。 | config、probe、production の直積 fixture。 |
| AC-13 | `auth = "none"` と残骸 `web.password` を `secret_unexpected` で拒否し、secret の存在だけでは auth が有効にならない。 | probe と production の rc。 |
| AC-14 | username `afx` と正しい password だけが通る。別 username と正しい password、`afx` と誤 password は同じ失敗になる。 | 200 / 401 と daemon 呼出し 1 / 0。 |
| AC-15 | Authorization の欠落、重複、scheme 違い、壊れた Base64、区切り無し、username 違い、password 違いが同じ 401、同じ `WWW-Authenticate: Basic realm="afx"`、同じ固定 journal 文になる。 | status、header、body、counter、journal。 |
| AC-16 | rate limit 中でない認証試行が、成功、誤り、壊れた入力を含め常に 32 byte 対 32 byteの `compare_digest` を 1 回だけ呼ぶ。rate limit 中は 0 回になる。 | spy した引数長と呼出し回数。 |
| AC-17 | 401 の GET は固定 error 本文を持つ。HEAD は本文 0 byteで GET と同じ Content-Length を持つ。 | GET / HEAD の status、body、Content-Length。 |
| AC-18 | 同じ送信元の失敗 4 / 5 / 6 回目が 401 / 401 / 429 になる。60 秒境界で古い失敗が契約どおり外れる。窓内 429 は比較と daemon 呼出しが 0 回になる。 | fake clock、status、compare 回数、daemon 回数。 |
| AC-19 | 全体の失敗 99 / 100 / 101 回目が 401 / 401 / 429 になる。異なる送信元でも全体窓を共有する。 | fake clock と全体 counter。 |
| AC-20 | 429 が 1..60 の Retry-After を持つ。HEAD の本文は空で GET と同じ Content-Length を持つ。 | 窓境界の GET / HEAD 応答。 |
| AC-21 | 送信元表は 1024 entry を超えず、1025 個目以降は 1 個の overflow bucket を共有する。期限切れ entry は回収できる。 | 1023 / 1024 / 1025 送信元と状態 object 数。 |
| AC-22 | allow、Host、auth、rate limit の各失敗で daemon 呼出しが 0 回になる。成功した GET / HEAD だけが既存 read-only method を呼ぶ。 | fake daemon の method 別回数。 |
| AC-23 | journal、例外、probe、status、activity、event に IP、Host、Authorization、username、password、secret canary が無い。daemon activity と event の件数も増えない。 | 各出力と保存先の個別 canary 検査。 |
| AC-24 | 既存の完全な設定ではウィザードが再質問しない。不足設定だけを順に聞く。非対話の LAN 設定不足は `setup_input_required` になる。 | IO fake の質問列と writer 呼出し回数。 |
| AC-25 | `ip -j addr show up` を shell なしの引数配列、timeout、出力上限付きで呼ぶ。scope global の非 loopback IPv4 だけを候補にし、候補なしで手入力へ進む。 | subprocess spy と JSON fixture。 |
| AC-26 | 「LAN 内の全機器」は固定警告 1 行と明示確認の後だけ `allow = ["0.0.0.0/0"]` を書く。未確認又は EOF では書かない。 | IO transcript と ops.toml。 |
| AC-27 | 生成 password が 128 bit 以上の entropy を持ち、初回だけ表示され、再実行、probe、status、stage、ログに現れない。 | deterministic RNG fixture と出力 canary。 |
| AC-28 | config と secret の atomic write が crash 前後に旧版又は新版の完全な file だけを残す。`web.password` は常に mode `0600` である。 | rename / fsync fault injection と mode。 |
| AC-29 | `feature_config.py` の writer が port、bind、allow、auth を書き、再読取りで scalar 型、list 順序、値を保持する。単一 IPv4 allow の `/32` 意味も保持する。 | write → collect → write → read の round-trip。 |
| AC-30 | `afx ops status web` が固定形式の公開範囲 1 行を出し、限定 allow の個別 IP と password を出さない。 | localhost、restricted、unrestricted、basic の出力全文。 |
| AC-31 | T1 単独版は localhost 又は非空の限定 allow 付き LAN だけを起動する。`0.0.0.0/0` と `auth = "basic"` は固定 reason で拒否する。 | T1 artifact の production 起動。 |
| AC-32 | `0.0.0.0` bind、IPv6 bind / allow、proxy header による peer 上書き、新しい変更 route が存在しない。 | schema、route 一覧、peer 判定入力の pin。 |

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
| wizard / writer | `src/agentic_fx/ops_setup/{interactive,feature_config}.py` | 質問、候補列挙、atomic secret、round-trip、無再質問を追加する。 |
| status | `src/agentic_fx/ops_setup/stages.py` | 公開範囲の固定 1 行を追加する。 |
| test | `afx-ops/tests/{isolation,web}/`、`afx-ops/tests/test_{config,probe}.py`、`tests/ops_setup/` | §9 の T1 / T2 観測点、境界値、実 process を追加する。 |
| 運用文書 | afx-ops の運用文書 | 平文 HTTP、到達範囲、password 変更時の再起動、残余リスクを記載する。 |

### 10.2 task 表

| task | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|
| T1 bind + allow | config と writer、設定 address の pre-bind / getsockname、accept 直後の allow、非 loopback Host 完全一致、probe、seccomp pin を実装する。単独公開は localhost 又は非空の限定 allow 付き LAN に限る。`0.0.0.0/0` と `auth = "basic"` はそれぞれ `unrestricted_not_supported` / `auth_not_supported` で拒否する。 | AC-1〜AC-11、AC-29、AC-31、AC-32。allow 外の受信 0、protocol 0、daemon 0、隔離前 connection の未処理、実 bind、実 seccomp を含む。 | M |
| T2 auth + 運用導線 | Basic、条件付き secret、固定長 digest、rate limit、ウィザード、無制限 LAN の解禁、status、運用文書を実装する。 | AC-12〜AC-30、AC-32。auth / secret の直積、Authorization の全失敗形、401 / 429 / HEAD、rate limit の時刻・回数・状態数境界、canary、無再質問、password 一度表示を含む。 | M |

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
| 平文 HTTP | 同じ経路を観測できる者は Basic credential と表示内容を盗聴できる。Basic は暗号化ではない。 |
| IP allowlist | IP spoofing、ARP spoofing、DHCP の払い出し変更、NAT により、端末の人間又は機器 identity を保証できない。 |
| 侵害済み LAN 端末 | allow 内の侵害済み端末は到達できる。unrestricted では LAN から到達できる全端末が対象になる。 |
| firewall / VPN | host firewall、router、VPN、container 又は cloud network の設定により、利用者の想定外の場所から指定 interface へ到達し得る。製品はこれを検証しない。 |
| password 変更 | secret は隔離前に一度読むため、変更の反映には web process の再起動が必要である。 |
| Basic logout | browser が credential を保持するため、確実な logout 操作を提供できない。browser を閉じても保持が続く場合がある。 |
| bind address の変化 | DHCP 等で設定 address が失われると、probe 又は次回起動が `bind_not_local` / `probe_bind_failed` で止まる。自動で別 address へ広げない。 |
| read-only の限界 | 変更権限は無くても、損益、稼働状態、ログ、承認詳細、戦略 diff は機密情報である。 |

## 13. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-23 | v0.1 | C0 下書きを新規作成。bind、allow、auth、接続順、隔離、probe、ウィザード、AC、T1 / T2 を定義した。 | チケットのユーザー裁定と第二意見 2 回目の是正 3 点を amendment spec に固定するため。 | — |

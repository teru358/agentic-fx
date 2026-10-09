# [afx-ops] operations surface の分離と自己隔離 v1.0

版: v1.0

日付: 2026-10-09

対象チケット: ops-ui

対象: afx-ops 独立 package、操作 API、機能別 systemd user unit

## 要点

- `afx-ops/` は独立 package として配布し、本体とは pathname UDS の操作 API だけで結合する。本体 package を import しない。
- bot、web、notify は機能ごとに別 process、別 principal、別鍵で動かす。既定は全 off であり、全 off の instance には ops 用 unit を置かない。
- principal の追加と bot の利用者名申告には操作 API spec v1.13 が必要である。v1.13 が確定するまで本番鍵で ops process を起動しない。
- bot は Discord interaction だけで plugin の決定を扱う。Discord 利用者の認可、確認状態、daemon 側の digest 再照合の全てを通った場合だけ決定を送る。
- web は loopback の read-only 表示、notify は `events.read` による generic HTTPS webhook 配送だけとする。いずれも承認権限を持たない。

## 1. 目的と信頼境界

### 1.1 目的

同一マシンで動く afx に、人が plugin 承認を扱う Discord bot、損益・成績・承認判断材料を読む web、通知を外部へ配送する notify を加える。便利な外部機能へ本体鍵や全権限を共有せず、操作 API を唯一の権限境界とする。

操作 API の正本は daemon である。承認の状態、payload digest、監査、job の終端は daemon が決める。Discord message、web 画面、webhook の配送位置は投影であり、正本ではない。

### 1.2 境界

|主体|扱うもの|境界での扱い|
|---|---|---|
|afx daemon|操作 API、instance lock、承認・監査の正本|server。鍵を受け取る前に client を検査する|
|`ops-bot`|bot 鍵、Discord token、gate 状態|決定専用。web / notify / 本体の鍵を読めない|
|`ops-web`|web 鍵、UI 認証 secret、read-only 表示|変更 scope を持たず、他 principal の鍵を読めない|
|`ops-notify`|notify 鍵、送信先、配送位置|event 読取りと webhook 送信だけを行う|
|Discord 利用者|Discord user ID、interaction|allowlist 外を deny-by-default で拒否する。channel は認可根拠にしない|
|localhost の利用者|web UI|UI 認証を通した者だけ表示を読む|

同 uid の非隔離 shell、Landlock のない trade worker、又は本物 daemon 停止中に instance lock を取れる攻撃者は、既存操作 API の信頼境界の内側である。本設計はそこから鍵を守らない。隔離済み ops process 相互については、Landlock だけに頼らず、fd 継承、ptrace、memory copy、`/proc/<pid>/mem` による鍵横取りを防ぐ。

## 2. 前提となる操作 API の変更（ops-api spec への変更要求: v1.13 manifest v2 + enroll、X-Afx-Asserted-Actor）

実装前に操作 API spec を v1.13 として更新し、以下の二項目を確定する。afx-ops はその変更済みの公開契約だけに従う。v1.13 は本体 client の server 認証を強化又は変更しない。

1. **versioned `.ready` manifest と enroll** — v1 reader は enum ではなく legacy 集合 `{operator, approver}` を読む。v2 は known principal の登録済み subset を map として持ち、未知 principal は API 起動不能、absent known principal は未登録として扱う。未登録と鍵欠損・digest 不一致は別状態であり、未登録 ops principal を理由に API 全体を閉じない。`afx keys enroll <principal>` は停止中だけ、`preparing(op=enroll, principal=p)` を durable に記録して開始する。既存 principal の鍵 file、token digest、generation、mtime は一切変更せず、追加 principal だけを新鍵・generation 1・`active` として commit する。対象が既に `active` なら `enroll(active)` は完全な無変更成功とする。鍵 file と directory を fsync し、最後に manifest を atomic rename と directory fsync して commit する。v1 から v2 への書込みは最初に成功した鍵変更だけで行う。準備状態の残骸は同じ enroll command の再実行だけが完結でき、別 command は v1.11 §3.2 と同じ `invalid_state`・無変更とする。client と daemon は preparing、鍵欠損、digest 不一致を fail closed で読む。
2. **`asserted_actor` wire 契約** — header 名は大文字小文字を問わない `X-Afx-Asserted-Actor` とする。送信を許す principal は `ops-bot` だけで、値は ASCII の `discord:` に 17〜20 桁の十進 Discord user ID を続ける形式、最大 28 byte とする。空、space / tab、C0 制御文字、DEL、非 ASCII、CR / LF、又はこの形式外は拒否する。許可されない principal が header を送った場合も HTTP 400 `invalid_argument` とする。`ops-bot` による決定作成とその job の polling / cancel は header を必須とし、欠ければ HTTP 400 `invalid_argument` とする。他 principal の header なし要求は従来どおり actor を null とする。server は認可根拠を authenticated principal から変えず、`decided_by` も従来どおり principal から生成する。一方で有効な値は監査行と job 所有者 `jobs.own = (principal, asserted_actor)` に記録する。決定を作る要求とその job の polling / cancel は同じ header 値を送る。

## 3. 構成と機能

### 3.1 配布、process、scope

`afx-ops/` は自前の `pyproject`、lockfile、README、tests、UDS client、鍵読込み、server 認証、自己隔離、plain gate module、機能別起動口、systemd 資材を持つ。本体の module や鍵 object を import しない。

|機能|unit|principal|scope|許さないこと|
|---|---|---|---|---|
|bot|`afx-ops@bot.service`|`ops-bot`|`events.read`、`approvals.list`、`approvals.detail`、`jobs.own`、`decide`|`operate`、`local_guard`、web / notify の鍵|
|web|`afx-ops@web.service`|`ops-web`|`status.read`、`approvals.list`、`approvals.detail`、`logs.read`|全変更、`decide`、`local_guard`、他の鍵|
|notify|`afx-ops@notify.service`|`ops-notify`|`events.read`|承認詳細、全変更、他の鍵|

各機能の設定、secret、静的資材、状態領域を分離する。一方の領域を他方の Landlock 許可へ入れない。共通 gate module は Discord SDK と cog lifecycle から独立し、表示、対応表、確認状態、interaction 認可、reconcile を担当する。同梱 bot は `discord.Client` と View から直接使い、既存の汎用 bot は薄い cog shim で同じ module を使う。

### 3.2 bot

bot は pending 承認、親が記録した facts と agent claims を区別した判断材料、payload SHA-256、期限、決定状態を表示する。自由文を event から復元せず、API が許す承認詳細だけを出す。`message_content` intent は要求も使用もしない。

投稿対応は `approval_id`、`payload_sha256`、`guild_id`、`channel_id`、`message_id`、`display_generation` を永続化する。投稿成功から状態保存までで中断した場合は固定識別子を照合できた自分の投稿だけ adopt し、できない場合だけ repost する。daemon 側で決着、期限切れ、無効化、取得不能になれば、message を冪等に更新して View を外す。Discord / API の一時障害は対応を残し、reconcile で再試行する。

primary View は `timeout=None` とし、全 component の `custom_id` は `afx:v1:<action>:<approval_id>` の形に固定する。`action` は `approve`、`reject`、`retry` のいずれかとする。nonce、secret、actor、表示 generation は `custom_id` に埋め込まず、確認状態から照合する。確認 record は `actor`、`approval_id`、`payload_sha256`、`action`、`expiry`、`nonce`、`guild_id`、`channel_id`、`message_id`、`display_generation`、状態を持つ。expiry は interaction token の 15 分より短い 10 分とする。状態は `fresh → in_flight → done | expired | invalidated` とし、各遷移は一 transaction の条件付き更新で一回だけ起こす。

全 interaction の callback は、安価な guild / channel / user 確認の後、API アクセスより前に 3 秒以内に `interaction.response.defer(ephemeral=True)` を呼ぶ。defer 後、`custom_id` の action と確認 record の action が一致すること、同じ actor と表示起点を再照合し、`fresh` の nonce を `in_flight` へ原子的に claim してから daemon を呼ぶ。競合再送、別 user、別 guild / channel / message、旧 generation、期限切れ、使用済み nonce は record を invalidated とし決定を送らない。`custom_id` が形式不正、又は `approval_id` が未知で確認 record を一意に特定できない場合は、状態を変更せず拒否する。`approval_id` で確認 record を一意に特定できる action 不一致又は改変では、その record を原子的に `invalidated` とする。いずれの `custom_id` 拒否でも daemon に決定を送らない。決定前に allowlist を再照合し、承認詳細を再取得して digest が record と一致することを確認する。成功時だけ actor header を付けて決定を送り、作成された job の polling / cancel にも同じ header を付ける。応答喪失又は競合では成功表示をせず、正本を再読取して反映する。

bot 再起動時は gateway 接続前に既存 message の View を再登録して dispatch 経路を用意し、未終端の全確認 record を一 transaction で `invalidated` とする。次に既存 message を「再確認が必要」に bot token で edit し、固定識別子を照合できた投稿を adopt し、それ以外だけを repost する。reconcile が新しい確認状態と View を作った後に gateway 接続する。10 分を越えた expiry 又は interaction token の失効後も、表示更新に interaction token は使わず bot token による message edit だけを使う。

### 3.3 web

web は損益、成績、稼働状態、承認一覧・詳細だけを表示する。HTTP 要求から変更 API を作る route、フォーム、バックグラウンド処理は作らない。web principal にその scope が無いため、侵害されても bot の決定権と `local_guard` に届かない。

隔離前に `127.0.0.1` の socket を bind + listen し、`SO_TYPE`、`SO_ACCEPTCONN`、`getsockname()` の loopback address と想定 port、close-on-exec を検査する。この時点では accept を呼ばず、接続は backlog に溜めるだけとする。Landlock、seccomp、`PR_SET_DUMPABLE=0`、fd 閉鎖の自己検査が完了した後、trace で固定した `accept4` 又は `accept` による自前 accept loop で受け、accepted socket を `loop.connect_accepted_socket` で aiohttp の `web.Server` protocol へ渡す。`create_server` と `SockSite` は listener に `listen` を再実行するため使わない。隔離前に到着した接続も隔離後に初めて処理される。

UI 認証は初版から必須である。API 鍵とは分離した cookie session secret を `afx ops` が初回に生成して表示し、`ops-web` 領域にだけ保管する。127.0.0.1 限定の password entry から短命 session cookie を発行し、`HttpOnly`、`SameSite=Strict`、loopback HTTP では `Secure` を付けられないこと、Host / Origin 検査、rotation、失効を設定 schema で一意にする。bearer secret は初版候補に含めない。

### 3.4 notify

notify は event cursor から固定 schema の event を読み、event ID で重複を落としてから generic HTTPS webhook へ送る。成功した位置だけを機能固有の状態領域に保存するため、配送は at-least-once である。送信先には event code と許可された固定 fields だけを summary として送り、approval payload、agent claim、reject reason、鍵、自由文、内部例外を送らない。送信失敗は上限付き exponential retry と原因別警告抑制で扱う。

## 4. 鍵と principal

principal と scope の対応は versioned な認可表で管理する。未知 principal / scope、重複 scope、`local_guard` を `approver` 以外へ与える表は API を起動不能にする。ops process は自分の principal の鍵だけを読む。鍵 directory / file は期待 owner、0700 / 0600、通常 file、`O_NOFOLLOW` を検査し、鍵平文を env、argv、子 process、ログ、画面、通知、例外へ出さない。

enroll は §2 の v1.13 manifest 遷移にだけ従い、online enroll は行わない。revoke 済み principal は enroll で復活させない。rotate / revoke を含め、準備状態は同じ停止中 command の再実行でだけ完結し、手編集、旧鍵への rollback、自動補完をしない。

本体 client と afx-ops client は、socket 所有者、親 directory、`SO_PEERCRED`、instance lock の FLOCK WRITE 保持者、保持者の生存を確認してから鍵を送る共有 server 認証契約を独立実装する。共有契約では `SO_PEERPIDFD` は取得できる場合に pid 照合へ使い、取得不能でも拒否理由にしない。`/proc/locks` の dev 不一致は保持者の `/proc/<pid>/fd` readlink 照合へ fallback する。共通の fixture や一方の内部実装を正とせず、共有契約の spec 表から生成した JSON ケースを第三者 oracle として両 client の別 process に流す。fixture は隔離した HOME / root、実 UDS fake daemon、実 instance lock、server が受信した pre-auth byte 数、client の rc / code を固定成果物とし、oracle の許可 / 拒否判定と照合する。

## 5. 自己隔離と server 認証

### 5.1 Landlock と接続

Landlock は pathname UDS への `connect` を仲介せず、path 指定の接続は Landlock 下でも通る。`lstat` / `stat` の metadata 読取りも Landlock の対象外である。このため、隔離後も socket path に connect し、既存の server 認証をその接続ごとに行う。保持した UDS 親 directory fd や instance lock fd、`openat` / `connectat` による接続は用いない。socket activation も採用しない。本体は ops を spawn せず、UDS server を本体自身が作る契約だからである。

Landlock には自分の鍵・設定・状態、必要ランタイム、機能に必要な外向き通信だけを許す。server 認証の `/proc` 規則は `/proc/locks` の leaf file に対する `READ_FILE` と、起動時に `os.getpid()` で解決した自己 `/proc/<ops-pid>` directory に対する `READ_FILE` と `READ_DIR` だけとする。`/proc/self` symlink は使わない。後者は `SO_PEERPIDFD` の fdinfo を読み pid を照合するためだけに使う。`/proc` 全体、他 pid の directory、`/proc/<peer-pid>/fd` は許可しない。同 uid の別 process の `environ` 等を侵害 process に読ませないためである。

接続ごとに、afx-ops 固有 admission を共有 server 認証の前段として適用する。これは共有契約を置き換えず、より厳しくするだけである。

1. `SO_PEERPIDFD` を必ず取得し、自己 `/proc/<ops-pid>/fdinfo/<pidfd-fd>` で peer pid と一致させる。pidfd を取得又は照合できない kernel は固定 reason `ops_requires_so_peerpidfd` で起動を拒否する。
2. `os.stat(instance.lock)` の `(dev, ino)` と `/proc/locks` の FLOCK WRITE 保持者を照合する。dev が一致しない場合は共有契約の readlink fallback を実行せず、固定 reason `instance_lock_dev_mismatch` で fail closed とする。
3. 前段を通過した接続に、socket path の型・所有者と親 directory 0700、`SO_PEERCRED` の同 uid、FLOCK WRITE 保持者が peer pid 一つだけであること、保持者の生存という共有 server 認証の全検査を適用する。
4. pidfd は接続を所有する要求 object が取得から要求終了まで保持する。所有者は token 送信直前と要求中に `poll` し、`POLLIN` を peer exit として失敗にする。reply 完了又は接続を閉じる例外処理で、所有者だけが pidfd を一回 close する。`/proc/<peer-pid>` は読まない。

前段 admission 又は共有 server 認証のいずれかを確定できなければ token は一 byte も送らない。本体再起動で lock inode が変われば、新 path を stat して新しい server と再認証する。認証不能なら終了し、systemd の restart で新 process が隔離前からやり直す。

### 5.2 seccomp

隔離順は、(1) 鍵と設定を安全に開き、web は loopback listener を pre-bind / listen して検査する、(2) 不要な継承 fd を閉じる、(3) Landlock、(4) seccomp、(5) `PR_SET_DUMPABLE=0` と自己検査、(6) API 認証と業務処理、とする。web は (5) の自己検査成功後まで accept を呼ばない。任意段の失敗は token を送らず終了する。

全 profile は canonical syscall 番号表を持つ。architecture、番号上限、同一番号の重複、未分類、条件式を生成時に拒否し、最終 BPF の hash を検査する。`clone3` は ENOSYS とし glibc に legacy `clone` への fallback をさせる。afx-ops は固有条件 ID `afx-ops-clone-thread-only-v1` と、それを実装する独立 BPF 条件を持つ。`clone` は `CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID` の thread flags と完全一致するときだけ許可し、それ以外は EPERM とする。`CLONE_NEW*` 又は `CLONE_PARENT` を含む `clone`、`unshare`、`setns` は EPERM とする。共通拒否集合は `ptrace`、`process_vm_readv`、`process_vm_writev`、`pidfd_getfd`、`kcmp`、`process_madvise`、`perf_event_open`、`bpf`、`io_uring_setup` / `io_uring_enter` / `io_uring_register`、keyring、namespace / mount 操作、`bind`、`listen` とする。`PR_SET_PTRACER` は拒否し、`PR_SET_DUMPABLE` は setup 中の 0 設定だけを許す。

|feature|socket / DNS と接続の許可 syscall|event loop・thread と listener|隔離後に明示的に拒否する通信 syscall|
|---|---|---|---|
|bot|UDS 用 `socket(AF_UNIX, SOCK_STREAM)`、`connect`。DNS 用 `socket(AF_INET/AF_INET6, SOCK_DGRAM)`、`sendto`、`recvfrom`、`connect`、NSS 用 `socket(AF_UNIX, SOCK_STREAM)` と `connect`、Discord 向け TCP `socket(AF_INET/AF_INET6, SOCK_STREAM)` と `connect`|`clone3` は ENOSYS。legacy `clone` は `afx-ops-clone-thread-only-v1` の完全一致だけ許可し、`futex` を許可。listener は持たない|`bind`、`listen`、accept 系、条件外 `clone`、`unshare`、`setns`|
|web|UDS 用 `socket(AF_UNIX, SOCK_STREAM)` と `connect`。pre-bind 済み fd の trace で固定した `accept4` 又は `accept`、accepted fd の `read` / `write`。外向き DNS・TCP は不要|自前 accept loop に必要な trace 固定の accept syscall、既存 fd の `read` / `write`。`clone3` は ENOSYS、legacy `clone` は `afx-ops-clone-thread-only-v1` の完全一致だけ許可。新規 network socket は許可しない|`socket(AF_INET, *)`、`socket(AF_INET6, *)`、`bind`、`listen`、外向き `connect`、`sendto`、`recvfrom`、trace 非選択の accept syscall、条件外 `clone`、`unshare`、`setns`|
|notify|UDS 用 `socket(AF_UNIX, SOCK_STREAM)`、`connect`。DNS 用 `socket(AF_INET/AF_INET6, SOCK_DGRAM)`、`sendto`、`recvfrom`、`connect`、NSS 用 `socket(AF_UNIX, SOCK_STREAM)` と `connect`、webhook 向け TCP `socket(AF_INET/AF_INET6, SOCK_STREAM)` と `connect`|`clone3` は ENOSYS。legacy `clone` は `afx-ops-clone-thread-only-v1` の完全一致だけ許可し、`futex` を許可。listener は持たない|`bind`、`listen`、accept 系、条件外 `clone`、`unshare`、`setns`|

DNS は宛先 address と port を seccomp で判定しないため、bot / notify は任意宛先・任意 port への UDP 送信も可能である。feature ごとの `prctl`、`fcntl`、`ioctl`、process 操作、上記以外の socket family / type と errno も番号表に明記する。hostile canary は隔離済み実 process で `0.0.0.0` / `::` を含む新規 bind、全 listen、ptrace、memory copy、pidfd fd 窃取、他 principal の鍵読取、非許可 `/proc` 読取り、web の新規 AF_INET/AF_INET6 socket が拒否されることを確認する。web の実 accept loop、bot の Discord gateway reconnect と DNS cache miss、notify の webhook hostname 再解決、AF_UNIX NSS、UDP DNS が必要な syscall だけで通ることも確認する。

## 6. 起動、enable、systemd

### 6.1 enable 状態機械

機能ごとに durable stage file を持ち、`stage`、feature、principal の登録済み確認、設置済み artifact、`Wants=` 設置、ops の active 確認、commit を記録する。stage は `staged → stopped → enrolled → installed → started → checked → enabled` のみとし、各外部操作の前後と stage 更新の前後に crash しても、stage file と実物を照合して同じ段を冪等に再実行する。同じ段の再実行は no-op、既に満たした成果物を変更しない。未終端 enable がある間、別機能の enable / disable は `invalid_state`・無変更とする。online enroll は行わない。

|stage|処理|失敗時の durable 遷移と再開先|
|---|---|---|
|`staged`|設定、機能別 secret、UI secret、token 形式、unit 資材を検証する|外部変更なしで `staged`。修正後に同段を再実行する|
|`stopped`|afx を停止し、instance lock を取得できることを確認する|停止できなければ `staged`。既に停止済みなら同段は no-op で次段へ進む|
|`enrolled`|未登録 principal だけを停止中に enroll する|`preparing(op=enroll)` は同じ command の再実行で完結する。active principal への enroll は無変更成功。enroll commit 後の crash も `enrolled` として再開する|
|`installed`|unit、設定、`Wants=` を設置する。`Wants=` による先行 pull-in 済みの ops を許容する|installing 失敗では対象 unit と未完了 artifact を除去して `enrolled` へ戻す。鍵は残す。次回は `enrolled` から設置をやり直して `installed` へ進む|
|`started`|afx を起動し API を開き、対象 ops が既に pull-in されていても idempotent に ensure-active する|起動不能は `installed` に留める。unit と鍵は残し、同じ enable は `started` の到達を再試行する|
|`checked`|実 UDS の server 認証と機能別疎通を確認し、ops の ready 待ち成功を観測する|checking 失敗では `installed` に留める。ops unit は `Restart=on-failure` で再試行し、`afx ops status` は固定 reason と直近失敗理由を示す。同じ enable が `started` の到達を再試行する|
|`enabled`|有効化済みとして commit する|以後通常運転。再実行は no-op|

`started` の ops は `Wants=` により先に起動され得るため、初回起動を仮定しない。ops は UDS と server 認証の有界 ready 待ち後にだけ業務開始し、timeout 又は認証不能なら fail closed で終了し systemd restart に委ねる。enable の `checked` はこの成功を観測する。`stopped` 以後の未終端 enable では afx を停止状態のまま保持し、同じ enable の再実行だけが次段又は失敗段から再開する。未終端 enable がある間、通常 start と別機能操作は `invalid_state`・無変更とする。

### 6.1.1 disable 状態機械

disable は feature ごとの durable stage file に `operation=disable` を記録して開始し、`enabled → stopping-ops → uninstalled → disabled` だけを進む。外部操作の直前・直後と stage 更新の直前・直後の crash は、stage file と unit、`Wants=`、ops 用 unit 雛形の実物を照合して同じ段を冪等に再実行する。未終端 disable がある間、同じ feature の enable、別機能の enable / disable は `invalid_state`・無変更とする。disable は鍵を revoke せず、`afx keys revoke <principal>` は停止中に行う別操作である。

|stage|処理|失敗時の durable 遷移と再開先|
|---|---|---|
|`enabled`|対象 `afx-ops@<機能>.service` を stop する|stop 又は停止確認に失敗すれば `enabled` に留め、同じ disable が stop を再試行する。stop 成功後は `stopping-ops` へ進む|
|`stopping-ops`|対象 unit を削除し、`afx.service` の対象 `Wants=` 行を削除し、daemon-reload を完了する。最後の有効機能なら ops 用 unit 雛形も削除する|削除、`Wants=` 更新、daemon-reload、又は最後の雛形削除のいずれかに失敗すれば `stopping-ops` に留める。実物との差分を再照合して同じ uninstall 操作を冪等に再実行する|
|`uninstalled`|対象 unit、対象 `Wants=` 行、対象設定 artifact が無く、daemon-reload 済みであることを確認する。最後の有効機能なら ops 用 unit 雛形も無いことを確認する|確認失敗は `stopping-ops` へ戻し、uninstall を再実行する。確認成功後だけ `disabled` へ進む|
|`disabled`|無効化済みとして commit する。principal 鍵は保持する|再実行は no-op。再有効化は独立した enable として `staged` から開始する|

初回ウィザードでは afx が未稼働なので `stopped` が停止操作を要しないだけで、同じ状態機械を通る。

### 6.2 unit 契約

各有効機能の unit は `BindsTo=afx.service`、`PartOf=afx.service`、`After=afx.service`、`Restart=on-failure`、`RestartSec=5s`、`StartLimitIntervalSec=60s`、`StartLimitBurst=3` を持つ。`afx.service` 側の該当 `Wants=afx-ops@<機能>.service` は維持する。ops の単独 start は afx を pull-in しない。afx の `Wants=` が ops を pull-in し、`After=` の後に ops 自身が readiness を待つ。ops crash は afx の取引・資金保護 tick を止めず、afx stop / explicit restart / crash では ops が停止し、afx 復帰時には `Wants=` により ops も再起動する。

ops 起動時は API が ready であると仮定しない。UDS が現れ、§5.1 の server 認証が成功するまで 250 ms 間隔、上限 30 秒だけ待つ。上限到達又は認証不能なら fail closed で非零終了し、鍵を送らない。unit property の実効値、`NoNewPrivileges=yes`、direct exec、`KillMode=control-group` も鍵を読む前に検査する。

## 7. 初回ウィザード

初回起動は対話ウィザードを必須とする。任意段で「外から見る / 操作する手段を今設定しますか」を一問だけ尋ね、既定は「あとで」とする。全 off 又は「あとで」では ops unit と link を置かない。機能を選ぶ場合は §6.1 を通し、完了前に次を確認する。

|機能|最小質問|疎通確認|
|---|---|---|
|bot|Discord 接続情報、単一 guild / channel、承認者 allowlist|allowlist user は interaction を受け、未登録 user は拒否され、同じ actor header で job を読める|
|web|password entry 用 UI secret|`127.0.0.1` だけで認証済み画面を取得でき、変更 route が無い|
|notify|generic HTTPS webhook と event 種類|安全な test event が届き、同じ event ID を受信側で識別できる|

## 8. 不変条件

|ID|不変条件|
|---|---|
|O-1|afx-ops は本体 package を import せず、UDS 操作 API だけで通信する|
|O-2|bot、web、notify は別 process、別 principal、別鍵、別書込み可能状態領域である|
|O-3|manifest の principal 追加は v1.13 の enroll 遷移だけで行い、既存 principal の鍵、token digest、generation、mtime を変えない。`enroll(active)` は無変更成功である|
|O-4|server 認証前又は認証不能時、token は一 byte も送らない|
|O-5|共有 server 認証は pathname UDS 接続後に socket、peer credential、`stat` と `/proc/locks` の lock holder、生存を検査し、pidfd は optional、dev 不一致は readlink fallback とする。afx-ops はその前段で必須 pidfd の fdinfo pid 照合と dev 一致を要求し、pidfd poll を行う|
|O-6|afx-ops の必須 pidfd は要求 object が要求終了まで保持し、token 送信直前と要求中に poll し、reply 完了又は例外時に所有者だけが一回 close する|
|O-7|各 process は Landlock、feature 別 seccomp、`PR_SET_DUMPABLE=0`、不要 fd close を満たしてから業務処理を始める|
|O-8|隔離後の全 feature は新規 `bind` と `listen` を実行できない。web は pre-bind listener を、隔離完了後の trace 固定の自前 accept loop だけで処理する|
|O-9|bot は allowlist を deny-by-default とし、3 秒以内の defer、10 分以内の確認、確認・決定・job polling を同じ actor、表示起点、nonce、action に束縛する|
|O-10|`asserted_actor` は `ops-bot` だけが wire 契約に従って送れ、認可・`decided_by` を変えない|
|O-11|web は認証済み loopback read-only、notify は `events.read` と固定 schema webhook だけである|
|O-12|enable と disable は operation ごとの durable stage file により中断再開可能で、段の再実行は冪等、enroll 済みの鍵を勝手に rollback 又は revoke しない|
|O-13|全 off の instance に ops 用 unit は無く、有効機能だけが `Wants=`、`BindsTo=`、`PartOf=`、`After=` で afx と連動する|

## 9. 受入条件

次の条件は mock だけで済ませず、実 process、実 UDS、実 fd、実 Landlock / seccomp、systemd user manager で観測する。

|ID|観測すること|
|---|---|
|AC-1|旧 schema manifest を v1.13 reader が legacy 集合として読み、v2 の absent known principal は未登録、未知 principal は起動拒否、鍵欠損・digest 不一致は別の fail closed となる。enroll 後は追加 principal だけ generation 1 / active となり、既存 principal の token digest、generation、mtime は不変である|
|AC-2|enroll の preparing を file fsync、manifest 更新前後で crash させ、同じ enroll だけが回復し、別鍵 command は `invalid_state`・無変更となる。`enroll(active)` と v1→v2 の無変更再実行を確認する|
|AC-3|実機能 enable の各外部操作の直前・直後、stage 更新の直前・直後で crash させ、stage file と成果物が `staged → stopped → enrolled → installed → started → checked → enabled` の唯一の再開規則を保つことを観測する。installing 失敗は unit 除去後 `enrolled`、checking 失敗は `installed`、enroll 済み失敗は鍵非 rollback である。`stopped` 以後の未終端 enable は afx を停止したまま同じ enable だけが再開できる|
|AC-4|Landlock 下の実 process が pathname UDS へ接続でき、`lstat` / `stat` は参照できる一方、`/proc/locks` の `READ_FILE` と起動時自己 pid の `/proc/<pid>` に限る `READ_FILE` / `READ_DIR` 以外の `/proc` read、他 principal の領域は拒否される。`/proc/self` と peer pid directory への依存がない|
|AC-5|afx-ops の実 UDS 接続は共有 server 認証に加えて、必須 `SO_PEERPIDFD`、自己 pid fdinfo による pid 照合、dev 一致、token 送信直前と要求中の pidfd poll を全て照合してから成功する。pidfd 不可、pid 不一致、pidfd `POLLIN`、dev 不一致、各一項目の偽装、`/proc/locks` 不可では token byte 数が 0 となる。pidfd が reply 完了又は例外まで開いたままで、要求 object が一回だけ close することを fd 監視で観測する|
|AC-6|afx 再起動で socket / lock inode が変わった後、新 path を再認証できた場合だけ ops が復帰する。認証不能なら旧 process は非零終了し、新 process が最初から隔離される|
|AC-7|各 feature の最終 BPF 番号表を検査し、未分類 / 重複を生成失敗にする。hostile canary が新規 bind、listen、ptrace、`process_vm_*`、`pidfd_getfd`、`/proc/<pid>/mem`、継承 fd、他鍵読取、web の AF_INET/AF_INET6 socket、`CLONE_NEW*` 又は `CLONE_PARENT` を含む clone、`unshare`、`setns` を拒否する。`clone3` は ENOSYS、`afx-ops-clone-thread-only-v1` の thread flags 完全一致の legacy `clone`、`PR_SET_DUMPABLE=0`、web の trace 固定の `accept4` 又は `accept`、bot / notify の TCP connect、AF_UNIX NSS、任意宛先・任意 port の UDP DNS、`sendto` / `recvfrom`、DNS cache miss、`futex` だけを許す。実 web accept loop、Discord gateway reconnect、webhook hostname 再解決を通し、accept syscall を実 trace と番号表で一致させる|
|AC-8|未登録 user、確認前に除外された user、別 actor、別 guild / channel / message、旧 display generation、期限切れ、使用済み nonce、payload digest 変更、並行 confirm、改変 `custom_id`、`custom_id` action と record action の不一致の全てで daemon に決定が届かない。形式不正又は未知 ID は無変更で拒否し、`approval_id` が一意に record を特定できる改変又は action 不一致だけを原子的に invalidated とする。全 interaction は 3 秒以内に ephemeral defer し、daemon 遅延、15 分境界、ACK 後の daemon 失敗、起動直後の旧 message click を観測する。再起動後は View 登録、全旧確認の失効、「再確認が必要」への bot token edit、adopt / repost、gateway 接続の順を観測する|
|AC-9|`ops-bot` の正しい actor header は監査行と `(principal, actor)` job 所有に残り、決定作成と polling が同じ actor で成功する。ops-bot の決定 / polling で header なし、形式外、又は他 principal の header は v1.13 契約どおり拒否され、`decided_by` は principal のままである|
|AC-10|web は隔離前の `127.0.0.1` listener と cookie session 認証だけで表示でき、隔離前の接続が handler に届かず隔離後に初めて処理される。実 trace は runtime が使う `accept4` 又は `accept` を一つに固定し、番号表はその syscall だけを許す。直接 HTTP、web 鍵、侵害を模した route のいずれからも変更 / 承認はできない|
|AC-11|notify は実 event を generic HTTPS webhook へ送り、重複 event ID を識別できる。許可外 field、自由文、鍵が payload に無い|
|AC-12|実 systemd user manager で機能別 unit の `BindsTo=`、`PartOf=`、`After=`、restart / start-limit 設定を観測する。ops 単独 start は afx を pull-in せず、afx の `Wants=` は ops を pull-in する。afx 停止、explicit restart、crash と復帰では ops も停止・再起動し、ops crash は 5 秒後に再起動、60 秒 3 回で start-limit となる。UDS ready が 30 秒以内なら起動し、期限超過なら fail closed となる|
|AC-13|共有 server 認証の spec 表から生成した JSON の black-box ケースを、本体 client と afx-ops の**共有 server 認証 verifier**へ同一入力として与える。afx-ops 側は固有 admission を通さない専用 harness から、その verifier を別 process で直接呼ぶ。socket 所有者、親 directory、peer uid、lock holder、保持者生存、optional pidfd、lock inode 交換、dev 不一致時の readlink fallback、`/proc/locks` 不可を含み、各ケースの fake daemon が記録した pre-auth byte 数、client rc / code が第三者 oracle の許可 / 拒否結果と一致する|
|AC-14|全 off では ops unit と `Wants=` が無い。disable の各外部操作の直前・直後、stage 更新の直前・直後で crash させ、`enabled → stopping-ops → uninstalled → disabled` の唯一の再開規則を観測する。停止失敗は `enabled`、uninstall 失敗は `stopping-ops`、uninstall 確認失敗は `stopping-ops` から再開し、最後の disable 後には対象 unit、`Wants=`、ops 用 unit 雛形が無く、鍵は revoke されない|
|AC-15|**公開 afx-ops 要求経路**を、固有 admission と共有 server 認証 verifier の連鎖で呼ぶ。pidfd 不可と dev 不一致は共有 oracle が許可するケースでも afx-ops が固定 reason で拒否し、pre-auth byte 数は 0 となる。本体 client は共有契約どおり optional pidfd と dev 不一致 fallback を維持する|

## 10. task 表

|順|成果物|完了条件|
|---|---|---|
|T1|ops-api v1.13|versioned manifest / enroll と actor header 契約を spec、server、client に固定する|
|T2|独立 package と oracle|配布定義、本体 import 不可検査、AC-13 の第三者 oracle を持つ|
|T3|鍵 lifecycle と enable 状態機械|停止中 enroll、crash 回復、stage file の再開 / rollback 境界を実 systemd で証明する|
|T4|UDS client と再認証|共有 server 認証 oracle、ops 固有 pidfd / dev admission、要求期間の pidfd 所有、lock holder 照合、偽 server 拒否、再起動時 fail closed を実 UDS で証明する|
|T5|自己隔離|Landlock 規則、feature 別 seccomp 番号表、`clone3` ENOSYS、thread-only legacy `clone`、trace 固定 accept loop、canary、cross-process token 防御を実 process で証明する|
|T6|plain gate と bot|allowlist、原子的確認、3 秒 defer、action 付き persistent View、actor header、指定順の adopt / repost / reconcile / View 除去を実装・試験する|
|T7|web|cookie session 認証、loopback pre-bind、自前 accept loop、read-only principal、書込み到達不能を確認する|
|T8|notify|event cursor、重複許容、DNS を含む generic HTTPS webhook 配送を確認する|
|T9|systemd と wizard|機能別 enable / disable の durable 遷移と crash 回復、readiness、全 off 無配置、最小質問・疎通確認を自動化する|

T1、T3、T4、T5 が完了するまで、ops process を本番鍵で起動しない。

## 11. 残余リスク

|点|扱い|
|---|---|
|同 uid の非隔離主体|利用者 shell と Landlock のない trade worker は既存鍵境界の内側であり、本設計では守らない|
|本物停止中の偽 server|攻撃者が instance lock も取得できる場合の既存 client 認証の限界は残る|
|非対応 filesystem|`data/` が btrfs、overlayfs、NFS 等で lock の dev が `/proc/locks` と一致しない環境では afx-ops を起動できない|
|Linux kernel|`SO_PEERPIDFD` を持たない kernel 6.5 未満では ops は固定 reason で起動を拒否する|
|DNS|bot / notify の DNS は AF_UNIX と、宛先 address・port を seccomp で判定しない UDP を許すため、任意宛先・任意 port への UDP 送信が可能であり、名前解決先と resolver 実装の信頼に依存する|
|Discord / webhook|外部サービスの可用性、投稿削除、event の best-effort 生成により、表示又は通知が遅延・欠落・重複し得る。監査の正本は daemon に残る|
|通知先の内容|固定 schema に限定しても、event の時刻・識別子・状態を外部送信することは送信先の信頼に依存する|
|承認後の回復|誤承認後の plugin disable / rollback は既存の未整備事項のままであり、afx-ops は解決しない|

## 12. 未決事項 (ユーザー裁定待ち)

次の四件はユーザー裁定で確定する。初版実装は仮置きの範囲を超えない。

1. **web 認証** — 仮置きは cookie session とする。UI 用 secret は API 鍵と分離し、`afx ops` が初回に生成して表示する。secret rotation、session の有効期間、loopback HTTP の `Secure` 非設定の扱いを設定 schema で確定する。根拠は、read-only であっても loopback 利用者を認証し、API 鍵の露出と bearer secret の常設を避けるためである。
2. **bot の設置範囲** — 仮置きは単一 guild・単一 channel とする。複数 guild / channel の設定、allowlist、状態領域分離は初版に含めない。根拠は、承認の表示起点と確認 record の束縛を単一の運用境界で検証可能に保つためである。
3. **notify の配送先** — 仮置きは generic HTTPS webhook だけとする。Discord、Slack、email 等の種類別 adapter は初版に含めない。根拠は、固定 schema と event cursor の配送契約を adapter 固有の認証・再試行規則から独立させるためである。
4. **`ops-bot` の権限** — 仮置きは `ask`、`improve`、その他 operator 操作を与えないこととする。必要になれば別 principal と別 process を設計する。根拠は、Discord interaction が plugin 決定以外の操作権限へ拡大することを防ぐためである。

## 13. 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-10-09|v1.0|C0 revision 0.4 (設計レビュー 4 周、最終周で収束) を公開 spec として清書|実装着手条件の固定 (未決 4 件はユーザー裁定待ち)|—|

# [ops-api] 操作 API と client 契約設計 v1.8

版: v1.8

日付: 2026-10-05

対象チケット: `policy-add-unwired-in-service`、`secret-env-guard-false-positive`、`load-settings-loads-dotenv-as-side-effect`、`legacy-submit-corridor-bypasses-gate`

対象: 同一マシンの agentic-fx daemon を操作する UDS API と同梱 CLI client `afx ctl`

## 要点

- API は agentic-fx daemon の同一プロセス内で動く有界な UDS HTTP server とし、全 endpoint を初回リリースから公開する。実装着手前に plugin worker 隔離を投入し、改善 mission worker の seccomp は実装の着手条件ではなく **運用上の有効化 (listener を開く) の条件** とし、seccomp 投入まで `api.enabled` の既定を false にする。機能を段階開放する実行時状態は持たない。
- 認証は導入インスタンス固有の principal 鍵で行い、GET も用途別 scope で最小権限化する。承認、kill switch 解除とその回復確定、価格源復帰は approver に限る。発注、SL 変更、クローズ、資金保護の無効化、`autopilot on`、サービス停止は API に出さない。
- 鍵は `~/.config/agentic-fx/api/<instance_id>/` に principal ごとの別ファイルで置く。`.ready` は鍵集合の commit marker とし、client は鍵を生成しない。
- 承認画面は既存の `approval_facts` が親の受理・計測・状態遷移として記録した `parent_facts` と、agent の自己申告 `agent_claims` を分けて表示する。決定内容の digest を再照合し、`decided_by` は要求本文でなく認証 principal からサーバが記録する。
- 初版 principal は `operator` と `approver` の 2 つだけとする。scope は用途別に細分し、principal から scope 集合への対応をデータとして持つため、後続設計で principal を追加できる。
- `GET /v1/events` と固定 schema の event は API 側で実装する。永続化された event の配送は at-least-once、event の生成は best-effort とする。
- 監査の正は append-only DB `ops_requests`。activity と通知 event は人向けの投影であり、監査判定には使わない。

## 1. 目的、範囲、実装着手の条件

### 1.1 目的

利用者が daemon の資金保護 tick を止めずに、同じマシンから現行の対話シェル相当の閲覧・日常操作・plugin 決定・限定的な回復操作を行えるようにする。コマンド文字列を送る万能 endpoint は作らず、構造化した ops 関数を対話シェルと API が共有する。

### 1.2 実装着手の条件

**前提: plugin worker 隔離が main に投入されていること (2026-10-05 `afdfb81` で充足)。** sandbox から他デーモン経由の脱出は実測せず塞ぐ (チケット `sandbox-escape-via-user-daemons-unmeasured`、2026-10-04 ユーザー裁定 案 1): 改善 mission worker への seccomp 投入 (別設計 `improve-cli-seccomp`、plugin worker 隔離と同規模) は **実装の着手条件ではなく、運用上 listener を開く条件** とする (2026-10-05 ユーザー裁定 案 2: 最終的な結果が変わらないこと)。T1〜T5 は seccomp と並行して進め、seccomp 投入まで `api.enabled` の既定は false (daemon は警告のみ、§5)。seccomp 投入後に既定を true へ変える変更を seccomp 束の完了条件に含める。人手実測はチケット `escape-probe-manual-measurement` で別途扱う。

trade worker の Landlock はこの前提に含めない。trade profile の `ClaudeRunner` の argv を `--tools StructuredOutput,ToolSearch` に pin し、LocalRunner の registry を読み取り専用に固定する。trade worker 自体の Landlock は別チケットとする。

### 1.3 本束の範囲

- daemon 内 UDS API、ops 層、job、鍵管理、DB 監査、同梱 CLI `afx ctl` / `afx keys`。
- `app_state.json` の世代付き kill switch CAS は main の既存実装を利用し、API との結合と競合テストだけを追加する。
- 承認の親記録と安全な表示は main の `loops/approval_facts.py` と `Commands._approval_detail` を利用する。
- `load_settings` の dotenv 副作用の分離、legacy submit 回廊の廃止、既存 policy 配線と secret env allowlist の結合確認。
- scope の細分、principal → scope のデータ構造、監査主体の分離、固定 schema の event 記録と polling。

### 1.4 範囲外

- 発注、SL 変更、クローズ、risk gate や drawdown kill switch の無効化、`mode`、`autopilot on`、サービス停止。
- `live_trade` 承認。最新価格、残高、Risk Gate、sizing、発注を `core_lock` 内で一体化する専用レーンは本設計の範囲外であり、別設計で扱う。
- TCP/TLS/reverse proxy、別マシンへの直接公開。
- 配備済み plugin の disable / rollback。`deployed-plugin-has-no-disable-or-rollback` (高) で扱う。初版の運用文書に「承認後の回復手段は未整備」と明記する。
- `activity.log` の rotation / retention。`activity-log-grows-without-rotation` (低) で扱う。
- symlink 配備の通常退役、緊急 disable / rollback、依存 strategy 確認。チケット `retire-symlink-deployed-plugin` は `deployed-plugin-has-no-disable-or-rollback` の設計へ統合して責務を決める。

`afx-ops`（bot / web / notify を機能ごとに別 process・別鍵で動かす）と Discord cog は次の束の別設計で扱う。そこで process 間の鍵分離、process を縛った後の server 認証、外部利用者の認可と確認、principal の鍵を追加する `enroll` を決める。本 spec はそれらの principal を追加できる principal → scope のデータ構造と、nullable な `asserted_actor` 欄だけを用意し、外部 client の principal・scope・process・unit・利用者規則は定めない。

## 2. 信頼境界と通信

API は `data/run/api.sock` の pathname UDS だけで待ち受ける。dir は 0700、socket は 0600。localhost TCP、pathname UDS、abstract UDS はいずれも既存 Landlock のファイル規則から到達できることが実測済みであり、UDS 自体を sandbox 境界とはみなさない。UDS を選ぶ理由は `SO_PEERCRED` と `SO_PEERPIDFD` で peer uid / pid を検査できることにある。

server は認証前に uid と子孫関係を検査し、サービス自身の子孫を本文を読まず拒否する。`SO_PEERPIDFD` は数値 77 で取得でき、fdinfo の `Pid:` で生存中の pid を再確認できることが実測済みである。二重 fork で子孫判定を外れられることも実測済みなので、主防御は隔離された文脈から読めない鍵と seccomp であり、子孫検査は多層防御である。

client は鍵を送る前に、socket の型・所有者・親 dir 0700、`SO_PEERCRED` の uid、`data/instance.lock` の保持者 pid と peer pid、可能なら pidfd の pid を照合する。`/proc/locks` から保持者を確定できなければ鍵を送らず rc=3 とする。pidfd を取得できない環境では server は `decide` と `local_guard` を拒否する。

初版ではサービスを child subreaper にしない。二重 fork の捕捉範囲は広がるが、無関係な孤児 process の回収責務まで引き受けるためである。

同じ uid で隔離されていない利用者 shell と trade worker は鍵を読めるため、信頼境界の内側に置く。この設計は、そのいずれかを侵害した攻撃者から鍵や `local_guard` を守らない。plugin worker、改善 worker、gate pytest worker はそれぞれの Landlock profile から鍵 dir 全体へ到達不能にし、隔離の適用に失敗した worker は起動しない。

|仕組み|守るもの|守らないもの|
|---|---|---|
|principal 鍵 + scope|鍵を持たない client による endpoint 利用、機能間の権限混同|同 uid で隔離されていない process による鍵ファイル読取|
|worker Landlock|plugin worker、改善 worker、gate pytest worker から鍵 dir 全体への到達|利用者 shell、Landlock のない trade worker|
|UDS + peer / 子孫検査|別 uid、遠隔接続、通常の service 子 process からの直接利用|同 uid の隔離されていない process、二重 fork で子孫判定を外れた process|
|`local_guard` の UDS 限定 + `approver` 鍵|TCP 経由、将来 client principal、隔離 worker からの guard 操作|同 uid の隔離されていない process。これは approver 鍵を読んで UDS を呼べる|

## 3. principal、鍵、承認

### 3.1 permission と principal

|scope|内容|endpoint|
|---|---|---|
|`status.read`|稼働状態と自分の認証情報、reflection 試行記録の閲覧|status、whoami、reflection 試行記録|
|`events.read`|固定 schema の通知 event の閲覧|events|
|`approvals.list`|承認依頼の安全な一覧の閲覧|approval list|
|`approvals.detail`|親記録、agent claims、holdout / in-sample 数値、reject reason を含む承認詳細の閲覧|approval detail|
|`logs.read`|log と activity の閲覧|log、activity|
|`jobs.own`|同じ `(authenticated_principal, asserted_actor)` が作成した job の閲覧と queued cancel|job get / cancel|
|`operate`|資金保護と採用に直接触れない日常操作|ask、improve、backlog、policy、reflection retry|
|`decide`|plugin 承認依頼の approve / reject / retry|決定 3 本|
|`local_guard`|kill switch 解除、解除途中状態の確定、data resume|UDS 上の 3 本だけ|

|principal|scope|`decided_by`|鍵|
|---|---|---|---|
|`operator`|`status.read events.read approvals.list approvals.detail logs.read jobs.own operate`|—|`operator.token`|
|`approver`|`status.read events.read approvals.list approvals.detail logs.read jobs.own operate decide local_guard`|`api:approver`|`approver.token`|

初版はこの表を設定ではなく versioned な認可データとして持ち、未知 principal / scope、重複、`local_guard` を `approver` 以外へ与える表では API を起動しない。endpoint の認可はこのデータを参照し、後続束は同じ構造へ principal を追加する。承認詳細の holdout / in-sample 数値と reject reason を読めるのは `approvals.detail` を持つ principal だけである。

server が鍵から確定した主体を `authenticated_principal`、将来 client が外部主体を申告するため予約する nullable 欄を `asserted_actor` として、監査・job 所有者・表示で必ず別欄に持つ。初版の CLI は `asserted_actor` を送らず、server は常に null を記録する。`asserted_actor` は認証や認可の根拠にせず、`decided_by` は必ず `authenticated_principal` だけから生成して要求 body では変えられない。job 所有者の schema は `(authenticated_principal, asserted_actor)` とする。

### 3.2 鍵 lifecycle

鍵 dir は `~/.config/agentic-fx/api/<instance_id>/`、dir 0700、principal ごとの鍵と `.ready` は 0600 とする。鍵は 256 bit の CSPRNG 出力とし、読込・検査は `O_NOFOLLOW`、通常 file、期待 owner、dir 0700 / file 0600 を必須にする。サービス起動時に鍵を読み、平文を捨てて digest だけを保持し、照合は定時間比較とする。鍵を env、argv、子 env、ログ、activity、応答へ載せない。

`afx keys init` と `afx keys rotate|revoke <principal>` は `data/instance.lock` を non-blocking で取得できる停止中だけ実行する。`init` は `.ready` が無い初回だけ `O_CREAT|O_EXCL` で欠けた初版鍵を作る。`rotate` は `active(g) → preparing(g+1) → active(g+1)` と遷移し、新鍵を同じ dir の一時 file に書いて file fsync、atomic rename、dir fsync、最後に新しい `.ready` を atomic rename + dir fsync で commit する。`revoke` は `active(g) → preparing(g+1) → revoked(g+1)` とし、鍵 file を non-secret tombstone へ atomic rename で置換して dir fsync、最後に revoked を示す `.ready` を atomic rename + dir fsync で commit する。既に revoked の revoke は同じ状態を返す。

`.ready` は principal ごとの generation、状態、鍵 digest を持つ鍵集合の commit marker である。鍵操作の途中で失敗して `.ready` と実体が一致しない状態は「旧世代も新世代も ready ではない」を意味し、daemon は API を起動しない。自動補完は初回 `init` の `.ready` 未作成時だけに限り、rotate / revoke の残骸を自動生成や旧鍵への rollback で隠さない。同じ key command を再実行して準備状態から完了させる。

漏洩時は、(1) daemon を停止、(2) `afx keys rotate <principal>` または権限を止めるなら `revoke`、(3) daemon を起動して旧鍵が拒否されることを確認、(4) rotate なら同梱 CLI が新鍵を読めることを確認する。停止中も資金保護は既存の latch と運用手順で維持し、鍵交換のために guard を緩めない。

`load_settings` は dotenv を読み込まない純粋な設定検証に変え、dotenv の明示読み込みは必要な top-level 起動経路にだけ置く。`afx ctl` と `afx keys` は `.env` をロードしない。

### 3.3 承認の表示と決定

承認詳細は既存の `approval_facts` が保存した `parent_facts` と `agent_claims` を分離して表示する。親が受理・計測・状態遷移として記録した値だけを判断材料の親欄に置き、agent の自由文を事実扱いしない。全表示文字列は既存 sanitizer で Cc / Cf を除去し、1 行化と長さ上限を適用する。

approve と retry は表示した `payload_json` の SHA-256 を必須とし、受理時と plugin flock 内の再読時に照合する。決定は `kind='plugin'` に限り、未知 kind と `live_trade` は受理時と job 実行時の両方で fail closed にする。`decided_by` は server が `authenticated_principal` から生成し、初版の `asserted_actor` は null を記録する。

## 4. API 契約

### 4.1 共通形

全 endpoint は `/v1/` 配下、JSON、`Content-Length` 必須、chunked 不可、1 接続 1 要求とする。認証は `Authorization: Bearer <token>`。成功は `{"ok":true,"data":{...}}`、失敗は固定 code / 固定文の `{"ok":false,"error":{...}}` とし、内部例外は incident id だけを返す。

実装着手条件を満たした完成形だけを実装するため、実行時の段階状態、段階専用の応答・schema 欄、切替 task は作らない。

`POST /v1/policy`、`POST /v1/backlog`、`POST /v1/asks`、`POST /v1/improve/runs`、`POST /v1/reflections/{order_id}/retry` の 5 本は `Idempotency-Key` を必須とする。durable な状態機械と crash 後の応答は §8 に定める。保持は 24 時間で、client は応答喪失時に同じ key で 1 回だけ再送する。

主な失敗 code は `invalid_argument`、`unauthenticated`、`forbidden`、`peer_rejected`、`autopilot_restricted` (`killswitch reconcile` には返さない)、`not_found`、`request_timeout`、`already_decided`、`payload_changed`、`not_deployed`、`invalid_state`、`not_latched`、`mission_busy`、`improve_running`、`decision_in_progress`、`generation_mismatch`、`attempt_changed`、`unsupported_kind`、`plugin_busy`、`idempotency_mismatch`、`outcome_unknown`、`job_running`、`payload_too_large`、`unavailable`、`database_busy`、`internal` とする。前提の未達を実行時 error にする code は持たない。

### 4.2 現行コマンドと endpoint の全数表

|#|現行コマンド|endpoint|permission|処理と二重実行防止|
|---|---|---|---|---|
|1|`status`|`GET /v1/status`|`status.read`|mode、autopilot、kill switch の latched / generation / latched_at と **`reconcile_required` (解除の途中で止まった marker の有無) および marker があるときの `requested_generation` / `started_at` / ファイル上のラッチ値**、残高、equity、orders、mission、health、data を構造化|
|2|`log [n]`|`GET /v1/log?n=`|`logs.read`|1〜500 行、後方 scan 4 MiB、1 行 2,000 文字|
|3|`activity [n] [category]`|`GET /v1/activity?n=&category=`|`logs.read`|列挙 category、後方 scan 4 MiB、打切りを明示|
|4|`approval <id>`|`GET /v1/approvals/{id}`|`approvals.detail`|親記録、自己申告、reason、`decided_by`、`asserted_actor`、archive、依存、holdout / in-sample、`payload_sha256`|
|5|`approval list [n]`|`GET /v1/approvals?status=pending&limit=`|`approvals.list`|pending と未終端 journal。holdout / in-sample 数値、reason は一覧に出さない|
|6|`ask <質問>`|`POST /v1/asks` → job|operate|冪等 key、1〜4,000 文字、mission slot の原子的受理|
|7|`data resume --acknowledge`|`POST /v1/data/resume`|local_guard|UDS のみ。gap summary を返し、次 tick で判定|
|8|`approve <id>`|`POST /v1/approvals/{id}/approve` → job|decide|plugin kind、digest 二重照合、既存 CAS、`decided_by` 記録|
|9|`reject <id> [理由]`|`POST /v1/approvals/{id}/reject` → job|decide|plugin kind、既存 CAS。理由は既存 `approval_requests.reason` だけに保存し、監査・activity・job・event に残さない|
|10|`approval retry <id>`|`POST /v1/approvals/{id}/retry` → job|decide|approve と同じ digest、flock、outcome 規律|
|11|`killswitch reset <世代>`|`POST /v1/killswitch/reset`|local_guard|UDS のみ。main に既存の `reset_kill_switch(expected_generation)` を呼ぶ。解除の途中状態 (marker 残存) では既存実装が `StateUncertain` で拒否し、行 11b へ誘導する|
|11b|`killswitch reconcile confirm`|`POST /v1/killswitch/reconcile`|local_guard|UDS のみ。解除が途中で止まり marker が残った状態を、**ラッチ中として確定**して marker を消す (既存 `confirm_latched`)。ファイルを解除側へ倒す操作ではなく、資金保護を弱めない。marker が無ければ 409 `invalid_state` で無変更。autopilot 中も許可する (ラッチ側にしか倒れないため)。確定後の解除は行 11 を世代付きで改めて実行する。marker の内容表示は行 1 の status が担い、確定だけを本 endpoint が行う|
|12|`reflect retry <order_id>`|`POST /v1/reflections/{order_id}/retry`|operate|`Idempotency-Key` 必須。body に client が行 12b で見た試行記録の識別子 (`expected_attempts`、`expected_last_attempt_at`、記録なしは `0` / null) を必須で送り、台帳の現在値と一致する場合だけ clear する。不一致は 409 `attempt_changed` で無変更。closed かつ reflection 未済の条件は従来どおり|
|12b|—|`GET /v1/reflections/{order_id}`|status.read|order の status、reflection の有無、試行台帳 (`attempts`、`last_attempt_at`) の現在値。自由文の `last_reason` は返さない|
|13|`improve`|`POST /v1/improve/runs` → job|operate|冪等 key、同時 1 本、停止時 join|
|14|`improve add <idea>`|`POST /v1/backlog`|operate|冪等 key、1〜2,000 文字|
|15|`backlog reject <id>`|`POST /v1/backlog/{id}/reject`|operate|状態条件付き UPDATE|
|16|`backlog reopen <id>`|`POST /v1/backlog/{id}/reopen`|operate|状態条件付き UPDATE、selected 不可|
|17|`backlog note <id>`|`POST /v1/backlog/{id}/note`|operate|状態条件付き UPDATE|
|18|`policy add <text>`|`POST /v1/policy`|operate|冪等 key。既存 `directives_path(root)` 配線を ops 層で再利用|
|19|`stop`|**廃止: endpoint に載せない**|—|systemd の SIGTERM を使う|
|-|`help`|client 内|—|server へ送らない|
|新|—|`GET /v1/whoami`|`status.read`|`authenticated_principal`、`asserted_actor`、scopes|
|新|—|`GET /v1/jobs/{id}`|`jobs.own`|所有者の組が一致する job の状態と結果|
|新|—|`POST /v1/jobs/{id}/cancel`|`jobs.own`|所有者の組が一致する queued だけ cancelled、running は 409|
|新|—|`GET /v1/events?after=&limit=`|`events.read`|DB cursor 後の通知用投影。limit 1〜500、長い待受はしない|

実装する endpoint は 24 本で、内訳は変更系 15 本 (POST: asks、data/resume、approve、reject、retry、killswitch/reset、killswitch/reconcile、reflections retry、improve/runs、backlog add、backlog reject / reopen / note、policy、jobs cancel) と読取 9 本 (status、log、activity、approval 詳細、approval 一覧、reflection 試行記録、whoami、job get、events) である。このうち冪等 key 必須は 5 本、`local_guard` は 3 本 (reset、reconcile、data resume)、決定は 3 本である。

### 4.3 event 契約

同梱 CLI は短い polling を使う。server は外向き接続、webhook、WebSocket、SSE、長い HTTP 待受を増やさない。`GET /v1/events` は単調増加する event id / ts / code / ref / 固定 schema の fields と high watermark を返す。**永続化された event の配送は at-least-once とし、event の生成は best-effort とする。** client は event id で表示を冪等化してから最後に処理した id を保存し、切断後は同じ id から再開する。

|event code|`ref`|許可する fields|
|---|---|---|
|`ops_request_terminal`|audit id|`endpoint_code`、`result_code`、`authenticated_principal`|
|`approval_state_changed`|approval id|`state`、`decision_code`、`decided_by`|
|`job_state_changed`|job id|`job_kind`、`state`、`result_code`|
|`guard_state_changed`|instance id|`guard_kind`、`state`、`generation`|
|`service_health_changed`|instance id|`component_code`、`health_code`|
|`event_stream_health`|instance id|`failure_count`、`first_failed_at`、`last_failed_at`|

各 code は上表以外の field を拒否する。notification-safe summary は code と上表の値だけから client が生成し、DB に自由文を持たない。holdout / in-sample の数値、agent の自己申告文、reject reason、ask / policy / backlog の本文、鍵、内部例外、その他の自由文は event に入れない。

event は append-only DB `ops_events` に置く通知用投影であり、要求監査の正は `ops_requests` のままである。cursor は `ops_events.id` とし、初版は無期限保持する。永続化済み event は同じ id を再配送し得るが失わず、high watermark の後退は health error として cursor を進めない。一方、event INSERT 失敗は元操作を失敗させず、その event は生成されない。生成失敗には event id が割り当てられないため、連番の欠けから取りこぼしを断定できない。失敗をメモリ上で観測できた場合は復旧後に `event_stream_health` の生成を試みるが、これ自体も best-effort である。生成されなかった event は `ops_requests` および正の業務表との突き合わせでしか復元できず、event stream 単独には完全性を保証しない。

## 5. job、並行性、上限

ask、手動 improve、approve / reject / retry は 202 と job id を返す。client は `GET /v1/jobs/{id}` を poll する。job 表はメモリ上で、未終端は決定 queued 8 + running 1、ask 1、improve 1、終端は 64 件か 1 時間とし、終端の古いものから evict する。

DB 操作は 2 レーンに分ける。ops レーンは ops lock + ops 用接続で read と通常変更を行う。決定レーンは decide lock + 決定用接続 + worker 1 本で決定だけを FIFO 実行する。両 lock を同時に取らず、API thread は `core_lock` を取らない。

ops lock、decide lock、plugin flock、StateStore flock、SQLite busy、socket / queue / thread の待ちを含む全ての待機は non-blocking の反復取得とし、各試行の上限を `min(その待機の設定値, caller の monotonic deadline までの残り時間)` にする。同期要求の caller deadline は accept から既定 5 秒、非同期 job は受理時に固定した job deadline とし、どちらも `stop_event` を毎回確認する。plugin flock は既定 30 秒、0.1 秒間隔で試し、取得前の timeout / shutdown は無変更で終える。設定値が要求の残り時間より長くても要求 deadline を延長しない。

同時接続 4、要求全体 deadline 5 秒、本文 64 KiB、ops lock 待ちの設定値 10 秒（実効値は要求の残り時間以下）、log / activity scan 4 MiB とする。API bind や listener が失敗しても daemon と資金保護 tick は継続し、health latch、activity、notifier に記録する。

停止は、(1) listener を閉じて新規受付を止める、(2) queued job を `shutdown` 終端にして待ち行列を閉じる、(3) running job に停止合図を渡し、未取得 lock 待ちは中止、既に commit 点を越えた処理は定義済み終端まで収束させる、(4) API thread、decide worker、improve thread を bounded join する、の順とする。join 自体も shutdown deadline を越えない。

`api.enabled=false` の daemon 起動は拒否せず警告する。daemon と API の可用性を同一視せず、資金保護を優先する。

autopilot 中は全 GET、reject、ask、jobs、`killswitch reconcile` だけを許し、その他の変更 (`killswitch reset` を含む) を 403 `autopilot_restricted` にする。`killswitch reconcile` を許すのは、回復はラッチ中への確定だけを行い、資金保護を弱めないためである。`autopilot on` 自体は API に存在しない。

**queued の job は実行開始時に autopilot を再確認する。** autopilot で制限される job (approve、retry、手動 improve) は、worker が最初の変更に入る直前に StateStore の flock 内で autopilot を読み、true なら `autopilot_restricted` で終端して何も変更しない。autopilot の切替も同じ flock 内の書込みなので、順序は 2 通りに定まる。切替が先なら job は開始判定で終端し、開始判定が先なら job は autopilot 前に開始したものとして定義済みの終端まで収束し、その後に新しい変更を始めない。どちらの順でも、autopilot=true になった後に新しい変更が始まることはない。受理時の autopilot 判定は維持し、queue 中の状態変化だけを開始判定が補う。

## 6. 状態ファイルと既存機能

main の `StateStore` はプロセス内 lock、プロセス間 flock、一意の一時ファイル、file fsync、replace、親 dir fsync、`kill_switch_generation` / `kill_switch_latched_at`、`reset_kill_switch(expected_generation)` を既に持つ。`Commands` の `killswitch reset <世代>` も既存である。本束は更新アルゴリズムを複製せず、`_exclusive` とそこへ入る公開更新 API に monotonic deadline と `stop_event` を受ける経路を追加する。endpoint はその経路で既存の世代 CAS を呼び、executor の再ラッチとの競合も結合テストする。状態ファイルの更新経路が `StateStore` だけである不変条件は維持する。

main の `approval_facts.py` は親が受理・記録した `parent_facts` と `agent_claims` の分離、validation、sanitizer、render を既に持つ。`Commands._approval_detail` もこれを表示する。本束は schema と renderer を共有し、別形式を作らない。

## 7. client

### 7.1 同梱 CLI `afx ctl`（本束）

`afx ctl <操作>` と、端末上の `afx ctl>` 対話モードを実装する。parser、結果 formatter、sanitizer は対話シェルと共有する。引数なしを非端末で起動した場合は usage と rc=2。`stop` は送らず systemd の停止方法を案内する。

approver 操作は stdin と `/dev/tty` の両方が端末の場合だけ送る。`--yes` は作らない。approve は親記録と自己申告を表示して id の再入力、reject / retry / data resume / job cancel は y/N、kill switch reset は世代と発動時刻と equity を表示して `reset` の入力を求める。`afx ctl killswitch reconcile` は status の marker 内容 (要求世代、開始時刻、ファイル上のラッチ値) を表示し、ラッチ中として確定する旨を示して y/N を求める。確定後は `killswitch reset <世代>` を別に案内し、自動では解除しない。途中で Ctrl-C しても受理済み job は続き、queued の間だけ明示 cancel できる。

## 8. 監査と activity

監査の正は append-only DB `ops_requests` とする。各行に `authenticated_principal` と nullable な `asserted_actor` を分けて持ち、初版では後者を常に null とする。全ての変更要求は副作用より前に `accepted` を INSERT し、`PRAGMA synchronous=FULL` の transaction commit が成功した時点を accepted の commit 点とする。書けなければ実行しない。完了は別の終端行で INSERT し、`ops_requests` は UPDATE / DELETE しない。ほかに endpoint、peer pid / exe、idempotency key と本文 digest、対象 ref、phase、result code、job id、再送用 response を持つ。

冪等要求は durable 表で `(authenticated_principal, endpoint, idempotency_key)` を一意にし、本文 digest と `accepted → succeeded | failed | outcome_unknown` の一方向状態を持つ。accepted 行と状態の作成を同じ transaction で commit してから副作用へ進み、終端状態と `ops_requests` 終端行も同じ transaction で commit する。同じ key / digest の終端後再送は保存済み response / job id を返し、異なる digest は 409 `idempotency_mismatch` とする。

daemon は API listener を開く前に、次の順で起動時回復を行う。**先に既存の plugin 切替 journal の reconcile (`system_reconcile`、`service.py` の起動処理で reconcile → sweep → expire の順に既に走る) を済ませ、その後に ops の回復を行う。** 決定 (approve / reject / retry) の結果の正は `approval_requests` と切替 journal であり、ops の回復はそれを読むだけで書き換えない。既存の reconcile は journal の `switched` 行で live 側が新版なら `retry_approval(decided_by="system_reconcile")` で決定を完遂し、live が旧版のままなら journal を巻き戻し、それ以外の phase は明示 retry を待つ。ops の回復は、journal reconcile が既に終端を決めた決定要求にはその結果 (approval の終端状態と `decided_by`) を元要求の終端行へ追記し、結果が `system_reconcile` による場合はそれを終端行に記す。journal が決められない要求だけを `outcome_unknown` にする。ops の回復が決定を自動で再実行することは無く、`system_reconcile` による完遂は既存の journal 規律であって元要求の自動再送ではない。

その上で、**全ての変更 endpoint** の `accepted` かつ終端行なしで上の規則で解決しなかった要求を走査し、元要求に対応する `outcome_unknown` 終端行を append-only で追記する。副作用は再実行せず、冪等 key を持つ 5 endpoint は durable 状態も同じ transaction で `outcome_unknown` にして以後の再送へ 409 の固定応答を返す。key を持たない要求は元 HTTP 応答を再取得できないが、audit id の終端と対象の GET で利用者が状態を確認できる。対象状態が成功らしく見えても、別要求との識別ができないため成功へ推測昇格しない。

|対象|crash 点|再起動時の扱い|再実行時の扱い|
|---|---|---|---|
|全変更要求|accepted commit 前|要求は未受理、副作用 0。回復行なし|新規要求として実行可|
|全変更要求|accepted commit 後、副作用開始前または実行中|`outcome_unknown` を追記。副作用を開始・再開しない|key ありは同じ 409、key なしは新しい要求として通常の状態条件 / CAS で判定|
|全変更要求|副作用後、終端 commit 前|`outcome_unknown` を追記。対象 GET と正の業務表は現状態を返すが、監査結果を success と推測しない|同上。状態条件 / CAS により二重作用を防ぐ|
|全変更要求|終端 commit 後|既存の `succeeded` / `failed` を維持|key ありは保存応答、key なしは新規要求|
|approve / reject / retry|accepted 後、journal が終端を決めた crash 点 (journal `switched` 完遂後の起動時 reconcile による `system_reconcile` 決定、または巻き戻し)|journal reconcile が先に収束し、ops の回復はその approval 終端状態と `decided_by` を終端行へ追記する。決定を ops が再実行しない|既存 CAS、plugin flock、digest / kind 再照合で二重決定を防ぐ|
|approve / reject / retry|accepted 後、journal が決められない crash 点 (journal の preparing / versioned / recorded、journal 行なし等)|元要求を `outcome_unknown` とし、approval 状態・`decided_by` は approval GET で別に確認。決定を自動再送しない|同上|
|kill switch reset|accepted 後のいずれの crash 点|元要求を `outcome_unknown` とし、latched / generation / `reconcile_required` を status で別に確認。解除を自動再送しない。marker が残った場合は起動後ラッチ中として扱われる|期待 generation を伴う既存 CAS で判定。marker 残存時は `StateUncertain` で拒否され、行 11b でラッチ中に確定してから改めて reset する|
|kill switch reconcile|marker 書込み後、state replace 後、marker 削除後のいずれの crash 点|元要求を `outcome_unknown` とし、status の `reconcile_required` で別に確認。確定は常にラッチ側にしか倒れない|marker が残っていれば再度確定できる (冪等)。marker が無ければ 409 `invalid_state` で無変更|
|reflect retry|accepted 後、clear 前 / clear 後、終端 commit 前|key 付きなので元要求を `outcome_unknown` にして durable 状態も `outcome_unknown`。同じ key の再送は 409 の固定応答|別 key での再要求は試行記録の識別子を再照合し、clear 後に積まれた新しい試行記録があれば 409 `attempt_changed` で消さない|
|data resume|accepted 後のいずれの crash 点|元要求を `outcome_unknown` とし、data health を status で別に確認。resume を自動再送しない|現行 data 状態と acknowledge 条件を再評価|

`reflect retry` の冪等化は二重の保護を持つ。(1) `Idempotency-Key` により応答喪失後の同じ要求の再送は保存応答を返し、clear を繰り返さない。(2) 試行記録の識別子による条件付き clear により、clear 後に reflection 処理が新しい失敗試行を記録した後でも、古い識別子を持つ要求がその新しい記録を消さない。識別子は現物の台帳 `reflection_attempts` に既にある `attempts` と `last_attempt_at` の組を使い、新しい列は足さない。clear 後の最初の bump でも `last_attempt_at` が進むため、`attempts` が同じ値に戻っても区別できる。

`policy` は要求本文を idempotency request id 付きの canonical DB record として一度だけ保存し、`directives_path(root)` の file は、利用者が手で書いた既存行を保ったまま、record 由来の行だけをその順序付き record 集合から作り直し (record 由来の行は末尾に並ぶ)、一時 file + fsync + atomic replace + dir fsync で書き換える。`directives.md` は利用者方針チャネルであり、API の record 集合から全文再生成すると手書きの方針が消えるため。file append 自体を副作用の正にしないため、file 更新後・終端監査前に crash しても再送や復旧で同じ directive を二重追記しない。

activity は best-effort の人向け投影で、読み取り要求は DEBUG、変更要求は固定 event と audit id を記録する。認証失敗、peer 拒否、上限拒否は分類ごとに 1 分の最初の 5 件だけ個別記録し、30 秒周期、分境界、shutdown の 3 契機で suppressed 件数を flush する。`activity.log` 自体の無期限増加は `activity-log-grows-without-rotation` に残す。

自由文は `ops_requests`、job、activity、event に保存しない。例外は `ask` の回答本文で、これは ask の成果物そのものなので memory 上の job result にだけ置き (DB / activity / event には書かない)、`jobs.own` の所有者だけが job GET で読める。job の evict と再起動で消える。ただし既存運用との互換のため activity の `policy_added` だけは policy 先頭 200 文字を残す。人間の reject reason は既存の保存先 `approval_requests.reason` だけを明示的な例外とし、承認詳細で sanitizer 適用後に表示する。改善 agent へは reject reason 本文を渡さず、既存どおり固定文言の拒否状態だけを渡す。表示や境界を通る既存文字列の除染規律も維持する。

## 9. 関連チケットの扱い

|ticket|本設計での扱い|
|---|---|
|`policy-add-unwired-in-service`|main は `Commands(..., policy_path=directives_path(root))` を既に配線済み。既存として ops context に引き継ぎ、`build_app` 結合テストを完了条件にする|
|`secret-env-guard-false-positive`|main は完全一致 `service.secret_env_allowlist`、一致名だけの warning、backend 名と一致 pattern を含む error を既に実装済み。既存として daemon + API 起動結合で回帰確認する|
|`load-settings-loads-dotenv-as-side-effect`|本束に含める。設定の parse / validate を副作用なしにし、dotenv は必要な top-level だけで明示ロードする。`afx ctl` / `afx keys` はロードしない|
|`legacy-submit-corridor-bypasses-gate`|本束に含める。`afx plugin submit <name>` の legacy `approval.submit_plugin` 経路を全 kind で拒否し、materialize + `--from _human` の `switch.submit_candidate` だけを案内する。API が独自 gate の pending を決定できる回廊を残さない|
|`deployed-plugin-has-no-disable-or-rollback`|**本束の対象チケットではない** (範囲外、§1.4)。本束の endpoint に含めない。このチケットの設計時に通常退役、緊急 disable / rollback、依存表示、版 rollback の責務を決め、`retire-symlink-deployed-plugin` を統合する|

## 10. 不変条件

|ID|不変条件|
|---|---|
|I-1|発注・SL 変更・クローズ・risk gate 等の資金関連設定・`mode`・`autopilot`・サービス停止の endpoint は存在しない|
|I-2|全 endpoint は認証を通る。認証前に peer uid と子孫を検査し、不一致は本文を読まず拒否する|
|I-3|権限は endpoint scope と `authenticated_principal` の集合だけで決まり、本文で権限・`decided_by` は変わらない。`asserted_actor` は別欄とし、初版では常に null にする|
|I-4|`local_guard` は approver 以外へ付与できず、違反する principal 表では API を起動しない|
|I-5|server は鍵平文を保持せず、env / argv / 子 env / log / activity / response に鍵を出さない|
|I-6|plugin worker、改善 worker、gate pytest worker は鍵 dir 全体へ到達不能とし、違反時は該当 worker を起動しない|
|I-7|API thread は `core_lock` を取らず、API の失敗・停止は scheduler / supervisor / watchdog を止めない|
|I-8|API 起動失敗・途中死でも取引と資金保護 tick を続け、health / activity / notifier に記録する|
|I-9|ops と決定の 2 レーンは別 lock / connection を使い、両 lock を同時に取らない。shell と API は同じレーンを共有する|
|I-10|`app_state.json` の lock、一意 temp、file fsync、replace、dir fsync は main の既存 `StateStore` を唯一の更新経路とし、排他取得は deadline / stop-aware にする|
|I-11|kill switch reset は main の既存世代 CAS を使い、ラッチ中かつ期待世代一致時だけ書く。世代は false→true だけで増える|
|I-12|approve / retry は必須 digest と flock 内で再読した payload digest が一致した場合だけ進む|
|I-13|二重決定は既存 CAS と plugin flock で防ぎ、API は flock 内 outcome をそのまま job result に写す|
|I-14|失敗は固定 code / 固定文で返し、内部例外文を response に入れない|
|I-15|manual improve は 1 本、decide worker は 1 本、同じ approval の決定 job は 1 件。停止は受付停止、queued 終端化、running 収束、bounded join の順とする|
|I-16|変更前に `ops_requests.accepted` を durable commit し、失敗時は実行しない。表は append-only とし、reject reason は `approval_requests.reason` 以外の ops_requests / job / activity / event に保存しない|
|I-17|1 接続 1 要求で、接続数、要求全体時間、本文、tail scan、job 数に上限を持つ|
|I-18|公式 CLI は approver 操作を端末確認なしに送らず、stdin 非端末なら送らない|
|I-19|`local_guard` は UDS listener だけで routing し、approver 以外へ付与しない。同 uid の隔離されていない process から呼べることは保証外とする|
|I-20|`.ready` は鍵集合の commit marker とする。初回 init 以外を自動補完せず、不一致は fail closed、client は生成せず、手動管理は停止中だけ行う|
|I-21|子孫検査後に pidfd と peer pid を再確認し、不能なら拒否する。pidfd 非対応時は `decide` / `local_guard` を拒否する|
|I-22|公式 client は peer が同 uid の instance lock 保持者だと確認する前に鍵を送らない|
|I-23|決定 3 本は plugin kind だけを扱い、未知 kind / `live_trade` は受理時と実行時の両方で fail closed|
|I-24|必須 idempotency key は principal / endpoint / key ごとの durable 一方向状態を持ち、同一 digest 再送で副作用を繰り返さない|
|I-25|**廃止**。段階開放を表した不変条件だったが、実装着手条件を満たしてから全 endpoint を公開する裁定により不要|
|I-26|**次の束（`afx-ops` の設計）へ移管**|
|I-27|全ての待機は `min(設定値, caller deadline の残り時間)` を上限とし、停止合図を観測する|
|I-28|job 所有者は `(authenticated_principal, asserted_actor)` であり、初版の `asserted_actor` は null とする|
|I-29|event は固定 schema とし、永続化された event の配送は at-least-once、生成は best-effort とする。client は event id で冪等化し、生成失敗は監査と正の業務表でのみ復元する|
|I-30|鍵 rotate / revoke は 256 bit CSPRNG、`O_NOFOLLOW`、owner / mode 検査、atomic rename と fsync を用い、`.ready` commit 前後を fail closed にする|
|I-31|全変更要求の accepted-only 行は起動時に `outcome_unknown` 終端を追記し、副作用を自動再実行しない。ただし I-33 により、journal が既に終端を決めた決定要求はその結果を追記する|
|I-32|autopilot で制限される job (approve、retry、手動 improve) は実行開始の直前に StateStore flock 内で autopilot を再確認し、true なら `autopilot_restricted` で終端して無変更とする。autopilot 切替後に新しい変更は始まらない|
|I-33|起動時は既存 plugin 切替 journal の reconcile が先、ops の回復が後。決定の結果の正は `approval_requests` と切替 journal で、ops の回復はそれを追記するだけで決定を自動再実行せず、決められない要求だけを `outcome_unknown` にする|
|I-34|`killswitch reconcile` は `local_guard` で UDS 限定とし、ラッチ中への確定だけを行う。解除側へは倒さず、資金保護を弱めない。status は `reconcile_required` を返す|
|I-35|`reflect retry` は `Idempotency-Key` 必須かつ試行記録の識別子 (`attempts`、`last_attempt_at`) の一致を条件とし、不一致は 409 `attempt_changed` で無変更。後から積まれた試行記録を消さない|

## 11. 受入条件

|ID|観測できる条件|
|---|---|
|AC-1|実 UDS で 24 endpoint を全数実行し、期待する HTTP / error code を得る|
|AC-2|鍵なし / 不一致は 401、operator の decide / local_guard は 403、approver は成功する|
|AC-3|service 子 process は正しい鍵でも本文読取前に 403、activity と notifier に記録。二重 fork は既知の限界として通る再現 test を持つ|
|AC-3b|接続直後に peer が exit し pidfd が `Pid:-1`、または pid 不一致なら 403|
|AC-4|**前提側へ移管**。隔離は実装着手条件 (§1.2)、改善 worker の seccomp は運用上の有効化条件 (`api.enabled` 既定 false) として別束で受け入れ、本束では重複実装・重複検収しない|
|AC-5|token dir を隔離 allowlist 内、`/etc`、venv、`/proc` に置くと API を起動せず `token_dir_in_sandbox` を記録する|
|AC-6|0644 の鍵 principal は読み込まず 401、他 principal は動く|
|AC-7|起動後 env、`/proc/self/environ`、worker env、log、activity に鍵文字列が無い|
|AC-8|正しい approve / retry digest は決定し、不一致・欠落・受理後改変は pending のまま 409 / failed `payload_changed`|
|AC-9|API 同士、shell と API の同時 approve でも決定は 1 回で、他方は in-progress / already-decided|
|AC-10|既存世代 CAS で古い reset は 409、新ラッチは残る。latched 中の update(True) 100 回で世代不変、thread 1,000 回と別 process 200 回で世代違反・lost update・壊れた JSON が 0。非 latched reset は mtime 不変、旧 state の既定値と型違反を検査|
|AC-11|ask は 202→running→done。slot 満杯は job を作らず 409、期限超過は timeout|
|AC-12|manual improve は 202 と mission id、2 本目は 409。停止時は新規受付停止後に queued が shutdown となり、running は未取得 lock 待ちなら停止、commit 点を越えていれば終端まで収束してから thread / worker が bounded join される|
|AC-13|selected backlog の reject / reopen / note は 409 で行不変|
|AC-14|5 接続目 503、64 KiB 超 413、1 byte/秒の slowloris は accept から 5.5 秒以内に 408。slowloris 4 本中の通常 status は 503 または 5.5 秒以内。200 tick の遅延差は p95 +20 ms、p99 +50 ms、最大 500 ms 以内|
|AC-15|socket path に通常 file があっても daemon と tick は起動し `api_start_failed` を記録する|
|AC-16|古い socket file は安全に除去して bind できる|
|AC-17|500 response に例外文がなく、同じ incident の技術 log がある|
|AC-18|現行の `dispatch(` 回帰 165 箇所が、manual improve の起動文以外の既存文言を変えず緑|
|AC-19|socket 無し / 拒否 / 鍵無し / 401 / 403 / 503 / server 認証失敗で規定文面と rc|
|AC-20|stdin pipe の approve は `/dev/tty` があっても送らず rc=2。pty で正しい id の場合だけ送る|
|AC-21|API 起動、100 要求、停止の前後で open fd 数が同じ|
|AC-22|1 分 100 回の 401 は activity に 5 個別 + 1 集約、notifier 1 回 (`ops_requests` は認証済み要求だけを持つので未認証の拒否は書かない)。無通信でも分境界後 30 秒以内と shutdown 時に flushし、`00:00:59Z`→`00:01:00Z` を wall clock で pin|
|AC-23|autopilot=true で policy・approve・retry・`killswitch reset`・data resume・improve は 403、reject / ask / GET / jobs / `killswitch reconcile` は通る|
|AC-24|別 process の plugin flock 待ち中も status / backlog / killswitch reset は各 50 回で p95 100 ms、p99 300 ms、最大 1 秒以内。解放後 job は done。決定 20 回と core write 100 ms 間隔の双方向 SQLite busy は 0、core write 最大 1 秒。API 側 busy 注入は 503 `database_busy`|
|AC-25|digest 不一致 / 決定済み / 不在は同期 409 / 409 / 404 で job 無し。202 後切断でも job 完了し二重送信は in-progress|
|AC-26|決定 queue 9 件目は 503、8 件は FIFO|
|AC-27|初回は instance 別 dir 0700、2 鍵と `.ready` 0600。初回 init の途中 crash は欠落だけ補完し既存鍵の内容 / mtime は不変、`.ready` 後の欠落・digest 不一致・rotate / revoke の途中状態は自動補完せず API 起動失敗。稼働中 key 管理は無変更 rc=1。root 別に鍵を分離し、不正 token dir は無作成、client も無生成。service と manual init は同じ生成関数|
|AC-28|対話 `afx ctl>` は approver 操作ごとに毎回確認し、非端末の引数なしは usage rc=2|
|AC-29|`local_guard` は test TCP listener で 404、初版設定は TCP listener を作らない|
|AC-30|偽 UDS server、uid 不一致、dir 0755 には Authorization を送らず rc=3。本物の instance lock holder にだけ送る|
|AC-31|trade Claude argv の tools は `StructuredOutput,ToolSearch` と完全一致し、LocalRunner registry は read-only だけ|
|AC-32|live_trade / unknown kind の決定は API / shell、受理時 / 実行時の全てで unsupported、DB pending、job 無し|
|AC-33|plugin flock timeout は `min(設定値, job deadline 残り)` +0.5 秒以内に plugin_busy、approval / journal / version dir / symlink 不変。停止は 0.5 秒以内。依存 2 個目で待つ場合も取得済み 1 個目を解放し、bless / submit の既存 blocking は不変|
|AC-34|100 MiB log / activity でも 1 秒以内、read bytes 4 MiB 以下、巨大行 2,000 文字|
|AC-35|policy / backlog / ask / improve / reflect retry の同じ idempotency key / body は副作用 1 回かつ同じ応答 / job id、別 body は 409、key 無し 400、24 時間後は新規。accepted commit 後の各 crash 点から再起動すると accepted-only は 409 `outcome_unknown` に終端化され再実行しない。policy file は canonical record から再生成され、同じ directive は 1 行だけ|
|AC-36|変更系 15 endpoint (下記内訳) それぞれの成功 / 失敗は append-only `ops_requests` に accepted と終端が残り UPDATE / DELETE 0。accepted commit 失敗は副作用 0、activity 失敗でも要求成功。reject reason は ops_requests・job・activity・event の全列に 0 件で、既存 `approval_requests.reason` にだけ 1 件保存される。ask / backlog 自由文も 4 投影に 0 件、policy は明示した `policy_added` 例外だけ|
|AC-37|**廃止**。段階開放の 503 を検査する条件だったため削除し、全 endpoint の初期公開は AC-1 / AC-2 で検査する|
|AC-38|永続化済み event を同じ cursor から再取得して同じ id が配送され、client は二重表示せず、limit 上限を守る。high watermark 後退は cursor を進めず検知する。INSERT 障害では元操作を成功させ event を生成しない。連番だけでは生成失敗を断定せず、`ops_requests` と正の業務表の突き合わせでのみ復元できる。復旧後の `event_stream_health` も best-effort とし、元操作、長い待受、server 外向き接続は増やさない。全 code で schema 外 field と禁止内容を拒否する|
|AC-39|`load_settings` 単体と `afx ctl` / `afx keys` が `.env` と `os.environ` を変更しない。service top-level の明示 dotenv load は従来設定を読める|
|AC-40|legacy `afx plugin submit` は全 kind で gate 前に拒否し、`--from _human` だけが共有 gate を通る。legacy 経路で pending 行は増えない|
|AC-41|policy path と secret env allowlist の既存実装が `build_app` + API 起動結合で有効。誤検知 allowlist は完全一致だけ|
|AC-42|実 process の plugin worker、改善 worker、gate pytest worker は鍵 dir の全 file を読めない。利用者 shell と Landlock のない trade worker は読めることも確認し、信頼境界の記述と一致する|
|AC-43|全 endpoint を scope ごとに総当たりし、表で許可された `operator` / `approver` だけ成功する。未知 principal / scope、重複、approver 以外への `local_guard` を含む認可データでは起動しない。`asserted_actor` は全 CLI 要求で null、body で `decided_by` / `asserted_actor` を指定しても変わらず、`approvals.detail` のない scope 集合は詳細を取得できない|
|AC-44|**次の束（`afx-ops` の設計）へ移管**|
|AC-45|ops lock と StateStore flock を要求 deadline より長く保持しても 5.5 秒以内に timeout し、stop 中は 0.5 秒以内に待機を抜ける。受付停止→queued 終端→running 収束→join の順序を barrier で観測する|
|AC-46|rotate / revoke の各 fsync / rename 前後へ crash を注入し、旧 token の再有効化、symlink 追従、owner / mode 不正の受理が 0。rotate 後は新 token だけ成功、revoke 後は当該 principal が常に 401、`.ready` 不一致では API を起動しない|
|AC-47|**次の束（`afx-ops` の設計）へ移管**|
|AC-48|全変更 endpoint について accepted commit 前 / 後、副作用中 / 後、終端 commit 後へ crash を注入する。未終端 accepted は listener 開放前に 1 件の終端 (journal が決めたものはその結果、それ以外は `outcome_unknown`) が追記され再実行されない。approve / reject / retry は approval GET と CAS、kill switch reset は status (`reconcile_required` を含む) と generation CAS、kill switch reconcile は status、data resume は data health で別に現状態を確認でき、いずれも監査結果を推測で success にしない。plugin journal の各 phase (preparing / versioned / recorded / switched) と crash 点の組を全数で試す|
|AC-49|autopilot=false で受理され queued の approve / retry / improve job が、autopilot=true への切替後に実行開始を迎えると `autopilot_restricted` で終端し approval・journal・symlink・mission は不変。開始判定が先に通った job は定義済みの終端まで収束し、切替後に新しい変更を始めない。切替と開始判定の両順序を queue barrier で挿入して観測する|
|AC-50|起動時、plugin 切替 journal の reconcile が ops の回復より先に走る (順序を観測する)。journal `switched` 完遂後 crash の approve は `system_reconcile` で完遂し、ops は元要求の終端行へその結果を追記する (`outcome_unknown` にしない)。journal が決められない要求は `outcome_unknown` となり、ops の回復が決定を自動再実行しない|
|AC-51|kill switch reset の途中 (marker 書込み後、state replace 後、marker 削除後) で落ちた後、`GET /v1/status` が `reconcile_required` と marker 内容を返し、同じ世代の reset は拒否され、`POST /v1/killswitch/reconcile` でラッチ中に確定でき、その後に世代付き reset で解除できる。確定は状態を解除側へ倒さず、取引は確定前後とも止まったまま。marker が無いときは 409 `invalid_state` で無変更。autopilot=true でも reconcile は通り、reset は 403。TCP listener と operator では 404 / 403|
|AC-52|reflect retry は key 無しで 400、識別子の欠落で 400。同じ key / 識別子の再送は 1 回しか clear せず同じ応答を返す。clear 後に新しい試行が bump された状態で古い識別子と別 key を送ると 409 `attempt_changed` で新しい記録は残る。`GET /v1/reflections/{order_id}` は `last_reason` を返さない|
|AC-53|`afx ctl killswitch reconcile` は autopilot 中も送れ、 marker 内容とファイル上のラッチ値を表示して y/N を求め、stdin 非端末では送らず rc=2。承認後の確定結果を表示し、解除は `killswitch reset <世代>` を別に案内する|

## 12. テスト方針

- 実 UDS、実 server thread、httpx UDS transport を主にし、routing / peer credential / fd / deadline を fake HTTP だけで済ませない。
- pid、全 flock、deadline / stop-aware StateStore、SQLite 双方向、shutdown 順序、Landlock 鍵到達性は実 process と barrier で検査する。端末確認は実 process + pty を使う。
- wall clock（監査時刻、集約、event 時刻、idempotency 24 時間）と monotonic（request、lock、job timeout）を別注入し、片方を進めても他方が動かないことを pin する。
- 実 home、実 `data/`、実 socket、実鍵へ触れないガードを conftest に置く。全資源は `tmp_path` を使う。
- 変異対象は scope 表、`authenticated_principal` / `asserted_actor` 分離、server 認証、digest の二地点照合、kind 二地点検査、世代 CAS 呼出し、2 レーン、全待機 deadline、停止順序、append-only 監査、全変更要求の accepted-only 回復、冪等状態遷移、policy 再生成、`.ready` commit、rotate / revoke、dotenv 非副作用、legacy corridor 拒否、event 固定 schema / 永続化後の冪等配送 / best-effort 生成、表示 sanitizer とする。
- 性能閾値は稼働中相当 CPU 負荷でも測る。bless の合成履歴 33,984 本では indicator 約 1.3〜1.4 秒、strategy 約 27〜29 秒だったが、API approve は gate を再実行しない。長い待ちの理由は外部 process の plugin flock である。

## 13. 実装 task

|task|範囲|所有ファイル|依存|完了条件|並列|
|---|---|---|---|---|---|
|T1 config / corridor|dotenv 明示化、legacy submit 拒否、既存 policy / secret env 配線と trade tool pin の結合確認|`config.py`、service / CLI entry、`plugin/approval.py`、`backtest/cli.py`、tests|着手条件 (隔離投入)|AC-31、39〜41|T2 と可（所有分離）|
|T2 ops core|schema、error、2 レーン、deadline-aware StateStore / approval facts 利用、backlog 条件更新、bounded flock、job、全変更要求の crash 回復を含む監査・冪等状態機械、event 投影|`ops/`、`store/db.py`、`store/state.py`、`store/backlog.py`、`plugin/switch.py`、`core/improve_supervisor.py`、`commands.py`、tests|着手条件 (隔離投入)、V-4 (T2 内の最初の gate として消化し、30 秒・AC-14・AC-24 の閾値を確定してから deadline / queue 容量 / flock 待機を固定する)|AC-8〜13、23〜26、32〜38、45、48 (journal 結果の追記と reflect 冪等の部分)、49、50 (回復規則)、52|T1 と可、内部は直列|
|T3 keys / server|鍵 lifecycle、rotate / revoke、設定、UDS listener、peer / auth / scope、上限、起動停止と service 配線|`ops/keys.py`、`ops/api_server.py`、`service.py`、`config/settings.yaml.example`、tests|T1、T2 (`api.enabled` 既定 false、§1.2)|AC-1〜3b、5〜7、14〜17、21〜22、27、29、43、46、50 (起動順序)、51|不可|
|T4 CLI client|`afx ctl`、対話、server 認証、確認、sanitizer、job / event polling、key command|`ops/client.py`、entry、tests|T3、V-10|AC-18〜20、28、30、53|不可|
|T5 integration / docs|worker の Landlock 鍵隔離、実 process、pty、負荷、鍵漏洩 runbook、運用文書。「承認後の回復手段は未整備」を明記|tests、運用 docs|T4|本束に残る AC 全件（移管済み AC-4、44、47 を除く）、特に AC-42、48 (journal phase × crash 点、reset / reconcile / reflect retry の crash 点の実 process 試験)、49 (mode 遷移)|不可|

## 14. 残余リスク

|点|扱い|
|---|---|
|同 uid の偽 server|本物停止中に attacker が instance lock も取る場合は client 認証を突破できる。別 uid 化までは残る|
|子孫判定|二重 fork / daemon 化で外れる。隔離と鍵を主防御にする|
|端末確認|公式 client の誤操作防止であり、鍵を持つ process や pty 自動化への人間性証明ではない|
|承認内容|表示 payload の一致は保証するが、人が読んだことは保証しない|
|鍵 rotate|停止まで旧鍵が有効。online reload は管理口を増やすため作らず、漏洩時は §3.2 の停止交換手順を使う|
|plugin 決定|外部 flock が 30 秒を超えると plugin_busy となり手動再試行が要る|
|承認後の回復|`deployed-plugin-has-no-disable-or-rollback`。初版には disable / rollback がなく、運用文書にも未整備と明記する|
|通常退役と緊急回復|`deployed-plugin-has-no-disable-or-rollback` の設計で責務を決め、`retire-symlink-deployed-plugin` を統合する。初版では未整備|
|同 uid の非隔離 process|利用者 shell と trade worker は信頼境界内で鍵を読める。trade argv / tool registry pin を守り、trade worker Landlock は別チケット|
|job|memory 上なので再起動で消える。受理 / 終端は DB、mission は missions 表に残り、未終端 accepted は `outcome_unknown` になる|
|通知 event|永続化後の配送は重複し得て、生成は best-effort のため DB INSERT 障害中に欠落し得る。event id の冪等化を使い、欠落は `ops_requests` と正の業務表の突合でのみ復元する|
|log 増加|`ops_requests` と `ops_events` は初版では無期限。`activity.log` rotation は `activity-log-grows-without-rotation`。DB 増加は件数監視し、保持変更は cursor 契約と一緒に別設計する|

## 15. 未決事項

|ID|未決|実装前の扱い|
|---|---|---|
|V-4|**実測済 (2026-10-05、`tmp/impl-ops/v4/result.md`)**: approve 無競合 p99 22 ms、flock 待機 = 保持 + 約 20 ms、core write 競合で SQLite busy 0、tick 差 p99 +0.01 ms|同期 deadline 5 秒と `plugin_busy` 30 秒を維持、決定 job deadline は 35 秒以上、AC-14 / AC-24 の閾値は維持|
|V-10|`/proc/locks` から instance lock holder を一意取得できるか|T4 前に実測。不能なら弱い pid file fallback ではなく client 認証方式を再設計する|

`ops_requests` は無期限、idempotency key は 24 時間、trade worker は信頼境界内、鍵 `.ready` の意味、全 endpoint の初期公開、全変更要求の accepted-only 回復、event の保証境界は裁定済みであり未決ではない。外部 client の principal 追加と `enroll` は本書の未決ではなく次の `afx-ops` 束へ移した。旧 R-1 は未決から外し、チケット `deployed-plugin-has-no-disable-or-rollback` の設計時に通常退役と緊急 disable / rollback の責務を決め、`retire-symlink-deployed-plugin` をそこへ統合する。

## 16. 設計レビューの経過

C0 v0.3 の設計レビューで、plugin worker 隔離、世代 CAS、kind fail-closed、server 認証、有界 flock、digest、監査 DB、slowloris、SQLite 逆方向競合を設計へ取り込んだ。2026-10-04 の裁定で段階開放を廃止し、鍵 `.ready`、trade argv pin、回復と rotation の別 ticket を確定して v1.0 とした。同日の設計レビュー r2（Critical 1 / High 5 / Medium 3 / Low 1）では実装着手不可と判定され、scope 細分、監査主体分離、有界待機、crash 回復、reject reason 例外、event 契約、鍵 lifecycle、未定義語の是正を取り込んで v1.1 とした。設計レビュー r3（Critical 0 / High 4 / Medium 3）では、High 4 件が全て `afx-ops` 契約に由来したため、裁定によりその契約と principal 追加を次の束へ切り離した。Medium 5 は全変更要求の accepted-only 回復へ広げ、Medium 6 は event 保証を「永続化後の配送は at-least-once、生成は best-effort」へ限定して v1.2 とした。設計レビュー r4（Critical 0 / High 0 / Medium 4 / Low 1）で収束と判定され、実装着手可となった。Medium 4 件 (queued job の開始時 autopilot 再確認、起動時の journal reconcile と ops 回復の順序、`killswitch reconcile` の API / CLI 化、`reflect retry` の冪等化) と Low 1 件 (対象チケットの整理) を裁定どおり取り込んで v1.3 とした。

## 17. 変更履歴

|日付|版|変更|
|---|---|---|
|2026-10-03|C0 v0.3|設計レビュー反映済み草稿|
|2026-10-04|v1.0|最終裁定、3 client 契約、systemd 連動、関連 ticket、main 既存実装を反映して清書|
|2026-10-04|v1.1|設計レビュー r2 と裁定を反映。`afx-ops` 分離、scope、監査主体、deadline、冪等回復、event、鍵 rotate / revoke、受入条件と task gate を改訂|
|2026-10-04|v1.2|設計レビュー r3 と裁定を反映。`afx-ops` / Discord cog 契約を次束へ移し、初版 principal を2つに限定。全変更要求の crash 回復と event の保証境界を改訂|
|2026-10-04|v1.3|設計レビュー r4 (収束) と裁定を反映。queued job の開始時 autopilot 再確認、起動順序 (journal reconcile → ops 回復)、`killswitch reconcile` と reflection GET の追加 (endpoint 24 本)、`reflect retry` の冪等 key と試行識別子、対象チケットの整理、I-32〜35、AC-49〜53。`killswitch reconcile` は autopilot 中も許可、個数 (24 / 変更系 15 / 冪等 key 5 / `local_guard` 3) を数え直して一致|
|2026-10-05|v1.4|着手条件を改訂。脱出実測を廃し (ユーザー裁定 案 1)、改善 mission worker の seccomp 投入を T3 の着手ゲートに移した。T1・T2 は隔離投入のみを条件に着手可。V-4 の計測内容を具体化 (root 一式の隔離複製、3 条件 × 20 回、tick 差の同時観測)。sol advise 2026-10-05|
|2026-10-05|v1.5|改善 worker の seccomp を T3 の着手ゲートから運用上の有効化条件へ (ユーザー裁定 案 2)。seccomp 投入まで `api.enabled` 既定 false|
|2026-10-05|v1.6|policy file の再生成を「record 由来の行だけを作り直し、手書き行は保つ」に (T2 実装時の逸脱を採用。全文再生成は利用者の手書き方針を消す)|
|2026-10-06|v1.7|ask の回答本文は memory 上の job result にだけ置く例外を明文化 (実装レビュー r1 OPS-R1-02 の裁定: spec の欠落)|
|2026-10-06|v1.8|AC-22 の未認証拒否の記録先を activity + notifier に限定 (`ops_requests` は認証済み要求のみ。T3 実装時の契約の穴)|

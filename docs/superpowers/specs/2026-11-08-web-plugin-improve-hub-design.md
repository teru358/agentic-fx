# 同梱 web の「plugin と改善」画面群 (web-plugin-improve-hub) 設計 C2.2

**チケット:** `web-plugin-improve-hub` (起票予定、高、機能)。段 B2 (TLS) と段 C (モデル選択) は別チケットにできる。関連: `improve-run-summary-for-humans` (要約の API を読む)、`web-ui-charts-equity-indicators` (チャートは後)
**状態:** C2.2 (2026-11-08)、§3 は利用者が決定。sol 1 周目・opus 2 周目・sol 3 周目と利用者の裁定 (書込みは loopback か TLS) を反映

## 0. 何が起きているか

同梱 web (afx-ops `web`) は 5 画面 (状態・承認一覧・承認詳細・ログ・activity) で、読取専用・JS なし。承認一覧は pending を並べるだけで、plugin がどう配備され、どの strategy がどの indicator を使っているか、改善が何をしているかは見えない。承認と改善の実行は Discord と CLI でしかできない。

利用者の要件 (2026-11-08): 今の approvals を組み替え、plugin と改善の関係を 1 か所にまとめる。

- plugin の一覧
- strategy と indicator の依存関係
- 手動の改善の実行
- 新規 indicator・strategy の承認・却下
- 可能なら、改善に使うモデルの選択

JS と Node.js の使用は許可された (2026-11-08)。11-01 の方針として、indicator が「どの strategy から参照されているか」を一覧に出すこと、indicator の有用性を事後に測る数字を併せて入れることも決まっている。

今の作り (file:line は `tmp/review-webhub/explore.md`、レビューの原文は `c0-sol-out.md`・`c1-r2-out.md`。A = afx-ops/afx_ops、S = src/agentic_fx):

- **web の権限と隔離**
  - principal は `ops-web` で、読取の scope だけを持つ。元の afx-ops 設計は「web が侵害されても決定権に届かない」ことを境界にしている (`docs/superpowers/specs/2026-10-09-afx-ops-design.md:100`)。
  - 隔離の土台は「1 機能 1 principal」で固定されている (`A/isolation/startup.py:187-195`、`:863-877`、`:313-317`、`S/ops_setup/stages.py:30-34`)。
  - web の secret dir に置けるのは `web.password` だけで、ほかの file があると起動が止まる (`startup.py:177-181`、`:797-798`)。
  - 隔離後の web には `/etc/ssl` の規則が無い (`startup.py:469-470`)。
  - web は 1 process で、thread は 4 本 (`A/web/runtime.py:45`、`:467`)。
- **要求の受け方**
  - GET / HEAD 以外は 405。CSRF の対策は無い。
  - CSP は `form-action 'none'`。`Referrer-Policy: no-referrer` を付けている。
  - 認証は Basic (任意)。TLS は無い。
- **承認と監査**
  - 承認 (`DECIDE`) は bot と approver が持ち、pidfd で検証した相手 (`SO_PEERPIDFD`) だけに許す。
  - daemon が `payload_sha256` を照合するのは approve と retry だけで、reject は照合しない (`S/ops/service.py:1963`)。Discord は reject の直前に client 側で詳細を照合する。
  - `decided_by` は principal から作る (`api:<principal>`)。actor は別欄の `asserted_actor` に置く。
  - actor を付けられるのは ops-bot だけ。形は `discord:<id>`、28 byte まで (`S/ops/contracts.py:107-109`、`A/api_client.py:132`)。
- **改善の実行**
  - `POST /v1/improve/runs` の scope は `OPERATE` (ask・backlog・policy も含む粗い scope)。route は scope を 1 つだけ持つ (`S/ops/api_server.py:275-285`)。
  - 改善の開始は actor を渡さずに job を作る。冪等は (principal, endpoint, key) ごとで、本文の digest を照合する (`S/ops/audit.py:79-90`)。
- **鍵**
  - 鍵の enroll には afx の停止が要る (`S/ops_setup/reconfigure.py:35`)。reconfigure は鍵に触れない。
  - afx-ops の `keys.py` は、manifest の principal の集合が既知の集合に含まれることを要求する (`A/keys.py:26`、`:129`)。
- **plugin の一覧と依存**
  - API は無い。CLI の `afx plugin list` は表示だけで、signal を unknown に寄せる。
  - resolver (`resolve_indicator_deps`) は、最初に失敗した alias で例外を投げ、alias ごとの状態を返さない (`S/plugin/resolve.py:194-241`)。
  - 再評価の依頼も `indicator_deps` を持つ (`S/plugin/switch.py:3185-3192`)。
- **改善のモデル**
  - 起動時の CLI 検査 (bin の絶対 path 化、opencode の検証済み、環境変数の秘密) は、設定済みの backend だけに掛かる (`S/service.py:400-533`)。
  - `ImproveLoop` は daemon の寿命の間 1 つの `self._settings` を持ち、47 箇所で読む。
  - `choose_for_mission(settings)` と worker の handshake は、settings を引数で受ける。

## 1. 何を作るか (動作)

4 つの段に分ける。段ごとに配備できる。

| 段 | 中身 | 利用者の LAN の平文 web で使えるか |
|---|---|---|
| A | 読む (plugin の一覧・依存・承認・改善の要約) | 使える |
| B1 | 書く (承認・却下・改善の実行)。loopback の bind に限る | 使えない (同じ機械のブラウザ、または SSH 転送で使う) |
| B2 | web を HTTPS で動かし、LAN からも書けるようにする | HTTPS にすれば使える |
| C | 改善のモデルを選ぶ | B1 / B2 の書込みと CLI で使う |

### 段 A: 読む (読取専用のまま)

ナビの「Approvals」を「Plugins」に替え、その下に次の画面を置く。

| 画面 | 中身 |
|---|---|
| `/plugins` | plugin の一覧 (下の read model)。kind、状態の 3 つの軸、配備中の版 (短い hash)、承認待ちの件数。indicator には参照している strategy と、有用性の数字。 |
| `/plugins/{name}` | 1 つの plugin。strategy なら使う indicator ごとの解決の状態。indicator なら参照する strategy。版の履歴、承認待ち・過去の承認へのリンク、この plugin を対象にした改善の試行。 |
| `/approvals`、`/approvals/{id}` | 今の承認一覧・詳細。`/plugins` から辿れる位置に移し、内容は変えない。 |
| `/improve`、`/improve/{id}` | 改善 mission の一覧と要約。`improve-run-summary-for-humans` の API を読む (その束が入ってから作る)。 |

- `{name}` は loader の名前の文法 (`^[a-z][a-z0-9_]{0,63}$`) で照合し、decode しない。`{id}` は今の数字 id と同じ照合にする。

**plugin の read model** (daemon 側に新設し、CLI の `afx plugin list` と共有する):

- **形**: 入力 (FS の entry、DB の行、settings) を受け取る純関数にする。daemon も CLI も、それぞれ入力を集めて同じ関数を呼ぶ。CLI は settings を読んで渡す。
- **名前の列挙元**: 次の和集合。
  - `plugins/` の entry
  - `.versions/<name>`
  - `approval_requests` (kind=plugin) の name
  - `plugin_deploy_ops` / `plugin_switch_journal` の name
  - 完了した disable の後に残る名前も含める。
- **kind**: strategy / indicator / signal / 不明 の 4 つ。CLI の表示もこれに揃える (今は signal を unknown に寄せている)。
- **状態**: 3 つの独立した軸で持ち、1 つにまとめない。
  - 配備実体: symlink の版 / plain / 無し / 読めない。
  - 承認と読込: 承認済みで読込可 / 承認済みだが読込除外 / 未承認。読込除外の理由は固定語に写し、loader の自由文 (例外の文面など) を出さない。
  - 承認待ち: 件数。
  - 切替の途中の状態は open_ops として出すだけで、lock を取らない。
- **依存**: strategy の `indicators:` の各 alias について、例外を投げずに状態を列挙する関数を新設する。
  - 照合する inventory は、承認済みの第 1 相 (`approved_plugins` と同じ)。
  - 状態は resolver と同じ語: 一致 / `pin_mismatch` / `not_found` / `not_indicator` / `unpinned` / パラメータ不正など。知らない状態は「不明」と出し、「一致」とは出さない。
  - loader の admit (今の例外の経路) は変えない。
- **有用性の数字** (初版は 2 つ):
  - 数える対象は、kind=strategy の新規提出の依頼 (改善の提出と CLI の submit / bless) に限る。再評価の依頼は除く。依頼単位で数え、同じ indicator を 2 つの alias で参照しても 1 件とする。
  - 提出件数: 対象の依頼のうち、その indicator を `indicator_deps` に持つ件数。名前ごとに数え、pin (content_hash) の違いは内訳に出す。
  - 配備成功件数: 対象の依頼のうち、その依頼の決定で `plugin_switch_journal` が `decided` まで進んだ件数。
  - `indicator_deps` を持たない古い strategy の依頼は、0 件ではなく「不明」として別に数える。
  - 床を通った回数 (非提出の試行を含む) は、要約の束で依存と判定が永続化されてから足す。
- **費用**: 要求のたびに全 plugin の content hash を計算しない。配備の世代 (live の状態と承認の行の最終更新) を key にした memo を持ち、変わったときだけ作り直す。

**API:** `GET /v1/plugins` (一覧)、`GET /v1/plugins/{name}` (詳細)。権限は `APPROVALS_LIST` / `APPROVALS_DETAIL` (ops-web が今持っている scope で足りる)。web は DB を読まない。

依存は表で出す。図は段 A の後に足す (§3 の裁定 3)。

### 段 B1: 書く (承認・却下・手動の改善。loopback に限る)

**書込みを許す条件 (利用者の裁定、2026-11-08):**
- 書込みは、web の bind が loopback か、web が HTTPS (段 B2) のときだけ許す。LAN の平文 HTTP では書けない。
- `ops.toml` の `[web]` に真偽値 `write` を足す。path や principal を設定から指す口は作らない。
- 起動時の検査 (`check_web_access`、`afx-ops probe web` も同じ関数を使う) で、次の構成を落とす。
  - `write = true` かつ `auth = none`
  - `write = true` かつ bind が loopback でなく、TLS (B2) でもない

**鍵と principal (1 process 1 鍵を保つ):**
- 書込み用の principal `ops-web-write` を足す。scope は `DECIDE`、`IMPROVE_RUN`、`JOBS_OWN`、読取の scope。
- 書込みが有効な web は `ops-web-write` の鍵だけを読み、読取にもその鍵を使う。`PRINCIPALS["web"]` は、検証済みの `write` から 2 つの固定値のどちらかを選ぶ。
- 書込みが無効なら、web は `ops-web` の鍵だけを読み、`ops-web-write` の鍵を読まない。隔離の後、web は他の principal の鍵を読めない。
- 有効化の手順: `afx ops web enable --write` (無効化は `--no-write`) を足す。
  1. 構成を検査する (loopback か TLS、`auth = basic`)。満たさなければ鍵に触れない。
  2. 目標の状態 (write の真偽と、使う principal) を、再開できる stage に記録する。既存の stage が `enabled` でも、目標の write が違えば切替として進める。
  3. afx を止める (今の鍵の enroll と同じ。ペーパー運用中なら一度止まることを画面で告げ、確認を取る)。
  4. 鍵を目標に合わせる。`ops-web-write` が未登録なら enroll、active なら照合、revoked なら rotate (今の鍵の状態機械は revoked の enroll を拒否するため)。`--no-write` なら `ops-web-write` を revoke し、戻る先の `ops-web` の鍵が active であることを確かめる。
  5. `ops.toml` の `write` を書く。
  6. afx と web を起動し直す。もともと afx が止まっていたなら、afx は起動しない (今の enable と同じ扱い)。
  - 途中で止まったら、記録した stage から再開する。鍵を変えた後・設定を書く前に止まっても、再実行で目標の状態に収束する。
  - 隔離側の `PRINCIPALS`・`read_api_token`・`client_for` は、同じ検証済みの write から同じ principal を選ぶ。
- 鍵の manifest の互換: afx-ops の `keys.py` を、自分の entry だけを検証し、知らない principal を無視する形に直す。これを先に配備する (古い afx-ops が新しい manifest で `ready_invalid` にならないよう、配備の順を運用手順に書く)。
- 残る危険: 書込みを有効にした web が侵害されると、承認の権限も渡る。元の境界「web が侵害されても決定権に届かない」は、書込みを有効にした構成では成り立たない。

**scope `IMPROVE_RUN`:**
- `POST /v1/improve/runs` だけを許す scope にする。route はこれ 1 つを要求する (any-of の認可は作らない)。
- operator と approver にも `IMPROVE_RUN` を足す (今どおり CLI から改善を始められる)。
- job の閲覧・取消は `JOBS_OWN` のまま。

**監査:**
- `decided_by` は今どおり principal から作る (`api:ops-web-write`)。
- actor は別欄 (`asserted_actor`) に `web:<設定の Basic ユーザー名>` で残す。
  - actor の規則を principal ごとの表にする。ops-bot は今の `discord:<id>` (28 byte)、ops-web-write は `web:` + ユーザー名の規則 (最大 32 byte、合わせて 36 byte)。
  - afx-ops の client の actor 検査 (`_check_actor_text`) も同じ表に合わせる。
  - HTTP の入力からは actor を指定させない。web が認証の結果から付ける。
- 改善の開始にも actor を渡し、job に保存する。job の閲覧・取消は、同じ principal と actor の組に限る。
- 承認者は 1 人の扱い (Basic の資格情報を共有する人は区別できない)。監査上の制約として了解したうえで使う (§3 の裁定 2)。

**確認の 2 段:**
- 「承認」「却下」「改善を実行」の POST (1 回目) で、web が確認 record を作り、確認画面を返す。1 回目の POST にも、下の要求の検査 1〜3・5 を掛ける。
- record に結び付けるもの:
  - 認証済みのユーザー
  - action (approve / reject / improve)
  - 対象 (承認 id と完全な `payload_sha256`、または改善の正規化した条件: 期間・回数・コメント・段 C の `runner_choice`)
  - 却下の理由
  - 使い捨ての token
  - 期限 (10 分)
- record は web の memory だけに置く。token と record を disk に書かない。web が再起動すれば消え、利用者は確認をやり直す。
- record の状態: 未使用 → 実行中 → 結果判明 / 結果不明 (daemon の応答を失った)。
- 未使用の record は 32 件まで。期限切れを捨てても満杯なら、33 件目の作成を固定の 4xx で拒否する (既存の record と token はそのまま)。
- 消費済みの record (実行中・結果判明・結果不明) は 1 時間・32 件まで保持し、結果の回復に使う。
- 最終の POST (2 回目) は token だけを送る。中身は record に保存したものを使い、POST の本文では変えられない。
- token の消費は `threading.Lock` で原子的に行い、1 回しか使えない。並行して 2 回送っても、実行は 1 回。
- 中身の照合:
  - 承認と retry: daemon が `payload_sha256` を照合する (今の仕組み)。
  - 却下: web が実行の直前に詳細を取り直して `payload_sha256` を照合し、違えば実行しない (Discord の perform と同じ水準)。保証するのは「直前に取り直した時点までの変化の検出」で、その後に決定されるまでの間の変化は検出しない (daemon の reject は digest を照合しないため。残余)。
- 応答を失ったとき:
  - 承認・却下は、承認詳細の状態で結果を確かめられる。
  - 改善は、token から作った値を `Idempotency-Key` にして送る。`runner_choice` は監査の本文に入るので、同じ key で別の条件を送れば衝突として検出される。
  - 結果不明の record には、確認画面から「結果を確かめる」(POST、同じ認証ユーザーだけ、要求の検査 1〜3・5 を掛ける) を出す。web は record に保存した本文と key だけを replay し、新しい操作として token を消費し直さない。daemon の冪等の仕組みが同じ job id を返すので、job は増えない。
- web から retry は出さない。`DECIDE` は API の上では retry も許すので、それは残余として記す。

**要求の検査** (書込みの POST は、すべてを満たしたときだけ受ける。どれかが欠ければ 4xx で、daemon を呼ばない):
1. `write = true` で、経路が loopback か HTTPS。
2. Basic で認証済み。
3. `Origin` が、設定と検査済みの Host から組み立てた web 自身の origin (scheme を含む。既定の port を省いた形も扱う) と完全に一致する。`Origin` が無い・`null`・重複・不正値は拒否する (`Referrer-Policy: no-referrer` のため Referer は見ない)。
4. token が有効で、まだ使われておらず、認証済みのユーザーと一致する (2 回目の POST)。
5. 対象外の method・path は今どおり 405 / 404。

**その他:**
- CSP は `form-action 'self'` に緩める。
- autopilot 中の制限 (`autopilot_restricted`) は今と同じに効く。
- 承認・却下の対象は今と同じ (`kind=plugin`)。web から disable・rollback・retire はしない。
- web から入れた却下理由は、今どおり `approval_requests.reason` にだけ残る。改善の prompt には流れない (固定文言 `rejected_by_human`)。

### 段 B2: HTTPS (LAN から書くため)

段 B1 の後に、別チケットにできる。

- 鍵と証明書は、web の secret dir に固定名で置く (`web.tls.key`・`web.tls.crt`、0600、自 uid、O_NOFOLLOW、`web.password` と同じ型の検査)。`secret_allowed` に足す。path の設定は作らない。
- `ops.toml` の `[web]` に真偽値 `tls` を足す。
- 隔離の前 (`isolate()` の段 1、secret を読むのと同じ位置) で鍵と証明書を読み、`SSLContext` を作る。OpenSSL の設定の読込みもここで済ませる。作った context は `Isolated` で web の本体へ渡す。`ssl` の import は preload に入れる。
- 接続は `connect_accepted_socket(..., ssl=ctx, ssl_handshake_timeout=<接続の期限以下>)` で受ける。handshake の間も接続は今の枠 (16 本) に数え、handshake の失敗・timeout・取消しのときに必ず枠を戻す。
- TLS の下で seccomp を trace し直し、表と hash を更新する。`use_send_only_transport` が SSL の transport の下でも効くかを確かめる。
- 自己署名の証明書を作る CLI (`afx ops web tls init`) を用意する。指紋を表示し、利用者がブラウザの初回の警告で照合できるようにする。
- 起動時の検査 (`check_web_access`) に、TLS の組み合わせを足す。証明書と鍵の不一致・mode 違反・不在は、固定の reason で落とす。

### 段 C: 改善のモデルを選ぶ

- **候補の置き場**: settings の `runner.improve_choices` に、名前付きの候補 (`name`・`backend`・`model`、opencode なら `context_limit`) を並べる。
  - 既定は、今の `runner.improve` を `default` という名前で 1 件持つ。
  - settings.example にも同じ形を足し、settings の検証で全候補を検査する。
- **起動時の検査**: 候補ごとに、今の起動時の CLI 検査 (`_check_cli_backend_steps`: bin の絶対 path 化、環境変数の秘密の検査) を通す。解決済みの候補の snapshot を daemon の起動時に作って持つ。
- **opencode の候補**: 検証を候補ごとに行う (`afx check backend --choice <name>`、検証済みを候補ごとに記録する)。それを実装するまでは、opencode の候補は `default` だけに限る。
- **選び方**: `POST /v1/improve/runs` に `runner_choice` (候補の名前) を足す。
  - 名前は、要約の設計で使う `choice` (agent の終端の選択) と分ける。
  - daemon は候補に無い名前を拒否する。
  - web と CLI (`afx ctl improve --choice <name>`) は同じ口を使う。
  - 候補の一覧 (名前・backend・model) を読む API を足す (`GET /v1/improve/choices`、`APPROVALS_LIST`)。
- **受け付けから実行までの運び方**:
  - API が受け付けた時点で候補を解決し、確定した snapshot を job の action に入れる。
  - job の action → supervisor (`submit_manual` / `submit_repeat` に引数を足す) → `_execute_mission` → `prepare` へ渡す。
  - 週次の実行は `default` を渡す。繰り返しは、開始時に確定した同じ snapshot を全回に渡す。
  - `prepare` は ctx を作る前に mission の runner / model を記録するので、記録にもこの snapshot を使う。
  - 実行専用の Settings (`runner.improve` を候補で置き換えたもの) は `prepare` で 1 回組み立て、ctx に載せる。admission (`choose_for_mission`) と worker の handshake には、この Settings を渡す。
- **実行への渡し方**: 選んだ候補の snapshot を `ImproveRunContext` に載せる。
  - 次のすべてが ctx から読む: `_build_worker_runner`、mission の記録 (`runner` / `model`)、状態行、admission (`choose_for_mission`)、canary・selftest、worker の handshake、繰り返し実行の各回。
  - 実行の経路で `self._settings.runner.improve` を読まないことを、AST か grep の test で固定する。
  - 週次と手動が同時に走っても (`improve.parallel > 1`)、共有の object を差し替えない。
- **既存の判定を迂回しない**: 選んでも、admission と runner の fingerprint の判定は今のまま効く。候補を settings に足すことは、fingerprint の許可集合に登録することとは別。
- 選んだ候補の名前は、改善の要約に残す。要約の設計に「使った候補」の欄を足す (要約の version を上げる。要約の束の変更として扱う)。
- settings.yaml を web から書き換えない。候補を足すのは、利用者が settings を編集して行う。

## 2. JS とビルド

- 段 A・B は、server 側で HTML を組み立て、form の POST で操作する。JS が無くても全部使える。
- 段 A・B には JS を入れない。Node の導入は、依存の図を足す段まで延ばす (§3 の裁定 3)。
- JS を入れるときの決まり (その段で詳しく決める):
  - Node と依存は lock file で固定する。生成のコマンドを 1 つに決める。
  - ビルド済みの成果物を afx-ops の package に同梱し、commit する。
  - 固定の asset route と MIME で返す。
  - wheel / sdist に収録されていることと、隔離後の web から読めることを test する。
  - CSP は `script-src` を同一 origin の固定 path に限る。inline script・eval・外部 CDN は許さない。

## 3. 裁定 (2026-11-08 利用者が決定、すべて推奨どおり)

1. **書込みの経路**: loopback か HTTPS に限る。
2. **web の承認者**: Basic のユーザー名を actor (`asserted_actor=web:<user>`) として別欄に残し、承認者を 1 人として扱う。共有した資格情報の人は区別できないことを了解する。
3. **依存の図**: 段 A は表だけ。図 (と Node の導入) は、表を使ってみてから決める。
4. **段の順序**: 段 A を今始め、改善の要約の束と並行して進める (`/improve` の画面だけは要約の束の後)。段 B1 は実装の前にもう 1 周レビューする。段 B2 (HTTPS) と段 C (モデル選択) は別チケット。

## 4. 受入 (段ごと)

段 A:
1. 一覧と状態:
   - `/plugins` に、列挙元の和集合にある名前がすべて出る。完了した disable の後の名前も出る。
   - 3 つの軸が独立に出る。CLI の `afx plugin list` と同じ純関数を使い、kind は 4 種で揃う。
   - 読込除外の理由は固定語で、loader の自由文が出ない。
   - 同じ入力なら CLI と API の意味が一致する。CLI は DB を作らず、移行もしない (observer の読取専用接続のまま)。
2. 依存の表:
   - 例外を投げない列挙関数で、alias ごとの状態が出る。知らない状態は「不明」で、「一致」とは出ない。
   - loader の admit は変わらない (退行させない test)。
3. 有用性の数字:
   - 提出件数と配備成功件数を、§1 段 A の定義どおりに数える (数え方を test で固定)。
   - 再評価の依頼は数えない。alias が重複しても 1 件。`indicator_deps` の無い古い strategy の依頼は「不明」として別に数える。
4. 新しい画面も、今の web の決まりに従う: Host 照合、Basic、64 KiB の上限、固定 CSS と CSP、DTO の allowlist。web は DB を読まない。`{name}` の照合は名前の文法で行う。read model は世代 key の memo で、要求ごとに作り直さない。
5. 既存 test の変更範囲:
   - afx-ops 側: route 表は読取の route を足すだけ。405 は変えない。
   - daemon 側: endpoint 数 (+2)。`ops-web` の scope は変えない。

段 B1:
6. 書込みは、§1 段 B1 の検査 1〜4 をすべて満たすときだけ通る。どれか 1 つが欠けると 4xx で、daemon を呼ばない (変異で 1 つずつ外して確かめる)。1 回目の POST にも検査 1〜3・5 が掛かる。
7. 起動時の検査: `write = true` かつ `auth = none`、または `write = true` かつ非 loopback で TLS 無しは、起動時の検査で落ちる。loopback では起動する。
8. 鍵:
   - 書込みが無効なら、web は `ops-web-write` の鍵を読まない。書込みが有効なら、`ops-web-write` の鍵だけを読む。
   - 隔離の後、web は他の principal の鍵を読めない。
   - `afx ops web enable --write` / `--no-write` で鍵が enroll / revoke される。構成が条件を満たさなければ enroll しない。
   - 新しい manifest で、afx-ops の bot・notify・web が止まらない (知らない principal を無視する)。
8b. 鍵の切替: 有効 → 無効 → 再有効 (revoked からの rotate)、既存の `enabled` からの切替、鍵を変えた後・設定を書く前の中断からの再開で、目標の状態に収束する。もともと止まっていた afx は起動しない。
9. 確認 record:
   - ユーザー・action・対象・完全な digest・理由・条件に結び付く。別の対象・action・条件に token を流用できない。
   - 期限切れ・2 回目・並行送信は拒否する (実行は 1 回)。
   - 承認と retry は daemon が、却下は web が実行の直前に、`payload_sha256` の変化を検出して実行しない。
   - record と token は disk に書かれない。未使用の record は 32 件までで、満杯なら 33 件目を 4xx で拒否し、既存の token は使える。
   - daemon が受け付けた直後に応答を失わせても、web の「結果を確かめる」で同じ job id を取り戻せ、job は増えない。
10. 監査:
    - `decided_by` は `api:ops-web-write`、`asserted_actor` は `web:<user>`。HTTP の入力からは actor を指定できない。
    - actor の規則は principal ごとの表で、bot の規則は変わらない。改善の job にも actor が残る。
11. 権限:
    - `ops-web-write` は、ask・backlog・policy・LOCAL_GUARD を呼べない。
    - `IMPROVE_RUN` は改善の開始だけを許し、operator・approver も持つ。改善 job の閲覧・取消は自分の job に限る。
    - 多重起動は `improve_running`。autopilot 中の制限が効く。
    - 同じ `Idempotency-Key` の replay で job id を取り戻せる。
12. 実際に隔離した web から、実際の UDS を通して daemon に承認できる。pidfd の検証ができない相手は拒否する。
13. web から入れた却下理由が、改善の prompt に流れない (固定文言 `rejected_by_human` のまま)。
14. daemon 側の固定 test を更新する範囲を明記して直す: `validate_principal_scopes` の表、AC-56 の表と「operator・approver 以外の行」の規則、ops-api 設計 §3.1 の表、actor の表。

段 B2:
15. 実際に隔離した TLS の web と handshake でき、書込みまで通る。handshake の期限は接続の期限以下。
16. 証明書と鍵の不一致・mode 違反・不在は、固定の reason で落ちる。seccomp の hash は TLS の trace で更新される。
16b. handshake を進めない接続を多数開いても 16 本の枠を超えず、timeout の後に枠が戻る。

段 C:
17. 候補:
    - 全候補が settings の検証と起動時の CLI 検査を通る。
    - opencode の候補は、候補ごとの検証が済むまで `default` に限る。
    - 候補一覧の API がある。
18. `runner_choice`:
    - 候補に無い `runner_choice` は拒否する。
    - 選んだ候補の snapshot が、ctx 経由で記録・admission・canary・runner・handshake・繰り返しの全回に渡る。
    - 実行の経路が `self._settings.runner.improve` を読まないこと、`choose_for_mission` と handshake に共有の `self._settings` を渡さないことを test で固定する。
    - 週次 (`default`) と手動 (別の候補) を並行に走らせ、それぞれの mission 記録・admission・worker handshake が自分の候補と一致する。
    - `runner_choice` を省くと今と同じ。
19. 選んでも、admission と fingerprint の判定は迂回されない。CLI と web が同じ口を使う。要約に「使った候補」が残る。

## 5. 残余

- 書込みを有効にした構成では、web が侵害されると承認の権限も渡る (元の境界は成り立たない)。
- 承認者は 1 人の扱い。Basic の資格情報を共有した人は区別できない。
- loopback の bind の前に reverse proxy を置くと、LAN の平文から書けてしまう (web には loopback に見える)。reverse proxy は要求しない方針 (2026-10-23) なので、置かないことを運用手順に書く。
- `DECIDE` は API の上では retry も許す (web からは出さない)。
- 却下の digest の照合は、直前に取り直した時点までの変化だけを検出する (Discord と同じ水準)。
- 自己署名の HTTPS では、初回にブラウザの警告を利用者が通す (段 B2)。
- 書込みを有効にするとき、afx が一度止まる (鍵の enroll のため)。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-08 | C2.2 | §3 を利用者の裁定で確定 (承認者は Basic のユーザー名で 1 人扱い、段 A は表だけ、A を今始め B1 は実装前にもう 1 周、B2・C は別チケット) | 利用者の裁定 | — |
| 2026-11-08 | C2.1 | 鍵の切替を状態機械に合わせる (未登録 enroll・active 照合・revoked rotate、目標を stage に記録、中断から再開、止まっていた afx は起動しない)。確認 record に状態 (未使用・実行中・結果判明・結果不明) と結果の回復の口、消費済みの保持、満杯時の 4xx。改善の候補を API → job → supervisor → `_execute_mission` → `prepare` へ運び、週次は default、繰り返しは同じ snapshot、実行専用の Settings を admission と handshake へ。TLS の handshake も接続枠に数える。却下の digest 照合は直前時点までと明記。CLI と API の一致、CLI は DB を作らない | sol 3 周目 (Important 4 / Minor 1) | — |
| 2026-11-08 | C2 | 段 B を B1 (loopback) と B2 (TLS) に分ける。`ops.toml` は `write`・`tls` の真偽値だけ、TLS の鍵と証明書は secret dir の固定名、SSLContext は隔離の前に作る、seccomp の再 trace、`check_web_access` で書込み × auth × 経路を落とす。1 process 1 鍵 (書込みが有効なら ops-web-write の鍵だけ)、`afx ops web enable --write` の手順、manifest の互換。確認 record は memory だけ・32 件・1 回目の POST にも検査、却下は web が直前に digest を照合、改善は token 由来の冪等 key で job id を取り戻す、web から retry は出さない。`IMPROVE_RUN` は改善の開始だけで operator・approver も持つ。actor の規則を principal ごとの表に。Origin を必須にして Referer を見ない。依存は例外を投げない列挙関数、read model は純関数で CLI と共有、kind 4 種。有用性は strategy の新規提出に限り再評価を除く、依頼単位、配備成功は journal の decided。read model の memo。段 C は候補ごとの起動時検査、opencode は候補ごとの検証まで default のみ、snapshot は ctx に載せる、`runner_choice` に改名、候補一覧の API、要約に候補名。却下理由の遮断、reverse proxy の残余、daemon 側の固定 test の範囲 | opus 2 周目 (Important 9 / Minor 9) | — |
| 2026-11-08 | C1 | 書込みは loopback か HTTPS に限る。書込み用の principal `ops-web-write` を分ける。確認 record を対象・action・digest・条件に結び付け、原子的に消費する。Origin の規則。actor は `asserted_actor` に分ける。`IMPROVE_RUN` と `JOBS_OWN`。plugin の read model。有用性は提出件数・配備成功件数に絞る。choice は実行専用の snapshot を全経路に渡す。JS は段 A・B に入れない。既存 test の変更範囲。実際の隔離を通す受入 | sol 1 周目 (Critical 1 / Important 8 / Minor 1)、利用者の裁定 (書込みは loopback か TLS) | — |
| 2026-11-08 | C0 | 初版 | 利用者の web 要件 (plugin と改善の画面群、JS・Node 解禁) | — |

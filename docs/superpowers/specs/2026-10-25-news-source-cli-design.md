# afx news (ニュース源の操作) と取得の連続失敗バックオフ設計書

**版:** v0.2 (裁定 6 点を反映。2026-10-25 ユーザー裁定「すべて推奨で」)

**対象:** `news-source-cli-and-429-backoff` (2026-10-15 のユーザー承認骨子、2026-10-16 に User-Agent の件を吸収)

**前提:** コマンド面の 7 群 × 2 レーン (`2026-10-23-cli-surface-design.md`)、ニュース源の追加は人間の運用操作 (2026-09-07 裁定、チケット `news-source-route-omitted`)。

## 0. 目的と非目標

目的は 2 つ。(1) 利用者がニュース源を追加・一覧・有効化・無効化・試験取得できるようにする。今は DB を直接触るしかない。(2) 取れない源を 30 分ごとに叩き続けない。yahoo-finance-topstories は 2026-09-02 から毎周期失敗し、累計 1,170 回の失敗が出ている。外向きリクエストは数が設計事項で、失敗が続く先への再試行は上限のある間引きにする。

- `afx news` は最上位コマンドで、daemon を必要としない。停止中でも動く。DB と activity log を直接書く点は `afx plugin` と同じ流儀である。
- 操作は人間だけが行う。改善ループ、bot、web、`afx ctl` (操作 API) からはニュース源を追加も変更もできない。経路を作らない。
- 非目標: 代替源の選定 (候補の実測表は別に出す。どの源を足すかは利用者の運用判断)。経済指標カレンダーの取得 (別の取得経路。本書の User-Agent とバックオフは news 取得だけに入れる)。源ごとの取得間隔の設定。源の削除 (無効化だけ。履歴と重複判定を残すため)。

## 1. 用語

- **源**: `news_sources` の 1 行。名前、取得方式 (`feed` = RSS/Atom、`web` = ページ本文抽出)、URL、有効か。
- **取得**: 1 つの源への 1 回の HTTP 要求とその解釈。収集器の周期 (30 分) と、利用者の `add` / `test` が行う。
- **連続失敗**: 同じ源で成功を挟まず失敗した取得の数。成功で 0 に戻る。
- **待機**: 連続失敗が閾値 N (3) に達した源を、期限まで取得しない状態。先方が Retry-After で指示した待ちとは別に数える。
- **源の健康記録**: 源ごとの最後の取得結果と連続失敗と待機期限。`alert_state` の key-value に JSON で持つ (既存の `fetch_attempt.news.<name>` と同じ置き場。schema 変更も migration も要らない)。

## 2. 利用者から見た動き

### 2.1 共通

- どの副コマンドも daemon 不要。root の解決は他の最上位コマンドと同じ。
- 変更系 (`add` / `enable` / `disable`) は最後に固定文を 1 行出す: `反映は次の収集周期 (最長 30 分) です。`
- rc: 0 = 成功 (冪等な再実行を含む)。1 = 実行時の失敗 (源が無い、重複、取得失敗、DB が使えない、確認に答えなかった・拒否した)。2 = 使い方の誤り (引数の文法、名前・URL の文法違反)。2 は DB にも先方にも触れる前に返す。
- 名前の文法: `[a-z0-9][a-z0-9-]{0,39}`。status の未完手順に名前をそのまま出すので、既存の `ident` (`[a-z0-9_.-]{1,64}`) に収まる範囲に絞る。同梱の 3 源 (`yahoo-finance-topstories` / `nhk-keizai` / `fxstreet-news`) は通る。
- URL の検査: scheme は `http` / `https` のみ、host あり、userinfo (`user:pw@`) 無し、2048 文字以内。URL の内容の可否 (私設 LAN など) は検査しない。本機の利用者自身の操作で、制限しても守る対象が無い。
- 重複: 名前または URL が既存 (有効・無効を問わず) なら、どちらが重複かを書いて rc=1。判定は完全一致で、DB の UNIQUE 制約を最終の判定とする (同時に 2 本走っても片方が確実に失敗する)。
- activity log には URL を書かない (query に鍵が入る URL がありうる)。名前と取得方式だけを書く。

### 2.2 `afx news list` `[読取]`

何も書かない。DB が無ければ作らず、`ニュース源を読めません (DB がありません)。` を出して rc=1。0 件なら `ニュース源はありません。` で rc=0。

```
ニュース源: 3 件 (有効 2 / 無効 1)
yahoo-finance-topstories  無効  feed  直近: 失敗 (HTTP 404)  2026-10-25 09:30 UTC
  https://example.invalid/rss
nhk-keizai                有効  feed  直近: 成功 12 件       2026-10-25 09:30 UTC
  https://example.invalid/nhk
fxstreet-news             有効  feed  直近: 失敗 (HTTP 429)  2026-10-25 09:30 UTC  連続 3 回、2026-10-25 10:30 UTC まで待機
  https://example.invalid/fx
```

- 「直近」は源の健康記録から読む。元の骨子は activity から読むとしていたが、activity に源ごとの成功行が無い (成功は全体で `collected` 1 行だけ) ので、収集器が源ごとに記録する (§3.4)。まだ一度も取得していない源は `直近: 未取得`。
- 失敗の理由は固定語に畳む: `HTTP <code>` / `タイムアウト` / `接続できません` / `取得できません`。例外の文面や URL は出さない。
- 待機中でなく連続失敗が N 以上のままなら `連続 <n> 回` だけを出す (期限が過ぎて再試行待ちの状態)。

### 2.3 `afx news add <name> <url> [--fetcher feed|web] [--yes]` `[変更]`

既定の取得方式は `feed`。順序は次のとおり。

1. 文法・URL を検査する (違反は rc=2)。重複を調べる (rc=1)。
2. **追加の前に 1 回だけ取得する**。再試行しない。固定の User-Agent を付ける。タイムアウトは daemon の data hook と同じ設定値 (`worker.data_hook_timeout_sec`)。
3. 結果を出す: `取得: <n> 件 (うち本文・要約あり <m> 件)`。
4. 取得が失敗したら `取得に失敗したので追加しません: <固定語>` で rc=1。DB は変えない。
5. 0 件だったら確認する: `0 件でした。それでも追加しますか? [y/N]`。`y` だけ進める。端末でない (stdin が TTY でない) ときは `--yes` が無ければ問わずに rc=1 で追加しない (`--yes` なしでは追加しない旨を 1 行出す)。件数が 1 以上なら確認しない。`--yes` は確認だけを省略し、取得の失敗は越えられない。
6. 有効 (`enabled=1`)、`added_by=user` で追加し、activity に `news_source_added` (名前、取得方式) を 1 行残す。`追加しました: <name> (<fetcher>)` と §2.1 の反映文を出す。

`--fetcher` を省略して 0 件だったとき、`--fetcher web` を試すよう 1 行案内する。

### 2.4 `afx news enable|disable <name>` `[変更]`

- 名前が無ければ rc=1 (`ニュース源 <name> はありません。`)。
- すでにその状態なら `既に有効です` / `既に無効です` で rc=0。DB も activity も変えない。
- 変更したら activity に `news_source_enabled` / `news_source_disabled` を 1 行残し、`有効にしました: <name>` / `無効にしました: <name>` と反映文を出す。
- `enable` は、その源の健康記録 (連続失敗と待機) を消す。人間が「もう一度試せ」と言ったことを意味するので、待機は引き継がない。`disable` は記録を残す (`list` で原因が読める)。有効のままの源の待機を解きたいときは `disable` → `enable` する。

### 2.5 `afx news test <name>` `[読取]`

有効・無効を問わず、その源を 1 回取得して結果だけを出す。DB (源の表、健康記録、取得試行の記録)、activity、RAG のどれにも書かない。

```
取得: <n> 件 (うち本文・要約あり <m> 件)  HTTP <code>
```

取得が失敗したら `取得に失敗しました: <固定語>` で rc=1。源が待機中でも取得する (利用者が明示した 1 回)。待機中なら先頭に `この源は <期限> UTC まで待機中です (収集器は取得しません)。` を足す。

### 2.6 daemon との同時実行

- daemon が動いていても、`afx news` は instance lock を取らない。lock の役割は daemon の単一起動と、資金・状態ファイルの所有である。`afx news` が触るのは `news_sources` の 1 行 (と `enable` が消す 1 つの健康記録) だけで、短い自動コミットの 1 文で終わる。`afx plugin` が daemon と同じ DB に書くのと同じ前提である。取っても守るものが無く、lock を取ると daemon の稼働中は何も操作できなくなる。
- DB は WAL で、書き込み待ちは 5 秒 (`busy_timeout`)。競合で待ちが切れたら `DB が使用中です。もう一度実行してください。` で rc=1 (部分的な変更は残らない)。
- 収集器は周期ごとに有効な源を読み直す (キャッシュしない)。`disable` した時点で走っている周期があれば、その周期が最悪 1 回その源を取る。それ以降は取らない。

## 3. 収集器のバックオフ

### 3.1 何を失敗として数えるか

収集器の 1 源の取得で例外が出たら 1 回の失敗とする。HTTP の 429 / 5xx に限らない。4xx (404 など)、タイムアウト、接続エラーも数える。理由: 現に yahoo は User-Agent を付けると 404 を返し、骨子の「429 / 5xx」だけでは止まらない。要求を無駄に出すという害は同じである。

数えないもの: 先方が Retry-After を指示した 429 / 503 (既存の待ちで守る。連続失敗の数は増減させない)、記事が 0 件の成功 (ページの本文抽出に失敗しただけで、要求は通っている)、未知の取得方式 (要求を出していない設定ミスで、従来どおり `news_source_failed` を残す)。

### 3.2 待機の段階

連続失敗 `f` が閾値 `N = 3` 以上になった取得の失敗時に、待機期限を `その取得を試みた時刻 + min(30 分 × 2^(f−N+1), 24 時間)` とする。

| 連続失敗 f | 待機 |
|---|---|
| 1, 2 | なし (次の周期で取得) |
| 3 | 1 時間 |
| 4 | 2 時間 |
| 5 / 6 / 7 | 4 / 8 / 16 時間 |
| 8 以上 | 24 時間 (上限) |

元の骨子は「30 分 → 1 時間 → …」だったが、収集周期が 30 分なので 30 分の待機は次の周期にはもう過ぎていて何も間引かない。最初の待機を 1 時間にして 1 周期を飛ばす。待機の最中に成功する経路は無い (取得しないので)。期限が過ぎた最初の周期で 1 回取得し、失敗なら `f` が 1 増えて次の段階へ進む。成功したら連続失敗も期限も消える。

収集器が先方の Retry-After を受けた取得は、従来どおり `fetch_attempts` の待ち (最長 24 時間) だけで守る。段階の待機と Retry-After の待機が両方あるときは遅いほうに従う。

### 3.3 見え方

- 待機中の周期ごとに、期限ごとに 1 回だけ activity に `news_source_skipped` を残す (今の Retry-After の待ちと同じ。同じ期限の間は再度書かない)。文面: `<name>: 連続 <f> 回失敗のため <HH:MM> UTC まで待つ`。
- 連続失敗が N 以上の有効な源ごとに、未完手順 (`afx status` と `afx ctl status` の `pending steps`) に 1 行出す。コードは `news_source_failing`、重大度は warn、param は源の名前。文面: `源 <name> が連続で失敗しています。`afx news list` で確認し、不要なら `afx news disable <name>` で止めてください`。成功するか `enable` / `disable` で消える (無効な源は出さない)。未完手順の最大件数の制約は他の step と同じ。読めないとき (DB 不調) は step を出さず、他の節を妨げない。
- `afx ctl status` が使う status の `_STATUS_SETUP_CODES` に新コードを足す。`afx status` (offline) は読取専用の DB 接続で同じ事実を読む。

### 3.4 状態の持ち方

源の健康記録 (JSON): `consecutive_failures`、`backoff_until` (tz-aware UTC か null)、`last_outcome` (`ok` / `failed`)、`last_at`、`last_count`、`last_reason` (§2.2 の固定語の元になる短い識別子)。key は `news_source_health.<name>`。

- 収集器が取得のたびに 1 回書く (成功も含む。`list` の「直近」の正本になる)。書き込みは既存の取得試行の記録と同じ 1 文のコミットで、失敗しても取得は止めない (読み書きの例外は技術ログに warning を残して取得を続ける。永続層の不調で取得が止まるより、従来どおり 30 分周期で動くほうを選ぶ)。
- DB なので再起動をまたぐ。メモリだけにすると、再起動のたびに失敗が数え直しになり、再起動の連打がそのまま先方への連打になる。
- 待機の判定は取得の直前に 1 回読む。壊れた値は「待機なし」として扱い、その取得の結果で書き直す。時計の巻き戻しで `backoff_until` が上限 (24 時間) より先なら、上限に丸めて扱う。
- `news_source_skipped` の「期限ごとに 1 回」は、収集器の実行中のメモリで持つ (今の実装と同じ)。再起動で同じ期限が 1 行増えうるが、再起動 1 回につき 1 行なので許す。

### 3.5 User-Agent

`feed` / `web` の取得はどちらも、固定の `User-Agent: agentic-fx/<版> (+https://github.com/teru358/agentic-fx)` を付ける。版は `importlib.metadata` から、URL は `pyproject.toml` の `[project.urls]` の `Repository` から読んで組み立てる (§7 の裁定 1)。URL を他の場所に文字列で複製しない。ブラウザを装う値は使わない。定義は 1 箇所に置き、`afx news add` / `test` の取得も同じ関数を通る (収集器と試験取得で挙動が分かれない)。

### 3.6 外向きリクエストの予算

- 収集器: 取得が失敗する源の 1 日の要求数は、従来の約 48 回から、N 回の連続失敗後は 1 時間 → 2 → 4 → 8 → 16 → 24 時間間隔へ減る (初日 12 回前後、以降 1 日 1 回)。成功する源は変わらない。
- `add` と `test` は利用者が 1 回ずつ起こす取得で、内部で再試行も繰り返しもしない。収集器の予算や健康記録には数えない (`test` は何も書かない)。

## 4. コマンド面への組み込み

- `cli_surface.py` の `news` record は `planned=True, implemented=False` から `implemented=True, exposed=True` に変える。親 record の見出しは `市場ニュース源を扱う`、ラベルは下位操作ごと。`ParentRecord("news", …)` を `history` と同様に足す。
- 葉 record を 5 本足す (list / add / enable / disable / test): `news list` `[読取]`、`news test` `[読取]`、`news add` `[変更]`、`news enable` `[変更]`、`news disable` `[変更]`。`add` は 0 件のときだけ確認するので `confirm=False` とし、condition に `0 件のとき確認` を書く。`add` / `enable` / `disable` の condition は `次の収集周期で反映`。help の文言は `cli_surface.py` の accessor だけから取る (cli-surface 設計 §3.1)。parser 側に str literal の help を書かない。
- 正式な local 親 command と argparse の一致 test の例外集合から `news` を外す (planned の例外は消える)。cli-surface 設計の AC-27 は本書の AC-14 で置き換え、変更履歴に残す。
- `afx --help` の全文 golden (cli-surface 設計 §3.2) と `afx news --help` の golden を、I1 と同じ条件 (幅 100、色の環境変数を消す、非 TTY の capture、`color=False` は 3.14 以上だけ) で**実装の実出力から再生成**し、§3.2 に写す。手で書かない。変わるのは「市場入力を整える」群の行 (`history` → `history / news`)、左右レーンの要約表、`news` の下位操作の説明 (`afx news --help`)。`afx ctl help` は変わらない。
- 原則の表 (`docs/operations/ops-api-2026-10-09.md` の「CLI の置き場の原則」) の「市場入力を整える」行を、`afx` 列を `[変更]` `afx history import` / `[読取]` `afx history compare`・`coverage` / `[読取]` `afx news list`・`test` / `[変更]` `afx news add`・`enable`・`disable` に更新する。`afx ctl` 列は「—」のまま。cli-surface 設計の分類表 (§2.1、`afx news …` の行)、§3.4、§0.1 の後続束の記述、変更履歴を同じ commit で更新する。doc test (c) の正式名 9 つは変えない (news は含まれない)。
- 操作 API には endpoint を足さない。`ENDPOINTS` の個数 pin (29) は変わらない。

## 5. 受入

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | 実機: 本機の yahoo-finance-topstories を `afx news disable` で止めると、activity に `news_source_disabled` が 1 行出る。次の収集周期から `news_source_failed` の同源の行が出ない。`afx news list` が `無効` と最後の失敗を示す。`enable` で戻せる (戻したら止め直す) | 本機の activity、`afx news list`、2 周期分の観測 |
| AC-2 | 連続失敗の境界。N-1=2 回では待機が付かず次の周期で取得する。N=3 回目の失敗で 1 時間の待機が付く。N+1=4 回目で 2 時間。f=7 で 16 時間、f=8 と f=9 で 24 時間 (上限 L の L-1 / L / L+1) | FakeClock と stub fetch の収集器 test |
| AC-3 | 待機中の周期は取得 0 回、期限ごとに `news_source_skipped` が 1 行 (周期を 2 回回しても 1 行)。期限を 1 秒過ぎた最初の周期で 1 回取得する。成功で連続失敗と期限が消え、以後の失敗は f=1 から数える | 収集器 test、activity の行数 |
| AC-4 | 数える失敗と数えない失敗。429 / 5xx (Retry-After なし)、404、タイムアウトは数える。Retry-After 付きの 429 / 503、0 件の成功、未知の取得方式は連続失敗を増やさない | 例外種の直積 test |
| AC-5 | 再起動をまたぐ。f=3 で待機中に収集器を作り直しても待機が続き、取得 0 回。健康記録が壊れた値なら待機なしとして取得し、書き直す。永続層が例外でも取得が続く | 収集器の再生成 test、fault injection |
| AC-6 | `feed` / `web` のすべての外向き取得 (収集器、`add`、`test`) に `User-Agent: agentic-fx/<版> (+<URL>)` が付く。python-httpx 既定の値は出ない。版と URL は pyproject の `version` と `[project.urls]` `Repository` に一致する (pyproject を読む test で pin。コード側に URL の文字列リテラルが無い) | httpx の送信記録 (mock transport)、pyproject との照合 test |
| AC-7 | `list` と `test` は実行前後で DB (全 table と `alert_state`)、activity log、RAG ディレクトリが byte 一致。DB が無いとき `list` は DB を作らず rc=1 | before/after の tree と DB の比較 |
| AC-8 | rc 規則。名前・URL・`--fetcher` の文法違反と使い方の誤りは rc=2 で、取得 0 回・DB 書込 0 回。源が無い・重複・取得失敗・確認拒否・非対話で `--yes` 無しは rc=1。冪等な `enable` / `disable` は rc=0 で activity を増やさない | CLI test |
| AC-9 | `add` の取得は 1 回だけで再試行しない。0 件は確認、`y` 以外は追加しない。`--yes` で 0 件を通すが、取得失敗は通らない。件数 1 以上は確認しない。名前または URL の重複は取得の前に rc=1 | 取得回数の spy、DB |
| AC-10 | 名前の文法 (先頭 1 文字・最大 40 文字の L-1 / L / L+1)、URL の scheme と userinfo と 2048 文字の境界 | 境界値 test |
| AC-11 | `enable` は健康記録を消し、`disable` は残す。有効のままの源には触らない。activity には URL が出ない | DB と activity の比較 |
| AC-12 | status: 連続失敗 N 以上の有効な源ごとに `afx status` と `afx ctl status` の未完手順に `news_source_failing` が 1 行出る。N-1、無効な源、成功後は出ない。DB が読めないときは step を出さず他の節は表示する | 両経路の stdout、pending steps 件数上限の境界 |
| AC-13 | 同時実行: 収集器が書いている DB に `add` / `enable` が成功する。2 本の同名 `add` は片方だけ成功。`busy_timeout` 超過は rc=1 で変更が残らない。instance lock を取らない (daemon が lock 保持中でも動く) | 2 process の test、flock 記録 |
| AC-14 | `news` は implemented / exposed、5 本の葉 record が [読取] / [変更] の分類と accessor 由来の help を持つ。planned の例外 pin は消える。`afx --help` と `afx news --help` が実出力の golden と byte 一致する。ops-api の原則表と cli-surface 設計に news が出る。doc test (c) の 9 名は不変 | golden、一致 test、文書 grep pin |
| AC-15 | 経路の遮断: `ENDPOINTS` に news 系の code が無い。`afx ctl news …` は parse できない。改善 agent の tool registry と bot / web の module が `news_sources` の変更関数 (`add` / `set_enabled`) を import していない。seed (`seed_default_sources`) は無効化した源を再有効化も再追加もしない | AST / grep pin、parse test、seed の再実行 test |

## 6. 変更一覧と task 分割

| 区分 | file | 内容 |
|---|---|---|
| 新規 | `datafeed/user_agent.py` (仮) | 固定 UA の組み立て 1 箇所 (`importlib.metadata` の版 + `Repository` URL) |
| 変更 | `pyproject.toml` | `[project.urls]` に `Repository = "https://github.com/teru358/agentic-fx"` を足す (現状は無い) |
| 新規 | `store/news_health.py` (仮) | 源の健康記録の読み書きと段階計算 (純関数 + `alert_state`) |
| 変更 | `datafeed/fetchers.py` | 両 `httpx.get` に UA |
| 変更 | `datafeed/news_collector.py` | 失敗の分類、段階待機、健康記録、`news_source_skipped` の文面 |
| 変更 | `setup/pending_steps.py` `pending_facts.py` `pending_text.py` `service.py` `status.py` | `news_source_failing` と読取 |
| 新規 | `news_cli.py` (仮) | list / add / enable / disable / test |
| 変更 | `backtest/cli.py` (parser と dispatch) `cli_surface.py` | 親と葉 5 本、planned 解除 |
| 変更 | cli-surface 設計 §2.1 / §3.2 / §3.4 / §9 AC-27 / 履歴、ops-api 原則表 | 文書 |
| 変更 | `config/settings.yaml.example` | 変更なし (設定キーを足さない) |
| 変更 | チケット | 起票時の骨子を状態に反映 |

- **T1 収集器 (UA + 健康記録 + バックオフ + status の step)**: 純 Python。コード約 250 行、test 約 350 行。codex (terra) 向き。AC-2〜6、12 (status 側)。
- **T2 CLI + コマンド面 + 文書**: 純 Python と文書。README と `docs/operations/first-run-acceptance.md` に §9 の注意を各 1 段落足す。コード約 300 行、test 約 400 行、golden の再生成は指揮者が実出力で確認。T1 の健康記録の読み書き (`list` の「直近」、`enable` の消去) に依存するので T1 の後。AC-1 は両方が main に入った後の本機確認で、指揮者が行う。
- 周回: 境界コードではない (資金・隔離・鍵・削除に触れない) が、DB 書込と外向き要求を含む。codex 1 周目 → 閉鎖確認。変異スイープは段階計算の境界とカウント規則を対象にする。

## 7. 裁定 (2026-10-25、ユーザー: 「すべて推奨で」)

技術で決められるもの (状態の置き場所、数えるエラー種、`enable` が記録を消す、確認の仕方) は本書で決めた。次の 6 点は製品・運用の判断で、すべて推奨どおりに決まった。

1. **User-Agent と repo URL**: `agentic-fx/<版> (+https://github.com/teru358/agentic-fx)`。URL は `pyproject.toml` の `[project.urls]` (key `Repository`) に 1 回だけ書き、UA は `importlib.metadata` の版とその URL から組み立てる。他の場所にリテラルとして複製しない。UA を pyproject と照合する test を足す (AC-6)。裁定 (2026-10-25、ユーザー): 推奨どおり。
2. **0 件の源の追加**: 確認のうえ追加を許す (非対話は `--yes` 必須)。更新が少ない feed を足せなくなるのを避ける。0 件が続けば `list` に出る。裁定 (2026-10-25、ユーザー): 推奨どおり。
3. **待機の上限**: 24 時間。長くすると回復に気づけない。上限に達した源は `afx news disable` を案内する step が残り続ける。裁定 (2026-10-25、ユーザー): 推奨どおり。
4. **閾値 N**: 3。一時障害を「連続」と数えて止めない一方、1.5 時間で間引きが始まる。裁定 (2026-10-25、ユーザー): 推奨どおり。
5. **最初の待機**: 1 時間 (§3.2)。骨子の 30 分は周期が 30 分なので何も間引かない。骨子の文面とは異なることを承知のうえの裁定。裁定 (2026-10-25、ユーザー): 推奨どおり。
6. **`test` を待機中の源に対して許す**: 許す (利用者の明示した 1 回で、書き込みも無い。禁止すると原因調査ができない)。裁定 (2026-10-25、ユーザー): 推奨どおり。

## 8. 利用者の責任と文書

- ニュース源を足すかどうかは運用者 (利用者) の判断である。各源の利用規約・robots の確認は運用者の責任で、製品は代わって確認も保証もしない。
- 現状の事実: 製品は既定のニュース源を同梱している。`datafeed/default_sources.py` の 3 源 (`yahoo-finance-topstories` / `nhk-keizai` / `fxstreet-news`、いずれも feed) で、`afx init` と起動時の `seed_default_sources` が登録する。公式 RSS だけではなく商用サイトの feed を含む。この同梱の扱い (残す / 利用者が選ぶ形にする) は本書では変えない。必要なら別チケット候補とする。`config/settings.yaml.example` の `news:` は `cleanup_hours` のみで、源は持たない。
- UA の URL は先方が連絡できる窓口で、責任の引き受けを意味しない。
- T2 で README と `docs/operations/first-run-acceptance.md` に各 1 段落を足す: ニュース源は運用者の判断で追加すること、各源の利用規約の確認は運用者の責任であること、UA の URL は連絡先であること。
- 免責の法的根拠は LICENSE (MIT) の無保証条項。リポジトリ直下に `LICENSE` が在り、`pyproject.toml` も `license = "MIT"` / `license-files = ["LICENSE"]` で参照している。新規作成は不要。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `news-source-cli-and-429-backoff` の骨子の設計化 | |
| 2026-10-25 | v0.2 | 裁定 6 点の反映と利用者責任の節 | ユーザー裁定「すべて推奨で」 | |

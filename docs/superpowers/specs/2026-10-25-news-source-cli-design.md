# afx news (ニュース源の操作) と取得の連続失敗バックオフ設計書

**版:** v0.4 (v0.3 への設計レビュー 1 周目の指摘 20 件を反映。Retry-After 連打の間引き、URL の秘密の全経路、健康記録の置き場と競合、status の枠、wizard の閉じ方ほか)

**対象:** `news-source-cli-and-429-backoff` (2026-10-15 のユーザー承認骨子、2026-10-16 に User-Agent の件を吸収)

**関連チケット:** `news-default-sources-to-user-input` (本書の T2 に統合)

**前提:** コマンド面の 7 群 × 2 レーン (`2026-10-23-cli-surface-design.md`)、ニュース源の追加は人間の運用操作 (2026-09-07 裁定、チケット `news-source-route-omitted`)。

## 0. 目的と非目標

目的は 2 つ。(1) 利用者がニュース源を追加・一覧・有効化・無効化・試験取得できるようにする。今は DB を直接触るしかない。(2) 取れない源を 30 分ごとに叩き続けない。yahoo-finance-topstories は 2026-09-02 から毎周期失敗し、累計 1,170 回の失敗が出ている。外向きリクエストは数が設計事項で、失敗が続く先への再試行は上限のある間引きにする。

- `afx news` は最上位コマンドで、daemon を必要としない。停止中でも動く。DB と activity log を直接書く点は `afx plugin` と同じ流儀である。
- 操作は人間だけが行う。改善ループ、bot、web、`afx ctl` (操作 API) からはニュース源を追加も変更もできない。経路を作らない。
- 既定のニュース源の同梱は廃止する (裁定 2026-10-25)。`datafeed/default_sources.py` と `seed_default_sources` を削除し、起動時も `afx init` も源を自動登録しない。`afx init` に名前と URL の入力式の段を足す (§2.7)。源は利用者が自分で選ぶ (§8)。
- 非目標: 代替源の選定 (候補の実測表は別に出す。どの源を足すかは利用者の運用判断)。経済指標カレンダーの取得 (別の取得経路。本書の User-Agent とバックオフは news 取得だけに入れる)。源ごとの取得間隔の設定。源の削除 (無効化だけ。履歴と重複判定を残すため)。同じ相手先 (origin) に対する源の合計数の上限 (源を足すのは利用者の判断で、1 源ごとの間引きは各行に効く。§8)。

## 1. 用語

- **源**: `news_sources` の 1 行。名前、取得方式 (`feed` = RSS/Atom、`web` = ページ本文抽出)、URL、有効か。
- **取得**: 1 つの源への 1 回の HTTP 要求とその解釈。収集器の周期 (30 分) と、利用者の `add` / `test` が行う。
- **連続失敗**: 同じ源で成功を挟まず失敗した取得の数。成功で 0 に戻る。
- **拒否の連続**: 同じ源で、成功も他種の失敗も挟まず、先方が Retry-After を付けた 429 / 503 を受け続けた回数。連続失敗とは別に数える。
- **待機**: 連続失敗または拒否の連続が閾値 N (3) に達した源を、期限まで取得しない状態。
- **実効待機期限**: 源の健康記録の待機期限と、先方が Retry-After で指示した待ち (`fetch_attempts`) の遅いほう。収集器、`list`、`test` はすべてこの 1 つの関数で読む。
- **源の健康記録**: 源ごとの最後の取得結果と連続失敗と拒否の連続と待機期限。`alert_state` の表に JSON で持つ。保存を担う `news_health` が、`fetch_attempts` と同じく自分の key 接頭辞 (`news_source_health.`) を検証して表を直接読み書きする。`alert_state` の既知 key の許可リストは広げない (§3.4)。表も列も増えないので `schema_version` は上げず、migration もしない。

## 2. 利用者から見た動き

### 2.1 共通

- どの副コマンドも daemon 不要。root の解決は他の最上位コマンドと同じ。
- 変更系 (`add` / `enable` / `disable`) は最後に固定文を 1 行出す: `反映は次の収集周期 (最長 30 分) です。`
- rc: 0 = 成功 (冪等な再実行を含む)。1 = 実行時の失敗 (源が無い、重複、取得失敗、DB が使えない、確認に答えなかった・拒否した)。2 = 使い方の誤り (引数の文法、名前・URL の文法違反)。2 は DB にも先方にも触れる前に返す。
- 名前の文法: `[a-z0-9][a-z0-9-]{0,39}`。status の未完手順に名前をそのまま出すので、既存の `ident` (`[a-z0-9_.-]{1,64}`) に収まる範囲に絞る。既存 DB に登録済みの 3 源 (`yahoo-finance-topstories` / `nhk-keizai` / `fxstreet-news`) の名前は通る。
- URL の検査: scheme は `http` / `https` のみ、host あり、userinfo (`user:pw@`) 無し、fragment (`#…`) 無し、2048 文字以内。fragment は HTTP 要求に載らず、同じ対象を別 URL に見せて重複判定をすり抜けるので拒否する。URL の内容の可否 (私設 LAN など) は検査しない。本機の利用者自身の操作で、制限しても守る対象が無い。
- 重複: 名前または URL が既存 (有効・無効を問わず) なら、どちらが重複かを書いて rc=1。URL は scheme と host を小文字にし、既定 port (`:80` / `:443`) を落とした形で比べる (保存も同じ形)。path と query は文字どおりに比べる。既存行は同じ正規化をかけて比べる。DB の UNIQUE 制約を最終の判定とする (同時に 2 本走っても片方が確実に失敗する)。重複の表示は「URL が源 `<既存の名前>` と重複」とし、URL は出さない。
- activity log への書込は best-effort で、通常は変更ごとに 1 行残る。書込が失敗しても DB の変更は成功として扱い (rc=0)、技術ログに warning を残す (既存の `ActivityLog.write` の契約)。activity には URL を書かない。名前と取得方式だけを書く。

### 2.1.1 URL に秘密が含まれうる場合の扱い

query や path に鍵を持つ URL を持つ源がありうる (有料の feed など)。そうした URL を拒否はしない (見分けられず、正当な源を締め出す)。そのかわり、取得に使う以外の経路に URL を出さない。経路を全数並べる。

| 経路 | 扱い |
|---|---|
| コマンド引数 (shell の履歴、プロセス一覧) | `add` の `<url>` に `-` を渡すと標準入力から読む (端末なら画面に表示しない入力。パイプならそのまま)。引数で渡した URL が履歴に残るのは利用者の環境の事柄で、ドキュメントに書く (§8) |
| DB の `news_sources.url` | 取得に必要なので原文を保存する。DB の外 (setup_record、活動記録、通知、RAG、操作 API の応答) に複製しない |
| `afx news list` | URL は `<scheme>://<host>/…` までに畳む。path と query は出さない |
| `afx news add` / `test` / wizard の出力とエラー | 取得失敗・重複・検査違反は固定語と名前だけ。URL も例外の文面も出さない |
| activity log | 名前・取得方式・固定語・例外を `safe_text` に通した短文だけ |
| 技術ログ (fetchers、収集器) | 源の URL を引数にしたログを持たない。feed の日時警告 (未知の timezone 略号) は URL でなく源の名前で出す。例外・警告は `safe_text` を通す |
| RAG に入る記事 | `feed` の記事 URL は記事ごとのリンク (源の URL ではない)。`web` の記事 URL は源の URL から query と fragment を落とした形、本文抽出で題名が取れないときの題名は URL でなく源の名前にする。agent は `search_news` でこれを読む |
| `afx status` / `afx ctl status` の未完手順、bot / web / 通知 | 源の名前だけ。URL を載せる欄が無い。改善 agent の context も名前と有効かだけ (既存) |

path に鍵を置く URL は、`web` 方式でも path が RAG の URL に残る。その場合の扱いは利用者の責任とする (§8)。

### 2.2 `afx news list` `[読取]`

何も書かない。DB が無ければ作らず、`ニュース源を読めません (DB がありません)。` を出して rc=1。0 件なら `ニュース源はありません。` で rc=0。

```
ニュース源: 3 件 (有効 2 / 無効 1)
yahoo-finance-topstories  無効  feed  直近: 失敗 (HTTP 404)  2026-10-25 09:30 UTC
  https://example.invalid/…
nhk-keizai                有効  feed  直近: 成功 12 件       2026-10-25 09:30 UTC
  https://example.invalid/…
fxstreet-news             有効  feed  直近: 失敗 (HTTP 429)  2026-10-25 09:30 UTC  連続 3 回、2026-10-25 10:30 UTC まで待機 (連続失敗)
  https://example.invalid/…
```

- 「直近」は源の健康記録から読む。元の骨子は activity から読むとしていたが、activity に源ごとの成功行が無い (成功は全体で `collected` 1 行だけ) ので、収集器が源ごとに記録する (§3.4)。まだ一度も取得していない源は `直近: 未取得`。この版より前に登録されていた源 (本機の 3 源を含む) は、健康記録が無いので最初の収集まで `未取得` と出る。activity から過去の連続失敗を推定して埋めることはしない (activity に源ごとの成功の記録が無く、正確に復元できない)。
- 失敗の理由は固定語に畳む: `HTTP <code>` / `タイムアウト` / `接続できません` / `取得できません`。例外の文面や URL は出さない。
- 待機の表示は実効待機期限 (§1) から出し、原因を `(連続失敗)` か `(先方の指示)` で区別する。
- 待機中でなく連続失敗が N 以上のままなら `連続 <n> 回` だけを出す (期限が過ぎて再試行待ちの状態)。

### 2.3 `afx news add <name> <url> [--fetcher feed|web] [--yes]` `[変更]`

既定の取得方式は `feed`。`<url>` に `-` を渡すと標準入力から読む (§2.1.1)。順序は次のとおり。

1. 文法・URL を検査する (違反は rc=2)。重複を調べる (rc=1)。
2. **追加の前に 1 回だけ取得する**。再試行しない。固定の User-Agent を付ける。タイムアウトは daemon の data hook と同じ設定値 (`worker.data_hook_timeout_sec`)。
3. 結果を出す: `取得: <n> 件 (うち本文・要約あり <m> 件)`。
4. 取得が失敗したら `取得に失敗したので追加しません: <固定語>` で rc=1。DB は変えない。
5. 0 件だったら確認する: `0 件でした。それでも追加しますか? [y/N]`。`y` だけ進める。端末でない (stdin が TTY でない) ときは `--yes` が無ければ問わずに rc=1 で追加しない (`--yes` なしでは追加しない旨を 1 行出す)。件数が 1 以上なら確認しない。`--yes` は確認だけを省略し、取得の失敗は越えられない。`<url>` を `-` で渡した場合、標準入力は URL が使うので確認は問えず、`--yes` が無ければ 0 件は追加しない。
6. 有効 (`enabled=1`)、`added_by=user` で追加し、activity に `news_source_added` (名前、取得方式) を 1 行残す。`追加しました: <name> (<fetcher>)` と §2.1 の反映文を出す。

`--fetcher` を省略して 0 件だったとき、`--fetcher web` を試すよう 1 行案内する。

同時に同じ名前の `add` が 2 本走ると、両方が事前検査を通って取得し、INSERT で片方が失敗する (取得は各 1 回まで)。これは許す。lock は取らない。

### 2.4 `afx news enable|disable <name>` `[変更]`

- 名前が無ければ rc=1 (`ニュース源 <name> はありません。`)。
- すでにその状態なら `既に有効です` / `既に無効です` で rc=0。DB も activity も変えない。
- 変更したら activity に `news_source_enabled` / `news_source_disabled` を 1 行残し、`有効にしました: <name>` / `無効にしました: <name>` と反映文を出す。
- `enable` は、源の更新と健康記録の初期化 (連続失敗と拒否の連続を 0、待機期限なし、`reset_at` に現在時刻) を 1 つの transaction で行う。人間が「もう一度試せ」と言ったことを意味するので、ローカルの段階待機は引き継がない。ただし先方が Retry-After で指示した待ち (`fetch_attempts`) は解かない (先方の指示は人間の操作で消えない)。`disable` は記録を残す (`list` で原因が読める)。有効のままの源の段階待機を解きたいときは `disable` → `enable` する。
- この抜け道 (`disable` → `enable` で段階待機が消える) は人間の運用操作に限られる。繰り返し実行すれば段階待機を恒久的に回避できることは、既知の残余として §8 に書く。

### 2.5 `afx news test <name>` `[読取]`

有効・無効を問わず、その源を 1 回取得して結果だけを出す。DB (源の表、健康記録、取得試行の記録)、activity、RAG のどれにも書かない。

```
取得: <n> 件 (うち本文・要約あり <m> 件)
```

取得が失敗したら `取得に失敗しました: <固定語>` (HTTP エラーなら `HTTP <code>`) で rc=1。源が待機中でも取得する (利用者が明示した 1 回)。実効待機期限が先なら先頭に `この源は <期限> UTC まで待機中です (<原因>、収集器は取得しません)。` を足す。`test` は何も記録しないので、期限が過ぎた直後の周期で収集器がもう一度取ることがある。

### 2.6 daemon との同時実行

- daemon が動いていても、`afx news` は instance lock を取らない。lock の役割は daemon の単一起動と、資金・状態ファイルの所有である。`afx news` が触るのは `news_sources` の 1 行と、その源の健康記録だけで、1 つの短い transaction で終わる。`afx plugin` が daemon と同じ DB に書くのと同じ前提である。取っても守るものが無く、lock を取ると daemon の稼働中は何も操作できなくなる。
- DB は WAL で、書き込み待ちは 5 秒 (`busy_timeout`)。競合で待ちが切れたら `DB が使用中です。もう一度実行してください。` で rc=1 (部分的な変更は残らない)。
- 収集器は周期ごとに有効な源を読み直す (キャッシュしない)。`disable` した時点で走っている周期があれば、その周期が最悪 1 回その源を取る。それ以降は取らない。
- 収集器は取得の前に健康記録を読み (そのときの `reset_at` を控える)、取得の後に結果を書く。書く時点で `reset_at` が変わっていたら (その間に `enable` が初期化した)、古い取得の結果は捨てて書かない。`enable` 直後に古い失敗が待機を復活させることはない。

### 2.7 `afx init` のニュース源の入力 (既定源の同梱廃止)

段 3 (価格源) の副段として足す。新しい番号の段は足さない。価格源と同じ「市場入力の取得元を決める」段で、取得元の入力がここに集まるため (初回ウィザード設計 `2026-10-12-first-run-wizard-design.md` §3 の段 3 を改訂する)。

- 副段は価格源の選び方 (bridge の使う・使わない、いずれの分岐) によらず、段 3 を抜けるときに必ず 1 度だけ通る。価格源の分岐ごとに終わりを持つ構造のままにせず、段 3 の出口を 1 つにそろえる。
- 問い: `ニュース源 (名前 URL、空 Enter で飛ばす):`。空行になるまで繰り返す。
- 1 件ごとに `afx news add` と同じ文法 (§2.1 の名前・URL の検査と重複判定。違反はその場で理由を出して同じ問いに戻る) で検査し、1 回取得する。結果は `取得: <n> 件 (うち本文・要約あり <m> 件)`。取得に失敗した件は登録せず理由を出して問いに戻る。0 件のときは §2.3 と同じ確認 (`0 件でした。それでも追加しますか? [y/N]`) を出す。取得方式は `feed`。重複は DB と、この入力で既に受けた件の両方に対して調べる。
- 入力した件は回答として保持する。後の段から段 3 へ戻って再実行したとき、保持した件を表示し、追加の入力だけを受ける (取り直しも二重登録もしない)。
- 空 Enter で終えたら `後から `afx news add` で足せます。候補例は README を参照。` を出す。1 件も入力しなかった場合も同じ。
- 登録は段 10 の確定 1 の DB transaction の中で、入力された件だけを INSERT する (取得は確定の前に済ませ、確定の transaction は INSERT のみ)。transaction の中で重複を再検査し、取得の後に他のプロセスが同じ名前・URL を足していた件は INSERT せず、確定の後に `既に登録されていたので飛ばしました: <名前>` を出す。確定全体は止めない。入力が無ければ `news_sources` は 0 行のまま。
- 既存の DB (再実行を含む) の登録済みの源には触らない。起動時の自動登録はなくなる。
- `--non-interactive` は源を登録しない (入力式の段が無い)。

## 3. 収集器のバックオフ

### 3.1 何を失敗として数えるか

収集器の 1 源の取得で例外が出たら、次のとおり数える。

- 連続失敗に数える: HTTP の 429 / 5xx で Retry-After を伴わないもの、4xx (404 など)、タイムアウト、接続エラー・名前解決の失敗、feed の取得・解釈の失敗。現に yahoo は User-Agent を付けると 404 を返し、骨子の「429 / 5xx」だけでは止まらない。要求を無駄に出すという害は同じである。
- 拒否の連続に数える (連続失敗は増やさない): 先方が正の Retry-After を付けた 429 / 503。先方の指示は既存の `fetch_attempts` の待ち (最長 24 時間) で守る。そのうえで、短い Retry-After を毎回返す源が 30 分周期のまま取得され続けないよう、拒否の連続にも連続失敗と同じ閾値と段階を当てる (§3.2)。Retry-After が壊れていて読めない 429 / 503 は、Retry-After なしとして連続失敗に数える。
- 数えない: 記事が 0 件の成功 (ページの本文抽出に失敗した、更新の少ない feed など。要求は 1 回通っただけで、健全な源と同じ頻度である)、未知の取得方式 (要求を出していない設定ミスで、従来どおり `news_source_failed` を残す)。

連続失敗が増える取得では拒否の連続は 0 に戻り、拒否の連続が増える取得では連続失敗は変わらず、成功では両方が 0 に戻る。

### 3.2 待機の段階

段階 `stage(k)` は、`k < N (3)` なら待機なし、`k >= N` なら `min(30 分 × 2^(k−N+1), 24 時間)`。計算は `k` を `N + 10` で頭打ちにしてから行う (壊れた巨大な値で計算が壊れない)。連続失敗 `f` が増えた取得は `stage(f)` を、拒否の連続 `s` が増えた取得は `stage(s)` を、それぞれ「その取得を試みた時刻」に足して健康記録の待機期限とする。

| 連続失敗 f (拒否の連続 s も同じ) | 待機 |
|---|---|
| 1, 2 | なし (次の周期で取得) |
| 3 | 1 時間 |
| 4 | 2 時間 |
| 5 / 6 / 7 | 4 / 8 / 16 時間 |
| 8 以上 | 24 時間 (上限) |

元の骨子は「30 分 → 1 時間 → …」だったが、収集周期が 30 分なので 30 分の待機は次の周期にはもう過ぎていて何も間引かない。最初の待機を 1 時間にして 1 周期を飛ばす。待機の最中に成功する経路は無い (取得しないので)。期限が過ぎた最初の周期で 1 回取得し、失敗なら `f` が 1 増えて次の段階へ進む。成功したら連続失敗も期限も消える。

実効待機期限は、健康記録の待機期限と、先方の Retry-After (`fetch_attempts` の待ち) の遅いほうである。Retry-After を受けた取得では、その待ちと段階の待機の遅いほうを守る。たとえば `Retry-After: 1` を毎回返す源でも、拒否の連続が 3 回に達した取得から 1 時間、以後 2 時間、… と間引かれる。

### 3.3 見え方

- 待機中の周期ごとに、期限ごとに 1 回だけ activity に `news_source_skipped` を残す (今の Retry-After の待ちと同じ。同じ期限の間は再度書かない)。文面: `<name>: 連続 <f> 回失敗のため <HH:MM> UTC まで待つ`、先方の指示が実効待機期限を決めているときは `<name>: 先方の指示により <HH:MM> UTC まで待つ`。
- 連続失敗が N 以上の有効な源ごとに、未完手順 (`afx status` と `afx ctl status` の `pending steps`) に 1 行出す。コードは `news_source_failing`、重大度は warn、param は源の名前。文面: `源 <name> が連続で失敗しています。`afx news list` で確認し、不要なら `afx news disable <name>` で止めてください`。成功するか `enable` / `disable` で消える (無効な源は出さない)。
- ニュースの行は他の未完手順の後ろに並べ、個別の行は名前の昇順で最大 5 行まで。6 件目以降は 1 行に畳む (コード `news_source_failing_more`、param は残りの件数、文面: `ほか <n> 件の源も連続で失敗しています。`afx news list` で確認してください`)。未完手順全体の上限 (32 件) に収まらないときは、承認や安全に関わる既存の action の行を優先し、ニュースの行が落ちる。setup record が無い・読めないときの未完手順は setup 側の行が先で、ニュースの行は出ない (`afx news list` で見られる)。
- 健康記録を読めないとき (DB 不調) は step を出さず、他の節を妨げない。
- `afx ctl status` が使う status の `_STATUS_SETUP_CODES` に新コード 2 つを足す。`afx status` (offline) は読取専用の DB 接続で同じ事実を読む。

### 3.4 状態の持ち方

源の健康記録 (JSON): `v` (記録の版、現在 1)、`consecutive_failures`、`retry_after_streak`、`backoff_until` (tz-aware UTC か null)、`reset_at` (`enable` 時刻か null)、`last_outcome` (`ok` / `failed` / `rate_limited`、`enable` 直後の初期化では null で、`list` は `未取得` と出す)、`last_at`、`last_count`、`last_reason` (§2.2 の固定語の元になる短い識別子)。key は `news_source_health.<name>`。未知の field は読んで捨て、書き直すときに落とす。

- 保存は `news_health` が担う。key の接頭辞と名前の文法を検証し、表を直接読み書きする (`fetch_attempts` と同じ流儀)。読み・書き・削除の各関数は、呼び出し側の transaction に参加できる (commit を呼び出し側が持つ) 形も持つ。`enable` はこれで源の更新と同じ transaction に入る。
- 収集器が取得のたびに 1 回書く (成功も含む。`list` の「直近」の正本になる)。書き込みは 1 文のコミットで、書込に失敗しても取得の結果の扱い (RAG への登録、`news_source_failed` の記録) は止めず、技術ログに warning を残す。書き損ねた分は次の取得で追いつく。
- DB なので再起動をまたぐ。メモリだけにすると、再起動のたびに失敗が数え直しになり、再起動の連打がそのまま先方への連打になる。
- 待機の判定は取得の直前に 1 回読む。読めた値は全 field を型・範囲・tz まで検証する (bool、負値、文字列、巨大な整数、tz なしの日時は壊れた値)。壊れた値は「待機なし」として扱い、その取得の結果で書き直す (取得は 1 回で、書き直した後は通常の段階に戻る)。時計の巻き戻しなどで `backoff_until` が現在から 24 時間より先なら、その場で `現在 + 24 時間` に直して保存し、以後その値で判定する (読み直すたびに期限が先へ滑らない)。
- 健康記録または Retry-After の待ちを**読めなかった**とき (DB の例外) は、その源のその周期の取得を見送る。ニュースは取引に必須のデータではなく、永続層の不調時に先方へ取得を続ける理由が無い。`news_source_skipped` は書かず、技術ログに warning を残す。
- `news_source_skipped` の「期限ごとに 1 回」は、収集器の実行中のメモリで持つ (今の実装と同じ)。再起動で同じ期限が 1 行増えうるが、再起動 1 回につき 1 行なので許す。

### 3.5 User-Agent

`feed` / `web` の取得はどちらも、固定の `User-Agent: agentic-fx/<版> (+<Repository の URL>)` を付ける。版と URL は、実行時にインストール済みの配布物の metadata (`importlib.metadata` の `Version` と、`Project-URL` の `Repository`) から読んで組み立てる。URL の正本は `pyproject.toml` の `[project.urls]` に 1 回だけで (§7 の裁定 1)、metadata はそこから作られる。wheel でも editable でも、どの cwd でも動く。他の場所に文字列で複製しない。ブラウザを装う値は使わない。metadata が読めないとき (開発の特殊な環境) は、URL の部分を省いた `agentic-fx/<版 または unknown>` にする (httpx の既定の値には戻さない)。定義は 1 箇所に置き、`afx news add` / `test` の取得も同じ関数を通る (収集器と試験取得で挙動が分かれない)。

### 3.6 外向きリクエストの予算

- 収集器: 取得が失敗し続ける源は、従来の 1 日約 48 回から、N 回の連続失敗後は 1 時間 → 2 → 4 → 8 → 16 → 24 時間間隔へ減る。周期の起点から数えて最初の 24 時間は 7 回 (0、0.5、1、2、4、8、16 時間)、以降は 24 時間に 1 回以下 (周期の格子の都合で最大 30 分ずれる)。Retry-After を毎回付ける源も同じ上限に収束する (§3.2)。成功する源は変わらない。
- `add` と `test` は利用者が 1 回ずつ起こす取得で、内部で再試行も繰り返しもしない。収集器の予算や健康記録には数えない (`test` は何も書かない)。`add` を入力ミスや再実行で繰り返す量は利用者の操作の量で、製品は制限しない。同じ名前・URL の再実行は取得の前に重複で止まる。
- 取得の戻り値は変えない (記事の一覧だけ)。`test` が成功時に HTTP の状態を表示しないのは、1 回の応答を共有する接口を足さずに、取得を 2 回に増やさないため。

## 4. コマンド面への組み込み

- `cli_surface.py` の `news` の planned record (`planned=True, implemented=False`) は削除し、`ParentRecord("news", "市場ニュース源を扱う")` を `history` と同様に足す。root 自体の `CommandRecord` は持たない (親は操作を持たない)。
- 葉 record を 5 本足す (list / add / enable / disable / test): `news list` `[読取]`、`news test` `[読取]`、`news add` `[変更]`、`news enable` `[変更]`、`news disable` `[変更]`。`add` は 0 件のときだけ確認するので `confirm=False` とし、condition に `0 件のとき確認` を書く。`add` / `enable` / `disable` の condition は `次の収集周期で反映`。help の文言は `cli_surface.py` の accessor だけから取る (cli-surface 設計 §3.1)。parser 側に str literal の help を書かない。
- 振り分けの変更: `entry.py` の `_BACKTEST_COMMANDS` に `news` を足し、`backtest/cli.py` の `dispatch` が `news_cli` へ渡す。これが無いと parser は通っても daemon 起動側へ落ちる。
- 正式な local 親 command と argparse の一致 test の例外集合から `news` を外す (planned の例外は消える)。cli-surface 設計の AC-27 は本書の AC-14 で置き換え、変更履歴に残す。
- `afx --help` の全文 golden (cli-surface 設計 §3.2) と `afx news --help` の golden を、I1 と同じ条件 (幅 100、色の環境変数を消す、非 TTY の capture、`color=False` は 3.14 以上だけ) で**実装の実出力から再生成**し、§3.2 に写す。手で書かない。変わるのは「市場入力を整える」群の行 (`history` → `history / news`)、左右レーンの要約表、`news` の下位操作の説明 (`afx news --help`)。`afx ctl help` は変わらない。
- 原則の表 (`docs/operations/ops-api-2026-10-09.md` の「CLI の置き場の原則」) の「市場入力を整える」行を、`afx` 列を `[変更]` `afx history import` / `[読取]` `afx history compare`・`coverage` / `[読取]` `afx news list`・`test` / `[変更]` `afx news add`・`enable`・`disable` に更新する。`afx ctl` 列は「—」のまま。cli-surface 設計の分類表 (§2.1、`afx news …` の行)、§3.4、§0.1 の後続束の記述、変更履歴を同じ commit で更新する。doc test (c) の正式名 9 つは変えない (news は含まれない)。
- 操作 API には endpoint を足さない。`ENDPOINTS` の個数 pin (29) は変わらない。

## 5. 受入

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | 実機: 本機の yahoo-finance-topstories を `afx news disable` で止めると、activity に `news_source_disabled` が 1 行出る。次の収集周期から `news_source_failed` の同源の行が出ない。`afx news list` が `無効` を示す。この版に上げた直後は健康記録が無く直近は `未取得` で、新しい収集器が 1 回失敗を記録した後は最後の失敗が出る (止める前に 1 周期観測する)。`enable` で戻せる (戻したら止め直す) | 本機の activity、`afx news list`、2 周期分の観測 |
| AC-2 | 連続失敗の境界 (FakeClock と stub fetch の収集器 test)。f=2 では待機が付かず次の周期で取得する。f=3 の失敗で 1 時間、f=4 で 2 時間、f=7 で 16 時間 (上限の直前)、f=8 で 24 時間 (計算上は 32 時間が上限に当たる最初)、f=9 も 24 時間。壊れた巨大な `consecutive_failures` (10^9) でも例外なく 24 時間 | 期限の値の直接比較 |
| AC-3 | 待機中の周期は取得 0 回、期限ごとに `news_source_skipped` が 1 行 (周期を 2 回回しても 1 行)。期限を 1 秒過ぎた最初の周期で 1 回取得する。成功で連続失敗と拒否の連続と期限が消え、以後の失敗は f=1 から数える | 収集器 test、activity の行数 |
| AC-4 | 数える失敗と数えない失敗。連続失敗に数える: 429 / 5xx (Retry-After なし、または読めない・0・負・過去)、404 などの 4xx、タイムアウト、接続エラー、名前解決の失敗。拒否の連続に数える (連続失敗は不変): 正の Retry-After 付きの 429 / 503。数えない: 0 件の成功、未知の取得方式。`Retry-After: 1` / `1799` / `1800` / `1801` / HTTP 日付を毎周期返す源で、3 回目の取得から 1 時間・2 時間と間引かれ、取得回数が AC-2 の表に収束する。拒否の連続の途中で他種の失敗か成功が入ると拒否の連続が 0 に戻る | 例外種の直積 test、取得回数の spy |
| AC-5 | 再起動をまたぐ。f=3 で待機中に収集器を作り直しても待機が続き、取得 0 回。健康記録が壊れた値 (JSON でない、bool、負値、文字列、tz なしの日時) なら待機なしとして 1 回取得し、書き直す。時計を巻き戻して `backoff_until` が 24 時間より先になった記録は、1 回目の判定で現在 + 24 時間に保存し直され、2 回目の判定で期限が先へ滑らない。健康記録または Retry-After の待ちの読取が例外の周期は、その源を取得 0 回で見送る (継続して失敗しても 0 回)。書込の例外では取得の結果の扱い (RAG 登録、失敗の記録) が止まらない | 収集器の再生成 test、fault injection、FakeClock の巻き戻し |
| AC-6 | `feed` / `web` のすべての外向き取得 (収集器、`add`、`test`) に `User-Agent: agentic-fx/<版> (+<URL>)` が付く。python-httpx 既定の値は出ない。版と URL は installed metadata から読み、pyproject の `version` と `[project.urls]` `Repository` に一致する (pyproject を読む test と installed 環境の test で pin。コード側に URL の文字列リテラルが無い)。metadata に `Repository` が無い環境では URL 部分を欠いた値になり、httpx 既定には戻らない | httpx の送信記録 (mock transport)、pyproject との照合 test |
| AC-7 | `list` と `test` は実行前後で DB (全 table の論理 dump と `alert_state`)、activity log、RAG ディレクトリが一致する。daemon を止めた状態の fixture で、DB は全 table の行の dump で比べる (WAL の sidecar の byte は比べない)。DB が無いとき `list` は DB を作らず rc=1 | before/after の tree と DB の dump の比較 |
| AC-8 | rc 規則。名前・URL・`--fetcher` の文法違反 (fragment を含む)・使い方の誤りは rc=2 で、取得 0 回・DB 書込 0 回。源が無い・重複・取得失敗・確認拒否・非対話で `--yes` 無しは rc=1。冪等な `enable` / `disable` は rc=0 で activity を増やさない。activity の書込が失敗しても DB の変更は成功し rc=0、技術ログに warning | CLI test、activity の fault injection |
| AC-9 | `add` の取得は 1 回だけで再試行しない。0 件は確認、`y` 以外は追加しない。`--yes` で 0 件を通すが、取得失敗は通らない。件数 1 以上は確認しない。名前または URL の重複 (scheme・host の大文字小文字と既定 port の違いを含む) は取得の前に rc=1 | 取得回数の spy、DB |
| AC-10 | 名前の文法 (先頭 1 文字・最大 40 文字の L-1 / L / L+1)、URL の scheme と userinfo と fragment と 2048 文字の境界 | 境界値 test |
| AC-11 | `enable` は源の更新と健康記録の初期化を 1 transaction で行い (途中で落とすと両方戻る)、連続失敗と待機を消す。先方の Retry-After の待ち (`fetch_attempts`) は消さない。`disable` は健康記録を残す。有効のままの源には触らない。activity には URL が出ない | DB と activity の比較、transaction の fault injection |
| AC-12 | status: 連続失敗 N 以上の有効な源ごとに `afx status` と `afx ctl status` の未完手順に `news_source_failing` が出る。N-1、無効な源、成功後は出ない。6 件以上では 5 行 + `news_source_failing_more` 1 行 (残り件数が正しい)。既存の action の行が 32 件近くあるときは action の行が残りニュースの行が落ちる。setup record が無いときはニュースの行が出ない。DB が読めないときは step を出さず他の節は表示する | 両経路の stdout、31 / 32 / 33 件の境界 |
| AC-13 | 同時実行 (2 process の test)。収集器が書いている DB に `add` / `enable` が成功する。2 本の同名 `add` は片方だけ成功し、先方への取得は各 process 1 回以下。`busy_timeout` 超過は rc=1 で変更が残らない。instance lock を取らない (daemon が lock 保持中でも動く)。収集器が HTTP の最中に `disable` → `enable` が入ると、古い取得の結果は書かれず、`enable` 直後の健康記録は初期化のまま (HTTP の前・最中・書込の直前を barrier で止める) | 2 process の test、barrier、HTTP spy、flock 記録 |
| AC-14 | `news` は ParentRecord で、5 本の葉 record が [読取] / [変更] の分類と accessor 由来の help を持つ。planned の `news` record と例外 pin は消える。`entry.py` が `afx news …` を `news_cli` に振り分ける (daemon 起動に落ちない) test がある。`afx --help` と `afx news --help` が実出力の golden と byte 一致する。ops-api の原則表と cli-surface 設計に news が出る。doc test (c) の 9 名は不変 | golden、一致 test、dispatch test、文書 grep pin |
| AC-15 | 経路の遮断: `ENDPOINTS` の個数が 29 のまま、news 系の code が無い。`afx ctl news …` は parse できない。改善 agent の tool registry と bot / web の module が `news_sources` を変更する経路を持たない (AST で `news_sources.add` / `set_enabled` の呼出し、`news_sources` への `INSERT` / `UPDATE` / `DELETE` の SQL 文字列、遅延 import と alias を禁じる)。`seed_default_sources` は存在しない (AC-16)。無効化した源は、起動でも `afx init` の再実行でも再有効化・再追加されない | AST / grep pin、parse test、起動と `afx init` の再実行 test |
| AC-16 | 既定源の廃止。(a) 空の root で、ニュース源を 1 件も入力せずに `afx init` を通すと `news_sources` は 0 行で、その後の起動も `news_source_*` の activity を 1 行も書かない。(b) `seed_default_sources` と `DEFAULT_SOURCES` と `datafeed/default_sources.py` が存在しない (`hasattr` と `src/` の grep で pin)。(c) ウィザードの入力で打った源がちょうどその件数だけ登録される (文法違反・取得失敗・重複は登録されない、0 件は確認、空 Enter で終了)。副段は bridge の分岐 (使う・使わない・飛ばす) のどれからでも 1 度通る。段を戻って再実行しても保持した件が二重登録も取り直しもされない。取得の後に別 process が同じ名前を INSERT した場合、確定は止まらずその件だけ飛ばされて通知される。2 件入力して 2 件目の INSERT を失敗させると、`news_sources` が 0 行・scheduled period が未消費・marker が残る。(d) 既存 DB の登録済み 3 源は起動後も変わらない。(e) ウィザードの say 行の変更に合わせ、受入手順書の引用 pin (`tests/acceptance/test_first_run_acceptance_doc.py`、`test_first_run_scripts_replay.py`) と台本を更新する。(f) 基本設計 `2026-07-25-agentic-fx-design.md` に、基本ニュースソースの同梱・agent 追加時の `news_source` approval・`POST /news` が現行の記述として残らない (廃止注記つきの記述は可。grep pin) | 空 root の init test、起動 test、hasattr / grep pin、ウィザードの台本 test、受入手順書の pin |
| AC-17 | URL の秘密 (§2.1.1)。query に値を持つ canary URL の源を登録し、`add` / `list` / `test` / 収集器の成功・失敗・Retry-After・feed の timezone 警告・`web` の題名なし記事・`enable` / `disable`・wizard の入力を通す。canary の値と path・query が、stdout / stderr、activity、技術ログ、RAG の記事 (URL と題名)、`afx status` / `afx ctl status` の出力、setup_record のどこにも現れない (DB の `news_sources.url` だけが原文を持つ)。`list` は `<scheme>://<host>/…` までしか出さない | canary の全経路 grep、`news_sources` 以外の全 table の dump |

## 6. 変更一覧と task 分割

| 区分 | file | 内容 |
|---|---|---|
| 新規 | `datafeed/user_agent.py` | 固定 UA の組み立て 1 箇所 (installed metadata の版 + `Repository` URL) |
| 変更 | `pyproject.toml` | `[project.urls]` に `Repository = "https://github.com/teru358/agentic-fx"` を足す (現状は無い) |
| 新規 | `store/news_health.py` | 源の健康記録の読み書き (接頭辞と名前の検証、`alert_state` の表を直接操作、commit を呼び出し側が持てる形)、検証、段階計算、実効待機期限 (`fetch_attempts` と合成) |
| 変更 | `store/news_sources.py` | `set_enabled` と追加に、commit を呼び出し側が持てる形 (`enable` の同一 transaction 用) と、URL の正規化を足す |
| 変更 | `datafeed/fetchers.py` | 両 `httpx.get` に UA。feed の日時警告から URL を外し源の名前にする。`web` の記事 URL から query・fragment を落とし、題名の代わりに URL を使わず源の名前にする |
| 変更 | `datafeed/news_collector.py` | 失敗の分類 (連続失敗と拒否の連続)、段階待機、健康記録 (取得前の読取、`reset_at` による古い結果の破棄、読取失敗時の見送り)、`news_source_skipped` の文面 |
| 変更 | `setup/pending_steps.py` `pending_facts.py` `pending_text.py` `service.py` `status.py` | `news_source_failing` と `news_source_failing_more`、読取、並びと上限 |
| 新規 | `news_cli.py` | list / add / enable / disable / test |
| 変更 | `entry.py` `backtest/cli.py` (parser と dispatch) `cli_surface.py` | `_BACKTEST_COMMANDS` に `news`、親と葉 5 本、planned の record 削除 |
| 変更 | cli-surface 設計 §2.1 / §3.2 / §3.4 / §9 AC-27 / 履歴、ops-api 原則表 | 文書 |
| 削除 | `datafeed/default_sources.py` | `DEFAULT_SOURCES` の 3 源。`news_collector.py` の import と `__all__` から外す |
| 変更 | `datafeed/news_collector.py` | `seed_default_sources` を削除 |
| 変更 | `service.py` (2 箇所の起動時 seed と `基本ニュースソースを N 件登録しました` の出力)、`setup/wizard.py` (段 10 の `seed_default_sources` 呼び出しを、入力された源の INSERT に置換。段 3 の出口を 1 つにそろえて副段を追加)、初回ウィザード設計の段 3 / 段 10 / 範囲外の記述 | 起動時の自動登録の除去とウィザードの入力式 |
| 変更 | 基本設計 `2026-07-25-agentic-fx-design.md` | 基本ニュースソースの同梱、agent 追加時の approval、`news add <url>` 記述、`POST /news` を現行の裁定 (2026-09-07、2026-10-25) に合わせて直し、廃止の注記を付ける |
| 変更 | README、`docs/operations/first-run-acceptance.md` | 「ニュース源の候補例」(§8) と注意 |
| 変更 | `config/settings.yaml.example` | 変更なし (設定キーを足さない。`schema_version` も変えない) |
| 変更 | チケット | 起票時の骨子を状態に反映 |

- **T1 収集器 (UA + 健康記録 + バックオフ + status の step)**: 純 Python。コード約 400 行、test 約 600 行。codex (terra) 向き。AC-2〜6、12 (status 側)。T1 は CLI からも使う契約 (`news_health` の API、実効待機期限、`reset_at`、URL 正規化、fetchers の URL 非露出) を先に確定する。
- **T2 CLI + コマンド面 + 文書**: 純 Python と文書。README と `docs/operations/first-run-acceptance.md` に §8 の注意の段落と「ニュース源の候補例」の表を足す。既定源の廃止 (`default_sources.py` と `seed_default_sources` の削除、起動時 seed の除去)、`afx init` の入力式の副段 (§2.7)、受入手順書の引用 pin と台本の更新を含む。AC-7〜11、13〜17 を所有する。コード約 500 行、test 約 750 行、golden の再生成は指揮者が実出力で確認。T1 の健康記録の読み書き (`list` の「直近」、`enable` の初期化) に依存するので T1 の後。AC-1 は両方が main に入った後の本機確認で、指揮者が行う。
- **既定源を pin している既存 test (T2 で更新または削除)**:
  - 削除: `tests/datafeed/test_news_collector.py` の seed 系 (`seed_default_sources` の冪等・DEFAULT_SOURCES の各フィールド・名前 / url 重複・1 件登録済みの 5 本前後)、`tests/store/test_news_sources.py::test_seed_default_sources_commit_flag_defaults_to_committing`。
  - 置換: `tests/setup/test_wizard_commit.py` の `seed_default_sources` を monkeypatch する失敗注入は、2 件入力して 2 件目の INSERT を失敗させる注入に置き換える (AC-16(c))。
  - 更新: `tests/test_init_and_guard.py` (行 12 の import、350・355・416 付近の「基本ニュースソースを N 件登録しました」と件数の pin。0 行の pin に反転)、`tests/test_service_app.py` (行 21・362〜374 の `default sources` の activity 行と件数の pin。起動が seed しない pin に反転)、`tests/core/test_app_conn_signal.py` (行 270 の `agentic_fx.service.seed_default_sources` の monkeypatch を除去)。
  - 追加: AC-16 の各 test、ウィザードの副段の台本 test。
- 周回: 境界コードではない (資金・隔離・鍵・削除に触れない) が、DB 書込と外向き要求と URL の秘密の扱いを含む。codex 1 周目 → 閉鎖確認。変異スイープは段階計算の境界、カウント規則、拒否の連続、`reset_at` の比較、URL の出力経路を対象にする。

## 7. 裁定 (2026-10-25、ユーザー: 「すべて推奨で」)

技術で決められるもの (状態の置き場所、数えるエラー種、`enable` が記録を消す、確認の仕方) は本書で決めた。次の 9 点は製品・運用の判断で、すべて推奨どおりに決まった (8・9 は 2026-10-25 の設計レビュー 1 周目で分離したもの)。

1. **User-Agent と repo URL**: `agentic-fx/<版> (+https://github.com/teru358/agentic-fx)`。URL は `pyproject.toml` の `[project.urls]` (key `Repository`) に 1 回だけ書き、UA は installed metadata の版とその URL から組み立てる。他の場所にリテラルとして複製しない。UA を pyproject と照合する test を足す (AC-6)。裁定 (2026-10-25、ユーザー): 推奨どおり。
2. **0 件の源の追加**: 確認のうえ追加を許す (非対話は `--yes` 必須)。更新が少ない feed を足せなくなるのを避ける。0 件が続けば `list` に出る。裁定 (2026-10-25、ユーザー): 推奨どおり。
3. **待機の上限**: 24 時間。長くすると回復に気づけない。上限に達した源は `afx news disable` を案内する step が残り続ける。裁定 (2026-10-25、ユーザー): 推奨どおり。
4. **閾値 N**: 3。一時障害を「連続」と数えて止めない一方、1.5 時間で間引きが始まる。裁定 (2026-10-25、ユーザー): 推奨どおり。
5. **最初の待機**: 1 時間 (§3.2)。骨子の 30 分は周期が 30 分なので何も間引かない。骨子の文面とは異なることを承知のうえの裁定。裁定 (2026-10-25、ユーザー): 推奨どおり。
6. **`test` を待機中の源に対して許す**: 許す (利用者の明示した 1 回で、書き込みも無い。禁止すると原因調査ができない)。裁定 (2026-10-25、ユーザー): 推奨どおり。
7. **既定源の同梱廃止と `afx init` の入力式**: 同梱をやめ、`afx init` で名前と URL を入力式にする (空 Enter で飛ばす、後から `afx news add`)。候補例は README と受入手順書に注記つきで載せる (yahoo は除く)。既存 DB の登録済みの源は消さない。裁定 (2026-10-25、ユーザー)。
8. **鍵を含む URL の扱い** (レビュー 1 周目 C-2): 受理し、取得以外の経路に出さない (§2.1.1)。URL は標準入力でも渡せる。DB の原文と引数経由の shell 履歴は利用者の責任 (§8)。拒否案は鍵らしさの判定が回避可能で、正当な query 付き URL も落とすので採らない。裁定済 (2026-10-25、推奨案)。
9. **`disable` → `enable` による段階待機の解除**: 許す (人間の運用操作を信頼境界とする)。先方の Retry-After の待ちは解かない。残余として §8 に明記する。裁定済 (2026-10-25、推奨案)。

## 8. 利用者の責任と文書

- ニュース源を足すかどうかは運用者 (利用者) の判断である。各源の利用規約・robots の確認は運用者の責任で、製品は代わって確認も保証もしない。
- 既定源の同梱は廃止する (裁定 2026-10-25、チケット `news-default-sources-to-user-input`)。従来の 3 源 (`yahoo-finance-topstories` / `nhk-keizai` / `fxstreet-news`) のうち 2 本は商用サイトの feed で、源は利用者の判断、規約の確認は利用者という原則と合わなかった。yahoo-finance-topstories は 2026-09-02 から停止している。`default_sources.py` と `seed_default_sources` を削除し、`afx init` は名前と URL の入力式にする (§2.7)。`config/settings.yaml.example` の `news:` は `cleanup_hours` のみで、源は持たない。
- 既存の DB に登録済みの 3 源は消さない。利用者が `afx news disable` で止める (削除は将来の `remove`)。
- README と受入手順書に「ニュース源の候補例」の表を載せる。取得方式はどちらも `feed`。載せるのは例であって推奨でも保証でもなく、各源の利用規約の確認は運用者の責任である旨を表の直下に書く。yahoo-finance-topstories は源が 2026-09-02 から停止しているので載せない。

  | 名前 | URL | 取得方式 |
  |---|---|---|
  | `nhk-keizai` | `https://www3.nhk.or.jp/rss/news/cat5.xml` | `feed` |
  | `fxstreet-news` | `https://www.fxstreet.com/rss/news` | `feed` |
- UA の URL は先方が連絡できる窓口で、責任の引き受けを意味しない。
- 鍵を含む URL の源: 製品は URL を取得以外の経路に出さない (§2.1.1)。ただし DB に原文が平文で残ること、引数で渡した URL が shell の履歴に残ること (標準入力で渡せば避けられる)、path に鍵を置く URL が `web` 方式で RAG に出ることは、利用者の環境と判断の事柄で、運用者の責任とする。
- 既知の残余: `disable` → `enable` を繰り返せば段階待機を恒久的に回避できる (先方の Retry-After は回避できない)。これは人間の運用操作に限られ、製品は信頼境界を人間の操作に置く。同じ相手先への複数の源の登録も運用者の判断である。
- T2 で README と `docs/operations/first-run-acceptance.md` に各 1 段落を足す: ニュース源は運用者の判断で追加すること、各源の利用規約の確認は運用者の責任であること、UA の URL は連絡先であること、鍵を含む URL の扱い。
- 免責の法的根拠は LICENSE (MIT) の無保証条項。リポジトリ直下に `LICENSE` が在り、`pyproject.toml` も `license = "MIT"` / `license-files = ["LICENSE"]` で参照している。新規作成は不要。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `news-source-cli-and-429-backoff` の骨子の設計化 | |
| 2026-10-25 | v0.2 | 裁定 6 点の反映と利用者責任の節 | ユーザー裁定「すべて推奨で」 | |
| 2026-10-25 | v0.3 | 既定源の同梱廃止と `afx init` の入力式 (§0、§2.7、§5 AC-15・AC-16、§6、§7 裁定 7、§8)、README の候補例 | ユーザー裁定 2026-10-25、チケット `news-default-sources-to-user-input` | |
| 2026-10-25 | v0.4 | 拒否の連続 (Retry-After 連打の間引き、§3.1・§3.2)、URL の秘密の全経路 (§2.1.1、AC-17)、fragment 拒否と URL 正規化、健康記録の置き場と `reset_at` による競合防止、実効待機期限、DB 不調時の見送りと時計補正、wizard 副段の出口・再入・確定時再検査、status の並びと上限、`test` の HTTP 表示を除去、UA を installed metadata に、`ParentRecord` への置換と `entry.py`、AC の強化、基本設計の更新、見積り改訂 | 設計レビュー 1 周目 (sol) の指摘 20 件 (採用 16、一部採用 4。詳細は `tmp/review-news/c0-r1-triage.md`) | |

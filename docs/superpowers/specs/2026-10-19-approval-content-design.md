# 承認の中身を見せる設計書

**版:** v1.6 (ユーザー承認済み、実装可)
**対象:** `approval-notice-lacks-change-content` と `bot-event-notices-are-raw-log-lines` の 2 チケットを 1 つの spec で扱う。実装束は 2 つに分ける (§10)。
**位置づけ:** ユーザー裁定 4 問 (§8.1) を反映した確定版。この版から実装に入る。

## 0. 目的、範囲、決定

週 1 回、改善 loop が plugin の候補を作り、人間が Discord で Approve / Reject を押す。いまの承認投稿は「承認 #25 / kind=plugin / digest=...」の 8 行で、**何がどう変わるのか読めないまま押すことになる**。決定後の通知も event の k=v 行がそのまま 3 通届く。この設計は次の 3 つを足す。

1. 承認者が Discord 上で「どの plugin が、新規か更新か、何が変わり、成績はどうか、agent は何と言っているか」を読める要約。
2. 既存の読み取り専用 web の承認 #N ページに、変更内容 (params の差分と 3 ファイルの diff) を出す。新しい UI や新しい公開 route は作らない。
3. 決定・失敗の通知を日本語の文にし、成功しただけの監査 event は既定で出さない。

| 項目 | 決定 |
|---|---|
| 要約の置き場 | Discord = 要約のみ。diff 全文 = 既存 web の承認 #N ページ。CLI = 既存の `afx ctl approval <id>` に要約が出る (`--diff` は作らない、§11)。 |
| 材料 | 新しい表・列は作らない。変更内容は**読み出しのたびに版 dir から safe opener で安全に読んで計算し、保存しない** (§2)。 |
| 出力境界 | 表示する全ての値は共通の redactor を通す。検出できないものは全文を伏せる (fail-closed、§7.1)。redactor が保証するのは、正規化後の各表示値に**字面として現れる**絶対パス、URL、hostname、IP、credential、token、口座数字の伏せ字である。Python の評価や定数畳み込みは行わず、複数行・複数リテラル・f-string 式・`chr()`・bytes・各種 encoding から再構成できる値は **diff に限り保証対象外**とし、単一利用者の loopback 運用における残余リスクとしてユーザーが受容した (§7.1、§8.1)。自由文 (summary / rationale / config 値 / parent_facts / reason / CLI) は従来どおり全保証である。上限は用途別に 3 種の名前で分け、値は §2.7 の 1 表に集約する。 |
| API | `approval_detail` に `change` 要約を足し、diff 本文は daemon の別 endpoint (`GET /v1/approvals/{id}/change`) で返す。公開 web は既存の 1 ページに統合する。`payload_sha256` の契約 (payload 不変) は変えない (§5)。 |
| 責務の分離 | daemon = 安全な読み取り・hash 検証・redaction・表示形式に依存しない構造化 diff。表示形式ごとの escape と最終上限は web / Discord / CLI がそれぞれ持つ (§4.3、§5)。 |
| 自己申告 | agent の summary は Discord と web の両方に出す (§8.1 問 1)。「agent の申告 (未検証)」の見出しを分け、事実欄と混ぜない。`selection_rationale` は web のみ。 |
| holdout | **web のみ** (§8.1 問 2)。Discord は in-sample だけ。Discord に出す設定 key は作らない。 |
| 遮断 | 変更要約を `last_result`・backlog・改善プロンプトに書かない。bot state・web cache・ログ・event にも残さない。holdout 数値は改善 agent に戻る経路を作らない (§7)。 |
| 決定者 | v0.4 は名前なし (「承認・配備されました」「却下されました」)。actor 表示は後続 (§6.3、§11)。 |
| 実装 | 1 つの spec、2 つの実装束: T1 → T2 (変更表示) と T3 (event 通知、小さく先行可) (§10)。 |

## 1. 用語と非目標

| 用語 | 意味 |
|---|---|
| 承認投稿 | 承認依頼 1 件につき Discord に 1 つ立つメッセージ (ボタン付き)。承認依頼の一覧を見て bot が投稿する。event では立たない。 |
| event 通知 | 決定・job・guard などの event を bot が日本語にして投稿するもの。承認の決定時にしか出ない。 |
| 変更要約 | 承認 #N の plugin 名、kind、分類、params の差分、成績、申告を 1 つにまとめた構造。読み出し時に計算する。 |
| 分類 | 候補と前版の関係。API の値 (machine enum) は `new` / `updated` / `same` / `unknown` の 4 つに固定し、表示 label は 1 つずつ: 新規 / 更新 / 同一版 / 不明。説明の括弧書き (「前版なし」など) は label ではなく補足文。 |
| 変更 diff | 前版と候補版の 3 ファイル (`plugin.py` / `config.yaml` / `test_plugin.py`) の diff。daemon は表示形式に依存しない構造 (ファイルごとの hunk の文字列、escape しない) で返し、各表示面が整形する。 |
| 申告 | agent が書いた `summary` / `selection_rationale`。親が計測した事実ではない。 |
| 比較対象 | 更新のときの前版 (live の symlink が指す版 dir、確定済みなら配備ジャーナルの旧版)。 |
| redactor | 表示する全ての値 (summary / rationale / config の key・value / diff の全行 / CLI 出力) に共通で掛ける 1 つの検出と伏せ字の部品 (§7.1)。 |
| safe opener | version / staging / archive / human の 4 種で共通に使う、root から component ごとに `openat` + `O_NOFOLLOW` で辿って開く読み取り部品。directory と 3 ファイルの性質・サイズ・hash の検査までを同じ中で行う (§2.4)。 |
| coherent read | 3 ファイルを 1 回の読み取りとして一貫して読み、読み終えた全 bytes から `artifact_hash` を再計算して payload と照合するまでを 1 単位とする読み方 (§2.4)。 |
| 入力読み取り上限 | DoS 防止のため、ディスクや API から読み込む量に掛ける byte の上限。表示の長さとは無関係 (§2.7)。 |
| redaction 対象上限 | redactor に渡す 1 値の byte の上限。超える値は切らずに全文を伏せる (切ると検出が崩れるため、§7.1)。 |
| 表示上限 | 利用者に見せる完成した出力に最後に掛ける上限。**escape 後**の文字数または byte で数える。80 字・300 字・2000 字・64 KiB はすべてこれ (§2.7)。 |
| reason code | 内容を表示できない理由を表す固定の列挙値。固定文との対応と、条件ごとの粒度 (change 全体か、ファイルか、値か) は §5 に置く。 |
| quota | diff の表示 budget (48 KiB) を 3 ファイルへ割り当てるときの、ファイルごとの割当 byte。出力量の保証ではない (§2.7)。 |
| exact archive | `(mission_id, payload.artifact_hash)` に一致する archive だけ。`content_hash` 一致での代用は認めない (§2.1)。 |

非目標: 履歴一覧ページ、diff の色分け、Discord のスレッド分割、却下理由の表示、CLI の `--diff`、他候補との対比、前版の成績の並記、承認ボタンの挙動変更、notify (webhook) の文面変更 (固定 schema のまま)、決定者の名前表示、画面を作った後に比較対象が変わったことの検知 (stale、§2.5)。

## 2. データと材料

### 2.1 既にあるもの

| 材料 | 所在 | 備考 |
|---|---|---|
| plugin 名・kind (strategy / indicator / signal) | 承認 payload の `name` / `kind` | 現在の `approval_detail` は返していない。返すだけでよい。 |
| 成績 (in-sample / holdout) | `approval_detail` が既に返す | trades・PF・avg R を表示用に整える。 |
| 申告 | `approval_detail` の `agent_claims` | 保存時に各 2000 字で切断済み (切断印が付く)。bot と web の allowlist が捨てているだけ。 |
| 候補版の bytes | pending = staging、承認済み = 版 dir、却下 = exact archive | 下記。 |
| 前版の bytes | pending = live symlink の指す版 dir、確定済み = 配備ジャーナルの旧版 | 更新のときだけ。 |
| 配備ジャーナルの旧版情報 | 切替ジャーナルの `old_kind` / `old_target` | 確定後の分類の判定に使う。 |

候補版の bytes の所在は、実際の遷移に合わせて次のとおりとする。

- archive は改善 mission の publish 時点で**事前に作られる** (`plugins/_archive/<mission_id>/<artifact_hash>`)。承認 / 却下のときに staging から archive へ移る遷移は無い。approve も reject も staging を削除する。
- pending の間は staging を読む。承認済みは版 dir を読む。
- 却下は、`(mission_id, payload.artifact_hash)` に一致する exact archive だけを読む。self-test の書き換えにより `content_hash` が同じでも `artifact_hash` は異なり得るので、`content_hash` 一致での代用 (fallback) は禁止する。
- exact archive が無ければ reason code `unavailable` (「変更内容を取得できません」) を返す (エラーにしない)。
- どの所在から読んだ場合も、§2.4 の hash 再検証を通ったものだけを表示する。

### 2.2 読み出し時に計算するもの

値の処理順は常に **入力読み取り上限 → Cc/Cf/Cs 除去 → redaction (対象上限を超える値は全文を伏せる) → 表示形式の escape → 表示上限** とする (§7.1)。下表の「80 字」などはすべて最後の表示上限であり、escape 後に数える。

| 計算 | 規則 |
|---|---|
| 分類 | 次の 4 つに分ける (API 値 / 表示 label)。`new` / 新規 = 同名の live が不在 (前版なし)。`updated` / 更新 = live が symlink で、その target が §2.4 の完全一致検査を通り、指す版の `artifact_hash` が候補と異なる (`content_hash` が同じで test だけ違う場合も更新とし、test の差分を出す)。`same` / 同一版 = 前版と候補の `artifact_hash` が等しい (変更なし)。`unknown` / 不明 = live が plain dir、または前版を特定できない (旧形式の配備)、または live の target が §2.4 の完全一致検査に通らない (この場合は reason code `unsafe_source` を併記する)。`unknown` のときは比較できないので、params の差分と diff は出さず、候補の hash・成績・申告だけを出す。 |
| 確定後の分類 | approval に対応する配備ジャーナルのうち、最終の `phase='decided'` row の `old_kind` / `old_target` を使う。`old_target` は live の target と同じ完全一致検査 (§2.4 の 5) を通ったものだけ baseline に使う。通らない、または同一 approval に複数 row があり内容が食い違うときは、baseline 不明として `unknown` に倒す (fail-closed。前者は `unsafe_source` を併記)。 |
| params の差分 | 前版と候補の `config.yaml` を読み、`params`・`indicators.*.params`・`pairs`・`timeframe`・`exit_mode`・`max_bars` を key ごとに「旧 → 新」にする。入れ子の値は決定的に flatten する (key は `a.b.c` の連結。最大深さ・最大項目数・差分件数の上限は §2.7)。key は `[A-Za-z0-9_.]{1,64}` に合うものだけ、値は redaction のあと escape し、表示上限 (80 字) で切る。合わない key は件数だけ出す。 |
| diff | 3 ファイル全部をファイルごとに diff にする。新規は全文を「追加」として出す。全行を redactor に通す。表示形式への整形 (色なしの unified diff 表示、escape) は各表示面が行う (§4.3、§5)。 |
| 比較対象 | 画面に前版と候補の `artifact_hash` の先頭と計算時刻を併記する (§2.5)。 |

### 2.3 新しい表や列を作らない理由

変更要約を payload や専用列に保存すると、その値が改善側から読める場所に置かれる可能性が生まれ、漏れ経路が増える (§7)。いま改善側が `approval_requests` を直接読む箇所は無いので、読み出し時の計算なら経路は増えない。承認する bytes と表示した bytes のずれは、§2.4 の表示時再検証と、承認時に `artifact_hash` を再検証する既存の仕組みが止める。diff を payload に固めて digest で束縛する案、承認操作に view token を必須にする案は、`payload_sha256` とボタンの契約を変えるため v0.4 では採らない (§11)。

### 2.4 読み取りの安全規則

承認 payload、ジャーナル、archive、live の symlink の path は永続化された値であり、壊れていれば任意のローカルファイルを読んで Discord / web に返す実装になり得る。既存の `resolve_candidate_dir()` (`src/agentic_fx/plugin/switch.py`) は、staging / human の candidate_path の**字句検査**と最終ディレクトリが symlink でないことの確認だけをする。version / archive の path は扱わず、中間 component の symlink も、root 外への到達も、個々のファイルの開き方も検査しない。よって読み取りの安全はこの関数に頼らず、**version / staging / archive 共通の safe opener を新設**する (T1 の新モジュール)。`resolve_candidate_dir()` は candidate_path の字句検査としてだけ再利用する。daemon は次の規則を満たしたときだけ読む。

1. plugin 名と `artifact_hash` と `mission_id` は完全一致の許可文字列 (既存の名前の形検査と、固定長 64 桁 hex、数値の mission_id) で検証する。通らなければ reason code `unsafe_source`。
2. safe opener は、下表の 3 種の root のいずれかを起点に取る。root 自体も開いたあと directory として `fstat` で確認し (symlink・非 directory は `unsafe_source`)、以後はその dirfd を起点に `openat` + `O_NOFOLLOW` (中間は `O_DIRECTORY` も) で component を 1 つずつ辿る。path 文字列を `resolve()` や `Path.is_dir()` で追従して開くことはしない。
3. **永続化された path 文字列は信用しない。** archive の `archive_path`、staging の `candidate_path`、live の symlink target、journal の `old_target` は、いずれも文字列のまま開かない。検証済みの値から、下表の component を safe opener 自身が構築する。
4. 候補ディレクトリが通常ディレクトリであること、3 ファイルがそれぞれ通常ファイル (symlink・FIFO・デバイス不可) であることを、開いた fd の `fstat` で同じ opener の中で確認する。symlink は 1 つでもあれば拒否する。この拒否と 1・2・3 の拒否は、すべて reason code `unsafe_source` (「内容を検証できません」) にする。
5. **baseline の target 解釈:** live の symlink target と journal の `old_target` は、`.versions/<候補と同名の plugin 名>/<64 桁 hex>` に**完全一致**する場合だけ baseline に使う。一致したら 64 桁 hex を抽出し、その hash と plugin 名から version root の component (下表) を再構築して開く (target の文字列を直接開かない)。一致しない target は baseline に使わず、分類 `unknown` + reason code `unsafe_source` にする (§2.2)。
6. 読み取りは 1 ファイルごとに入力読み取り上限付きとする (§2.7)。上限を超えるファイルは読まず、reason code `too_large` (change 全体、固定文「内容を検証できません」) にする。非 UTF-8 のファイルは `redacted` (§5 の粒度表)。
7. **coherent read:** 3 ファイルを読み終えた後、読んだ全 bytes から `artifact_hash` を再計算し、payload の値と一致したときだけ表示する。不一致なら reason code `hash_mismatch` (固定文「内容を検証できません」) を返し、bytes は一切返さない。hash の検査は opener の中で完結させ、検査を通る前の bytes は呼び出し側へ出さない。
8. 解決した絶対パスを、レスポンス・例外・ログに入れない。失敗は固定の reason code で返す。

T1 の root と component の契約 (現物の配置に一致する):

| 対象 | root (起点) | component (root の下を順に辿る) | 備考 |
|---|---|---|---|
| version (候補の承認済み版、前版) | `plugins/.versions` | `[name, artifact_hash]` | live は `plugins/<name>` → `.versions/<name>/<artifact_hash>` の symlink |
| staging (pending の候補) | `plugins/_staging` | `[mission_id, name]` | mission_id は数値の文字列 |
| archive (却下の候補) | `plugins/_archive` | `[mission_id, artifact_hash]` | root 名の `_archive` は component に含めない。`.tmp-` で始まる作成途中の dir は対象外 |
| human (pending の人間候補、`afx plugin submit --from _human`。承認依頼 payload の `candidate_origin` が `human`) | `plugins/_human` | `[name]` | staging と同じ fstat / `openat` + `O_NOFOLLOW` / 通常ファイル / サイズ / hash の検査。origin が上記 4 種のどれでもない、locator が形に合わない、hash 不一致は `unsafe_source` |

### 2.5 baseline の束縛と残余リスク

- 応答には、候補の `artifact_hash`、比較対象 (baseline) の `artifact_hash`、計算した時刻の 3 値を載せる。画面にはそれぞれ先頭 12 桁と時刻を短く表示する。承認者は表示時点でどの版と比べたかを読める。
- **stale 検知は作らない。** 現行の web は `/approvals/{id}` の query を拒否し (`afx-ops/afx_ops/web/request.py`)、ページは meta refresh するだけで過去の baseline を保持しない。bot state・web cache への保存も禁止している (§7.2)。よって「前回表示の baseline と比べて古さを知らせる」ことの材料が存在しない。query で前回 hash を運ぶ案は §11 に置く。
- **残余リスク (明記):** 候補 C の bytes は payload の hash で束縛されているが、baseline は payload に束縛されていない。ユーザーが A → C の diff を見たあとで live が B に変わると、承認時点の実際の差分は B → C になる。画面の hash と時刻は気付く手がかりを与えるだけで、承認そのものは止めない。digest を payload に足す案や view token の案で閉じられるが、`payload_sha256` とボタンの契約が変わるため v0.4 では採らず、§11 の後続に置く。

### 2.6 見られる期間

承認済みは、新旧の版 dir が保持の対象 (承認済み payload の版・live・未完ジャーナル) なので残る限り見られる。却下は exact archive が残る間だけ見られる (archive は掃除される)。内容が取れないときは reason code `unavailable` (「変更内容を取得できません」) を返し、エラーにしない。

### 2.7 上限の定数表

上限は次の 3 種に分けて定義する。値はすべて 1 か所のここで持ち、実装は定数 1 つずつ、test は境界の前後を観測する (§9)。

**入力読み取り上限 (byte、DoS 防止。表示の長さではない):**

| 定数 | 値 | 超えたとき |
|---|---|---|
| 1 ファイルの読み取り上限 | 256 KiB | 読まず `too_large` (change 全体。§5) |
| flatten の最大深さ | 4 | 超えた入れ子は 1 つの項目の件数に数える |
| flatten の最大項目数 | 64 | 旧・新の key の和集合を key 順に並べた先頭 64 件を両側に当て、超えた分は「比較していない key」の件数にする |
| 行単位 diff の計算量 `DIFF_MAX_CELLS` | 旧の行数 x 新の行数 = 4,000,000 (2000 x 2000) | 行ごとの比較を省いて全行削除 + 全行追加の 1 hunk にし、file に `diff_degraded: too_many_lines` を付ける。web は「差分の計算を省略 (行数が多いため)」を出す。1 側 20000 行 (`DIFF_MAX_LINES`) を超える file も同じ |
| API 応答の上限 (diff 本文の合計) | 512 KiB | hunk を行単位で末尾から省略し「…(N 行省略)」の件数を構造に載せる |

**redaction 対象上限 (redactor に渡す 1 値の byte):**

| 定数 | 値 | 超えたとき |
|---|---|---|
| 1 値 (params の key・value、summary、rationale) | 8 KiB | 切らず、その値だけ全文を伏せる (`[redacted]`、他の値は出す。§5) |
| diff の 1 ファイル | 読み取り上限と同じ (256 KiB) | 読み取り段階で `too_large` (change 全体。§5) |

**表示上限 (escape 後。利用者に見せる完成した出力):**

| 定数 | 値 | 面 |
|---|---|---|
| params の 1 value | 80 字 | Discord / web / CLI |
| params 差分の件数 | API 応答に 64 件まで。Discord は 10 行まで、超えた分は「ほか N 件」 | API / Discord |
| summary | 300 字 | Discord |
| summary / rationale | 各 2000 字 | web |
| Discord の完成本文 | 2000 字 | Discord |
| web のページ全体 | 64 KiB 未満 (完成後の byte) | web |
| web の diff の合計 | 48 KiB 以下 (見出し・省略印を含む)。3 ファイルへの割当は下の決定的手順 | web |
| web の他の節 (ナビ・CSS・HTML の骨格を含む) | 15 KiB 以下 (diff と合わせて 63 KiB で、64 KiB に 1 KiB の余裕を残す)。超えるときは params 差分 → 申告 の順で省略する | web |

切るときは escape の単位 (`&amp;` など 1 記号分の置換) の途中で切らない。切った印は「…」または「…(N 行省略)」で統一する。

**diff の 48 KiB の配分手順 (決定的):** 数えるのは `html.escape` 後の UTF-8 byte。ファイルごとの見出しと省略印も 48 KiB の中に含める。

1. 各ファイルに最大 8 KiB の quota を与える。ファイル i の必要量 `need_i` (見出し + 全行の escape 後の byte) が 8 KiB 以下なら、使用量 `u_i = need_i`、超えるなら `u_i = 8 KiB`。
2. quota の未使用分 (`8 KiB − u_i` の総和) を回収する。残余 `R = 48 KiB − Σ u_i`。
3. `need_i` が 8 KiB を超えたファイルへ、plugin.py、config.yaml、test_plugin.py の順に、各ファイルの不足分 (`need_i − 8 KiB`) を上限として `R` を割り当てる。
4. 各ファイルは割当 byte の中に、先頭から行単位で収まる行だけを出し、残りは省略印「…(N 行省略)」にする。省略印の byte は先に割当から確保する。1 行目が (割当 − 省略印) を超えるときは、本文 0 行 + 省略印だけを出す。
5. 「各ファイル 8 KiB」は quota (割り当てる枠) であって、出力量の保証ではない。短いファイルは枠を使い切らず、巨大な 1 行は枠に入らなければ出ない。

## 3. 承認投稿 (Discord) の文面

### 3.1 誰が何を読むか

承認者は投稿 1 つを読んで、Approve / Reject を決める。要約は次の順に並べる。日本語で、ボタン (Approve / Reject / Retry) と `footer` は固定のまま。

| 順 | 項目 | 内容 |
|---|---|---|
| 1 | 見出し | `承認 #N` / `戦略 <plugin 名> (新規 / 更新 / 同一版 / 不明)` |
| 2 | 変更 (更新のみ) | params の差分を `key: 旧 → 新` で最大 10 行。超えた分は「ほか N 件」。新規は「前版なし」とだけ書く。 |
| 3 | 成績 | in-sample の trades・PF・avg R を最大 4 行 (pair の行。節ごとの固定上限で、段落省略の 2000 字とは別の見た目の上限)。**holdout は Discord に出さない** (web のみ。§8.1 問 2)。 |
| 4 | agent の申告 | §8.1 問 1 の決定により出す。見出し「agent の申告 (未検証)」の下に引用ブロックで `summary` を 300 字まで (表示上限、escape 後)。 |
| 5 | 期限 | 承認依頼の期限。 |
| 6 | 操作案内 | 「詳細: web の承認 #N / `afx ctl approval N`」。URL は出さない。 |

### 3.2 無害化と上限

最終出力の順序は常に **Cc/Cf/Cs 除去 → redaction → 表示形式の escape → 表示上限** とする (§2.2、§7.1)。

- すべての非信頼値 (申告、params の key・value、plugin 名以外の文字列) を、まず共通の redactor に通す (1 値 8 KiB を超えるものは全文を伏せる)。
- 申告は既存の表示用整形 (制御文字・書式文字の除去、改行を空白へ、連続空白の圧縮) を通したあと、Discord の escape を掛け、最後に 300 字 (escape 後) で「…」切りにする。
- Discord の escape は全非信頼値に掛ける: メンション記法 (`@everyone` / `@here` / `<@...>` / `<@&...>`、**設定済みの role / user の ID を含む**)、markdown、コードフェンス、URL の autolink を無効化する (ゼロ幅文字を入れず、`@` の後ろに空白を足す、記号の前に `\` を置くなど表示上読める形)。本文は引き続き `allowed_mentions` なしで送るが、これに頼らず本文側でも無効化する。
- 絶対パスは出さない。plugin 名は既存の名前の形検査を通ったものだけ。
- **escape は文字列を増やす。** よって 2000 字の表示上限は、escape 後の**完成した本文**に対して最後に hard check として掛ける。超えるときは段落単位で省略し、残す優先順は 操作案内 > 見出し > params 差分 > 成績 > 申告。スレッド分割はしない。
- detail を取れない経路 (reconcile の再投稿、旧形式 payload) は、**見出し 1 行 + 代替 1 行の計 2 行**に落とす。代替の 1 行は reason code の固定文 (`detail_unavailable` 「詳細を取得できません」、`legacy_payload` 「旧形式のため要約なし」、§5)。壊れない。

### 3.3 現行 pin の書き換え (§8.1 問 1)

現行 test `test_approval_renderer_shows_only_server_allowed_fields_and_never_claim_bodies` は「claims の本文が投稿に出ない」ことを固定していた明示的な安全 pin であり、緩和にはユーザー裁定が要った。ユーザー裁定 (2026-10-19) により、「summary 本文は専用の見出しの下にだけ出る。`selection_rationale` の本文と、事実欄 (成績・差分) への申告の混入は出ない」の pin に書き換える。あわせて ops-api spec の §3.3 (承認の表示と決定) に「申告は見出しを分けて表示する」の 1 文を足す。

### 3.4 終端時の edit

承認依頼が終わった投稿は、View を外した上で「承認済み / 却下 / 失効」と決定時刻を残す (本文の要約は残す。決定者の名前は出さない)。現行の「Approval is no longer active.」1 行は、決定結果が取れない場合の代替に格下げする。

## 4. web の承認 #N ページ

### 4.1 位置づけ

既存の `/approvals/{id}` を厚くする。ナビ・固定 CSS・JS なしの方針を変えない。**新しい公開 web route は作らない。** web は内部で daemon の `GET /v1/approvals/{id}/change` (§5) と既存の detail を呼び、1 ページに合成する。変更内容は同ページの下部の節として出す。

### 4.2 節構成

| 節 | 内容 |
|---|---|
| 概要 | 承認 #N、plugin 名、kind、分類 (新規 / 更新 / 同一版 / 不明)、状態、候補と比較対象の hash (先頭 12 桁)、計算時刻。 |
| params の差分 | key ごとの 旧 → 新。値は 80 字 (表示上限) で切る。 |
| 成績 | in-sample / holdout の trades・PF・avg R (web は holdout も出す)。 |
| 申告 | 「agent の申告 (未検証)」の見出しで `summary` と `selection_rationale` を各 2000 字まで表示 (`selection_rationale` を出すのは web のみ)。 |
| 変更 diff | 3 ファイルの diff。ファイルごとの見出しと `<pre>`。 |

### 4.3 diff の形と上限、責務の分担

- 形式は色なしの unified diff を `<pre>` に入れる。根拠: 固定 CSS と JS なしの方針で、色を付けると CSS が変わり CSP の hash と pin 2 か所の更新が要る。daemon が返すのは構造化された hunk (ファイルごとの行の文字列、HTML escape はしない) で、unified diff の見た目への整形は web が行う。
- **責務の分担:** daemon の `/change` は、safe read、hash 検証、redaction、表示形式に依存しない構造化 diff、API 自身の応答上限 (512 KiB) までを受け持つ。web は、Unicode の Cc / Cf / Cs の除去 (改行だけ正規化して保持)、`html.escape`、他の節を含むページ全体の budget を受け持つ。Discord と CLI は、それぞれの表示形式の escape と最終上限を受け持つ。escape は表示面でちょうど 1 回だけ掛かるので、二重 escape は起きない。
- web の値の処理順は、(daemon で Cc / Cf / Cs の除去と redaction 済み) → 表示面での Cc / Cf / Cs の除去 (改行の正規化。冪等) → `html.escape` → 表示上限、とする。
- 上限は **`html.escape` 後の UTF-8 byte 数**で共有 budget を持つ。raw の文字数で制限すると `&` や `<` の膨張で破れるため、diff 合計 48 KiB 以下 (見出し・省略印を含む。3 ファイルへの配分は §2.7 の手順)、他の節 (骨格含む) 15 KiB 以下の規則でページ全体を 64 KiB 未満に収める。超えた分は末尾を「…(N 行省略)」にする。既存の renderer はページが 64 KiB を超えるとページ全体を「Output clipped」に差し替えるので、**ページ完成後の byte 数が 64 KiB 未満**であることを AC にする。
- 新規は全文を「追加」(`+` 行) として同じ budget で出す。
- 入力ファイルの読み取り上限超過は change 全体を `too_large`、非 UTF-8・検出不能のファイルはそのファイルだけ `redacted` (他のファイルの diff は出す)、単一値の 8 KiB 超過はその値だけ `[redacted]`。粒度と code の対応は §5 の粒度表のとおり。

### 4.4 認可と読み取り専用の守り

認証なしの loopback、GET / HEAD のみ、web の鍵に変更 scope が無い、DB と plugin tree を直接見られない、出力は allowlist の field だけ、という既存の守りをそのまま使う。戦略コードが同じホストの他ユーザーから読めることは、単一利用者ホストという運用前提でユーザーが受容した (§7.2 の 8、§8.1 問 3、§11)。web は daemon の API から変更内容を受け取るだけで、版 dir を自分で読まない。新 field は allowlist に明示的に足す。

## 5. 操作 API の拡張

| 変更 | 内容 |
|---|---|
| `approval_detail` への追加 | `change` を足す: 名前・kind・分類 (machine enum `new` / `updated` / `same` / `unknown`)・候補 hash・比較対象 hash・計算時刻・params 差分 (64 件上限)・成績の表示用の値・申告の `summary`。巨大な本文は含めない。bot が毎周回呼ぶので軽く保つ。表示 label (新規 / 更新 / 同一版 / 不明) は各表示面が enum から引く。 |
| 新 endpoint | daemon の `GET /v1/approvals/{id}/change` が 3 ファイルの構造化 diff (redaction 済み、escape なし、応答上限 512 KiB 適用済み) を返す。scope は既存の `approvals.detail` を流用し、新 scope は作らない。bot は呼ばない。web と将来の CLI が使う。これは daemon API の分離であり、公開 web の route ではない (§4.1)。 |
| 契約 | `payload_sha256` の対象 (payload) は変えない。approve / retry が要求する digest は従来どおり。 |
| 失敗 | 内容が取れないときは 200 で reason code (固定の列挙) を返す。壊れた payload は従来どおり invalid_state。 |
| 組み立ての置き場 | 計算は daemon 側の新しい小さなモジュールに分け、`service.py` は 1 本の呼び出しだけを足す。 |

reason code と固定文の対応 (表示面はこの表の文だけを出し、自由文を足さない):

| reason code | 起きる場合 | 固定文 |
|---|---|---|
| `unavailable` | 保持期間外、exact archive 無し、版 dir / staging 欠落 | 変更内容を取得できません |
| `hash_mismatch` | 3 ファイルの再計算 hash が payload と不一致 | 内容を検証できません |
| `too_large` | 1 ファイルが入力読み取り上限を超えた | 内容を検証できません |
| `unsafe_source` | symlink・非通常ファイル・許可外の名前や hash・root の fstat 不適合、live target / `old_target` の完全一致不成立など、§2.4 の 1〜5 の拒否 | 内容を検証できません |
| `redacted` | redactor が判定できないファイル、非 UTF-8、redaction 対象上限を超えた値 | 機微情報の可能性があるためローカルで確認してください |
| `detail_unavailable` | Discord の再投稿経路で detail を取れない | 詳細を取得できません |
| `legacy_payload` | 旧形式 payload で要約を作れない | 旧形式のため要約なし |

条件ごとの粒度と code (API の `change` に載る reason code。表示面は固定文だけを出す):

| 条件 | 粒度 | code |
|---|---|---|
| 入力ファイルが読み取り上限 (256 KiB) を超過 | change 全体 (diff・params 差分とも出さない) | `too_large` |
| §2.4 の 1〜5 の拒否 (名前・hash・mission_id、root・component、symlink・非通常ファイル) | change 全体 | `unsafe_source` |
| live target / `old_target` が完全一致しない | 分類 `unknown` に倒し、比較を要する params 差分と diff を出さない (候補の hash・成績・申告は出す) | `unsafe_source` を併記 |
| 3 ファイルの再計算 hash が不一致 | change 全体 | `hash_mismatch` |
| 非 UTF-8、redactor が判定不能なファイル | そのファイルの全文を伏せる (他ファイルの diff は出す) | `redacted` |
| 単一の params 値 / summary / rationale が 8 KiB を超過 | その値だけ全文を `[redacted]` にする (他の値は出す) | `redacted` (値単位) |

## 6. event 通知の文面

### 6.1 決定 event

| event | 文面 (例) | 備考 |
|---|---|---|
| 承認 | `承認 #25 (戦略 <plugin 名>) が承認・配備されました。` | `approval_state_changed` は**配備成功後**に発火する。配備失敗はこの event ではなく job failure 側 (§6.2) で通知される。「配備ジョブを開始します」とは書かない。 |
| 却下 | `承認 #25 (戦略 <plugin 名>) が却下されました。` | 却下理由は出さない。 |
| 失効 | `承認 #25 (戦略 <plugin 名>) は期限切れになりました。` | |

plugin 名は event に無いので、bot が承認 #N の詳細から引く。引けないときは名前を省く。

### 6.2 job event と既定

| 種類 | 既定 | 文面 (例) |
|---|---|---|
| 承認の決定 | 出す | §6.1 |
| job の失敗・タイムアウト | 出す | `承認 #25 の配備ジョブが失敗しました (理由 code: ...)。` |
| kill switch・guard の状態変化 | 出す | 状態変化を 1 行で。 |
| サービス健全性の変化 | 出す | 1 行で。 |
| job の成功 | 出さない | 設定で有効化できる。 |
| 操作要求の終了 (`ops_request_terminal`) | 出さない | 設定で有効化できる。 |

出さない種類は、web の活動ページと `afx ctl events` で見られる。全件に戻す設定は `ops.toml` の通知節に足し、隔離下で読める検査を通す。

### 6.3 決定者は出さない (v0.4)

event の `decided_by` は API の principal 名 (`api:ops-bot` など) で、人間ではない。人間は承認投稿の記録 (`ApprovalRecord.actor`、Discord の利用者 id) にだけあるが、この record は終端の edit が成功すると削除され、終端処理は actor・action・nonce を消す。event は後続の poll で届き得るので、actor を 1 つ残すだけでは event 到着前に record が無くなる。しかも bot の状態は version 4 で exact な schema 検証をしている。

したがって v0.4 は**名前を出さない**。通知は「承認・配備されました」「却下されました」とし、principal 名も人間の名前も出さない。監査上の決定者は daemon 側の actor 記録で残る。event の schema は変えない。

決定者の表示が将来必要になったら別設計とする (安定 ID、メンションにならない表示、state v4 → v5 の migration、event 消費までの terminal tombstone、期限による GC、重複 event の冪等性。§11)。

### 6.4 無害化

文面は種別ごとの固定 template に、検査済みの値 (id、plugin 名、code) だけを埋める。event から自由文を復元しない。

## 7. 遮断と秘密

### 7.1 出力境界 (共通 redactor)

表示する値の出どころは全て非信頼である。agent の summary / rationale、`config.yaml` の key と value (入れ子を含む)、plugin コードの文字列・コメント・fixture、およびそれらから作る diff の全行が該当する。改善 agent のプロンプトには staging 等の絶対パスが渡されており、自由文にそれが混じり得る。既存の `safe_text` は URL scheme と一部の `token=` 形しか扱わないため足りない。

そこで、次の 1 つの共通 redactor を、Discord・web・CLI の全ての表示に掛ける。

**保証の範囲 (ユーザー裁定 (C)、2026-10-19)。** redactor が保証するのは、正規化後の各表示値に**字面として現れる**絶対パス、URL、hostname、IP、credential、token、口座数字の伏せ字である。Python の評価や定数畳み込みは行わない。自由文 (summary / rationale / config 値 / parent_facts / reason / CLI) は全保証。**diff だけ**が「字面」に限定される。

**保証対象外の形 (diff のみ)。** 次のように、字面には現れず、評価や復号をして初めて値になる形は伏せない。
- 複数行・複数リテラルに分割した値 (`"ev" "il.xyz"`、`a = "ev"; b = a + "il.xyz"`、行をまたぐ連結)。
- f-string の式で組み立てる値 (`f"{a}{b}"`)。
- `chr()` / `bytes` / `bytearray` / `bytes.fromhex` / `str.join` などで組み立てる値。
- base64・hex・rot13・zlib など各種 encoding を復号して得られる値。
- 定数畳み込みや変数の経由 (別の名前・別の関数・別のファイルに置いた断片)。


- 検出対象: 絶対パス、URL・裸の hostname・IP、credential の代入 (`token=` `key=` `password=` `secret=` 等)、高エントロピーの token、口座らしい数字列。
- 順序: **入力読み取り上限 → Cc/Cf/Cs 除去 → redaction → 表示形式の escape (§3.2、§4.3) → 表示上限**。この順を崩さない (除去の前に redact すると、表示面が後で制御文字を消したときに分断された秘密が復元される。escape の後に redact すると検出を逃す、表示上限を先に掛けると完成本文が超える)。除去は redactor が検出の前に行い、出力も除去済みの文字列を基に作る。改行とタブは表示面が扱うため残し、CR は除去する。80 字・300 字・2000 字・64 KiB はすべて escape 後の表示上限である。
- 裸の hostname (`evil.xyz` のように scheme も `host=` の文脈もないもの) は、`.` でつながる語の最後の label が TLD (IANA の一覧の ASCII の TLD と、`local` / `internal` / `test` など特殊用途の名前を `change_limits.py` に同梱) か、どれかの label が `xn--` で始まる語を伏せる。全角の句点類 (U+3002 / U+FF0E / U+FF61) は `.` に、U+FF0F は `/` に、U+FF20 は `@` に NFKC の前に寄せる。`%XX` の decode は固定点まで (上限 3 回) 掛け、どの段で検出されても値全体を伏せる (3 回で終わらないものも伏せる)。plugin のファイル名 (`plugin.py` など) 自体は hostname としない。自由文では `foo.py` や `README.md` のように TLD と同じ拡張子の語も伏せる (明示の受容判断。誤検出より漏れを嫌う)。
- **検出の範囲は入力の種類で分ける**。自由文 (config.yaml の key と value、summary、rationale、parent_facts、reason、diff の Python 以外のファイル) は全体が対象。Python ソース (diff の `plugin.py` と `test_plugin.py`) は、ファイル全文を `tokenize` して求めた文字列リテラルとコメントの範囲にかかる語だけが対象で、識別子の連なり (`logger.info`、`self.local`) は hostname としない。f-string は全体を文字列として扱う。構文として読めない (`ast.parse` か `tokenize` が失敗する) ファイルは全行を自由文として扱う。URL・IP・credential・token・パスの検出は Python ソースでも全行に掛ける。
- **口座らしい文脈の日付**: 口座の数字列の検出から ISO 日付・時刻を除く例外は、`account` / `acct` / `iban` / `card` / `口座` / `customer` / `client_id` / `login` / `user_id` / `number` を含む key、または語としての `no` で終わる key の値 (`=` か `:` の直後) には効かない。裸の `2026-10-19` や `from 2026-10-01 to 2026-10-19` は日付として残し、`account=2026-10-19` は日付の形でも伏せる (明示の受容判断。日付表示と口座番号の隠蔽は両立しないので、key が口座らしいときは隠蔽を優先する)。params の値も同じ key 規則で見る。
- redactor に渡す 1 値には redaction 対象上限 (§2.7、8 KiB) がある。超える値は**切らずに**全文を伏せる (切ると token が境界で割れて検出を逃すため)。
- fail-closed: 判定不能なファイル (検出が確信できない、非 UTF-8) は**そのファイルの全文**を伏せ、reason code `redacted` (「機微情報の可能性があるためローカルで確認してください」) を表示する。他のファイルは出す。部分的に出して誤魔化さない。入力ファイルの読み取り上限超過は redaction ではなく `too_large` (change 全体)、単一値の 8 KiB 超過はその値だけ `[redacted]` (§5 の粒度表)。
- 検出に失敗した値は伏せる側に倒す。

### 7.2 改善側への経路と永続化先

1. **改善 agent への経路は増やさない。** 今回生成する派生物 (変更要約 `change`、diff、params 差分、redaction の reason code、およびそれらの複製) は読み出し時に計算し、承認 payload、承認行の新しい列、`improvement_backlog.last_result`、backlog のどれにも保存しない。`last_result` は `approval_pending:<id>` のままにする。保存形を変えたくなったら遮断規律のレビューをやり直す。
2. **既存の保存は変えない。** 承認 payload は、成績 (in-sample / holdout)、`summary` / `selection_rationale` (`agent_claims`)、artifact の hash などを従来どおり保存し、人間の却下理由も `approval_requests.reason` に従来どおり保存される。これらは正規の材料であり、本設計は保存を足しも消しもしない。新しく禁止するのは派生物の保存と、次の**永続化先への流入**である: bot state、web の cache、daemon のログ、event payload、`last_result`、backlog discovery、改善プロンプト。metrics・claims・reason・派生物のいずれもここには入らない。例外ログは固定の reason code で出し、diff・source・path を含めない (§2.4 の 8)。
3. **improve の tool registry** に Discord・transcript・承認 detail・ログ読み取りが無いことを pin する (追加されたら test が落ちる)。
4. **holdout 数値は表示専用で、web のみに出す。** 人間の判断材料とする。改善 agent に戻る経路 (プロンプト、ツール出力、backlog) は作らない。Discord に出さないことはユーザー裁定 (§8.1 問 2) で確定しており、出す設定は作らない。Discord の閲覧者が人間専用のチャンネルであるかは運用前提で、リポジトリからは確認できない。
5. 却下理由は今回も表示しない (人間の理由が改善プロンプトに漏れた既往の再発を避ける)。
6. 伏せる対象: 絶対パス (staging や home を含む)、メンションの実効化、URL、コードフェンスによる偽装。申告は自己申告であり、Discord では引用ブロックと「未検証」の見出しで事実欄と分ける。
7. 秘密: plugin コードは検査済みで環境変数やファイルを読めず、`config.yaml` に鍵や口座は入らない前提だが、`params` の任意 key と任意の文字列を許すため、これに頼らず §7.1 の redactor を必ず通す。
8. **運用前提 (§8.1 問 3):** 認証なしの loopback web で取引ロジック (diff 全文) が同じホストの他の利用者から読める。redactor は秘密を伏せるが、戦略は見える。リポジトリが保証するのは loopback bind までで、単一利用者のホストであることは運用前提であり、ユーザーが受容した。この前提は運用文書 `docs/operations/ops-api-2026-10-09.md` に T1 で追記する。
9. 既存の未是正経路として、gate の pytest 末尾が `last_result` に入るものがある。本設計はこれに触れず、新しい書き込みを足さない。
10. **diff の伏せ字は字面限定 (残余リスク、裁定 (C)):** 字面に現れない再構成可能な値 (§7.1 の保証対象外の形) は diff で伏せない。受容の根拠は次の 3 つである。(a) 改善 agent の入力に実ホスト名・秘密が無い (settings の bridge URL、webhook、API 鍵、鍵 path は prompt にも tool 応答にも渡らない、AC-32)。(b) plugin は外部に触れない (隔離 sandbox で network・環境変数・鍵に届かない)。(c) 閲覧者は単一利用者の loopback である。したがって agent が diff に実ホスト名や秘密を再構成して書き込む動機も材料も無い。**前提が崩れる条件:** 多利用者ホスト (loopback web を他の利用者が読める)、または agent の入力に実ホスト名・秘密が入る変更 (例: prompt や tool 応答に bridge URL・鍵 path・webhook を足す)。どちらかが起きるなら、この受容をやり直し、diff の伏せ字を評価ベースに強めるか web を無効にする。

## 8. 裁定の記録

### 8.1 ユーザー裁定 (2026-10-19、確定)

設計の途中で保留していた 4 問に、ユーザーが 2026-10-19 に答えた。以下は決定であり、本文はこれに従う。

| # | 問い | 決定 | 日付 | 根拠 |
|---|---|---|---|---|
| 1 | agent の summary を Discord に出すか (現行 pin `test_approval_renderer_shows_only_server_allowed_fields_and_never_claim_bodies` の緩和) | **出す。** 「agent の申告 (未検証)」の見出し、引用ブロック、300 字、redactor と escape を通す。`selection_rationale` は web のみ。pin は「summary 本文は出す、rationale 本文と事実欄への混入は出さない」に書き換える (§3.3) | 2026-10-19 | 「何がどう変わるか読めない」の直接の原因。上位 spec ops-api §3.3 の「`parent_facts` と `agent_claims` を分離して表示する」は表示の禁止でなく事実と混ぜない意味と読める。見出しの分離と「未検証」で事実扱いを避ける |
| 2 | holdout の数値を Discord に出すか | **出さない (web のみ)。** Discord は in-sample だけ。設定 key は作らない | 2026-10-19 | 運用チャンネルが人間専用か共有かはリポジトリから確認できない。共有 channel でも漏れない側に固定し、設定の分岐を持たない |
| 3 | 認証なし loopback の web で戦略コードが同じホストの他ユーザーから読める点 | **受容する。** 単一利用者ホストの運用前提。redactor と安全な読み取りは必須 | 2026-10-19 | 秘密は redactor が伏せるが戦略コードは隠れない。多利用者ホストにするなら別途、認可の設計が要る。前提を §4.4 と §11 に書き、運用文書への追記は T1 に含める |
| 5 | event 通知の既定 | **決定 + 失敗系のみ。** 承認の決定 (承認・配備 / 却下 / 失効) と失敗系 (job 失敗・timeout・kill switch・guard・健全性)。job 成功と操作要求の終了は出さず設定で有効化 | 2026-10-19 | 主題は週 1 の承認。成功の記録は web と `afx ctl events` で足りる。文言は発火時点 (配備成功後) に合わせる |

問 4 は欠番である (決定者の表示は名前なしで設計時に決まり、§8.2 の確認事項に移した)。

### 8.2 既に決めた確認事項 (問いにしない)

次は設計で決めた事項であり、ユーザーの裁定は要らない。覆したいときだけ指摘する。

- 決定者の表示: 名前なし (「承認・配備されました」「却下されました」)。名前は後続 (§6.3、§11)。record は終端の edit 後に削除され、bot の state は schema v4 の exact 検証で、名前を残すには v5 の migration・tombstone・GC・冪等性が要り、通知の主目的に対して重い。監査上の決定者は daemon 側に残る。
- 更新の基準: §2.2 のとおり。新規 / 更新 / 同一版 / 不明の 4 分類、journal は最終 `phase='decided'` の row、複数で不整合、または target が完全一致しないなら不明。誤って「新規」と言うより「不明」と言うほうが安全。
- diff の範囲: 3 ファイル全部。test の変更も承認対象。
- params 差分の範囲と key 制限: §2.2 の 6 項目、入れ子は決定的 flatten、key 形検査、値 80 字、redactor。
- diff の表示形式: 色なしの unified diff を `<pre>`。色を付けると CSS の hash の pin 2 か所が動く。
- API と web の形: daemon は `approval_detail` に `change` 要約 + 別 endpoint。公開 web は既存 `/approvals/{id}` の 1 ページに統合し、新しい公開 route は作らない。daemon は構造化 diff、escape と最終上限は表示面 (§4.3)。
- 計算のタイミングと束縛: 読み出しごとに safe opener + coherent read + hash 再検証、保存しない。候補 hash・baseline hash・計算時刻を表示する。baseline の変更窓は残余リスクとして受容し、stale 検知は作らない (§2.5)。digest を payload に足す案・view token 案は `payload_sha256` とボタン契約が変わるので見送り。
- 見られる期間: 承認済みは版 dir が残る限り、却下は exact archive が残る間。無ければ「変更内容を取得できません」。content_hash fallback は使わない。新たな保持は作らない。
- 履歴一覧: 今回は作らない (URL 直打ちで到達)。
- Discord に URL を出すか: 出さず「web の承認 #N」とコマンド文。loopback は押せず、ホスト名開示を避ける。
- 日本語化の範囲: 承認投稿と event 通知の本文。ボタンと `footer` は固定。
- 2000 字超: escape 後の完成本文で hard check。段落単位で省略、優先順 操作案内 > 見出し > params 差分 > 成績 > 申告。スレッドなし。
- 実装束の分割: 1 spec、2 束 (T1 → T2 の変更表示、T3 の event 通知)。名前なしなら T3 は小さく、先行して出せる。
- safe opener の契約: root と component は §2.4 の表のとおり。永続化された path 文字列は信用せず、検証済みの値から再構築する。
- 上限の値と diff の配分手順: §2.7 のとおり。
- 固定文と粒度: §5 の reason code 対応表と粒度表のとおり。

## 9. 受入条件

境界値の AC は、上限 `L` について `L-1` / `L` / `L+1` を入れて観測する。

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | 新規候補の `approval_detail` が name・kind・分類 `new` (表示は「新規」+「前版なし」の補足)・成績・申告 summary (無条件) を返し、`payload_sha256` が変更前と同じ | API 応答と digest の before/after |
| AC-2 | 更新候補 (live が symlink で hash 違い) が分類 `updated` と比較対象 hash を返し、params 差分が key ごとの 旧 → 新 になる | 合成した 2 版 dir での応答 |
| AC-3 | live が plain dir のとき `unknown`、不在のとき `new` と返る (推測で `updated` と言わない) | 応答の分類欄 |
| AC-4 | `content_hash` が同じで test だけ違う場合、`updated` として test の diff が出る | 合成版 dir |
| AC-5 | 形に合わない key は表示されず件数だけ出る。値は escape 後 80 字で切れ (79 / 80 / 81 字)、`&` を含む値は escape の途中で切れない。入れ子 config は決定的に flatten され、深さ 4 と項目 64 を (L-1 / L / L+1 で) 超えた分は件数になる。params 差分は 64 件で (63 / 64 / 65) 切れる。単一の値が 8 KiB を (L-1 / L / L+1 で) 超えると、その値だけが `[redacted]` になり切られず、他の差分は出る | 合成 config.yaml |
| AC-6 | 内容が取れない (版 dir・staging・exact archive が無い) とき、200 で reason code `unavailable` と固定文「変更内容を取得できません」。エラーにならない。`content_hash` だけ一致する archive は使われない | 応答、content_hash 一致・artifact_hash 不一致の archive fixture |
| AC-7 | 承認投稿に 見出し・params 差分・成績 (in-sample のみ。holdout は含まれない)・申告 (summary のみ)・期限・操作案内が順に出る。URL は無い | 描画結果の文字列 |
| AC-8 | 申告に `@everyone`・設定済み role / user の ID のメンション・URL・コードフェンス・連続改行・偽の「Approved」行を入れても、投稿本文で実効化・偽装されない。escape で増える文字を含む申告が escape 後 300 字で切れる (299 / 300 / 301) | 敵対 fixture |
| AC-9 | escape で膨らむ入力でも、完成本文が 2000 字以下で (1999 / 2000 / 2001 字になる fixture)、操作案内と見出しが残り段落単位で省略される | 長い fixture、最終本文の長さ |
| AC-10 | detail が取れない再投稿経路と旧形式 payload で、描画が例外なく「見出し 1 行 + 固定文の代替 1 行」の 2 行になる (`detail_unavailable` / `legacy_payload`) | reconcile 経路と legacy fixture |
| AC-11 | 事実欄 (成績・差分) に申告が混ざらない | 描画結果の節別検査 |
| AC-12 | web の承認 #N に 5 節が出る (申告は web に出る)。diff の 48 KiB は §2.7 の手順で配分される: (a) 3 ファイルとも 8 KiB 以下なら省略なし、(b) 1 ファイルだけ 8 KiB 超なら未使用 quota を回収して残余が不足分まで割り当たる、(c) 複数が超えるなら plugin.py、config.yaml、test_plugin.py の順、(d) 合計が 48 KiB を (L-1 / L / L+1 で) 超えると「…(N 行省略)」、(e) 割当に 1 行も入らない巨大な 1 行は本文 0 行 + 省略印。見出しと省略印も 48 KiB に含まれる。他の節は 15 KiB 以下。`&` `<` を多く含む入力でも、**ページ完成後の byte 数が 64 KiB 未満**で「Output clipped」に差し替わらない。holdout が出る | web 応答の byte 数と節別検査、割当ごとの境界 fixture |
| AC-13 | web が POST・追加 header・未知 path を従来どおり拒否し、新 field 以外が allowlist から漏れない。新しい公開 route は無い。CSS と STYLE_HASH が不変 | 既存 pin test、route 一覧の pin |
| AC-14 | `GET /v1/approvals/{id}/change` が `approvals.detail` で通り、他の scope では拒否。bot は呼ばない。応答は escape 済みの文字列を含まず (`&` は `&` のまま)、512 KiB を (L-1 / L / L+1 で) 超えると行単位で省略される | scope test、bot の呼び出し記録、応答の byte 数 |
| AC-15 | 派生物の非保存: 今回生成する派生物 (change、diff、params 差分、redaction reason code、その複製) を保存していない。(1) 変更要約を計算する前後で approval 行 (全列、`payload_json`、`payload_sha256`) を比較し、byte 一致する (既存 payload と digest が不変)。(2) approval 行の列一覧と payload の key 集合が変わらず、新しい field・列・複製が無い。(3) 合成した plugin ファイルと diff に入れた一意 canary (入力側に存在することを先に assert) が、DB の JSON 全列 (`approval_requests`・`improvement_backlog`・`last_result`) のどこにも現れない。既存の保存 (成績、`agent_claims`、人間の却下理由) は検査の対象外で、従来どおり保存されている。`last_result` は `approval_pending:<id>` のまま | 計算前後の approval 行の比較、列・key 集合、派生 canary の個別検査 |
| AC-16 | 改善プロンプトを実生成して、成績・diff・申告由来の一意 canary (入力側に存在すると先に assert) が混ざらない | 生成した prompt 文字列の検査 |
| AC-17 | 承認の決定 event が日本語の文になり (承認・配備 / 却下 / 失効)、plugin 名が入る (引けないときは名前なし)。k=v 行が出ない。「配備ジョブを開始します」を含まない | 描画結果 |
| AC-18 | 既定設定で job の成功と操作要求の終了が投稿されず、失敗と kill switch は投稿される。設定で有効化すると出る | event 列の投入と投稿数 |
| AC-19 | 決定者の名前・principal 名が通知にも終端 edit にも出ない | 描画結果 |
| AC-20 | 全 event 通知の本文が許可された値だけから組まれ、自由文を含まない | event 変種ごとの描画 |
| AC-21 | ボタンと `footer` が従来と同じ | 既存 pin test |
| AC-22a | 字面に現れる敵対 fixture (絶対パス・host・IP・token・口座らしい数字列・コードフェンス・メンション・入れ子 config) が API の `change`・web の全節・CLI 出力のどれからも漏れない。判定不能なファイルはそのファイルだけ全文を伏せて `redacted` の固定文になり、他のファイルの diff は出る。文字列分割・`chr()`・bytes・encoding などの再構成 fixture は AC に入れない (§7.1 の保証対象外) | 敵対 fixture と API / web / CLI の検査 (T1) |
| AC-22b | 同じ敵対 fixture が Discord の承認投稿 (summary・params の key と value) から漏れない | 描画結果 (T2) |
| AC-23a | 順序の境界 fixture (API・web・CLI): escape で文字数が増える記号 (`&` `<` など) を表示上限ちょうどの手前まで並べ、**末尾に redaction 対象 (token など) を置く**。redaction → escape → 表示上限の順なら token は伏せられ上限内に収まる。順序が逆なら token が出るか上限を超える。params の 80 字と web budget のそれぞれに fixture を置く | 境界 fixture の出力に対する assert (T1) |
| AC-23b | 同じ順序の境界 fixture を Discord の 300 字 (summary) と 2000 字 (完成本文) に置く | 境界 fixture の出力に対する assert (T2) |
| AC-24 | 読み取りの安全: 許可外の名前・hash・mission_id、root 外の path、root の fstat が directory でない、中間 component の symlink、最終 dir の symlink、ファイルの symlink、非通常ファイル、`archive_path` が壊れている payload (§2.4 の表の component で読み、壊れた値は使われない) が `unsafe_source` で拒否される。root と component は表のとおり (version = `.versions` / `[name, hash]`、staging = `_staging` / `[mission_id, name]`、archive = `_archive` / `[mission_id, hash]`、human = `_human` / `[name]`、`.tmp-` 作成途中の dir は読まない)。human origin の pending 候補の fixture (正常 1 件、locator 不正 1 件、未知の origin 1 件) を含む。live の symlink target と journal の `old_target` は、`.versions/<同名>/<64 桁 hex>` に完全一致するときだけ baseline になり (hash 抽出 → component 再構築)、`../` を含む・別名・hex 長違い・絶対パスの target は `unknown` + `unsafe_source`。1 ファイルが 256 KiB を (L-1 / L / L+1 で) 超えると `too_large` (change 全体)。3 ファイルの hash 再計算が payload と不一致なら `hash_mismatch` で bytes を返さない。レスポンス・例外・ログに解決済み絶対パスが無い | 合成 fixture、ログの検査 (T1) |
| AC-25 | baseline の表示: 応答に候補 hash・baseline hash・計算時刻の 3 値が含まれ、web の画面に先頭 12 桁と時刻が短く表示される | 応答のフィールドと画面の文字列 |
| AC-26 | 分類の API 値が `new` / `updated` / `same` / `unknown` の enum で、表示 label は 新規 / 更新 / 同一版 / 不明 の 1 つずつ (「同一版・変更なし」は label として現れない)。journal が複数 row で不整合のとき `unknown`。最終 `phase='decided'` row の選択が正しい | 合成 journal、enum と label の対応 |
| AC-27 | negative test: 一意 canary (AC-15 と互いに異なる値) を change / metrics / claims / reason に入れ、入力側に存在することを先に assert したうえで、bot state、web cache、daemon ログ、event payload、`last_result`、backlog discovery のそれぞれを個別に検査し、canary が現れない (承認 payload と人間の却下理由の既存保存は対象外)。例外ログは固定の reason code だけで、canary・path を含まない | 各永続化先の個別検査 |
| AC-28 | improve の tool registry に Discord・transcript・承認 detail・ログ読み取りが無い | registry の pin |
| AC-29 | `afx ctl approval <id>` が変更要約を出す (整形は redactor 経由、diff は出さない) | CLI の出力 test |
| AC-30 | 終端時の edit (§3.4): 承認 / 却下 / 失効のそれぞれで、本文の要約 (見出し・params 差分・成績) が保持され、状態名と決定時刻が出て、View (ボタン) が除去される。決定者の名前は出ない。決定結果が取れないときだけ「Approval is no longer active.」の 1 行になる | 終端 edit の描画と component の検査 |
| AC-31a | reason code の対応 (API・web・CLI): §5 の表の `unavailable` / `hash_mismatch` / `too_large` / `unsafe_source` / `redacted` が、表のとおりの固定文だけを出し、自由文を足さない。§5 の粒度表の各行 (change 全体 / ファイル / 値) が、表のとおりの単位で出る | code ごとの描画と粒度ごとの検査 (T1) |
| AC-31b | reason code の対応 (Discord): `detail_unavailable` / `legacy_payload` が固定文だけを出し、自由文を足さない | code ごとの描画 (T2) |
| AC-32 | 改善 agent への入力に実ホスト名・秘密が含まれない (§7.1 の diff の受容の前提)。settings 由来の値 (`mt5_bridge` の host / port、Discord webhook の host と token、API 鍵、`~/.config/agentic-fx` の鍵 path) を一意の canary にし、入力側 (settings・環境変数・鍵ファイル) に存在することを先に assert したうえで、改善 mission のプロンプト全文と生成した context、research / staging 群の代表 tool の応答と tool schema に canary が現れない。既知の例外は staging の絶対パス (`plugins/_staging/...`、意図して渡している) で、canary は staging の外に置く | プロンプト文字列と tool 出力の検査 (`tests/loops/test_improve_input_no_host_secrets.py`) |

## 10. 変更ファイルと task 表

### 10.1 変更ファイル (予定)

| 領域 | ファイル | 変更 |
|---|---|---|
| 安全な読み取り | `src/agentic_fx/ops/` の新モジュール (safe opener) | version / staging / archive / human 共通の `openat` + `O_NOFOLLOW` の opener、root の fstat、§2.4 の表の component 構築、live target / `old_target` の完全一致と再構築、通常ファイル・サイズ上限・hash 検査 |
| 計算 | `src/agentic_fx/ops/` の新モジュール | coherent read、4 分類、params 差分、構造化 diff、redactor、定数表、reason code の粒度 |
| API | `ops/service.py`、`ops/api_server.py` | `change` の追加と新 endpoint の配線 |
| CLI | `src/agentic_fx/ops/client.py` と formatter | `afx ctl approval <id>` に変更要約 (`--diff` は非目標) |
| web | `afx-ops/afx_ops/web/{routes,request,render}.py`、`api_client.py` | 既存 `/approvals/{id}` に節を追加 (新 route は無し)、allowlist、escape と budget |
| bot | `afx-ops/afx_ops/gate/render.py`、`bot/adapter.py` | 承認投稿の文面、escape、event template、終端 edit |
| 設定 | `afx-ops/afx_ops/config.py`、example | 通知既定を全件に戻す設定 |
| spec | ops-api spec §3.3、afx-ops spec | 申告の見出し分離、event 文面の追記 |
| test | 各領域 | 実 payload 形の fixture (匿名化)、合成版 dir、敵対 fixture、canary、永続化先の negative test |

`resolve_candidate_dir()` は変更せず、candidate_path の字句検査としてだけ呼ぶ。bot の状態 (`gate/{types,state}.py`) は変えない (決定者を出さないため。schema 変更なし)。holdout を Discord に出す設定は作らない (§8.1 問 2)。

### 10.2 task 表 (2 束、1 spec)

| task | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|
| T1 API + CLI + web | 運用文書 `docs/operations/ops-api-2026-10-09.md` への前提 1 行の追記 (単一利用者ホスト、§7.2 の 8)、safe opener (root と component の契約、baseline target の完全一致)、coherent read、redactor、変更要約の計算、`approval_detail.change`、新 endpoint (構造化 diff)、CLI の formatter、web の節と diff と escape と budget (48 KiB の配分手順)、spec の追記 | AC-1〜6、12〜16、22a、23a、24〜29、31a、32。合成 2 版 dir (中間 symlink を含む)、現行形の匿名化 payload fixture、境界値 (L-1 / L / L+1)、web の byte 上限の pin、敵対 fixture、canary、negative test | L |
| T2 承認投稿 | `render_approval` の書き換え、escape、最終 2000 字 hard check、代替経路 (2 行)、終端 edit、pin の書き換え (§3.3) | AC-7〜11、21、22b、23b、30、31b。敵対 fixture と長文 fixture、順序の境界 fixture | M |
| T3 event 通知 (別束) | 種別 template (名前なし、配備成功後の文言)、plugin 名の補完、既定の絞り込みと設定 | AC-17〜20。state の schema は変えないので旧 fixture はそのまま | S〜M |

T1 → T2 を 1 束 (T2 は T1 の safe な change モデルと API に依存)、T3 を別の束とする。T3 は T1 と独立で、名前なしなら小さく、先に出してよい。

## 11. 後続と非目標

- 履歴一覧ページ、承認の検索 (低)。
- CLI の `afx ctl approval show <id> --diff` (T1 の endpoint ができた後の小さな追加)。
- diff の色分け (CSS 更新を伴う別束)。
- 前版の成績との並記 (指標条件が同一か未確認のため見送り)。
- 同一 mission の他候補との対比。
- notify (webhook) は固定 schema のまま。
- **stale 検知:** 前回表示した baseline hash を refresh URL の query で運び、次回応答と比べる案。現行 web は `/approvals/{id}` の query を拒否する (`afx-ops/afx_ops/web/request.py`) ので、query の許可と allowlist の変更が要る。残余リスク (§2.5) を運用で見たうえで判断する。
- **決定者 (actor) の表示:** 必要になったら別設計 (安定 ID、メンションにならない表示、state v4 → v5 の migration、event 消費までの terminal tombstone と期限 GC、重複 event の冪等性)。
- **baseline の束縛を閉じる案:** diff の digest を payload に足す、または承認操作に view token を必須にする。`payload_sha256` とボタンの契約が変わるので、v0.4 の残余リスク (§2.5) を運用で見たうえで別途判断する。
- **holdout を Discord に出す設定:** 作らない (§8.1 問 2)。出したくなったときは別設計で、設定 key・config schema・pin test を伴う。
- **diff の伏せ字を字面から評価ベースへ:** 受容 (§7.2 の 10) の前提 (単一利用者の loopback、改善 agent の入力に実ホスト名・秘密が無い、AC-32) が崩れたときの別設計。多利用者ホストにする、または agent の入力にホスト名を入れる変更を入れるときは、先にこの受容を見直す。
- **単一利用者ホストの前提:** 戦略コードが loopback web で同じホストの他ユーザーから読める点は受容済み (§8.1 問 3)。多利用者ホストにするなら認可の設計が先に要る。

## 12. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-18 | v0.1 | C0 下書きを新規作成 | 2 チケット (`approval-notice-lacks-change-content`、`bot-event-notices-are-raw-log-lines`) の設計 | |
| 2026-10-19 | v0.2 | 共通 redactor と出力境界 (§7.1)、安全な読み取りと coherent read (§2.4)、archive の事前生成と exact archive (§2.1)、baseline の束縛と stale と残余リスク (§2.5)、最終出力の escape と長さ (§3.2、§4.3)、決定者を名前なしに (§6.3)、event 文言を配備成功後に (§6.1)、web route を既存ページに統合 (§4.1)、CLI を T1 に追加、4 分類 (§2.2)、遮断の AC 拡充 (§7.2、§9)、実装束を 2 つに分割 (§10)、§8 を主要 3 点 + 推奨更新に再構成 | codex sol のレビュー指摘 12 件 (Critical 2、High 5、Medium 5) の全件採用 | |
| 2026-10-19 | v0.3 | safe opener を新設し `resolve_candidate_dir()` は字句検査のみに (§2.4、§10)、stale 検知を削除し hash と時刻の表示と残余リスクのみに (§2.5、AC-25、§11)、daemon と表示面の責務分離 (§4.3、§5)、入力読み取り上限 / redaction 対象上限 / 表示上限の 3 種と処理順の統一 (§1、§2.2、§3.2、§7.1)、上限の定数表 (§2.7)、分類 enum と label と reason code 対応表と 2 行の代替 (§1、§5、AC-10、AC-26、AC-31)、holdout を web のみに固定 (§3.1、§8 問 2)、AC-15 / 16 / 23 / 27 の canary と境界 fixture 化と終端 edit の AC-30 追加 (§9)、§8 を裁定が必要な問いと確認事項に分離 | codex sol 2 周目の指摘 8 件 (Critical 1、High 3、Medium 4) と判定 7 の全件採用 | |
| 2026-10-19 | v0.4 | §8.1 から問 4 (決定者) を §8.2 へ移し、問 2 (holdout) は残して「v0.4 の既定は web のみ、問 2 で覆せる」に本文を統一 (§0、§3.1、§7.2、§8)。§7.2 の「成績を承認 payload に保存しない」を削除し、禁止を今回生成する派生物と永続化先への流入に限定、AC-15 を approval 行の before/after 比較へ書き換え (§7.2、AC-15、AC-27)。safe opener の root と component の契約と、live target / `old_target` の完全一致検査を追加 (§2.2、§2.4、AC-24)。`too_large` / `redacted` / `unsafe_source` の粒度表 (§5、§4.3、§7.1、AC-31a)。16 KiB の残骸を削除し 48 KiB 共有 budget に統一 (§1、§4.3、§7.1)。48 KiB の配分手順を quota 方式に (§2.7、AC-12)。AC-22 / 23 / 31 を面ごとに分割し task 欄に列挙 (§9、§10.2)。web の申告表示を無条件に統一 (§4.2) | codex sol 3 周目の指摘 7 件 (High 3、Medium 4) の採用。問 2 は指揮者の裁定で §8.1 に残した (運用チャンネルの性質はユーザーしか知らない) | |
| 2026-10-19 | v0.5 | safe opener の契約に human origin (`plugins/_human` / `[name]`) を追加 (§1、§2.4、AC-24、§10.1)。AC-1 の申告 summary を無条件に (API は常に返す、Discord だけ §8.1 問 1 に依存) | codex sol 4 周目 (閉鎖確認) の Low 2 件 | |
| 2026-10-19 | v1.0 | ユーザー裁定 4 問 (問 1 summary を Discord に出す、問 2 holdout は web のみで設定 key なし、問 3 loopback の受容、問 5 event 既定は決定 + 失敗系のみ) の反映。§8 を裁定の記録に変え、条件分岐を決定後の文に書き換え (先頭、§0、§3.1、§3.3、§4.2、§4.4、§7.2、§8、§9 AC-7 / AC-12、§10、§11)。運用文書への前提追記を T1 に追加 | ユーザー裁定 | |
| 2026-10-19 | v1.1 | T1 の実装で決めた細部を追記: `/change` の応答は `{id, change, claims, diff}`、diff は file ごとに `{path, status (added / changed / unchanged / redacted), reason, hunks[{header, lines}], omitted_lines}` (§5)。web の diff で hunk header も 1 行として数える (§2.7)。タブは空白 4 つにして表示 (§4.3)。展開上限を超えた項目の件数 `flatten_omitted` を params に載せる。pending 以外で配備ジャーナルが無い候補 (却下・失効など) の比較対象は `unknown` (§2.2) | T1 実装 | |
| 2026-10-19 | v1.2 | 処理順を 入力読み取り上限 → Cc/Cf/Cs 除去 → redaction → escape → 表示上限 に訂正 (§2.2、§3.2、§4.3、§7.1)。redactor が検出の前に制御文字を除き、出力も除去済みの文字列から作る | NUL・ゼロ幅・孤立サロゲート・CR を値に挟むと検出を逃し、表示面の除去で伏せていない秘密が復元されることを実測 | |
| 2026-10-19 | v1.3 | 行単位 diff の計算量を `DIFF_MAX_CELLS` で打ち切る (§2.7)。日付・時刻を口座の数字列の検出から除き、hostname は主要 TLD・文脈 (URL・email・host=) ・3 label 以上の `.local` に限り、添字のスライスを IPv6 とみなさない (redactor)。params の flatten は key の和集合の先頭 64 件を両側に当てる。`/change` の応答が上限を超えるときは diff → claims → params 項目 → 理由だけの順に落として必ず上限以下にする。web と CLI は型の外れた変更内容を固定文 1 行に倒す | /code-review high の指摘 8 件の是正 | |
| 2026-10-19 | v1.4 | hostname の検出を入力の種類で分ける (自由文は全 TLD で全体、Python ソースは文字列・コメントのみで、tokenize / 構文の失敗は全行自由文)。TLD は IANA の一覧を同梱、`xn--` の label は無条件。decode は固定点まで (上限 3 回)、全角の句点類・スラッシュ・アットを NFKC の前に寄せる。口座らしい key の値は日付の形でも伏せる (§7.1)。`approval_detail` の change は diff を作らないことを test で pin | 閉鎖確認の指摘 3 件 (主要 TLD だけ裸で伏せる設計の回避、日付の無条件通過、diff の二重計算) の是正 | |
| 2026-10-19 | v1.5 | redactor の保証を「正規化後の各表示値に字面として現れる値」に限定 (ユーザー裁定 (C))。diff に限り、複数行・複数リテラル・f-string 式・`chr()`・bytes・encoding から再構成できる値は保証対象外 (§0、§7.1)。受容の根拠と前提が崩れる条件を追記 (§7.2 の 10、§11)。AC-22a を字面に現れる fixture に限定し、新 AC-32 (改善 agent への入力に実ホスト名・秘密が無い、canary test) を追加 (§9、§10.2) | redactor の検出で Critical が 3 周続いた。評価ベースの検出は部品として成立しないため、前提 (単一利用者の loopback、agent の入力に実ホスト名・秘密が無い) を pin して残余を受容 | |
| 2026-10-20 | v1.6 | §3.1 の成績に「最大 4 行 (in-sample の trades・PF・avg R)」を明記。段落省略 (2000 字) とは別の、見た目の固定上限として残す。`cut_escaped` は「…」を上限の内側に収める (全体で表示上限以下) | 実装レビューで節別上限が spec に無いと指摘され、残す裁定 | |

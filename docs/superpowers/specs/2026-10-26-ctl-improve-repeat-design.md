# afx ctl improve の繰り返し (-t) / 一時的な指示 (-c) / 回数 (-n) 設計書

**版:** v0.1 C0 (指揮者承認前の清書)

**対象:** `ctl-improve-repeat-duration-and-oneoff-comment` (2026-10-25 のユーザー依頼と裁定)

**関連チケット:** `improve-feedback-structure` (族別試行結果。繰り返しの連続情報はこれに乗る)、`improve-start-and-result-to-discord` (mission ごとの開始・終了通知。設計 `2026-10-25-improve-discord-notice-design.md` §4.4)

**前提:** 操作 API の job 契約 (`2026-10-04-ops-api-design.md` §5)、コマンド面 (`2026-10-23-cli-surface-design.md`)。

## 0. 目的と非目標

目的: 1 回の改善で当たりが出ることは少ないので、`afx ctl improve` を手で何度も打たずに、指定した時間(または回数)の間 mission を続けて回せるようにする。あわせて、恒久の方針 (`directives.md`) に書くほどでない「今回だけの指示」を渡せるようにする。

- 既定は従来どおり 1 回。週次の wave は変えない。繰り返しは利用者の明示操作に限る (周回数でなく情報構造を変える方針、2026-10-24 と矛盾しない)。
- 非目標: 実行中 mission の強制中断。週次 wave の頻度変更。`-t` の最長 (24h) や失敗停止の回数を settings にすること (安全側の固定値。チケット裁定)。comment の永続化。daemon 再起動をまたぐ再開。繰り返し全体の終了通知 (§8 の候補)。

### 0.1 前提の再検討 (チケットの案との差)

- **「directives と同じ除染」という関数は無い。** `directives.md` の注入は、空白の畳み込みと管理マーカーの除去だけで、遮断 8 の除染 (`project_last_result` の固定語彙化) は agent が読むシステム由来の文字列 (reject reason、last_result) を対象にしている。利用者本人が書いた文は、方針でも comment でも利用者の意思としてそのまま渡す。本書は「directives と同じ作法」(空白を畳む、1000 字、制御文字を拒否、固定の枠で包む) を comment に適用し、遮断 8 の対象にはしない (§4)。
- **「job を再投入」でなく「job 1 本の中で mission を直列」。** 再投入は、job 表の 1 本枠 (`improve_running`) と冪等 key、autopilot の再確認、client の待ちが mission ごとに要り、間の隙間に週次が割り込む。1 本の job が mission を順に回せば、枠・停止・期限が 1 か所で済む。代償は daemon 再起動で繰り返しが消えること (§5.3)。
- **連続情報は新しい経路が要らない。** 各 mission は開始時に DB から文脈を組む。族別試行結果 (`family_attempts`)、backlog の状態と観察文 (`note`)、履歴は、前の mission の commit 後の状態を自然に読む。繰り返し専用の持ち越しは作らない。comment は逆に、DB に書かないので次の mission に観察文として残らない (二重にならない)。
- **手動 improve は週次の slot を使わない (既存)。** `submit_manual` は slot / wave 行を作らず、`ImproveSupervisor.tick` の空き計算 (slot の数だけ) にも数えられない。つまり今も手動 1 回と週次 wave は重なり得る。24h の繰り返しでは重なる確率が高いので §3.5 で扱う。

## 1. 用語

- **繰り返し job**: `-t` または `-n` を付けた `afx ctl improve` が作る 1 本の job。mission を直列に回す。
- **期限**: job が実際に走り始めた時刻 (単調時計) に `-t` を足した時刻。`-n` だけのときは 24h。
- **完走**: 開始した mission を中断せずに終端まで走らせること。
- **一時指示 (comment)**: `-c` の文。その job の全 mission に同じ文で注入する。job が終わると消える。

## 2. 利用者から見た動き

### 2.1 構文

```
afx ctl improve [-t|--duration <h/m>] [-c|--comment '<文>'] [-n|--max-runs <N>]
```

| 指定 | 動き |
|---|---|
| なし | 従来どおり 1 回 |
| `-c` のみ | 1 回。その mission にだけ comment を注入 |
| `-n N` | 最大 N 回。期限は 24h (先に達した方で止まる) |
| `-t D` | 期限まで。回数の上限は 24 |
| `-t D -n N` | 先に達した方で止まる |

- `<h/m>` は `2h`、`45m`、`1h30m`。`^(\d+h)?(\d+m)?$` で空でなく、合計 1 分以上 24 時間以下 (`90m` や `1h90m` は合計で数える)。単位なし (`30`)、小数 (`1.5h`)、`d`、大文字、空白、`0m` は rc=2。
- `-n` は 1〜24 の整数。範囲外・非整数は rc=2。`-n 1` は従来の 1 回と同じ (繰り返し job の形で走るが 1 回で終わる)。
- `-c` は §4 の文法。違反は rc=2。同じ option の重複、未知 option、値の欠落、`improve add …` との併用は rc=2 (usage)。長形式は `--duration=1h` も受ける。
- 検査はすべて鍵を読む前・接続する前に client で行い、daemon も同じ検査を持つ (client を通らない要求が来るため)。daemon 側の違反は 400 `invalid_argument`。

### 2.2 表示と rc

繰り返し job は、最初の mission の準備が済んだ時点で client が次を出して rc=0 で戻る (job は daemon で続く)。

```
job <id> を受理しました
improve を繰り返します: 期限 2026-10-26 14:30 UTC (1h30m)、最大 3 回、一時指示あり
1 回目 mission #41 を起動しました
各 mission の開始・終了は通知に出ます。止める: afx ctl jobs cancel <id> (実行中の mission は完走し、次を始めません)
daemon を再起動すると繰り返しは中断します。
```

`-c` の本文は出さない (`一時指示あり` だけ)。`-t` も `-n` も無い従来の形は、表示も rc も今のまま。

| rc | 意味 |
|---|---|
| 0 | 受理して 1 回目の mission を起動した (繰り返しでは以後 client は待たない) |
| 1 | `improve_running` (別の improve job が走行中)、autopilot 制限、daemon 側の拒否 |
| 2 | 引数の違反 (上記)、usage |
| 3 | daemon に届かない |
| 130 | 待機中の Ctrl-C。受理済みの job は続く (既存の案内) |

### 2.3 進捗、停止、status

- `afx ctl jobs get <id>` は job の進捗を出す: 状態、実施済み回数、最大回数、期限 (UTC)、最後の mission id、終了後は停止理由。comment は出さない。
- **停止は既存の `afx ctl jobs cancel <id>` を、走行中の繰り返し job にも許す**。現在の mission は完走し、次を始めない (停止要求の記録は即時、job の終端は mission の終了後)。単発 improve の cancel は今まで通り走行中は拒否 (`job_running`)。新しい `improve stop` は作らない (面が増える割に job id が要るのは同じ)。
- 同時に 1 本だけ (既存の 1 本枠)。走行中に再実行すると rc=1 `improve_running`。
- `afx ctl status` / `afx status` の見え方は変えない。走行中の mission が既に出ているならそのまま出る (確認事項 §9)。

## 3. 繰り返しの規則

### 3.1 mission の直列実行と判定点

job は次を繰り返す。

1. 1 回目は期限・停止の判定なしに必ず開始する (最低保証。`-t 1m` でも 1 回)。
2. mission を 1 つ最後まで走らせる (従来の手動 1 回と同じ prepare → run → commit)。
3. 2 回目以降は、**開始する直前にだけ**次を順に判定し、1 つでも当てはまれば止める: 停止要求、daemon の停止、回数が上限、現在時刻が期限以上、autopilot が true、連続失敗が上限 (§3.3)。
4. 当てはまらなければ、週次の slot が空くのを待ってから (§3.5) 次を開始する。

期限は mission の途中では見ない。期限の 1 秒前に始めた mission は完走し、その終了が期限を越えてもよい。1 つの mission の長さの上限は既存の `improve.mission_timeout_sec` (既定 1 時間) なので、job の最悪の長さは期限 + その値 (24h の上限なら約 25h)。期限ちょうどの時刻 (等号) は始めない。

### 3.2 上限

- 期間 24h、`-n` 24、comment 1000 字は固定値で、settings にしない (チケット裁定。理由: 無人で codex / claude の枠を使い切る、429 連続、週次との重なり)。
- `-n` だけのときも、期限は 24h として判定する (無期限の job を作らない)。

### 3.3 失敗時

mission の失敗とは、commit 後の mission の終端が `failed` のこと (runner が 429 や枠切れで失敗した場合も、`timeout` / `max_turns` も含む。`mission_failed` が activity に残るもの)。prepare / run / commit が例外を投げた場合も失敗に数える。

- **連続 2 回失敗したら止める** (成功が 1 回挟まれば数え直し)。同じ原因 (429、枠切れ、runner の不調) で失敗を並べるだけの mission を、24h 分積まないため。停止理由は `consecutive_failures`。値は §8 の裁定候補。
- 1 回目の mission が例外で落ちたとき、繰り返し job も従来の単発と同様にその例外の失敗コードで終わる (2 回目以降の例外は握って数えるだけ)。単発の挙動は変えない。
- mission 間の待ちは置かない (失敗が連続 2 回で止まるので、速く失敗する mission が job を空回しすることはない)。

### 3.4 回数の数え方

「実施」は mission を開始した回数 (失敗も数える)。`-n` と期限は開始前に見るので、`-n 3` は最大 3 回開始する。

### 3.5 週次との排他

手動 mission は slot を持たない (既存)。この性質は変えず、**繰り返し job 側が譲る**: 2 回目以降の開始前に、`improve_wave_slots` に `claimed` / `running` の行があれば、空くまで待つ (30 秒ごとに再確認。この待ちも停止要求と daemon 停止、期限を見る)。1 回目は待たない (従来の手動 1 回と同じ)。週次の tick 側は変えない。待っている間に期限が過ぎたら、次を始めずに止める (`deadline`)。

## 4. comment の扱い

- **文法**: 前後の空白を除いたうえで空でない。1000 字以下 (code point)。改行・タブを含む空白の連続は 1 つの空白に畳む (`directives.md` の `_render_directive` と同じ作法)。畳んだ後で、制御文字・書式文字 (Unicode の `Cc` / `Cf`)・サロゲート・NUL を含むものは拒否 (rc=2)。長さは畳んだ後の文字数で測る。
- **除染**: 本文は変えない (上の畳み込みのみ)。§0.1 のとおり、遮断 8 の語彙化は適用しない。利用者が holdout の数値や口座情報を書けば agent に渡る。これは `policy add` と同じ信頼で、利用者の責任。
- **プロンプトの位置**: テンプレートの「ユーザー方針」節の直後に、comment があるときだけ次の節を入れる (無いときは節ごと出さず、現行のプロンプトとバイト一致)。

  ```
  ## 今回だけの指示

  この job の間だけ有効な指示です。規律 (制約・出力契約) と矛盾する場合は規律が優先します。恒久の方針と食い違う場合は、この指示を優先します。この指示の文を backlog や提案に転記しないでください。

  - <comment>
  ```

  値の差し込みはテンプレートの `.format()` の値として入るので、comment の `{` `}` は展開されない (test で pin)。
- **経路**: comment は daemon のメモリ (job の closure) と、各 mission の prompt にだけある。DB (`ops_requests`、idempotency 表、missions、backlog、event、snapshot)、activity、技術ログ、Discord には書かない。冪等 key は本文の digest だけを持つ (digest は既存の作法)。activity には固定語 `comment=yes` だけ。
- **backlog に残さない**: comment を backlog や note に保存しない。agent が出力に comment を写したときの backlog 行は agent の出力で、防げない (prompt の転記禁止文は誘導)。残余として §8 に書く。
- job が終わる (終端、cancel、daemon 再起動) と comment は消える。

## 5. 配線

### 5.1 job の形

`POST /v1/improve/runs` の本文に任意の `duration_sec` (整数秒)、`max_runs`、`comment` を足す。本文が空の要求は従来と同じ (digest も同じ)。job は既存の `enqueue_single("improve")` を使い、1 本枠と `improve_running` をそのまま保つ。job の `result` は進捗を持つ辞書 (`mission_id`、`runs`、`max_runs`、`deadline_at`、終了後は `stop_reason`)。自由文を含めない (ops-api §8)。`stop_reason` は固定語: `deadline` / `max_runs` / `stopped` / `consecutive_failures` / `autopilot` / `shutdown`。

### 5.2 supervisor

`submit_manual` の本体を 1 mission を走らせる内部関数に切り出し (挙動は同一)、`submit_manual` はそれを 1 回呼ぶ薄い形にする。新規の `submit_repeat` が §3 のループを持つ。判定は純関数 (次を始めるか、止める理由)に出し、時計・停止要求・autopilot・slot の状態は引数で渡す (test しやすくするため)。comment は `prepare` の任意引数で受けてプロンプトの組立てに渡し、`ImproveRunContext` には保存しない。各 mission の開始・終了の通知は、Discord 設計 §4.4 のとおり `prepare` 成功後と終端後に supervisor が mission ごとに出すので、繰り返しのために変えない。

停止要求は job が持つ `threading.Event`。`jobs cancel` が走行中の繰り返し job に対してこれを立てて、現在の job を返す (終端にしない)。

### 5.3 再起動と失敗

- job 表はメモリ上 (ops-api §5)。daemon 再起動で繰り返しは消える。走行中の mission は起動時回収で `interrupted` になり、Discord の `finished` (failed / interrupted) で分かる。繰り返しは再開しない (安全側。期限・回数の再計算と comment の復元が要るため)。開始時の表示 (§2.2) で利用者に伝える。
- autopilot: ops-api §5 の規律 (autopilot=true の後に新しい変更を始めない) を mission ごとに守る。開始前の再確認 (`_autopilot_at_start` と同じ flock 内の読み)を 2 回目以降の開始前に行う。単発の既存の確認は変えない。
- 補償は mission ごと (既存の `compensate_*`)。job の途中で止まった理由は `stop_reason` と activity に固定語 `improve_repeat_finished reason=<固定語> runs=<n>` で残す。

### 5.4 変更しないもの

settings のキー、DB の schema、週次 wave の経路、ImproveRunContext、event の種類 (`improve_run_changed`)。

## 6. 受入

| AC | 内容 |
|---|---|
| 1 | 引数なしは従来と同一 (本文空の要求、表示、rc、digest)。`-c` のみは 1 回で comment が入る |
| 2 | 期間の文法: `2h` `45m` `1h30m` `90m` を受理。`30` `1.5h` `1d` `1H` `0m` ` 1h` `25h` `24h1m` と空を rc=2。`24h` は受理 |
| 3 | `-n` は 1〜24 のみ。重複・未知・値欠落・`improve add` との併用は rc=2。daemon 側も 400 |
| 4 | 最低保証: 期限が過ぎていても 1 回目は開始する (`-t 1m` で 1 回目の mission が 1 分を越えて走っても完走) |
| 5 | 期限の判定点: 期限の 1 秒前に開始した mission は完走し、期限ちょうどでは次を始めない。mission の途中で期限が来ても中断しない。実時計の短い期限 (内部 API で秒指定) で、単調時計の統合 test を 1 本 |
| 6 | `-n` と `-t` は先に達した方で止まる。`-n` のみは 24h の期限を持つ。回数の上限は開始数 (失敗も数える) |
| 7 | 連続 2 回の失敗で止まり `consecutive_failures`。成功が挟まれば数え直す。2 回目以降の例外は握って数える。1 回目の例外は従来どおり job を失敗にする |
| 8 | 停止: `jobs cancel` が走行中の繰り返し job に停止要求を立て、現在の mission は完走して次を始めない (`stopped`)。単発 improve の cancel は従来どおり `job_running` |
| 9 | 週次との排他: slot が `claimed` / `running` の間、2 回目以降は始めず待つ。待ちの間も停止・期限が効く。1 回目は待たない。週次の tick の挙動は変えない |
| 10 | autopilot: 2 回目以降の開始前に再確認し、true なら `autopilot` で止まる。API の principal のみに効き、対話シェルは制限しない (既存) |
| 11 | comment: 畳み込み、1000 字、制御文字・書式文字の拒否。`{}` を含んでも prompt が崩れない。comment が無い prompt は現行とバイト一致。comment ありの prompt に節が 1 つだけ、全 mission に同じ文が入る |
| 12 | comment の不在: DB 全表、activity、技術ログ、event、Discord 送出物、job の GET、ops_requests に canary の comment が無い。activity は `comment=yes` のみ |
| 13 | 再起動: 走行中の繰り返し job の daemon 再起動後、新しい mission が始まらず、走行中の mission は `interrupted` になる |
| 14 | 通知: mission ごとに started / finished が 1 組ずつ出る (繰り返しで増減しない)。繰り返し専用の event は出ない |
| 15 | `jobs get` の進捗が各時点で正しい (`runs`、`stop_reason`)。comment は出ない |
| 16 | `cli_surface` の record と help が `-t` `-c` `-n` を出し、golden が通る。help に literal を置かない (AST 検査) |
| 17 | 変異スイープ: 期限の等号、判定点 (mission 途中で見ない)、1 回目の無条件開始、連続失敗の数え直し、slot 待ちの対象、autopilot の再確認、comment の経路 |

## 7. 変更一覧と task 分割

純 Python。実 kernel・systemd に触れない。

**T1: 繰り返しの中核 (約 300 行 + test 450 行、codex terra)**

- 新規 `src/agentic_fx/core/improve_repeat.py`: 期間・comment の文法 (client と daemon が共有)、停止判定の純関数、定数 (24h / 24 回 / 1000 字 / 連続失敗 2)。
- `core/improve_supervisor.py`: `submit_manual` の本体切り出し、`submit_repeat`、slot 待ち、進捗の更新。
- `loops/improve_loop.py` と `loops/prompts/improve_mission.md`: `prepare` の任意引数 comment、節の差し込み (無いときバイト一致)。
- test: AC 1(core 部)、4〜7、9、11、12(DB・activity・ログ)、13、14。

**T2: 面 (約 250 行 + test 400 行、codex terra)**

- `ops/client.py` (parse、`_op_improve` の繰り返し形: 開始の応答で戻る)、`cli_surface.py` (record、help、golden)、`ops/api_server.py`・`ops/service.py` (本文の検査、`enqueue_single`、停止 Event、autopilot の再確認、進捗の result)、`ops/jobs.py` (走行中 cancel を許す条件)、`docs/operations/ops-api-2026-10-09.md` と ops-api 設計 §5 の job 契約の更新。
- test: AC 1〜3、8、10、12(GET と ops_requests)、15、16。

順序: T1 → T2 (T2 が T1 の文法と `submit_repeat` に依存)。周回: 境界コードではない (資金・隔離・鍵・削除に触れない) が、mission の起動経路と autopilot 規律に触れる。codex 1 周目 → `/code-review high` → 閉鎖確認。配備は daemon の再起動 1 回 (afx-ops は変更なし)。

## 8. 裁定が要る点 (製品判断)

技術で決めたもの (1 本の job、メモリ上、再起動で消える、停止は走行中 cancel の拡張、判定点、slot 譲り、comment を DB に置かない) は本書で決めた。次は製品・運用の判断。

1. **連続失敗で自動停止する回数**: 推奨 **2**。根拠: 429 や枠切れは次も失敗する可能性が高く、1 回の失敗で止めると一時的な不調で繰り返しが無駄になり、3 以上だと枠を空打ちする。ユーザーは無人で回すつもりなので、止まったことは activity と `jobs get` でしか分からない点が代償。
2. **停止の意味**: 推奨 **現在の mission は完走、次を始めない**。根拠: mission の強制中断は Landlock 下の worker の途中終了と補償の新しい経路を作る。代償は、止めても最長 1 時間待つ。
3. **一時指示と恒久方針が食い違うとき**: 推奨 **一時指示を優先** (規律は常に優先)。根拠: 「今回だけ」の融通が目的。恒久方針を上書きするのではなく、その job の間だけ効く。
4. **`-n` / 暗黙の回数の上限**: 推奨 **24**。根拠: 1 時間 1 回なら 24h で 24 回。それ以上は枠を使い切る。変更は定数の書き換えが要る (settings にしない)。
5. **繰り返しの終了通知を出すか**: 推奨 **今回は出さない** (mission ごとの通知と activity の `improve_repeat_finished` だけ)。根拠: 通知の event を増やすと Discord 設計の契約 (`improve_run_changed`) を広げる。連続失敗の停止に気づけない場合は別チケットで足す。
6. **mission ごとの通知の量**: 24h で最大 24 組の開始・終了が出る。推奨 **まとめない**。根拠: 通知の設定 (`notify.improve_events`、開始通知の既定) で利用者が絞れる。

決定済み (再議論しない): `-t` 上限 24h を settings にしない、comment を backlog に残さない、既定は従来の 1 回、週次は変えない。

残余: agent が comment を出力 (backlog 行・提案文) に写したら残る。再起動で繰り返しは消える。繰り返し中の週次との同時実行は、1 回目だけ重なり得る (§3.5)。

## 9. 確認事項 (既存コードとの食い違い・未確認)

1. 現行の `_op_improve` は job が完了するまで最大 45 秒だけ poll し、超えると rc=1 で `待ち時間の上限に達しました` を出す (`_POLL_LIMITS["improve"]`)。mission は通常 45 秒より長いので、`mission #N を起動しました` が出るのは速く終わったときだけ。test は即終了の fake。本書の単発の挙動は変えない (範囲外)。要確認: 実機の `afx ctl improve` の表示。
2. チケットの「directives と同じ除染」に当たる関数は無い (§0.1)。
3. 走行中の mission の prompt が archive などに残るか (comment が worker の作業領域に残るか)。T1 で `archive_*` と transcript の保存先を確認し、残るなら AC 12 の観測点に足す。
4. `afx ctl status` / `afx status` に走行中の improve mission が出るかは未確認 (本書は変えない)。
5. 期限の表示は UTC (news 設計に合わせた)。client の他の時刻表示の流儀と合わせる。
6. 対話シェル (`afx ctl>`) の引数分割で `-c 'a b'` の引用が保たれるか。T2 の parse test で確かめる。
7. mission の失敗の定義 (commit 後の終端 `failed`) は `MissionResult.status` と commit の結果の食い違いに備えて missions の終端を読む。T1 で実装時に決める。

## 10. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-26 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `ctl-improve-repeat-duration-and-oneoff-comment` の設計化 | |

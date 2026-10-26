# afx ctl improve の繰り返し (-t) / 一時的な指示 (-c) / 回数 (-n) 設計書

**版:** v0.2 C0 (設計レビュー 1 周目の反映。ユーザー確認待ちの製品判断 6 件を §8 に残す)

**対象:** `ctl-improve-repeat-duration-and-oneoff-comment` (2026-10-25 のユーザー依頼と裁定)

**関連チケット:** `improve-feedback-structure` (族別試行結果。繰り返しの連続情報はこれに乗る)、`improve-start-and-result-to-discord` (mission ごとの開始・終了通知。設計 `2026-10-25-improve-discord-notice-design.md` §4.4)

**前提:** 操作 API の job 契約 (`2026-10-04-ops-api-design.md` §5)、コマンド面 (`2026-10-23-cli-surface-design.md`)。

## 0. 目的と非目標

目的: 1 回の改善で当たりが出ることは少ないので、`afx ctl improve` を手で何度も打たずに、指定した時間(または回数)の間 mission を続けて回せるようにする。あわせて、恒久の方針 (`directives.md`) に書くほどでない「今回だけの指示」を渡せるようにする。

- 引数なしは従来どおり 1 回で、従来の経路をそのまま使う。週次 wave の頻度と slot の規則は変えない。繰り返しは利用者の明示操作に限る (周回数でなく情報構造を変える方針、2026-10-24 と矛盾しない)。
- 非目標: 実行中 mission の強制中断。週次 wave の頻度変更。`-t` の最長 (24h) や失敗停止の回数を settings にすること (安全側の固定値。チケット裁定)。comment の永続化。daemon 再起動をまたぐ再開。繰り返し全体の終了通知 (§8)。agent が comment を出力へ復唱することの遮断 (§4 の残余)。

### 0.1 前提の再検討 (チケットの案との差)

- **「directives と同じ除染」という関数は無い。** `directives.md` の注入 (`_render_directive`) は、空白の畳み込みと `- ` の付与だけで、遮断 8 の除染 (`project_last_result` の固定語彙化) は agent が読むシステム由来の文字列 (reject reason、last_result) を対象にしている。利用者本人が書いた文は、方針でも comment でも利用者の意思としてそのまま渡す。本書は「directives と同じ作法」(空白を畳む、1000 字、制御文字を拒否、固定の枠で包む) を comment に適用し、遮断 8 の対象にはしない (§4)。
- **「job を再投入」でなく「job 1 本の中で mission を直列」。** 再投入は、job 表の 1 本枠 (`improve_running`) と冪等 key、autopilot の再確認、client の待ちが mission ごとに要り、間の隙間に週次が割り込む。1 本の job が mission を順に回せば、枠・停止・期限が 1 か所で済む。代償は daemon 再起動で繰り返しが消えること (§5.3)。
- **連続情報は新しい経路が要らない。** 各 mission は開始時に DB から文脈を組む。族別試行結果 (`family_attempts`)、backlog の状態と観察文 (`note`)、履歴は、前の mission の commit 後の状態を自然に読む。繰り返し専用の持ち越しは作らない。comment は逆に、afx が DB に書かないので次の mission に観察文として残らない (二重にならない)。
- **手動 improve は週次の slot を持たず、今は重なり得る。** `submit_manual` は slot / wave 行を作らず、`ImproveSupervisor.tick` の容量計算 (slot の数だけ) にも数えられない。24h の繰り返しでは重なる確率が高い。レビューで、「空きを読んでから始める」だけでは週次側の `reserved → claimed` と競合し、1 回目を待たない案は重複を仕様化することが分かった。本書は繰り返し job の全 mission と週次の claim を supervisor 内の admission gate で線形化する (§3.5)。引数なしの従来の手動 1 回はこの gate を通らない (挙動を変えない)。
- **`-c` のみ、`-n 1` も繰り返し job の形で走る。** 引数が 1 つでもあれば同じ経路 (gate、停止、進捗、cancel) に載せる。分岐を減らすため。表示は §2.2 の形になる (最大 1 回)。

## 1. 用語

- **繰り返し job**: `-t` `-n` `-c` のどれかを付けた `afx ctl improve` が作る 1 本の job。mission を直列に回す (最大 1 回の場合も含む)。
- **期限**: 繰り返し controller が走り始めた時刻 (単調時計) に `-t` を足した時刻。`-n` だけのときは 24h。**新しい mission を始めてよいかの判定にだけ使う** (admission の上限)。開始済みの mission と後処理は既存の各 timeout に従う。
- **完走**: 開始した mission を中断せずに終端まで走らせること。
- **一時指示 (comment)**: `-c` の文。その job の全 mission に同じ文で注入する。job が終わると消える。
- **admission gate**: supervisor 内の 1 つの排他。繰り返し job の mission 開始と、週次 slot の `claimed` への遷移を線形化する (§3.5)。

## 2. 利用者から見た動き

### 2.1 構文

```
afx ctl improve [-t|--duration <h/m>] [-c|--comment '<文>'] [-n|--max-runs <N>]
```

| 指定 | 動き |
|---|---|
| なし | 従来どおり 1 回 (従来の経路) |
| `-c` のみ | 繰り返し job の形で 1 回。その mission にだけ comment を注入 |
| `-n N` | 最大 N 回。期限は 24h (先に達した方で止まる) |
| `-t D` | 期限まで。回数の上限は 24 |
| `-t D -n N` | 先に達した方で止まる |

- `<h/m>` は `2h`、`45m`、`1h30m`。`^(\d+h)?(\d+m)?$` で空でなく、合計 1 分以上 24 時間以下 (`90m` や `1h90m` は合計で数える)。単位なし (`30`)、小数 (`1.5h`)、`d`、大文字、空白、`0m` は rc=2。
- `-n` は 1〜24 の整数。範囲外・非整数は rc=2。
- `-c` は §4 の文法。違反は rc=2。同じ option の重複、未知 option、値の欠落、`improve add …` との併用は rc=2 (usage)。長形式は `--duration=1h` も受ける。
- 検査はすべて鍵を読む前・接続する前に client で行い、daemon も同じ検査を持つ (client を通らない要求が来るため)。daemon 側の違反は 400 `invalid_argument`。
- 対話シェル (`afx ctl>`) は現行 `line.split()` で引用を解釈しない。**対話シェルだけ `shlex.split(line, posix=True)` に変える**。引用の不整合は rc=2 相当のエラーを出して shell を続ける (通常 shell と同じ引用が使える)。

### 2.2 表示と rc

繰り返し job は、**repeat 専用の poll 契約**で戻る。client は job の GET を見て、`state=running` かつ `result.mission_id` が付き `result.runs >= 1` になった時点 (1 回目の mission の準備が済んだ時点) で次を出して rc=0 で戻る (job は daemon で続く)。

```
job <id> を受理しました
improve を繰り返します: 期限 2026-10-26 14:30 UTC (1h30m)、最大 3 回、一時指示あり
1 回目 mission #41 を起動しました
各 mission の開始・終了は通知に出ます。止める: afx ctl jobs cancel <id> (実行中の mission は完走し、次を始めません)
daemon を再起動すると繰り返しは中断します。
```

- 期限の表示は、wire 値 (ISO-8601 UTC、`+00:00`) を CLI の formatter だけが上の人向け表記にする。例は formatter の出力と一致させる。
- 準備前に job が終端した場合は rc=1 (失敗コードを出す)。45 秒 (`_POLL_LIMITS["improve"]` の値は変えない) で `mission_id` が付かなければ、既存の「待ち時間の上限に達しました」案内を出して rc=1 (週次の slot が空くのを待っているときなど。job は daemon で続く)。単発の従来形は従来どおり終端まで待つ。
- `-c` の本文は出さない (`一時指示あり` だけ)。引数なしの従来の形は、表示も rc も今のまま。

| rc | 意味 |
|---|---|
| 0 | 受理して 1 回目の mission を起動した (繰り返しでは以後 client は待たない) |
| 1 | `improve_running` (別の improve job が走行中)、autopilot 制限、daemon 側の拒否、準備前の終端、待ち上限 |
| 2 | 引数の違反 (上記)、usage |
| 3 | daemon に届かない |
| 130 | 待機中の Ctrl-C。受理済みの job は続く (既存の案内) |

### 2.3 進捗、停止、status

- `afx ctl jobs get <id>` は job の進捗を出す: 状態、実施済み回数、最大回数、期限、最後の mission id、`phase`、停止要求の有無、終了後は停止理由。comment は出さない。
- **停止は既存の `afx ctl jobs cancel <id>` を、走行中の繰り返し job にも許す**。現在の mission は完走し、次を始めない。応答は `state=running, stop_requested=true` で、CLI は「停止を受け付けました。実行中の mission の終了後に止まります」と出す (GET にも同値)。mission の完了後に `state=done, stop_reason=stopped`。二重 cancel は同じ応答 (冪等)。単発・ask・decision の走行中 cancel は今まで通り `job_running` で拒否。所有者の規則 (`(principal, actor)`) も既存のまま。新しい `improve stop` は作らない (面が増える割に job id が要るのは同じ)。
- 同時に 1 本だけ (既存の 1 本枠)。走行中に再実行すると rc=1 `improve_running`。
- `afx ctl status` / `afx status` の見え方は変えない。表示は**最新 mission 1 件のみ**で、走行中であることの保証ではない。繰り返しの正確な状態は `jobs get`。

## 3. 繰り返しの規則

### 3.1 mission の直列実行と判定点

繰り返し controller は次を繰り返す。

1. **開始の判定** (mission を始める直前、1 回目も含めて毎回): 停止要求 (利用者 cancel)、daemon の停止、autopilot が true のどれかなら、その時点で止める (1 回目でも開始しない。`runs=0` で終わる)。
2. **期限・回数の判定は 2 回目以降だけ**: 回数が上限、現在の単調時刻が期限以上、前回までの失敗による停止条件 (§3.3)。1 回目はこれらを見ない (最低保証。`-t 1m` でも 1 回目は走る)。**無条件なのは期限と回数だけ**で、cancel / daemon 停止 / autopilot / gate は 1 回目も毎回見る。
3. admission gate を取る (§3.5)。待つ間も 1 の停止系と、2 回目以降は期限を 30 秒ごとに見直す。1 回目は期限を無視して gate が空くまで待つ (週次を優先するので、1 回目は週次の slot が解放された後に必ず 1 回走る)。
4. mission を 1 つ最後まで走らせ (§5.2 の 1 mission executor)、commit・補償後の終端を受け取り、gate を放す。
5. 失敗があれば §3.3 の backoff と停止判定をして 1 に戻る。

期限は mission の途中では見ない。期限の 1 秒前に始めた mission は完走し、その終了が期限を越えてもよい。期限ちょうど (等号) では始めない。**期限は新しい mission の admission の上限であり、job の長さの上限ではない**。開始済みの mission と後処理 (prepare、worker の起動・grace、cleanup、commit・補償) は既存の各 timeout (`improve.mission_timeout_sec` ほか) と daemon shutdown の回収に従い、slot 待ちも停止・shutdown で抜ける。job の最悪の長さを一つの式では約束しない。

### 3.2 上限と時計

- 期間 24h、`-n` 24、comment 1000 字は固定値で、settings にしない (チケット裁定。理由: 無人で codex / claude の枠を使い切る、429 連続、週次との重なり)。
- `-n` だけのときも、期限は 24h として判定する (無期限の job を作らない)。
- **時計**: 期限の判定は単調時計。`Clock` 契約 (`now()` のみ) には単調時刻が無いので、repeat controller に `monotonic: Callable[[], float]` を注入する。開始時に `repeat_deadline_mono = start_mono + duration` を一度だけ計算する。表示用の `deadline_at` は、同時に採った UTC の wall time から別に計算する (wire は ISO-8601 UTC)。wall clock が前後しても開始判定は変わらない。
- `repeat_deadline_mono` は新設の値で、既存 `Job.deadline_mono` (improve 開始時の認可・flock のための 35 秒の期限) とは別。後者の意味は変えない。

### 3.3 失敗時

全 mission を同じ outcome に正規化する。executor が prepare / run / commit・補償を実行し、**補償後の DB 終端 `missions.status`** を返す (`MissionResult.status` が `completed` でも commit の schema / gate 判定で `failed` になり得るため、正はこちら)。prepare / run / commit が例外を投げた場合も、補償して失敗として数える。1 回目も 2 回目以降も同じ扱いで、従来の例外再送出は非 repeat の `submit_manual` だけに残す。

- **一般の失敗: 連続 2 回で止める** (成功が 1 回挟まれば数え直し)。停止理由 `consecutive_failures`。
- **回復待ちの失敗: 1 回で止める**。429、usage limit、認証失効は runner が固定の構造化 failure class (`resource_exhausted`) として返す (cli_runner の fatal pattern を controller が使える分類に出す。T1)。停止理由 `resource_exhausted`。回復しない枠にもう 1 mission 分を使わないため。
- **分類できない失敗の後に backoff を置く**: 固定 300 秒 (固定値、settings にしない)。待ちの間も停止要求・daemon 停止・期限を見る。成功の後は置かない。
- 「失敗」は終端が `failed` のもの (`timeout` / `max_turns` も含む)。`interrupted` (daemon 停止による) は失敗に数えず、停止理由 `shutdown` で終わる。

### 3.4 回数の数え方

「実施」は mission を開始した回数 (失敗も数える)。`-n` と期限は開始前に見るので、`-n 3` は最大 3 回開始する。`runs` は mission の開始を確定した時点で数える。

### 3.5 週次との排他 (admission gate)

supervisor が 1 つの gate を持ち、次の規則で線形化する。

- **繰り返しの mission を始める条件**: `improve_wave_slots` に `reserved` / `claimed` / `running` の行が無く、gate に待っている週次の開始が無いこと。満たすときだけ `manual_active` を立てて始める (条件の判定と `manual_active` を立てることは gate の lock 内で一度に行う)。
- **週次側**: `reserved → claimed` の遷移 (claim) と start は、`manual_active` の間は待つ。`manual_active` が下りた後に遷移する。待っている週次の開始があるとき、繰り返しの次の mission は始めない (**週次を優先**)。
- `manual_active` は mission の終端 (commit・補償の後) に必ず下ろす (例外・cancel・shutdown でも)。
- 1 回目も同じ gate を通る。期限は無視するが、cancel / shutdown / autopilot は見る (§3.1)。
- 待ちは 30 秒ごとの再確認で、停止要求・daemon 停止と、2 回目以降は期限を見る。待っている間に期限が来たら次を始めずに止める (`deadline`)。
- 引数なしの従来の手動 1 回はこの gate を通らない (従来どおり)。週次の `tick` の容量計算は変えない。変えるのは claim と start が `manual_active` を待つ点だけ。

## 4. comment の扱い

- **文法**: 正規化の前に文字を走査する。許す空白は space / TAB / CR / LF の明示集合に限る。それ以外の制御文字・書式文字 (Unicode の `Cc` / `Cf`)・サロゲート (`Cs`)・NUL は拒否 (rc=2)。通ったものについて、許す空白の連続を 1 つの space に畳み (Python の `str.split()` の空白定義は使わない)、前後の空白を除く。畳んだ後で空は拒否。長さは畳んだ後の code point で 1000 以下。NBSP など `Zs` の他の空白は拒否も畳みもせずそのまま通す。
- **除染**: 本文は変えない (上の畳み込みのみ)。§0.1 のとおり、遮断 8 の語彙化は適用しない。利用者が holdout の数値や口座情報を書けば agent に渡る。これは `policy add` と同じ信頼で、利用者の責任。
- **プロンプトの位置**: テンプレートの `{user_policy_tail}\n\n## 参照` を `{user_policy_tail}{oneoff_comment_section}\n\n## 参照` に変える。`oneoff_comment_section` は comment が無ければ `""` (現行のプロンプトとバイト一致)、あれば先頭が `\n\n## 今回だけの指示` の次の文字列。comment は `.format()` の値として一度だけ渡る (comment 中の `{` `}` は展開されない。test で pin)。

  ```
  ## 今回だけの指示

  この job の間だけ有効な指示です。規律 (制約・出力契約) と矛盾する場合は規律が優先します。恒久の方針と食い違う場合は、この指示を優先します。この指示の文を backlog や提案に転記しないでください。

  - <comment>
  ```
- **契約 (直接経路に限る)**: **afx が入力された comment を直接永続フィールドへ書かない**。comment は daemon のメモリ (job の closure) と、各 mission の prompt にだけある。DB (`ops_requests`、idempotency 表、missions の入力側、backlog、event、snapshot)、activity、技術ログ、Discord、job の GET には書かない。冪等 key は本文の digest だけを持つ (digest は既存の作法)。activity には固定語 `comment=yes` だけ。
- **残余 (契約の外)**: agent が comment を出力に復唱・派生させたもの (`artifact.summary`、observation、discovery、backlog 行、提案・approval の payload、レポート) は afx の通常の保存経路で残る。mission の output / transcript は DB に保存され、CLI runner の stdout は診断 transcript に常時保存される。prompt の転記禁止文は誘導にとどまる。復唱を禁止するには全出力・transcript・report・approval 経路の情報フロー遮断が要り、prompt の注意書きでは保証できない。範囲外とし、残余として §8 に書く。prompt 本文は WorkerRunner の一時 workdir の `prompt.txt` に入り、通常は終了で消える。opencode backend は prompt を argv に渡す既存経路があり、実行中は `/proc` から見える (残余)。
- job が終わる (終端、cancel、daemon 再起動) と comment は daemon のメモリから消える。

## 5. 配線

### 5.1 job の形と進捗

`POST /v1/improve/runs` の本文に任意の `duration_sec` (整数秒)、`max_runs`、`comment` を足す。`ENDPOINTS` の code・route・scope・件数は変えない (同じ endpoint の本文拡張。routing の期待引数、全 endpoint 表の pin、crash / idempotency grid は更新対象)。本文が空の要求は従来と同じ (digest も同じ、従来の経路)。何か 1 つでもあれば繰り返し job。job は既存の `enqueue_single("improve")` を使い、1 本枠と `improve_running` をそのまま保つ。

job の `result` は進捗を持つ辞書: `mission_id`、`runs`、`max_runs`、`deadline_at` (ISO-8601 UTC)、`phase` (固定語: `waiting_slot` / `running` / `backoff` / `finished`)、`stop_requested`、終了後は `stop_reason`。自由文を含めない (ops-api §8)。`stop_reason` は固定語: `deadline` / `max_runs` / `stopped` / `consecutive_failures` / `resource_exhausted` / `autopilot` / `shutdown`。

**進捗の更新は thread-safe にする**。現行の prepared callback は `job.result` を lock 外で直接更新している。`JobRegistry.update_result(job, patch)` を追加し、registry の lock 内で辞書を丸ごと置換する。`runs` は mission の開始を確定した時点、`mission_id` は同じ更新で公開する。終端の audit / result との競合は CAS で防ぐ。job 側の `cancel_event` と `stop_requested` は registry の lock 内で扱う (§2.3)。

### 5.2 supervisor

`submit_manual` の本体を **1 mission executor** (prepare → run → commit・補償 → DB 終端 `missions.status` を返す) に切り出し (挙動は同一)、`submit_manual` はそれを 1 回呼ぶ薄い形にする。新規の **repeat controller** (`submit_repeat`) が §3 のループ、gate、backoff を持つ。判定は純関数 (次を始めるか、止める理由) に出し、単調時刻・停止要求・daemon stop・autopilot・slot の状態は引数で渡す (test しやすくするため)。

controller には callback interface を渡す: `on_progress(patch)` (job の `update_result` に繋ぐ)、利用者 cancel の `cancel_event`、autopilot の再確認 callback。**利用者 cancel の Event は WorkerRunner の daemon stop Event と分離する**。cancel は走行中 mission を中断せず、daemon stop は既存どおり WorkerRunner が mission を打ち切る (`interrupted`)。comment は `prepare` の任意引数で受けてプロンプトの組立てに渡し、`ImproveRunContext` には保存しない。各 mission の開始・終了の通知は、Discord 設計 §4.4 のとおり `prepare` 成功後と終端後に supervisor が mission ごとに出すので、繰り返しのために変えない。

### 5.3 再起動と失敗

- job 表はメモリ上 (ops-api §5)。daemon 再起動で繰り返しは消える。走行中の mission は起動時回収で `interrupted` になり、Discord の `finished` (failed / interrupted) で分かる。繰り返しは再開しない (安全側。期限・回数の再計算と comment の復元が要るため)。開始時の表示 (§2.2) で利用者に伝える。daemon の通常停止では controller が停止理由 `shutdown` で終わる。
- autopilot: ops-api §5 の規律 (autopilot=true の後に新しい変更を始めない) を mission ごとに守る。開始前の再確認 (`_autopilot_at_start` と同じ flock 内の読み) を**毎回の開始前**に行う (1 回目の確認は job 受理時の既存のものに加えて、controller が開始直前にもう一度)。引数なしの従来の確認は変えない。
- 補償は mission ごと (既存の `compensate_*`)。job の途中で止まった理由は `stop_reason` と activity に固定語 `improve_repeat_finished reason=<固定語> runs=<n>` で残す。controller 自体の予期しない例外は job を `failed` にする (固定の失敗コード)。通常の停止 (上の `stop_reason`) は `state=done`。

### 5.4 変更しないもの

settings のキー、DB の schema、ImproveRunContext、event の種類 (`improve_run_changed`)、週次の `tick` の容量計算、引数なしの手動 1 回の経路。変えるのは、週次の claim / start が `manual_active` を待つ点 (§3.5) と、`JobRegistry` の cancel 条件 (繰り返し job のみ)。

## 6. 受入

| AC | 内容 |
|---|---|
| 1 | 引数なしは従来と同一 (本文空の要求、表示、rc、digest、gate を通らない)。`-c` のみは繰り返し job の形で 1 回、comment が入る |
| 2 | 期間の文法: `2h` `45m` `1h30m` `90m` を受理。`30` `1.5h` `1d` `1H` `0m` ` 1h` `25h` `24h1m` と空を rc=2。`24h` は受理 |
| 3 | `-n` は 1〜24 のみ。重複・未知・値欠落・`improve add` との併用は rc=2。daemon 側も 400。対話シェルの `shlex` 分割: 単一引用、二重引用、escaped quote、空 comment、値が `-n` で始まる comment、引用不整合 (shell 継続) |
| 4 | 最低保証: 期限が過ぎていても 1 回目は開始する (`-t 1m` で 1 回目が 1 分を越えて走っても完走)。ただし cancel / daemon stop / autopilot が先に成立していれば 1 回目も開始しない (`runs=0`、`stopped` / `shutdown` / `autopilot`) |
| 5 | 期限の判定点: 期限の 1 秒前に開始した mission は完走し、期限ちょうど (`now == deadline`) では次を始めない。mission の途中で期限が来ても中断しない。単調時計を注入した test で、wall clock を前後させても開始判定が変わらない。実時計の短い期限 (内部 API で秒指定) で単調時計の統合 test を 1 本。`Job.deadline_mono` (35 秒) は変わらない |
| 6 | `-n` と `-t` は先に達した方で止まる。`-n` のみは 24h の期限を持つ。回数の上限は開始数 (失敗も数える) |
| 7 | 失敗: 全回で同じ outcome (DB の終端 `missions.status` で数える)。1 回目の例外も 2 回目以降と同じく、補償して失敗に数える。一般の失敗は連続 2 回で `consecutive_failures`、成功が挟まれば数え直す。`resource_exhausted` class は 1 回で停止。分類不能の失敗の後は 300 秒の backoff (待ちの間も cancel / shutdown / 期限が効く。成功の後は置かない)。`interrupted` は失敗に数えず `shutdown` |
| 8 | 停止: `jobs cancel` が走行中の繰り返し job に停止要求を立て、応答は `state=running, stop_requested=true`、GET も同値。現在の mission は完走して次を始めない。mission 完了後に `state=done, stop_reason=stopped`。二重 cancel は冪等。cancel と daemon stop の Event は別で、cancel は走行中 mission を中断しない。単発・ask・decision の走行中 cancel は従来どおり `job_running`。所有者の規則も既存 pin のまま |
| 9 | 週次との排他 (競合 test): 1 回目を含む繰り返しの全 mission が gate を通る。`reserved` 作成直後、claim 直前、repeat の gate 取得直前に barrier を置いた競合で、繰り返しと週次の mission が重ならない。週次が待っていれば繰り返しは次を始めない (週次優先)。`manual_active` 中、週次の claim / start は待つ。週次の tick の容量計算は変わらない。待ちの間も停止・期限 (2 回目以降) が効く。1 回目は期限を無視して slot の解放後に走る |
| 10 | autopilot: 毎回の開始前 (1 回目の直前を含む) に再確認し、true なら `autopilot` で止まる。API の principal のみに効き、対話シェルは制限しない (既存) |
| 11 | comment: 畳み込み (許す空白の明示集合)、1000 字、`Cc` / `Cf` / `Cs` / NUL の拒否 (Python の `split()` が消す `Cc` が受理されない)。`{}` を含んでも prompt が崩れない。comment が無い prompt は現行とバイト一致。comment ありの prompt に節が 1 つだけ、全 mission に同じ文が入る |
| 12a | comment の不在 (直接経路): runner を起動前に止める test で、DB 全表、activity、技術ログ、event、Discord 送出物、job の GET、`ops_requests` に canary の comment が無い。activity は `comment=yes` のみ |
| 12b | 残余の確認: 意図的に comment を復唱する fake runner で、復唱が agent 出力の保存経路に残ることを test が確認する (契約の外であることの pin。afx が追加で書いた箇所が無いこと) |
| 13 | 再起動: 走行中の繰り返し job の daemon 再起動後、新しい mission が始まらず、走行中の mission は `interrupted` になる |
| 14 | 通知: mission ごとに started / finished が 1 組ずつ出る (繰り返しで増減しない)。繰り返し専用の event は出ない |
| 15 | `jobs get` の進捗が各時点で正しい (`runs`、`mission_id`、`phase`、`stop_requested`、`stop_reason`)。comment は出ない。`update_result` は lock 内で丸ごと置換し、終端 audit / result との競合 (CAS) で壊れない |
| 16 | `cli_surface` の record、usage、説明、対話シェルの usage、help golden が `-t` `-c` `-n` を出す。help に literal を置かない (AST 検査) |
| 17 | client の poll 契約: `state=running` かつ `mission_id` と `runs>=1` で rc=0 の表示。準備前の終端は rc=1。45 秒で未準備なら既存の待ち上限案内と rc=1。単発は終端まで待つ従来のまま。`afx ctl status` は変わらない |
| 18 | 変異スイープ: 期限の等号、判定点 (mission 途中で見ない)、期限と回数だけが 1 回目で無条件であること (cancel / shutdown / autopilot は見る)、連続失敗の数え直し、`resource_exhausted` の 1 回停止、backoff、gate の対象 (reserved / claimed / running)、週次優先、autopilot の再確認、comment の経路、単調時計 |

## 7. 変更一覧と task 分割

純 Python。実 kernel・systemd に触れない。

**T1: 繰り返しの中核 (約 400 行 + test 550 行、codex terra)**

- 新規 `src/agentic_fx/core/improve_repeat.py`: 期間・comment の文法 (client と daemon が共有)、停止判定の純関数、定数 (24h / 24 回 / 1000 字 / 連続失敗 2 / backoff 300 秒)。
- `core/improve_supervisor.py`: 1 mission executor の切り出し、repeat controller (`submit_repeat`)、admission gate (週次の claim / start 側の待ちを含む)、単調時計の注入、callback interface (`on_progress`、`cancel_event`、autopilot 再確認)。
- `runners/cli_runner.py`: fatal pattern を `resource_exhausted` の構造化 failure class として返す。
- `loops/improve_loop.py` と `loops/prompts/improve_mission.md`: `prepare` の任意引数 comment、節の差し込み (無いときバイト一致)。
- test: AC 1(core 部)、4〜7、9、11、12a、12b、13、14。変異 (AC 18 の core 部)。

**T2: 面 (約 350 行 + test 500 行、codex terra)**

- `ops/jobs.py`: `Job` の `cancel_event` / `stop_requested`、`JobRegistry.update_result`、走行中の繰り返し job のみ cancel を許す条件 (lock 内)。
- `ops/api_server.py` / `ops/service.py`: 本文の検査、`enqueue_single`、callback の結線 (`on_progress` → `update_result`、`cancel_event`)、autopilot の再確認、進捗の result、GET。
- `ops/client.py`: parse、`_op_improve` の繰り返し形 (§2.2 の poll 契約)、cancel の表示、対話シェルの `shlex`。
- `cli_surface.py` (record、usage、説明、対話シェルの usage、help、golden)。
- `docs/operations/ops-api-2026-10-09.md` と ops-api 設計 §5 の job 契約の更新。
- test: AC 1〜3、8、10、12a(GET と `ops_requests`)、15〜17。

**統合条件**: 全 cross-layer AC (4 の cancel-before-first、8、9、12a、13〜15、17、18) は T2 の完了条件とする。T2 の worktree で T1 を取り込んだ状態の実 `JobRegistry` + service + supervisor で通すこと (T1 の単体 fake だけでは完結しない)。

順序: T1 → T2 (T2 が T1 の文法と `submit_repeat` に依存)。周回: 境界コードではない (資金・隔離・鍵・削除に触れない) が、mission の起動経路と autopilot 規律、週次 claim の排他に触れる。codex 1 周目 → `/code-review high` → 閉鎖確認。配備は daemon の再起動 1 回 (afx-ops は変更なし)。

## 8. 裁定

レビュー (設計 1 周目) で、技術で決まるものは確定した: 1 本の job、メモリ上、再起動で消える、停止は走行中 cancel の拡張、判定点、admission gate、週次優先、comment を DB に置かない、回復待ちの失敗 (429・usage limit・認証失効) は 1 回で `resource_exhausted` 停止、daemon shutdown は `interrupted` (cancel と区別)。

次の 6 件は製品・運用の判断で、**推奨 (ユーザー確認待ち)**。実装は推奨で進め、変更が出たら定数・文言の差し替えで済む。

1. **失敗の停止回数: 推奨 2、枠切れ (429・usage limit・認証失効) は即停止**。根拠: 一般の失敗 (runner の不調、スキーマ違反) は一度は偶然がある。2 回続けば原因が残る可能性が高い。枠切れは回復しない状態なので 1 回で止め、もう 1 mission 分を使わない。3 以上は枠を空打ちする。無人で回す前提で、止まったことは `jobs get` と activity の `improve_repeat_finished` でしか分からない点が代償。
2. **cancel の意味: 推奨 現在の mission は完走、次を始めない** (daemon の停止は別で、走行中 mission は `interrupted`)。根拠: mission の強制中断は Landlock 下の worker の途中終了と補償の新しい経路を作る。代償は、止めても最長 1 時間待つ。
3. **一時指示と恒久方針の優先: 推奨 一時指示を優先** (規律は常に優先)。根拠: 「今回だけ」の融通が目的。恒久方針を上書きするのではなく、その job の間だけ効く。
4. **回数の上限: 推奨 24** (`-n` の最大と暗黙の上限)。根拠: 1 時間 1 回なら 24h で 24 回。それ以上は枠を使い切る。変更は定数の書き換えが要る (settings にしない)。
5. **繰り返し全体の終了通知: 推奨 出さない** (mission ごとの通知、`jobs get`、activity の固定語だけ)。根拠: 通知の event を増やすと Discord 設計の契約 (`improve_run_changed`) を広げる。連続失敗の停止に気づけない場合は別チケットで足す。
6. **mission ごとの通知: 推奨 まとめない**。根拠: 24h で最大 24 組の開始・終了が出るが、通知の設定 (`notify.improve_events`、開始通知の既定) で利用者が絞れる。

決定済み (再議論しない): `-t` 上限 24h を settings にしない、comment を backlog に残さない、既定は従来の 1 回、週次の頻度と容量計算は変えない。

**残余 (契約の外)**:

- agent が comment を出力 (backlog 行・提案文・approval payload・レポート・mission output / 診断 transcript) に復唱・派生させたものは残る。prompt の転記禁止文は誘導にとどまる (§4)。
- opencode backend は prompt を argv に渡す既存経路があり、実行中は `/proc` から見える。
- 再起動で繰り返しは消える。
- 繰り返し中の週次は gate で待たされる (週次優先なので繰り返しの次の mission は遅れ得る)。

## 9. 確認事項

設計上の未確認は無い (レビューで、現行の実機相当の動作 = 受理行の後に最大 45 秒 poll して rc=1、`directives` に除染関数は無い、prompt は一時 workdir で通常は消える、`status` は最新 mission 1 件、時刻は ISO 文字列、対話シェルは引用を解釈しない、連続失敗の正は `missions.status`、を確認済み。本文に反映した)。実装時に確かめる点:

1. T1: `reserved` の slot に待ち時間の上限 (期限切れ回収) があるか。あるなら、`manual_active` を待つ週次が回収されて壊れないこと、または待ち時間が上限を越えないことを確かめる (gate で最長 1 mission 分待つため)。
2. T1: cli_runner の fatal pattern を `resource_exhausted` の failure class として `MissionResult` / missions のどこから controller が読むか。クラスが取れないときは「分類不能」(backoff あり、連続 2 回) に倒す。

## 10. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-26 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `ctl-improve-repeat-duration-and-oneoff-comment` の設計化 | |
| 2026-10-26 | v0.2 C0 | admission gate と週次優先 (1 回目も gate 経由)、無条件を期限と回数に限定、comment の契約を直接経路に限定し復唱を残余化、単調時計の注入、失敗の正規化と `resource_exhausted` と backoff、cancel の応答と thread-safe な進捗、repeat 専用の poll 契約、対話シェルの `shlex`、task 分割と統合条件、§8 を確定事項 + ユーザー確認待ちに組み替え | 設計レビュー 1 周目 (sol、詳細 `tmp/review-repeat/c0-r1-triage.md`) | |

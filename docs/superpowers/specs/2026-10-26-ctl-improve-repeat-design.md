# afx ctl improve の繰り返し (-t) / 一時的な指示 (-c) / 回数 (-n) 設計書

**版:** v0.5 C0 (T1 実装とレビュー 1 周目の裁定の反映。v0.4 は設計レビュー 3 周目の反映。§8 の製品判断 6 件はユーザー裁定済、2026-10-26)

**対象:** `ctl-improve-repeat-duration-and-oneoff-comment` (2026-10-25 のユーザー依頼と裁定)

**関連チケット:** `improve-feedback-structure` (族別試行結果。繰り返しの連続情報はこれに乗る)、`improve-start-and-result-to-discord` (mission ごとの開始・終了通知。設計 `2026-10-25-improve-discord-notice-design.md` §4.4)

**前提:** 操作 API の job 契約 (`2026-10-04-ops-api-design.md` §5)、コマンド面 (`2026-10-23-cli-surface-design.md`)。

## 0. 目的と非目標

目的: 1 回の改善で当たりが出ることは少ないので、`afx ctl improve` を手で何度も打たずに、指定した時間(または回数)の間 mission を続けて回せるようにする。あわせて、恒久の方針 (`directives.md`) に書くほどでない「今回だけの指示」を渡せるようにする。

- `-t` `-n` を付けない `afx ctl improve` (引数なし、`-c` のみ) は従来どおり 1 回で、従来の経路をそのまま使う。週次 wave の頻度と slot の規則は変えない。繰り返しは利用者の明示操作に限る (周回数でなく情報構造を変える方針、2026-10-24 と矛盾しない)。
- 非目標: 実行中 mission の強制中断。週次 wave の頻度変更。`-t` の最長 (24h) や失敗停止の回数を settings にすること (安全側の固定値。チケット裁定)。comment の永続化。daemon 再起動をまたぐ再開。繰り返し全体の終了通知 (§8)。agent が comment を出力へ復唱することの遮断 (§4 の残余)。

### 0.1 前提の再検討 (チケットの案との差)

- **「directives と同じ除染」という関数は無い。** `directives.md` の注入 (`_render_directive`) は、空白の畳み込みと `- ` の付与だけで、遮断 8 の除染 (`project_last_result` の固定語彙化) は agent が読むシステム由来の文字列 (reject reason、last_result) を対象にしている。利用者本人が書いた文は、方針でも comment でも利用者の意思としてそのまま渡す。本書は「directives と同じ作法」(空白を畳む、1000 字、制御文字を拒否、固定の枠で包む) を comment に適用し、遮断 8 の対象にはしない (§4)。
- **「job を再投入」でなく「job 1 本の中で mission を直列」。** 再投入は、job 表の 1 本枠 (`improve_running`) と冪等 key、autopilot の再確認、client の待ちが mission ごとに要り、間の隙間に週次が割り込む。1 本の job が mission を順に回せば、枠・停止・期限が 1 か所で済む。代償は daemon 再起動で繰り返しが消えること (§5.3)。
- **連続情報は新しい経路が要らない。** 各 mission は開始時に DB から文脈を組む。族別試行結果 (`family_attempts`)、backlog の状態と観察文 (`note`)、履歴は、前の mission の commit 後の状態を自然に読む。繰り返し専用の持ち越しは作らない。comment は逆に、afx が DB に書かないので次の mission に観察文として残らない (二重にならない)。
- **手動 improve は週次の slot を持たず、今は重なり得る。** `submit_manual` は slot / wave 行を作らず、`ImproveSupervisor.tick` の容量計算 (slot の数だけ) にも数えられない。24h の繰り返しでは重なる確率が高い。レビューで、「空きを読んでから始める」だけでは週次側の `reserved → claimed` と競合し、1 回目を待たない案は重複を仕様化することが分かった。本書は繰り返し job の全 mission と週次の claim を supervisor 内の admission gate で線形化する (§3.5)。引数なしの従来の手動 1 回はこの gate を通らない (挙動を変えない)。
- **繰り返し job になるのは `-t` か `-n` があるときだけ。`-c` は直交する option。** `-c` のみは従来の同期 1 回 (`submit_manual(comment=...)`) で、gate も早期 return も running cancel も持たない (一時指示の追加だけで利用者契約が非同期 job に変わるのは不自然)。`-n 1` は回数の指定なので繰り返し job の形で走る (最大 1 回)。

## 1. 用語

- **繰り返し job**: `-t` か `-n` を付けた `afx ctl improve` が作る 1 本の job。mission を直列に回す (最大 1 回の場合も含む)。`-c` はその全 mission に注入される。
- **期限**: 繰り返し job を受理した時刻 (単調時計。slot 待ちも期限に含む) に `-t` を足した時刻。`-n` だけのときは 24h。**新しい mission を始めてよいかの判定にだけ使う** (admission の上限)。開始済みの mission と後処理は既存の各 timeout に従う。
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
| `-c` のみ | 従来どおり 1 回 (従来の経路、終端まで待つ)。その mission にだけ comment を注入 |
| `-n N` | 最大 N 回。期限は 24h (先に達した方で止まる) |
| `-t D` | 期限まで。回数の上限は 24 |
| `-t D -n N` | 先に達した方で止まる |

繰り返し job になるのは `-t` か `-n` があるときだけ。`-c` はどの形にも付けられる直交 option。

- `<h/m>` は `2h`、`45m`、`1h30m`。`^(\d+h)?(\d+m)?$` で空でなく、合計 1 分以上 24 時間以下 (`90m` や `1h90m` は合計で数える)。単位なし (`30`)、小数 (`1.5h`)、`d`、大文字、空白、`0m` は rc=2。
- `-n` は 1〜24 の整数。範囲外・非整数は rc=2。
- `-c` は §4 の文法。違反は rc=2。同じ option の重複、未知 option、値の欠落、`improve add …` との併用は rc=2 (usage)。長形式は `--duration=1h` も受ける。
- 検査はすべて鍵を読む前・接続する前に client で行い、daemon も同じ検査を持つ (client を通らない要求が来るため)。daemon 側の違反は 400 `invalid_argument`。
- 対話シェル (`afx ctl>`) は現行 `line.split()` で引用を解釈しない。**対話シェルの `improve` 行だけ `shlex.split(line, posix=True)` に変える** (他のコマンドは `split()` のまま。`ask what's up` のような自由文の apostrophe を壊さないため)。引用の不整合は rc=2 相当のエラーを出して shell を続ける (通常 shell と同じ引用が使える)。

### 2.2 表示と rc

繰り返し job は、**受理した時点で rc=0** で戻る。job の初期 result は enqueue 時に完全な形 (`runs=0`、`max_runs`、`deadline_at`、`phase=waiting_slot`、`stop_requested=false`) で公開される (§5.1)。client は受理後に job の GET を最大 45 秒 (`_POLL_LIMITS["improve"]` の値は変えない) 見て、`state=running` かつ `result.mission_id` が付き `result.runs >= 1` になった時点 (1 回目の mission の準備が済んだ時点) で次を出し、rc=0 で戻る (job は daemon で続く)。

```
job <id> を受理しました
improve を繰り返します: 期限 2026-10-26 14:30 UTC (1h30m)、最大 3 回、一時指示あり
1 回目 mission #41 を起動しました
各 mission の開始・終了は通知に出ます。止める: afx ctl jobs cancel <id> (実行中の mission は完走し、次を始めません)
daemon を再起動すると繰り返しは中断します。
```

45 秒で `mission_id` が付かないとき (週次の slot が空くのを待っているときなど) は、3 行目を「週次の slot が空くのを待っています。1 回目は空き次第始まります」に替え、同じく rc=0 で戻る。再投入はしない (再投入すると `improve_running` になる)。

- 期限の表示は、wire 値 (ISO-8601 UTC、`+00:00`) を CLI の formatter だけが上の人向け表記にする。例は formatter の出力と一致させる。
- **rc=1 は job を作らなかった同期拒否に限る**。job id を伴う受理後は、準備前に終端した場合 (`state=failed`、または `runs=0` で `done`: `autopilot` / `shutdown` / `stopped`) も rc=0 で、理由と job id を出す。slot 待ちの長さでは rc=1 にしない。単発の従来形は従来どおり終端まで待つ。
- `-c` の本文は出さない (`一時指示あり` だけ)。引数なしの従来の形は、表示も rc も今のまま。

| rc | 意味 |
|---|---|
| 0 | 受理した (繰り返しでは以後 client は待たない。1 回目が slot 待ちのままでも、受理後に終端していても 0) |
| 1 | job を作らなかった同期拒否 (`improve_running`、autopilot 制限、daemon 側の拒否) |
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
4. mission を 1 つ最後まで走らせ (§5.2 の 1 mission executor)、commit・補償後の `MissionOutcome` を受け取り、`manual_active` を下ろす。
5. 失敗があれば §3.3 の backoff と停止判定をして 1 に戻る。

期限は mission の途中では見ない。期限の 1 秒前に始めた mission は完走し、その終了が期限を越えてもよい。期限ちょうど (等号) では始めない。**期限は新しい mission の admission の上限であり、job の長さの上限ではない**。開始済みの mission と後処理 (prepare、worker の起動・grace、cleanup、commit・補償) は既存の各 timeout (`improve.mission_timeout_sec` ほか) と daemon shutdown の回収に従い、slot 待ちも停止・shutdown で抜ける。job の最悪の長さを一つの式では約束しない。

### 3.2 上限と時計

- 期間 24h、`-n` 24、comment 1000 字は固定値で、settings にしない (チケット裁定。理由: 無人で codex / claude の枠を使い切る、429 連続、週次との重なり)。
- `-n` だけのときも、期限は 24h として判定する (無期限の job を作らない)。
- **時計**: 期限の判定は単調時計。`Clock` 契約 (`now()` のみ) には単調時刻が無いので、repeat controller に `monotonic: Callable[[], float]` を注入する。受理時 (submit_repeat の呼び出し時。controller thread の起動や slot 待ちより前) に `repeat_deadline_mono = start_mono + duration` を一度だけ計算する。表示用の `deadline_at` は、同時に採った UTC の wall time から別に計算する (wire は ISO-8601 UTC)。wall clock が前後しても開始判定は変わらない。
- `repeat_deadline_mono` は新設の値で、既存 `Job.deadline_mono` (improve 開始時の認可・flock のための 35 秒の期限) とは別。後者の意味は変えない。

### 3.3 失敗時

全 mission を同じ outcome に正規化する。executor が prepare / run / commit・補償を実行し、`MissionOutcome(status, failure_class, mission_id)` を返す。`status` は**補償後の DB 終端 `missions.status`** (`MissionResult.status` が `completed` でも commit の schema / gate 判定で `failed` になり得るため、正はこちら)。ただし run の終了後に supervisor の stop が立っていた場合 (graceful shutdown) は、executor 固有の `status=shutdown` を返す。DB の `missions.status` は既存どおり (`failed` / `timeout` など) で変えない。`interrupted` は起動時回収 (`store/missions.py`) だけが書く値で、停止中の controller は読めないので、これには依存しない。prepare / run / commit が例外を投げた場合も補償して `failed` として数える (stop Event が立っていれば `shutdown`)。1 回目も 2 回目以降も同じ扱いで、従来の例外再送出は非 repeat の `submit_manual` だけに残す。

**failure_class** は固定語の閉集合 `resource_exhausted` / `runner_fault` / なし。自由文の `stderr_fatal` は診断用に残し、controller は解析しない。5 段で通す (T1):

1. `CliRunner` が exact pattern を分類し、`MissionResult.failure_class` (`runners/base.py`) に設定する。
2. `mission_worker` の result frame に `failure_class` を足す。
3. `WorkerRunner` が閉集合で検証し (外の値はなしに倒す)、親側の `MissionResult` に復元する。
4. executor が、commit 後に読んだ `missions.status` と手元の class を `MissionOutcome` にまとめる。
5. controller は `status` が `failed` または `timeout` で `failure_class == resource_exhausted` のときだけ即停止する (`completed` / `max_turns` は対象外)。

pattern の分類: `resource_exhausted` は `usage limit`、`try again at`、`429 Too Many Requests`、認証失効 (`401 Unauthorized` の形のみ。素の `Unauthorized` は含めない) の固定 pattern のみ (現行に 429 は無く追加する)。照合は行単位・語境界で行い、複数の class に一致したときは `resource_exhausted` を優先する。`runner_fault` は `code-mode host`、`SIGTRAP`、`closed its stdout`、`Segmentation fault`。`runner_fault` は資源枯渇ではない (一般の失敗)。正確な文字列は実 transcript から確定する (§9)。

- **一般の失敗: 連続 2 回で止める** (成功が 1 回挟まれば数え直し)。`failed` のうち `resource_exhausted` でないもの (`timeout` / `max_turns` / `runner_fault` / class なしを含む)。停止理由 `consecutive_failures`。
- **回復待ちの失敗: 1 回で止める** (`resource_exhausted`、status が `failed` または `timeout` のとき)。回復しない枠にもう 1 mission 分を使わないため。停止理由 `resource_exhausted`。
- **一般の失敗の後に backoff を置く**: 固定 300 秒 (固定値、settings にしない)。同時 controller は 1 本なので jitter は不要。`sleep` でなく Event / Condition の wait とし、cancel / daemon 停止で即座に起き、30 秒ごとに停止系と (2 回目以降は) 期限を見直す。成功の後と `resource_exhausted` の停止は置かない。
- `shutdown` は失敗に数えず、停止理由 `shutdown` で終わる。

### 3.4 回数の数え方

「実施」は mission を開始した回数 (失敗も数える)。`-n` と期限は開始前に見るので、`-n 3` は最大 3 回開始する。`runs` は mission の開始を確定した時点で数える。

### 3.5 週次との排他 (admission gate)

supervisor が 1 つの admission gate (lock + 条件 + `manual_active`) を持つ。**gate は admission だけを守り、runner の実行中は lock を保持しない** (mission 全体を lock してはいけない)。

- **繰り返し側**: gate の lock 内で「現プロセスが所有する週次開始 (`reserved` / `claimed` / `running` の slot)・待っている週次の開始が無い」ことを確かめ、`manual_active` を立てて lock を放す。保持範囲は `manual_active` を立てるまで。条件の判定と `manual_active` の設定は lock 内で一度に行う。
- **週次側**: gate の lock 内で `manual_active` が下りるのを待ち (Condition)、**同じ lock を保持したまま slot の claim Tx (`reserved → claimed`) を完了してから放す**。claim 後は gate を保持しない。待っている週次の開始があるとき、繰り返しの次の mission は始めない (**週次を優先**)。この順序で循環待ちは無い。
- `manual_active` は mission の終端 (commit・補償の後) に `finally` で必ず下ろし、Condition を notify する (例外・cancel・shutdown でも)。
- 繰り返しが待つ slot は、**supervisor が所有登録している週次開始に対応するもの**だけ。DB にある所有登録の無い非終端 slot は待たず、固定語の警告を技術ログに出す (永久待ちを避ける)。
- **孤児 `reserved` の fail-closed 収束** (時限 lease は作らない。daemon crash は既存の起動時回収 `recover_stale_slots`): (a) wave / slot 作成後に slot thread の登録・`start()` が失敗したら、その slot を `failed` にして所有登録を外す。(b) slot thread (`_run_slot_thread`) の top-level `finally` で、所有中の非終端 slot を `failed` に終端し (`store/improve_waves.py` の終端 API)、所有登録を外して gate を notify する。claim 前の例外終了もここに含む。(c) pre-ready の補償が失敗しても slot は `failed` にする。
- 1 回目も同じ gate を通る。期限は無視するが、cancel / shutdown / autopilot は見る (§3.1)。繰り返し側の gate 待ちは、cancel / stop / 2 回目以降の期限 / autopilot を短い刻み (0.5 秒) で確認し、DB の週次占有確認は gate の通知時と `WAIT_RECHECK_SEC` (30 秒) 刻みで行う。gate 待ちの後は時刻を採り直して判定する。待っている間に期限が来たら次を始めずに止める (`deadline`)。
- 週次の `tick` で slot の spawn に失敗したら、その slot を `failed` に収束して残りの slot を起動し、全 slot の処理後に最初の例外を再送出する。
- 引数なし・`-c` のみの手動 1 回はこの gate を通らない (従来どおり)。週次の `tick` の容量計算は変えない。変えるのは claim と start が `manual_active` を待つ点だけ。

## 4. comment の扱い

- **文法**: 正規化の前に文字を走査する。許す空白は space / TAB / CR / LF の明示集合に限る。それ以外の制御文字・書式文字 (Unicode の `Cc` / `Cf`)・サロゲート (`Cs`)・NUL は拒否 (rc=2)。NBSP を含む他の Unicode 空白・区切り (`Zs` / `Zl` / `Zp`) も拒否する (NBSP だけの comment が空でないと見なされるのを防ぐ)。通ったものについて、許す空白の連続を 1 つの space に畳み (Python の `str.split()` の空白定義は使わない)、前後の空白を除く。畳んだ後で空は拒否。長さは畳んだ後の code point で 1000 以下。
- **除染**: 本文は変えない (上の畳み込みのみ)。§0.1 のとおり、遮断 8 の語彙化は適用しない。利用者が holdout の数値や口座情報を書けば agent に渡る。これは `policy add` と同じ信頼で、利用者の責任。
- **プロンプトの位置**: テンプレートの `{user_policy_tail}\n\n## 参照` を `{user_policy_tail}{oneoff_comment_section}\n\n## 参照` に変える。`oneoff_comment_section` は comment が無ければ `""` (現行のプロンプトとバイト一致)、あれば先頭が `\n\n## 今回だけの指示` の次の文字列。comment は `.format()` の値として一度だけ渡る (comment 中の `{` `}` は展開されない。test で pin)。

  ```
  ## 今回だけの指示

  この job の間だけ有効な指示です。規律 (制約・出力契約) と矛盾する場合は規律が優先します。恒久の方針と食い違う場合は、この指示を優先します。この指示の文を backlog や提案に転記しないでください。

  - <comment>
  ```
- **契約 (直接経路に限る)**: **afx が入力された comment を直接永続フィールドへ書かない**。comment は daemon のメモリ (job の closure) と、各 mission の prompt にだけある。DB (`ops_requests`、idempotency 表、missions の入力側、backlog、event、snapshot)、activity、技術ログ、Discord、job の GET には書かない。冪等 key は本文の digest だけを持つ (digest は既存の作法)。activity には固定語 `comment=yes` だけ。
- **冪等と監査の digest**: `ops_requests` / 冪等表の `body_sha256` は comment 本文を除き `has_comment` (真偽) に置き換えた本文から計算する (短い定型文は digest から推測できるため)。代償として、comment だけが違う同じ冪等 key の再送は区別されず、同じ job を返す。
- **残余 (契約の外)**: agent が comment を出力に復唱・派生させたもの (`artifact.summary`、observation、discovery、backlog 行、提案・approval の payload、レポート) は afx の通常の保存経路で残る。mission の output / transcript は DB に保存され、CLI runner の stdout は診断 transcript に常時保存される。prompt の転記禁止文は誘導にとどまる。復唱を禁止するには全出力・transcript・report・approval 経路の情報フロー遮断が要り、prompt の注意書きでは保証できない。範囲外とし、残余として §8 に書く。prompt 本文は WorkerRunner の一時 workdir の `prompt.txt` に入り、通常は終了で消える。opencode backend は prompt を argv に渡す既存経路があり、実行中は `/proc` から見える (残余)。
- comment は消去可能な holder (`CommentHolder`) に入れ、`Job.action` の closure は holder だけを捕捉する (本文を直接捕捉しない)。終端後の `Job` は最大 1 時間または 64 件保持されるので、controller (one-shot の `-c` では `submit_manual` の呼び出し側) の `finally` で `holder.clear()` する。job が終わる (終端、cancel、例外) と保持中の `Job` から本文へ到達できない。daemon 再起動ではメモリごと消える。

## 5. 配線

### 5.1 job の形と進捗

`POST /v1/improve/runs` の本文に任意の `duration_sec` (整数秒)、`max_runs`、`comment` を足す。`ENDPOINTS` の code・route・scope・件数は変えない (同じ endpoint の本文拡張。routing の期待引数、全 endpoint 表の pin、crash / idempotency grid は更新対象)。本文が空の要求は従来と同じ (digest も同じ、従来の経路)。`comment` のみの要求は従来の経路 (one-shot) で、`duration_sec` か `max_runs` がある要求が繰り返し job になる。job は既存の `enqueue_single("improve")` を使い、1 本枠と `improve_running` をそのまま保つ。

job の `result` は進捗を持つ辞書で、**enqueue 時に完全な初期値 (`runs=0`、`phase=waiting_slot` ほか) を設定する**: `mission_id`、`runs`、`max_runs`、`deadline_at` (ISO-8601 UTC。正本は受理時に決めた期限)、`phase` (固定語: `waiting_slot` / `running` / `backoff` / `finished`)、`stop_requested`、終了後は `stop_reason`。自由文を含めない (ops-api §8)。`stop_reason` は固定語: `deadline` / `max_runs` / `stopped` / `consecutive_failures` / `resource_exhausted` / `autopilot` / `shutdown`。

**進捗の更新は thread-safe にする**。現行の prepared callback は `job.result` を lock 外で直接更新している。`JobRegistry.update_result(job, patch)` を追加し、registry の lock 内で辞書を丸ごと置換する。`runs` は mission の開始を確定した時点、`mission_id` は同じ更新で公開する。終端の audit / result との競合は CAS で防ぎ、`JobRegistry.finish()` は registry の lock 内で終端 result を合成し、終端 result は controller が最後に確定した値なので、終端 result が持つキーはその値を優先し、`stop_requested` だけは registry が書く現在値を優先する。終端 result に無い進捗キーは現在の `job.result` の値を残し、`phase` は `finished` にする (進捗更新が途中で失われても終端の `runs` / `stop_reason` が正しい)。job 側の `cancel_event` と `stop_requested` は registry の lock 内で扱う (§2.3)。

### 5.2 supervisor

`submit_manual` の本体を **1 mission executor** (prepare → run → commit・補償 → `MissionOutcome` を返す) に切り出し (挙動は同一)、`submit_manual` はそれを 1 回呼ぶ薄い形にする。新規の **repeat controller** (`submit_repeat`) が §3 のループ、gate、backoff を持つ。判定は純関数 (次を始めるか、止める理由) に出し、単調時刻・停止要求・daemon stop・autopilot・slot の状態は引数で渡す (test しやすくするため)。

controller には callback interface を渡す: `on_progress(patch)` (job の `update_result` に繋ぐ)、利用者 cancel の `cancel_event`、autopilot の再確認 callback。**利用者 cancel の Event は WorkerRunner の daemon stop Event と分離する**。cancel は走行中 mission を中断せず、daemon stop も改善 mission を打ち切らない (改善 mission の WorkerRunner に daemon の stop_event は渡っていない。`improve_loop.py` の `_build_worker_runner`、現行挙動が正。shutdown 挙動の変更は本書の範囲外)。走行中 mission は自分の timeout まで走り、終了後に supervisor の stop を見て、executor が repeat 用の outcome として `shutdown` を返す。DB の mission は既存の補償終端 (`failed` / `timeout` など) のまま。`interrupted` は crash-restart の起動時回収専用で、停止中の controller は使わない。comment は `prepare` の任意引数で受けてプロンプトの組立てに渡し、`ImproveRunContext` には保存しない。各 mission の開始・終了の通知は、Discord 設計 §4.4 のとおり `prepare` 成功後と終端後に supervisor が mission ごとに出すので、繰り返しのために変えない。

### 5.3 再起動と失敗

- job 表はメモリ上 (ops-api §5)。**graceful shutdown (通常停止)** では、走行中 mission は打ち切られず自分の timeout まで走り (WorkerRunner に daemon stop は渡っていない。shutdown 挙動の変更は範囲外)、終了後に supervisor の stop を見て executor が `shutdown` outcome を返し、controller が停止理由 `shutdown` で終わる (shutdown-before-first、worker ready 前、running、backoff 中を別々に test)。**crash-restart** (daemon が落ちた場合) は controller が消えており、繰り返しは再開しない (安全側。期限・回数の再計算と comment の復元が要るため)。走行中の mission は起動時回収で `interrupted` になり、Discord の `finished` (failed / interrupted) で分かる。二つは別経路。開始時の表示 (§2.2) で利用者に伝える。
- autopilot: ops-api §5 の規律 (autopilot=true の後に新しい変更を始めない) を mission ごとに守る。開始前の再確認 (`_autopilot_at_start` と同じ flock 内の読み) を**毎回の開始前**に行う (1 回目の確認は job 受理時の既存のものに加えて、controller が開始直前にもう一度)。引数なしの従来の確認は変えない。
- 補償は mission ごと (既存の `compensate_*`)。job の途中で止まった理由は `stop_reason` と activity に固定語 `improve_repeat_finished reason=<固定語> runs=<n>` で残す。controller 自体の予期しない例外は、既存 `_run_single` と同じく job を `failed` (固定の失敗コード `internal`、新設しない) にする。その前に `finally` で `manual_active` の解放、comment holder の消去、最後の進捗の保持を行う。通常の停止 (上の `stop_reason`) は `state=done`。

### 5.4 変更しないもの

settings のキー、DB の schema、ImproveRunContext、event の種類 (`improve_run_changed`)、週次の `tick` の容量計算、引数なし・`-c` のみの手動 1 回の経路。変えるのは、週次の claim / start が `manual_active` を待つ点 (§3.5)、週次 slot の thread 失敗経路の収束 (§3.5)、`JobRegistry` の cancel 条件 (繰り返し job のみ)、runner protocol への `failure_class` の追加 (§3.3)。

## 6. 受入

| AC | 内容 |
|---|---|
| 1 | 引数なし・`-c` のみは従来と同一の経路 (本文空の要求は表示、rc、digest が従来と同じ。gate を通らない)。`-c` のみは同期で 1 回、comment が入る。`-n 1` は繰り返し job の形で 1 回 |
| 2 | 期間の文法: `2h` `45m` `1h30m` `90m` を受理。`30` `1.5h` `1d` `1H` `0m` ` 1h` `25h` `24h1m` と空を rc=2。`24h` は受理 |
| 3 | `-n` は 1〜24 のみ。重複・未知・値欠落・`improve add` との併用は rc=2。daemon 側も 400。対話シェルの `shlex` 分割: 単一引用、二重引用、escaped quote、空 comment、値が `-n` で始まる comment、引用不整合 (shell 継続) |
| 4 | 最低保証: 期限が過ぎていても 1 回目は開始する (`-t 1m` で 1 回目が 1 分を越えて走っても完走)。ただし cancel / daemon stop / autopilot が先に成立していれば 1 回目も開始しない (`runs=0`、`stopped` / `shutdown` / `autopilot`) |
| 5 | 期限の判定点: 期限の 1 秒前に開始した mission は完走し、期限ちょうど (`now == deadline`) では次を始めない。mission の途中で期限が来ても中断しない。単調時計を注入した test で、wall clock を前後させても開始判定が変わらない。実時計の短い期限 (内部 API で秒指定) で単調時計の統合 test を 1 本。`Job.deadline_mono` (35 秒) は変わらない |
| 6 | `-n` と `-t` は先に達した方で止まる。`-n` のみは 24h の期限を持つ。回数の上限は開始数 (失敗も数える) |
| 7 | 失敗: 全回で同じ outcome (`MissionOutcome` の DB 終端 `missions.status` で数える)。1 回目の例外も 2 回目以降と同じく、補償して失敗に数える。一般の失敗は連続 2 回で `consecutive_failures`、成功が挟まれば数え直す。`failure_class` は CliRunner → `MissionResult` → worker frame → WorkerRunner → `MissionOutcome` の 5 段を通し、`usage limit` / `try again at` / `429` / 認証失効 (`401 Unauthorized` の形のみ。素の `Unauthorized` は対象外) は `resource_exhausted` で 1 回停止 (status が `failed` / `timeout` のとき。`completed` / `max_turns` は対象外)、行単位・語境界で照合し複数一致では `resource_exhausted` 優先、`SIGTRAP` / `Segmentation fault` / `code-mode host` / `closed its stdout` は `resource_exhausted` にならない (一般の失敗)。worker frame の閉集合外の値はなしに倒す。一般の失敗の後は 300 秒の backoff (Event wait で cancel / shutdown に即応し、30 秒ごとに期限も見直す。成功の後は置かない)。graceful shutdown は `shutdown` outcome で失敗に数えず、停止理由 `shutdown` |
| 8 | 停止: `jobs cancel` が走行中の繰り返し job に停止要求を立て、応答は `state=running, stop_requested=true`、GET も同値。現在の mission は完走して次を始めない。mission 完了後に `state=done, stop_reason=stopped`。二重 cancel は冪等。cancel と daemon stop の Event は別で、cancel は走行中 mission を中断しない。単発・ask・decision の走行中 cancel は従来どおり `job_running`。所有者の規則も既存 pin のまま |
| 9 | 週次との排他 (競合 test): 1 回目を含む繰り返しの全 mission が gate を通る。gate は admission のみで、runner 実行中に gate の lock が取れる (別 thread が lock を取得できることで保持範囲を pin)。`reserved` 作成直後、claim 直前、repeat の gate 取得直前に barrier を置いた競合で、繰り返しと週次の mission が重ならない。週次の claim Tx は gate 保持のまま完了する。週次が待っていれば繰り返しは次を始めない (週次優先)。`manual_active` 中、週次の claim / start は待つ。週次の tick の容量計算は変わらない。待ちの間も停止・期限 (2 回目以降) が効く (0.5 秒刻み)。週次 tick の slot spawn 失敗は、その slot が `failed` に収束し残りが起動し、最初の例外が再送出される。1 回目は期限を無視して slot の解放後に走る。**孤児収束**: slot thread の登録・`start()` 失敗、claim 前の例外終了、pre-ready の補償失敗のそれぞれで、`reserved` が残らず (`failed`)、繰り返しが永久待ちしない。所有登録の無い非終端 slot は待たない |
| 10 | autopilot: 毎回の開始前 (1 回目の直前を含む) に再確認し、true なら `autopilot` で止まる。API の principal のみに効き、対話シェルは制限しない (既存) |
| 11 | comment: 畳み込み (許す空白の明示集合)、1000 字、`Cc` / `Cf` / `Cs` / NUL の拒否 (Python の `split()` が消す `Cc` が受理されない)、`Zs` / `Zl` / `Zp` (NBSP だけの comment を含む) の拒否。`{}` を含んでも prompt が崩れない。comment が無い prompt は現行とバイト一致。comment ありの prompt に節が 1 つだけ、全 mission に同じ文が入る |
| 12a | comment の不在 (直接経路): runner を起動前に止める test で、DB 全表、activity、技術ログ、event、Discord 送出物、job の GET、`ops_requests` に canary の comment が無い。activity は `comment=yes` のみ。**メモリ**: job の終端・cancel・controller 例外のあと、保持中の `Job` (action の closure を含む) から canary に到達できず、`CommentHolder` が空 |
| 12b | 残余の確認: 意図的に comment を復唱する fake runner で、復唱が agent 出力の保存経路に残ることを test が確認する (契約の外であることの pin。afx が追加で書いた箇所が無いこと) |
| 13 | 停止と再起動の区別: graceful shutdown を shutdown-before-first (`runs=0`)、worker ready 前、mission running (打ち切られず自分の timeout まで走り、終了後に `shutdown`)、backoff 中の 4 点で別々に test し、いずれも新しい mission が始まらず停止理由 `shutdown` で失敗に数えない。crash-restart (daemon 異常終了後の再起動) では繰り返しが再開せず、走行中の mission は起動時回収で `interrupted` になる |
| 14 | 通知: mission ごとに started / finished が 1 組ずつ出る (繰り返しで増減しない)。繰り返し専用の event は出ない |
| 15 | `jobs get` の進捗が各時点で正しい (`runs`、`mission_id`、`phase`、`stop_requested`、`stop_reason`)。comment は出ない。`update_result` は lock 内で丸ごと置換し、終端 audit / result との競合 (CAS) で壊れない。`finish()` のあとも最後の `phase` / `stop_reason` / `runs` / `mission_id` / `stop_requested` が残り、同名キーは終端 result の値、`stop_requested` は現在値が勝ち、終端に無いキーは進捗が残る (同名キーの競合 test、進捗更新が失われた場合の test)。cancel 応答に `stop_requested` が必ず入る |
| 16 | `cli_surface` の record、usage、説明、対話シェルの usage、help golden が `-t` `-c` `-n` を出す。help に literal を置かない (AST 検査) |
| 17 | client の poll 契約: 繰り返し job は受理で rc=0。enqueue 時の初期 result が `phase=waiting_slot` で公開される。slot 待ちが 45 秒を超えても rc=0 で「slot 待ち」の表示になり、同じ job が続き、再投入されない。`state=running` かつ `mission_id` と `runs>=1` で 1 回目の起動行を出す。受理後に終端した場合 (`failed`、`runs=0` の `done`) も rc=0 で理由を出す。rc=1 は job を作らなかった同期拒否だけ。単発は終端まで待つ従来のまま。`afx ctl status` は変わらない |
| 18 | 変異スイープ: 期限の等号、判定点 (mission 途中で見ない)、期限と回数だけが 1 回目で無条件であること (cancel / shutdown / autopilot は見る)、連続失敗の数え直し、`resource_exhausted` の 1 回停止、backoff、gate の対象 (reserved / claimed / running)、週次優先、autopilot の再確認、comment の経路、単調時計 |

## 7. 変更一覧と task 分割

純 Python。実 kernel・systemd に触れない。

**T1: 繰り返しの中核 (約 550 行 + test 750 行、codex terra)**

- 新規 `src/agentic_fx/core/improve_repeat.py`: 期間・comment の文法 (client と daemon が共有)、停止判定の純関数、`CommentHolder`、`MissionOutcome`、定数 (24h / 24 回 / 1000 字 / 連続失敗 2 / backoff 300 秒)。
- `core/improve_supervisor.py`: 1 mission executor の切り出し、repeat controller (`submit_repeat`)、admission gate (週次の claim / start 側の待ちと、claim Tx までの lock 保持を含む)、単調時計の注入、callback interface (`on_progress`、`cancel_event`、autopilot 再確認)、graceful shutdown の `shutdown` outcome、slot thread の登録・起動失敗と top-level `finally` の収束。
- `store/improve_waves.py`: 非終端 slot の fail-closed 終端 (孤児 `reserved` の収束)。時限回収は作らない。
- `runners/base.py` (`MissionResult.failure_class`)、`runners/cli_runner.py` (pattern の分類。`resource_exhausted` と `runner_fault`)、`mission_worker.py` (実体 `src/agentic_fx/mission_worker.py`。result frame)、`runners/worker_runner.py` (閉集合の検証と復元)。
- `loops/improve_loop.py` と `loops/prompts/improve_mission.md`: `prepare` の任意引数 comment、節の差し込み (無いときバイト一致)。
- test: AC 1(core 部)、4〜7、9、11、12a、12b、13、14。変異 (AC 18 の core 部)。

**T2: 面 (約 400 行 + test 550 行、codex terra)**

- `ops/jobs.py`: `Job` の `cancel_event` / `stop_requested`、`JobRegistry.update_result`、`finish()` の進捗保持、走行中の繰り返し job のみ cancel を許す条件 (lock 内)、retention 後に comment へ到達できないこと。
- `ops/api_server.py` / `ops/service.py`: 本文の検査 (`comment` のみは one-shot)、`enqueue_single` と完全な初期 result、callback の結線 (`on_progress` → `update_result`、`cancel_event`)、autopilot の再確認、`CommentHolder` の closure、controller 例外の `internal`、GET。
- `ops/client.py`: parse、`_op_improve` の繰り返し形 (§2.2: 受理で rc=0、slot 待ち表示)、cancel の表示、対話シェルの `shlex`。
- `cli_surface.py` (record、usage、説明、対話シェルの usage、help、golden)。
- `docs/operations/ops-api-2026-10-09.md` と ops-api 設計 §5 の job 契約の更新。
- test: AC 1〜3、8、10、12a(GET、`ops_requests`、メモリ)、15〜17。

**統合条件**: 全 cross-layer AC (4 の cancel-before-first、8、9、12a、13〜15、17、18) は T2 の完了条件とする。T2 の worktree で T1 を取り込んだ状態の実 `JobRegistry` + service + supervisor で通すこと (T1 の単体 fake だけでは完結しない)。

順序: T1 → T2 (T2 が T1 の文法と `submit_repeat` に依存)。周回: 境界コードではない (資金・隔離・鍵・削除に触れない) が、mission の起動経路と autopilot 規律、週次 claim の排他に触れる。codex 1 周目 → `/code-review high` → 閉鎖確認。配備は daemon の再起動 1 回 (afx-ops は変更なし)。

## 8. 裁定

レビュー (設計 1〜2 周目) で、技術で決まるものは確定した: 1 本の job、メモリ上、再起動で消える、停止は走行中 cancel の拡張、判定点、admission gate (admission のみ保持)、週次優先、comment を DB に置かない、回復待ちの失敗 (429・usage limit・認証失効) は 1 回で `resource_exhausted` 停止、graceful shutdown は executor 固有の `shutdown` (crash-restart と区別)、repeat は `-t` か `-n` があるときだけ、受理で rc=0。

次の 6 件は製品・運用の判断で、**ユーザー裁定済 (2026-10-26「推奨で」)**。定数・文言の差し替えで変えられる。

1. **失敗の停止回数: 2、枠切れ (429・usage limit・認証失効) は即停止**。一般の失敗は一度は偶然がある。2 回続けば原因が残る可能性が高い。枠切れは回復しない状態なので 1 回で止め、もう 1 mission 分を使わない。3 以上は枠を空打ちする。止まったことは `jobs get` と activity の `improve_repeat_finished` でしか分からない点が代償。
2. **cancel の意味: 現在の mission は完走、次を始めない** (daemon の停止も走行中 mission を打ち切らず、終了後に `shutdown`。§5.2)。mission の強制中断は Landlock 下の worker の途中終了と補償の新しい経路を作る。代償は、止めても最長 1 時間待つ。
3. **一時指示と恒久方針の優先: 一時指示を優先** (規律は常に優先)。「今回だけ」の融通が目的で、その job の間だけ効く。
4. **回数の上限: 24** (`-n` の最大と暗黙の上限)。1 時間 1 回なら 24h で 24 回。それ以上は枠を使い切る。変更は定数の書き換えが要る (settings にしない)。
5. **繰り返し全体の終了通知: 出さない** (mission ごとの通知、`jobs get`、activity の固定語だけ)。通知の event を増やすと Discord 設計の契約 (`improve_run_changed`) を広げる。連続失敗の停止に気づけない場合は別チケットで足す。
6. **mission ごとの通知: まとめない**。24h で最大 24 組の開始・終了が出るが、通知の設定 (`notify.improve_events`、開始通知の既定) で利用者が絞れる。

決定済み (再議論しない): `-t` 上限 24h を settings にしない、comment を backlog に残さない、既定は従来の 1 回、週次の頻度と容量計算は変えない。

**残余 (契約の外)**:

- agent が comment を出力 (backlog 行・提案文・approval payload・レポート・mission output / 診断 transcript) に復唱・派生させたものは残る。prompt の転記禁止文は誘導にとどまる (§4)。
- opencode backend は prompt を argv に渡す既存経路があり、実行中は `/proc` から見える。
- 再起動で繰り返しは消える。
- 繰り返し中の週次は gate で待たされる (週次優先なので繰り返しの次の mission は遅れ得る)。

## 9. 確認事項

設計上の未確認は無い。実装時に確かめる点だけ残す。

1. T1: `resource_exhausted` / `runner_fault` の pattern の exact な文字列 (特に 429 と認証失効) は、実 transcript から確定し、fixture も実 transcript から作る。クラスが取れない失敗は「一般の失敗」(backoff あり、連続 2 回) に倒す。

## 10. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-26 | v0.1 C0 | 初版 (指揮者承認前の清書) | チケット `ctl-improve-repeat-duration-and-oneoff-comment` の設計化 | |
| 2026-10-26 | v0.2 C0 | admission gate と週次優先 (1 回目も gate 経由)、無条件を期限と回数に限定、comment の契約を直接経路に限定し復唱を残余化、単調時計の注入、失敗の正規化と `resource_exhausted` と backoff、cancel の応答と thread-safe な進捗、repeat 専用の poll 契約、対話シェルの `shlex`、task 分割と統合条件、§8 を確定事項 + ユーザー確認待ちに組み替え | 設計レビュー 1 周目 (sol、詳細 `tmp/review-repeat/c0-r1-triage.md`) | |
| 2026-10-26 | v0.3 C0 | repeat の判定を `-t` / `-n` に (`-c` 単独は one-shot)、受理で rc=0 と初期 `phase=waiting_slot`、gate の lock 範囲 (admission のみ、週次は claim Tx まで)、孤児 `reserved` の fail-closed 収束、`MissionOutcome` と `failure_class` の 5 段 (resource_exhausted / runner_fault の pattern)、graceful shutdown の outcome と crash-restart の区別、`CommentHolder` の明示消去と Job retention、`Zs` / `Zl` / `Zp` の拒否、controller 例外は `internal`、`finish()` の進捗保持、§8 を裁定済に、§9 を実装時確認のみに、T1 / T2 の範囲と AC 1・7・9・11・12a・13・15・17 を追従 | 設計レビュー 2 周目 (sol、詳細 `tmp/review-repeat/c0-r2-triage.md`) | |
| 2026-10-26 | v0.4 C0 | job id を伴う受理後の終端はすべて rc=0 (rc=1 は同期拒否だけ、§2.2 と AC 17)、daemon stop 時の契約を `shutdown` outcome に統一 (DB mission は既存の補償終端、`interrupted` は起動時回収専用、§5.2)、`finish()` は lock 内で合成し進捗キーは現在の `job.result` 優先 (§5.1、AC 15 に同名キー競合 test) | 設計レビュー 3 周目 (sol 差分閉鎖確認、`tmp/review-repeat/c0-r3-out.md`) | |
| 2026-10-27 | v0.5 C0 | 期限の正本を受理時に (slot 待ちも含む、§1・§3.2・§5.1)、daemon stop は改善 mission を打ち切らず終了後に `shutdown` outcome (§3.3・§5.2・§5.3・AC13、shutdown 挙動の変更は範囲外)、mission_worker の実体パス (§7)、`resource_exhausted` は failed / timeout のみ・行単位語境界・優先・`401 Unauthorized` 形のみ (§3.3・AC7)、週次 tick の spawn 失敗の収束と再送出 (§3.5)、gate 待ちの確認刻み (0.5 秒 / 30 秒) と時刻の採り直し (§3.5・AC9) | T1 実装とレビュー 1 周目の裁定 | |
| 2026-10-27 | v0.5.1 | §8-2 の daemon 停止の記述を §5.2 に合わせる、対話シェルの shlex は `improve` 行だけ (§2.1) | T2 準備の食い違い D1 / D10 | |
| 2026-10-27 | v0.5.2 | `finish()` の合成規則を変更: 同名キーは終端 result、`stop_requested` だけ現在値、終端に無い進捗キーは残す (§5.1、AC 15) | T2 レビュー 1 周目 (進捗更新が握りつぶされると古い `runs` が終端に残る) | |
| 2026-10-27 | v0.5.3 | 冪等・監査の digest から comment を除き `has_comment` に (§4) | T2 レビュー 1 周目 | |

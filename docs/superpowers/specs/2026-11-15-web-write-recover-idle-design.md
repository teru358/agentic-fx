# web-write-recover-idle-interruption — C0.4

対象: idle（未 enable / disabled）の write 回復。実装・commit は含めない。
根拠: `docs/tickets/open/web-write-recover-idle-interruption.md`、関連設計 `2026-11-08-web-plugin-improve-hub-design.md` の B1・受入 8b。
行番号は本 worktree の調査時点。以下 `W` = `src/agentic_fx/ops_setup/web_write.py`、`S` = 同 `stages.py`、`K` = 同 `write_key.py`。

## §0 何が起きているか

`_switch` は stage_mode が idle なら `_recover_idle` へ渡す（W:195–200）。回復は整合済みなら目標に関係なく not_enabled（W:138–140）。停止前の ActiveState はメモリだけで、stage は書かない（W:150–159、S:512–515 は既存フィールドがあるため通らない）。

| 中断点（既存 hook） | 残る実物 | 同じ目標で再実行した結果・根拠 |
|---|---|---|
| 停止後・整合前（after_stop / after_lock / before_keys） | web 所有 marker、afx 停止、不整合 | 整合操作へ進むが、今回観測した inactive/failed を停止前状態にしてしまう。marker は消せても afx を起こさない（W:150–171）。 |
| 設定整合後・marker 削除前（after_config / before_unmark） | 鍵・write 整合、marker、afx 停止 | Consistent で not_enabled。marker の解消にも起動にも届かない（W:138–140、169–171）。 |
| marker 削除後・起動前（after_unmark） | 鍵・write 整合、marker 無し、afx 停止 | 同じく not_enabled。元の稼働状態を失い、起動しない（W:138–140、170–171）。 |

チケットの現象記述と相違なし。ただし W:132 の「同じコマンドの再実行で収束」は鍵・設定についてだけで、marker と起動までの回復保証にはならない。config 不在の --no-write では config 書込みを省くため、after_keys が既に整合後となる（W:142–145、163）。idle の起動には現状 start hook が無い（W:170–171）。

`_quiesce_afx` は marker → stop → 停止確認 → instance lock（S:506–542）。`_unmark` は durable 削除と前後 hook（W:287–297、S:171–173）。`_guarded` は排他 lock ではなく OSError の記録境界であり、idle は `_current` 未設定かつ unrecorded のため stage に失敗を残さない（S:251–254、294–312）。

## §1 動作

### 記録と既存 stage の関係

**推奨: 別 file `root/data/ops/web.write-recovery.json`。** OpsSetup の回復状態機械が所有する。通常の web.stage.json は未作成なら未作成、disabled なら内容を変えずに保つ。

JSON v1 の必須項目:

| 項目 | 値・用途 |
|---|---|
| version / feature / operation | 1 / web / recover_idle（通常 enable と区別） |
| target | bool。今回収束させる write。principal は固定対応から導出し重複保存しない。 |
| afx_before_stop | 最初に観測した ActiveState。active/activating/reloading のみ起動を戻す（S:45）。再開で上書きしない。 |
| idle_origin | never / disabled。通常 stage と矛盾していないか照合する。 |

記録が持つのは上記6項目だけ。failure / failure_detail や進捗は保存せず、失敗理由は例外・診断で返す。鍵・password・設定の複製は持たない。設定は再読込みし port 等を保持して write だけ合わせる。

既存規則との照合:

- read_stage は operation が enable/disable のみ、各段を固定列挙で検証する（S:79–144）。recover の追加は既存形式の拡張になる。
- stage_mode は「無し / disable.disabled = idle」「enable.enabled = enabled」、他は unfinished（K:120–126）。stage の新 mode は全呼出側へ影響する。
- `_unfinished` は通常の終端と write_target の有無で競合を止める（S:265–275）。disabled に write_target を載せる案も形式上は可能だが、idle の履歴と回復の進捗を混ぜ、未 enable に架空の stage を作る。
- `_active_features` は disable の途中を有効扱いする（S:277–287）。回復を disable の途中で表すと実態と食い違う。
- `_check_marker_owner` は feature だけを判定する（S:484–496）。別 file でもこの検査を省かず、他機能・壊れた marker は回収しない。同じ web の marker は、正常な idle stage と回復記録の照合後だけ引き継ぐ。

stage に recover mode を追加する案は影響が広く、disabled に切替フィールドを流用する案は未 enable の意味を壊す。別 file の代償（全入口で追加照合）は、共通の読取専用 pending 検査で閉じる。

marker に afx_before_stop と target を載せる案も不採用。marker は `_unmark` で durable 削除した後に start するため（W:169–171、287–297）、その間に中断すると復帰に必要な情報を失う。
marker を起動後まで残す案も、起動阻止のために停止前に置く既存の役割（S:506–524）と衝突する。別 file なら marker の寿命と回復情報の寿命を分けられる。

### 状態機械と再実行

回復記録が保証するのは、止める前の afx の状態の復元（元停止なら起こさない）と web 所有 marker の解消だけである。鍵・write の継続的な整合そのものは保証しない。安全性は既存の fail-closed 検査が担う。enable は `_check_web_write` → `check_write_key_consistency` で不整合を拒否する（S:548–558、576–583、K:72–117）。web 起動は `afx-ops/afx_ops/isolation/startup.py:881–899、1030` の `read_api_token` で write に対応する principal の state・digest を検査し、他方の鍵にフォールバックしない。write=false では読取 principal だけを検査するため、書込み鍵 active の残存不整合があっても起動し得る。起動時検査と setup の整合検査は同じ関数ではない。起動時検査の拡張は範囲外とする（§4）。

**不変条件:** 鍵・設定を変える間は marker と停止確認と instance lock が揃う。記録がある間は通常の setup 操作を成功させない（直接 keys 操作は入口表の例外）。記録の削除は、目標との整合・marker 無し・必要な afx の復帰をすべて確認した後だけ。

判定は `check_write_key_consistency` に集約したまま、回復の遷移・完了条件は単一の回復関数へ集める。hook や CLI ごとに独自の整合判定を作らない。

段を記録しないのは、実物との整合を状態機械の中心の1関数で守り、進捗記録と実物の二重管理を避けるため。

新規回復は従来どおり idle の不整合時だけ開始する。setup の排他の下で、通常 stage・他機能の未完了・marker 所有者、構成と鍵の事前条件を検査し、停止前の ActiveState を採る。稼働中なら既存の停止に対する利用者確認を経て、**marker 設置や停止より先に記録を durable 保存する**。保存失敗時は停止しない。記録の書込みはこの最初の1回と目標差替え時だけ。

記録がある場合は idle の Consistent → not_enabled より先に、毎回次の同じ手順を実行する。途中経過は記録しない。

(a) 記録を検証する。通常 stage も検証し、enabled/unfinished や idle_origin との矛盾、他機能の未完了は変更せず拒否する。

(b) 実物を観測する。鍵と write の整合（**記録の target との一致も含む**）、marker の有無・持ち主、afx の ActiveState を読む。他機能・壊れた marker は回収しない。--write の安全条件や読めない config の検査も省略しない。

(c) 目標と不整合、または marker があるなら整合作業へ進む。直前に afx を再観測し、停止が必要で元が稼働（afx_before_stop が `_AFX_RUNNING`）なら既存の confirm を経る。拒否は setup_aborted、非端末は setup_input_required（interactive.py:292–295）として変更せず記録を残す。元の操作と同じ停止権限であり、再開だから確認を省かない。元が停止で、現在は手動起動され稼働しているなら、**止めずに非成功** `write_recovery_afx_running` を返し、記録を残す。固定 reason の detail に「`systemctl --user stop <unit>` で止め、`systemctl --user is-active <unit>` の出力が `inactive` であることを確かめてから、同じ `afx ops web enable --write` / `--no-write` を再実行する」と案内する。`<unit>` は `setup.paths.afx_unit` から作り（units.py:43、停止先も S:523）、既定名を固定しない。is-active の終了コードだけでは停止確認としない。marker のみの解消でもこの停止規則を適用する。

停止可能なら `_quiesce_afx(reset_stage=False)` で marker・停止確認・自分の instance lock を揃え、align_keys と設定反映で目標へ整合させる。最初の afx_before_stop を必ず渡して通常 stage の保存を起こさず、鍵 preparing は既存 align_keys で完了させる。disabled の --no-write は読取鍵を検査し、never では新規要求しない。config 不在・target=false は既存 idle の許容に従う。整合済みでも marker があればこの手順を通る。marker 無しで目標と整合済みなら変更せず次へ進む。

(d) 自分が instance lock を持つ経路は、その下で鍵・write と記録の target を再照合する。(c) を省いた停止中の経路も lock を取得して再照合する。取得できなければ記録を残して非成功とし、確認のためだけに停止しない。既に afx が稼働中の再開経路は lock を取らずに観測する。他者の lock 保持を根拠に排他があるとは扱わない。

再照合で不整合を見たら、実物を再観測し (c) へ **1回だけ** 戻る（実行内の再試行上限1、loop しない）。この戻りでも元稼働なら confirm / setup_input_required、元停止・現在稼働なら write_recovery_afx_running の規則を適用する。自分の instance lock 下で (c) の整合操作を行った後になお不整合なら、再試行せず `write_recovery_inconsistent` で非成功とし記録を残す。戻った後の再照合で再び不整合でも同じ reason で終える。整合確認後、自分の lock があれば閉じ、`_unmark` で marker を durable 削除する。既に無ければ何もしない。直接 keys 操作との競合の限界は §4 に定める。

(e) afx_before_stop が active/activating/reloading なら稼働を確認し、止まっていれば start して ActiveState=active を確かめる。記録が残る間は回復が稼働に戻すため、start 後の中断中に利用者が手動停止しても再実行で起こす。元が停止状態ならここでは何もしない。手動で起動されていても停止しない。回復は完了時に元の停止状態へ戻すための停止権限を新たに持たない（(c) でも元停止・現在稼働なら止めずに非成功）。idle の web の enable/READY 待ちはしない。

これは既存の「元が稼働なら起動を戻す、元が停止なら回復から起こさない」という規則に沿う。通常 enable は S:649–657、稼働状態の分類は S:45・422–423、idle 回復は W:150–171。enabled 切替も元も現在も停止なら何も起こさず、現在稼働なら継続する（W:300–317）。enabled 切替は web の READY 確認もするが、idle 回復には導入しない。

(f) (d) の目標整合確認（稼働中は排他無しの観測）・marker 無し・(e) の必要な afx 復帰という完了条件をすべて満たした後だけ、記録を durable 削除し、`StageResult(web, recover, disabled)` を返す。この完了は鍵・write の継続的整合を保証しない。削除前の失敗では記録を保持して非成功とする。unlink 後に親 directory の fsync が失敗した場合は「削除の永続性が不明」として非成功を返し、記録残存は保証しない。再実行・再起動後に記録が残っていれば同じ手順で収束し、消えていれば既に整合済みなので通常の not_enabled で安全に終わる（後続の別操作がない場合）。削除直後の中断も同じ扱い。

目標変更（--write 中断 → --no-write、逆も同様）は**新しい明示目標を採用**する。(a) の検証と新目標の事前検査後、target だけを durable 更新して (b) へ進む。最初の afx_before_stop と idle_origin は保持する。旧目標を一度起動してから変更しない。更新前で落ちれば旧記録、更新後で落ちれば新記録から、次の明示目標に向け同じ手順で再開する。

| 中断点 | 残る状態 | 改訂後の再実行（同じ目標） |
|---|---|---|
| 記録保存の直後 | 記録あり、実物はまだ不整合、afx は元の状態 | (a)(b) から (c) の停止・lock・整合へ進み、(d)〜(f) で完了 |
| 停止後・整合前（§0） | 記録・marker あり、afx 停止、不整合 | (c) を冪等に再実行。元の稼働状態は記録から戻す |
| 設定整合後・marker 削除前（§0） | 記録・marker あり、目標と整合 | marker があるので (c) を通り、(d) で外し (e)(f) へ |
| marker 削除後・起動前（§0） | 記録あり、marker 無し、目標と整合、afx 停止 | (c) の変更を省き、(d) で lock 下の再照合、(e) で必要な起動、(f) で削除 |
| start の後・記録削除の前 | 記録あり、marker 無し、目標と整合、afx active | lock を取らず観測し、整合・marker 無しと必要な active を確認後に (f) で削除。手動停止されていれば再起動 |
| 記録削除の後 | 記録・marker 無し、目標と整合、必要な復帰済み | 回復は完了。idle の通常判定で not_enabled（副作用無し） |

記録は 0600、自 uid の通常 file、4096 byte を上限として最大4097 byte を読み超過を拒否し、O_NOFOLLOW、JSON の版・必須型・列挙を厳密検証する。afx_before_stop の許容集合は systemd.py:13–17 の `_UP_STATES | _STOPPED_STATES | _BETWEEN_STATES`（active / reloading / refreshing / inactive / failed / unknown / activating / deactivating / maintenance）とする。復帰対象は引き続き S:45 の `_AFX_RUNNING` のみ。余分な JSON 項目を拒否し、`json.loads` の object_pairs_hook で重複キーも拒否する。version は bool を除く int の1、target は厳密な bool とする。無い場合だけ「記録無し」。壊れた JSON、未知版、必須欠落、読取不能、symlink は `write_recovery_invalid` と path・修復案内で fail-closed。推測で afx を起こしたり記録/marker を削除しない。管理者が停止下で原本を確認・修復して再実行する。保存は既存 atomic_write、削除は remove_durably と同じ fsync 規則（units.py:87–118）。保存の rename 前の失敗と rename 後の親 directory fsync 失敗は区別する。後者は「保存の永続性が不明」として非成功を返し、marker 設置・停止・鍵や設定変更へ進まない。新規なら次回は記録あり/なし、目標差替えなら旧/新 target のどちらでも実物を再読込みして開始または再開できる。停止前状態と idle_origin は差替えで変えない。

`_guarded` / `_failure` は回復中の例外を診断として返し、回復記録にも web.stage.json にも失敗を追記しない。既存 unrecorded の扱いを保ち、保存・削除の永続性不明は上記の例外として扱う。process 終了では flock だけが解放され、記録は残る。

### 入口 × 状態

record の読取・保存・削除は既存 `stages.py` の read_stage / `_write_stage` と marker I/O 付近（S:79–173）に `read_write_recovery` / `write_write_recovery` / `remove_write_recovery` としてまとめる。InvalidState と既存 durable I/O を共有し、web_write.py はこれを利用する。

pending 検査を共通化し、CLI の入力収集・probe・一時 file 掃除・secret/config 保存より前と、OpsSetup の状態遷移の中心で呼ぶ。既存 check_write_switch_idle / `_unfinished` へ回復記録の検査を接続し、reconfigure の独自 stage 検査にも共有する。通常入口は setup lease → 共通 pending 検査 → 入力・probe・保存 → OpsSetup の `_unfinished`（再検査）→ 既存遷移の順とする。

明示 write 専用分岐（interactive.py:347–349）は通常 pending 拒否の前に `_switch_write` へ渡す。`web_write._switch`（W:195–200）は通常 stage と record を読み、record があれば idle の通常判定より先に回復へ渡す（stage 矛盾は (a) で拒否）。record 無しの idle も新規回復へ渡す。回復自身の `_unfinished` 呼出しには専用の `recovering_idle=True` を追加し、feature=web / operation=enable / switching_write=True の組合せだけ回復 pending 拒否を通す。他機能の未完了・通常 stage の競合は引き続き検査し、record の妥当性・idle_origin は (a) で必ず照合する。この引数は通常 enable・enabled 切替では指定しない。これにより回復自身が pending で止まることを防ぐ。

| 入口 | 記録無し | 正常な回復記録あり（整合済みも含む） | 壊れた/読めない記録 |
|---|---|---|---|
| web enable --write / --no-write | 既存の enabled 切替 / idle 不整合回復。idle 整合済みは not_enabled | 新目標で回復完了まで進む | write_recovery_invalid、変更無し |
| web 通常 enable（対話・非対話・再開） | 既存整合検査 | write_recovery_pending、変更無し・明示フラグへ案内 | 同上 |
| bot/notify enable・disable、web disable、reconfigure | 既存規則 | 同上。marker・記録を奪わない | 同上 |
| init（実 wizard 全体） | 既存規則 | wizard の lease 取得直後、instance lock 取得・入力・保存・probe より前に init 全体を拒否 | 同上 |
| keys init / enroll / rotate / revoke（直接実行） | 既存の instance lock 下で各操作 | setup lock / pending 検査には参加させず既存操作を許す。記録・marker・通常 stage は変えない。回復は次の再実行で実物を再照合 | 記録を修復・削除せず既存 keys 操作のみ |
| status | 読取のみ | pending と目標・観測した実物の状態を表示、完了扱いしない | 診断のみ、変更無し |

wizard の検査位置は `_stage0` の acquire_setup_lease 成功直後（setup/wizard.py:675–684 の間）とする。web 準備だけを飛ばす形にはしない。回復途中に init が afx の停止・起動や鍵を触ると、記録した停止前状態の意味が変わるため init 全体を止める。marker の有無には依存しない。wizard の init は handoff_and_enable（setup/wizard.py:2449）経由の enable を試す。keys init 自体は write 回復ではない（ops/keys.py:575–620 は停止中の instance lock と initialize のみ）。直接の keys 操作は既存修復手段として残し、回復入口で実物を再照合する。interactive.py:369 の keys 初期化フォールバックより先に pending を拒否する。通常の「不整合だけなら disable は警告して停止できる」（S:559–573）は維持するが、回復記録ありでは既存 write_switch_pending と同様に操作を保護する。

## §2 test の表

対象は `tests/ops_setup/test_web_write_switch.py` と実 wizard の関連 test。副作用の境界ごとに代表例を置き、状態依存の分岐に必要な組合せだけ追加する。全 STATES や全中断点の直積は作らない。

| 追加行 | 作る状態・hook | 期待 |
|---|---|---|
| 副作用境界の代表 | 新規記録保存前後、停止後、lock 取得後、鍵変更後、設定変更後、marker 削除前後、start 前後、記録削除前後 | 元稼働・idle 不整合の代表で再生成した OpsSetup から再開。鍵・write の目標整合、marker 解消、必要な稼働復帰。削除後だけ通常 not_enabled |
| 状態による分岐 | never/disabled、元稼働/停止、true/false の各分岐を覆う最小組合せ。config 不在 false、preparing の両方向、読取鍵の required 差 | stage 不変、never で読取鍵を新規要求しない。既存 STATES はこの分岐に必要な行だけ選ぶ |
| start 後の手動停止 | 元稼働、after_recovery_start 中断 → 手動 stop → 再実行 | 記録を保持する間は start し active を確認して完了 |
| 元停止時の手動起動 | marker 削除後に中断 → 手動 start → 同目標で再実行 | 回復から start/stop しない。active のまま完了可能 |
| (b) と (d) の間の鍵変更 | (b) は整合・marker 無し、(d) 前に直接 keys で変更 | (c) へ1回戻り整合。再度の不整合は非成功・記録保持、loop しない |
| 元停止・手動起動中に不整合 | 元停止の記録、現在 active、(b) または (d) で不整合 | stop なし、write_recovery_afx_running の detail に setup.paths.afx_unit 由来の stop / is-active inactive 確認と同じ明示 write コマンドの再実行案内、記録保持 |
| 元停止・marker と自動再起動ありの案内から収束 | 元停止の記録・web marker あり、手動起動後に自動再起動設定がある unit（既定以外の setup.paths.afx_unit）で非成功 | detail の案内どおり user unit を stop し、is-active の出力 inactive を確認して同じ --write / --no-write を再実行。両目標で整合・marker 解消・記録削除まで収束し、回復から start しない |
| 元稼働で不整合 | 元稼働の記録、現在 active、再照合で不整合 | confirm 後 stop・lock・整合・復帰。拒否/非端末では停止せず setup_aborted / setup_input_required。lock 下の整合後も不整合なら write_recovery_inconsistent・記録保持 |
| 両方向の目標差替え | true→false / false→true、保存前後の中断 | 新しい明示目標へ収束。最初の afx_before_stop / idle_origin を保持。旧目標で一旦起動しない |
| 実 wizard 入口 | run_wizard を通す。正常 pending + marker あり/なし、記録無しの既存正常経路 | pending は lease 取得直後に init 全体を拒否。質問・保存・probe・keys・systemd 操作なし。記録無しは既存経路へ進む |
| 直接 keys と競合 | init / enroll / rotate / revoke を回復の lock 保持中、解放後、再実行前に試す | 保持中は排他。解放後は既存規則で動作し記録不変、次回は変更済み実物を照合。稼働中再開の観測中に daemon 停止→直接 keys 変更を挟む。検出時は上記1回の再試行、未検出の残存不整合は次の enable（フラグなし・--write・--no-write）/ reconfigure の整合検査が止めることを確認 |
| write=false・書込み鍵 active の残存 | target=false の整合観測後、daemon 停止と直接 keys 操作で書込み鍵を active にし、回復が変更を見逃して記録削除。読取鍵は正常 | 次の enable（フラグなし・--write・--no-write）/ reconfigure の整合検査が止める。明示 write は既存の回復経路へ進む。web 起動の拒否は期待せず、読取 principal の検査を通って起動し得る。write=false の web は書込み鍵 file を読まない |
| 他 setup 入口と status | 入口表の通常操作を正常 pending の代表状態で実行 | setup は無変更で pending。status は読取のみ。enabled 切替・通常 enable 再開は既存 test と必要な代表回帰だけ |
| 破損・競合 | JSON/型/版/権限/読取エラー/symlink、余分な項目・重複キー・未知ActiveState・4096 byte超過、foreign/corrupt marker、通常 stage 矛盾 | fail-closed、記録・stage・marker・鍵・設定を保全 |
| 保存の永続性不明 | 新規/差替えで rename 後の directory fsync 失敗 | 非成功、停止や整合操作なし。新規は記録あり/なし、差替えは旧/新記録の両方から安全に再開 |
| 削除の永続性不明 | 完了条件を満たした後の unlink 後 directory fsync 失敗 | 非成功。記録ありなら同手順で収束、無しなら整合済み not_enabled。残存を必須にしない |
| その他 I/O・systemd 失敗 | rename/unlink 前、marker 削除、stop/show/lock/start の代表失敗 | 非成功。記録削除前は再開可能、通常 stage 不変 |

再実行ごとに新しい OpsSetup を生成する。hook 例外と実 I/O 境界の fake 失敗を区別し、鍵・設定の実物、stage、marker、記録、start/stop の呼出しを照合する。afx の最終停止は保証せず、元停止なら回復が起こさないことを検証する。記録の書込みは新規1回と目標差替え時だけ。既存の関連 test は維持する。

## §3 受入

- §1 の中断点表（§0 の3中断点を含む）から、整合済みであっても marker 解消と必要な afx 復帰まで完了する。
- 目標変更でも停止前状態を失わず、web を enable せず目標へ収束する。元が停止なら回復は afx を起こさない（最終停止状態の保証ではない。関連受入8b）。元稼働なら記録が残る間は手動停止後も再実行で起こす。
- setup の全入口で正常/異常な記録を検出し、通常 setup 操作が回復を飛び越えない。直接 keys は入口表の既存操作を許す。通常 stage と既存整合判定の意味を保つ。
- §2 の表と既存 web_write_switch / stages / interactive / reconfigure の関連 test が通る。これは実装時の受入であり、本 C0 では test を変更・実行していない。

## §4 残余

旧版で既に記録無しのまま中断した状態から、元の afx 稼働状態は復元できない。自動推測はせず、marker と停止前状態を運用者が確認する。古い実行ファイルは新記録を知らないため、pending 中の downgrade は対象外。直接 keys の setup lock 参加は範囲外。回復記録の保証は止める前の afx の状態の復元と web marker の解消であり、鍵・write の整合自体は既存 fail-closed 検査が担う（enable: S:548–558、576–583 / K:72–117、web 起動: afx-ops/afx_ops/isolation/startup.py:881–899、1030 の state・digest 検査と principal 固定）。自分の instance lock 下での照合はその期間だけ排他がある。稼働中再開は lock を取らずに観測し、daemon の継続的な lock 保持を仮定しない。整合・marker 無し・元稼働なら必要な稼働を確認して記録を消せる。daemon の停止と直接 keys 変更が観測に重なり、不整合を見逃したまま完了する可能性は残る。被害の上限は、afx 復帰・marker 解消後にも鍵・write の不整合が残り、次の `afx ops web enable`（フラグなし・`--write`・`--no-write`）/ reconfigure の整合検査が止め、修復が必要になること（明示 write は既存回復経路へ進む）。例えば `write=false` で書込み鍵が active の場合、web 起動は読取 principal だけを検査するので起動し得る（afx-ops/afx_ops/isolation/startup.py:190–204、881–899、1030）。これは回復が無いときに同じ直接 `afx keys` 操作を行った場合と同じ状態で、回復が新しく作る危険ではない。書込み鍵 file は secret dir（API 鍵専用の `~/.config/agentic-fx/api/<instance_id>/`、0700。feature の secret dir とは別）内に置かれる（src/agentic_fx/ops/keys.py:3、51–62、115）。web は write が無効ならこの書込み鍵 file を読まず、読取鍵だけを使う（startup.py:199–204、881–899、1030）。起動時検査を全不整合へ広げる変更は範囲外とする。回復成功を web の書込み可能性や継続整合の保証として扱わない（idle の web は enable しない）。検出できた不整合は §1(d) の1回の再試行で扱う。手動 systemd 操作は §1(e) の非対称な扱いとし、元停止の手動起動を完了時に止めず、元稼働の手動停止は記録が残る間再実行で起こす。記録は再実行で進むもので、自動再起動サービスは追加しない。

## §5 裁定が要る点

C0 の技術判断は別 file、明示目標の差替え、共通 pending 検査、fail-closed を推奨として決める。新たな利用者裁定を必須とする点はない。回復完了のため元の afx を戻すこと、元が停止なら起こさないことは既存の意図に従う。pending 中に通常 disable を特別許可する方針へ変えるなら別途裁定が要るが、本案では既存の切替中拒否と揃える。

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-15 | C0 | idle 回復記録、再開順序、入口と中断 test、受入を提案 | 記録無し回復が marker と停止前状態を失うため | —（未 commit） |
| 2026-11-15 | C0.1 | 回復の段記録を廃止し実物から毎回同じ手順で再開。中断 test を集約し marker 流用案を比較 | 整合判定を中心の1関数に保ち、実物と進捗記録の二重管理を避けるため | —（未 commit） |
| 2026-11-15 | C0.2 | レビュー1周目の Important 4件・Minor 1件を裁定に沿って反映。手動 start/stop の非対称な保証、排他下の整合確認と直接 keys の保証境界、wizard lease 直後の init 全体拒否、保存/削除の永続性不明、分岐中心の test 表を明記 | 回復の停止権限を増やさず、実 source と保証範囲を一致させるため | —（未 commit） |
| 2026-11-15 | C0.3 | レビュー2周目の裁定を反映。稼働中の排他保証を撤回し既存 fail-closed との責任分界を明記。再照合不整合の1回再試行・停止権限・固定 reason、record 配置と入口順序・厳密な読取規則、試験3行を追加 | 観測と排他を区別し、手動起動を止めない規則と再開分岐を一意にするため | —（未 commit） |
| 2026-11-15 | C0.4 | レビュー3周目 Important 2件の裁定を反映。残存不整合は次の enable / reconfigure の整合検査が止め、write=false では web が起動し得る保証境界と source を明記。元停止・手動稼働の detail に unit 停止・inactive 確認・同じ明示目標の再実行を指定し、残存不整合と marker・自動再起動ありの収束試験を追加 | 起動時検査は拡張せず実装の保証に合わせ、案内に従う再実行を収束させるため | —（未 commit） |

# web-write-recover-idle-interruption — C0.5

対象: idle（未 enable / disabled）の write 回復。実装・commit は含めない。
根拠: `docs/tickets/open/web-write-recover-idle-interruption.md`、関連設計 `2026-11-08-web-plugin-improve-hub-design.md` の B1・受入 8b、4 周目レビュー `tmp/review-web/recover-d4-opus.md`。
以下 `W` = `src/agentic_fx/ops_setup/web_write.py`、`S` = 同 `stages.py`、`K` = 同 `write_key.py`、`IA` = 同 `interactive.py`、`T` = `tests/ops_setup/test_web_write_switch.py`。行番号は調査時点。

## §0 現状

`_switch` は idle を `_recover_idle` へ渡すが、整合済みなら目標に関係なく not_enabled（W:138–140、195–200）。停止前状態はメモリだけで stage に残らない。

| 中断点（既存 hook） | 現行の再実行で失うもの |
|---|---|
| after_stop / after_lock / before_keys | 今の停止状態を停止前状態とするため、整合・marker 解消後も afx を起こさない |
| after_config / before_unmark | 整合済み not_enabled で marker 解消・afx 復帰に届かない |
| after_unmark | 整合済み not_enabled で afx 復帰に届かない |

config 不在の --no-write では after_keys が既に整合後（W:142–145、163）。`_quiesce_afx` は marker → stop → 停止確認 → instance lock（S:506–542）、`_unmark` は durable 削除（W:287–297）。`_guarded` は排他ではなく OSError の記録境界で、idle は unrecorded のため通常 stage に失敗を残さない。

## §1 動作

### 記録・分類・事前検査

別 file `root/data/ops/web.write-recovery.json` を OpsSetup が所有し、内容は **`{version, afx_before_stop}` のみ**。version は 1、afx_before_stop は最初に観測した ActiveState で、再開でも上書きしない。target は毎回の明示 `--write` / `--no-write` から取り、新しいフラグが勝つ。目標変更でも記録は更新しない。設定は再読込みし、port 等を保って write だけ合わせる。
通常の web.stage.json は未作成/disabled のまま保持する。stage の新 mode は既存呼出側への影響が広く、disabled へのフィールド追加は未 enable に架空の stage を作る。marker は start 前に消す必要があり復帰情報を持てないため、寿命を分けた別 file とする。

停止は `{inactive, failed}`、それ以外は停止でない。起動を戻す対象は `_AFX_RUNNING = {active, activating, reloading}`（S:45）のみ。整合の式は **`check_write_key_consistency(root, mode="idle") == Consistent(target)`** とする。遷移・完了条件は単一の回復関数へ集め、進捗は保存しない。

新規回復は setup lease 下で、次の順に検査する（記録無しの idle の不整合時だけ開始）。

1. `_unfinished`（通常 stage・他機能の未完了）
2. `_check_marker_owner`（他機能・壊れた marker は回収しない）
3. `check_write_key_consistency`（Consistent なら従来どおり not_enabled）
4. `web_live_config`（config 不在かつ target=false のみ既存どおり許容）
5. target=true なら `_check_target` と `principal_key_mismatch` の検査
6. `_check_key_dir`
7. `check_read_key(required=通常 stage が disabled)`（never では新規要求しない）
8. afx の ActiveState 観測
9. 今が `_AFX_RUNNING` のときだけ confirm
10. 記録を durable 保存（marker 設置・停止より先）

1〜9 で止まったら記録を作らない。confirm は **今の ActiveState が `_AFX_RUNNING` のときだけ、1 回の実行で 1 回**。保存前の確認済みを `confirmed` として (c) に渡し、再度聞かない。拒否は setup_aborted、非端末は setup_input_required（IA:292–295）。

### 再実行 (a)〜(f)

記録があれば Consistent → not_enabled より先に次を実行する。新規保存後も同じ関数へ入る。

(a) 記録と通常 stage を検証する。enabled/unfinished、他機能の未完了は変更せず拒否する。

(b) 毎回の明示 target で上記の整合式、marker の有無・持ち主、afx を観測する。live config、target=true の安全条件、鍵 dir・読取鍵の検査も省かない。

(c) 目標と不整合、または marker があれば整合作業へ進む。直前に afx を再観測し、元が停止で今が停止でないなら **止めずに** `write_recovery_afx_running` で非成功、記録を保持する。detail は `setup.paths.afx_unit` 由来の `<unit>` を用い、「`systemctl --user stop <unit>` で止め、`systemctl --user is-active <unit>` の出力が `inactive` であることを確認し、同じ `afx ops web enable --write` / `--no-write` を再実行」とする。終了コードだけで停止確認にしない。marker のみの解消にも適用する。それ以外は今が `_AFX_RUNNING` かつ未確認なら confirm し、`_quiesce_afx(reset_stage=False)` で marker・停止確認・自分の instance lock を揃えて `align_keys` と設定反映を行う。最初の afx_before_stop と unrecorded を渡して通常 stage を書かず、preparing は既存 align_keys で完了させる。marker 無し・目標整合済みなら (c) を省く。

(d) 同じ整合式で再照合する。(c) の lock があればその下で、(c) を省いた経路は lock を取らず観測する。不整合なら記録を残して非成功とし、実行内では戻らない。Inconsistent は既存の reason/guidance、Consistent(別目標) は既存 write_change_refused で返す。再実行は (b) から必要な (c) に入る。元停止・今が停止でないときの (c) の停止禁止は維持する。整合確認後、自分の lock を閉じ、`_unmark` で marker を durable 削除する（無ければ no-op）。

(e) afx_before_stop が `_AFX_RUNNING` なら現在を観測し、`_AFX_RUNNING` でなければ start する。start 後の show が `_AFX_RUNNING` であることを確認する。記録が残る間は手動停止後も再実行で起こす。それ以外は起こさず、手動で起動されていても止めない。idle の web の enable / READY 待ちはしない。

(f) (d) の目標整合・marker 無し・(e) の必要な復帰をすべて確認後だけ記録を durable 削除し、`StageResult(web, recover, disabled)` を返す。削除後の再実行は通常の整合済み not_enabled。

新 hook 名は `before_recovery_record` / `after_recovery_record`（保存）、`before_recovery_start` / `after_recovery_start`（実際の start と show 確認を囲む）、`before_recovery_remove` / `after_recovery_remove`（削除）に固定する。

### 読取り・失敗・入口

記録は 0600、自 uid の通常 file、O_NOFOLLOW、4096 byte 上限（最大4097 byte 読んで超過拒否）。余分な項目・重複キー（object_pairs_hook）・欠落を拒否し、version は bool を除く int の1、afx_before_stop は systemd.py:13–17 の列挙（active / reloading / refreshing / inactive / failed / unknown / activating / deactivating / maintenance）に限定する。無い場合だけ記録無し。破損・未知版・読取不能・symlink 等は `write_recovery_invalid` と path・停止下での原本確認/修復案内で fail-closed とし、推測で起動・削除しない。
記録 I/O は S:79–173 付近の `read_write_recovery` / `write_write_recovery` / `remove_write_recovery` にまとめ、保存は既存 atomic_write、削除は remove_durably を使う。保存・削除の OSError は前進せず、marker_write_failed と同じ形の `SystemdError("material_write_failed", _describe(error))` に包んで非成功とし、再実行は記録の有無どちらでも安全に収束する。読取失敗は上記 invalid。失敗や進捗は記録・通常 stage に追記せず、process 終了では flock が解放される。
新 reason は `write_recovery_invalid` と `write_recovery_afx_running` のみで IA の `REASON_CODES` に登録し、通常入口でも detail を出せるよう `_GUIDED_REASONS` に加える。pending は既存 **write_switch_pending + WRITE_SWITCH_GUIDANCE** を流用する。

共通の読取専用 pending 検査を `check_write_switch_idle` / `_unfinished` に接続する。通常入口は setup lease → pending 検査 → 入力・probe・一時 file 掃除・secret/config 保存 → `_unfinished` 再検査 → 既存遷移。明示 write 分岐（IA:347–349）は通常拒否より先に `_switch_write` へ渡し、W の `_switch` は記録を通常 idle 判定より先に回復へ渡す。回復自身は既存の `switching_write=True` で通過でき、新しい専用引数は不要。記録の書き手は setup lock 下にあり、記録ありで enabled 切替へ進まない。他機能の未完了・stage 競合は検査し続ける。

| 入口 | 記録無し | 正常な記録あり | 壊れた/読めない記録 |
|---|---|---|---|
| web enable --write / --no-write | 既存 enabled 切替 / idle 不整合回復 | 毎回の明示目標で (a)〜(f) | write_recovery_invalid、無変更 |
| web 通常 enable、bot/notify enable・disable、web disable | 既存規則 | write_switch_pending と既存案内、無変更 | 同上 |
| init（実 wizard 全体） | 既存規則 | lease 取得直後に全体拒否 | 同上 |
| reconfigure | idle は整合検査より前に not_enabled | 同じく not_enabled、専用記録検査は足さない | 同左 |

wizard は `src/agentic_fx/setup/wizard.py` の `_stage0` で lease 取得後、instance lock・入力・保存・probe より前に検査する。正常 pending の文言は `write_switch_pending: ` + WRITE_SWITCH_GUIDANCE、RC_STOPPED=1（既存 write_switch_pending の RC=1 に合わせる）。異常記録も reason と修復案内を表示し RC=1。web 準備だけを飛ばす形にはしない。通常 enable は IA:369 の keys 初期化フォールバックより前に拒否する。不整合だけの disable は従来どおり警告して停止できるが、pending は既存切替中と同様に拒否する。status の表示追加は行わない。
直接 `afx keys init/enroll/rotate/revoke` は既存規則のまま許し、記録・marker は触らず、setup lock への参加と回復観測との競合保証は範囲外とする。

## §2 test の表

対象は T と実 wizard の関連 test。既存 ENTRIES を狭めず、下記を追加する（チケットの要求をすべて含む）。

| 追加・更新 | 期待 |
|---|---|
| ENTRIES: enable 再開 staged / enrolled / started、切替 staged / stopped / keyed / configured の両目標、init、no-write-never-enabled | 既存 STATES の整合/拒否/収束規則を適用。init は実 wizard を通す |
| T:1205 の idle converge セル | 成功後に記録・marker 無し、目標整合、元稼働なら復帰、元停止なら回復から起動しない。refuse は記録も含め world 不変 |
| idle の全中断点（T:244 の型を流用する 1 本） | probe で既存 hook と §1 の新 hook を集め、各 index で Crash、新しい OpsSetup で再実行。never/disabled × 元稼働/停止 × target の分岐を覆う最小組合せで §0 の3点から完了まで確認。削除後だけ not_enabled |
| config 不在 false、preparing 両方向、読取鍵 required 差、新規事前検査の拒否 | 通常 stage 不変、never で読取鍵を新規要求しない。保存前拒否は記録無し・world 不変 |
| confirm と状態分類 | 新規稼働時は1回、停止中の非端末再開は0回。拒否/非端末稼働時は setup_aborted / setup_input_required。停止集合外と start 後 show の判定も確認 |
| 手動操作・marker と自動再起動 | 元稼働は after_recovery_start 後の手動停止から再起動。元停止・現在停止でない状態で整合操作が必要なら stop 無しの案内で非成功。既定以外の unit の案内に従い stop / inactive 確認後に両目標で収束。marker 無し・整合済みの手動稼働は start/stop 無しで完了可能 |
| (d) の再照合不整合 | 記録を保持して非成功、実行内の再試行無し。次回 (b)→(c) で収束し、停止禁止も守る |
| 再実行の目標変更（両方向） | 新しい明示目標へ収束し、最初の afx_before_stop は保持、記録の再保存無し |
| 通常入口・実 wizard | 正常 pending（marker あり/なし）と異常記録を副作用前に拒否。固定 reason・案内・RC を確認。idle reconfigure は not_enabled |
| 破損・競合・I/O / systemd 失敗 | 厳密読取の全規則、foreign/corrupt marker、通常 stage 矛盾、保存/削除の rename/unlink・fsync、stop/show/lock/start/marker 削除失敗を確認。固定 reason で非成功、記録あり/なしから安全に再実行、通常 stage 不変 |
| 実行と再実行の間の直接 keys 変更 | 記録不変、次の再実行で変更済みの実物を照合して整合 |

hook 例外と実 I/O の fake 失敗を区別し、鍵・設定・stage・marker・記録と start/stop 呼出しを照合する。元停止の最終停止状態は保証せず、回復が起こさないことを検証する。重複する残存不整合試験は追加しない。

## §3 受入・範囲

- 整合済みでも記録から marker 解消と必要な afx 復帰まで完了し、web を enable しない。記録は新規時の1回だけ保存し、完了条件を満たした後だけ削除する。
- 元停止の手動稼働を止めず、元稼働なら記録が残る間は手動停止後も再実行で起こす。通常 stage と既存整合判定の意味を保つ。
- §2 と既存 web_write_switch / stages / interactive / reconfigure / wizard の関連 test が通る。実装と同じ commit で `docs/operations/ops-api-2026-10-09.md` §10.8 の「整合しているときは not_enabled」を記録無しの場合へ限定し、pending に idle 回復と wizard の文言・RC を追記、W の `_recover_idle` docstring も記録からの再開に合わせる。本改訂ではコード・test・運用文書は変更・実行しない。

## §4 残余

旧版で記録無しのまま中断した停止前状態は推測できず運用者が確認する。pending 中の downgrade と自動回復サービスは対象外。自分の instance lock 下だけ排他があり、(c) を省く観測から鍵・write の継続整合は保証しない。idle の reconfigure は整合検査前に not_enabled（reconfigure.py:55–58、161）。idle の web は unit が無いので起動せず、次の enable の `_check_web_write` が不整合を拒否する（S:548–558、737–754）。起動時検査は広げない。

## §5 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-15 | C0 | idle 回復記録、再開順序、入口と中断 test、受入を提案 | 記録無し回復が marker と停止前状態を失うため | —（未 commit） |
| 2026-11-15 | C0.1 | 回復の段記録を廃止し実物から毎回同じ手順で再開。中断 test を集約し marker 流用案を比較 | 整合判定を中心の1関数に保ち、実物と進捗記録の二重管理を避けるため | —（未 commit） |
| 2026-11-15 | C0.2 | レビュー1周目の Important 4件・Minor 1件を裁定に沿って反映。手動 start/stop の非対称な保証、排他下の整合確認と直接 keys の保証境界、wizard lease 直後の init 全体拒否、保存/削除の永続性不明、分岐中心の test 表を明記 | 回復の停止権限を増やさず、実 source と保証範囲を一致させるため | —（未 commit） |
| 2026-11-15 | C0.3 | レビュー2周目の裁定を反映。稼働中の排他保証を撤回し既存 fail-closed との責任分界を明記。再照合不整合の1回再試行・停止権限・固定 reason、record 配置と入口順序・厳密な読取規則、試験3行を追加 | 観測と排他を区別し、手動起動を止めない規則と再開分岐を一意にするため | —（未 commit） |
| 2026-11-15 | C0.4 | レビュー3周目 Important 2件の裁定を反映。残存不整合は次の enable / reconfigure の整合検査が止め、write=false では web が起動し得る保証境界と source を明記。元停止・手動稼働の detail に unit 停止・inactive 確認・同じ明示目標の再実行を指定し、残存不整合と marker・自動再起動ありの収束試験を追加 | 起動時検査は拡張せず実装の保証に合わせ、案内に従う再実行を収束させるため | —（未 commit） |
| 2026-11-15 | C0.5 | 4 周目 I1〜I3・M1〜M7 と簡素化1〜11を採用。記録を2項目へ縮小、confirm・事前検査・reason・hook・入口・受入を固定 | 再開を一意にし、保証外の競合対策と重複記述を削るため | —（未 commit） |

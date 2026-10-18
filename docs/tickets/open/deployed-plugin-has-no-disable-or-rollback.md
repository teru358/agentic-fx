---
id: deployed-plugin-has-no-disable-or-rollback
title: 配備済み plugin を止める・前の版に戻す操作が無い
status: 実装中
priority: 高
opened: 2026-10-04
closed: null
related: [retire-symlink-deployed-plugin]
---
# [deployed-plugin-has-no-disable-or-rollback] 配備済み plugin を止める・前の版に戻す操作が無い

**状態**: 実装中 / **優先**: 高

## 現象

承認して配備した plugin を取り消す手段が無い。承認済みの行への reject は CAS (status='pending' 限定、approvals.py:77-86) で拒否され、afx plugin retire は plain directory 専用で配備済みの symlink を拒否する (switch.py:1856-1858)。鍵が漏れたとき・誤って承認したときの回復手段が無い (操作 API 設計レビュー r1 F4、2026-10-03)。

## 原因

切替ジャーナルに disable / rollback の遷移が無い。

## 処置案・裁定

切替ジャーナルの規律に乗る disable (配備を外す) と rollback (前の版へ戻す) を設計する。入ったら操作 API の approver 権限の endpoint と、鍵が漏れたときの手順に足す。着手はウィザード完了後 (2026-10-04 ユーザー裁定)。それまで運用文書に『回復手段は未整備』と明記する。

## 修正内容

## 経緯

- 2026-10-04: 起票。
- 2026-10-16: 状態: 設計待ち — 2026-10-16 ユーザー承認で設計着手 (C0 を sonnet、tmp/design-plugin-rollback/C0.md)。retire-symlink-deployed-plugin を統合
- 2026-10-16: 設計収束 (C0 v0.8、sol 8 周: r1 C2/H4 → r2 C1/H4 → r3 C1/M7 → r4 C1/M8 → r5 H1 → r6 H1 → r7 H1 → r8 C0/H0/M1/L2 = 着手可)。ユーザー裁定: U1 CLI のみ (API/bot に出さない) / U2 非対称反映を許容 / U3 版 dir は自動削除しない。spec 清書 → docs/superpowers/specs/2026-10-16-plugin-deploy-ops-design.md v1.0 + switch-ops-hardening v1.9。task 表 T1 → T2 → (T3, T4a, T6) → T5 → T4b → T7 → T8。
- 2026-10-16: 状態: 実装待ち — 設計収束、spec 清書中
- 2026-10-17: T1 (schema + store) を main に merge (`f48ccba`)。codex 3 周 (r1 High 1 / M 3 → /code-review 8 件 (採用 5) → r3 Medium 1 `live_identity` 未実装 → r4 Medium 1 containment / Low 1 正規名) を是正して閉鎖。判定 store / tools / plugin 緑。次は T2 (共通手順 + CLI 引き継ぎ部品)。
- 2026-10-17: T2 (共通手順 + CLI 引き継ぎ部品、approve / bless / retry / retire 入口の open op 拒否) を main に merge (`05ef241`)。codex r1 High 1 / M 2 / L 1 → /code-review 10 件 (採用 5、却下 5 は spec §2.2 / §2.1 / §6.4 と `_plugin_lock` の sink で理由付き) → codex r3 Medium 1 (setup の分類到達性) → 是正 3 本。判定 plugin / store / tools / setup / backtest 3410、loops / core 2851 緑。次は T3 (disable + revoke) と T6 (遅延化 + conn_signal + OPEN lock) を並行。
- 2026-10-17: T3 (disable + 依存列挙 + --force cascade + revoke_version、store に insert_done_revoke / get_by_id / latest_decision_for_identity) を main に merge (`2a51c74`)。codex 実装 (test 追補は上限直前) → /code-review 10 件 (採用 8、却下 #4 = IV-16) → codex r2 High 1 / M 2 (採用 2) → codex r3 閉鎖可。判定 plugin / store / tools 2378、loops / core / setup / backtest 3916 緑。ローカル LLM 3 本は走行中 (結果は後追い)。
- 2026-10-18: T4a (rollback: 戻り先決定 + IV-8 検証 1〜5 + temp symlink / atomic swap、reconcile: disable / rollback / retire_plain の各行 + 通知回収 + 両方非終端 conflict) を main に merge。/code-review 10 件 (採用 9、却下 #4 = artifact_hash は content_hash の上位集合) → 是正 → codex r2 High 1 (is_symlink 後 readlink 前の live 消失で FNF 漏れ) → 是正 + 変異確認で閉鎖。判定 plugin / store / tools 2471 緑。申し送り: T4b fault seam 一覧・T5 `_retired/` 前提・T6 reconcile 配線順と settings 値・T7 refusal_code 9 種 (`tmp/review-20261016-pdo/t4a-r2-out.md`)。
- 2026-10-18: 状態: 実装中 — T6 (scheduler 遅延化 + 専用接続 conn_signal + producer の lock 内再検証 + OPEN の source lock / 2 述語の鮮度 + lock 順序計装 + 起動時 deploy reconcile の配線 + settings v3 の worker.plugin_lock_timeout_sec / reconcile_lock_timeout_sec) を main に merge (0724a46)。codex 実装 3 回 → r1 High 2 / M 3 / L 1 → /code-review 9 件 (採用 8 + #9 部分) → codex r3 閉鎖 (実装) → 配線 + AC-34 / 22 (codex) → AC-19 / 20 / 21 の競合 test 10 本 (sonnet、実装欠陥 0) → /code-review 8 件 (採用 6、却下 2 = ActivityLog.write は非送出契約 / v3 は本 branch 導入) → codex r5 閉鎖可 → ローカル LLM 3 本 (21 走、実装欠陥 0、変異生存 8 → pin 8 本)。判定 plugin / store / tools / backtest 2944、core / service_app / setup 2788、loops 931 緑。申し送り: T4b (deadline 超過 op の次回収束、producer の fault seam)、T7 (versions で open のまま残る op の表示、signal_max_id 契約)、T8 (reconcile_lock_timeout_sec = 起動 1 回全体の予算、schema v3、latest_decision_status)。本番 settings は v2 のまま (afx init 再実行で v3 へ、既定値で動作に支障なし)
- 2026-10-18: 状態: 実装中 — T5 (retire 統合: symlink は disable_plugin(revoke=False) をそのまま呼ぶ、plain は retire_plain 状態機械 = name lock + config.yaml 名での依存確認 + st_dev 検査 + op_id を含む退避先を intent の tx 内で記録 + discover した content_hash を intent / 署名に記録し FS 直前と reconcile で照合 + 名前単位と依存 identity の pending 放棄 + rename 失敗は needs_retry + 署名一致の再実行で引き継ぎ。旧 switch.retire_plugin と自前 flock を廃止、CLI に --force / --dry-run、非成功 outcome は rc 1) を main に merge。codex 実装 → codex r1 High 1 (plain identity 未記録) / Low 2 → /code-review 10 件 (採用 8、却下 #5 = spec §2.1 の NULL 規定 / #7 = os.stat は mount・symlink 先の st_dev) → codex r3 Medium 1 (dry-run の force 不一致) → 是正。判定 (T6 結合 tree) plugin / store / tools / backtest 2963、core / service_app / setup / loops 3719 緑。申し送り: T4b (after_insert の各永続化境界の P 系 seam)、T7 (RetireOutcome → rc / 文言 / refusal_code)、T8 (plain identity の記録・名前単位 cascade を spec に明文化)。次は T4b (crash matrix 全点統合、下書き tmp/review-20261016-pdo/t4b-brief-draft.md)
- 2026-10-18: 状態: 実装中 — T4b 完了 (crash matrix 全点統合)。spec §7 の N1〜N5 / D1〜D12 / R1〜R19 / P1〜P12 / revoke / S1〜S7 / G1〜G7 を実 FS + 実 SQLite の fault injection (関数境界の CrashPoint、unlink / rename / symlink の失敗注入、process 死の再現) で網羅し、変異 M1〜M9 は全部対応 test が落ちる。実測で見つかった現物の欠陥を是正: rollback の CLI 再実行 (検証失敗の終端化、source 消失後の署名、既定 target の open 行からの解決、live が戻り先のときの dry-run 署名順序、foreign な live の誤終端)、disable の open 行引き継ぎを依存検査より先に、retire の退避先衝突は dry-run 含め拒否、syscall_failed から抜ける全遷移で通知をやり直す (settle_op)、終端化と通知回収を reconcile と CLI で共有、起動時回収で付随表に結ばれた claimed は abandoned で終端。裁定: linked signal は spec の pending でなく abandoned 終端を正とし T8 で spec を直す。残り T7 (CLI の rc / 文言 / 表示: verify_failed + live_removed、retire の refused 理由、open op の versions 表示) と T8 (spec 追記: sweep ④ と rollback temp、R4 / D11 / 6.4 の通知文言、G4a / G5 の abandoned、recover_interrupted の付随表参照)。

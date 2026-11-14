---
id: live-confirmed-bars-into-history
title: backtest の履歴が自動で更新されず、評価が古い入力で通る
status: 実装中
priority: 高
opened: 2026-09-21
closed: null
related: [history-import-stops-at-first-conflict, status-history-hidden-while-daemon-runs, improve-eval-window-reference-availability, cross-cutting-invariant-review]
backfilled: true
source_section: 完了ログ
kind: 不具合
---
# [live-confirmed-bars-into-history]

**状態**: 実装中 / **優先**: 高 / **種別**: 不具合

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[live-confirmed-bars-into-history]** (2026-09-21 ユーザー論点 + codex 確認 `tmp/design-a2/unify-check.md`、A2-1 の後): ライブで集めた確定足をバックテスト履歴に不変で積み上げ、cache は形成中の最新値だけにする (二重保存の解消、MT5 再 import を不要に)。前提 = A2-1 が確定足の規則を import と揃えること。設計項目: live 収集行の source 名と HistoryDataset の許可 / **`prune_cache` が source を問わず消す — 履歴を消さない GC** / 確定後に源が値を訂正した場合 / `backtest_runs` にデータの版 (fingerprint / as-of) が無く、履歴が日々増えると同条件の再実行で結果が変わる / 物理的な 2 表 → 1 表統合 (約 20.8 万行、テスト 11 ファイル、3 本目の data migration) はその先。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: ライブで集めた確定足を履歴に積めず二重保存になる — 仕分け (2026-10-02、現物で成立を確認): prune_cache の GC 範囲やデータ版の扱いが設計項目。 根拠: ライブ確定足を履歴に積む実装・commit が無い。
- 2026-11-11: 状態: 実装中、優先: 高、種別: 不具合、題名: backtest の履歴が自動で更新されず、評価が古い入力で通る — 2026-11-11: 評価用履歴 (mt5 5m) が USDJPY 09-04・参照 10-30 で止まり、holdout の充足率が 11-08 に 0.22 まで落ちたまま承認が通っていた。手で 11-10 まで取込み (参照 4 銘柄は 11-04〜11-09 が端末に無い)。sol・fable の実装案を突合し、段 1a (import の実時刻の境界・評価前検査・gate の既定拒否・承認の証跡照合・status・配備中 4 版の再評価) を設計 docs/superpowers/specs/2026-11-11-backtest-history-freshness-guard-design.md (C1) で承認。利用者裁定: 主 pair 不足は採用判定と承認だけ止める / 参照の穴は見せる / 同期は既定有効 / 09-04 以降承認の 15 件すべて再評価 / 非配備 7 版への rollback は監査まで拒否。
- 2026-11-13: 関連: [improve-eval-window-reference-availability] — 段 1a を main へ merge (2026-11-13)。履歴の鮮度・充足を共有 gate で検査し、主 pair が古い・穴ありなら採用判定と承認を止める。承認・再評価・rollback は承認時の証跡を照合し、rollback の強行は人間専用で、開始の監査を activity に、op との関連付けを DB に token で記録する (設計 C2.2)。評価区間の設計 (improve-eval-window-reference-availability) はこの上に乗せる。残り: 配備、task 6 (配備中 4 版の再評価)、段 1b (非配備 7 版の監査)、段 2 (afx history sync)、段 3 (daemon の定期同期)。
- 2026-11-13: 関連: [history-import-stops-at-first-conflict] — 段 1a の配備と task 6 を完了 (2026-11-13)。履歴を 11-13 12:05Z まで補充 (参照 4 銘柄が取り込めなかった真因は import の衝突停止、history-import-stops-at-first-conflict に起票)。配備中 4 版を再評価し #122〜#125 を作成、利用者が 4 件とも承認。manifest に記録し validate・verify_against_db とも問題 0 件。残り: 段 1b (非配備 7 版の監査)、段 2 (afx history sync)、段 3 (daemon の定期同期)、status-history-hidden-while-daemon-runs。
- 2026-11-13: 関連: [killswitch-reset-hwm-rebase] — 2026-11-13 22:20: 利用者判断で asym_shock_breakout (#122、holdout PF 0.81) と ichimoku_rsi_cloud_reentry (#124、PF 0.92) を afx plugin disable --revoke で配備から外し、版を rejected にした (追記 #126・#127)。配備中は close_pressure_followthrough と volume_close_pressure の 2 版。manifest の human_decision は再評価依頼 #122・#124 自体の決定 (approved) のままで、後の失効は DB の #126・#127 が記録する。
- 2026-11-14: 関連: [history-import-stops-at-first-conflict, status-history-hidden-while-daemon-runs, improve-eval-window-reference-availability] — 段 1b の設計 C2 を 2026-11-14 に利用者承認 (docs/superpowers/specs/2026-11-14-stage1b-non-deployed-audit-design.md)。非配備 7 版のうち 4 版 (#21・#25・#23・#24) は既に失効済みで rollback の恩恵は無いが、既存裁定どおり 7 版すべてを監査する (J0)。方式 D: 7 版を配備せず現在の条件で各 1 回 warn / compare off で評価し、人間専用 report と manifest (最小 v2、従来の v1 は受理を維持) に 4 軸 (実行完了 / 床合否 / 履歴検証 / 未実行 scope) で記録する。rollback が要るときは既存の人間専用強行を使い、段 1a 設計 §8.4 の「後続実装で通常 rollback」は需要まで延期 (J1)。7 版の実行は実行前に利用者が確認する。関連を段 1 の後続チケットに付け直した。
- 2026-11-14: 関連: [history-import-stops-at-first-conflict, status-history-hidden-while-daemon-runs, improve-eval-window-reference-availability] — 段 1b の task 1.1〜1.3 を実装 (非配備版を配備せず現在の条件で評価する入口 `plugin/history_audit.py`、共有 gate の失敗の記録先の切替、manifest 版 2 と版別の validate / verify)。段 1a 設計 §8.1・§8.3・§8.4・§8.5 と運用手順に今回の裁定を追記した (過去の裁定は残している)。通常 rollback を可能にする接続は需要まで延期。残り: task 1.4 (7 版の実行、実行前に利用者が確認)、段 2 (afx history sync)、段 3 (daemon の定期同期)、status-history-hidden-while-daemon-runs。
- 2026-11-14: 関連: [history-import-stops-at-first-conflict, status-history-hidden-while-daemon-runs, improve-eval-window-reference-availability, cross-cutting-invariant-review] — 2026-11-14: 段 1b の task 1.1〜1.3 を main に merge。非配備版を配備せず現在の条件で評価する入口 (plugin/history_audit.py、warn / compare off、保存版はコピー元を symlink を追わず検査してから fd でコピー、記録先は明示の非 DB sink、4 軸の人間専用 report)、manifest 版 2 (従来受理された v1 はそのまま受理)。gate の pytest worker に後方互換の状態の pipe を足した (本番 gate の判定は不変)。task 1.4 (7 版の実行) は利用者の確認の後。

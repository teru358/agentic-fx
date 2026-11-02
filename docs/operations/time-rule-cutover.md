# 時刻規則の切替 (21:00 UTC 固定 → NY 17:00 規則)

この手順は、daemon を新しい時刻規則の版へ更新する運用です。実行は daemon を動かしている repository root で行います。切替は片道で、旧版へ戻す手順はありません (末尾の「rollback はしない」を先に読んでください)。

## 何が起きるか

新版の daemon は起動時、instance lock を持った状態で最初の tick の前に、次の 4 通りに分けます。

| marker | capture | 動き |
|---|---|---|
| 無 | 無 | 切替。marker (`data_migrations` の `market_hours_ny_rule_v1`、`applied_at` が切替時刻 T_c) と capture (`alert_state` の `time_rule_cutover.capture`) を 1 つの transaction で書き、activity に `time_rule_cutover` を 1 行書く |
| 有 | 有 | 何もしない。activity が未記録なら 1 行だけ足す |
| 有 | 無 | capture を 1 回だけ補完する (`supplemented_at` つき、T_c は marker のまま) |
| 無 | 有 | 起動を拒否する (`time_rule_marker_missing`)。marker を消した DB である |

T_c から新規則の次の境界 T_b までの間だけ、旧規則と新規則の厳しい側が使われます。

- day 建玉の期限: T_c より前に始まった建玉は、旧新の早い方
- 日次損失の基準: 旧新の日初エクイティの大きい方 (どちらかが不明なら不明、新規は拒否される)
- signal の日次上限の起点: 旧新の早い方

日次通知のラベルは econ 側の日次レポートが main に入るときに扱います。

T_b 以後は新規則だけです。夏の切替では旧新の境界が同じで、何も変わりません。

## 手順

1. 時刻を決める。推奨は 2026-11-05 (木) 10:00-12:00 UTC (境界から遠く、週次改善と重ならない)。期限は 2026-11-06 (金) 20:30 UTC。旧版が金曜 21:00 UTC を越えると閉場処理をする (取引モードでは未約定 limit を取消し、新版でも戻らない)。停止時間に 20:30-22:30 UTC を含めない (停止中に境界を越えると、起動後の最初のスナップショットが基準になる)。
2. 実行中の mission が無いことを `afx ctl status` で確かめ、OPEN / PENDING_FILL の注文と日次損益を記録する。
3. `systemctl --user stop afx`。`afx status` で instance lock が空いたことを確かめる。停止から起動までの間に `afx plugin bless` / `afx plugin submit` と `afx history import` を走らせない。
4. 新版の commit に更新し `uv sync`。`systemctl --user start afx`。
5. activity の `time_rule_cutover` を 2. の記録と照合する。day 期限の適用値 (`applied`) が旧期限 (`legacy`) より遅い行が無いこと。日次基準の適用値が旧新の大きい方であること。`afx ctl status` で data が ready であること。

   ```bash
   grep -P '\ttime_rule_cutover\t' logs/activity.log
   ```

6. 当日 21:00〜22:00 UTC の日次損失の基準は旧新の大きい方 (上がることはあっても下がらない)。22:00 UTC (冬) で新しい日次に切り替わることを観測する。夏は 21:00 UTC で切り替わる。
7. 不具合が出たら: `systemctl --user stop afx` で止める。marker 欠落で起動拒否 (`time_rule_marker_missing`) になった場合も、systemd が再起動を繰り返すので同じコマンドで止める。新規発注が止まる。停止中は afx の監視、つまり day close と SL / TP の判定も止まるので、`afx status` と OPEN / PENDING_FILL を手で確かめる。修正を足した新版 (新規則と marker を保つ版) で起動する。

## rollback はしない (forward-fix だけ)

T_c の後に旧版へ戻してはいけません。日次基準と day 期限が旧 21:00 規則だけになり、冬は日次損失の基準が緩み、期限が延びます。T_b の後なら過渡規則も働きません。旧版は marker も DB の版も読まないので、旧版の起動を後から止める仕組みはありません。そこで次の三つで担保しています。

- この手順書に旧版へ戻す手順を書かない。
- 新版は marker を消す経路を持たない。
- marker を消した DB では新版が起動しない (`time_rule_marker_missing`)。

旧版を起動してしまった場合は、新版へ戻してください。marker は残っているので T_c は動かず、新規則で動きます。marker を手で消さないでください。消すと新版は起動を拒否します。

# bot の回復と kill switch 通知の運用

設計の正: `docs/superpowers/specs/2026-11-14-ops-alert-delivery-design.md`。

## 1. bot の終了と systemd の挙動

`afx-ops@bot` は gateway・配送・poll の進捗を自分で監視します。10 分進まなければ固定語を stderr に 1 行出して終了します。bot 専用の drop-in が、終了コードごとに systemd の再起動を分けます。

| 終了コード | 意味 | systemd |
|---|---|---|
| 79 | 一時 (Discord の一時障害、配送・poll の停止、state の I/O、停止処理の超過、想定外) | 再起動する。待ちは 30 秒から段階的に伸び、最大 10 分 |
| 80 | event loop が 10 分止まった | 同上 |
| 3 | daemon に届かない | 同上 |
| 78 | 恒久 (token 不正、Discord の login 拒否、state / payload / event の内容不正、配送先の拒否) | 再起動しない。failed のまま止まる |
| 71 | 起動・設定・secret・隔離の失敗 | 再起動しない |
| 2 / 4 / 69 / 70 | unit の引数、鍵、未実装、import の失敗 | 再起動しない |
| 0、SIGTERM / SIGHUP | 正常停止 | 再起動しない |

- start-limit は bot では無効です。一時障害が続いても failed に倒れず、間隔だけが伸びます。web と notify の再起動方針は変わりません。
- 恒久 (78 / 71 など) で止まったときは、人が原因を直してから明示的に再起動します。原因は `journalctl --user -u afx-ops@bot` の固定語と `afx ops bot status` で確かめます。71 は設定不正のほか、secret の一時的な読取り失敗や隔離の環境失敗も含みます。直した後も明示 restart が要ります。
- failed から戻すときは `systemctl --user reset-failed afx-ops@bot` の後に start します。開発機 (systemd 259) では reset-failed で再起動の段階も初期値に戻ることを観測しましたが、他の版では確かめていません。版を変えたときは `systemctl --user show afx-ops@bot -p NRestarts` を reset-failed の前後で見て確かめてください。
- 再起動の待ちが段階的に伸びるのは、自動再起動が続く間です。手動の restart や READY で段階が初期化されるかは systemd の版で変わり得るので、断定しません。長期の障害では、最悪の回復は検知の 10 分と再起動待ちの最大 10 分の合計までを見込みます。
- bot の recovery drop-in は systemd 254 以上を前提とします。`afx ops bot enable` は稼働中の user manager の版を照会し、254 未満か、版を取得できなければ `systemd_too_old` で拒否します。
- bot の unit は `Requisite=afx.service` です。afx が止まっている間は bot も立ち上がりません。afx を戻した後は、`afx ops bot status` と `systemctl --user is-active afx-ops@bot` で bot が active に戻ったことを確かめます。戻らなければ start します。

## 2. health の見方

bot は state dir の `gateway.snapshot` に最新の状態を置き、daemon が `/v1/status` の `bot_health` として読みます。`afx status`、`afx ops bot status`、web に同じ値が出ます。

| 値 | 意味 |
|---|---|
| healthy | gateway が接続済みで、配送と poll に停止が無い |
| unhealthy / stalled | gateway の不通、または配送・poll の停止。10 分続けば bot が終了して再起動する |
| stale | snapshot が 2 分より古い。bot が止まっているか、更新できていない |
| unknown | snapshot が無い・読めない・不正。旧版の bot でも出る |
| not_configured | bot を使わない構成 |
| permanent-failure | bot が恒久の失敗を記録して終了した。最後に記録できた終了判断で、systemd の現在の状態そのものではない |

- gateway が不健全な間は、Discord 上の承認 interaction が使えません。承認は `afx ctl` から行ってください。
- 起動前の失敗 (rc 71、import の失敗) と snapshot 自体を書けない失敗は最終値を残せません。stale / unknown / 古い値になり得るので、journal も見ます。
- 導入時は、bot と daemon の両 unit の実効 `XDG_STATE_HOME` と HOME から同じ state dir が解決されることを確かめます。bot を動かし、`afx ops bot status` の観測時刻が更新されれば一致しています。ずれていても別の場所を探しに行かないため、古い有効な値が表示され得ます。

## 3. `alternate_push=none` の意味

- `alternate_push=none`: 別 cursor の notify (`afx-ops@notify`) が enable 完了状態にありません。bot が止まると、kill switch の作動や承認待ちの通知を受け取る別経路がありません。`afx status` と `afx ops bot status` に固定の警告が出ます。
- `alternate_push=configured`: notify が enable 完了状態にあります。endpoint の値や種別は出ません。

この値は notify が設定されたことだけを表します。Discord と障害領域が独立していること、現在の到達性、配送の成功は保証しません。独立性は利用者が確認してください。notify は任意で、未設定でも daemon と bot は動きます。警告は bot の health を unhealthy にはしません。

## 4. kill switch の通知と reset

- drawdown でラッチすると、通常は次の scheduler tick の末尾で `guard_state_changed` (latched) の event が出ます。event になるまでの通常の遅れは、最大でおよそ 1 分に tick の処理時間を足した程度です。ラッチ自体の判定時点は変わりません。
- event になることと、届くことは別です。ops lane が取れない、DB が一時的に使えないといったときは、event 化が次の起動か tick に延びます。journal に固定語 `guard_notice_deferred` が出て、`afx status` の `guard_notice_pending` が立ちます。次の tick で自動的に再試行するので、通常は待つだけで足ります。続くときは DB と daemon の状態を確かめます。event が出た後の配送も、Discord や notify の障害で遅れたり止まったりします。届かないときは `afx ops bot status` と bot の health を確かめます。
- reset と reconcile の event も同じ経路で出ます。解除済みで event が無い世代 (導入前の世代) には後から通知しません。導入後の最初の起動で `guard_notice_backfill_skipped` が journal に出ることがありますが、閉じた世代を飛ばした正常な動作です。
- reset は `afx ctl --as approver killswitch reset <世代>` (または shell の `killswitch reset <世代>`) です。成功すると、どちらも次の固定文を 1 回出します。

```text
DD が閾値以上のままなら、次の新規判断で kill switch が再びラッチします。
```

- reset は HWM を付け直しません。DD が閾値以上のままなら、次の新規判断で再びラッチし、新しい世代の通知が出ます。固定文に数値は付かず、API の応答や event にも DD の数値は載りません。HWM を付け直す操作は別チケット (`killswitch-reset-hwm-rebase`) で扱います。
- bot や notify を止めても、kill switch の state と世代は変わりません。status、web、journal で確認できます。

## 5. rollback

順序が重要です。

1. 先に bot の recovery drop-in を戻す (`afx ops bot disable` で drop-in も外れる)。旧版の bot は新しい終了コードの意味を知らず、`StartLimitIntervalSec=0` だけが残ると、決定的な不正を無期限に再起動し得ます。
2. その後で afx-ops を戻す。
3. daemon を戻すと、自動ラッチの event 補完が止まります。既存の event と state は削除しません。
4. event の行、bot と notify の cursor は削除・巻き戻しをしません。`gateway.snapshot` は参照されない file として残して構いません。

# 取引通知の運用

発注・決済を ops event として保存し、bot が短い日本語メッセージにして投稿する運用です。設計の正は末尾の設計書です。

## 1. 届く通知

取引 event は次の 2 種です。

| event | 内容 |
|---|---|
| `order_state_changed` | 発注の状態変化。`placed`、`filled`、`rejected`、`cancelled`、`expired`。成行は `placed` を出さず、`filled` だけを出す。 |
| `position_closed` | 決済。約定・決済価格、損益、保有時間、決済理由を持つ。 |

bot の通常の文面は、たとえば次の形です。実際の識別子や取引値は各 event の値で置き換わります。

```text
[ペーパー] 約定しました #<order-id> <PAIR> 買い
数量 <qty> lot / 価格 <price>
SL <sl> / TP <tp>
起点 <origin>

[ペーパー] 決済しました #<order-id> <PAIR> 買い (<reason>)
決済価格 <close-price> (約定 <entry-price>) / 数量 <qty> lot
損益 <pnl> <CCY> (<pnl-r>R) / 保有 <duration>
起点 <origin>
```

`trade_amounts = "absolute"`（既定）は上記のように数量と損益の絶対額を出します。`relative` は Discord の文面から数量と損益の絶対額を省き、決済を次の形にします。event 自体と webhook の payload から数量・損益が消える設定ではありません。

```text
[ペーパー] 決済しました #<order-id> <PAIR> 買い (<reason>)
決済価格 <close-price> (約定 <entry-price>)
損益 <pnl-r>R (価格 <change>%) / 保有 <duration>
```

取引通知には設定済みの role/user mention を付けず、本文の値からも mention を発生させません。bot または notify の初回正常 poll は、その時点の high watermark を cursor に保存して配送しません。したがって、配備前の取引 event は流れず、配備後の新しい event だけが対象です。

## 2. 設定

### 2.1 daemon

`settings.yaml` の `notify.trade_events` は daemon の emitter の有効・無効を決めます。既定は `true` です。

```yaml
notify:
  trade_events: true
```

`false` は `order_state_changed` と `position_closed` の生成を止める rollback 用の設定です。bot の表示を絞る設定ではありません。変更後は `afx` を再起動します。

### 2.2 bot の `ops.toml`

bot の `ops.toml` では、次の optional な平坦 key を使います。key がない旧設定は既定値で起動します。不正な値は起動時に拒否されるため、変更後は再起動で確認します。

| key | 値 | 既定 | 意味 |
|---|---|---|---|
| `trade_notices` | `all` / `results` / `fills_closes` / `off` | `all` | 投稿する event の種類。`all` は全状態と決済、`results` は約定・拒否・決済、`fills_closes` は約定・決済、`off` は取引通知を投稿しない。skip した既知 event も cursor は進む。 |
| `trade_amounts` | `absolute` / `relative` | `absolute` | 文面の数値表示。`relative` は数量と損益の絶対額を出さず、R 倍数と価格変化率を出す。投稿する種類は変えない。 |

```toml
trade_notices = "results"
trade_amounts = "relative"
```

### 2.3 notify の `ops.toml`

notify の `deliver_trade_events` は真偽値で、既定は `false` です。`false` では取引 event を webhook に配送せず cursor だけ進めます。`true` にした場合だけ、検証済みの取引 event を既存形式で webhook に配送します。bot の設定とは独立です。

```toml
deliver_trade_events = false
```

実装上の値の集合と既定値は上表どおりで、設計書との相違はありません。

## 3. 配備

この手順は systemd user unit `afx`、`afx-ops@bot`、`afx-ops@notify` を前提にします。コマンドは daemon を動かしている repository root で実行します。

1. 初回 cursor の初期化、allowlist、数値検証、renderer、filter、設定 reader を含む afx-ops を先に配る。この段階では daemon が新 event を出さない状態にします。
2. bot と notify を再起動する。

   ```bash
   systemctl --user restart afx-ops@bot
   systemctl --user restart afx-ops@notify
   journalctl --user -u afx-ops@bot -b -n 100
   journalctl --user -u afx-ops@notify -b -n 100
   ```

   起動ログで想定した afx-ops の配備版が実プロセスで使われ、設定検証が成功したことを確認します。afx-ops を editable install している場合も、ファイルを更新しただけでは稼働中の process は更新されません。必ずこの再起動を行います。
3. bot と notify が健全で、event cursor が進められることを確認する。`events` は通常、保存済み cursor の続きだけを表示します。過去を確認しても通常 cursor を動かさないよう、`--after` を使います。

   ```bash
   uv run afx ctl status
   uv run afx ctl events
   uv run afx ctl events --after <event-id> --limit 100
   ```

4. daemon 側の emitter を配り、`notify.trade_events: true` を確認して有効にしてから daemon を再起動する。

   ```bash
   systemctl --user restart afx
   journalctl --user -u afx -b -n 100
   uv run afx ctl status
   ```

5. 最初の配備後の取引 event を確認する。

   ```bash
   uv run afx ctl events
   journalctl --user -u afx-ops@bot -b -n 100
   journalctl --user -u afx-ops@notify -b -n 100
   ```

   bot には設定に合う投稿が届くことを確認します。notify は既定で配送しませんが cursor は進みます。webhook への配送を選ぶ場合だけ `deliver_trade_events = true` に変更して notify を再起動し、配送を確認します。

## 4. rollback

afx-ops を戻す前に、必ず daemon の emitter を止めます。

1. `notify.trade_events: false` に変更し、daemon を再起動する（または emitter を含む daemon の変更を戻す）。

   ```bash
   systemctl --user restart afx
   journalctl --user -u afx -b -n 100
   ```

2. bot と notify の cursor が daemon の high watermark に追いつき、保存済みの取引 event を通過したことを確認する。

   ```bash
   uv run afx ctl events
   journalctl --user -u afx-ops@bot -b -n 100
   journalctl --user -u afx-ops@notify -b -n 100
   ```

3. その後で afx-ops を旧版へ戻し、bot と notify を再起動する。

`trade_notices = "off"` は bot の表示 filter にすぎず、daemon の event 生成も notify の配送方針も止めません。そのため rollback に使ってはいけません。emitter を止めずに afx-ops だけを旧版へ戻すと、新しい event code を読んだ bot / notify が停止します。

## 5. 欠落 counter の読み方

取引通知は best-effort です。通知の保存・配送の成否は発注や決済の成否に影響しません。`uv run afx ctl status` の「取引 event の欠落」には、次の 7 種の理由別 counter が出ます。

| counter | 意味 |
|---|---|
| `queue_drop` | sink の有限 queue が満杯だった、または worker が死んだ後に投入されたため捨てた。 |
| `observer_error` | 遷移後に event を作る observer が例外になったため捨てた。 |
| `db_busy_drop` | sink の DB 書き込みが busy / locked だった。sink worker 内で最大 1 秒再試行しても書けなかった後の欠落。 |
| `db_error_drop` | busy 以外の DB 書き込み失敗のため捨てた。再試行しない。 |
| `invalid_value_drop` | event の field 値または書式の検証を通らず、event を作成・保存しなかった。 |
| `worker_death` | sink の書き込み worker が予期せず停止した。以降の投入は `queue_drop` にもなり得る。 |
| `shutdown_drop` | 停止時の排出期限内に保存を確認できなかった件数。処理中の commit が直後に成功している可能性も含む、保存を確認できなかった件数の上限であり、取りこぼしを過少に数えない側の値。 |

欠落が起きると warning log とプロセス内 health は degraded になり、直近 60 秒に新しい欠落がなければ ok に戻ります。確認には次を使います。

```bash
uv run afx ctl status
journalctl --user -u afx -b -n 100
```

## 6. 設計の正

[発注・決済の通知 設計書](../superpowers/specs/2026-10-20-trade-notices-design.md) を正とします。

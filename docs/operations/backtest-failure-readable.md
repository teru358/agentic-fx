# backtest / plugin 評価の失敗を読む手順

改善 mission の backtest と、live の signal 計算 (plugin 評価) が失敗したとき、
人間がどこで何を見るかをまとめる。**改善 agent には数値も stderr も届かない**
(固定の `error` と固定 hint だけ)。詳しい理由を読めるのは人間だけで、
読む場所は 2 つ: `logs/activity.log` (要約) と `logs/agentic.log` (技術ログ、死因の事実)。

この文書は読み方の手順であり、設定や仕組みは変えない。

## 1. どこに何が残るか

| 場所 | 行の名前 | 出る場面 | 主な項目 |
|---|---|---|---|
| `logs/activity.log` (IMPROVE) | `backtest_cpu` | 改善 backtest が始まったあと、成功・失敗を問わず評価 1 回につき 1 行 | mission / plugin / scope / pair / deps / cpu_sec / cpu_source / result / returncode / signal |
| `logs/activity.log` (TECH) | `plugin_eval_failed` | live の signal 計算が失敗したとき (間引きあり、§5) | consumer / pair / interval / bucket / result (再通知時は suppressed_count) |
| `logs/agentic.log` | `plugin_worker_diagnostic` | worker 1 個ごとの後始末時 (成功・失敗とも) | plugin / code / cpu_sec / returncode / signal / stderr_unavailable、あれば truncated と stderr_tail |

`backtest_cpu` の実例 (2026-10-02、成功):

```
2026-10-02T18:03:41+00:00	IMPROVE	backtest_cpu	mission=575 plugin=ema200_rsi_pullback scope=in_sample pair=USDJPY deps=2 cpu_sec=14.238446999999999 cpu_source=parent_wait4 result=ok returncode=0 signal=null	-
```

対応する技術ログ (同じ worker):

```
2026-10-02 18:09:40,860 UTC INFO agentic_fx.plugin.sandbox: plugin_worker_diagnostic plugin=ema200_rsi_pullback code=None cpu_sec=15.023330999999999 returncode=0 signal=None stderr_unavailable=False
```

- 成功時の技術ログは `code=None`。`code` に値が入るのは失敗のときだけ。
- activity の欠測値は `null`、技術ログ側 (Python の表記) では `None`。どちらも「取れなかった」であり 0 ではない。
- `stderr_tail` は末尾 8 KiB を 1 行に escape したもので、**技術ログにだけ**出る。activity・改善 agent・承認画面には出ない。空なら項目ごと無い。
- started 前に失敗するもの (履歴なし・pair 未宣言) は worker に触れていないので `backtest_cpu` を書かない。

## 2. 改善 agent に見える面との対応

agent に返るのは下表の固定文言だけ (`src/agentic_fx/loops/improve_loop.py` の `_SANDBOX_CODE_TO_PUBLIC`)。
人間は activity の `result` / 技術ログの `code` から、agent が何を言われたかを逆引きできる。

| 内部 code (技術ログ) | activity `result` | agent が受ける `error` | agent への hint の趣旨 |
|---|---|---|---|
| `cpu_limit` | `cpu_limit` | `worker_cpu_limit` | より粗い timeframe、依存・計算の削減、または人間への報告 |
| `timeout` | `timeout` | `worker_timeout` | 計算を減らす、または人間への報告 |
| `crashed` | `crashed` | `worker_crashed` | 原因を推測せず人間へ報告 |
| `plugin_error` | `plugin_error` | `backtest_failed` | 候補を確認・修正、繰り返すなら人間へ報告 |
| `protocol_error` | `plugin_error` | `backtest_failed` | 同上 |
| `backtest_failed` | `backtest_failed` | `backtest_failed` | 同上 (起動前の検証失敗など) |
| (成功) | `ok` | (なし) | |

同じ content hash と pair の `worker_cpu_limit` を 2 回受けた後、3 回目の呼び出しを
実行せず `repeated_worker_cpu_limit` で拒否する (拒否は mission の transcript と
counters に残り、activity と台帳には出ない)。`worker_crashed` / `worker_timeout` は対象外。

## 3. `result` / `code` の語彙

| 値 | 何が起きたか | 人間が次に見るもの | 典型的な原因 |
|---|---|---|---|
| `ok` | 評価が正常に終わった | `cpu_sec` (余裕の目安、§4) | |
| `cpu_limit` | worker が寿命の累積 CPU 上限で OS に止められた | `cpu_sec` が上限付近か、`deps`、timeframe | 細かい足、多い依存、長い replay |
| `timeout` | 1 回の呼び出しが `sandbox_timeout_sec` (既定 10 秒) に収まらず、親が kill した | 技術ログの stderr_tail、plugin の計算量 | 重い計算、無限ループ、I/O 待ち |
| `crashed` | 上のどれとも断定できない異常終了 | 技術ログの returncode / signal / stderr_tail | メモリ上限 (OOM)、外部 kill、`SIGXFSZ` (ファイル書き込み上限)、プロセス消失 |
| `plugin_error` | worker は生きていて、plugin 側が失敗を返した | 技術ログの stderr_tail (例外の最終行) | plugin / 依存の例外、入力の検証失敗、戻り値の直列化失敗 |
| `backtest_failed` | 上記に当てはまらない一般失敗 (改善 backtest のみ) | `agentic.log` の traceback | 評価前提の不備 |
| `internal_error` | `SandboxError` 以外の例外 (live のみ) | `agentic.log` の warning | 想定外のバグ |

`protocol_error` (worker との通信の異常: 不正な JSON、応答が大きすぎる、読み取り失敗) は
activity / live の `result` では `plugin_error` に寄せて表示される。区別したいときは技術ログの `code` を見る。
live では起動前の `backtest_failed` に当たるものも `plugin_error` と表示される。

**`cpu_limit` と判定する条件** は次の 3 つが全部成り立つときだけ。

1. 親 (本体) が kill を送っていない (timeout や後始末の kill ではない)
2. worker が `SIGKILL` で終わった (CPU 上限は soft==hard で、カーネルが `SIGKILL` で止める)
3. 親が観測した worker の累積 CPU が、上限 − 0.05 秒以上

3 を満たさない `SIGKILL` は `crashed` になる。OOM や外部からの kill を CPU と断定しないため。
0.05 秒は、CPU 上限で止まった worker の CPU 観測値が上限を最大 26 ms 下回る実測に基づく余裕。
閾値すれすれで外部 kill が重なった場合は誤判定しうるので、activity と技術ログには CPU と signal を併記してあり、人間が再判定できる。

## 4. `cpu_source=parent_wait4` と旧い行の比較禁止

- 新しい `backtest_cpu` の `cpu_sec` は、**親が worker を回収した時点の CPU (wait4 の値)** で、失敗時も取れる。`cpu_source=parent_wait4` がその印。
- `cpu_source` の無い旧い行 (2026-09-20 以前の例: `... deps=2 cpu_sec=9.69...`) は、worker の**自己申告**値で、成功時にしか得られず、測り方も異なる。
- **旧い行と新しい行の `cpu_sec` を同じ列で比較・傾向判断してはいけない。** 「CPU が増えた / 減った」は新しい行同士 (`cpu_source=parent_wait4`) でだけ言える。

## 5. live の `plugin_eval_failed`

live の signal 計算は maintenance tick ごとに新しい worker で行う。失敗すると、その足 (bucket) の cursor は進まず、
同じ tick の以降の評価を止めて次 tick に再試行する。scheduler は止まらない。

- 通知の単位 (key) は plugin 名 × content hash × pair × bucket × `result`。
- **1 回目、61 回目、121 回目…** だけ activity (TECH `plugin_eval_failed`) と warning を出す。間の 59 回は出さない。61 回目以降の行には `suppressed_count=60` が付く。
- 解除 (次の失敗が 1 回目として通知される) になる条件:
  - 同じ plugin × pair で評価が成功した
  - plugin の内容が変わった (content hash が変わると key が別になる。旧 hash の記録は成功時に消える)
  - 対象の足が鮮度窓 (`signal_freshness_bars`) を過ぎて放棄された
- activity の書き込みが失敗しても評価の結果は変わらない。
- 重く、かつ完走する plugin が tick を遅らせる問題は、この仕組みの対象外 (別の課題)。

## 6. `worker_cpu_limit` が出たときの選択肢

先に `backtest_cpu` の `cpu_sec` と `deps` を見る。同じ戦略の ok の行 (新形式) があれば、その `cpu_sec` が実際の所要。

| 選択肢 | 内容 | 注意 |
|---|---|---|
| (a) 戦略側の計算量を減らす | 依存 (指標) を減らす、計算を軽くする。改善 agent に任せてよい。agent には hint で同じ方向が伝わっている | 同じ失敗が続く場合は 3 回目から拒否する (§2)。拒否は mission の transcript と counters に残り、activity と台帳には出ない |
| (b) `plugin.sandbox_session_cpu_sec` を上げる | `config/settings.yaml` の値を上げる (`config/settings.yaml.example` の既定は 60) | **再起動が必要** (`systemctl --user restart afx`、起動時に読み込まれる)。**live の signal 計算にも同じ上限が効く**。暴走した plugin が使える CPU の天井も上がる |
| (c) 判断足を粗くする | より粗い timeframe の戦略にする | 戦略の性質が変わる。人間の判断で決める |

(b) を選ぶ前の判断材料:

- 上限は**worker 1 個の寿命の累積 CPU** で、1 回の評価の上限ではない。replay 全体が 1 つの worker で走るので、足が細かい・期間が長い・依存が多いほど積み上がる。
- 手元の実測 (1h / 依存 2 本 = 約 10 秒、15m / 依存 3 本 = 約 64 秒) のように、上限の 60 秒付近で失敗するかどうかは足と依存で変わる。まず新形式の ok の行から必要量を見積もる。
- **上げても解決しない場合がある**: 累積なので、期間を延ばす・足を細かくする・依存を足すたびに再び届く。上げるほど暴走の検出も遅れる。根本策 (1 回あたりの上限と、評価回数に応じた予算) は別途設計中で、今回の仕組みは上限そのものを変えない。
- 1 回の呼び出しの wall 上限 (`sandbox_timeout_sec`、既定 10 秒) は (b) では変わらない。`timeout` が出るなら (b) は効かない。

## 7. 困ったときの確認手順

1. 状態の概観: afx の対話シェル (`screen -r afx`、抜けるのは `Ctrl-a d`) で `status`。kill switch・health・直近 mission を見る。plugin 評価の失敗は `status` には出ない。
2. 改善 backtest の結果: `activity 50 IMPROVE` で `backtest_cpu` 行を探し、`result` / `cpu_sec` / `signal` を読む。シェル外からは `grep backtest_cpu logs/activity.log | tail`。
3. live の失敗: `activity 50 TECH` で `plugin_eval_failed` を探す。`result` と `bucket` を読み、同じ plugin が繰り返し出ていないか見る。
4. 死因の事実: `grep plugin_worker_diagnostic logs/agentic.log | grep 'plugin=<名前>' | tail` で `code` / `cpu_sec` / `returncode` / `signal` を読む。`result` が `plugin_error` / `crashed` なら `stderr_tail` が理由になる。
5. 時刻で突き合わせる: activity の時刻 (UTC、秒まで) と技術ログの時刻 (UTC) は同じ worker の後始末が数秒内に並ぶ。`grep -n 'plugin=<名前>' logs/agentic.log` の前後の traceback も見る。
6. §6 の選択肢に進む前に、`cpu_source` の有無を確認する (§4)。旧い行で判断しない。

## 8. 誤読しやすい点

- **`signal` / `returncode`**: `returncode=-9` と `signal=9` は同じ `SIGKILL` で、CPU 上限とは限らない。判定は§3 の 3 条件で、`result=cpu_limit` なら条件を満たしている。`result=crashed` で `signal=9` なら、CPU が上限に届いていない (OOM・外部 kill の可能性)。`result=timeout` の `SIGKILL` は親が送ったもの。`returncode=0` / `signal=null` は正常終了。
- **`deps`**: その戦略が使う依存 (指標 plugin) の本数。依存が多いほど worker の CPU が増える。CPU の大小を比べるときは `deps` と pair・scope・足をそろえる。
- **`scope`**: `in_sample` と `holdout` の評価は別の行になる。同じ mission で行が複数あるのは正常。
- **`unreaped` / `orphan`**: kill のあと 5 秒たっても worker を回収できなかった印 (カーネル側で終了待ちなど)。その評価は `crashed`、CPU は `null` になる。回収待ちの worker は本体が内部で保持し、次の worker 作成時とサービス終了時に再試行して、回収できたら技術ログに `plugin worker orphan reaped pid=...` と 1 行残す。**たまに出る程度なら放置してよい。** 保持数が 64 を超えたときだけ `plugin worker orphan list exceeds 64 entries` が 1 回 warning で出る。これが出たら放置せず、OS 側 (D 状態のプロセスや、ディスク・NFS の詰まり) を調べる。
- **`stderr_unavailable=true`**: stderr を保存する一時ファイルを作れなかったことを示す。評価は続いているが、この行には `stderr_tail` も `truncated` も付かない。
- **拒否も `max_refusal_streak` に数える**: 3 回目拒否は tool の error として errors と streak を増やすので、閾値が 1 なら 1 回で mission が打ち切られる。

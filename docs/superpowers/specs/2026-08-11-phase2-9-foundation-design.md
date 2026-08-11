# Phase 2 プラン 9 設計書: 前提整備 (入力 spec 3 本 + 起票返済 + 可観測性)

- 日付: 2026-08-11 (初版)
- ステータス: **レビュー待ち** (CLAUDE.md 規約 — 了承を得てから実装計画の執筆に進む)
- 準拠: 本体設計書 **改訂第 17 版** (`c2355e8`) / 分解書 `2026-08-01-phase2-decomposition.md` (2026-08-11 改訂)
- 前提: プラン 8 完了 (main `c2355e8`, 1726 passed / 1 deselected)

## 0. 本書の役割 — 何を設計し、何を設計しないか

プラン 9 は**新機能を増やさない**。プラン 8 完了時点で積み上がった以下を返済し、改善ループ (プラン 10) が乗る基盤を固める。

| 由来 | 件数 | 設計状態 |
|---|---|---|
| 入力 spec 3 本 (gather deadline / ctx 超過診断 / ohlcv キャッシュ窓) | 11 task | **設計済み・レビュー済み・未裁定ゼロ** → 本書は再記述せず**ポインタで参照**する |
| spec 小改訂束 (設計書改訂 17) から落ちた実装項目 | 3 task | 設計書が**方針**を定めた。**実装形は本書で確定させる** |
| 独立起票 2 件 (reflection starvation / ohlcv 無制限増大) | 2 task | **未設計 → 本書が設計する** |
| Task 20 申し送り 2 件 (gate_rejected 可観測性 / llama_swap timeout) | 2 task | **未設計 → 本書が設計する** |

**計 18 task。** spec 小改訂束のうち **`plugins/` 入れ子 git (旧 Task 14) はプラン 10 へ移送した** (ユーザー裁定 2026-08-11 — 根拠は §1 D6)。設計自体は本書に残してあり、プラン 10 の入力として引き継ぐ。

**入力 spec を本書に転記しない**のは意図的である。設計・裁定の正はリポジトリの当該 spec にあり、重複させると必ず食い違う (spec 改訂 17 の作業中に、分解書と設計書の記述がずれている実例を作ったばかりである)。

**入力 spec (すべてレビュー済み・未裁定ゼロ)**:

| spec | 場所 | レビュー |
|---|---|---|
| ① gather deadline | `docs/superpowers/specs/2026-08-11-gather-deadline-design.md` | codex 1 周。**「確定仕様」節が正**。初稿本文は誤りを含む履歴 |
| ② ctx 超過の診断 | `docs/superpowers/specs/2026-08-10-context-overflow-diagnosis-design.md` (改訂 3) | codex 1 周 (I7/M4 全採用) |
| ③ ohlcv キャッシュ窓 + 配線 pin | `docs/superpowers/specs/2026-08-10-ohlcv-cache-fallback-design.md` (改訂 4) | codex 3 周 (6 → 3+1 → 2 で収束) |

---

## 1. 本書で新規に設計する 8 件

入力 spec が存在しない、または設計書が方針しか定めていない項目。**ここが本書の実質である。**

### D1. reflection の再試行ポリシー (Task 15) — 独立起票①

**問題** (`loops/reflection_cycle.py:75-79`):

```sql
SELECT o.* FROM orders o LEFT JOIN reflections r ON r.order_id = o.id
WHERE o.status='closed' AND r.order_id IS NULL ORDER BY o.id LIMIT ?
```

「reflection 行が無い closed order を id 昇順で 3 件」という抽出には**失敗の記憶が無い**。恒久的に失敗する order (ctx 超過・破損データ等) は毎回この 3 件の先頭に居座り続け、①**後続の order が永久に振り返られない (starvation)** ②`failed` の `missions` 行が周期ごとに増え続ける。

**設計 — signals の確立パターンを再利用する** (`requeue_count` + `abandoned` 終端。同じ問題に同じ形を使う):

- **新テーブル `reflection_attempts`** (`order_id INTEGER PRIMARY KEY REFERENCES orders(id)`, `attempts INTEGER NOT NULL DEFAULT 0`, `last_attempt_at TEXT NOT NULL`, `last_reason TEXT`)
  - **`reflections` に列を足さない理由**: 抽出条件が「`reflections` 行が無いこと」なので、失敗時にプレースホルダ行を入れると**その order は「振り返り済み」になってしまう** (再試行が 1 回も効かない)。`reflections` 行の存在は「振り返りが在る」の意味を保つ
- 抽出は `LEFT JOIN reflection_attempts a` を足し、`AND (a.attempts IS NULL OR a.attempts < :max)` を条件に加える
- 失敗時に `attempts + 1` と `last_reason` (**spec ② の安全化済み `reason` をそのまま使う** — 単一行・上限内) を upsert する
- **成功時は `reflection_attempts` の行を削除する** (codex I1)。振り返りが在る以上、失敗の記憶を持つ理由が無い。これにより**残るのは abandon された order の行だけ**になり、この表は「恒久的に失敗した order の集合」という有限で意味のある集合に収束する (成功時に消さないと、closed order が増え続ける環境でこの表も単調増加し、`missions` の増大を別の表に移し替えただけになる)
- `attempts` が上限に達した時点で activity `reflection_abandoned` を書く。**通知は出さない** — spec ② §4.5 の裁定 (reflection の失敗は資金に直結しないため activity で足りる) と揃える
- config: **`reflection.max_attempts`** (既定 **2**。signals の `signal_requeue_max` の既定 2 と揃える)。`settings.yaml.example` を同期する
- `ORDER BY o.id` は**変えない**。abandon が入れば starvation は解ける

**裁定: 一時障害と恒久障害を区別しない。ただし手動の復帰手段を用意する** (codex I2)。上限 2 回を短時間に使い切る一時障害 (モデル停止・DB の一時ロック・RAG 競合) でも恒久 abandon になる。これを**自動の時間ベース再試行で救わない**理由: ①「何分待てば一時障害か」を決める根拠が無く、恣意的な定数が 1 つ増える ②再試行を自動化すると starvation の再来を招く経路が復活する ③**振り返りは資金保護でも判断入力でもない** — 失われるのは過去トレードの学習材料 1 件であり、失敗コストが非対称ではない。代わりに **`reflection_attempts` の行を削除すれば再試行される**という単純な回復手段を持ち、対話シェルのコマンド (`reflect retry <order_id>` 相当) として露出させる。`reflection_abandoned` の activity が手がかりになる。

**この task は spec ② が置いた回帰ピンを意図的に破る。** spec ② のテスト 16 は「同一 order で 2 回 `run_pending` → `failed` の missions 行が 2 本」という**現挙動の固定**であり、上限 2 のもとでは 3 回目以降が走らなくなる。Task 15 は**このピンを更新する責任を負う** (削除ではなく「上限まで試行し、その後は試行しない」へ書き換える)。順序制約: **Task 4 → Task 15**。

**変異**: 上限判定を削除 (→ 無制限再試行が復活) / `attempts` の加算を削除 / abandon の activity を削除 / 抽出の `LEFT JOIN` 条件を落とす (→ starvation 復活) / 上限を大きな定数に差し替える。

### D2. `ohlcv` の保持ポリシー (Task 16) — 独立起票②

**問題**: `store/ohlcv.py` に `DELETE` が無く保持ポリシーも無い。1m は 1 ペアあたり 1 日 1,440 行ずつ増え続ける。spec ③ で**読み込み量**は窓ぶんに一定化するが、**DB のサイズ自体は増え続ける**。

**設計の中核 — prune は source を見なければ履歴を破壊する**:

`ohlcv` には**ライブ逐次キャッシュとバックテスト用一括インポートが同居する** (設計書 §12。一意キーは `(symbol, interval, bar_time, source)`)。実測した source ID の分布:

| 用途 | source ID | prune 可否 |
|---|---|---|
| ライブキャッシュ | `yfinance` / `twelvedata` / **`mt5-live`** (`_STORAGE_SOURCE = {"mt5": "mt5-live"}`, `price_provider.py:42`) | **prune 対象** |
| バックテスト履歴 | `dukascopy` / **`mt5`** (`backtest/mt5_import.py` は `import_bars(source="mt5")`) | **絶対に触らない** |

`mt5` と `mt5-live` が 1 文字違いで意味が正反対である。**symbol/interval/期間だけで prune すると、バックテストの履歴 (再取得に数時間かかる) を無音で消す。**

**裁定 (2026-08-11、ユーザー承認): `ohlcv` を `ohlcv_cache` と `ohlcv_history` の 2 テーブルに分割する。** 設計書 §12 を改訂済み。

当初の設計は「prune 対象をライブチェーン名から導出し、未知 source は決して prune しない」という**規約 + 変異テスト**で履歴を守るものだった。これでも実害は防げるが、**構造で守れるなら構造で守る**方がこのプロジェクトの原則に合う (improve worker に DB パスを渡さないことで遮断を成立させたのと同型)。分割すれば **`DELETE FROM ohlcv_cache` は履歴に到達できない** — 規約ではなく設計上の事実になる。

**今やる理由 (移行コストがゼロ)**: 実 DB は **116K・`ohlcv` 0 行**である (2026-08-11 実測)。分割は DDL の差し替えで済む。**後になるほど高くつく** (180 日運用で 161.9 MB)。

**境界が実際に割れていることを実コードで確認済み** — 両テーブルを跨いで読む経路は存在しない:

| 経路 | 読む source | 分割後 |
|---|---|---|
| plugin 採用ゲート | **`_EVAL_SOURCE = "dukascopy"` 固定** (`plugin/approval.py:86`) | 履歴のみ |
| `compare_sources` | `dukascopy` × `mt5` (`backtest/mt5_import.py:155`) | 両方履歴・同一テーブル |
| `PriceProvider._cached_bars` | ライブチェーンの source のみ | キャッシュのみ |
| 人間 CLI `--source` | ユーザー指定 | **履歴のみを対象とする** (下記) |

**設計の要点**:

- **書き分けは API で強制する**。書き込み関数をテーブルごとに分け (キャッシュ書き込み / 履歴インポート)、**それぞれが受理する source 名を検証する** (キャッシュ側はライブチェーン名のみ、履歴側はインポータ名のみ)。**呼び出し側がテーブルを引数で選ぶ形にしない** — それでは誤爆の余地を prune から API 層へ移すだけになる
- **prune は `DELETE FROM ohlcv_cache WHERE bar_time < cutoff` だけ**になり、source 絞りも「未知 source は消さない」fail-safe 分岐も**不要**になる
- **キャッシュのバックテストは対象外**とする。キャッシュは保持期間 (既定 30 日) で刈られるため**最低取引数 30 を満たす標本にならない**。人間 CLI のバックテスト・履歴分析は履歴テーブルのみを対象とし、ライブ source 名を渡されたら **fail closed** (「キャッシュはバックテスト対象外」と明示エラー)。昇格経路は YAGNI として起票のみ
- **`spread` 列はキャッシュ側に持たない** (ライブ経路は埋めないため常に NULL だった)
- **移行**: 空 DB なら DDL 差し替え。**非空の既存 DB に備えて**、source 名でキャッシュ/履歴に振り分ける migration を書く。**未知の source は履歴側へ入れる** (fail-safe の向き — 履歴側は削除されないため、判断を誤っても失われない)
- **保持期間の下限は読み込み窓から決まる**。spec ③ の `live_window_days(source, interval, lookback_days)` が要求する最大値を下回る保持期間は、キャッシュフォールバックを自ら壊す。config `datafeed.cache_retention_days` (既定 **30**) を設定検証にかけ、下回れば**起動拒否する** (`intervals` に `1m` 必須と同じ扱い。**黙って clamp しない** — 設定ミスを隠すため)

  **検証対象の集合を厳密に定める (codex C1)**。`live_window_days` は `lookback_days` を入力に取るので「全 intervals の最大値」だけでは値が定まらない:

  | 何を検証するか | 定義 |
  |---|---|
  | **検証する** | 構成済みの `datafeed.intervals` × **サービスが実際に使う既定 `lookback_days`** の組。この積集合に対する `live_window_days` の最大値を下限とする |
  | **検証しない** | `get_bars(lookback_days=N)` を既定より大きい `N` で呼ぶアドホック経路。**キャッシュ被覆は best-effort** であり契約に含めない (窓外はライブ取得に落ちるか、短い系列が返る)。`latest_1m_bar` の `lookback_days=1` は既定より小さいので常に被覆内 |

  **導出足を `intervals` に足すと保持期間の下限が跳ね上がる。これは仕様であってバグではない** — `_finest_native_base` は「導出できる最も粗いネイティブ足」を選ぶため (`price_provider.py:301`)、yfinance では `4h` → base `1h` → ratio 4 → `5 × 4 = 20 日`、**`1d` → base `1h` → ratio 24 → `5 × 24 = 120 日`** になる。既定の `intervals: [1m, 5m, 15m, 1h, 4h]` の下限は **20 日**で既定保持 30 日に収まるが、**`1d` を足すと 120 日が必要**になり、既定のままでは起動を拒否する。起動拒否のメッセージは**どの interval が何日を要求したか**を名指しする (「`1d` requires 120 days; cache_retention_days=30」)。粗い足をキャッシュから導出したいなら細かい足を長く持つしかない、という物理的なトレードオフを設定で表現しているだけである。

- 実行位置は **maintenance 経路** (signals の `expire_stale` / `reclaim_expired` と同じ場所)
- **`core_lock` の保持窓を「有界に」伸ばす**: 1 回の prune は `LIMIT` 付きの**有界バッチ**で削除する。maintenance hook は `core_lock` 内なので保持窓は必ず伸びる — 伸びること自体は避けられないので、**上限を保証する**のが設計目標である (大きな `DELETE` が SL/TP 監視を止めるのは本末転倒。CLAUDE.md 絶対制約)。バッチ上限は「1 回の DELETE が SL/TP 監視の許容遅延を超えない」ことを基準に決め、実測で裏を取る
- **バッチは「追いつくまで毎 maintenance」実行する (codex I3)**。「1 日 1 回・残りは次回」にすると、削除 `LIMIT` が日次増分 (1m で 1 ペア 1 source あたり 1,440 行/日、ペア数 × source 数で乗算される) を下回った瞬間に**永久に追いつかず DB が増え続ける**。正しい定常状態の見立ては次のとおり: **初回 prune だけが大量削除**であり (既存 DB の蓄積分)、追いついた後の 1 回あたり削除量は「1 maintenance 間隔ぶんの増分」に落ちる。したがって**有界バッチ × 毎 maintenance で追いつくまで回す**なら、初回の lock 保持窓を抑えつつ定常では自明に容量が足りる。受入条件として「日次投入量 > 1 回のバッチ上限」でも蓄積が収束することをテストで示す
- **ファイルサイズは `VACUUM` 無しでは縮まない** ことを明記する。自動 `VACUUM` は本 task のスコープ外 (長時間の排他が要るため)。運用手順として文書化する

**変異**: **prune の対象を `ohlcv_cache` から `ohlcv_history` に差し替える** (→ 履歴が消える。**このテストが最重要**。分割前は「source 絞りを外す」変異が担っていた検査点) / **キャッシュ書き込み関数に履歴 source 名 (`dukascopy`) を渡しても通るようにする** (→ source 検証の迂回) / **履歴インポートにライブ source 名を渡しても通るようにする** / migration の振り分けで未知 source をキャッシュ側に入れる (→ 削除されうる) / 保持期間の設定検証を削除 / バッチ `LIMIT` を外す / maintenance の呼び出しを削除 / 人間 CLI がライブ source を受理する。

### D3. `gate_rejected` の可観測性 (Task 17) — Task 20 申し送り③

**問題**: `gate_rejected` は `activity.write` のみで、集計も通知も無い。実測① の帰結である**「健全に見える `gate_rejected` を記録しながら一度も取引しないシステム」**が、ログを読まない限り誰にも上がらない。

**現スキーマからは導出できない (codex C2 — 初稿の設計は成立しなかった)**。当初は「executor に触らず `trade_intents.gate_result='rejected'` を数える」設計にしたが、実コードで 2 つ崩れた:

1. **`gate_result='rejected'` は Risk Gate 専用ではない。** `origin` 不正 (`executor.py:368-374`) と mission loop 不正 (`executor.py:377-384`) も同じ値を書く。行に却下**種別**の列が無く、`reject_reason` の文字列解析以外に区別手段が無い
2. **`action` の列が無い。** 「open の却下のみ」を絞るには `payload_json` の JSON 解析が要る (本コードベースに `json_extract` の使用実績はゼロ)

また **activity ログは DB ではなく TSV ファイル**であり (`activity.py:56`、ローテートあり・書き込み失敗は warning のみで握り潰す契約)、**集計の根拠にできない**。

**設計 (改稿) — 単一の chokepoint に構造化列を足す**:

- `trade_intents` に **`action TEXT`** と **`reject_category TEXT`** を追加する (migration)
- **`action` は `store/intents.py` の `insert(...)` に必須引数として渡す** (codex 2 周目 I2)。現行の `insert(conn, mission_id, payload: dict, now)` は `TradeIntent` ではなく**任意の dict** を受けるため、「`intent.action` から書く」だけでは書き込み契約が閉じない。payload から再抽出せず**呼び出し側に明示させる** (`executor.py:355` の唯一の呼び出し点)。**既存行の `action` は NULL を許す** — 移行前の行は集計対象外として扱う (集計クエリが `action='open'` で絞るので自然に除外される)
- **`reject_category` の書き込み点は `store/intents.py` の `set_gate_result(...)` ただ 1 つ**である。**既定値の無い必須引数**にして、全呼び出し点に種別の申告を強制する
  - **呼び出し点は 2026-08-11 時点で 21 箇所** (`core/executor.py` + `loops/trade_loop.py:318`)、**うち 4 箇所は `accepted=True`** である。件数を設計書に固定値として書かない — 実装時に全 call site を機械的に列挙すること (codex 2 周目 M1: 初稿の「10 箇所」は実コードと不一致だった)
  - **組み合わせ制約 (codex 2 周目 I1)**: `reject_category` は**引数としては常に必須**とし、値は **`accepted=True` のとき必ず `None`** / **`accepted=False` のとき必ず 4 値のいずれか** (`risk_gate` / `origin` / `mission` / `execution`) とする。この対応は**DB の CHECK 制約でも固定する**。ただし **`action IS NULL` の移行前既存行を必ず除外する (codex 3 周目 I1)** — legacy 行は `gate_result='rejected'` を持ちながら `reject_category` が NULL なので、素朴な CHECK では**テーブル再構築時のコピーで移行そのものが落ちる**:

```sql
CHECK (action IS NULL                                    -- 移行前の行は対象外
       OR gate_result IS NULL                            -- 未判定
       OR (gate_result='accepted' AND reject_category IS NULL)
       OR (gate_result='rejected'
           AND reject_category IN ('risk_gate','origin','mission','execution')))
```
  - **既定値を付けない理由**: 既定 `risk_gate` にすれば diff は数箇所で済むが、将来足される却下点が黙って `risk_gate` に混ざる。**「防御は適用範囲全体に当てる」**という本プロジェクトの規則 (プラン 8 Task 6 で 6 site 中 4 経路の迂回変異が生存した実測) に従い、機械的な全 site 編集を選ぶ
  - **executor に触らないという初稿の主張は撤回する。** ただし触るのは「引数を増やす」だけで、**判定ロジックは不変**である (受入条件で固定する)
- 判定は**純 SQL** になる (具体形は下記①〜③。**カテゴリでは絞らない** — 理由は後述)
- **バックテストの行は混ざらない** — `backtest/runner.py:153` は in-memory DB を使う (codex 2 周目で確認)
- **単位は口座全体** (シグナル起動のレート制限と同じ理由 — ペア単位にすると増やすだけで閾値の意味が消える)
- **直前の accepted 以降の open 却下件数**が閾値 (config `alert.consecutive_gate_reject` 既定 **10**) に達したら**通知を 1 回だけ出す**。通知には**支配的な却下理由**を添える (「10 件連続で却下、最多理由: データ不健全」)

**ラッチをカウンタで持たない (codex 1 周目 I4)**。`StateStore`/`AppState` は固定 4 フィールドで未知キーを持てず、カウンタを別に持つと「カウンタと実データの同期」という新しい壊れ方を作る。代わりに**連続数を毎回 SQL で数え直す**。

**連続区間の同一性は「直前の accepted の id」で表す (codex 2 周目 C1)**。初稿は「連続の先頭 (= 最新) intent id が最後に通知した id より新しければ通知」としていたが、**最新 id は却下のたびに進むので 11 件目・12 件目…で毎回条件が再成立し、「1 回だけ」にならない**。連続区間の中で**不変な識別子**を使う必要がある:

```sql
-- ① 区間の同一性 (この値は accepted が入るまで変わらない)
SELECT COALESCE(MAX(id), 0) FROM trade_intents
WHERE action='open' AND gate_result='accepted';          -- → streak_id

-- ② 直前の accepted 以降の open 却下件数 (カテゴリを問わない)
SELECT COUNT(*) FROM trade_intents
WHERE action='open' AND gate_result='rejected' AND id > :streak_id;

-- ③ 通知に添える支配的な理由
SELECT reject_category, COUNT(*) c FROM trade_intents
WHERE action='open' AND gate_result='rejected' AND id > :streak_id
GROUP BY reject_category ORDER BY c DESC LIMIT 1;
```

`streak_id` は **accepted が 1 件入るまで不変**であり、accepted が入れば新しい値になる (= 新しい区間)。`LIMIT` を使わないので「連続が窓長を超えると識別子がずれる」問題も起きない。

**②で `reject_category` を絞らない (codex 3 周目 I3)**。初稿は `reject_category='risk_gate'` で絞っていたが、それだと「risk 5 件 → execution 1 件 → risk 5 件」を 10 件**連続**として数えてしまい、**「連続」という語と SQL の意味 (accepted 間の累積) がずれていた**。絞りを外すと**この区間のすべての行が却下**になるので、累積と連続が一致して曖昧さが消える。意味論も正しくなる — 運用者が知りたいのは「**判断は出ているのに 1 件も建たない**」であり、却下が Risk Gate によるものか origin/mission 不正 (= 実装バグ) によるものかは**どちらも同じくらい上げるべき事象**である。カテゴリは③で診断情報として添える。

**`hold` は数えない** — hold は `gate_result` を書かずに早期 return するため (`executor.py:360`)、条件 `gate_result='rejected'` で自然に除外される。「取引しない」の正常形を異常として数えない。

- 永続状態は **`last_notified_streak_id` 1 個だけ**。新テーブル **`alert_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)`** に置く (`StateStore` の 4 フィールド契約を広げない)
- 通知条件は **`count >= threshold AND streak_id != last_notified_streak_id`**。通知後に `last_notified_streak_id = streak_id` を書く
- **accepted が入ると `streak_id` が変わる** → 次に閾値を超えたときは別区間として再び通知される。これが「ラッチ解除」であり、専用の解除処理を持たない

**`alert_state` の契約を明示する (codex 2 周目 M3 — 汎用 KV テーブル化による責務拡散を防ぐ)**: `value` は **str のみ** (数値は文字列で保持し読み出し時に変換)。既知 key は本 task 時点で **`gate_reject.last_notified_streak_id` の 1 個だけ**とし、**key の追加は設計判断として扱う** (何でも入る置き場にしない)。API は `get(key) -> str | None` と `set(key, value)` の 2 つ。

この形なら**再起動をまたいでも自然に正しい** (カウンタを持たないため)。kill switch のラッチ (人間の明示操作でのみ解除) とは**別物**である — こちらは資金を止める判断ではなく通知の抑制なので、状態が回復したら自動で戻ってよい。

**通知の送信失敗**は既存の notifier の契約に従う (失敗しても取引経路に伝播させない)。失敗時は `last_notified_streak_id` を**更新しない**ので**次の取引 Mission の commit-post で再送**される。**判定 (①②の SQL) と `alert_state` の更新は同一トランザクションに置かず、「通知成功 → 更新」の順**にする — 逆順だと送信失敗時に永久に再送されない。

**実行位置と接続の割り当て (codex 4 周目 I1)** — 評価は取引 Mission の commit-post 相で行う (D3'):

| 手順 | 接続 | lock |
|---|---|---|
| ①②③ の判定 SQL | **`conn_supervisor`** (lock 外の読み取り専用接続。プラン 8 新設) | **非保持** |
| 通知送信 | — (外部 I/O) | **非保持** |
| `alert_state` の更新 | `conn_core` | **`core_lock` を短く取得** (1 行の upsert のみ・外部 I/O ゼロ) |

`TradeLoop.conn` は scheduler と共有する `conn_core` なので、**判定を素直に `self.conn` で書くと lock 契約 (「`conn_core` は `core_lock` 保持中のみ触れる」) を破る**。読みは `conn_supervisor` を使う。

**五相契約の明確化 (codex 5 周目 I1)**: commit-post は「lock 非保持」と定義されてきたが、**その規則が禁じているのは「lock を保持したまま外部 I/O をすること」**であり (プラン 8 が解いた問題は LLM 実行と通知が lock を占有することだった)、lock 取得そのものではない。本 task は commit-post の中で **`alert_state` の 1 行 upsert のためだけに `core_lock` を短く取得する**。この区間は**外部 I/O を含まず有界**である。**設計書の五相の記述に「commit-post は外部 I/O を lock 外で行う。外部 I/O を含まない有界な DB 書き込みのための短い lock 取得は許す」という 1 文を足す** — 暗黙の運用にしない。

**例外隔離を既存の遅延通知と同じ契約にする**: 判定・通知・状態更新のいずれが失敗しても **commit-post 内で捕捉して握り潰す** (技術ログに warning のみ)。ここで例外を漏らすと、**finalize 済みの正常な Mission がサービス境界で失敗に見える**。

**なぜ「N 時間取引が無い」ではなく却下件数か**: 取引しないこと自体は正常でありうる (相場が動かない・LLM が hold を返す)。異常なのは**判断は出ているのに機械が全部弾いている**状態であり、それを直接表すのが却下件数である。

**変異**: 閾値判定を削除 / **`streak_id` を「連続の最新 id」に差し替える** (→ 却下のたびに再通知。**2 周目 C1 の再発を殺す変異**) / `last_notified_streak_id` の更新を外す (→ 毎回再送 = 実質的な無通知) / `action='open'` の絞りを外す (→ close/cancel の却下を数える) / **②に `reject_category='risk_gate'` の絞りを足す** (→ 区間内に他カテゴリが挟まると累積と連続がずれる。**3 周目 I3 の再発を殺す変異**) / `set_gate_result` の呼び出し 1 箇所で category を誤った値に変える (**全 call site に当てる** — 防御の適用範囲全体に) / 通知失敗時に id を更新してしまう / **判定を `conn_core` で行う** (→ lock 契約違反) / **評価を scheduler スレッドで実行する** (→ D3' の裁定が壊れる) / commit-post の例外捕捉を外す (→ 正常 Mission が失敗に見える)。

### D3'. 実行位置の裁定 — **scheduler スレッドに外部 I/O を一切載せない** (D2/D3/D6 共通)

**2 周にわたって誤った配線を設計した箇所である。経緯を残す**:

- **2 周目 I3**: maintenance hook は**毎 tick 呼ばれ、tick 全体が `core_lock` 内**だと判明した (`service.py:665-671`)。素直にぶら下げると prune の DELETE・通知・git が全部 lock 内で走る
- **3 周目 C1**: それを受けて「hook は queue に積み、lock 解放後に drain」と直したが、**これも誤り**だった。**lock を外しても同じ scheduler スレッドで同期実行する限り次の tick が始まらない**。git が 30 秒ハングすれば SL/TP 監視は 30 秒止まる。しかも **scheduler スレッド自体は生存しているので watchdog も検出しない** (最悪の形の無音故障)
- **3 周目 I2**: `Scheduler.tick()` は本体例外時も `finally` で `_run_hooks()` を実行して**例外を再送出する**ため、「lock 解放後に drain」は失敗経路で drain に到達せず、アクションが消える

**裁定: queue も専用スレッドも作らない。外部 I/O を scheduler の経路から外し、それぞれが本来属する場所に置く。** 新しい配管を足すほど壊れ方が増えるというのが上の 2 度の失敗の教訓である。

| 処理 | 実行位置 | 根拠 |
|---|---|---|
| **prune (D2)** | **maintenance hook (`core_lock` 内) のまま** | **DB 操作のみで外部 I/O が無い**。有界バッチなので lock 保持は**「伸ばさない」ではなく「有界に伸ばす」**が正確 (2 周目 I3 の表現訂正)。バッチ上限は「1 回の DELETE が SL/TP 監視の許容遅延を超えない」ことを基準に決め、実測で裏を取る |
| **D3 の判定と通知** | **取引 Mission の commit-post 相** | commit-post は**既に `core_lock` 非保持**で、**scheduler スレッドではなく mission supervisor スレッド**上にあり、**通知の定位置**でもある (`trade_loop.py:357-364`。「Notifier.send は最大 10 秒ブロックするので commit-post まで遅延させる」という既存の裁定がそのまま当てはまる)。判定材料 (`trade_intents` の行) はその Mission 自身が書いた直後であり、**評価する自然なタイミングそのもの**である。scheduler は一切関与しない |
| **D6 の git 操作** | **承認スレッド (シェル / API) 上で完結** | 承認は人間が起こす操作であり、**周期駆動を必要としない**。失敗した承認の再試行契機は ①サービス起動時の reconcile ②**次に何らかの承認操作が起きたとき** ③対話シェルの明示コマンド の 3 つで足りる。**scheduler tick に再試行をぶら下げない** |

これで **scheduler スレッド上で走る新規処理は「有界バッチの DELETE」だけ**になり、外部 I/O はゼロになる。3 周目 C1 と I2 はどちらも構造的に消滅する (queue が存在しないため ownership も再試行契約も不要)。

**受入条件で固定する**: 通知送信と git サブプロセス実行が **scheduler スレッドから呼ばれないこと** (呼ばれたら fail するシームを置く)。「lock 内か」ではなく「**どのスレッドか**」で検査する — 3 周目 C1 が示したとおり、lock だけを見る検査ではこの欠陥を捕まえられない。

### D4. approval の最新決定優先 (Task 12) — 設計書 §7

設計書 §7 が意味論を確定させた (kind=plugin の `(name, content_hash)` 単位で、`decided_at` 最大・同時刻は `id` 最大の決定が有効)。**実装形を確定させる**:

現状 (`tools/plugin_loader.py:70-101`) は `WHERE kind=? AND status='approved'` で承認済みハッシュ集合を作るため、**後から reject しても取り消せない**。

- 取得を **`status IN ('approved','rejected')` かつ `decided_at IS NOT NULL`** に広げ、**`ORDER BY decided_at, id`** で読む
- `payload_json` から `(name, content_hash)` を取り出して辞書に順に上書きし、**最後に残った決定が `approved` のものだけ**を admit する
- **`expired` / `invalidated` は決定として数えない**。どちらも「決定されないまま終端した要求」であり、**新しい要求の失効が古い承認を取り消すのは誤り**である
- 既存の防御 (payload が JSON でない / dict でない / `name`・`content_hash` が str でない行の warning + skip) は**そのまま維持**する
- SQL でグループ化しない (payload_json の中身が鍵のため)。行数は小さく、起動時 1 回の処理なので Python 側で畳んでよい

**変異**: reject-after-approve で取り消されること (→ 現状のバグが復活する変異) / approve-after-reject で再承認されること / **expired-after-approve で取り消され *ない* こと** / `ORDER BY` を落とす / `id` の同時刻タイブレークを落とす。

### D5. `signals.claimed_by_mission_id` の FK (Task 13) — 設計書 §12

SQLite は `ALTER TABLE ... ADD CONSTRAINT` を持たないため、**テーブル再構築**になる (新テーブル作成 → コピー → drop → rename → `PRAGMA foreign_key_check`)。UNIQUE 制約と既存インデックスを保つこと。

**設計の要点は移行時の宙吊り行の扱いである**。既存 DB には、参照先 `missions` 行が存在しない `claimed_by_mission_id` が残りうる (プラン 8 以前のクラッシュ経路)。コピー前に**status ごとに異なる修復**を当てる:

| 移行前の status | 修復 | 根拠 |
|---|---|---|
| `claimed` | `status='pending'` + `claimed_by_mission_id=NULL` + `claimed_at=NULL` | mission 行が無い claim は、まさに lease 回収 (`reclaim_expired`) が直すべき状態である。pending に戻せば次の機会に拾われる |
| `consumed` / `abandoned` | `claimed_by_mission_id=NULL` のみ | **終端状態を蘇らせない**。消費済みシグナルを pending に戻すと二重判断になる |
| `pending` | 変更なし (元々 NULL) | — |

移行は `PRAGMA foreign_keys=OFF` の下で行い、完了後に `PRAGMA foreign_key_check` が空であることを検査してからコミットする。

**変異**: FK を付けない (→ 存在しない mission id での claim が通る) / `claimed` の修復を落とす (→ 移行が FK 違反で落ちる) / `consumed` を pending に戻す (→ 二重判断) / `foreign_key_check` を削除。

### D6. `plugins/` の入れ子 git と承認有効化の不変条件 — **プラン 10 へ移送 (ユーザー裁定 2026-08-11)**

> **本節はプラン 9 のスコープ外である。** 以下の設計は codex 敵対レビュー 3〜6 周ぶんの指摘を反映済みだが、**Task 14 としては実装しない**。プラン 10 (改善ループ) の設計時に本節を入力として引き継ぎ、そこで最終収束させる。
>
> **移送の根拠**: ①設計書 §6 自身が「**価値は改善ループが plugin を上書きし始めてから発生する**」と明記しており、プラン 9 には**上書きする書き手が存在しない** — 守る対象が無い ②`plugins/` を Landlock の `read_write_paths` に足すのも同じ契機 (プラン 10) であり、**両者は「改善ループが書き始める」という単一の契機で同時に必要になる** ③本節は D1〜D8 のうち唯一 **3 周連続で Critical を生んだ**箇所であり、指摘が git の低レベル挙動 (unborn HEAD・CAS・detached HEAD・pathspec commit の意味論) に降りている。**書き手が居ない段階でこの複雑さを抱えるより、必要になる時点で実装する方が安い**
>
> なお「**承認台帳はあるが原本が無い**」という現状のリスクは、**プラン 9 の期間中は顕在化しない** (改善ループが動いていないため、plugin を上書きするのは人間だけであり、人間の編集は親リポジトリの git 履歴の外にあるとはいえ意図的な操作である)。

設計書が確定させた 3 固定点 (submodule にしない / commit は親のみ / git 失敗を資金保護に波及させない) と、不変条件ⓐ〜ⓓ (決定時ハッシュ再照合・対象パス限定 commit・同一対象の直列化・後発決定優先) を実装形にする。

- **リポジトリの初期化**: `plugins/.git` が無ければ親が `git init` する (lazy)。親リポジトリからは独立しており、gitlink は発生しない

**承認時の手順は「専用 index + plumbing」で組む (codex 4 周目 C1)**。ポーセリンの `git add` / `git commit` を使う限り TOCTOU は閉じない:

- **`git commit -- <path>` は index を commit する操作ではない。** pathspec 付きの commit は**そのパスのワークツリー内容を取り直して commit する**ため、「index を検証してから `commit -- <name>/`」では**検証後のワークツリー書き換えを拾ってしまう** (3 周目の修正は穴が残っていた)
- 一方、pathspec 無しの `git commit` は**他 plugin の stage 済み差分を巻き込む**。リポジトリ単位の直列化をしても、**操作開始前から存在する staged 差分**は排除できない

したがって**共有 index を一切使わず、承認ごとに使い捨ての index を作る**:

```
ref  = git symbolic-ref HEAD              # 対象 ref (unborn でも HEAD が指す ref を返す)
old  = git rev-parse --verify <ref>       # 失敗 = unborn (初回)

GIT_INDEX_FILE=<tmp>  git read-tree <old>        # unborn なら read-tree --empty
GIT_INDEX_FILE=<tmp>  git add -- <name>/         # 対象だけを stage
GIT_INDEX_FILE=<tmp>  git cat-file blob :<name>/plugin.py   # ← 検証はこの内容に対して
GIT_INDEX_FILE=<tmp>  git write-tree             # → tree
                      git commit-tree <tree> [-p <old>] -m ...   # unborn なら -p なし
                      git update-ref <ref> <new> <old>           # ← CAS (unborn は <old> に空文字)
```

**初回コミットが無い状態 (unborn HEAD) を分岐する (codex 5 周目 C1)**。`read-tree HEAD` / `HEAD^{tree}` / `commit-tree -p HEAD` は**すべて root commit の存在を前提にしており、`git init` 直後には全滅する**。`plugins/.git` は本 task が lazy に作るので、**初回承認は必ずこの経路を通る**:

- `read-tree --empty` で空 index から始める
- `commit-tree` の `-p` を付けない (root commit)
- 空 commit 判定 (`write-tree` == `HEAD^{tree}`) は**行わない** (比較対象が無い。unborn では常に commit を作る)
- **対象 ref は `git symbolic-ref HEAD` で解決する**。**unborn (初回) でも HEAD が指す ref 名は返る** — ブランチ名は `init.defaultBranch` 次第で `main` とは限らないので、**既定名を決め打ちしない**
- **`symbolic-ref` の失敗は終了コードで区別する (codex 6 周目 I1)**。「失敗 = detached HEAD」と一括りにするのは誤りで、**記録する理由と運用判断が変わる**:

  | exit | 意味 | 動作 |
  |---|---|---|
  | 0 | symbolic ref が取れた | 続行 |
  | 1 | **detached HEAD** | 承認を成立させず pending。理由「人間が履歴を操作中」 |
  | 128 (その他) | **git / リポジトリの障害** (破損・権限・I/O) | 承認を成立させず pending。理由「git リポジトリ異常」 |

  どちらも fail closed で pending に残す点は同じだが、**activity と通知に残す理由が異なる** (前者は待てば解消する人間の作業、後者は運用者の対処が要る故障)

**`update-ref` は必ず CAS で行う (codex 5 周目 C2)**。手順は**同一対象 (plugin) について直列化**されるが、**異なる plugin の並行承認は直列化されない**。旧 OID を検証せずに `update-ref` すると、**後発が先発の commit を履歴から外したまま `decide(approved)` を続行**し、「approved なら必ず commit 済み」という不変条件が破れる。

- `read-tree` 時点で読んだ `<old>` を `git update-ref <ref> <new> <old>` の第 3 引数に渡す
- **CAS が失敗したら手順を頭からやり直す** (最新 HEAD で `read-tree` し直す)。有限回で諦め、失敗時は pending のまま残す
- **より安全側の代替として、リポジトリ単位のロックで全 git 操作を直列化してもよい** (承認は人間操作で頻度が低く、並行性を犠牲にするコストが無い)。**実装はどちらでもよいが、CAS 無しの `update-ref` は不可**

- **利用者のワークツリー・index の状態から完全に独立**する。ユーザーが何を stage していようと承認は成功し、逆に無関係な差分が「承認済み状態」に混入することもない
- **検証した内容がそのまま commit される** — `write-tree` は検証に使った専用 index から tree を作るので、**検証と commit の間にワークツリーが変わっても結果は変わらない**。これで TOCTOU が閉じる
- **空 commit の判定も専用 index で行う**: `write-tree` の結果が `HEAD^{tree}` と一致すれば**内容に変化なし**なので commit を作らず成功として扱う。`git diff --cached` を使わないので「他 plugin の staged 差分に反応する」問題 (3 周目 M1) も構造的に消える
- **ハッシュ照合の実装 (codex 4 周目 M1)**: `git cat-file blob :<name>/plugin.py` / `:<name>/config.yaml` で**バイト列**を取り出し、それを content_hash に通す。現行の `content_hash(Path)` (`plugin/loader.py:91`) は Path を受けるので、**bytes を受ける内部ヘルパへ切り出し、Path 版はその薄いラッパにする** (`sha256(b"plugin.py\0" + bytes + b"\0config.yaml\0" + bytes)` の定義は変えない)
- submit 時の照合だけでは、申請〜承認操作の間の差し替えを捕まえられない (だから決定時に再照合する)
- 検証を通ったら commit し、**その後に `decide(status="approved")`** を実行する
- **commit 失敗時**: approval 行は **pending のまま**にし、activity + 通知を出す。**原本を保全できないまま plugin を有効化しない (fail closed)**

**git と SQLite は原子化できない — 収束性で担保する (codex I5)**。「commit → decide の順序が成立条件」という初稿の言い切りは**過大だった**。commit 成功後 `decide` 前のクラッシュ・DB 障害で「**git は commit 済みだが approval は pending**」が残りうる。これを状態機械の照合で解くのではなく、**手順全体を冪等にして再試行が収束する形**にする:

- **「同一内容なら commit を作らず成功扱いにする」分岐を明示的に書く (codex 2 周目 I4)**。ポーセリンの `git commit` は変更が無いと "nothing to commit" で**非ゼロ終了する**ため、素朴に扱うと**永久に pending になる**。上記の plumbing 手順では **`write-tree` の結果が `HEAD^{tree}` と一致するかで判定**する
- 再試行時は手順を頭からそのまま流せばよい — 専用 index を組み直し、ハッシュ照合は同じ結果を出し、tree が一致すれば commit は作られず、`decide` が実行される
- **中間状態は「pending のまま」に統一される**ので、外から見た不変条件は「**approved になっている plugin は必ず commit 済み**」の一方向だけになる。逆 (commit 済みなら approved) は保証しない — 保証する必要が無い (承認されていない commit は履歴に残るだけで、ロードには `approved` が要る)
- **再試行の契機は 3 つだけ (D3' の裁定)**: ①**サービス起動時の reconcile** ②**次に何らかの承認操作が起きたとき** ③対話シェルの明示コマンド。**scheduler tick にぶら下げない** (周期駆動を持たない)
  - **起動時 reconcile の位置 (codex 4 周目 I3)**: `init_db` と中断 Mission の回収より**後**、かつ **`approved_plugins()` (`service.py:466`) より前**に置く。後に置くと、その起動では復旧した承認がロードされず**次の再起動まで有効化されない**
  - **git 不在・reconcile 失敗はサービス起動を止めない** — 警告を出して当該 approval を pending のまま残すに留める
- **ⓓ 後発決定優先**: 再試行の前に「同一対象へのより新しい決定」を確認し、後発の reject があれば保留中の承認を**失効させる** (再試行で復活させない)。D4 の最新決定優先と同じ順序規則を使う
- **git の可用性**: git コマンド不在なら plugin 承認は成立しない。これは**起動時に検査して警告する** (SQLite ≥3.35 の起動 assert と同じ扱い)。本プロジェクトは git 管理下で動く前提なので、依存として受容する
- **commit identity を明示的に供給する (codex 6 周目 I2)**。`git init` で作った入れ子リポジトリは**親リポジトリのローカル `user.name` / `user.email` を継承しない**。global/system 設定も `GIT_AUTHOR_*` / `GIT_COMMITTER_*` も無い環境では **`commit-tree` が失敗し、承認が恒久的に pending になる**。したがって**サービス固定の identity を環境変数で渡す** (例: `agentic-fx <noreply@localhost>`) ことを契約とする — 利用者の git 設定に依存させない。**具体的な値と設定方法は実装計画で決めてよい**が、「identity を供給する主体はサービスである」ことは設計で固定する
- **資金保護への非波及**: 本経路は**承認スレッド (シェル / API) と起動シーケンス**でのみ実行し、**scheduler スレッドでは決して実行しない** (D3')。`core_lock` にも触れない。git がどう失敗しても SL/TP 監視と発注経路は動き続ける

**Landlock への波及 (プラン 10 への申し送り)**: 改善 worker が `plugins/` に書けるようにするには `read_write_paths` に `plugins/` を足す必要がある (プラン 8 Task 18 の配線は `read_only_paths=[code_root, sys.prefix, sys.base_prefix]`)。**本プランでは配線しない** — 書き手が居ないうちに権限だけ開けない。

**変異**: 決定時のハッシュ再照合を削除 (→ 差し替えが通る) / **照合対象を index からワークツリーに戻す** (→ TOCTOU が復活。3 周目 C2 の再発を殺す) / **専用 index をやめて共有 index + `git commit -- <path>` にする** (→ 検証後のワークツリー書き換えが commit される。**4 周目 C1 の再発を殺す変異**。killer テスト = 「検証後に plugin.py を書き換えてから commit させ、commit された内容が検証済みの内容であること」を assert) / 空 commit 判定を `write-tree` 比較から `git diff --cached` に戻す / commit と decide の順序を入れ替える / commit 失敗時に承認を成立させる / 後発 reject の確認を削除 / **起動時 reconcile を `approved_plugins()` の後に置く** (→ その起動で有効化されない) / **git 実行を scheduler スレッドから呼ぶ** (→ D3' の裁定が壊れる)。

### D7. `improvement_runs` の PR 列 (Task 19) — 設計書 §12

設計書改訂 17 で PR 経路が廃止され、`result='pr'` と `pr_url` が死んだ概念になった (`store/db.py:81-87`)。

**裁定: プラン 9 で migration を実施する** (`pr_url` を落とし、`result` の値域を `approval | report` として文書化する)。

根拠: ①**このテーブルには書き手がまだ居ない** (改善ループはプラン 10 で実装される) ので、今なら空テーブルの再構築で済み最も安い ②プラン 10 が「死んだ概念を含むスキーマ」に対して最初の書き込みを実装するのは筋が悪い ③放置すると「なぜこの列は常に NULL なのか」を後から誰かが調べ直すことになる。

**防御**: 移行時に **`result='pr' OR pr_url IS NOT NULL`** の行が 1 件でもあれば**中断してエラーにする** (想定外の状態を黙って捨てない)。`result` に CHECK 制約が無いため **`result='pr', pr_url=NULL` は現スキーマで合法**であり、`pr_url` だけを見るガードはこの行を見逃して不正値を新契約に持ち込む (codex I6)。**新テーブル側には `CHECK (result IN ('approval','report'))` を付ける**。

### D8. `llama_swap.timeout_sec` の実測 (Task 18) — Task 20 申し送り②

**これは設計 task ではなく計測 task である。** 既定 300 秒の妥当性は未確定で、実測済みなのは**モデルがロード済みの状態で 31.16 秒** (プラン 8 Task 20) だけ。**TTL unload 後の cold load を含む初回が未計測**である。

- 計測は **cold (TTL unload 後の初回) と warm (ロード済み) の 2 点**を採る ([[bench-scaling-before-committing]] — 1 点では外挿できない)
- **trade モデルと improve モデルが異なる場合は cold load が 2 回発生する** (spec ② §4.4 が init について同じ指摘をしている)。両方を計測対象にする
- 記録には**環境を含める** ([[measurement-is-environment-bound]] — モデル・量子化・VRAM・同時ロード数・TTL 設定)。一度の観測を構造的主張に格上げしない
- 結果に応じて `timeout_sec` を調整する。**計測前に値を決めない**

---

## 2. task 一覧・依存・並列束

**18 task** (旧 Task 14 = D6 はプラン 10 へ移送。**番号は振り直さない** — 過去のレビュー記録との対応を保つため)。**worktree 並列は「束」単位**で、節目確認も束単位で行う (プラン規約)。

| 束 | # | task | 由来 | 依存 |
|---|---|---|---|---|
| **A** | 1 | `MissionResult.reason` 契約 + `AgentRunner` docstring 規範 | spec ② | — |
| A | 2 | ctx 超過の検知 (envelope 解析・`safe_text`・**status code で分岐しない**) | spec ② | 1 |
| A | 3 | reason の worker 運搬 (`mission_worker` → `worker_runner`) | spec ② | 1 |
| A | 4 | trade / reflection 出口 (activity + 通知 / `reflection_mission_failed` 新設) | spec ② | 1, 2 |
| A | 5 | init の `n_ctx` 可視化 (重複除去・improve→trade 順・限定 except) | spec ② | — (A 内で独立) |
| **B** | 6 | monotonic 注入 + gather deadline (OPEN/**CLOSE 両方**) | spec ① | — |
| B | 7 | deadline の脚伝播 (`to_account_rate` の `for spec in legs`) | spec ① | 6 |
| **C** | 8 | 窓計算ヘルパ + floor (`1d` を含む) | spec ③ | — |
| C | **16** | **`ohlcv` の 2 テーブル分割** + 保持ポリシー (**束 C の中に置く — 下記**) | D2 | 8 |
| C | 9 | `lookback_days` 配線 (**既定値の推測禁止**) | spec ③ | 8 |
| C | 10 | `_cached_bars` 窓適用 (**source 単位 try の中**) | spec ③ | 8, 9, **16** |
| C | 11 | 本番連鎖 E2E pin + 実データ実測 | spec ③ | 10 |
| **D** | 12 | approval 最新決定優先 | D4 | — |
| D | 13 | `signals.claimed_by_mission_id` FK migration | D5 | — |
| D | 19 | `improvement_runs` の PR 列 migration | D7 | — |
| **E** | 15 | reflection の再試行ポリシー | D1 | **4** (ピンを更新する) |
| E | 17 | `gate_rejected` の可観測性 (`trade_intents` 列追加 + `alert_state`) | D3 | — (ただし `executor.py` を触るため束 B の後) |
| E | 18 | `llama_swap.timeout_sec` 実測 | D8 | — (計測のみ) |

**実行グラフ (codex I7 — 初稿は「A/B/C/D 並列」と「B は C の後」が自己矛盾していた)**:

```
A ─┐
C ─┴─ B ─┐          C = 8 → 16 → 9 → 10 → 11 (この順で直列)
D ───────┴─ E   (E は A・C・D・B すべての後)
```

**束 C の内部順序 — Task 16 (分割) を `_cached_bars` の改修より前に置く (codex 指摘)**。分割を後に回すと、Task 9〜11 を旧 `ohlcv` 前提で実装・単体テスト・E2E pin まで作った直後に、Task 16 が **API・fixture・SQL・assert を全面的に作り直す**ことになる (特に E2E テスト 11/12 は**保存先テーブルまで観測する**ので、そのままでは成立しない)。

- **Task 16 は Task 8 のみに依存** (保持期間の下限計算に窓計算ヘルパを使う)
- **Task 10 は 8・9・16 に依存** — 最初から**キャッシュ専用 API に対して**実装できる
- Task 16 が束 C に移ったので、**束 E は 15・17・18 の 3 task** になる

- **A / C / D は worktree 並列**に置ける
- **B は C の後に直列**で流す (両者が `price_provider.py` の別関数を触る。B は 2 task と小さく、並列の利得より衝突解決コストが上回る)
- **E は A・C・D に加えて B の後**でもある (codex 2 周目 I5 — 初稿のグラフは E を「A・C・D の後」としか書いておらず、**C 完了後に B と E を並列実行できてしまい `executor.py` が競合した**)。依存の内訳: A・C は成果物 (Task 4 のピン / Task 8 の窓計算)、D は **DDL の集中** (`store/db.py`)、**B は `executor.py` の競合** (B = spec ① の deadline、E の Task 17 = `set_gate_result` 引数追加)

### task 間のファイル競合 (worktree 並列時の注意)

| ファイル | 触る task | 競合の性質 |
|---|---|---|
| `service.py` | **5** (`_check_llama_swap` ≈ :79) / **11** (コメント :518-523) | 関数が異なるので論理衝突は無いが**同一ファイル**。マージ時に注意 |
| `reflection_cycle.py` | **4** (activity 新設) / **15** (再試行ポリシー) | **順序依存**。15 が 4 のピンを更新する |
| `trade_loop.py` | **4** (reason 付与) / **17** (`set_gate_result` の call site が 1 箇所ある) | 領域は異なるが**同一ファイル**。17 は executor と trade_loop の**両方**の call site を触る |
| `store/db.py` | **13** / **15** (`reflection_attempts`) / **16** / **17** (`alert_state` + `trade_intents` 列追加) / **19** | **DDL/migration が集中する**。束 D と E を同時にマージしない (E を D の後に置く根拠) |
| `core/executor.py` | **6-7** (spec ① deadline) / **17** (`set_gate_result` の引数追加) | 束 B と E。**判定ロジックは双方とも不変**であることを受入条件で固定する |
| `price_provider.py` | **7** (脚伝播) / **8-10** (窓) / **16** (source 一覧の参照) | 束 B と C が同一ファイルの別関数を触る。**最も競合しやすい** |

**`price_provider.py` の競合は束 B と C の並列を制限する。** 束 B (2 task) は小さいので、**B は C の後に直列で流す**のが安全 (worktree 並列の利得より、同一ファイルの衝突解決コストが上回る)。

---

## 3. 受入条件

- **入力 spec の受入条件をすべて満たす** — 件数をここに転記しない (再乖離を防ぐため各 spec の現行受入条件を正とする)
- **変異は 1 つずつ独立に当て、殺した固有のテスト名を記録した mutation ledger を成果物とする**。「何本落ちたか」ではなく「狙った検査点のテストが落ちたか」を見る
- **決定論的コア (`core/risk_gate.py` / `core/paper_broker.py` / `core/transitions.py`) は diff ゼロ**
- **`core/executor.py` の変更は 2 種に限る**: spec ① の deadline 追加と、Task 17 の `set_gate_result` 引数追加。**どちらも判定ロジックを変えない** — 却下条件・受理条件の差分がゼロであることをレビューで明示的に確認する
- 既存テストが 1 本も壊れない (プラン 9 開始時点 1726 passed / 1 deselected)
- 新規 config キーは `config/settings.yaml.example` と同期する (`reflection.max_attempts` / `datafeed.cache_retention_days` / `alert.consecutive_gate_reject`)
- **migration は既存 DB に対して冪等**であること (**Task 13 / 15 / 16 / 17 / 19**)。空 DB と既存 DB の双方でテストする。**Task 16 の分割 migration は「非空の旧 `ohlcv` を source 名で振り分け、未知 source は履歴側へ」**をテストで固定する
- **テーブル分離の構造的保証 (Task 16)**: キャッシュ書き込み関数が履歴 source を、履歴インポートがライブ source を**それぞれ拒否する**こと / prune が `ohlcv_history` に一切触れないこと / 人間 CLI がライブ source を fail closed で拒否すること
- **prune の収束性**: 日次投入量が 1 回のバッチ上限を超える構成でも、**`ohlcv_cache`** の行数が保持窓ぶんに収束することをテストで示す (D2)
- **実行位置 (D3')**: **通知送信が scheduler スレッドから呼ばれないこと**を回帰テストで固定する (呼ばれたら fail するシームを置く)。**「`core_lock` 内か」ではなく「どのスレッドか」で検査する** — lock を外しても同一スレッドの同期実行なら次の tick が止まり、しかも watchdog はスレッド生存を見るので検出できない (3 周目 C1)。**git サブプロセスについての同等の検査は D6 と一緒にプラン 10 の受入条件へ移す** (本プランには git を実行する経路が無い)
- **スキーマ制約**: `trade_intents` の `gate_result` と `reject_category` の対応が **CHECK 制約**で強制されていること (不正な組み合わせの INSERT/UPDATE が DB 層で落ちる)。**かつ `action IS NULL` の移行前既存行を含む DB で migration が成功すること**

## 4. 変えないもの

- 決定論的コアの判定ロジック / drawdown kill switch の経路
- `captured_at` を刻む位置・`snapshot_max_age_sec` の既定 10.0・commit-core の鮮度再検証 (spec ①)
- `MissionResult.status` の 4 値・送信前のトークン見積もり (**入れない**)・`n_ctx` のキャッシュ (spec ②)
- `readonly` の silent-skip (裁定書 F-5 / CR-4 — RPC 面を拡大しない)・`DERIVE_ONLY_INTERVALS` の設計・**先頭バケットの欠損許容** (spec ③)
- **`ohlcv_history` の中身** — prune の対象に決して入れない (D2。分割後は構造的に到達しない)
- Landlock の `read_write_paths` (`plugins/` の追加はプラン 10。書き手が居ないうちに権限を開けない)

## 5. プラン 10 への申し送り

- **`plugins/` 入れ子 git と承認有効化の不変条件 (旧 Task 14) を実施する** — **設計は本書 §1 D6 に完成形で残してある**。codex 敵対レビュー **3〜6 周ぶんの指摘をすべて反映済み** (専用 index による TOCTOU 閉塞 / unborn HEAD 分岐 / `update-ref` の CAS / `symbolic-ref` の終了コード区別 / commit identity の供給契約 / 起動時 reconcile の位置 / 再試行の 3 契機)。**プラン 10 の設計時に改めて再収束させること** (D6 単独では未収束判定を受けていないが、7 周を通じて最も指摘の多い箇所だった)
- **D6 の受入条件をプラン 10 側で持つ**: **git サブプロセス実行が scheduler スレッドから呼ばれないこと**の回帰テスト (プラン 9 の受入条件から移送した)
- **`plugins/` を Landlock `read_write_paths` に追加する** (改善 worker が plugin を書くため。**D6 と同じ契機**で必要になる)
- **improve worker 内で `uv run pytest` を回すかの裁定** — EXECUTE 権と一時書き込み先が要り Landlock 設計に跳ね返るため、**プラン 10 の設計段階で確定させる** (task に埋めない)

- **⚠ D3 の前提を覆す裁定 (ユーザー 2026-08-11)**: プラン 7 の D3 は「**改善ループに汎用シェル/コマンド実行ツールを与えない**」を前提にしていた (与えると `afx plugin bless` で自己承認でき人間承認を迂回できる、という懸念)。**この前提は放棄する。**
  - **理由**: 改善ループは **plugin の pytest を回すことが職務**であり、その時点で**任意コード実行が成立する** (設計書 §6 が既に「plugin submit 時の pytest は sandbox 外実行。AST 防御は原理的に不完全」と認めている)。シェルだけを絞っても実効的な差が無い。**ツール層で絞るのは形式的な防御にすぎない**
  - **代替となる不変条件 (これが D3 の役割を引き継ぐ)**: **改善 worker から `data/` と DB パスに到達できないこと**。`bless` は `approval_requests` への書き込みを要するので、**到達できなければ実行できない** (「到達不能 = 実行不能」§4.6 の意味論)。プラン 8 が既に持っている防御 (DB パス非提供 + Landlock) がそのまま担う
  - **したがってプラン 10 の受入条件で検証すべきは「シェルが無いこと」ではなく「`data/` へ到達できないこと」**である。**`plugins/` を `read_write_paths` に足す際に `data/` を巻き込まないこと**が最重要の検査点になる
  - **残余リスク (明示)**: 改善 worker は web 取得のためネットワークを要する。任意コード実行 + ネットワークが揃うため、**plugin ソースの信頼性に対する脅威モデルは従来どおり「submit/bless は信頼できるソースのみ」**のまま (設計書 §6 のセキュリティ残余と同じ)
- **CodexRunner を第 4 の AgentRunner として加える (ユーザー裁定 2026-08-11)**。`ClaudeRunner` と**両方を導入し、config で切り替えられる**ようにする (既存の「LocalRunner / ClaudeRunner を config で切替」と同じ枠組みに乗せる)。**設計書 §4 の改訂はプラン 10 の設計時に行う** (本プランのスコープ外)。用途は**改善ループ (plugin 実装・news ソース取得)**。取引 loop は既定どおり `local` — `RunnerChoice.backend` は `^(local|claude|codex)$` へ拡張し、`config/settings.yaml.example` と同期する。

  **実装順序 (ユーザー承認 2026-08-11 — この順を守ること)**:

  | # | やること | なぜこの位置か |
  |---|---|---|
  | 1 | **codex の実現可能性実測** — worker 隔離下 (Landlock + DB パス非提供 + 空 cwd) で **1 ターン完走するか**だけを見る | **基盤を作る前**に置く。improve profile は `landlock.is_available()` が偽なら起動拒否する設計であり、**codex は独自サンドボックス機構を持つ**ので衝突すると成立しない。後で判明すると基盤の作り直しになる。**動かなければ CodexRunner を落としプラン 10 のスコープを縮める** |
  | 2 | **共通基盤 (契約層) の設計** | **両方の実測結果を見てから**決める。ClaudeRunner だけ見て作ると claude 都合に偏る |
  | 3 | **ClaudeRunner 実装** — 動作基盤の確保 | 完動を実測済みでリスクが低い |
  | 4 | **CodexRunner 実装** — 契約を揃える | 機構は codex の強みを活かす (下記) |
  | 5 | **両者で同一の不変条件が成立することを検証** | 機構によらず「`data/` に到達できない」を確認する |

  **「同様な動作」の範囲を取り違えないこと。契約は揃えるが、防御機構は揃えない**:
  - **揃えるもの (契約)**: `MissionResult` の 4 終端 status / spec ② の `reason` 契約 (安全化済み・単一行・上限内・外部応答の本文を生で入れない) / `output_schema` の扱い / `max_turns`・`timeout_sec` の意味論
  - **揃えないもの (機構)**: claude は `allowed_tools` で絞り **Landlock が唯一の防御線**。codex は `Sandbox.workspace-write` + `cwd=plugins/` で **SDK 層でも絞れる** (Landlock は二重防御)。**「codex を claude と同様に」動かすと codex の強みを捨てることになる**
  - **揃えるべきは不変条件であって手段ではない**。両者に共通して成立させるのは ①**改善 worker から `data/` と DB パスに到達できない** ②従量課金経路が無い ③ユーザー個人の設定を継承しない — の 3 つで、実現手段は SDK ごとに違ってよい
  - **公式 Python SDK が存在する (2026-08-11 実測)**: **`openai-codex`** (PyPI 0.144.4、`openai/codex` リポジトリの `sdk/python`、`requires_python >=3.10`)。依存は `pydantic>=2.12` と **`openai-codex-cli-bin==0.144.4`** — **claude-agent-sdk と同じく CLI をラップする構造**であり、実体は CLI サブプロセスである
  - **紛らわしい別パッケージに注意**: PyPI の **`openai-codex-sdk`** (0.1.11) は `author: OpenAI` を名乗るが **repository も homepage も無く**、版体系も公式 (0.14x) と一致しない。**使わないこと**。TypeScript 版の公式は `@openai/codex-sdk` (0.147.0, Apache-2.0)
  - **従量課金の回避は成立する見込み**: `codex login` の ChatGPT サブスクリプション認証を使えば、CLAUDE.md の絶対制約 (従量課金 API 不可) と両立する。ただし**未実測**
  - **プラン 10 で実測すべきこと**: ①worker 隔離下 (DB パス非提供・空 cwd・Landlock) で `codex exec` が完走するか ②`openai-codex-cli-bin` がバイナリを同梱するため **Landlock の allowlist に効く** — 実行可能パスの扱い ③ClaudeRunner と同様に**ユーザー個人の設定 (`~/.codex/`・MCP・plugin) を継承しないか** (claude-agent-sdk は既定で継承した。同型の問題を疑うこと) ④従量課金経路の遮断をどう構造的に強制するか (Claude 側は子 env から `ANTHROPIC_API_KEY` を除去する形にした)
  - ローカル CLI は `codex-cli 0.147.0`、Python SDK は 0.144.4 で**版が少しずれている**
  - **API 形状は AgentRunner と相性が良い (実測)**: `Codex().thread_start(...)` → `Thread.run(input, *, output_schema=..., model=..., effort=..., sandbox=..., approval_mode=..., cwd=...)` → `TurnResult`。**`output_schema` がネイティブにある**ので `Mission.output_schema` をそのまま渡せ、**JSON 修復リトライが不要になる可能性がある** (LocalRunner の自前 tool-calling loop との差)。`CodexConfig(codex_bin=..., env=..., cwd=...)` で**バイナリパスと環境変数を明示できる**
  - **サブスク認証は確認済み (実測)**: `Codex().account()` が ChatGPT Plus アカウントを返した。`login_chatgpt` / `login_api_key` が SDK に分かれており、**API キー経路を使わない構成が取れる**
  - **同梱バイナリは 336 MB** で、`bin/codex` のほか **`bwrap` (サンドボックス)・`zsh`・`rg`** を含む。Landlock の allowlist と依存の重さの両方に効く。`codex_bin` でシステム側 CLI を指せば回避できるが、「clone + init で動く」原則から外れる — **どちらを採るかはプラン 10 で裁定**

- **⚠ 両 SDK のサンドボックス特性は正反対だった (2026-08-11 同一条件で実測)**。空 cwd を与え、①cwd への書き込み ②`/etc/passwd` 読み ③`ls ~` を試した結果:

  | | **codex** (`Sandbox.read_only` + `deny_all`) | **claude-agent-sdk** (`allowed_tools=["Bash"]`) |
  |---|---|---|
  | cwd への書き込み | **blocked** (読み取り専用 FS) | **成功** (`probe.txt` が実際に作られた) |
  | cwd 外の読み取り | 通る | 通る |
  | SDK 層のサンドボックス | **実効** (独自機構。`bwrap` は経由していなかった) | **無し** (素の bash) |
  | シェルの起動形 | `bash -c "cmd"` (使い捨て) | **`bash -c -l`** (ログインシェル) |
  | ツールの無効化 | **できない** (シェルは常にある) | `allowed_tools=[]` で**ゼロにできる** |

  - **claude は「ツールの有無」を制御でき、codex は「権限」を制御できる。** 改善ループはシェルを必要とする (pytest) ので、**効くのは後者の軸**である。`Sandbox.workspace-write` + `cwd=plugins/` にすれば「**plugins/ にだけ書ける**」が SDK 層で宣言的に成立し、Landlock がその上の二重防御になる。claude 側は Bash/Write を許可した時点で **Landlock だけが唯一の防御線**になる
  - **claude 側の追加要件 (新規発見)**: `setting_sources=[]` / `strict_mcp_config=True` / `plugins=[]` を指定しても、**Bash ツールは `bash -c -l` (ログインシェル) を起動し `~/.claude/shell-snapshots/` を読み込む**。既存の隔離オプションは**シェル環境までは覆わない**。改善 worker がユーザーの shell rc 環境を継承するため、**env を明示的に組み立てる等の対処をプラン 10 の要件に含めること**
  - **プロセス階層が深くなる (実測)**: `worker → codex/claude CLI → (code-mode-host) → bash → 実コマンド`。プラン 8 の `WorkerSettings` (resource limit) と**停止シーケンス (`start_new_session=True` + `killpg`) の再検証が要る** — CLI 自身が子を作るため二重になる

- **ClaudeRunner の実測結果 (2026-08-11、本設計時に確認済み)**:
  - `claude-agent-sdk` 0.2.134 の依存は `anyio` / `mcp` / `sniffio` のみで **`anthropic` SDK を含まない**。`shutil.which("claude")` で **CLI をサブプロセス起動する**構造であり、**サブスク認証で完動する** (`ANTHROPIC_API_KEY` 未設定で応答を得た)。設計書 §4「Claude Agent SDK」と CLAUDE.md「`claude -p`」は**矛盾しない** — SDK は CLI のラッパである
  - **既定ではユーザーの Claude Code 設定 (hooks / plugins / MCP / settings) を継承する** — 素の実行で hook が 4 本発火した。トレードシステムがユーザー個人の設定に左右されるのは受け入れられない。**`setting_sources=[]` / `strict_mcp_config=True` / `plugins=[]` を構造的に強制する** (実測で hook 発火 4 → 0)
  - **従量課金の遮断は「子 env から `ANTHROPIC_API_KEY` を除去すること」で成立する** — SDK は env に API キーがあればそれを使う。回帰テストで固定すること
  - 隔離しても **1 回あたり約 18.4k トークンの cache 作成オーバーヘッド**が乗る (CLI 自身のシステムプロンプト + tool 定義。オプションでは削れない)。LocalRunner との使い分けの判断材料にする
- spec ② が要求する **ClaudeRunner の `reason` 契約テスト**を blocking チェックリストに入れる (spec ② §4.3 — 「runner は failed / timeout / max_turns を返すとき、可能な限り安全化済みの `reason` を設定する。外部応答の本文を生で入れない」)
- **遮断 8 項目の統合回帰は registry task の直後に red で書き始める** (最後の E2E に置かない)。項目 4 は「`data/` 不可視」に加えて「**書き込み可能パスが `plugins/` と `reports/` に閉じている**」が成立条件に入った

## 6. 起票 (本プランでは直さない)

- `missions.failure_reason` 列の要否 (spec ② §4.6 — 永続監査が必要になったら)
- `WorkerRunner` の親側失敗 (startup timeout / worker timeout / protocol error / EOF) の理由付け (spec ② §4.3 が明示的に範囲外とした)
- `stream=true` 導入時の SSE error event 設計 (spec ② §4.7)
- **`ohlcv_cache` を別 DB ファイルへ物理分離するか** (D2 — `VACUUM` は SQLite では database 単位なので、**同一 DB 内のテーブル分割では履歴を巻き込む**。キャッシュだけを vacuum したければ物理分離が要る。あわせて `run_in_sample(*, history_conn=...)` が既に接続を別引数で受けている構造とも噛み合う)
- **キャッシュ → 履歴の「昇格」経路** (D2 — Dukascopy が提供しないペアで蓄積したキャッシュをバックテストしたくなった場合。現時点では YAGNI)
- ヘッジ併存時の「close 後 net exposure 悪化」検出 (設計書 §5 — ヘッジ運用を本格化する場合)

## 7. レビュー方針

**設計レビューは codex 単独の反復**とする。実績: ohlcv spec で 6 → 3+1 → 2 と収束、spec 小改訂束で C1+I4+M2 → I3+M1 → M1 と 3 周で収束。**プラン規約の段構成 (段 0 変異スイープ → codex+ローカル → `/code-review high` → sonnet) は実装レビュー用**であり、設計レビューにそのまま適用しない。

**「修正ラウンドは新しい欠陥を持ち込む」は spec で 3 度実証されている。** 1 周で収束したと判断しないこと。

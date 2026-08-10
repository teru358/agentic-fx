# 設計: ohlcv キャッシュ・フォールバックの読み込み窓と配線 pin

**位置づけ**: プラン 9 の task として実装する。本書はその spec。
**日付**: 2026-08-10 (**改訂 2** — codex 敵対レビュー Important 6 件を全件反映。
改訂 1 は中核命題 3 つが誤っていたため大幅に書き直した)
**出自**: C3 調査 (`.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/c3-investigation.md`) の副産物。
C3 自体は「エスカレーション不要・実質解消済み」で閉じた。
**レビュー記録**: `.superpowers/sdd/2026-08-10-ohlcv-cache-fallback/spec-review-codex.md`

---

## 0. 改訂 1 の誤りと訂正 (残す — 同じ誤りを繰り返さないため)

| 改訂 1 の主張 | 実際 | 出所 |
|---|---|---|
| 「1h は本番で行として保存されない」 | **偽**。`NATIVE_INTERVALS["yfinance"]` に `1h` が含まれ、`DERIVE_ONLY_INTERVALS` は `{"4h","1d"}` のみ。**ライブ 1h が検証を通れば 1h 行は保存される**。既存テストも「4h 導出後に 1h 100 行が保存される」と assert している | codex I5、指揮者が再確認 |
| 「`lookback_days` で切ればライブと同じ期間になる」 | **偽**。ライブの導出経路は `lookback_days * ratio` で取得する (`price_provider.py:325`)。単純に切ると**キャッシュがライブより短くなる** | codex I3、指揮者が再確認 |
| 「`since` を floor すれば先頭足は完全になる」 | **不十分**。floor が防ぐのは**クエリ切断由来の部分化だけ**。境界時刻の base 行が欠けていれば、後続だけで足が作られ「正常な足」として残る | codex I1 |
| 「長期間だと週末 gap で `validate_bars` が落ちうる」 | **偽**。gap 検査は**末尾 `max(24 本, 24 時間)` の窓だけ**で、しかも閉場分を控除する (`health.py:147-178`) | codex I4 |

**根本原因**: 改訂 1 は「1 回の観測」を構造的主張に格上げしていた。1h 行が増えなかったのは
その日の yfinance の 1h が gap 検証に落ちてキャッシュ経路に流れていたためで、環境依存だった。

---

## 1. 事実関係 (改訂 2 の土台)

### 1.1 ライブ経路の窓の取り方 (`price_provider.py`)

| 経路 | 条件 | 取得期間 |
|---|---|---|
| native 直接 | `interval in NATIVE_INTERVALS[source]` かつ `interval not in DERIVE_ONLY_INTERVALS` | **`lookback_days`** |
| 導出 | それ以外 | **`lookback_days × ratio`**、`base = _finest_native_base(source, interval)`、`ratio = INTERVAL_MIN[interval] / INTERVAL_MIN[base]` |

導出で比を掛ける理由はコードが明記している:

> `lookback_days` は「期間」なので比を掛けなくても期間自体は満たせるが、粗い足に畳むと
> 本数が 1/比 になり、指標計算に必要な長さを満たさない (5 日分の 4h は 30 本しかない)。

### 1.2 キャッシュ経路 (`_cached_bars`, `:192-246`)

- 候補は `[interval, *_base_candidates(interval)]` (`DERIVE_ONLY` を除く)、
  **source を外側・interval 候補を内側**にループ
- `ohlcv.load_bars(...)` を **`since`/`until` なしで呼ぶ** (`:228`) → **全件を読む**
- `src == interval` なら直読して返す。そうでなければ `_resample` で導出する

### 1.3 蓄積

`store/ohlcv.py` に `DELETE` は無く、保持ポリシーも無い。1m は 1 ペアあたり
1 日 1,440 行ずつ増え続ける。

### 1.4 実測 — 蓄積量に対するフォールバックのコスト

ライブ bar ソースを全て落とし、本番の readonly provider で `get_bars(pair,"1h")`:

| 1m 蓄積 | 1m 行数 | 導出 1h 本数 | 所要 | ピークメモリ |
|---|---|---|---|---|
| 1 日 | 1,440 | 25 | 0.03 秒 | 0.9 MB |
| 7 日 | 10,080 | 169 | 0.19 秒 | 6.1 MB |
| 30 日 | 43,200 | 721 | 0.83 秒 | 26.7 MB |
| **180 日** | 259,200 | 4,321 | **5.76 秒** | **161.9 MB** |

**計測の限界**: 連続合成バーで測った。実データでの再測は §6。

### 1.5 このコストは障害時だけではない

実環境の測定で、**ライブ健全時にも `get_bars(pair,"1h")` が `origin='cache'` で返った**
(その日のライブ 1h が gap 検証に落ちたため)。フォールバック経路は例外時のみでなく
**平常運転でも通りうる**。

---

## 2. 解く問題

**`_cached_bars` は要求と無関係に全件を読み、resample し、`validate_bars` に掛ける。**
蓄積は無制限なので、コストは稼働日数に比例して増え続ける。

これは性能の問題である前に**契約の欠落**である — ライブ経路には
§1.1 の明確な窓の規則があるのに、キャッシュ経路には窓が無い。

---

## 3. 設計

### 3.1 キャッシュの読み込み窓は「ライブ経路が同じ要求に対して取る期間」に揃える

**改訂 1 の「`lookback_days` で切る」も、「ライブの ratio をそのまま適用する」も誤りである。**

- 前者はライブの導出経路 (`lookback_days × ratio`) より短くなる
- 後者は**キャッシュ側の base が細かいときに破綻する** — キャッシュで 1h を 1m から作る場合、
  `ratio = 60/1 = 60` となり `lookback_days=5` で **300 日**になる。これは解こうとしている
  問題そのものである。ライブに「1h を 1m から導出する」経路は存在しない (yfinance の 1h は
  native) ので、ライブの比をキャッシュの base に流用する根拠が無い

**正しい規則**: 窓は**要求 `(source, interval, lookback_days)` から決まる**のであって、
キャッシュ側でどの base を使うかには依存しない。

```
live_window_days(source, interval, lookback_days):
    if interval in NATIVE_INTERVALS[source] and interval not in DERIVE_ONLY_INTERVALS:
        return lookback_days
    base  = _finest_native_base(source, interval)
    ratio = INTERVAL_MIN[interval] / INTERVAL_MIN[base]
    return lookback_days * ratio
```

`_cached_bars` は source を外側にループしており `name` を持っているので、**source ごとに**
この値を求められる。既存ヘルパ (`_finest_native_base` / `INTERVAL_MIN` / `NATIVE_INTERVALS`)
をそのまま使う。

境界の効果 (yfinance・`lookback_days=5`):

| 要求 | live_window_days | 1m から読む行数 |
|---|---|---|
| `1h` (native) | 5 | 7,200 |
| `4h` (base=1h, ratio=4) | 20 | 28,800 |

**DB に 180 日あっても読むのは上記まで**に収まる。

### 3.2 二つの窓を名前で分ける (codex I2)

`_cached_bars` の候補には**直読 (`src == interval`) と導出 (`src != interval`) の 2 種**がある。
一文で扱うと実装者が floor の基準を取り違える。**別々に定義する**:

| 用途 | 値 | floor |
|---|---|---|
| `window_start` | `now - live_window_days` | **しない** |
| `derive_since` | `window_start` | **要求 interval の境界へ floor する** (`BAR_ANCHOR` 格子) |

- **直読候補** (`src == interval`) は `window_start` をそのまま渡す。resample しないので
  部分バケットの問題が無く、floor すると要求より広い期間を返してしまう
- **導出候補** (`src != interval`) は `derive_since` を渡す。floor の基準は
  **常に要求 interval** であって base の interval ではない (1h を作るなら 1h 境界)

**`1d` の floor は別実装が要る。** `bars.py:85-101` のとおり pandas の `origin` は
Tick 系にしか効かず、日足は UTC 00:00 前提で成立している。共通 floor ヘルパを作るなら
`1d` を検査対象に含めること。

### 3.3 floor が保証すること・しないこと (codex I1)

**floor が防ぐのは「クエリ切断が base 系列をバケット途中で切ること」だけである。**

floor 時刻の base 行が未保存・欠損・取得漏れであれば、同じバケットの後続行だけで
足が作られ、**open が真の始値でない足が「正常な足」として残る**。`resample` は
バケットの期待本数を検査しない (`bars.py:91-102`)。`validate_bars` の gap 検査は
末尾窓のみを見るので、**先頭の欠損は原理的に検出できない**。

**これは現行挙動と同じであり、本 task で改善しない。** ライブ経路も同じ性質を持つので
パリティが保たれる。受入条件・テストの文言も「クエリ切断由来の部分化を防ぐ」に揃える。

### 3.4 [①] 本番の連鎖を pin する (codex I5 で狙いを訂正)

**改訂 1 は「`1m → 1h` が本番唯一の連鎖」と書いたが誤り**である。1h 行はライブ 1h が
検証を通れば保存されるので、キャッシュ経路はまず **1h の直読**に当たる。

**pin の狙いは「唯一の本番経路」ではなく、「1h キャッシュが無い/古いときに成立すべき
最終フォールバック」である。** これが死ぬと、症状はライブ全滅かつ 1h キャッシュ不在の
ときにしか出ない。

**pin の必須要素 (codex I6)**:

1. **`build_app` の実 factory** を使う (ローカル組立てでは配線の証明にならない)
2. **対象 pair の open または pending order を seed する** — `scheduler.tick` は無条件に
   `bars_fn` を呼ばない。時価評価・約定・exit の経路で呼ばれる (`scheduler.py:301-308`,
   `:357-379`)。**空の初期 DB で `tick()` を呼んでも 1m 取得は保証されない**
3. ライブ成功フェーズでは **1m だけを返す**ようにし、書かれた source を確認する
4. **同 source の 1h 行が空であること**を事前に assert する — さもないと 1h 直読で green に
   なり、狙った `1m → 1h` に到達しない
5. 全 live fetcher を失敗させる
6. 同じ `app` の provider で `get_bars(pair,"1h")` を呼び、
   `bars_origin` が `cache(1m→1h derived)` であることを確認する
7. 非同期 Mission は観測対象から外し、`app.close()` まで行う

`_storage_source` により source ID は永続化名に変換される (`mt5` → `mt5-live`)。
**テスト DB と設定を厳密に制御しないと別 source を拾って green になる。**

### 3.5 [③] コメントに機構を書く

`service.py:518-523` と `mission_registry.py:54` は「親の scheduler tick が継続的に
cache を温めるため実害は限定的」とだけ書いている。**tick が温めるのは 1m だけ**であり、
1h は「ライブ 1h が通れば保存される / 通らなければ 1m から導出して復元される」という
二段構えである。この機構を書き足す (改訂 1 の指揮者が実際に誤読した)。

---

## 4. 変えないもの

- **`readonly` の silent-skip** (裁定書 F-5 / CR-4 — RPC 面を拡大しない)
- `DERIVE_ONLY_INTERVALS` の設計 (4h/1d の導出足は保存しない)
- `latest_1m_bar` の `lookback_days=1`
- 末尾の形成中バケットを残す挙動
- **先頭バケットの欠損許容** (§3.3 — ライブ経路とのパリティ)
- **保持ポリシー (prune)** — §7 で起票
- 決定論的コア

---

## 5. テストと変異 (検査点ごとに独立させる)

| # | 独立テスト | 殺す変異 |
|---|---|---|
| 1 | `_cached_bars` が `since` 付きで `load_bars` を呼ぶ | `since` を渡さない |
| 2 | **native 要求 (1h/yfinance) の窓が `lookback_days`** である | ratio を掛ける |
| 3 | **導出要求 (4h/yfinance) の窓が `lookback_days × ratio`** である | ratio を落とす |
| 4 | **窓は「キャッシュ側の base」に依存しない** — 1m から 1h を作っても窓は 5 日 | キャッシュ base の比で計算する (= 300 日になる退行) |
| 5 | 窓より古い行が結果に**入らない** | 絞りを外す |
| 6 | **直読候補には floor しない** (要求より広く返さない) | 直読にも floor する |
| 7 | **導出候補は要求 interval の境界へ floor される** | floor を外す |
| 8 | **クエリ切断由来の部分バケットが先頭に出ない** (7 とは別の観測点) | floor を外す |
| 9 | `1d` の floor が UTC 00:00 になる | Tick 系と同じ実装を流用する |
| 10 | 末尾の形成中バケットは従来どおり残る | 末尾も落とす |
| 11 | **本番連鎖**: build_app → order seed → tick が 1m を書く → 1h 行が空であることを確認 → ライブ全滅 → `bars_origin == "cache(1m→1h derived)"` | `_base_candidates` から 1m を外す |
| 12 | 11 の前提条件そのもの: tick が `bars_fn` を呼んでいる | `bars_fn` の配線を切る |
| 13 | 蓄積が大きくても読み込み件数が窓ぶんに収まる | 絞りを外す |
| 14 | **窓の縮小で受理集合が変わることの明示** (§6) — 窓外の古い異常行があってもフォールバックが成立する | 全期間検証に戻す |

**変異は 1 つずつ独立に当て、殺した固有のテスト名を記録する。**
1 テストに複数 assert を並べない — 先に落ちる assert が後続を短絡させる
(プラン 8 Task 19/20 で 2 度実測した穴)。

---

## 6. 契約変更として明示すること (codex I4 — 改訂 1 の仮説は誤りだった)

改訂 1 は「長期間の全走査で週末 gap に掛かる」と書いたが**誤り**である。
`validate_bars` の gap 検査は**末尾 `max(24 本, 24 時間)` の窓だけ**を見て、
`_closed_minutes` で市場閉場分を控除する (`health.py:147-178`)。

**実際の契約変更はこちら**: `validate_bars` が**全期間を走査する検査**
(未来時刻・OHLC の zero/NaN・連続 close の 10% spike、`health.py:154-181`) は、
窓を絞ると**窓外の古い行を見なくなる**。したがって:

- **これまでフォールバック全体を落としていた「古い異常行」が、落とさなくなる** —
  受理集合が広がる
- **これは意図した変更である**。返さないデータを検証する理由が無い
- 併せて `validate_bars` 自体の O(n) 走査コストも窓ぶんに減る

**実装時に実測すること**: 実データ (週末ギャップを含む) で、窓を絞った前後の
`validate_bars` の合否と所要時間。§1.4 は連続合成バーでの計測なので実データで取り直す。

---

## 7. 本 task では直さず起票するもの

**`ohlcv` の無制限増大。** §3.1 で読み込みコストは一定になるが、**DB のサイズ自体は
増え続ける** (1 ペア 1 日 1,440 行)。保持期間の要件・バックテスト履歴 (`history_conn`) との
関係を含む別の設計判断なので、本 task には混ぜない (ユーザー裁定)。

---

## 8. 受入条件

1. キャッシュ経路の読み込み窓が、**ライブ経路が同じ要求に対して取る期間と一致する**
2. 窓が**キャッシュ側の base に依存しない** (1m から 1h を作っても窓は変わらない)
3. **クエリ切断由来の部分バケットが先頭に出ない** (欠損由来の部分化は現行どおり許容)
4. DB の蓄積量に関係なく、フォールバックの読み込み量と所要が窓ぶんに収まる
5. **最終フォールバック `1m → 1h` が、`build_app` から tick の書き込みを経て通しで pin されている**
   (1h 行が空であることの事前確認を含む)
6. §6 の契約変更が実データで実測され、記録されている
7. 既存テストが 1 本も壊れない

# 設計: snapshot gather の deadline (実測① への対処)

> **昇格メモ (2026-08-11)**: 本書は `.superpowers/sdd/2026-08-04-phase2-8-worker-isolation/task20-gather-deadline-design.md` (git-ignored) からプラン 9 の入力 spec として昇格したもの。内容は同一。
>
> **⚠ 実装者は下の「確定仕様」だけを読めばよい。** 本書の**初稿本文 (§「解く問題」以降) は codex 敵対レビューで Critical 2 / Important 2 を受けて中核の論証が崩れており、そのままでは使えない** (特に「非損失性が恒真」「CLOSE には入れない」「最悪 = 予算 + 1 脚」は**誤り**)。初稿と「レビュー結果と改稿方針」は**経緯の履歴**として残してある。

---

## 確定仕様 (改稿後 — これが正)

**解く問題**: 一次ソースが**ハング**すると `gather_open_snapshot` が **20.5 秒**かかる (10 秒/ネットワーク脚 × 2 脚、通貨が増えれば線形)。gather 自体は成功するが直後の commit-core が `age_sec > 10.0` で拒否するため、**「健全に見える `gate_rejected` を記録しながら一度も取引しないシステム」**になる。ダウン (即 refuse、0.28 秒) とハング (timeout を払う) を区別しないと結論を誤る。

| # | 確定事項 | 根拠 / 出典 |
|---|---|---|
| 1 | 予算は**新しい config キーを作らず** `snapshot_max_age_sec` (既定 10.0) を流用する | 初稿 §1 (この部分は維持) |
| 2 | 比較は **`>`** に揃える (`>=` にしない) | 改稿 C1 — commit-core は `age_sec > max` (executor.py:560/807) で**等号は受理側**。`>=` だと `age_sec == 10.0` で成功しえたものを落とす |
| 3 | 経過は **`time.monotonic()`** で測る (テスト用に `monotonic_fn` を注入可能にする)。`self.clock` は **`captured_at` にのみ**使う | 改稿 C1 — `FixedClock`/`ReplayClock` は進まないので `clock` 基準の deadline は**永久に到来しない**。`SystemClock` も壁時計で NTP 補正により後退しうる。**`Clock` は業務上の論理時刻の抽象であって処理時間の抽象ではない** |
| 4 | 効果の主張は限定する — **「完全な非損失」は撤回**。正しくは「壁時計が後退せず monotonic と十分一致する条件下で、**既に stale が確定した仕事の後続 I/O を抑止する**」 | 改稿 C1 |
| 5 | 検査点は外部脚の**あいだ**に加え、**`PriceProvider.to_account_rate` の `for spec in legs` の各脚の前**まで deadline を伝播させる (checker コールバック or 絶対 deadline を渡す。通常の呼び出し元では no-op) | 改稿 C2 — `cycle_rate(ccy)` **1 回の内部で最大 2 本のネットワーク脚**が走る (price_provider.py:440-457 の USD クロス)。gather 直下の検査だけでは **EUR→JPY のクロス 1 通貨で 10 秒 × 2 を払い**、初稿が解こうとした 20.5 秒がそのまま残る |
| 6 | 例外メッセージの脚名は **`rate:EUR:EURUSD` のように実シンボルまで**含める | 改稿 C2 |
| 7 | **OPEN と CLOSE の両方に入れる** (`gather_open_snapshot` / `gather_close_snapshot`) | 改稿 I1 — 初稿の「CLOSE は待てば close できる」は**事実として誤り**。`close_from_snapshot` も**同じ予算で stale を拒否する** (executor.py:806-810)。待って得られるのは古い snapshot と拒否であって close ではない。現行仕様下では**早く失敗を確定して次の再試行機会に戻る方が資金保護に有利** |
| 8 | 失敗の出口は **`DataUnhealthy`** 送出。**新しい配管は作らない** — `trade_loop.py` の commit-pre が既に `except Exception` で捕捉し、commit-core で fail closed の執行失敗として `trade_intents` に理由を残す。メッセージに**経過秒数と打ち切った脚**を入れ、`"execution snapshot is stale"` と区別できる文言にする | 初稿 §3 (維持) |
| 9 | **限界を明記する**: スレッドを使わないので**実行中の 1 脚は中断できない**。deadline が保証するのは「**次の**脚に進まないこと」 | 初稿 §2 (維持。ただし「最悪 = 予算 + 1 脚」の見積もりは #5 の伝播を入れて初めて成立する) |

**変えないもの**: `captured_at` を刻む位置 (gather の先頭のまま — 「fetch 完了後に刻む」案はユーザー裁定で不採用。gather の遅さ自体を見逃し古い価格で発注しうる) / `snapshot_max_age_sec` の既定値 10.0 / commit-core の鮮度再検証ロジック / 決定論的コアの 3 ファイル。

### テスト (TDD — 実装前に red を観測する)

**検査点ごとのテーブル駆動**にし、**全 stub の呼び出し列**を assert する (`calls == ["quote:intent", "spec:intent", ...]`)。境界は**直前・等号・直後の 3 点**。

1. **非損失性のピン**: gather 所要が予算以内なら deadline は発火しない
2. **打ち切りのピン (検査点ごと)**: 予算を使い切ったら**次の脚の関数が呼ばれない**ことを**呼び出し回数で**観測する (「例外が出た」だけでは打ち切りを pin できない)
3. **論理時計からの独立**: `FixedClock` を業務時計にしたまま monotonic fake を進めて deadline が発火することを pin する
4. **出口のピン**: `DataUnhealthy` が commit-pre で捕捉され `trade_intents` に理由が残り、`"stale"` とは異なる文字列であること
5. **CLOSE 対称のピン**: `gather_close_snapshot` **でも**次脚を呼ばないこと (初稿の「非対称」テストは**反転**させる)
6. **脚伝播のピン**: `to_account_rate` の 2 脚目に進まないこと

> **初稿のテスト計画は先頭の検査点しか pin しない** (改稿 I2) — 「1 脚目で予算超過 → 2 脚目を呼ばない」1 本では、**2 番目以降の検査点を全部削除しても green** になる。

### 変異 (実装後)

- **各検査点を 1 つずつ削除**する (改稿 I2 — まとめて 1 変異にしない)
- 予算を `snapshot_max_age_sec` から独立の大きな定数に差し替える
- 経過測定を `monotonic` から `clock` に戻す (→ 論理時計テストが red)
- 比較を `>` から `>=` に変える (→ 等号境界テストが red)
- `DataUnhealthy` を送出せず `None` を返す
- CLOSE 側の deadline を削除する
- `to_account_rate` への伝播を削除する

### 改稿後のスコープ

| 変更対象 | 初稿 | 確定 |
|---|---|---|
| `Executor.gather_open_snapshot` | ✓ | ✓ |
| `Executor.gather_close_snapshot` | — | **✓** |
| `Executor` の monotonic 注入点 | — | **✓** |
| `Executor.cycle_rate_fn` の deadline 伝播 | — | **✓** |
| `PriceProvider.to_account_rate` | — | **✓** (決定論的コア外) |
| テスト | 4 本 | **検査点ごとのテーブル駆動 (10 観点)** |

---

---

# 以下は履歴 (初稿本文 — 誤りを含む。実装の根拠にしないこと)

**ユーザー裁定 (2026-08-10)**: 実測① の対処は「gather に deadline を入れる」。
本書はその設計。**実装前にレビューと了承を得ること** (CLAUDE.md 規約)。

## 解く問題 (実測済み)

一次ソースが**ハング**すると `gather_open_snapshot` が **20.5 秒**かかる
(10 秒/ネットワーク脚 × 2 脚。通貨が増えれば線形に伸びる)。gather 自体は
**成功**して snapshot を返すが、直後の commit-core が `age_sec > 10.0` で拒否する。
結果は「健全に見える `gate_rejected` を記録しながら一度も取引しないシステム」。
`gate_rejected` には集計も通知も無いので、ログを読まない限り誰にも上がらない。

## 設計

### 1. 予算は**新しい config キーを作らず** `snapshot_max_age_sec` を流用する

**根拠 — この deadline は「成功しえたものを一つも落とさない」**:

`captured_at` は gather の**先頭**で刻まれる。commit-core の
`age_sec = now - captured_at` は必ず **gather 所要時間以上**である。したがって

> gather 所要 > `snapshot_max_age_sec` ⟹ commit-core は必ず stale で拒否する

が恒真に成立する。**deadline を `snapshot_max_age_sec` ちょうどに置けば、
落とすのは「どのみち拒否が確定していたもの」だけ**になる。振る舞いの損失はゼロで、
得るのは ①無駄な外部 I/O の打ち切り ②`gate_rejected` (原因不明に見える) ではなく
`DataUnhealthy` (データ不健全として上がる) という区別。

**分数 (例: 予算の 50%) を採らない理由**: 0.6×予算 の gather は、lock 待ちが
短ければ成功しえた。分数にした瞬間「成功しえたものを落とす」ようになり、
上記の非損失性が壊れる。**ちょうど 1 倍だけが非損失。**

### 2. 検査点は「外部脚の**あいだ**」

```
now = clock.now()            ← captured_at (現状どおり、変えない)
quote = quote_fn(pair)       ← 脚
  [検査]
spec = spec_fn(pair)         ← 脚
  [検査]
cycle_rate = cycle_rate_fn(now)
for pair in exposure_pairs:
    spec_fn(pair)            ← 脚
      [検査]
rates = {ccy: cycle_rate(ccy) ...}   ← 通貨ごとに脚
      [検査 (通貨ごと)]
```

**限界を明記する**: スレッドを使わないので**実行中の 1 脚は中断できない**。
deadline が保証するのは「**次の**脚に進まないこと」であり、最悪所要は
`予算 + 実行中だった 1 脚の timeout` (quote/rate なら 10 秒)。
実測の 20.5 秒ケースでは、1 脚目 (10 秒) の直後の検査で打ち切るので
**20.5 秒 → 約 10 秒**になる。**「速くなる」ではなく「二重に払わなくなる」**。

### 3. 失敗の出口は既存配線に乗る (新しい配管は不要)

`DataUnhealthy` を送出する。`trade_loop.py:246` の commit-pre は既に

```python
open_snapshot = self.executor.gather_open_snapshot(...)
except Exception as e:
    snapshot_error = e
```

で捕捉し、commit-core で **fail closed の執行失敗**として扱い `trade_intents` に
理由を残す。**新規の例外処理は要らない。** メッセージに経過秒数と打ち切った脚を
入れ、`"execution snapshot is stale"` と区別できるようにする。

### 4. CLOSE 側 (`gather_close_snapshot`) には**入れない**

OPEN の拒否は「取引しない」で済むが、**CLOSE の拒否は資金保護の失敗**である。
遅い close は悪いが、close できない方が悪い。非対称は意図的なものとしてコメントに
残す。**この判断はレビューで特に見てほしい点。**

## 変えないもの

- `captured_at` を刻む位置 (gather の先頭のまま)。「fetch 完了後に刻む」案は
  ユーザー裁定で採らなかった — gather の遅さ自体を見逃す (古い価格で発注しうる)
- `snapshot_max_age_sec` の既定値 10.0
- commit-core の鮮度再検証ロジック
- 決定論的コアの 3 ファイル (受入条件 7)

## テスト (TDD — 実装前に red を観測する)

1. **非損失性のピン**: gather 所要が予算以内なら deadline は発火しない
2. **打ち切りのピン**: 1 脚目で予算を使い切ったら、**2 脚目の `spec_fn` が
   呼ばれない**ことを assert する (呼び出し回数で観測 — 「例外が出た」だけでは
   打ち切りを pin できない)
3. **出口のピン**: `DataUnhealthy` が commit-pre で捕捉され、`trade_intents` に
   理由が残り、`"stale"` とは異なる文字列であること
4. **CLOSE 非対称のピン**: `gather_close_snapshot` は同条件でも打ち切らない

## 変異 (実装後)

- deadline 検査を削除 → テスト 2 が red
- 予算を `snapshot_max_age_sec` から独立の大きな定数に差し替え → テスト 2 が red
- `DataUnhealthy` を送出せず `None` を返す → テスト 3 が red
- CLOSE 側にも deadline を足す → テスト 4 が red

---

# レビュー結果と改稿方針 (2026-08-10)

codex 敵対レビュー: **Critical 2 / Important 2**。全件を指揮者が実コードで再確認し、
**全件採用**。詳細は `task20-design-review-codex.md`。

## 採用した指摘

### C1 「非損失性が恒真」は誤り (中核の論証が崩れた)

初稿は `self.clock` で経過を測る前提だったが:

- **`FixedClock.now()` は進まない** — 外部関数が実時間で 20 秒ハングしても
  `clock.now() - captured_at` は恒等的に 0。**deadline が永久に到来しない**
  (`ReplayClock` も同じ)。`Clock` は**業務上の論理時刻**の抽象であって、
  処理時間を測る抽象ではない
- **`SystemClock` は壁時計**であり monotonic ではない。NTP 補正で後退しうる
- **等号が不一致**: commit-core は `age_sec > max` (executor.py:560/807) で
  **等号は受理側**。初稿の「使い切ったら打ち切る」(= `>=`) は、
  `age_sec == 10.0` で成功しえたものを落とす

**改稿**: deadline は `time.monotonic()` (テスト用に `monotonic_fn` を注入可)
で測り、`self.clock` は `captured_at` にのみ使う。比較は `>` に揃える。
**「完全な非損失」の主張は撤回**し、「壁時計が後退せず monotonic と十分一致する
条件下で、既に stale が確定した仕事の後続 I/O を抑止する」に限定する。

### C2 検査粒度が実際のネットワーク境界と一致しない

`cycle_rate(ccy)` **1 回の内部で最大 2 本のネットワーク脚**が走る
(`price_provider.py:440-457` の USD クロス: `for spec in legs: self._rate_of(spec)`)。
指揮者が実コードで確認済み。

つまり **EUR→JPY のクロス 1 通貨だけで 10 秒 × 2 を払い**、gather 側の
検査は 20 秒後にしか走らない。**初稿が解こうとした 20.5 秒がそのまま残る。**
「最悪 = 予算 + 1 脚」も過小評価。

**改稿**: deadline を `to_account_rate` の `for spec in legs` の**各脚の前**まで
伝播させる (checker コールバック or 絶対 deadline を渡す。通常の呼び出し元では
no-op)。例外メッセージの脚名は `rate:EUR:EURUSD` のように実シンボルまで含める。

### I1 CLOSE 非対称の理由付けが**事実として誤り**

初稿は「待てば close できる」としたが、`close_from_snapshot` も
**同じ予算で stale を拒否する** (executor.py:806-810。指揮者が実コードで確認)。
待って得られるのは古い snapshot と拒否であって、close ではない。

**改稿**: **CLOSE にも同じ deadline を入れる**。現行の stale 拒否仕様の下では、
早く失敗を確定して次の再試行機会に戻る方が資金保護に有利。テスト 4・変異 4 は
**反転**させる (CLOSE でも次脚を呼ばないことを pin)。

### I2 テスト計画は先頭の検査点しか pin しない

「1 脚目で予算超過 → 2 脚目を呼ばない」1 本では、**2 番目以降の検査点を
全部削除しても green**。

**改稿**: 検査点ごとのテーブル駆動テストにし、**全 stub の呼び出し列**を
assert する (`calls == ["quote:intent", "spec:intent", ...]`)。境界は
直前・等号・直後の 3 点。`FixedClock` を業務時計にしたまま monotonic fake を
進めて deadline が論理時計から独立に動くことも pin する。
変異は「各検査点を 1 つずつ削除」に分解する。

## 改稿後のスコープ (初稿から大幅に増えた)

| 変更対象 | 初稿 | 改稿後 |
|---|---|---|
| `Executor.gather_open_snapshot` | ✓ | ✓ |
| `Executor.gather_close_snapshot` | — | **✓ (追加)** |
| `Executor` の monotonic 注入点 | — | **✓ (追加)** |
| `Executor.cycle_rate_fn` の deadline 伝播 | — | **✓ (追加)** |
| `PriceProvider.to_account_rate` | — | **✓ (追加。決定論的コア外だが本プランで未変更のファイル)** |
| テスト | 4 本 | **検査点ごとのテーブル駆動 (10 観点)** |

**これは Task 20 (E2E + 受入条件検証) の範囲を明確に超える。**
着手先はユーザー判断。

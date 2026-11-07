# 承認時に候補の土台版を照合する (approval-base-identity-cas) 設計 C1

**チケット:** `approval-base-identity-cas` (高、不具合)
**状態:** C1 (2026-11-07)、sol 1 周目を反映。§7 の 3 点は 2026-11-07 に利用者が推奨どおり承認

## 0. 何が起きるか

同じ配備版 v1 から作られた同名の候補 A と B が両方承認待ちのとき、利用者が A を承認してから B を承認すると、B は v1 を土台にしているので A の変更が黙って消える。

承認の lock の中には、候補 hash・依存 pin・未完の操作・版の再検証などの検査がある。しかし候補がどの版を土台にしたかは、どれも見ていない。同名の別候補との競合を見るのは `_is_superseded` (`plugin/switch.py:1569-1586`) だけで、これは「同名で自分より **後の** ID が承認済み」を探す。B を承認してから A を承認する順は止めるが、A → B の順は通す。

承認画面の差分 (`ops/change.py:109-121`) は閲覧時の配備版と比べるので、B が A の変更を戻す差分そのものは見える。見えないのは、それが「別の候補の変更を消す」という意味であること。

本番では今のところ起きていない (2026-11-07 の DB 読取: 同名の承認待ちが重なったのは 8 月の rsi_indicator #1・#3・#4 だけ、今の承認待ちは #78 の 1 件)。`afx ctl improve` の繰り返しで次の mission が同じ版から作り始めるため、今後は起きやすくなる。

## 1. 方針

候補ごとに「何の版から作ったか」(土台) を承認依頼に保存し、承認の lock の中で今の live と照合する。違えば配備しない。

### 1.1 土台の決め方 (経路ごと)

| 経路 | 土台 | 理由 |
|---|---|---|
| 改善 mission (`candidate_origin=staging`) | mission の開始時に読取専用スナップショットへ写した版 (`copy_source_snapshot` の `meta`)。同名が写されていなければ「スナップショットに同名なし」 | agent が見て手を入れたのはスナップショットの版。提出時の live を土台にすると、mission の途中で別の承認が入ったときに取りこぼす |
| 人間の提出 (`afx plugin submit --from _human`) | 提出時の live | 人間が `_human` に何を複製したか (`materialize_plugin`、`switch.py:2434`) は記録が無い。提出時点を土台とみなす。提出前の競合は残余 (§6) |
| 人間の bless | 照合しない | 利用者による明示的な上書き。中断後の再試行も同じ (§6) |
| 再評価 (`candidate_origin=reevaluation`) | 照合しない | 専用の `_reevaluation_is_fresh` (`switch.py:1887-`) が配備世代まで見る |

スナップショットの材料は `approved_plugins(...).phase1_metas` (`improve_loop.py:1475-1479`)。mission ごとに読み直すので、daemon の registry が古くてもその版を写すことはない。ただし、discover に失敗した版や、承認の決定が付く前の版は含まれない (`tools/plugin_loader.py:70-80`)。そのため「スナップショットに同名なし」は「live が無かった」と同じ意味ではない。照合は live に何かあれば止めるので、安全側に倒れる。

依存 pin が壊れた strategy も phase1 には残るので、pin だけを変える re-lock 候補の土台もスナップショットから取れる。

### 1.2 保存の形

payload に `base` を 1 つ足す。DB の列は足さない (schema を変えない)。

```
"base": {"kind": "version", "artifact_hash": "<hex>"}   # live が .versions/<name>/<hash> の symlink
"base": {"kind": "plain", "content_hash": "<hex>"}      # live が plain dir (旧形式)
"base": {"kind": "none"}                                 # スナップショット (人間の提出なら live) に同名なし
```

artifact_hash を使うのは、切替・journal・再評価の照合がすべて live symlink の target で動いているため。同じ mission が同名を 2 回提出しても、土台は mission 開始時のまま変えない (1 つ目の候補を 2 つ目の土台にしない)。

`base` が無い依頼 (配備前に作られたもの) だけを旧形式とし、照合を省く。キーはあるが形が壊れている (kind が未知、hash が hex でない等) 依頼は、照合を省かずに不一致として扱う。

### 1.3 照合

置く場所は、`approve_candidate` が live を読む箇所 (`switch.py:2307-2313`) の直後。その前にある検査 (履歴の世代、`_is_superseded`、依存 pin、候補の欠損・破損・hash) と、自分の journal からの再開の分岐 (`switch.py:2179-2214`。実際に切替が済んでいれば finalize して先に return する) は今のまま残す。順序が変わらないので、再開に特別な除外は要らない。

| `base` | 今の live | 結果 |
|---|---|---|
| version H | symlink → `.versions/<name>/H` | 通す |
| version H | 別の版 / 無し / plain / 不正な target | 配備しない |
| plain C | plain で content_hash C | 通す (この後は既存の `legacy_plain_present` 分岐で pending、retire してから再試行) |
| plain C | 無し | 通す (retire で plain を外した後。plain を外すのは retire だけ) |
| plain C | それ以外 | 配備しない |
| none | 無し | 通す |
| none | 何かある | 配備しない |
| 無し (旧形式) | — | 照合しない |
| 形が壊れている | — | 配備しない |

本番の plugins/ には plain dir が 0 本 (2026-11-07 確認) なので、plain の行は旧形式の救済のために置く。

**配備しないときの処理** は、同じ lock の中で次の順に行う。

1. 自分の未完の journal (`preparing` / `versioned` / `recorded`) を既存の `_close_own_unfinished_journal_if_any()` (`switch.py:2244`) で閉じる。第三者の live は戻さない。
2. 1 つの tx で `apply_decision(..., "invalidated", reason="base_changed")` を commit する。
3. commit の後に staging の候補を片付ける (`_drop_staging_candidate`)。人間の候補は消さない (既存の invalidated と同じ)。tx が失敗したら staging は消さない。

依頼は pending に残さず無効にする (§7 の裁定 3)。

**配備世代は見ない。** 目的は「今の live のソースを、候補が知らないまま上書きしない」こと。v1 → A → v1 と戻った後の B (v1 土台) は A の変更を消さない (A は既に外れている)。ただし A の ID が B より大きいと、先にある `_is_superseded` が B を無効にする。今の検査を緩めないため、これはそのまま残す。

### 1.4 retry と却下

- `retry_approval` (失敗した承認のやり直し) は `approve_candidate` を通るので、同じ照合を受ける。
- 却下・期限切れは live を変えないので照合しない。

### 1.5 理由の運び先

`base_changed` を次のすべてに通す。

- DB の reason
- `ApprovalOutcome.reason`
- ops の job の結果 (`ops/service.py:2055-2058` は今 `reevaluation_stale` だけを運ぶ)
- ctl / shell の文面 (`commands.py:853-858` は通常の invalidated を「既に決着」としか言わない)
- Discord の終端カード (`afx-ops/afx_ops/gate/render.py:351-377`)。通知 event は拡張せず、終端カードは詳細 API の reason を読む

文面は固定で「土台の版が配備中の版と違うため配備しませんでした。配備中の版から作り直して提出してください」。

## 2. 承認画面での見え方

承認待ちの依頼の詳細に、比較成績とは独立した項目「土台」を足す。既存の警告 `_baseline_hash_warning` (`ops/service.py:589-609`、content_hash の比較) は比較成績の描画に従属している (`commands.py:790-795`、`afx-ops/afx_ops/web/render.py:382-389`)。そのため indicator・re-lock・人間の提出には出ない。既存の警告は消さずに残す (`strategy_gate.py:668` に生成経路があり、test もある)。

項目の状態は次の 5 つ。

| 状態 | 表示 |
|---|---|
| 一致 | 出さない |
| 不一致 | 「警告: この候補の土台は配備中の版と違います (土台 <12 桁> / 配備中 <12 桁 または 無し / plain>)。このままでは配備できません」 |
| 土台なし・live あり | 「警告: この候補は同名の配備中の版を見ずに作られました。このままでは配備できません」 |
| 読めない (live が不正な target 等) | 「警告: 配備中の版を確かめられません」 |
| 旧形式 | 出さない |

文面は「配備できません」にとどめ、「無効になります」とは言わない。照合より前の検査が別の理由で止めることがあるため。

出す場所は ctl・shell・web・Discord の承認カード (`render_approval`、`gate/render.py:298-323`)。Discord では、文字数で省くときの優先順位を比較成績より上にする。

## 3. 配備前の依頼

`base` の無い依頼は照合しない。本番で承認待ちなのは #78 (indicator、us500_hourly_range_surprise、10-31 作成) の 1 件で、同名の候補は他に無い。

## 4. 受入条件

1. 同じ v1 から作った同名の候補 A・B を提出し、A → B の順に承認すると、B は `invalidated` / `base_changed` になる。live は A のまま、B の staging は消え、open の journal は残らない。**先に今のコードで B が通って A が消えることを test で固定してから実装する。**
2. B → A の順 (B の ID が大きい) では、A は今と同じに `_is_superseded` で無効になる (reason なし)。
3. mission の途中で同名の別候補が承認されると、その後に提出された候補は承認で止まる (土台は mission 開始時)。
4. 土台なしの候補は、承認時に live が無ければ通り、何かあれば止まる。disable で live が外れた名前も「無し」として通る (§7 の裁定 2)。
5. 人間の提出は提出時の live が土台になり、提出後に live が変わると止まる。bless は照合しない。
6. journal の各段階 (preparing / versioned / recorded / switched の切替前後)、foreign、再検証の失敗の後に照合で止めても、open の journal が残らない。切替が済んだ自分の journal の再開は止まらない。
7. v1 → A → v1 と戻った後の B (v1 土台) は、A の ID が B より小さければ通る。
8. plain の土台: live が同じ plain なら既存の `legacy_plain_present` で pending、retire した後の retry で通る。plain が別の内容なら止まる。
9. 欠損した版、dangling や不正な symlink、形の壊れた `base` は止まる。`base` が無い依頼だけは今と同じに通る。
10. pin だけを変える re-lock 候補、content_hash が同じで artifact_hash が違う更新も照合される。
11. rollback・disable・retire・reconcile の後に、土台と live が一致するか / しないかを確かめる (`none → 配備 → disable → 無し` を含む)。
12. 決定の tx が失敗すると staging は残り、commit の後に消える。人間の候補は消えない。backlog と候補台帳は `apply_decision` で終端する (`store/approvals.py:243-253`)。
13. `base_changed` が ops の結果、ctl / shell の文面、Discord の終端カードに出る。
14. 承認画面の土台の項目が、比較成績の無い依頼 (indicator・re-lock・人間の提出) を含めて、ctl / shell / web / Discord に出る。一致と旧形式では出ない。
15. 変異: 照合を外す / 土台を提出時の live にする / journal を閉じる手順を外す / 壊れた `base` を旧形式扱いにする、のいずれでも上の test が落ちる。

## 5. task

| task | 内容 | 担当 |
|---|---|---|
| T1 | `base` の保存 (staging・人間の提出) と承認の照合、journal の収束、tx と片付けの順、reason の運び先 (ops・ctl・shell)。受入 1〜13・15 | codex luna |
| T2 | 承認画面の土台の項目 (ctl / shell / web / Discord の承認カードと終端カード)。受入 13 の Discord 部分・14 | codex luna |

T1 → T2 の順。T1 だけでも lost update は止まる。

## 6. 残余

- 人間が `_human` に複製してから提出するまでの間に live が変わった場合は止まらない。由来を残すには、複製の時点で版を記録し、提出時に照合する仕組みが要る (§7 の裁定 1)。
- bless は照合しない。plain の pending や中断後の再試行も含め、利用者による明示的な上書きとして扱う (§7 の裁定 1)。
- 旧形式の依頼 (`base` なし) は照合しない。
- 無効になった候補を作り直すかは、次の改善周の判断に任せる。必ず作り直されるわけではない。却下 (利用者の判断) と無効 (`base_changed`、仕組みの判断) は別の終端として残る。
- 土台なしの候補が、配備中の同名版を上書きしようとすることは提出の時点では止めず、承認で止める。

## 7. 裁定 (2026-11-07 利用者承認、3 点とも推奨どおり)

1. **人間の提出前の競合と bless**: 明示的な上書きとして今のまま許すか、複製時の版を記録して照合するか。
   - 推奨: 許す (残余に記録)。人間の経路は利用者自身が作って承認するもので、改善 mission のように並行して候補が作られることは無い。
2. **停止 (disable) した名前の再配備**: mission の開始時に停止中だった名前の候補 (土台なし) を、停止中のまま承認してよいか。
   - 推奨: 今と同じに許す。承認は利用者の明示的な操作。土台の項目には、停止中であることを足さない。
   - 一方、停止の前の版を土台にした候補は、照合で止まる (土台 ≠ 無し)。停止の意図はこちらで守られる。
3. **土台が変わった依頼の扱い**: pending に残す (rollback で土台に戻れば通る) か、すぐ無効にして作り直させるか。
   - 推奨: すぐ無効。土台へ戻る rollback はまれ。pending に残すと、通らない依頼が承認待ちに溜まる。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-07 | C0 | 初版 | チケット approval-base-identity-cas の設計 | — |
| 2026-11-07 | C1 | journal の収束と tx・片付けの順 (§1.3)、再開の除外を外して既存の制御構造を維持、人間の経路と bless を残余と裁定に、「スナップショットに同名なし」の定義、plain と壊れた `base` の規則、ABA と `_is_superseded` の関係、照合の位置と表示の文面、reason の運び先 (§1.5)、土台の表示を比較成績から独立 (§2)、既存の content_hash 警告は残す、受入 15 項目に | sol 1 周目 (Critical 1 / Important 8 / Minor 1) | — |

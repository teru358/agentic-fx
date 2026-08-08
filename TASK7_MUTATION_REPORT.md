# Task 7 変異テスト結果 (指揮者実施, 2026-08-08)

ベースライン: 1459 → **1477 passed** (+18: `test_mission_protocol.py` 6 本 + `test_mission_worker_protocol.py` 12 本)。
実施規律: 毎回**正典 (プラン逐語コピー)** から復元 → 注入 → **diff を表示して目視確認** → `__pycache__` 削除 → pytest → 復元。

> 注: 初回実行では注入内容の表示が全件空だった (新規ファイルは untracked のため `git diff` に出ない)。
> 正典との difflib 比較に直して**生存 16 件を再実行し、全件で注入内容を目視確認済み**。

## 結果一覧

### プラン Step 11 記載の変異 (9 件 → 6a/9 は分割して 11 ケース)

| # | 変異 | 結果 | red になったテスト |
|---|---|---|---|
| 1 | `SeqTracker.check` の条件を `if False:` に | **red** | rejects_duplicate / _gap / _regression / rejects_seq_gap / wrong_seq / in_seq_continues (6 件) |
| 2 | `_set_pdeathsig` 後の ppid 照合を削除 | **生存** | — (プランに「Task 7 単体では観測不能・Task 10 E2E で拾う」と明記済み) |
| 3 | `_call` の `ok` チェックを削除 | **red** | propagates_error |
| 4 | `main()` が `_RagRpcProxy` に渡す `out_seq` を独立インスタンスに | **生存** | — ← **プラン記載の期待と食い違い** |
| 5a | `_call` の type チェックを削除 | **red** | rejects_wrong_frame_type |
| 5b | `_call` の `in_seq.check` を削除 | **red** | rejects_seq_gap / in_seq_continues_after_handshake |
| 6a | `main()` の handshake type チェックを削除 | **生存** | — ← **プラン記載の期待と食い違い** |
| 6b | `main()` の handshake `in_seq.check` を削除 | **red** | wrong_seq (指揮者が着手前に追加したテスト) |
| 7 | `_build_clock` を `FixedClock` 固定に | **red** | build_clock_default_rejects_stale_signal |
| 8 | `_make_on_message` の try/except + `os._exit` を削除 | **red** | on_message_exits_process_on_write_failure |
| 9 | `main()` が `_RagRpcProxy` に渡す `in_seq` を独立インスタンスに | **生存** | — ← **指揮者が着手前に追加した変異。同じく食い違い** |

### 自主追加変異 (17 件 — 「リストは下限であって天井ではない」)

| # | 変異 | 結果 |
|---|---|---|
| A | `SeqTracker.check` の int 型検証を削除 | **生存** |
| B | `SeqTracker.check` のカウンタ前進を削除 | red (4 件) |
| C | `write_frame` の `flush()` を削除 | **生存** |
| D | `read_frame` の EOF 判定を反転 | (アンカー不一致 — 未実施) |
| E | `_call` の `rpc_id` 照合を削除 | **生存** |
| F | `_call` の EOF (`response is None`) 判定を削除 | **生存** |
| G | `_RagRpcProxy._seq_next` のカウンタ前進を削除 (seq 固定) | **生存** |
| H | `_make_on_message` の `os._exit(1)` → `os._exit(0)` | red |
| I | `_set_pdeathsig` の戻り値チェックを無効化 | red |
| J | `_set_pdeathsig` の prctl 呼び出しごと削除 | red (2 件) |
| K | `main()` の `worker_profile != "trade"` チェックを削除 | **生存** |
| L | `main()` の `backend != "local"` (ClaudeRunner fail-closed) を削除 | **生存** |
| M | `main()` の `build_mission_registry(..., readonly=True)` を外す | **生存** |
| N | `_set_resource_limits` の `RLIMIT_AS` 設定を削除 | **生存** |
| O | `_protect_protocol_stdout` の `dup2` を削除 | **生存** |
| P | `main()` の `ready` 送出を `ok=False` に反転 | **生存** |
| Q | `runner.run` 例外時の `result` 送出 (fail closed) を削除 | **生存** |

## 生存 16 件の根本原因 (1 つ)

**`main()` の本体に一切テストが無い。** プラン Step 5 のテスト 12 本の内訳:

- `_RagRpcProxy` 単体 6 本 / `_make_on_message` 1 本 / `_set_pdeathsig` 2 本 / `_build_clock` 1 本
- `main()` を呼ぶのは 2 本だけ (`wrong_type` / `wrong_seq`) で、**どちらも最初の数行で例外を投げて外側 `except` に落ちる経路**しか通らない

結果、`main()` の bootstrap 本体 (ppid 照合・rlimit・profile 検証・**backend fail-closed**・`readonly=True`・`out_seq`/`in_seq` の共有配線・`ready` 送出・`result` 送出) は**全て無防備**。
`_set_resource_limits` と `_protect_protocol_stdout` も同様に未テスト。

これは既知の教訓 3 つの複合:
- 「変異リストは天井でなく下限」— リスト外の防御 11 件が無防備
- 「配線そのものを検証する」— 単体は全部緑だが `main()` の配線が誰にも検証されていない
- 「1 site の red で防御全体を守られていると誤判断しない」— `_RagRpcProxy` 単体の seq 共有テストは**契約**を pin するが `main()` の**配線**を pin しない (CR-3 の再発を防げない)

### 特に重大

- **追加L** — `runner.trade.backend != "local"` の fail closed は **Global Constraints (Anthropic API 従量課金は使用不可)** の強制点。削除しても緑
- **変異4 / 変異9** — CR-3 (レビューで実際に見つかった不具合) の回帰が、`main()` 側では**再発しても検出されない**
- **追加M** — CR-4 (`readonly=True`) も同様
- **追加N** — rlimit (§12 申し送り②の確定値) が実際に設定されるかを誰も確かめていない

## プラン記述側の欠陥 (レビューへの申し送り)

1. **Step 11 変異 4 は無効** — `main()` の call site を変異させても、`test_rag_rpc_proxy_shares_out_seq_...` は `_RagRpcProxy` を直接構築するため red にならない。指揮者が着手前に追加した変異 9 も同じ誤り (同じ穴を踏んだ)
2. **Step 11 変異 6a も無効** — type チェックを削除しても `test_main_rejects_handshake_with_wrong_type` は緑。理由: 後続の `handshake["expected_parent_pid"]` が `KeyError` を投げ、外側 `except` が同じ `ready: ok=False` を返すため。テストが**エラー内容を検証していない**

---

# 対応: `main()` の配線テストを追加 (指揮者, 2026-08-08)

生存 16 件の根本原因が 1 つ (`main()` 未テスト) だったため、レビュー前に塞いだ。
`tests/test_mission_worker_protocol.py` に **12 本追加 + 既存 1 本にアサート追加**:

| 追加テスト | 塞いだ変異 |
|---|---|
| `test_main_happy_path_emits_ready_event_result_in_one_seq_sequence` | 追加P (ready ok 反転) |
| `test_main_shares_out_seq_and_in_seq_with_rag_rpc_proxy` | **変異4 / 変異9** (CR-3・I2 の配線) |
| `test_main_builds_registry_readonly_and_applies_resource_limits` | **追加M** (CR-4 readonly) |
| `test_main_reports_result_failed_when_runner_raises` | 追加Q |
| `test_main_exits_without_ready_when_reparented` | **変異2** (プランは「Task 7 単体では観測不能」としていたが pin 可能だった) |
| `test_main_rejects_unsupported_worker_profile` | 追加K |
| `test_main_fails_closed_when_runner_backend_is_claude` | **追加L (Global Constraints 強制点)** |
| `test_set_resource_limits_sets_all_four_limits` | 追加N |
| `test_seq_tracker_rejects_bool_as_seq` | 追加A |
| `test_rag_rpc_proxy_rejects_rpc_id_mismatch` | 追加E |
| `test_rag_rpc_proxy_raises_when_parent_closes_pipe` | 追加F |
| `test_rag_rpc_proxy_advances_out_seq_across_successive_calls` | 追加G |
| `test_main_rejects_handshake_with_wrong_type` に `assert "handshake" in sent["error"]` を追加 | **変異6a** |

## 再検証結果

**生存 16 件 → 2 件**。全 16 件を再注入し、上表のテストが red になることを目視確認込みで実測した。

## 残存 (accepted-unpinned — 意図的に pin しない)

| # | 変異 | 判断 |
|---|---|---|
| 追加C | `write_frame` の `flush()` 削除 | pin には実 fd とバッファリング挙動の観測が要る。子→親は行志向 JSON で親が `readline` するため、flush 欠落は**実 subprocess でのみ**症状が出る。**Task 10/20 の実 spawn テストで拾う** |
| 追加O | `_protect_protocol_stdout` の `dup2` 削除 | 同上。fd 1 の付け替えは実プロセスでしか観測できない (インプロセスでは `_protect_protocol_stdout` 自体を monkeypatch しているため)。**Task 20 の E2E で拾う** |

いずれも「テストが無いことに気づいていない」のではなく、**Task 7 の観測範囲外**と判断して次 task へ送る。レビュアーはこの判断の妥当性を見てほしい。

## 最終状態

- `uv run pytest -q` = **1489 passed, 1 deselected** (ベースライン 1459 + 30)
- `mission_protocol.py` / `mission_worker.py` はプラン逐語コードと **byte 一致** (変異残留なし・機械照合済み)

---

# レビュー 1 周目の反映 (指揮者裁定, 2026-08-08)

codex 主査 Important 3 件 + sonnet 副査 Important 4 件を裁定し、**全件採用**した。

| 出典 | 指摘 | 裁定 |
|---|---|---|
| codex + sonnet | `read_frame` が `JSONDecodeError` を素通し (`ProtocolError` 単一表現と矛盾)。codex はさらに **`main()` が handshake 読取を `try` の外で呼ぶため不正 JSON では `ready:false` すら返らない**ことと、非 dict 検証の欠落を指摘 | **採用**。`read_frame` で `ValueError` を `ProtocolError` へ正規化 + 非 dict を拒否。handshake 読取を `try` 内へ移動 |
| codex + sonnet | 外側 `except` の `seq: 1` ハードコード。codex はさらに **`_next_seq` が送出成功前にカウンタを消費する**ため内側 `except` 単独でも欠番が出ることを指摘 | **採用**。`_send_frame` を新設し**送出成功後**に採番を進める。外側 `except` は `ready_sent` を見て `result: failed` へ切替 |
| codex + sonnet (両者一致) | accepted-unpinned 2 件 (`flush` / `dup2`) は Task 7 内で pin 可能 | **採用 — 指揮者の判断を撤回**。sonnet が `os.pipe()` を使う実行可能な probe を提示。両方 pin した |
| sonnet 単独 | `_set_resource_limits` の fail closed が未検証 (`_drive_main` が常に成功する fake に差し替えているため) | **採用**。例外送出 fake で `ready:false` + registry 未到達を pin |

重大度が割れた 1 件 (seq=1 ハードコード: codex「実害あり」/ sonnet「到達するが実害再現できず」) は、**codex の指摘した欠番経路が本質**と指揮者が判定して採用した。

## 追加テスト 10 本

`test_read_frame_raises_protocol_error_on_malformed_json` / `_on_invalid_utf8` / `test_read_frame_rejects_non_object_frames` / `test_write_frame_flushes_immediately` / `test_main_reports_ready_false_on_malformed_handshake_json` / `_on_non_object_handshake` / `test_main_does_not_leave_seq_gap_when_result_write_fails_once` / `test_main_does_not_resend_ready_when_outer_except_is_reached_after_ready` / `test_main_fails_closed_when_resource_limits_cannot_be_set` / `test_protect_protocol_stdout_redirects_fd1_to_stderr`

## 変異テスト再走 (34 件)

修正で変わったアンカーを更新し、**プラン記載 9 + 自主追加 17 + レビュー反映 8 = 34 件**を全て再注入。

**生存 0 件**（初回の 16 件 → 12 本追加で 2 件 → レビュー反映で 0 件）。`追加D` は初回アンカーのインデント誤りで空振りしていたことも判明し、修正して red を確認した。

## 最終状態

- `uv run pytest -q` = **1499 passed, 1 deselected** (ベースライン 1459 + 40)

---

# レビュー 2 周目の反映 (指揮者裁定, 2026-08-08)

**重大度が割れた** (codex: Important 1 + Minor 1 / sonnet: Minor 3 のみ)。規約どおり指揮者が再判定し、**codex の Important を採用**した。sonnet は「テストが元の欠陥を検出できるか」を 8 件の変異で実測確認したが、**retry ポリシー自体の健全性**は検証しておらず、この欠陥に到達していない。

| 出典 | 指摘 | 裁定 |
|---|---|---|
| codex | **1 周目の修正が新しい欠陥を持ち込んでいた** — `write`/`flush` の例外は「wire に 1 バイトも出ていない」ことを保証しないのに、同じ seq で再送していた。部分書込み後なら**行が壊れ**、flush 後の失敗なら**重複**になる。あわせて `_FlakyStream` が「書き込む前に raise」しており実 I/O の曖昧性をモデル化していないことも指摘 | **採用 (Important)** |
| sonnet 単独 | `_RagRpcProxy._call` の「送出成功後に採番」が未 pin (pre-increment に戻しても 1499 件 green) | **採用** |
| sonnet 単独 | `write_frame` の `TypeError` を正規化しない非対称が未文書 | **採用** (docstring に理由を明記) |
| codex + sonnet | 廃止済み `_next_seq` / `_seq_next` への言及が散文に残存。sonnet は **Task 18 のスケッチ**にも残っていることを追加検出 | **採用** |

## 実装した修正

- `mission_protocol.encode_frame()` を分離 (ストリームに触れない serialize 専用)
- `mission_worker._write_frame_or_die()` を新設 — **serialize 失敗**は例外として上げ (wire 未接触 → 同一 seq で再送可)、**transport 失敗**は `os._exit(1)` で**再送せず即終了** (配信不明 → fail closed)
- `_RagRpcProxy` の `tool_rpc` 送出も同じ writer を通す
- `_FlakyStream` に `partial_bytes` を追加し、「部分書込み後の失敗」「全バイト書込み後の flush 失敗」「serialize 失敗」の 3 経路を個別に pin
- `_make_on_message` のピンを **serialize 失敗**へ変更 (transport 失敗は `_write_frame_or_die` が先に落とすため、この guard の単独ピンにならなくなっていた)

テスト 5 本追加 (計 45 本)。

## 変異テスト再走 (40 件)

**生存 1 件** — `encode_frame` を `write_frame` にインライン戻しする**等価変異** (equivalent mutant: 出力がバイト単位で同一なので、原理的にどのテストでも区別できない)。実質的な生存はゼロ。

### 変異 driver の落とし穴 (今回実測)

変異が `os._exit` を踏むと **pytest プロセスごと停止**し、driver からは「FAILED 行なし = 生存」に見える。`R2-b` (serialize 失敗も transport 扱いにする変異) で実際に**偽の生存**を 1 件出した。

対策 2 つを入れた:
1. driver 側: pytest の要約行に `passed`/`failed`/`error` が無ければ「異常終了 = 検出扱い」とする
2. テスト側: fail-closed 経路を持つコードのテストは `os._exit` を必ず monkeypatch する。**「終了しないこと」の assert も pin になる** (serialize 失敗の 2 本に `assert codes == []` を追加した)

## 最終状態

- `uv run pytest -q` = **1504 passed, 1 deselected** (ベースライン 1459 + 45)
- 実装ファイルはプラン Step 3 / Step 8 の逐語コードと **byte 一致** (機械照合済み)

"""signals ストアのテスト (プラン 7 Task 7)。

signals は 4 状態 (pending/claimed/consumed/abandoned) のキュー。§5 の
「3 段階」は正常系遷移 (pending → claimed → consumed) の呼称で、abandoned
は requeue 上限超過 / 鮮度切れの終端状態。

コントローラ解決 (task-7-brief.md 添付) の骨子:
- 格納値は isoformat ('T' 区切り)。SQLite の datetime() は 'T' 区切りを
  受理するが返り値はスペース区切りなので、鮮度・lease 判定は両辺を
  datetime() で正規化して比較する ('T' vs ' ' 文字列比較の罠)。
- claim_oldest の freshness_bars=None は鮮度条件そのものを WHERE から
  外す (ゲート無効)。
- CASE 式 (timeframe→分) は PLUGIN_TIMEFRAMES と同期必須 — 同期テストを
  必ず置く。
- requeue の上限判定は「現在の requeue_count >= max_requeue なら
  abandoned (増分なし)、未満なら pending + requeue_count+1」
  (controller 解決 #6 の逐語)。reclaim_expired も同じ判定を通す。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES
from agentic_fx.store import signals
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _add(conn, *, bar_ts: datetime, content_hash="h1", pair="USDJPY",
         timeframe="1h", kind="signal", payload=None, now=NOW):
    return signals.add(
        conn, plugin="p.py", content_hash=content_hash, pair=pair,
        timeframe=timeframe, bar_ts=_iso(bar_ts), kind=kind,
        payload=payload or {"x": 1}, now=now)


# ---------------------------------------------------------------------
# ① 重複キー 2 回目 None
# ---------------------------------------------------------------------
def test_add_duplicate_key_returns_none_on_second_insert(tmp_path):
    conn = _conn(tmp_path)
    bar_ts = NOW
    first = _add(conn, bar_ts=bar_ts)
    second = _add(conn, bar_ts=bar_ts)
    assert isinstance(first, int)
    assert second is None
    rows = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()
    assert rows["c"] == 1


# ---------------------------------------------------------------------
# ② content_hash 違いは別行
# ---------------------------------------------------------------------
def test_add_different_content_hash_is_separate_row(tmp_path):
    conn = _conn(tmp_path)
    bar_ts = NOW
    first = _add(conn, bar_ts=bar_ts, content_hash="h1")
    second = _add(conn, bar_ts=bar_ts, content_hash="h2")
    assert isinstance(first, int) and isinstance(second, int)
    assert first != second
    rows = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()
    assert rows["c"] == 2


# ---------------------------------------------------------------------
# ③ claim_oldest が bar_ts 最古
# ---------------------------------------------------------------------
def test_claim_oldest_picks_oldest_bar_ts_even_if_inserted_later(tmp_path):
    """新しい bar_ts を先に INSERT する (id ASC だけでは正しい行を選べない
    ことを保証する — ORDER BY datetime(bar_ts) が効いているかの killer)。"""
    conn = _conn(tmp_path)
    newer_id = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
                    content_hash="newer")
    older_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    content_hash="older")
    assert newer_id < older_id  # id 順は逆 (先に入れた方が古い bar_ts)

    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                    freshness_bars=None)
    assert claimed is not None
    assert claimed["id"] == older_id
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by_mission_id"] == 1


# ---------------------------------------------------------------------
# ④ 2 連続 claim は別行
# ---------------------------------------------------------------------
def test_two_consecutive_claims_return_different_rows(tmp_path):
    conn = _conn(tmp_path)
    id1 = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
              content_hash="a")
    id2 = _add(conn, bar_ts=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
              content_hash="b")
    c1 = signals.claim_oldest(conn, mission_id=1, now=NOW, freshness_bars=None)
    c2 = signals.claim_oldest(conn, mission_id=2, now=NOW, freshness_bars=None)
    assert {c1["id"], c2["id"]} == {id1, id2}
    assert c1["id"] != c2["id"]
    c3 = signals.claim_oldest(conn, mission_id=3, now=NOW, freshness_bars=None)
    assert c3 is None  # もう pending は無い


# ---------------------------------------------------------------------
# ⑤ consume の mission_id 不一致拒否
# ---------------------------------------------------------------------
def test_consume_rejects_mismatched_mission_id(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, bar_ts=NOW)
    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                    freshness_bars=None)
    with pytest.raises(ValueError, match="not claimed"):
        signals.consume(conn, claimed["id"], mission_id=999, now=NOW)
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (claimed["id"],)).fetchone()
    assert row["status"] == "claimed"  # 拒否されたので状態不変


def test_consume_succeeds_with_matching_mission_id(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, bar_ts=NOW)
    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                    freshness_bars=None)
    signals.consume(conn, claimed["id"], mission_id=1, now=NOW)
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (claimed["id"],)).fetchone()
    assert row["status"] == "consumed"


def test_consume_rejects_non_claimed_status(tmp_path):
    """pending のまま consume しようとすると拒否 (fail closed)。"""
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    with pytest.raises(ValueError, match="not claimed"):
        signals.consume(conn, sid, mission_id=1, now=NOW)


# ---------------------------------------------------------------------
# ⑥ requeue 上限超過 abandoned (境界を厳密にピン: >= max_requeue で
#    abandoned、増分なし。未満は pending + requeue_count+1)
# ---------------------------------------------------------------------
def test_requeue_boundary_table(tmp_path):
    conn = _conn(tmp_path)
    max_requeue = 2
    sid = _add(conn, bar_ts=NOW)

    signals.claim_oldest(conn, mission_id=1, now=NOW, freshness_bars=None)
    status = signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    assert status == "pending"
    row = conn.execute("SELECT requeue_count FROM signals WHERE id=?",
                       (sid,)).fetchone()
    assert row["requeue_count"] == 1

    signals.claim_oldest(conn, mission_id=2, now=NOW, freshness_bars=None)
    status = signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    assert status == "pending"
    row = conn.execute("SELECT requeue_count FROM signals WHERE id=?",
                       (sid,)).fetchone()
    assert row["requeue_count"] == 2

    signals.claim_oldest(conn, mission_id=3, now=NOW, freshness_bars=None)
    status = signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    assert status == "abandoned"  # requeue_count(2) >= max_requeue(2)
    row = conn.execute(
        "SELECT status, requeue_count, claimed_by_mission_id, claimed_at "
        "FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "abandoned"
    assert row["requeue_count"] == 2  # abandoned 分岐では増分しない
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_requeue_rejects_non_claimed_status(tmp_path):
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    with pytest.raises(ValueError, match="is not claimed"):
        signals.requeue(conn, sid, now=NOW, max_requeue=2)


# ---------------------------------------------------------------------
# ⑦ reclaim_expired: 期限内は不変・期限切れは requeue_count 増・
#    上限超過 abandoned。lease 境界 <= を厳密にピン。
# ---------------------------------------------------------------------
def test_reclaim_expired_boundary_and_transitions(tmp_path):
    conn = _conn(tmp_path)
    lease_min = 15
    max_requeue = 2

    # 期限内 (claimed_at == now - 14min, cutoff は now - 15min): 不変
    within_id = _add(conn, bar_ts=NOW, content_hash="within")
    signals.claim_oldest(conn, mission_id=1, now=NOW, freshness_bars=None)
    within_claimed_at = NOW - timedelta(minutes=14)
    conn.execute("UPDATE signals SET claimed_at=? WHERE id=?",
                (within_claimed_at.isoformat(), within_id))

    # ちょうど境界 (claimed_at == now - 15min): <= なので回収対象
    boundary_id = _add(conn, bar_ts=NOW, content_hash="boundary")
    signals.claim_oldest(conn, mission_id=2, now=NOW, freshness_bars=None)
    boundary_claimed_at = NOW - timedelta(minutes=15)
    conn.execute("UPDATE signals SET claimed_at=? WHERE id=?",
                (boundary_claimed_at.isoformat(), boundary_id))

    # 期限切れ・requeue_count=1 で上限未満 → pending + count=2
    expired_id = _add(conn, bar_ts=NOW, content_hash="expired")
    signals.claim_oldest(conn, mission_id=3, now=NOW, freshness_bars=None)
    conn.execute(
        "UPDATE signals SET claimed_at=?, requeue_count=1 WHERE id=?",
        ((NOW - timedelta(hours=2)).isoformat(), expired_id))

    # 期限切れ・requeue_count=2 (>= max_requeue) → abandoned・増分なし
    over_id = _add(conn, bar_ts=NOW, content_hash="over")
    signals.claim_oldest(conn, mission_id=4, now=NOW, freshness_bars=None)
    conn.execute(
        "UPDATE signals SET claimed_at=?, requeue_count=2 WHERE id=?",
        ((NOW - timedelta(hours=2)).isoformat(), over_id))
    conn.commit()

    result = signals.reclaim_expired(conn, now=NOW, lease_min=lease_min,
                                     max_requeue=max_requeue)
    assert sorted(result) == sorted(["pending", "pending", "abandoned"])

    def _row(sid):
        return dict(conn.execute(
            "SELECT status, requeue_count, claimed_by_mission_id, claimed_at "
            "FROM signals WHERE id=?", (sid,)).fetchone())

    within = _row(within_id)
    assert within["status"] == "claimed"  # 不変
    assert within["requeue_count"] == 0

    boundary = _row(boundary_id)
    assert boundary["status"] == "pending"  # 境界含む (<=)
    assert boundary["requeue_count"] == 1

    expired = _row(expired_id)
    assert expired["status"] == "pending"
    assert expired["requeue_count"] == 2
    assert expired["claimed_by_mission_id"] is None

    over = _row(over_id)
    assert over["status"] == "abandoned"
    assert over["requeue_count"] == 2  # 増分なし
    assert over["claimed_by_mission_id"] is None


# ---------------------------------------------------------------------
# ⑧ abandoned は claim 対象外
# ---------------------------------------------------------------------
def test_abandoned_is_not_claimable(tmp_path):
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    signals.claim_oldest(conn, mission_id=1, now=NOW, freshness_bars=None)
    signals.requeue(conn, sid, now=NOW, max_requeue=0)  # 即 abandoned
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (sid,)).fetchone()
    assert row["status"] == "abandoned"
    claimed = signals.claim_oldest(conn, mission_id=2, now=NOW,
                                   freshness_bars=None)
    assert claimed is None


# ---------------------------------------------------------------------
# ⑨ expire_stale: freshness 超過の pending が abandoned・以内は残る・
#    claimed には触れない
# ---------------------------------------------------------------------
def test_expire_stale_only_touches_stale_pending(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2  # 1h × 2 = 120min cutoff

    stale_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    content_hash="stale")  # 3h 前 -> stale
    fresh_id = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
                    content_hash="fresh")  # 1h 前 -> fresh
    claimed_stale_id = _add(
        conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        content_hash="claimed_stale")  # stale だが claimed 済み → 触れない
    # claim_oldest は bar_ts 最古優先で stale_id (同じ bar_ts だが id が
    # 若い) を選んでしまうため、対象行を直接 claimed にする (このテストは
    # claim_oldest の選択ロジックではなく expire_stale の対象範囲を見る)。
    conn.execute("UPDATE signals SET status='claimed', "
                "claimed_by_mission_id=1, claimed_at=? WHERE id=?",
                (NOW.isoformat(), claimed_stale_id))
    conn.commit()
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (claimed_stale_id,)).fetchone()
    assert row["status"] == "claimed"

    count = signals.expire_stale(conn, now=NOW, freshness_bars=freshness_bars)
    assert count == 1

    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (stale_id,)).fetchone()["status"] == "abandoned"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (fresh_id,)).fetchone()["status"] == "pending"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (claimed_stale_id,)).fetchone()["status"] == "claimed"


# ---------------------------------------------------------------------
# ⑩ 1h/4h/1d 混在の pending で timeframe 別 cutoff が SQL 内で正しく効く
#    (codex R3 C1 killer)
# ---------------------------------------------------------------------
def test_expire_stale_mixed_timeframes_cutoff(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2
    # 1h の 3 バー前 (3h 前) = stale (cutoff 2*60=120min)
    h1_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                content_hash="h1", timeframe="1h")
    # 4h の 3 バー前 相当 (12h 前と見せかけて実際は 3h 前 = fresh。
    # cutoff は 2*240=480min=8h なので 3h 前は fresh)
    h4_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                content_hash="h4", timeframe="4h")
    # 1d の 3 時間前 = fresh (cutoff 2*1440=2880min=48h)
    d1_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                content_hash="d1", timeframe="1d")

    count = signals.expire_stale(conn, now=NOW, freshness_bars=freshness_bars)
    assert count == 1  # 1h の行だけ stale

    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (h1_id,)).fetchone()["status"] == "abandoned"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (h4_id,)).fetchone()["status"] == "pending"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (d1_id,)).fetchone()["status"] == "pending"


# ---------------------------------------------------------------------
# ⑪ expire_stale を呼ばずに claim_oldest だけ呼んでも stale 行は claim
#    されない (claim 側の鮮度条件ピン)
# ---------------------------------------------------------------------
def test_claim_oldest_skips_stale_row_without_expire_stale(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2
    # stale (bar_ts が古い方) と fresh (新しい方) の 2 行。bar_ts 最古優先
    # だけを見ると stale の方が選ばれてしまう — 鮮度ゲートが効いているかの
    # killer。
    stale_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    content_hash="stale")  # 3h 前 -> stale (1h tf, cutoff 2h)
    fresh_id = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 30,
                                          tzinfo=timezone.utc),
                    content_hash="fresh")  # 30min 前 -> fresh

    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                   freshness_bars=freshness_bars)
    assert claimed is not None
    assert claimed["id"] == fresh_id  # stale はスキップされ fresh が選ばれる

    # stale 行はまだ pending のまま (claim も expire もされていない)
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (stale_id,)).fetchone()
    assert row["status"] == "pending"


# ---------------------------------------------------------------------
# claim 経路でも timeframe 別 cutoff が効くことをピン (⑩ は expire_stale
# 経由、⑪ は 1h のみでの claim 経路だったため、claim が「行ごとの
# timeframe」で cutoff を計算していることを直接見るテストが無かった —
# advisor 指摘)。
# ---------------------------------------------------------------------
def test_claim_oldest_computes_cutoff_per_row_timeframe(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2
    # 1h, bar_ts が古い方 (3h 前) -> stale (cutoff 2*60=120min)。bar_ts が
    # 最古なので鮮度ゲートが無ければこちらが選ばれてしまう。
    stale_1h_id = _add(
        conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        content_hash="stale_1h", timeframe="1h")
    # 4h, 同じく 3h 前だが cutoff は 2*240=480min=8h なので fresh。
    fresh_4h_id = _add(
        conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        content_hash="fresh_4h", timeframe="4h")

    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                   freshness_bars=freshness_bars)
    assert claimed is not None
    assert claimed["id"] == fresh_4h_id  # stale な 1h 行はスキップされる

    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (stale_1h_id,)).fetchone()
    assert row["status"] == "pending"  # stale 行はまだ触れられていない


# ---------------------------------------------------------------------
# claim_oldest の freshness_bars=None はゲート無効 (stale でも claim 対象)
# ---------------------------------------------------------------------
def test_claim_oldest_freshness_none_disables_gate(tmp_path):
    conn = _conn(tmp_path)
    stale_id = _add(conn, bar_ts=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))
    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None
    assert claimed["id"] == stale_id


# ---------------------------------------------------------------------
# CASE 式 (_TF_MINUTES_CASE) の PLUGIN_TIMEFRAMES との同期テスト
# ---------------------------------------------------------------------
def test_tf_minutes_case_synced_with_plugin_timeframes():
    case_tfs = set(re.findall(r"WHEN '([^']+)'", signals._TF_MINUTES_CASE))
    assert case_tfs == set(PLUGIN_TIMEFRAMES)


# ---------------------------------------------------------------------
# add(): timeframe は PLUGIN_TIMEFRAMES 限定 (CASE が NULL を返す組み合わせ
# を db に入れさせない — 入ってしまうと鮮度判定不能な不死身 pending になる)
# ---------------------------------------------------------------------
def test_add_rejects_timeframe_outside_plugin_timeframes(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="timeframe"):
        signals.add(conn, plugin="p.py", content_hash="h", pair="USDJPY",
                    timeframe="5m", bar_ts=_iso(NOW), kind="signal",
                    payload={}, now=NOW)


# ---------------------------------------------------------------------
# pending_exists / recent
# ---------------------------------------------------------------------
def test_pending_exists(tmp_path):
    conn = _conn(tmp_path)
    assert signals.pending_exists(conn) is False
    _add(conn, bar_ts=NOW)
    assert signals.pending_exists(conn) is True


def test_recent_filters_by_pair_and_bar_ts_since(tmp_path):
    """recent() は bar_ts を鮮度軸に使う (claim/freshness/staleness と同じ
    時間軸)。created_at (格納時刻) が新しくても bar_ts が古ければ「recent」
    には含めない — 取引 loop に古いバー由来のシグナルを「最近」として
    見せない (advisor 判断)。"""
    conn = _conn(tmp_path)
    old_bar = _add(conn, bar_ts=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
                   content_hash="old", pair="USDJPY", now=NOW)
    new_bar = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
                   content_hash="new", pair="USDJPY", now=NOW)
    other_pair = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0,
                                            tzinfo=timezone.utc),
                      content_hash="other", pair="EURUSD", now=NOW)

    since = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)
    rows = signals.recent(conn, "USDJPY", since=since)
    ids = {r["id"] for r in rows}
    assert ids == {new_bar}
    assert old_bar not in ids
    assert other_pair not in ids

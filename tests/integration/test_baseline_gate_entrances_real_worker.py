"""同名更新の比較 gate を、3 入口 (週次 / 手動単発 / 手動 repeat) から実 worker・
実 DB で通し、証拠の SQL・却下の観測点・agent や通知に漏れないことを確かめる。

gate の backtest は plugin sandbox の本物の worker プロセスで動く。差し替えるのは
LLM を呼ぶ `WorkerRunner` だけ (候補の置き込み)。合成履歴は決定論なので、候補と
配備中の版の成績は毎回同じ値になる。
"""
from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentic_fx.core import improve_repeat as ir
from agentic_fx.loops import baseline_monitor
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.ops import client as ops_client
from agentic_fx.ops.contracts import Principal
from agentic_fx.runners.base import MissionResult
from tests.loops.test_improve_e2e import FakeImproveWorkerRunner
from tests.fixtures import baseline_world as bw

pytestmark = pytest.mark.slow

BASE_VERSION = dict(take_profit=0.6, stop_loss=0.3)
BETTER_VERSION = dict(take_profit=0.8, stop_loss=0.3)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """enforce で、同じ挙動の候補を 3 入口から、上回る候補を手動で 1 本通す。"""
    root = tmp_path_factory.mktemp("entrances")
    with bw.open_world(root, mode="enforce") as world:
        deployed = bw.deploy(world, py=bw.strategy_py(**BASE_VERSION))
        timings: dict[str, float] = {}

        def timed(label, fn):
            start = time.monotonic()
            value = fn()
            timings[label] = round(time.monotonic() - start, 1)
            return value

        def same(note):
            return bw.candidate_files(
                py=bw.strategy_py(**BASE_VERSION, note=note))

        weekly = timed("weekly", lambda: bw.run_weekly(world, same("w")))
        manual = timed("manual", lambda: bw.run_manual(world, same("m")))
        repeat, progress = timed("repeat", lambda: bw.run_repeat(
            world, [same("r1"), same("r2")]))
        improved = timed("improved", lambda: bw.run_manual(
            world, bw.candidate_files(py=bw.strategy_py(**BETTER_VERSION))))
        violation = timed("violation", lambda: bw.run_manual(
            world, bw.candidate_files(
                config=bw.CONFIG.replace("timeframe: 1h", "timeframe: 4h"))))
        ids = [r[0] for r in world.conn.execute(
            "SELECT id FROM missions ORDER BY id")]
        yield SimpleNamespace(
            world=world, deployed=deployed, weekly=weekly, manual=manual,
            repeat_result=repeat, repeat_progress=progress,
            improved=improved, violation=violation, timings=timings,
            all_ids=ids)



ENTRANCES = ["weekly", "manual", "repeat-1", "repeat-2"]
NAME = bw.NAME


def _entrance_missions(runs):
    others = [i for i in runs.all_ids
              if i not in (runs.weekly, runs.manual, runs.improved,
                           runs.violation)]
    first, second = others
    return {"weekly": runs.weekly, "manual": runs.manual,
            "repeat-1": first, "repeat-2": second}


def _rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


# --- 同条件の証拠 (SQL) --------------------------------------------------------

_PAIRED = """
SELECT c.scope, c.pair, c.content_hash AS candidate_hash,
       b.content_hash AS base_hash, b.ref_content_hash, b.variant,
       b.mission_outcome AS base_outcome, c.mission_outcome AS cand_outcome,
       (SELECT count(*) FROM backtest_runs x
         WHERE x.mission_id=b.mission_id AND x.variant='candidate'
           AND x.plugin_ref='plugins/{name}' AND x.scope=b.scope
           AND x.pair=b.pair AND x.content_hash=b.ref_content_hash) AS unique_ref
FROM backtest_runs c
JOIN backtest_runs b
  ON b.mission_id=c.mission_id AND b.variant='baseline'
 AND b.plugin_ref=c.plugin_ref
 AND b.ref_content_hash=c.content_hash
 AND b.scope=c.scope AND b.pair=c.pair
 AND b.created_at=c.created_at
 AND b.period_start=c.period_start AND b.period_end=c.period_end
 AND b.source=c.source AND b.base_interval=c.base_interval
 AND b.timeframe=c.timeframe AND b.settings_hash=c.settings_hash
 AND b.core_commit=c.core_commit
WHERE c.mission_id=? AND c.variant='candidate'
  AND c.plugin_ref='plugins/{name}'
ORDER BY c.scope
""".format(name=NAME)


@pytest.mark.parametrize("entrance", ENTRANCES + ["improved"])
def test_candidate_and_baseline_rows_are_paired_by_pair_and_stage(
        runs, entrance):
    conn = runs.world.conn
    mission = (runs.improved if entrance == "improved"
               else _entrance_missions(runs)[entrance])
    paired = _rows(conn, _PAIRED, mission)
    # 1 pair x 2 段が、全ての同条件列で対になる
    assert [(r["scope"], r["pair"]) for r in paired] == [
        ("holdout_gate", "USDJPY"), ("in_sample", "USDJPY")]
    # 対の外に行が余らない (候補 2 行・baseline 2 行だけ)
    for variant in ("candidate", "baseline"):
        (count,) = conn.execute(
            "SELECT count(*) FROM backtest_runs WHERE mission_id=? "
            "AND variant=? AND plugin_ref='plugins/momo'",
            (mission, variant)).fetchone()
        assert count == 2, variant
    for row in paired:
        assert row["variant"] == "baseline"
        assert row["base_hash"] == runs.deployed
        assert row["candidate_hash"] != runs.deployed
        assert row["ref_content_hash"] == row["candidate_hash"]
        assert row["unique_ref"] == 1
        # baseline 行の outcome は候補行と同じ値
        assert row["base_outcome"] == row["cand_outcome"]


# --- 同じ指標で却下 (enforce) ---------------------------------------------------

def _activity_lines(runs):
    return runs.world.app.activity.tail(10_000)


@pytest.mark.parametrize("entrance", ENTRANCES)
def test_an_equal_candidate_is_rejected_with_five_observation_points(
        runs, entrance):
    world = runs.world
    conn = world.conn
    mission = _entrance_missions(runs)[entrance]
    # mission は completed で終わり、承認依頼は増えない
    (status,) = conn.execute("SELECT status FROM missions WHERE id=?",
                             (mission,)).fetchone()
    assert status == "completed"
    assert not [a for a in _rows(conn, "SELECT payload_json FROM approval_requests")
                if json.loads(a["payload_json"]).get("mission_id") == mission]
    assert conn.execute(
        "SELECT count(*) FROM approval_requests WHERE status='pending'"
    ).fetchone()[0] == 1          # 上回った 1 本だけ
    # activity
    gate_failed = [l for l in _activity_lines(runs)
                   if f"gate_failed\tmission={mission} " in l]
    assert [l.split("\t")[3] for l in gate_failed] == [
        f"mission={mission} reason=not_improved"]
    # backlog の last_result / origin_outcome
    ((last_result, origin_outcome),) = conn.execute(
        "SELECT last_result, origin_outcome FROM improvement_backlog "
        "WHERE origin_mission_id=?", (mission,)).fetchall()
    assert (last_result, origin_outcome) == (
        "observation:not_submitted", "not_submitted")
    # 通知の固定語
    ((result_code, reason_code),) = conn.execute(
        "SELECT result_code, reason_code FROM improve_run_notices "
        "WHERE mission_id=?", (mission,)).fetchall()
    assert (result_code, reason_code) == ("gate_failed", "not_improved")
    # 親専有レポートに節がある
    (report,) = list((world.root / "data" / "improve_reports").glob(
        f"improve-*-{mission}.md"))
    text = report.read_text(encoding="utf-8")
    assert "reason: `not_improved`" in text
    assert "## Baseline comparison detail" in text
    # archive の outcome と baseline 行の mission_outcome が同時に観測される
    outcomes = {r["outcome_class"] for r in _rows(
        conn, "SELECT outcome_class FROM candidate_archives WHERE mission_id=?",
        mission)}
    assert outcomes == {"gate_failed"}
    base = {r["mission_outcome"] for r in _rows(
        conn, "SELECT mission_outcome FROM backtest_runs WHERE mission_id=? "
        "AND variant='baseline'", mission)}
    assert base == {"not_improved"}
    # 比較の activity (数値なし・hash 12 桁と固定語)
    (compare,) = [l for l in _activity_lines(runs)
                  if f"baseline_compare\tmission={mission} " in l]
    assert re.fullmatch(
        rf"IMPROVE\tbaseline_compare\tmission={mission} plugin=momo "
        r"mode=enforce result=not_improved detail=holdout_not_exceeds "
        rf"candidate=[0-9a-f]{{12}} base={runs.deployed[:12]}\t-",
        "\t".join(compare.split("\t")[1:]))


def test_repeat_does_not_count_not_improved_as_a_failure_and_never_backs_off(
        runs):
    result = runs.repeat_result
    assert (result.stop_reason, result.runs) == (ir.STOP_MAX_RUNS, 2)
    phases = [p.get("phase") for p in runs.repeat_progress]
    assert "backoff" not in phases
    assert phases.count("running") == 2
    first, second = (_entrance_missions(runs)["repeat-1"],
                     _entrance_missions(runs)["repeat-2"])
    statuses = [r[0] for r in runs.world.conn.execute(
        "SELECT status FROM missions WHERE id IN (?,?) ORDER BY id",
        (first, second))]
    assert statuses == ["completed", "completed"]
    # 2 本目は 1 本目の終了直後に始まる (BACKOFF_SEC を待たない)
    assert runs.timings["repeat"] < ir.BACKOFF_SEC / 2


def test_a_better_candidate_gets_an_approval_with_the_stored_comparison(runs):
    world = runs.world
    (row,) = _rows(world.conn,
                   "SELECT payload_json FROM approval_requests "
                   "WHERE status='pending'")
    payload = json.loads(row["payload_json"])
    assert payload["mission_id"] == runs.improved
    baseline = payload["baseline"]
    assert baseline["base_content_hash"] == runs.deployed
    comparison = baseline["comparison"]
    assert (comparison["mode"], comparison["verdict"],
            comparison["reason"]) == ("enforce", "improved",
                                      "holdout_exceeds")
    assert list(comparison["stages"]) == ["holdout", "in_sample"]
    stages = comparison["stages"]["holdout"]["USDJPY"]
    stored = {
        r["content_hash"]: json.loads(r["metrics_json"])
        for r in _rows(world.conn,
                       "SELECT content_hash, metrics_json FROM backtest_runs "
                       "WHERE mission_id=? AND scope='holdout_gate'",
                       runs.improved)}
    assert stages["candidate"]["pf"] == stored[payload["content_hash"]]["pf"]
    assert stages["deployed"]["pf"] == stored[runs.deployed]["pf"]
    assert stages["candidate"]["pf"] > stages["deployed"]["pf"]
    assert stages["candidate"]["avg_r"] > stages["deployed"]["avg_r"]
    assert stages["verdict"] == "improved"


# === 遮断 8: agent・通知の面に比較の値と語が出ない ==============================

AGENT_WORDS = ("baseline", "not_improved", "same_name_contract_changed",
               "in_sample", "holdout", "holdout_gate", "unavailable_rate")


def _stored_metrics(conn):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT metrics_json FROM backtest_runs")]


def _number_canaries(conn):
    """親専有の置き場にある実際の成績から、画面に出てはいけない文字列を作る。
    小数は全桁・2 桁・3 桁、取引数は単語境界つきの整数。"""
    text, trades = set(), set()
    for metrics in _stored_metrics(conn):
        for key in ("pf", "avg_r", "win_rate", "total_pnl"):
            value = metrics.get(key)
            if isinstance(value, float):
                text |= {repr(value), f"{value:.2f}", f"{value:.3f}"}
        value = metrics.get("max_drawdown")
        if isinstance(value, float):
            text.add(repr(value))
        trades.add(int(metrics["trades"]))
    assert len(text) >= 20 and len(trades) >= 4
    return text, trades


def _contains_trades(body, trades):
    return [t for t in trades if re.search(rf"(?<![\d.]){t}(?![\d.])", body)]


@pytest.fixture(scope="module")
def surfaces(runs):
    """全 mission が終端した後の、agent が読む面と通知先が読む面の本文。"""
    world = runs.world
    conn = world.conn
    mission_result = MissionResult(status="completed", output={}, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(
                   result=mission_result, **kw)):
        next_mission, next_ctx, _ = world.loop.prepare(
            slot_key=None, now=bw.NOW)
    ctx_data = build_improve_context(
        conn, settings=world.settings, now=bw.NOW, root=world.root,
        allowed_backlog_ids=None)

    def dump(sql):
        return json.dumps([dict(r) for r in conn.execute(sql)],
                          ensure_ascii=False, default=str)

    out = {
        "generated_prompt": next_mission.prompt,
        "improve_context": json.dumps(ctx_data, ensure_ascii=False,
                                      default=str),
        "family_summary": json.dumps(
            ctx_data["improvement_history"]["family_attempts"],
            ensure_ascii=False, default=str),
        "rendered_prompt": world.loop._render_improve_mission_prompt(
            ctx_data, ctx=next_ctx),
        "mission_output": dump("SELECT * FROM missions"),
        "backlog": dump("SELECT * FROM improvement_backlog"),
        "improvement_runs": dump("SELECT * FROM improvement_runs"),
    }
    world.loop.compensate_launch_failure(ctx=next_ctx, now=bw.NOW)
    return out


def test_agent_facing_surfaces_carry_no_number_from_the_comparison(
        runs, surfaces):
    text_canaries, trades = _number_canaries(runs.world.conn)
    for name, body in surfaces.items():
        assert body, f"{name} is empty; the surface was not exercised"
        for canary in text_canaries:
            assert canary not in body, f"{canary} leaked to {name}"
        assert _contains_trades(body, trades) == [], name


def test_agent_facing_surfaces_carry_no_stage_or_comparison_word(
        runs, surfaces):
    for name, body in surfaces.items():
        for word in AGENT_WORDS:
            assert word not in body, f"{word} leaked to {name}"


def test_the_words_and_numbers_do_exist_in_the_parent_only_places(runs):
    """上の不在検査が空振りでないことの対照。"""
    conn = runs.world.conn
    reports = "\n".join(p.read_text(encoding="utf-8") for p in sorted(
        (runs.world.root / "data" / "improve_reports").glob("improve-*.md")))
    assert "not_improved" in reports and "Baseline comparison detail" in reports
    assert "same_name_contract_changed" in reports
    text_canaries, _ = _number_canaries(conn)
    assert any(c in reports for c in text_canaries)
    notices = {tuple(r) for r in conn.execute(
        "SELECT result_code, reason_code FROM improve_run_notices")}
    assert ("gate_failed", "not_improved") in notices
    assert ("gate_failed", "same_name_contract_changed") in notices


def _notice_event(conn, mission_id):
    row = conn.execute(
        "SELECT trigger, result_code, reason_code, tool_calls "
        "FROM improve_run_notices WHERE mission_id=?", (mission_id,)).fetchone()
    return {"event_id": 1, "ts": "2026-03-01T00:00:00+00:00",
            "code": "improve_run_changed", "ref": f"improve:{mission_id}",
            "state": "finished", "trigger": row["trigger"],
            "result_code": row["result_code"], "reason_code": row["reason_code"],
            "tool_calls": row["tool_calls"]}


def _discord_body(runs, mission_id):
    world = runs.world
    dto = world.app.ops.improve_run(Principal.OPERATOR, mission_id)
    out = bw.afx_ops_render({
        "kind": "discord_finish", "event": _notice_event(world.conn, mission_id),
        "dto": json.loads(json.dumps(dto, default=str))})
    assert out["validated"] is True
    return out["text"], dto


def test_discord_body_and_activity_have_no_number_when_not_improved(runs):
    conn = runs.world.conn
    text_canaries, trades = _number_canaries(conn)
    # `backtest_cpu` の行は worker の CPU 秒を載せる別の記録で、成績ではない
    activity = "\n".join(l for l in _activity_lines(runs)
                         if "\tbacktest_cpu\t" not in l)
    for canary in text_canaries:
        assert canary not in activity
    assert _contains_trades(
        "\n".join(l.split("\t", 3)[3] for l in _activity_lines(runs)
                  if "\tbaseline_compare\t" in l), trades) == []
    for key in ("weekly", "manual", "repeat-1", "repeat-2"):
        mission = _entrance_missions(runs)[key]
        body, dto = _discord_body(runs, mission)
        assert "comparison" not in dto or dto["comparison"] is None
        assert body.splitlines()[1:2] == [
            "配備中の版を上回らなかったため見送ります"], body
        for canary in text_canaries:
            assert canary not in body
        assert _contains_trades(body, trades) == []
    body, _ = _discord_body(runs, runs.violation)
    assert "意味が変わる変更は新しい名前で提出する" in body
    assert _contains_trades(body, trades) == []


def test_discord_body_for_an_improved_mission_shows_the_four_holdout_items(
        runs):
    """GET の応答 (実 DB の payload から作った DTO) を afx-ops の描画に通す。"""
    conn = runs.world.conn
    body, dto = _discord_body(runs, runs.improved)
    stored = {r["content_hash"]: json.loads(r["metrics_json"]) for r in _rows(
        conn, "SELECT content_hash, metrics_json FROM backtest_runs "
        "WHERE mission_id=? AND scope='holdout_gate'", runs.improved)}
    (candidate_hash,) = [h for h in stored if h != runs.deployed]

    def line(label, m):
        return (f"USDJPY {label}: pf {m['pf']:.2f} avg_r {m['avg_r']:+.2f} "
                f"取引 {m['trades']} 評価可能 はい")

    lines = body.splitlines()
    assert "配備中の版を上回りました" in lines
    assert line("候補", stored[candidate_hash]) in lines
    assert line("配備中の版", stored[runs.deployed]) in lines
    # 出してよいのは holdout の pf・avg_r・取引数・評価可能だけ
    assert set(dto["comparison"]) == {"holdout"}
    entry = dto["comparison"]["holdout"]["USDJPY"]
    assert set(entry["candidate"]) == {"pf", "avg_r", "trades", "evaluable"}
    in_sample = {r["content_hash"]: json.loads(r["metrics_json"]) for r in _rows(
        conn, "SELECT content_hash, metrics_json FROM backtest_runs "
        "WHERE mission_id=? AND scope='in_sample'", runs.improved)}
    for metrics in in_sample.values():
        for key in ("pf", "avg_r", "win_rate", "max_drawdown", "total_pnl"):
            assert repr(metrics[key]) not in json.dumps(dto)


# === 監視の集計器と activity の件数 =============================================

def test_the_monitor_section_counts_terminated_missions_and_says_so(runs):
    """集計節は当該 mission を含まない。節の固定文がそれを言い、件数は
    その時点までに終端した mission の baseline_compare 行と一致する。"""
    world = runs.world
    reports = {}
    for path in (world.root / "data" / "improve_reports").glob("improve-*.md"):
        mission = int(path.stem.rsplit("-", 1)[1])
        reports[mission] = path.read_text(encoding="utf-8")
    lines = _activity_lines(runs)
    compare_missions = [int(re.search(r"mission=(\d+)", l).group(1))
                        for l in lines if "\tbaseline_compare\t" in l]
    assert compare_missions == sorted(compare_missions) == [1, 2, 3, 4, 5]
    with_section = {m: t for m, t in reports.items()
                    if "## Baseline monitoring" in t}
    assert sorted(with_section) == [1, 2, 3, 4, 6]       # 提出した 5 は親レポート無し
    for mission, text in with_section.items():
        section = text.split("## Baseline monitoring", 1)[1]
        assert "Counts cover missions committed before this one." in section
        before = [m for m in compare_missions if m < mission]
        if not before:
            assert "No comparison attempts recorded." in section
            continue
        (attempts,) = re.findall(r"momo: attempts=(\d+)", section)
        assert int(attempts) == len(before), (mission, section)
        hashes = re.findall(r"candidate=([0-9a-f]{12}) base=", section)
        logged = [re.search(r"candidate=([0-9a-f]{12})", l).group(1)
                  for l in lines if "\tbaseline_compare\t" in l
                  and int(re.search(r"mission=(\d+)", l).group(1)) < mission]
        assert hashes == logged
    # 最後の集計は、終端した全 mission 分
    (summary,) = baseline_monitor.monitor_summary(world.conn)
    assert summary["attempts"] == len(compare_missions)
    assert [h["candidate"] for h in summary["by_candidate_hash"]] == [
        re.search(r"candidate=([0-9a-f]{12})", l).group(1)
        for l in lines if "\tbaseline_compare\t" in l]
    # 契約違反の mission は比較行を持たず、集計にも数えない
    assert world.conn.execute(
        "SELECT count(*) FROM backtest_runs WHERE mission_id=?",
        (runs.violation,)).fetchone()[0] == 0


# === 承認詳細の 3 面 =============================================================

def test_cli_shell_and_web_show_the_same_stored_values(runs):
    world = runs.world
    conn = world.conn
    (approval_id,) = [r[0] for r in conn.execute(
        "SELECT id FROM approval_requests WHERE status='pending'")]
    data = world.app.ops.approval_detail(Principal.OPERATOR, approval_id)
    comparison = data["baseline"]["comparison"]
    cli = ops_client.format_approval_detail(data)
    web = bw.afx_ops_render({
        "kind": "web_approval",
        "data": json.loads(json.dumps(data, default=str))})["text"]
    from agentic_fx.commands import Commands
    shell = Commands._approval_detail(_shell(world), approval_id)
    base12 = runs.deployed[:12]
    for surface, name in ((cli, "cli"), (shell, "shell"), (web, "web")):
        assert base12 in surface, name
        assert "配備中の版 (同条件で再評価)" in surface, name
        assert "改善" in surface, name
        for stage in ("in_sample", "holdout"):
            entry = comparison["stages"][stage]["USDJPY"]
            for version in ("candidate", "deployed"):
                metrics = entry[version]
                for key in ("pf", "trades", "win_rate", "avg_r",
                            "max_drawdown", "total_pnl"):
                    assert str(metrics[key]) in surface, (name, stage, version, key)
    # 8 指標のうち kill_switch_latches も、保存された実値のまま 3 面に出る
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()[0])
    stored = {
        (r["scope"], r["content_hash"]): json.loads(r["metrics_json"])
        for r in _rows(conn, "SELECT scope, content_hash, metrics_json FROM "
                       "backtest_runs WHERE mission_id=?", runs.improved)}
    for stage, scope in (("in_sample", "in_sample"),
                         ("holdout", "holdout_gate")):
        entry = comparison["stages"][stage]["USDJPY"]
        for version, chash in (("candidate", payload["content_hash"]),
                               ("deployed", runs.deployed)):
            expected = stored[(scope, chash)]["kill_switch_latches"]
            assert entry[version].get("kill_switch_latches") == expected, (
                stage, version)
    for surface in (cli, shell):
        for line in surface.splitlines():
            if "候補:" in line or "配備中の版:" in line:
                assert "kill_switch_latches=-" not in line, line
    assert "<th>kill_switch_latches</th>" in web
    assert "<td></td>" not in web


def _shell(world):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.health_latch import HealthLatch
    from agentic_fx.core.paper_broker import PaperBroker
    from agentic_fx.store.state import StateStore
    from unittest.mock import MagicMock
    return Commands(
        conn=world.conn, state_store=StateStore(world.root / "shell-state.json"),
        broker=PaperBroker(world.conn, world.settings, world.app.clock),
        trade_loop=MagicMock(), activity=world.app.activity,
        log_dir=world.root / "logs", clock=world.app.clock,
        health_latch=HealthLatch(), plugins_root=world.root / "plugins",
        settings=world.settings)

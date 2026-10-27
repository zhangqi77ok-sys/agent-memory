"""第 41 轮：千万级 50ms 证明的机制钉子（小测量 + CAP 界定）。

计时断言只属于证明脚本（scripts/brief_scale_proof.py），单测里不断言
墙上时间（VM 负载抖动必误伤）；这里钉的是界本身：各臂上限常数、
锚点探针的有界性与回落、探针输出截断、判定函数与摘要结构。
PG-only 用例在 SQLite 套件里 skip（锚点探针只走 Postgres）。
"""

import uuid

import pytest
from sqlalchemy import or_, text

from app import models as m
from app.auth import UserContext
from app.episodes import brief_for_task
from app.recall import (
    _ANCHOR_CAP,
    _PROBE_CAP,
    _VECTOR_CANDIDATES,
    _anchor_probe,
    _probe_arm,
    _QueryPlan,
)
from app.tokenize import tokens_of
from scripts.brief_scale_proof import (
    APP_ID,
    USER_ID,
    build_summary,
    loglog_slope,
    percentile,
    seed,
    verdict,
)

TOP_QUERY = "部署服务时端口被占用怎么处理"
U_PROOF = UserContext(app_id=APP_ID, user_id=USER_ID)


def _dialect(db) -> str:
    return db.get_bind().dialect.name


def _pg_only(db):
    if _dialect(db) != "postgresql":
        pytest.skip("anchor probe is PG-only")


def _seed_same(db, n: int, subject: str, content: str, applies: str) -> None:
    """n 条同一主题文本的行（直接 SQL，不走 ORM 事件，速度快）。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ins = text(
        "INSERT INTO memory_insight (id, app_id, user_id, kind, subject, content,"
        " applies_when, valid_from, valid_to, version, source_episode_id, project,"
        " confidence, use_count, success_count, feedback_pos, feedback_neg,"
        " last_used_at, decayed_until, verified, agent, evidence_chained,"
        " pinned, muted, applied_count, auto_pos, auto_neg)"
        " VALUES (:id, :app_id, :user_id, 'lesson', :subject, :content, :applies,"
        " :now, NULL, 1, NULL, NULL, 0.5, 0, 0, 0, 0, NULL, NULL, TRUE, NULL, FALSE,"
        " FALSE, FALSE, 0, 0, 0)")
    tok = text(
        "INSERT INTO memory_insight_token (insight_id, token, app_id, user_id)"
        " VALUES (:insight_id, :token, :app_id, :user_id)")
    toks = tokens_of(f"{subject} {content} {applies}")
    db.execute(ins, [{"id": uuid.uuid4().hex, "app_id": APP_ID, "user_id": USER_ID,
                      "subject": subject, "content": content, "applies": applies,
                      "now": now} for _ in range(n)])
    db.execute(tok, [{"insight_id": r[0], "token": t, "app_id": APP_ID,
                      "user_id": USER_ID}
                     for r in db.execute(
                         text("SELECT id FROM memory_insight")).fetchall()
                     for t in toks])
    db.commit()


def _scope():
    return (m.MemoryInsight.app_id == APP_ID, m.MemoryInsight.user_id == USER_ID,
            m.MemoryInsight.valid_to.is_(None),
            or_(m.MemoryInsight.muted.is_(False), m.MemoryInsight.muted.is_(None)))


def _cols():
    return [m.MemoryInsight.id, m.MemoryInsight.subject,
            m.MemoryInsight.content, m.MemoryInsight.applies_when]


def test_anchor_cap_is_100():
    assert _ANCHOR_CAP == 100


def test_probe_cap_is_2000():
    assert _PROBE_CAP == 2000


def test_vector_cap_is_200():
    assert _VECTOR_CANDIDATES == 200


def test_analogy_caps_are_200():
    from app.analogy import ANALOGY_CANDIDATE_CAP, ANALOGY_PER_MOVE
    assert ANALOGY_PER_MOVE == 200
    assert ANALOGY_CANDIDATE_CAP == 200


def test_anchor_probe_bounded_pg(db):
    _pg_only(db)
    seed(db, 1000)  # 主导主题约 250 行，过门行远超 CAP
    rows = _anchor_probe(db, m.MemoryInsight, m.MemoryInsightToken, "insight_id",
                         _cols(), _scope(), _QueryPlan(TOP_QUERY))
    assert rows is not None
    assert len(rows) <= _ANCHOR_CAP
    assert all(hasattr(r, "kw_hits") and r.kw_hits >= 2 for r in rows)


def test_anchor_probe_falls_back_when_insufficient_pg(db):
    _pg_only(db)
    # 回落条件是过门行 < CAP（不是主导主题行数）：seed 模板每行都含
    # “占用/处理”通用词，对本查询（门槛 hits ≥ 2）全部过门，故总量
    # 必须 < CAP 才能测到“找不够”；seed 100 行时恰好攒够 CAP 返回行，
    # 是机制正确行为，不是产品 bug（PG 全量第 3 跑实锤）。
    seed(db, 10)  # 总量 10 行 < CAP=100，过门行再多也找不够
    rows = _anchor_probe(db, m.MemoryInsight, m.MemoryInsightToken, "insight_id",
                         _cols(), _scope(), _QueryPlan(TOP_QUERY))
    assert rows is None  # 调用方回落精确探针，正确优先


def test_probe_output_capped_at_2000(db):
    _seed_same(db, 2100, "部署服务端口占用处理经验",
               "部署服务端口占用时先定位占用方再处理", "当部署服务端口占用发生时")
    stmt = _probe_arm(m.MemoryInsight, m.MemoryInsightToken, "insight_id",
                      _cols(), _scope(), _QueryPlan("部署服务端口占用"))
    assert stmt is not None
    rows = db.execute(stmt).fetchall()
    assert 0 < len(rows) <= _PROBE_CAP


def test_brief_winners_bounded_pg(db):
    _pg_only(db)
    seed(db, 1000)
    brief = brief_for_task(db, U_PROOF, TOP_QUERY, 5)
    assert 0 < len(brief) <= 5


def test_percentile_values():
    assert percentile([10, 20, 30, 40], 0.5) == 25.0
    assert percentile([5], 0.95) == 5
    assert percentile([], 0.5) == 0.0
    assert percentile([1, 2, 3, 4], 0.25) == 1.75


def test_loglog_slope_flat_and_linear():
    assert abs(loglog_slope([100, 1000, 10000], [10.0, 10.0, 10.0])) < 1e-9
    assert abs(loglog_slope([100, 1000, 10000], [10.0, 100.0, 1000.0]) - 1.0) < 1e-9
    assert loglog_slope([100], [10.0]) == 0.0


def test_verdict_pass_and_fail():
    ok = verdict("postgresql", [1000, 10000], {1000: 8.0, 10000: 9.0})
    assert ok["pass"] is True
    over_budget = verdict("postgresql", [1000, 10000], {1000: 8.0, 10000: 60.0})
    assert over_budget["pass"] is False
    # p50 未超但斜率超 0.5（4ms → 45ms，100 倍规模）：只判斜率这一项
    steep = verdict("postgresql", [1000, 100000], {1000: 4.0, 100000: 45.0})
    assert steep["pass"] is False and "slope" in steep["reason"]


def test_verdict_sqlite_not_applicable():
    v = verdict("sqlite", [1000, 10000], {1000: 8.0, 10000: 9.0})
    assert v["pass"] is None


def test_build_summary_structure():
    s = build_summary("postgresql", {"top": {1000: {"p50_ms": 8.0, "p95_ms": 12.0,
                                                   "max_ms": 15.0}}})
    assert s["dialect"] == "postgresql"
    assert s["scales"] == [1000]
    assert s["queries"]["top"][1000]["p50_ms"] == 8.0
    assert s["verdict"]["top"]["pass"] is True


def test_brief_keyword_only_degraded(db):
    meta: dict = {}
    brief = brief_for_task(db, U_PROOF, "今天股票行情怎么样", 5, meta=meta)
    assert brief == []
    assert meta.get("degraded") is True


def test_proof_script_has_analyze_flag():
    """--analyze 选项存在：播种后 ANALYZE，测的是生产常驻的已分析稳态，
    而不是刚 bulk 灌入、规划器尚无统计量的新鲜库（第 41 轮诊断出的口径差）。"""
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "brief_scale_proof.py"
    out = subprocess.run([sys.executable, str(script), "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert "--analyze" in out.stdout


def test_large_bucket_brief_still_correct(db):
    _seed_same(db, 2100, "部署服务端口占用处理经验",
               "部署服务端口占用时先定位占用方再处理", "当部署服务端口占用发生时")
    brief = brief_for_task(db, U_PROOF, TOP_QUERY, 5)
    assert 0 < len(brief) <= 5
    assert all("端口占用" in (b.subject + b.content) for b in brief)

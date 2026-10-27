"""第 41 轮规模证明：小规模多点测量 + CAP 界定外推（不灌千万行）。

用法（证明测量，目标 30 分钟内跑完）：
    # PG 临时集群（先 alembic upgrade head 建表，再跑本脚本）：
    DATABASE_URL=postgresql+psycopg://memory@127.0.0.1:55435/memory_proof \\
        python3 scripts/brief_scale_proof.py --scales 1000,10000,100000
    # SQLite 文件库（脚本自己 create_all 建表）：
    DATABASE_URL=sqlite:////tmp/brief_proof.db \\
        python3 scripts/brief_scale_proof.py --scales 1000,10000,50000 --init

口径（与第 20 轮一致）：纯关键词路径（embedder=None），k=5，iters=20，
热身 3 次后计时；计时含简报记账 commit（真实 brief 成本的一部分）。
判定只对 PG 生产路径：最大规模点 p50 < 50ms 且 log-log 斜率 < 0.5；
SQLite 侧只报告数字，不参与判定（SQLite 探针 stage-1 与命中桶成正比，
不在同一界内，见 docs/eval.md 第 41 轮章节）。

播种口径：直接 SQL 批量插入（不用 ORM 逐行事件，保证速度），token 行
用 app.tokenize.tokens_of 对打分文本（subject + content + applies_when）
取集，与 ORM 维护的倒排口径一致。主题分布仿第 20 轮 Zipf 形，主导主题
约 25%，使命中桶随 N 同比放大——桶放大而耗时不放大才是有效的证伪实验。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_ID = "tiancode"
USER_ID = "proof-user"

# (主题词, 占比)：主导主题约 25%，次主题约 16%，其余长尾。
TOPICS = [
    ("部署服务端口占用", 0.25),
    ("缓存热点重建", 0.16),
    ("数据库连接池耗尽", 0.12),
    ("消息队列积压", 0.10),
    ("磁盘写满清理", 0.09),
    ("证书过期续签", 0.08),
    ("配置漂移回滚", 0.07),
    ("定时任务重复执行", 0.06),
    ("接口超时重试", 0.04),
    ("日志采样降噪", 0.03),
]

# 查询：top 命中主导主题大桶，mid 命中次主题桶（与第 20 轮同形）。
QUERIES = {
    "top": "部署服务时端口被占用怎么处理",
    "mid": "缓存被打穿热点重建怎么办",
}

BUDGET_MS = 50.0
MAX_SLOPE = 0.5
ITERS = 20
WARMUP = 3


def percentile(times: list[float], q: float) -> float:
    """线性插值分位数（q in [0, 1]）；空序列返回 0.0。"""
    if not times:
        return 0.0
    ordered = sorted(times)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def loglog_slope(ns: list[int], times_ms: list[float]) -> float:
    """log(耗时) 对 log(规模) 的最小二乘斜率；点数 < 2 或规模无变化时返回 0.0。

    斜率 ≈ 0 表示耗时与规模无关（CAP 界成立）；≈ 1 表示耗时随规模线性涨。
    """
    if len(ns) < 2 or len(set(ns)) < 2:
        return 0.0
    xs = [math.log(n) for n in ns]
    ys = [math.log(max(t, 1e-9)) for t in times_ms]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def verdict(dialect: str, scales: list[int], p50_ms: dict[int, float]) -> dict:
    """PG 侧判定；SQLite 侧不判定（口径外），只返回 not_applicable。

    PASS 当且仅当：最大规模点 p50 < 50ms 且 log-log 斜率 < 0.5。
    """
    if dialect != "postgresql":
        return {"pass": None, "reason": "not_applicable: sqlite probe is bucket-proportional, out of scope"}
    ordered = sorted(p50_ms)
    max_n = max(ordered) if ordered else 0
    slope = loglog_slope(ordered, [p50_ms[n] for n in ordered])
    reasons = []
    ok = True
    if not ordered or p50_ms.get(max_n, float("inf")) >= BUDGET_MS:
        ok = False
        reasons.append(f"p50 at N={max_n} >= {BUDGET_MS}ms")
    if slope >= MAX_SLOPE:
        ok = False
        reasons.append(f"log-log slope {slope:.3f} >= {MAX_SLOPE}")
    if ok:
        reasons.append(f"p50 at N={max_n} < {BUDGET_MS}ms and slope {slope:.3f} < {MAX_SLOPE}")
    return {"pass": ok, "slope": round(slope, 4), "reason": "; ".join(reasons)}


def build_summary(dialect: str, results: dict) -> dict:
    """results: {query: {N: {"p50_ms":..,"p95_ms":..,"max_ms":..}}}。"""
    out: dict = {"dialect": dialect, "scales": sorted({n for q in results.values() for n in q}),
                 "queries": results, "verdict": {}}
    for query, per_n in results.items():
        out["verdict"][query] = verdict(
            dialect, sorted(per_n), {n: v["p50_ms"] for n, v in per_n.items()})
    return out


def _topic_rows(n: int):
    """按 TOPICS 占比切分 n 行，返回 [(主题词, 行内序号)]。"""
    rows = []
    used = 0
    for topic, share in TOPICS:
        count = int(n * share) if topic != TOPICS[-1][0] else n - used
        for i in range(count):
            rows.append((topic, used + i))
        used += count
    return rows


def seed(session, n: int) -> None:
    """批量播种 n 条经验 + 倒排行（直接 SQL，不走 ORM 事件）。"""
    from sqlalchemy import text

    from app.tokenize import tokens_of

    now = datetime.now(timezone.utc)
    insight_sql = text(
        "INSERT INTO memory_insight (id, app_id, user_id, kind, subject, content,"
        " applies_when, valid_from, valid_to, version, source_episode_id, project,"
        " confidence, use_count, success_count, feedback_pos, feedback_neg,"
        " last_used_at, decayed_until, verified, agent, evidence_chained,"
        " pinned, muted, applied_count, auto_pos, auto_neg)"
        " VALUES (:id, :app_id, :user_id, 'lesson', :subject, :content, :applies_when,"
        " :now, NULL, 1, NULL, NULL, 0.5, 0, 0, 0, 0, NULL, NULL, TRUE, NULL, FALSE,"
        " FALSE, FALSE, 0, 0, 0)")
    token_sql = text(
        "INSERT INTO memory_insight_token (insight_id, token, app_id, user_id)"
        " VALUES (:insight_id, :token, :app_id, :user_id)")
    insight_params = []
    token_params = []
    for topic, i in _topic_rows(n):
        iid = uuid.uuid4().hex
        subject = f"{topic}处理经验"
        content = f"{topic}时先定位占用方再处理，处理完验证恢复，不要直接重启了事{i}"
        applies = f"当{topic}发生时"
        insight_params.append({"id": iid, "app_id": APP_ID, "user_id": USER_ID,
                               "subject": subject, "content": content,
                               "applies_when": applies, "now": now})
        for tok in tokens_of(f"{subject} {content} {applies}"):
            token_params.append({"insight_id": iid, "token": tok,
                                 "app_id": APP_ID, "user_id": USER_ID})
    session.execute(insight_sql, insight_params)
    session.execute(token_sql, token_params)
    session.commit()


def clear(session) -> None:
    from sqlalchemy import text
    try:
        session.rollback()
    except Exception:
        pass
    try:
        session.execute(text("DELETE FROM memory_insight_token"))
        session.execute(text("DELETE FROM memory_insight"))
        session.execute(text("DELETE FROM brief_delivery"))
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
        raise


def measure(session, n: int, iters: int = ITERS, dialect: str = "sqlite") -> dict:
    """在当前库规模下测 top/mid 两条查询的 brief 耗时（毫秒）。

    PG 侧同时单测锚点探针耗时（probe_p50_ms），用于区分“探针变慢”
    与“简报其它环节（含 commit）变慢”；SQLite 不走锚点探针，不测此项。
    """
    from app.auth import UserContext
    from app.episodes import brief_for_task

    u = UserContext(app_id=APP_ID, user_id=USER_ID)
    out = {}
    for name, query in QUERIES.items():
        for _ in range(WARMUP):
            brief_for_task(session, u, query, 5)
        times = []
        for _ in range(iters):
            t0 = time.perf_counter()
            brief_for_task(session, u, query, 5)
            times.append((time.perf_counter() - t0) * 1000.0)
        row = {n: {"p50_ms": round(percentile(times, 0.5), 2),
                   "p95_ms": round(percentile(times, 0.95), 2),
                   "max_ms": round(max(times), 2)}}
        if dialect == "postgresql":
            from sqlalchemy import or_

            from app import models as m
            from app.recall import _anchor_probe, _QueryPlan
            scope = (m.MemoryInsight.app_id == APP_ID,
                     m.MemoryInsight.user_id == USER_ID,
                     m.MemoryInsight.valid_to.is_(None),
                     or_(m.MemoryInsight.muted.is_(False),
                         m.MemoryInsight.muted.is_(None)))
            cols = [m.MemoryInsight.id, m.MemoryInsight.subject,
                    m.MemoryInsight.content, m.MemoryInsight.applies_when]
            plan = _QueryPlan(query)
            probe_times = []
            for _ in range(iters):
                t0 = time.perf_counter()
                _anchor_probe(session, m.MemoryInsight, m.MemoryInsightToken,
                              "insight_id", cols, scope, plan)
                probe_times.append((time.perf_counter() - t0) * 1000.0)
            row[n]["probe_p50_ms"] = round(percentile(probe_times, 0.5), 2)
            row[n]["probe_p95_ms"] = round(percentile(probe_times, 0.95), 2)
        out[name] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="brief scale proof (small multi-point)")
    ap.add_argument("--scales", default="1000,10000,100000",
                    help="comma-separated insight counts")
    ap.add_argument("--iters", type=int, default=ITERS)
    ap.add_argument("--init", action="store_true",
                    help="create tables via Base.metadata.create_all (SQLite path)")
    ap.add_argument("--analyze", action="store_true",
                    help="PG only: ANALYZE the insight/token tables after each seed, "
                    "so the measurement reflects the analyzed steady state that "
                    "production (autovacuum on) stays in. A freshly bulk-loaded "
                    "cluster measured before any ANALYZE leaves the planner "
                    "without statistics and picks slower plans; that state is "
                    "reported separately, never silently mixed into the verdict.")
    args = ap.parse_args()
    scales = [int(s) for s in args.scales.split(",") if s.strip()]

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.environ["DATABASE_URL"]
    engine = create_engine(url)
    dialect = engine.dialect.name
    if args.init:
        import app.models  # noqa: F401  (register tables on Base.metadata)
        from app.db import Base
        Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    results: dict = {name: {} for name in QUERIES}
    errors: dict = {}
    try:
        for n in scales:
            try:
                clear(session)
                t0 = time.perf_counter()
                seed(session, n)
                seed_s = time.perf_counter() - t0
                if args.analyze and dialect == "postgresql":
                    from sqlalchemy import text as _text
                    session.execute(_text("ANALYZE memory_insight"))
                    session.execute(_text("ANALYZE memory_insight_token"))
                    session.commit()
                per_query = measure(session, n, iters=args.iters, dialect=dialect)
            except Exception as exc:  # noqa: BLE001 — 单规模点失败记下来，不掩盖
                errors[str(n)] = f"{type(exc).__name__}: {exc}"
                print(f"N={n}: ERROR {errors[str(n)]}", flush=True)
                break
            for name, per_n in per_query.items():
                results[name].update(per_n)
            print(f"N={n}: seeded in {seed_s:.1f}s, "
                  + ", ".join(f"{name} p50={per_query[name][n]['p50_ms']}ms"
                              for name in QUERIES), flush=True)
    finally:
        try:
            clear(session)
        except Exception:
            pass
        session.close()
        engine.dispose()
    summary = build_summary(dialect, results)
    if errors:
        summary["errors"] = errors
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if errors:
        return 1
    verdicts = summary["verdict"]
    if dialect == "postgresql":
        return 0 if all(v.get("pass") for v in verdicts.values()) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

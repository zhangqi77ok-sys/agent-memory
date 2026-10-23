#!/usr/bin/env python3
"""简报延迟基准（第十二轮）：在指定规模的经验库上量 brief_for_task 的耗时分布。

为什么需要：对外讲性能要有数字；对内要知道全表扫描式召回在哪个量级开始变慢
（第十二轮路线图里索引化召回的触发线）。本脚本用临时 SQLite 库造数、纯关键词
路径（无嵌入服务），数字只代表本机本量级，别拿去和别人的托管服务 p50 直接比。

用法：python3 scripts/bench_brief.py [--sizes 100,1000,5000] [--iters 30]
"""
import argparse
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def bench(size: int, iters: int) -> dict:
    tmp = tempfile.mkdtemp(prefix="bench-brief-")
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/bench.db"
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("SERVICE_KEYS", "bench")
    # 环境变量必须在 import app 之前就位（Settings 在导入时读取）——本脚本独立进程，没问题。
    from sqlalchemy.orm import sessionmaker

    from app.auth import UserContext
    from app.config import settings
    from app.db import Base, engine
    from app.episodes import brief_for_task
    from app.models import MemoryInsight

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    subjects = ["部署端口", "数据库迁移", "缓存策略", "限流重试", "日志排障",
                "构建优化", "权限配置", "备份恢复", "监控告警", "依赖升级"]
    kinds = ["lesson", "strategy", "pitfall", "cost_saver"]
    rows = []
    for i in range(size):
        s = subjects[i % len(subjects)]
        rows.append(MemoryInsight(
            app_id=settings.app_id, user_id="bench", kind=kinds[i % 4],
            subject=f"{s}{i // len(subjects)}",
            content=f"{s}场景下的第 {i} 条经验：先检查现状再动手，失败时保留现场信息，"
                    f"避免重复试错；完成后记录关键参数与回滚办法。",
            applies_when=f"处理{s}相关任务时"))
    db.add_all(rows)
    db.commit()
    u = UserContext(app_id=settings.app_id, user_id="bench")
    times = []
    hits = 0
    for _ in range(iters):
        t0 = time.perf_counter()
        out = brief_for_task(db, u, "部署服务时端口被占用怎么处理", 5)
        times.append((time.perf_counter() - t0) * 1000)
        hits += len(out)
    times.sort()
    return {"size": size, "iters": iters,
            "p50_ms": round(statistics.median(times), 2),
            "p95_ms": round(times[int(len(times) * 0.95) - 1], 2),
            "max_ms": round(times[-1], 2), "avg_hits": round(hits / iters, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100,1000,5000")
    ap.add_argument("--iters", type=int, default=30)
    args = ap.parse_args()
    for size in [int(s) for s in args.sizes.split(",") if s.strip()]:
        r = bench(size, args.iters)
        print(f"insights={r['size']:>6}  iters={r['iters']}  "
              f"p50={r['p50_ms']}ms  p95={r['p95_ms']}ms  max={r['max_ms']}ms  "
              f"avg_hits={r['avg_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

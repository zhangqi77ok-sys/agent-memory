#!/usr/bin/env python3
"""简报延迟基准（第十二轮建，第十四轮加典型/最坏两种语料）：量 brief_for_task 的耗时分布。

为什么需要：对外讲性能要有数字；对内要知道召回在哪个量级开始变慢。
本脚本用临时 SQLite 库造数、纯关键词路径（无嵌入服务），数字只代表本机本量级，
别拿去和别人的托管服务 p50 直接比。

两种语料（第十四轮补）：
- 典型（默认）：10 个主题各自有独立词汇的内容与适用情境——真实经验库的样子
  （提炼出的经验按主题天然分散）。查询只与一个主题相关，索引化召回的关键词
  通道能把候选压到该主题的量级。
- 最坏（--worst-case）：第十二轮的原始造数——所有行共用同一套模板正文，
  且固定查询与每行的「适用情境」都共享 bigram，于是每一行都过关键词门槛，
  通道压不掉任何候选，量的是「全员命中后逐行精确打分」的线性地板。
  真实语料不会长这样，保留它是为了不让数字只挑好看的报。

用法：python3 scripts/bench_brief.py [--sizes 100,1000,5000] [--iters 30] [--worst-case]
"""
import argparse
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 典型语料：每个主题一套自己的说法（正文与适用情境都按主题写，不共用模板句）
TOPICS = [
    ("部署端口", "上线前先用 ss 查监听占用，冲突就换端口或停掉旧进程，改完同步更新反向代理配置并验证健康检查通过。",
     "发布服务、启动新实例时"),
    ("数据库迁移", "改表结构前先在影子库跑一遍迁移脚本，确认锁表时间可接受；大表加列用在线 DDL，分批回填后再切流量。",
     "升级表结构、跑迁移时"),
    ("缓存策略", "热点键设置随机过期抖动防雪崩，更新走先删缓存再写库；穿透用空值短缓存，容量按内存水位留三成余量。",
     "设计缓存、排查命中率低时"),
    ("限流重试", "下游抖动时用指数退避加随机抖动重试，最多三次；令牌桶限流要给突发流量留余量，熔断半开探测别太密。",
     "调用不稳定接口、批量请求时"),
    ("日志排障", "先按 trace_id 串起整条链路，再看错误码分布；磁盘写满先清旧日志，定位时保留现场快照别急着重启。",
     "线上报错、定位故障时"),
    ("构建优化", "依赖层与源码层分开缓存，流水线并行跑静态检查；镜像瘦身用多阶段构建，只拷贝运行期产物。",
     "构建变慢、优化流水线时"),
    ("权限配置", "新成员按最小权限开通，密钥放保险库不进代码仓；离职当天回收全部令牌，审计日志保留九十天。",
     "开通账号、分配权限时"),
    ("备份恢复", "全量备份每周一次、增量每天，恢复演练每季度做；备份文件异地存一份，校验和对不上立即报警。",
     "制定备份计划、灾难恢复时"),
    ("监控告警", "告警阈值按历史分位设，别拍脑袋；同类告警合并发送，值班表与升级路径写进运行手册。",
     "配置监控、告警太多时"),
    ("依赖升级", "先看变更日志里的破坏性改动，小版本灰度一周再全量；锁文件提交进仓库，回滚点保留两个大版本。",
     "升级第三方库、框架大版本时"),
]
QUERY = "部署服务时端口被占用怎么处理"


def bench(size: int, iters: int, worst_case: bool = False) -> dict:
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
    kinds = ["lesson", "strategy", "pitfall", "cost_saver"]
    rows = []
    for i in range(size):
        s, body, applies = TOPICS[i % len(TOPICS)]
        if worst_case:
            content = (f"{s}场景下的第 {i} 条经验：先检查现状再动手，失败时保留现场信息，"
                       f"避免重复试错；完成后记录关键参数与回滚办法。")
            applies = f"处理{s}相关任务时"
        else:
            content = f"第 {i // len(TOPICS)} 次遇到{s}：{body}"
        rows.append(MemoryInsight(
            app_id=settings.app_id, user_id="bench", kind=kinds[i % 4],
            subject=f"{s}{i // len(TOPICS)}", content=content, applies_when=applies))
    db.add_all(rows)
    db.commit()
    u = UserContext(app_id=settings.app_id, user_id="bench")
    times = []
    hits = 0
    for _ in range(iters):
        t0 = time.perf_counter()
        out = brief_for_task(db, u, QUERY, 5)
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
    ap.add_argument("--worst-case", action="store_true",
                    help="用第十二轮的共用模板语料（全员过门槛的最坏情形）")
    args = ap.parse_args()
    label = "worst-case" if args.worst_case else "typical"
    for size in [int(s) for s in args.sizes.split(",") if s.strip()]:
        r = bench(size, args.iters, args.worst_case)
        print(f"[{label}] insights={r['size']:>6}  iters={r['iters']}  "
              f"p50={r['p50_ms']}ms  p95={r['p95_ms']}ms  max={r['max_ms']}ms  "
              f"avg_hits={r['avg_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

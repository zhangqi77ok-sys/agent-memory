#!/usr/bin/env python3
"""1M 行 PG 简报压测（第二十轮 50ms 规模验证）。

语料：32 个主题按 Zipf 权重分布（1/(k+1)），主题 0 约占 24.6%——
真实经验库的头部主题就长这样。查询分两种：头部主题查询（最坏真实桶）
与中部主题查询。纯关键词路径（无嵌入服务），量 brief_for_task 端到端。

播种走 COPY（ORM 事件逐行维护倒排要几小时，COPY 直接写两张表，
token 集用 app.tokenize.tokens_of 按模型口径现算，与 ORM 事件同口径）。

用法：
  DATABASE_URL=postgresql+psycopg://memory@127.0.0.1:55434/memory_bench \
    python3 scripts/bench_brief_pg.py --size 1000000 --iters 20 [--seed-only|--bench-only]
"""
import argparse
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "bench")

TOPICS = [
    ("部署端口", "上线前先用 ss 查监听占用，冲突就换端口或停掉旧进程，改完同步更新反向代理配置并验证健康检查通过。", "发布服务、启动新实例时"),
    ("数据库迁移", "改表结构前先在影子库跑一遍迁移脚本，确认锁表时间可接受；大表加列用在线 DDL，分批回填后再切流量。", "升级表结构、跑迁移时"),
    ("缓存策略", "热点键设置随机过期抖动防雪崩，更新走先删缓存再写库；穿透用空值短缓存，容量按内存水位留三成余量。", "设计缓存、排查命中率低时"),
    ("限流重试", "下游抖动时用指数退避加随机抖动重试，最多三次；令牌桶限流要给突发流量留余量，熔断半开探测别太密。", "调用不稳定接口、批量请求时"),
    ("日志排障", "先按 trace_id 串起整条链路，再看错误码分布；磁盘写满先清旧日志，定位时保留现场快照别急着重启。", "线上报错、定位故障时"),
    ("构建优化", "依赖层与源码层分开缓存，流水线并行跑静态检查；镜像瘦身用多阶段构建，只拷贝运行期产物。", "构建变慢、优化流水线时"),
    ("权限配置", "新成员按最小权限开通，密钥放保险库不进代码仓；离职当天回收全部令牌，审计日志保留九十天。", "开通账号、分配权限时"),
    ("备份恢复", "全量备份每周一次、增量每天，恢复演练每季度做；备份文件异地存一份，校验和对不上立即报警。", "制定备份计划、灾难恢复时"),
    ("监控告警", "告警阈值按历史分位设，别拍脑袋；同类告警合并发送，值班表与升级路径写进运行手册。", "配置监控、告警太多时"),
    ("依赖升级", "先看变更日志里的破坏性改动，小版本灰度一周再全量；锁文件提交进仓库，回滚点保留两个大版本。", "升级第三方库、框架大版本时"),
    ("容器编排", "副本数按峰值流量设，探针分存活与就绪两种；滚动更新设最大不可用一副本，亲和性别把同服务挤到一台宿主机。", "扩缩容、发布新版本时"),
    ("服务网格", "超时与重试预算写进网格策略，熔断阈值按错误率滑动窗口算；东西流量先上观测再上管控，别一上来就全量拦截。", "治理服务间调用、排查超时时"),
    ("消息队列", "消费幂等键落库再执行业务，死信队列配告警；分区数按吞吐预留，堆积先限流生产端再扩容消费端。", "削峰填谷、异步解耦时"),
    ("对象存储", "大文件走分片上传断点续传，生命周期按访问热度分层；公开桶默认关，签名链接设最短有效期。", "存文件、做备份归档时"),
    ("配置中心", "配置变更走灰度，先一台再一组成比例放量；敏感配置加密存，客户端缓存过期时间别设太长。", "改配置、推开关时"),
    ("链路追踪", "采样率按流量动态调，根因先看 span 的耗时占比；上下文透传别丢 trace_id，异步线程池要手动接力。", "排查跨服务慢请求时"),
    ("压测演练", "压测先在影子环境跑基线，梯度加压找拐点；全链路演练提前报备，熔断与限流阈值演练前先对齐。", "大促前、容量评估时"),
    ("灰度发布", "灰度按用户分桶放量，观察指标半小时无恶化再扩；回滚开关常驻，发布窗口避开业务高峰。", "上线新功能、放量时"),
    ("证书轮换", "证书到期前三十天告警，轮换先备后切；私钥权限最小化，吊销列表定时同步到边缘节点。", "证书快到期、换 CA 时"),
    ("密钥管理", "密钥按服务隔离轮换，旧密钥设过渡期；代码仓扫历史提交，命中密钥立即作废并审计调用链。", "轮换密钥、查泄露时"),
    ("成本优化", "闲置实例定时关停，预留实例按基线买；存储分层按访问频率迁，账单异常波动先看 top 资源。", "降本、盘点资源时"),
    ("多云容灾", "主备跨云部署，演练切流按季度来；数据同步延迟设告警线，脑裂时以多数派仲裁为准。", "做容灾、跨云迁移时"),
    ("数据归档", "冷数据按时间分区归档，查询先走分区裁剪；归档前校验行数与校验和，回滚保留原始快照。", "清理历史数据、降存储时"),
    ("索引优化", "慢查询先看执行计划，复合索引按选择率排字段；写多读少的表少建索引，定期清理膨胀。", "查得慢、优化 SQL 时"),
    ("连接池", "连接数按实例数乘单实例并发算，超时设两档；池满先看慢查询，别无脑扩池把库打垮。", "连接打满、排查超时时"),
    ("慢查询", "慢日志按执行次数乘平均耗时排优先级；回表多就加覆盖索引，排序溢出先看内存参数。", "定位慢 SQL 时"),
    ("缓存穿透", "空值短缓存加布隆过滤前置，热点重建用单飞；穿透告警按 key 维度聚合，别被扫描打穿。", "缓存被打穿、热点重建时"),
    ("布隆过滤", "误判率按内存预算反推位数，建好后只增不改；重建期间双跑比对，切换前先压测误判。", "判存在性、省内存时"),
    ("幂等设计", "幂等键由业务唯一标识生成，处理前先查后写；超时重试靠键去重，别靠调用方自觉。", "防重复提交、重试时"),
    ("分布式锁", "锁超时按业务最长耗时设，续租用后台线程；锁粒度按资源拆，别一把大锁串行化。", "抢资源、防并发写时"),
    ("最终一致", "写端先落本地事务再发事件，消费端幂等重放；对账任务定时跑，差异超阈值立即告警。", "跨服务数据同步时"),
    ("事件溯源", "状态由事件流折叠得出，快照定期存；事件 schema 演进要兼容旧版，重放先在影子库验证。", "审计追溯、重建状态时"),
    ("读写分离", "写走主库读走从库，延迟敏感读强制走主；从库延迟设告警，切换时先停写再切。", "读压力大、分流时"),
]
QUERIES = {
    "top": "部署服务时端口被占用怎么处理",
    "mid": "缓存被打穿热点重建怎么办",
}

ZIPF = [1.0 / (k + 1) for k in range(len(TOPICS))]
_ZSUM = sum(ZIPF)
WEIGHTS = [w / _ZSUM for w in ZIPF]


def topic_of(i: int, size: int) -> int:
    """确定性 Zipf 分配：行 i 落到哪个主题（累积权重查表）。"""
    x = (i % 100000) / 100000.0
    acc = 0.0
    for k, w in enumerate(WEIGHTS):
        acc += w
        if x < acc:
            return k
    return len(TOPICS) - 1


def seed(engine, size: int):
    from datetime import datetime, timezone

    import app.models  # noqa: F401  # 模型注册到 Base.metadata，否则 create_all 建不出表
    from app.db import Base
    from app.tokenize import tokens_of

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    raw = engine.raw_connection()
    now = datetime.now(timezone.utc)
    kinds = ["lesson", "strategy", "pitfall", "cost_saver"]
    app_id = "bench-app"
    try:
        with raw.cursor() as cur:
            # Bulk-load 优化（仅压测 infra，最终库状态与建表一致）：
            # 先卸掉倒排表的两个索引再 COPY，写完后一次性重建。
            # 原版逐行维护 30M 行索引是播种 3 小时跑不完的主因（VM 替换已
            # 杀掉两次）；重建是排序式，比逐行插索引快一个量级。
            # tokens_of 返回 set，无重复 (insight_id, token)，主键重建安全。
            cur.execute("DROP INDEX IF EXISTS ix_insight_token_token")
            cur.execute(
                "ALTER TABLE memory_insight_token"
                " DROP CONSTRAINT memory_insight_token_pkey")
            with cur.copy(
                "COPY memory_insight (id, app_id, user_id, kind, subject, content,"
                " applies_when, valid_from, version, confidence, use_count,"
                " success_count, feedback_pos, feedback_neg, applied_count,"
                " auto_pos, auto_neg, verified, evidence_chained, pinned, muted)"
                " FROM STDIN"
            ) as copy:
                ids = []
                for i in range(size):
                    k = topic_of(i, size)
                    s, body, applies = TOPICS[k]
                    iid = uuid.uuid4().hex
                    ids.append((iid, k, f"{s}经验{i}", f"第{i}次遇到{s}：{body}", applies))
                    copy.write_row((iid, app_id, "bench", kinds[i % 4],
                                    f"{s}经验{i}", f"第{i}次遇到{s}：{body}", applies,
                                    now, 1, 0.5, 0, 0, 0, 0, 0, 0, 0, True,
                                    False, False, False))
            with cur.copy(
                "COPY memory_insight_token (insight_id, token, app_id, user_id)"
                " FROM STDIN"
            ) as copy:
                for iid, _k, subject, content, applies in ids:
                    for t in tokens_of(f"{subject} {content} {applies}"):
                        copy.write_row((iid, t, app_id, "bench"))
            cur.execute(
                "ALTER TABLE memory_insight_token"
                " ADD PRIMARY KEY (insight_id, token)")
            cur.execute(
                "CREATE INDEX ix_insight_token_token"
                " ON memory_insight_token (token, insight_id)")
        raw.commit()
    finally:
        raw.close()
    print(f"seeded {size} insights", flush=True)


def bench(engine, iters: int):
    from sqlalchemy import bindparam, text
    from sqlalchemy.orm import sessionmaker

    from app.auth import UserContext
    from app.config import settings
    from app.episodes import brief_for_task
    from app.tokenize import tokens_of

    db = sessionmaker(bind=engine, expire_on_commit=False)()
    u = UserContext(app_id="bench-app", user_id="bench")
    for name, q in QUERIES.items():
        toks = tokens_of(q)
        # text() 的 :toks 若直接绑 tuple，psycopg3 只渲染成单个 $1，
        # PG 不接受 IN $1；必须用 expanding=True 展开成 ($1, $2, …)。
        bucket = db.execute(text(
            "SELECT COUNT(DISTINCT insight_id) FROM memory_insight_token"
            " WHERE token IN :toks").bindparams(
                bindparam("toks", expanding=True)),
            {"toks": tuple(toks)}).scalar()
        times = []
        winners = 0
        for _ in range(iters):
            t0 = time.perf_counter()
            out = brief_for_task(db, u, q, 5)
            times.append((time.perf_counter() - t0) * 1000)
            winners = len(out)
        times.sort()
        p50 = statistics.median(times)
        p95 = times[int(0.95 * len(times)) - 1] if len(times) > 1 else times[0]
        print(f"[{name}] query={q!r} bucket={bucket} winners={winners} "
              f"p50={p50:.1f}ms p95={p95:.1f}ms max={times[-1]:.1f}ms", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=1000000)
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--seed-only", action="store_true")
    ap.add_argument("--bench-only", action="store_true")
    args = ap.parse_args()
    from app.db import engine

    if not args.bench_only:
        seed(engine, args.size)
    if not args.seed_only:
        bench(engine, args.iters)


if __name__ == "__main__":
    main()

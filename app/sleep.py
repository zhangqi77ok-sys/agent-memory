"""睡眠巩固（第二十一轮）：巡检在空闲时从使用证据里重组记忆。

人脑的睡眠不是关机，是整理：白天一起被想起的连接变强、无用的连接
淡出断开、重复的表述合并、同类经验抽象成原则。这一轮把这四件事落
成巡检任务，触发证据全部来自简报交付记录（brief_delivery），不靠猜测：
- 共用强化：窗口内（自上次巩固报告以来）同份简报递出的经验对，已有
  扩散边的按应用规则加权一次——Hebbian 式「一起想起就连得更紧」；
- 共用建边：全量同现 ≥ 2 次且目前无任何有效边的经验对，连 related 边；
- 死边剪除：强度到下限 + 闲置 ≥ 3 月 + 从未被证实应用过的扩散边，关边；
- 事实合并：事实此前没有合并巡检，同主题近重复事实在此合并（佐证相加）；
- 共用簇抽象：同现簇跨项目 ≥ 3 条且无现成原则覆盖时，判定器起草原则。

与其它巡检的分工：consolidate 管经验生命周期（衰减/合并/裁决/退役），
weave 管新节点连边与余弦簇抽象，睡眠只管使用证据驱动的重组。每项任务
自带门限，每小时巡检顺带调用也不会重复做功；自己 commit（与
consolidate_insights、weave_links 同口径）。
"""
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.edge_learning import STRENGTH_FLOOR, _aware, find_spread_edge, strength_on_applied
from app.extract import norm, same_subject
from app.links import SPREAD_RELATIONS, create_link, migrate_links, view_of
from app.models import AuditLog, BriefDelivery, BriefDeliveryEdge, ConsolidationReport, MemoryFact, MemoryInsight, MemoryLink, _now

COUSE_CREATE_MIN = 2        # 共用建边：全量同现至少这么多次才连边
MAX_NEW_COUSE_EDGES = 50    # 每轮新建共用边上限（按同现次数降序）
PRUNE_IDLE_MONTHS = 3       # 死边剪除：闲置整月数门限
FACT_MERGE_COSINE = 0.95    # 事实合并：同主题向量余弦门限（与经验合并同线）
MAX_NEW_PRINCIPLES = 5      # 每轮共用簇抽象起草上限（判定器调用有成本，按簇规模降序）
CLUSTER_MIN_SIZE = 3        # 共用簇抽象：簇规模门限
CLUSTER_MIN_PROJECTS = 2    # 共用簇抽象：跨项目数门限
_MAX_DELIVERY_IDS = 30      # 单份简报参与共用统计的经验数上限（防超大简报 pair 爆炸）


def _support_confidence(n: int) -> float:
    """佐证置信度，与 worker.support_confidence 同公式：n/(n+1)。

    这里不 import worker（worker 在函数级才 import episodes/links，
    模块级互引容易成环）；公式只有一行，注释标明出处，改一处要同步另一处。
    """
    s = max(int(n or 1), 1)
    return s / (s + 1)


def _couse_counts(deliveries: list[BriefDelivery]) -> Counter:
    """交付记录 → 经验对同现计数（对内 id 排序去向，同一份简报内一对只计一次）。"""
    counts: Counter = Counter()
    for d in deliveries:
        ids = sorted(set((d.insight_ids or [])[:_MAX_DELIVERY_IDS]))
        for a, b in combinations(ids, 2):
            counts[(a, b)] += 1
    return counts


def _strengthen_coused(db: Session, app_id: str, user_id: str,
                       window_counts: Counter) -> list[str]:
    """窗口内同现对的已有扩散边加权一次；返回加权过的边 id。"""
    strengthened: list[str] = []
    seen: set[str] = set()
    for (a, b) in window_counts:
        edge = find_spread_edge(db, app_id, user_id, a, b)
        if edge is None or edge.id in seen:
            continue
        edge.strength = strength_on_applied(edge.strength or 0.5)
        seen.add(edge.id)
        strengthened.append(edge.id)
    return strengthened


def _link_coused(db: Session, app_id: str, user_id: str,
                 all_counts: Counter) -> list[str]:
    """全量同现 ≥ 2 次、无任何有效边的对连 related 边；返回新建边 id。"""
    linked: list[str] = []
    for (a, b), n in sorted(all_counts.items(), key=lambda kv: -kv[1]):
        if n < COUSE_CREATE_MIN or len(linked) >= MAX_NEW_COUSE_EDGES:
            continue
        existing = db.scalar(select(MemoryLink).where(
            MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
            MemoryLink.valid_to.is_(None),
            (((MemoryLink.src_id == a) & (MemoryLink.dst_id == b))
             | ((MemoryLink.src_id == b) & (MemoryLink.dst_id == a)))))
        if existing is not None:
            continue
        link = create_link(db, app_id, user_id, ("insight", a), ("insight", b),
                           "related", 0.5, "rule")
        if link is not None:
            linked.append(link.id)
    return linked


def _prune_dead_edges(db: Session, app_id: str, user_id: str,
                      now: datetime) -> list[str]:
    """死边剪除：强度到下限 + 闲置 ≥ 3 月 + 从未被证实应用 → 关边。"""
    pruned: list[str] = []
    for link in db.scalars(select(MemoryLink).where(
            MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
            MemoryLink.valid_to.is_(None),
            MemoryLink.relation.in_(SPREAD_RELATIONS))):
        if (link.strength or 0.5) > STRENGTH_FLOOR + 1e-9:
            continue
        last_del = db.scalar(select(func.max(BriefDeliveryEdge.created_at)).where(
            BriefDeliveryEdge.link_id == link.id))
        base = max(x for x in (_aware(last_del), _aware(link.decayed_until),
                              _aware(link.created_at)) if x is not None)
        if (now - base).days // 30 < PRUNE_IDLE_MONTHS:
            continue
        applied = db.scalar(select(BriefDeliveryEdge).where(
            BriefDeliveryEdge.link_id == link.id,
            BriefDeliveryEdge.applied.is_(True)))
        if applied is not None:
            continue
        link.valid_to = now
        pruned.append(link.id)
    return pruned


def _merge_facts(db: Session, app_id: str, user_id: str,
                 now: datetime) -> list[dict]:
    """事实合并（睡眠档）：同主题近重复事实合并，佐证相加、置信重算、边迁移。"""
    from app.links import _pair_cosine_rows

    rows = list(db.scalars(select(MemoryFact).where(
        MemoryFact.app_id == app_id, MemoryFact.user_id == user_id,
        MemoryFact.valid_to.is_(None))))
    merged: list[dict] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a.valid_to is not None or b.valid_to is not None:
                continue
            if not same_subject(a.subject, b.subject):
                continue
            cos = _pair_cosine_rows(a, b)
            if norm(a.content) != norm(b.content) and \
                    (cos is None or cos < FACT_MERGE_COSINE):
                continue
            keeper, loser = (a, b) if ((a.confidence or 0.5), (a.support_count or 1),
                                       a.valid_from) >= \
                ((b.confidence or 0.5), (b.support_count or 1), b.valid_from) else (b, a)
            keeper.support_count = (keeper.support_count or 1) + (loser.support_count or 1)
            keeper.confidence = round(_support_confidence(keeper.support_count), 4)
            loser.valid_to = now
            migrate_links(db, app_id, user_id, "fact", loser.id, keeper.id)
            db.add(AuditLog(app_id=app_id, user_id=user_id, action="sleep",
                            fact_ids=[keeper.id, loser.id], decision="merge"))
            merged.append({"keeper_id": keeper.id, "loser_id": loser.id,
                           "keeper_subject": keeper.subject,
                           "loser_subject": loser.subject})
    return merged


def _abstract_couse_clusters(db: Session, app_id: str, user_id: str,
                             all_counts: Counter, linker) -> list[dict]:
    """共用簇抽象：同现簇跨项目且无现成原则覆盖时，判定器起草原则。"""
    if linker is None:
        return []
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    nodes: set[str] = set()
    for (a, b), n in all_counts.items():
        if n < COUSE_CREATE_MIN:
            continue
        nodes.add(a)
        nodes.add(b)
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    clusters: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        clusters[find(node)].append(node)
    by_id = {r.id: r for r in db.scalars(select(MemoryInsight).where(
        MemoryInsight.app_id == app_id, MemoryInsight.user_id == user_id,
        MemoryInsight.valid_to.is_(None)))}
    generalized: list[dict] = []
    ordered = sorted(clusters.values(), key=len, reverse=True)
    for members in ordered:
        if len(generalized) >= MAX_NEW_PRINCIPLES or len(members) < CLUSTER_MIN_SIZE:
            continue
        rows = [by_id[i] for i in members if i in by_id and by_id[i].project]
        if len(rows) < CLUSTER_MIN_SIZE or \
                len({r.project for r in rows}) < CLUSTER_MIN_PROJECTS:
            continue
        member_ids = {r.id for r in rows}
        # 已有原则覆盖 ≥2 名成员 → 本簇不再重复起草（防每轮生一条同款原则）。
        touch = db.scalars(select(MemoryLink).where(
            MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
            MemoryLink.relation == "generalizes", MemoryLink.valid_to.is_(None),
            ((MemoryLink.src_id.in_(member_ids)) | (MemoryLink.dst_id.in_(member_ids))))).all()
        cover_counts: Counter = Counter()
        for link in touch:
            other = link.dst_id if link.src_id in member_ids else link.src_id
            principle = by_id.get(other)
            if principle is not None and principle.project is None:
                cover_counts[other] += 1
        if any(n >= 2 for n in cover_counts.values()):
            continue
        from app.auth import UserContext
        from app.episodes import _apply_insight, _Cand  # 局部导入避免循环

        drafted = linker.generalize([view_of(r) for r in rows])
        if drafted is None:
            continue
        subject, content, applies_when = drafted
        if not content:
            continue
        decision, gid = _apply_insight(db, UserContext(app_id, user_id),
                                       _Cand("principle", subject, content, applies_when),
                                       None, None, project=None)
        if not gid:
            continue
        for r in rows:
            create_link(db, app_id, user_id, ("insight", gid), ("insight", r.id),
                        "generalizes", 0.8, "llm")
        if decision != "noop":
            generalized.append({"id": gid, "subject": subject})
    return generalized


def sleep_consolidate(db: Session, linker=None,
                      now: datetime | None = None) -> dict:
    """睡眠巩固一轮：按用户跑五项任务、落报告行、自己 commit。

    返回 {"reports": n, "merged": n, "pruned": n, "strengthened": n,
    "linked": n, "generalized": n}。无记忆的用户不落行（weave 同口径）。
    """
    now = _aware(now) or _now()
    users = db.execute(select(MemoryInsight.app_id, MemoryInsight.user_id).distinct()).all()
    users += [u for u in db.execute(select(MemoryFact.app_id, MemoryFact.user_id).distinct()).all()
              if u not in users]
    stats = {"reports": 0, "merged": 0, "pruned": 0, "strengthened": 0,
             "linked": 0, "generalized": 0}
    for app_id, user_id in users:
        last = db.scalar(select(ConsolidationReport).where(
            ConsolidationReport.app_id == app_id, ConsolidationReport.user_id == user_id,
        ).order_by(ConsolidationReport.run_at.desc()))
        window_q = select(BriefDelivery).where(
            BriefDelivery.app_id == app_id, BriefDelivery.user_id == user_id)
        if last is not None:
            window_q = window_q.where(BriefDelivery.created_at > last.run_at)
        window_deliveries = list(db.scalars(window_q))
        all_deliveries = list(db.scalars(select(BriefDelivery).where(
            BriefDelivery.app_id == app_id, BriefDelivery.user_id == user_id)))
        window_counts = _couse_counts(window_deliveries)
        all_counts = _couse_counts(all_deliveries)
        strengthened = _strengthen_coused(db, app_id, user_id, window_counts)
        linked = _link_coused(db, app_id, user_id, all_counts)
        pruned = _prune_dead_edges(db, app_id, user_id, now)
        merged = _merge_facts(db, app_id, user_id, now)
        generalized = _abstract_couse_clusters(db, app_id, user_id, all_counts, linker)
        db.add(ConsolidationReport(
            app_id=app_id, user_id=user_id, run_at=now,
            merged=len(merged), merged_pairs=merged,
            pruned=len(pruned), pruned_links=pruned,
            strengthened=len(strengthened), strengthened_links=strengthened,
            linked=len(linked), linked_ids=linked,
            generalized=len(generalized), generalized_ids=generalized))
        stats["reports"] += 1
        stats["merged"] += len(merged)
        stats["pruned"] += len(pruned)
        stats["strengthened"] += len(strengthened)
        stats["linked"] += len(linked)
        stats["generalized"] += len(generalized)
    db.commit()
    return stats


def report_to_dict(row: ConsolidationReport | None) -> dict | None:
    """报告行 → 面板/接口用的 dict；无报告时返回 None（面板显示“尚未巩固过”）。"""
    if row is None:
        return None
    return {"run_at": row.run_at.isoformat() if row.run_at else None,
            "merged": row.merged, "merged_pairs": row.merged_pairs or [],
            "pruned": row.pruned, "pruned_links": row.pruned_links or [],
            "strengthened": row.strengthened,
            "strengthened_links": row.strengthened_links or [],
            "linked": row.linked, "linked_ids": row.linked_ids or [],
            "generalized": row.generalized,
            "generalized_ids": row.generalized_ids or []}

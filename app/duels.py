"""跨项目矛盾对决（第 36 轮，审计 F-11）。

背景：织网巡检早就发现跨项目矛盾对并落 contradicts 边，简报也标
conflicts_with；但第 16 轮裁决循环不区分项目，跨项目对会像同桶对
一样退役输家行——巡检借裁决关掉别项目的行，正是修复第 6 轮分桶
原则在巡检侧的漏口。本模块做三件事：

1. 对决（`decide_pair`）：跨项目矛盾对的胜负裁决，只记记录、
   只影响跨项目简报排序，永不关行。
2. 分化（`differentiate_pair`）：情境不同的跨项目对走分化，与
   同桶分化同口径（关 contradicts 边 → related 边 + 可选合成）。
3. 巡检步（`duel_cross_project`）：织网之后跑，复用巡检锚点候选
   对；裁决循环入口的同桶收口见 episodes.consolidate_insights。

胜负判据（duel-v1，论证值非标定值）：置信度为主（权重 2.0，
已含人工与自动反馈），第 35 轮派生奖励分打破近僵局（归一化后
权重 0.5），30 天内用过再加 0.25。平局（分差 < 1e-9）记 tie：
有记录、无降权。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app import models as m
from app.extract import norm, same_subject
from app.links import _opposing_markers, _overlap, create_link, view_of
from app.reward import reward_score

log = logging.getLogger(__name__)

CRITERIA = "duel-v1"
DUEL_LOSER_MULT = 0.7  # 跨项目简报里败方行的排序乘子（只动排序，不动门槛）
RECENT_DAYS = 30
TIE_EPS = 1e-9


def _aware(ts):
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _now():
    return datetime.now(timezone.utc)


def duel_scores(a, b) -> dict:
    """两行各自的胜负分与分项（派生快照，可由行上计数器重算）。"""
    now = _now()

    def one(row):
        conf = row.confidence if row.confidence is not None else 0.5
        rew = reward_score(row)
        stamp = _aware(row.last_used_at or row.valid_from)
        recent = 1.0 if stamp is not None and (now - stamp).days < RECENT_DAYS else 0.0
        return {"total": round(2.0 * conf + 0.5 * (rew / 10.0) + 0.25 * recent, 4),
                "confidence": round(conf, 4), "reward": round(rew, 4), "recent": recent}

    return {a.id: one(a), b.id: one(b)}


def _conditions_differ(a, b) -> bool:
    """情境字符串分界：两边归一化后都不空且不相等 → 视为条件不同。

    同义不同写的情境会被误送分化——分化不关行不降权，是保守方向
    （见设计草稿 §1.2）。"""
    na, nb = norm(a.applies_when or ""), norm(b.applies_when or "")
    return bool(na and nb and na != nb)


def _current_duel(db, app_id, user_id, a_id, b_id):
    x, y = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    return db.scalar(select(m.MemoryDuel).where(
        m.MemoryDuel.app_id == app_id, m.MemoryDuel.user_id == user_id,
        m.MemoryDuel.a_id == x, m.MemoryDuel.b_id == y,
        m.MemoryDuel.valid_to.is_(None)))


def _current_contradicts_edge(db, app_id, user_id, a_id, b_id):
    """两行之间的现行 contradicts 边（任一方向，边是对称的）。"""
    return db.scalar(select(m.MemoryLink).where(
        m.MemoryLink.app_id == app_id, m.MemoryLink.user_id == user_id,
        m.MemoryLink.relation == "contradicts", m.MemoryLink.valid_to.is_(None),
        m.MemoryLink.src_kind == "insight", m.MemoryLink.dst_kind == "insight",
        or_((m.MemoryLink.src_id == a_id) & (m.MemoryLink.dst_id == b_id),
            (m.MemoryLink.src_id == b_id) & (m.MemoryLink.dst_id == a_id))))


def differentiate_pair(db, a, b, linker, now) -> str:
    """跨项目分化：关规则源 contradicts 边 → related 边 + 可选合成。

    与同桶分化同口径（episodes.consolidate_insights）：判定器源边不
    动（判定器已权衡过条件）；无 linker 或起草失败/返回空时分化照常。
    返回 "differentiated"（本对已处理，调用方不再对决）。"""
    from app.links import create_link as _create_link

    edge = _current_contradicts_edge(db, a.app_id, a.user_id, a.id, b.id)
    if edge is not None and edge.origin == "rule":
        edge.valid_to = now
    _create_link(db, a.app_id, a.user_id, ("insight", a.id), ("insight", b.id),
                 "related", 0.5, "rule")
    db.add(m.AuditLog(app_id=a.app_id, user_id=a.user_id, action="duel",
                      fact_ids=[a.id, b.id], decision="differentiate"))
    if linker is not None:
        try:
            drafted = linker.synthesize_condition(view_of(a), view_of(b))
        except Exception as e:
            log.warning("duel synthesize_condition failed: %s", e)
            drafted = None
        if drafted is not None:
            from app.episodes import _apply_insight, _Cand

            subject, content, applies_when = drafted
            from app.auth import UserContext

            decision, gid = _apply_insight(
                db, UserContext(a.app_id, a.user_id),
                _Cand("principle", subject, content, applies_when),
                None, None, project=None, additive=True)
            if gid and decision != "noop":
                _create_link(db, a.app_id, a.user_id, ("insight", gid), ("insight", a.id),
                             "generalizes", 0.8, "llm")
                _create_link(db, a.app_id, a.user_id, ("insight", gid), ("insight", b.id),
                             "generalizes", 0.8, "llm")
    return "differentiated"


def _loser_evidence(row) -> int:
    """输家证据条数（同桶分化同口径）：人工 + 自动反馈计数。"""
    return ((row.feedback_pos or 0) + (row.feedback_neg or 0)
            + (row.auto_pos or 0) + (row.auto_neg or 0))


def decide_pair(db, a, b, linker, now) -> str:
    """裁决一对跨项目矛盾行。调用方保证：同用户、两行现行未禁、
    project 均非空且不相等、same_subject 为真。

    顺序与同桶裁决同构：先计分定胜负，输家被置顶 → 跳过（用户策展
    高于自动裁决）；再看分化——情境不同（且无判定器源 contradicts
    边，判定器已权衡过条件）且输家证据 ≥ 1 条 → 分化，不对决；
    其余 → 按 duel-v1 计分，胜负记记录；tie 有记录无降权。
    对决对确保有一条现行 contradicts 边（织网已建的复用；织网因
    “已有他边”跳过重分类的对，在此补建——边只标记矛盾，胜负在记录里）。
    返回 "win" | "tie" | "differentiated" | "skipped"。"""
    edge = _current_contradicts_edge(db, a.app_id, a.user_id, a.id, b.id)
    scores = duel_scores(a, b)
    sa, sb = scores[a.id]["total"], scores[b.id]["total"]
    if abs(sa - sb) < TIE_EPS:
        winner_id = loser_id = None
        verdict = "tie"
    elif sa > sb:
        winner_id, loser_id, verdict = a.id, b.id, "win"
    else:
        winner_id, loser_id, verdict = b.id, a.id, "win"
    if loser_id is not None:
        loser = a if loser_id == a.id else b
        if loser.pinned:
            cur = _current_duel(db, a.app_id, a.user_id, a.id, b.id)
            if cur is not None:
                cur.valid_to = now  # 置顶败方不再被自动裁决：旧裁决同时失效
            return "skipped"
        if (edge is None or edge.origin == "rule") and _conditions_differ(a, b) \
                and _loser_evidence(loser) >= 1:
            return differentiate_pair(db, a, b, linker, now)
    if edge is None:
        create_link(db, a.app_id, a.user_id, ("insight", a.id), ("insight", b.id),
                    "contradicts", 0.6, "rule")
    x, y = (a.id, b.id) if a.id < b.id else (b.id, a.id)
    cur = _current_duel(db, a.app_id, a.user_id, a.id, b.id)
    if cur is not None:
        if cur.winner_id == winner_id and cur.loser_id == loser_id:
            cur.scores = scores  # 胜负不变：信号漂移只刷新分快照，不产新裁决
            return verdict
        cur.valid_to = now
    db.add(m.MemoryDuel(app_id=a.app_id, user_id=a.user_id, a_id=x, b_id=y,
                        a_project=a.project if a.id == x else b.project,
                        b_project=b.project if a.id == x else a.project,
                        winner_id=winner_id, loser_id=loser_id, verdict=verdict,
                        scores=scores, criteria=CRITERIA, valid_from=now))
    return verdict  # "win" | "tie"


def current_loser_ids(db, app_id, user_id, candidate_ids) -> set:
    """简报降权查询：候选集里现行对决记录的败方 id，一条 IN 查询。"""
    if not candidate_ids:
        return set()
    rows = db.scalars(select(m.MemoryDuel.loser_id).where(
        m.MemoryDuel.app_id == app_id, m.MemoryDuel.user_id == user_id,
        m.MemoryDuel.valid_to.is_(None), m.MemoryDuel.loser_id.is_not(None),
        m.MemoryDuel.loser_id.in_(list(candidate_ids)))).all()
    return set(rows)


def delete_duels_for_insights(db, app_id, user_id, ids) -> int:
    """forget(insight) 用：硬删涉及这些行的全部对决记录（现行与历史）。

    与 delete_links_touching 同口径：节点硬删，关联不留；删除证据
    本身由 AuditLog 的 forget 记录保留，不靠对决表。"""
    if not ids:
        return 0
    idset = set(ids)
    rows = list(db.scalars(select(m.MemoryDuel).where(
        m.MemoryDuel.app_id == app_id, m.MemoryDuel.user_id == user_id,
        or_(m.MemoryDuel.a_id.in_(idset), m.MemoryDuel.b_id.in_(idset)))))
    for r in rows:
        db.delete(r)
    return len(rows)


def duel_cross_project(db, linker=None, now=None) -> dict:
    """巡检对决步（run_patrol 里织网之后、睡眠之前）：按用户分组，
    锚点候选对里找跨项目矛盾对并裁决；开关关闭的用户整轮跳过。

    自己 commit（与织网/合并同口径的分片提交）。返回统计。"""
    from app.api.me import switched_off_users
    from app.patrol import PATROL_PAIR_BUDGET, candidate_pairs

    now = now or _now()
    off = switched_off_users(db)
    # 会话一律 expire_on_commit=False（app/db.py）：同会话里先跑过的
    # 巡检步（合并/织网）已把行载入 identity map，后续 scalars 查询
    # 只返回缓存属性、不刷新——跨会话写入的反馈会计入旧值。对决判据
    # 必须读已提交的最新信号，入口先全部过期，后续取行即真刷新；
    # 入口时前序步已提交，本步尚无待写状态，过期安全。
    db.expire_all()
    stats = {"duels": 0, "ties": 0, "differentiated": 0, "skipped": 0,
             "closed": 0, "deferred": 0}
    users = db.execute(select(m.MemoryInsight.app_id, m.MemoryInsight.user_id).distinct()).all()
    for app_id, user_id in users:
        if (app_id, user_id) in off:
            continue
        rows = list(db.scalars(select(m.MemoryInsight).where(
            m.MemoryInsight.app_id == app_id, m.MemoryInsight.user_id == user_id,
            m.MemoryInsight.valid_to.is_(None),
            or_(m.MemoryInsight.muted.is_(False), m.MemoryInsight.muted.is_(None)))))
        by_id = {r.id: r for r in rows}
        # 失活扫尾：端点已不在现行集的现行记录关掉（行关闭/被禁/被删的后账；
        # 行硬删走 forget 入口直接删记录，这里只处理关行与被禁）。
        for rec in list(db.scalars(select(m.MemoryDuel).where(
                m.MemoryDuel.app_id == app_id, m.MemoryDuel.user_id == user_id,
                m.MemoryDuel.valid_to.is_(None)))):
            if rec.a_id not in by_id or rec.b_id not in by_id:
                rec.valid_to = now
                stats["closed"] += 1
        # 现行 contradicts 边一次预取：逐对查库会把填充对也打回 O(n²) 查询。
        edge_pairs = set()
        for link in db.scalars(select(m.MemoryLink).where(
                m.MemoryLink.app_id == app_id, m.MemoryLink.user_id == user_id,
                m.MemoryLink.relation == "contradicts", m.MemoryLink.valid_to.is_(None),
                m.MemoryLink.src_kind == "insight", m.MemoryLink.dst_kind == "insight")):
            edge_pairs.add(frozenset((link.src_id, link.dst_id)))
        cand, deferred = candidate_pairs(rows, budget=PATROL_PAIR_BUDGET,
                                         now=now.timestamp())
        stats["deferred"] += deferred
        for i, j in cand:
            a, b = rows[i], rows[j]
            if not a.project or not b.project or a.project == b.project:
                continue
            if not same_subject(a.subject, b.subject):
                continue
            va, vb = view_of(a), view_of(b)
            if frozenset((a.id, b.id)) not in edge_pairs and \
                    not (_opposing_markers(va, vb) and _overlap(va, vb) >= 0.3):
                continue
            res = decide_pair(db, a, b, linker, now)
            if res == "win":
                stats["duels"] += 1
            elif res == "tie":
                stats["ties"] += 1
            elif res == "differentiated":
                stats["differentiated"] += 1
            else:
                stats["skipped"] += 1
        db.commit()
    return stats

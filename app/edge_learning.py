"""第二十轮「边学习」：边的强度从使用中长出来，而不是创建时 frozen。

反思第 1 条的落地。规则写死成纯函数，方便单测、方便日后调参：
- 被应用（扩散带出的经验后来在任务回流里被证实有用，或人工正反馈）：
  向 1 靠拢一小步：s += (1 - s) * 0.10，越强涨得越慢，永不超 1。
- 被否决（扩散带出后人工负反馈）：s *= 0.90，下限 0.10——弱边保留
  可见性，不直接删边；删边是遗忘/裁决的事，不由一次否决决定。
- 长期不兑现（巡逻任务按月结算）：s *= 0.95 ** 月数，下限 0.10。
  「长期不兑现的连接自然淡出」落在这里，不是简报时现算。

接线点（第二阶段已接线）：
- 简报扩散时记下「哪条边带出了哪条经验」（新表 brief_delivery_edge，
  迁移 0020；记账在 app/episodes.py brief_for_task）；
- 任务回流 / 人工反馈给某条经验记应用时，顺着交付记录找到边，
  调 strength_on_applied / strength_on_rejected（episodes.py）；
- 巡检对闲置边调 decay_idle_edges（本文件；weave_links 顺带调用，
  锚点保证同一段闲置只扣一次）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.links import SPREAD_RELATIONS
from app.models import BriefDeliveryEdge, MemoryLink, _now


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 存不下时区：库里读回来的时间可能是 naive，一律按 UTC 计。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

APPLIED_GAIN = 0.10
REJECTED_DECAY = 0.90
MONTHLY_DECAY = 0.95
STRENGTH_FLOOR = 0.10
STRENGTH_CEIL = 1.0


def strength_on_applied(strength):
    """边带出的经验被证实有用：向 1 靠拢一小步。"""
    return min(STRENGTH_CEIL, strength + (1.0 - strength) * APPLIED_GAIN)


def strength_on_rejected(strength):
    """边带出的经验被人工否决：打九折，不低于下限。"""
    return max(STRENGTH_FLOOR, strength * REJECTED_DECAY)


def strength_after_idle(strength, months):
    """边长期未兑现：按月打折，下限保底；months <= 0 不动。"""
    if months <= 0:
        return strength
    return max(STRENGTH_FLOOR, strength * (MONTHLY_DECAY ** months))


def find_spread_edge(db, app_id, user_id, from_id, to_id):
    """找一条扩散边：from_id 的经验经哪条边带出了 to_id 的经验。

    对称关系（supports / contradicts / related）按端点 id 序规范化
    存储，有向关系（refines / generalizes）只存一个方向，所以两个
    方向都要查；多条边并存时取强度最高的（扩散打折按最强边算，
    与 spread_from 的取法一致）。矛盾边不扩散，排除在外。
    """
    rows = list(db.scalars(select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        MemoryLink.valid_to.is_(None),
        MemoryLink.relation.in_(SPREAD_RELATIONS),
        (((MemoryLink.src_id == from_id) & (MemoryLink.dst_id == to_id))
         | ((MemoryLink.src_id == to_id) & (MemoryLink.dst_id == from_id))))))
    if not rows:
        return None
    return max(rows, key=lambda r: r.strength)


def decay_idle_edges(db: Session, now: datetime | None = None) -> int:
    """闲置衰减结清（第二十轮，巡检节拍调用）：现行边按「自上次被扩散交付
    或上次结清以来」的整月数衰减一次（0.95^月数，下限 0.10），并把
    decayed_until 记到 now——下次只算新增的闲置月数，同一段闲置不重复扣。

    未被交付过的边从创建起算闲置；被交付过的从最近一次交付起算；结清
    过的从上次结清起算——三者取最晚的作基准。调用方负责 commit。
    每小时的 weave 巡检顺带调用也安全：不足整月的边直接跳过。
    """
    now = _aware(now) or _now()
    decayed = 0
    for link in db.scalars(select(MemoryLink).where(
            MemoryLink.valid_to.is_(None))):
        last_del = db.scalar(select(func.max(BriefDeliveryEdge.created_at)).where(
            BriefDeliveryEdge.link_id == link.id))
        base = max(x for x in (_aware(last_del), _aware(link.decayed_until),
                              _aware(link.created_at))
                   if x is not None)
        months = (now - base).days // 30
        if months < 1:
            continue
        link.strength = strength_after_idle(link.strength or 0.5, months)
        link.decayed_until = now
        decayed += 1
    return decayed

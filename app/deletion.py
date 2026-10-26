"""删除清单的单点收口（修复第 8 轮，审计根因 1）。

此前 forget / clear_all / restore.sh 各自手写删除清单，新增表（交付账、
提醒、交付边、巩固报告）时只补了部分入口，清单随之漂移。本模块是
用户数据删除的唯一清单来源：

- CLEAR_ALL_MODELS：clear_all 必须删除的全部用户域模型（含任务行）。
  任务行由 clear_all 调用方先删（修复第 4 轮顺序：任务行删除的 PG 阻塞
  语义要求它发生在快照之前），helper 本身不删任务行。
- CLEAR_ALL_KEEP：刻意保留项——同意状态不是记忆数据，清空不静默撤回同意。
- 其余 helper 供 forget 各分支调用，交付剥离 / 报告 scrub / 附录剥离 /
  来源消息条件删不再各入口手写。

刻意保留且不在清单里的三类（删了反而是 bug）：audit_log（删除证据链，
只存 id 不存正文）、forgotten_id / forgotten_content（删除机制本身）、
admin_audit（管理端审计，与用户数据域无关）。
"""

import re

from sqlalchemy import delete, select

from app import models as m

# clear_all 删除的全部用户域模型。UserScoped 的直接子类集合必须等于
# CLEAR_ALL_MODELS ∪ CLEAR_ALL_KEEP（注册表测试钉住，见 test_fix8_deletion）。
CLEAR_ALL_MODELS = (
    m.MemoryJob,
    m.MemoryFact,
    m.MemoryPreference,
    m.SessionSummary,
    m.SessionMessage,
    m.MemoryInsight,
    m.TaskEpisode,
    m.MemoryReminder,
    m.BriefDelivery,
    m.BriefDeliveryEdge,
    m.ConsolidationReport,
    m.MemoryLink,
    m.MemoryWorkingEntry,
)

# 保留项：UserSetting 是同意状态，不是记忆数据；清空是“删数据”，撤回同意
# 是另一个动作，清空后自动关开关会让用户下次写入静默失败。
CLEAR_ALL_KEEP = (m.UserSetting,)

# 合并附录段首标记（修复第 8 轮，审计 B-F6）：段格式为
# [合并自同类经验：{subject} <!-- merged-from:{loser_id} -->] {content}。
# 标记在段首括号内，即使内容被 MERGE_CONTENT_CAP 截断标记仍在。
_MERGED_FROM_RE = re.compile(
    r"\n\n\[合并自同类经验：.*?<!-- merged-from:([0-9A-Za-z_-]+) -->\]"
    r"[\s\S]*?(?=\n\n\[合并自同类经验：|\Z)"
)


def delete_user_memory_data(db, app_id: str, user_id: str, fact_ids=(), insight_ids=()) -> None:
    """除任务行外的全部用户域删除（任务行由调用方先删，见模块 docstring）。

    token 行不继承 UserScoped，按父 id 精确删（Core delete 不触发 ORM 事件）。
    只删行不 commit，调用方负责墓碑与审计后的统一提交。
    """
    for model in CLEAR_ALL_MODELS:
        if model is m.MemoryJob:
            continue  # 调用方先删，顺序语义见修复第 4 轮
        db.execute(delete(model).where(model.app_id == app_id, model.user_id == user_id))
    if fact_ids:
        db.execute(delete(m.MemoryFactToken).where(m.MemoryFactToken.fact_id.in_(fact_ids)))
    if insight_ids:
        db.execute(delete(m.MemoryInsightToken).where(m.MemoryInsightToken.insight_id.in_(insight_ids)))


def strip_deliveries_for_insights(db, app_id: str, user_id: str, ids) -> int:
    """forget(insight) 的交付清账：删交付边、剥交付 JSON 里的已删 id。

    变空的交付行保留空列表：交付行本身是“某次简报发生过”的账，删行会抹掉
    简报历史；空列表已不能驱动回流（无 id 可结账）与共用统计（无对可计数）。
    不碰 consumed_at：剥 id 后未消费交付仍是未消费，只是名单变短。
    返回被改写的交付行数。
    """
    ids = set(ids)
    if not ids:
        return 0
    db.execute(
        delete(m.BriefDeliveryEdge).where(
            m.BriefDeliveryEdge.app_id == app_id, m.BriefDeliveryEdge.user_id == user_id, m.BriefDeliveryEdge.insight_id.in_(ids)
        )
    )
    stripped = 0
    rows = db.scalars(select(m.BriefDelivery).where(m.BriefDelivery.app_id == app_id, m.BriefDelivery.user_id == user_id)).all()
    for d in rows:
        cur = d.insight_ids or []
        new = [i for i in cur if i not in ids]
        if len(new) != len(cur):
            d.insight_ids = new
            stripped += 1
    return stripped


def scrub_reports(db, app_id: str, user_id: str, ids) -> int:
    """巩固报告的外科式 scrub：只移除引用已删行的条目，不改写历史计数。

    merged_pairs 中 keeper_id / loser_id 在删除集合中的条目整条移除
    （事实合并对的主题字符串随之消失）；generalized_ids 中 id 在集合中的
    条目移除（原则由此被忘时清掉）。计数列不动：聚合数不含正文，改写
    计数等于篡改账本。返回被改写的报告行数。
    """
    ids = set(ids)
    if not ids:
        return 0
    scrubbed = 0
    rows = db.scalars(
        select(m.ConsolidationReport).where(m.ConsolidationReport.app_id == app_id, m.ConsolidationReport.user_id == user_id)
    ).all()
    for r in rows:
        pairs = [
            p for p in (r.merged_pairs or []) if isinstance(p, dict) and p.get("keeper_id") not in ids and p.get("loser_id") not in ids
        ]
        gen = [g for g in (r.generalized_ids or []) if (g.get("id") if isinstance(g, dict) else g) not in ids]
        if len(pairs) != len(r.merged_pairs or []) or len(gen) != len(r.generalized_ids or []):
            r.merged_pairs = pairs
            r.generalized_ids = gen
            scrubbed += 1
    return scrubbed


def strip_merged_appendices(db, app_id: str, user_id: str, ids) -> int:
    """forget(insight) 的附录剥离：从存活经验行（含已关闭历史版本——导出含
    历史，只清现行行会在导出里复活）中剥离 merged-from 在删除集合中的
    附录段。内容变化的行按 patch 同规则作废向量。

    fix8 之前合并的旧附录无标记，无法按 id 定位，不按主题猜测剥离
    （主题可重名，猜错即误删）；旧附录随保留行自身被忘时自然消失。
    返回被改写的行数。
    """
    ids = set(ids)
    if not ids:
        return 0
    stripped = 0
    rows = db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.app_id == app_id, m.MemoryInsight.user_id == user_id)).all()
    for row in rows:
        content = row.content or ""
        new = _MERGED_FROM_RE.sub(lambda mo: "" if mo.group(1) in ids else mo.group(0), content)
        if new != content:
            row.content = new
            row.embedding = None
            row.embed_model = None
            stripped += 1
    return stripped


def delete_orphan_source_messages(db, app_id: str, user_id: str, session_ids) -> int:
    """forget(fact) 的来源清理（审计 TODO 落地）：只删已无事实引用的会话。

    事实只有 source_session_id，没有到单条消息的映射；同一会话可能产出多条
    事实，全删该会话消息会误删其它事实的来源。故规则：任一版本（含已关闭
    历史版本——历史仍以该会话为溯源）引用该会话时一律保留；全无引用时
    删该会话的消息行与摘要行。任务行不删：它不含原文且是修复第 4 轮在途
    门禁的依据。返回删除的会话数。
    """
    deleted = 0
    for sid in session_ids:
        if not sid:
            continue
        survives = db.scalar(
            select(m.MemoryFact.id)
            .where(m.MemoryFact.app_id == app_id, m.MemoryFact.user_id == user_id, m.MemoryFact.source_session_id == sid)
            .limit(1)
        )
        if survives is None:
            db.execute(
                delete(m.SessionMessage).where(
                    m.SessionMessage.app_id == app_id, m.SessionMessage.user_id == user_id, m.SessionMessage.session_id == sid
                )
            )
            db.execute(
                delete(m.SessionSummary).where(
                    m.SessionSummary.app_id == app_id, m.SessionSummary.user_id == user_id, m.SessionSummary.session_id == sid
                )
            )
            deleted += 1
    return deleted

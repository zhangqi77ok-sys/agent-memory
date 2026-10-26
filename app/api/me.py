"""「我的记忆」面板用的 /v1/me/* 接口：查看、纠正、清空、导出、总开关、写入任务状态。

所有查询都带 user_id 条件，Postgres 上再由 FORCE RLS 兜底。
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models as m
from app.api.memories import _audit, get_db
from app.auth import UserContext, bind_user, resolve_asserted, user_ctx, user_from_query
from app.graph_stats import compute_graph_stats
from app.links import neighbors_of
from app.schemas import (
    CONSENT_VERSION,
    CorrectRequest,
    DuelOut,
    DuelPage,
    EpisodeDetail,
    InsightListItem,
    InsightPage,
    InsightPatch,
    JobOut,
    MemoryItem,
    MemoryPage,
    SettingsIn,
    SettingsOut,
)

router = APIRouter(prefix="/v1/me")


def _mine(model, u: UserContext):
    return (model.app_id == u.app_id, model.user_id == u.user_id)


def get_setting(db: Session, u: UserContext) -> m.UserSetting | None:
    return db.scalar(select(m.UserSetting).where(*_mine(m.UserSetting, u)))


def memory_enabled(db: Session, u: UserContext) -> bool:
    s = get_setting(db, u)
    return bool(s and s.memory_enabled and s.consent_version == CONSENT_VERSION)


def switched_off_users(db: Session) -> set[tuple[str, str]]:
    """显式关掉记忆的用户集合（修复第 4 轮，巡检过滤用）：有设置行且
    未开启（含同意版本过期——memory_enabled 对这类用户同样返回 False，
    口径一致）。无设置行的用户不过滤：巡检面对的存量行可能早于设置表，
    且多轮巡检测试直接造行而不走 consent，严格语义会悄悄改变 legacy
    行为；主动关过开关的用户一定有设置行，一定被滤掉。"""
    rows = db.execute(select(m.UserSetting.app_id, m.UserSetting.user_id).where(
        (m.UserSetting.memory_enabled.is_(False))
        | (m.UserSetting.consent_version != CONSENT_VERSION))).all()
    # 转普通 tuple：Row 对象与 tuple 的哈希不一致，直接 set(rows) 会使
    # 后续的 tuple 成员判断静默失效。
    return {(r[0], r[1]) for r in rows}


def _fact_item(f: m.MemoryFact) -> MemoryItem:
    return MemoryItem(id=f.id, kind="fact", subject=f.subject, content=f.content, version=f.version,
                      valid_from=f.valid_from, source_session_id=f.source_session_id,
                      confidence=f.confidence if f.confidence is not None else 0.5,
                      support_count=f.support_count or 1,
                      feedback_pos=f.feedback_pos or 0, feedback_neg=f.feedback_neg or 0)


def _pref_item(p: m.MemoryPreference) -> MemoryItem:
    return MemoryItem(id=p.id, kind="preference", subject=p.key, content=p.value, version=p.version,
                      valid_from=p.valid_from,
                      confidence=p.confidence if p.confidence is not None else 0.5,
                      support_count=p.support_count or 1,
                      feedback_pos=p.feedback_pos or 0, feedback_neg=p.feedback_neg or 0)


@router.get("/memories", response_model=MemoryPage)
def list_memories(kind: str | None = Query(None, pattern="^(fact|preference)$"),
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """只列当前有效版本；按时间倒序。"""
    bind_user(db, u)
    items: list[MemoryItem] = []
    if kind in (None, "preference"):
        items += [_pref_item(p) for p in db.scalars(select(m.MemoryPreference).where(
            *_mine(m.MemoryPreference, u), m.MemoryPreference.valid_to.is_(None)))]
    if kind in (None, "fact"):
        items += [_fact_item(f) for f in db.scalars(select(m.MemoryFact).where(
            *_mine(m.MemoryFact, u), m.MemoryFact.valid_to.is_(None)))]
    items.sort(key=lambda i: i.valid_from, reverse=True)
    return MemoryPage(items=items[offset:offset + limit], total=len(items), offset=offset, limit=limit)


@router.patch("/memories/{item_id}", response_model=MemoryItem)
def correct(item_id: str, req: CorrectRequest, db: Session = Depends(get_db), request: Request = None):
    """纠正：旧版本 valid_to=now 失效，新建 version+1；召回只会看到新版本。

    修复第 7 轮：同意门——未同意/已暂停/同意过期时 404，与反馈/提醒
    入口同形（不泄露行存在性）。门查在行查找之前，避免墓碑 409 泄露
    行存在性；纠正是新建版本行的实质写，不适用“策展豁免”。
    """
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    if not memory_enabled(db, u):
        raise HTTPException(404)
    now = datetime.now(UTC)
    old = db.get(m.MemoryFact, item_id)
    if old and old.user_id == u.user_id and old.app_id == u.app_id and old.valid_to is None:
        # 修复第 2 轮：纠正的新内容若命中内容墓碑，拒绝——否则纠正
        # 就是一条绕过 _apply 的复活路。
        from app.tombstone import is_tombstoned

        if is_tombstoned(db, u.app_id, u.user_id, "fact", old.subject, req.content):
            raise HTTPException(status_code=409, detail={"code": "tombstoned"})
        old.valid_to = now
        new = m.MemoryFact(app_id=u.app_id, user_id=u.user_id, subject=old.subject, content=req.content,
                           version=old.version + 1, valid_from=now, source_session_id=None)
        # 用户手动纠正没有来源会话；向量由 Worker 后续补（embedding 为空时召回走关键词）
        db.add(new)
        # 第 32 轮：纠正的新版本行落库即织网（weave_on_write 永不抛错）。
        from app.links import weave_on_write

        db.flush()
        weave_on_write(db, u.app_id, u.user_id, new)
        _audit(db, u, "correct", [old.id, new.id])
        db.commit()
        return _fact_item(new)
    oldp = db.get(m.MemoryPreference, item_id)
    if oldp and oldp.user_id == u.user_id and oldp.app_id == u.app_id and oldp.valid_to is None:
        oldp.valid_to = now
        newp = m.MemoryPreference(app_id=u.app_id, user_id=u.user_id, key=oldp.key, value=req.content,
                                  version=oldp.version + 1, valid_from=now)
        db.add(newp)
        _audit(db, u, "correct", [oldp.id, newp.id])
        db.commit()
        return _pref_item(newp)
    raise HTTPException(404)  # 不存在、已失效或不属于当前用户，一律 404


@router.delete("/memories", status_code=204)
def clear_all(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """全部清空：事实（含历史版本和向量）、偏好、摘要、原始消息、未完成的写入任务、
    经验与任务轨迹（第七轮补：此前清空后经验侧会残留，与「全部」二字不符）、
    简报交付账与交付边、巩固报告（第八轮补：此前清空后边与报告残留）。

    v1 同步执行（单用户数据量小）；id 记入 forgotten_id，备份恢复后据此重删。
    同意状态（UserSetting）与删除审计（AuditLog）、墓碑（ForgottenId/Content）
    刻意保留：前者是同意不是数据，后两者是删除本身的证据与机制。
    """
    bind_user(db, u)
    # 修复第 4 轮：任务行最先删（审计 F1c 的 PG 一半）。PG 下在途 worker
    # 持有任务行 FOR UPDATE 锁，任务行删除会阻塞到 worker 提交为止——
    # 阻塞是期望的：它把“在途写回”变成“发生在事实删除之前”，从而被
    # 后续的用户谓词删除覆盖；快照随之移到任务行删除之后（PG READ
    # COMMITTED 下快照语句看得见 worker 刚提交的新行，新事实的 token
    # 与墓碑一并覆盖，孤儿 token 消失）。SQLite 下任务行删除立即成功，
    # 在途 worker 靠提交前存在性重查回滚，同样零残留。
    db.execute(delete(m.MemoryJob).where(*_mine(m.MemoryJob, u)))
    # 修复第 2 轮：删前先载内容字段（Core delete 后无从取），逐行记内容墓碑；
    # 此前只载 id，清空后重发同一内容无墓碑可查。
    fact_rows = list(db.scalars(select(m.MemoryFact).where(*_mine(m.MemoryFact, u))))
    pref_rows = list(db.scalars(select(m.MemoryPreference).where(*_mine(m.MemoryPreference, u))))
    insight_rows = list(db.scalars(select(m.MemoryInsight).where(*_mine(m.MemoryInsight, u))))
    episode_rows = list(db.scalars(select(m.TaskEpisode).where(*_mine(m.TaskEpisode, u))))
    # 修复第 3 轮：提醒是 clear_all 删、此前从未写过任何墓碑；不补则恢复
    # 五类重删里的 reminder 一类是空文。subject=条件，content=动作。
    reminder_rows = list(db.scalars(select(m.MemoryReminder).where(*_mine(m.MemoryReminder, u))))
    fact_ids = [r.id for r in fact_rows]
    pref_ids = [r.id for r in pref_rows]
    insight_ids = [r.id for r in insight_rows]
    reminder_ids = [r.id for r in reminder_rows]
    # 删前快照内容元组：Core delete 后 ORM 对象过期，删后再取会取不到。
    fact_snap = [(r.subject, r.content) for r in fact_rows]
    pref_snap = [(r.key, r.value) for r in pref_rows]
    insight_snap = [(r.subject, r.content) for r in insight_rows]
    episode_snap = [(r.title, r.steps) for r in episode_rows]
    reminder_snap = [(r.condition_text, r.action_text) for r in reminder_rows]
    # 修复第 8 轮：删除清单收口到 app/deletion（任务行已在上一步先删，
    # 修复第 4 轮顺序不动）。此前手写清单漏了交付边与巩固报告——新增用户域
    # 表只改 helper + 注册表测试，不再各入口补漏。
    from app.deletion import delete_user_memory_data

    delete_user_memory_data(db, u.app_id, u.user_id, fact_ids, insight_ids)
    for i in fact_ids:
        db.merge(m.ForgottenId(id=i, kind="fact"))
    for i in pref_ids:
        db.merge(m.ForgottenId(id=i, kind="preference"))
    for i in insight_ids:
        db.merge(m.ForgottenId(id=i, kind="insight"))
    for i in reminder_ids:
        db.merge(m.ForgottenId(id=i, kind="reminder"))
    from app.tombstone import record_episode_tombstone, record_tombstone

    for subject, content in fact_snap:
        record_tombstone(db, u.app_id, u.user_id, "fact", subject, content)
    for key, value in pref_snap:
        record_tombstone(db, u.app_id, u.user_id, "preference", key, value)
    for subject, content in insight_snap:
        record_tombstone(db, u.app_id, u.user_id, "insight", subject, content)
    for title, steps in episode_snap:
        record_episode_tombstone(db, u.app_id, u.user_id, title, steps)
    for condition, action in reminder_snap:
        record_tombstone(db, u.app_id, u.user_id, "reminder", condition, action)
    _audit(db, u, "clear_all", fact_ids + pref_ids + insight_ids)
    db.commit()


@router.get("/memories/export")
def export(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """导出我的全部数据（个保法 可携带权）：含历史版本，不含向量。

    第九轮补齐：经验（insights）与任务轨迹（episodes）也是用户数据——第七轮删除权
    已覆盖它们，导出不能缺席，否则用户带得走事实却带不走经验。"""
    bind_user(db, u)
    facts = db.scalars(select(m.MemoryFact).where(*_mine(m.MemoryFact, u)).order_by(m.MemoryFact.valid_from))
    prefs = db.scalars(select(m.MemoryPreference).where(*_mine(m.MemoryPreference, u)))
    insights = db.scalars(select(m.MemoryInsight).where(*_mine(m.MemoryInsight, u))
                          .order_by(m.MemoryInsight.valid_from))
    episodes = db.scalars(select(m.TaskEpisode).where(*_mine(m.TaskEpisode, u))
                          .order_by(m.TaskEpisode.created_at))
    links = db.scalars(select(m.MemoryLink).where(*_mine(m.MemoryLink, u))
                       .order_by(m.MemoryLink.created_at))
    reminders = db.scalars(select(m.MemoryReminder).where(*_mine(m.MemoryReminder, u))
                            .order_by(m.MemoryReminder.created_at))
    s = get_setting(db, u)
    _audit(db, u, "export", [])
    db.commit()
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "user_id": u.user_id,
        "settings": {"memory_enabled": bool(s and s.memory_enabled),
                     "consent_version": s.consent_version if s else None},
        "facts": [{"id": f.id, "subject": f.subject, "content": f.content, "version": f.version,
                   "confidence": f.confidence, "support_count": f.support_count,
                   "valid_from": iso(f.valid_from), "valid_to": iso(f.valid_to),
                   "source_session_id": f.source_session_id} for f in facts],
        "preferences": [{"id": p.id, "key": p.key, "value": p.value, "version": p.version,
                         "confidence": p.confidence, "support_count": p.support_count,
                         "valid_from": iso(p.valid_from), "valid_to": iso(p.valid_to)} for p in prefs],
        "insights": [{"id": i.id, "subject": i.subject, "content": i.content,
                      "applies_when": i.applies_when, "version": i.version,
                      "project": i.project, "confidence": i.confidence,
                      "verified": True if i.verified is None else bool(i.verified),
                      "applied_count": i.applied_count or 0,
                      "use_count": i.use_count or 0,
                      "pinned": bool(i.pinned), "muted": bool(i.muted),
                      "valid_from": iso(i.valid_from), "valid_to": iso(i.valid_to)} for i in insights],
        "episodes": [{"id": e.id, "source": e.source, "title": e.title, "outcome": e.outcome,
                      "steps": e.steps, "cost": e.cost, "distill_status": e.distill_status,
                      "salience": e.salience, "skeletonized": bool(e.skeletonized),
                      "created_at": iso(e.created_at)} for e in episodes],
        # 第十五轮的边（第十七轮补进导出）：星图据此画真实关联，不再靠同主题猜线
        "links": [{"id": lk.id, "src_kind": lk.src_kind, "src_id": lk.src_id,
                   "dst_kind": lk.dst_kind, "dst_id": lk.dst_id, "relation": lk.relation,
                   "strength": lk.strength, "origin": lk.origin,
                   "valid_to": iso(lk.valid_to)} for lk in links],
        "reminders": [{"id": r.id, "condition": r.condition_text, "action": r.action_text,
                       "project": r.project, "once": bool(r.once), "status": r.status,
                       "fire_count": r.fire_count or 0} for r in reminders],
    }


def _insight_item(i: m.MemoryInsight) -> InsightListItem:
    return InsightListItem(
        id=i.id, kind=i.kind, subject=i.subject, content=i.content,
        applies_when=i.applies_when, project=i.project,
        confidence=i.confidence if i.confidence is not None else 0.5,
        use_count=i.use_count or 0, success_count=i.success_count or 0,
        feedback_pos=i.feedback_pos or 0, feedback_neg=i.feedback_neg or 0,
        pinned=bool(i.pinned), muted=bool(i.muted), version=i.version,
        source_episode_id=i.source_episode_id, valid_from=i.valid_from,
        last_used_at=i.last_used_at,
        verified=True if i.verified is None else bool(i.verified),
        applied_count=i.applied_count or 0, auto_pos=i.auto_pos or 0,
        auto_neg=i.auto_neg or 0)


@router.get("/skills")
def export_skills_ep(project: str | None = Query(None, max_length=128),
                     min_confidence: float = Query(0.5, ge=0.0, le=1.0),
                     u: UserContext = Depends(user_from_query),
                     db: Session = Depends(get_db)):
    """把高置信经验编译成 Skills 文件（第十三轮）：每个项目一份 SKILL.md + 汇总 AGENTS.md。

    返回 {files: {相对路径: 内容}}，调用方（脚本 / 工具）落盘到仓库里，编程工具
    即可原生加载。与 JSON 全量导出分工：导出是备份/可携带，这份是给工具吃的成品。"""
    from app.skills_export import export_skills

    bind_user(db, u)
    files = export_skills(db, u, project=project, min_confidence=min_confidence)
    _audit(db, u, "skills_export", [])
    db.commit()
    return {"files": files, "count": len(files)}


@router.get("/markdown")
def export_markdown_ep(u: UserContext = Depends(user_from_query),
                       db: Session = Depends(get_db)):
    """记忆的 Markdown 镜像（第十三轮）：事实/偏好/经验按主题渲染成 .md（带条目 id）。

    这是给人看的真相文件：可读、可 diff、可进 git。与 JSON 导出的分工——
    JSON 是备份/可携带的完整数据，这份是日常可编辑的工作副本。"""
    from app.markdown_sync import render_memory_markdown

    bind_user(db, u)
    files = render_memory_markdown(db, u)
    _audit(db, u, "md_export", [])
    db.commit()
    return {"files": files, "count": len(files)}


@router.post("/markdown/import")
def import_markdown_ep(payload: dict, u: UserContext = Depends(user_from_query),
                       db: Session = Depends(get_db)):
    """把编辑过的 Markdown 导回（第十三轮）：按 id 应用编辑与新增；删除只报告不执行。

    修复第 7 轮：同意门——未同意/已暂停/同意过期时 404（审计 F-14
    建议口径，与提醒创建一致）。导回的 created 分支会新建记忆行，
    是实质写；门查在 apply 之前，apply 本身不再另设门（仅本端点调用）。
    """
    from app.markdown_sync import apply_markdown_import

    files = payload.get("files")
    if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise HTTPException(422, "files 必须是 {路径: 内容} 的对象")
    if len(files) > 500:
        raise HTTPException(422, "文件数超限（500）")
    bind_user(db, u)
    if not memory_enabled(db, u):
        raise HTTPException(404)
    return apply_markdown_import(db, u, files)


@router.get("/insights", response_model=InsightPage)
def list_insights(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """我的经验库（第十一轮）：现行经验全量可见——学到了什么、置信度多少、用过几次，
    不再是黑盒。含已被禁用的（muted 本人要能看见才能取消禁用）。
    第二十五轮改为分页（与 /v1/me/memories 同口径）：此前无界列表，面板只能
    静默截断在前 100 条。"""
    bind_user(db, u)
    rows = db.scalars(select(m.MemoryInsight).where(
        *_mine(m.MemoryInsight, u), m.MemoryInsight.valid_to.is_(None))
        .order_by(m.MemoryInsight.valid_from.desc()))
    items = [_insight_item(i) for i in rows]
    return InsightPage(items=items[offset:offset + limit], total=len(items),
                       offset=offset, limit=limit)


@router.get("/duels", response_model=DuelPage)
def list_duels(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
               u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """跨项目对决记录（第 36 轮，审计 F-11）：只列现行裁决——哪两条
    经验在对决、各自属于哪个项目、谁胜谁负、判据分。行已不在现行集
    的记录由巡检失活扫尾关闭，不会留在这里；面板暂不渲染本表，
    本接口即“可查”的落点。"""
    bind_user(db, u)
    recs = list(db.scalars(select(m.MemoryDuel).where(
        *_mine(m.MemoryDuel, u), m.MemoryDuel.valid_to.is_(None))
        .order_by(m.MemoryDuel.valid_from.desc())))
    ids = {i for r in recs for i in (r.a_id, r.b_id)}
    subjects = {}
    if ids:
        for row in db.scalars(select(m.MemoryInsight).where(
                *_mine(m.MemoryInsight, u), m.MemoryInsight.id.in_(ids))):
            subjects[row.id] = row.subject
    items = [DuelOut(a_id=r.a_id, b_id=r.b_id,
                     a_subject=subjects.get(r.a_id, ""),
                     b_subject=subjects.get(r.b_id, ""),
                     a_project=r.a_project, b_project=r.b_project,
                     winner_id=r.winner_id, loser_id=r.loser_id,
                     verdict=r.verdict, scores=r.scores or {},
                     criteria=r.criteria, valid_from=r.valid_from)
             for r in recs]
    return DuelPage(items=items[offset:offset + limit], total=len(items),
                    offset=offset, limit=limit)


@router.get("/identity")
def identity(request: Request, u: UserContext = Depends(user_from_query),
             db: Session = Depends(get_db)):
    """当前身份（第二十五轮）：面板登录后调用，诚实展示模式——bound=true 表示
    本次请求的身份来自服务密钥绑定（?user_id= 已失效），false 表示自断言。
    只回自己的身份，不泄露映射。"""
    bind_user(db, u)
    return {"app_id": u.app_id, "user_id": u.user_id,
            "bound": getattr(request.state, "bound_user", None) is not None}


@router.get("/episodes/{episode_id}", response_model=EpisodeDetail)
def episode_detail(episode_id: str, u: UserContext = Depends(user_from_query),
                   db: Session = Depends(get_db)):
    """轨迹详情（第二十五轮溯源落点）：面板点一条经验的“溯源”后看到的来源
    轨迹全文。跨用户与不存在一律 404（与 insight_links 同形，不泄露存在性）。"""
    bind_user(db, u)
    ep = db.scalar(select(m.TaskEpisode).where(
        *_mine(m.TaskEpisode, u), m.TaskEpisode.id == episode_id))
    if ep is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return EpisodeDetail(id=ep.id, title=ep.title, source=ep.source, outcome=ep.outcome,
                         project=ep.project, steps=ep.steps or [], cost=ep.cost or {},
                         distill_status=ep.distill_status, salience=ep.salience or 0.0,
                         skeletonized=bool(ep.skeletonized), created_at=ep.created_at)


@router.get("/insights/{insight_id}/links")
def insight_links(insight_id: str, u: UserContext = Depends(user_from_query),
                  db: Session = Depends(get_db)):
    """一条经验的邻里（第十五轮「织网」）：它和哪些记忆连着、什么关系、边的出身。
    记忆不再是孤行——用户能看见网长什么样。不存在或不是本人的 → 404（同 patch 口径）。"""
    bind_user(db, u)
    row = db.scalar(select(m.MemoryInsight).where(
        *_mine(m.MemoryInsight, u), m.MemoryInsight.id == insight_id))
    if row is None:
        raise HTTPException(status_code=404, detail="insight not found")
    out = []
    for link, okind, oid in neighbors_of(db, u.app_id, u.user_id, "insight", insight_id):
        node = db.get(m.MemoryInsight if okind == "insight" else m.MemoryFact, oid)
        out.append({
            "link_id": link.id, "relation": link.relation, "strength": link.strength,
            "origin": link.origin,
            "node": None if node is None else {
                "kind": okind, "id": oid, "subject": node.subject, "content": node.content,
                "active": node.valid_to is None},
        })
    return {"insight_id": insight_id, "links": out}


@router.get("/graph/stats")
def graph_stats(u: UserContext = Depends(user_from_query),
                db: Session = Depends(get_db)):
    """网结构统计（第二十轮）：节点/边/密度/孤点/枢纽/社群/桥/矛盾张力。

    只读快照。千万级下这是一次全量扫描，不承诺 50ms——50ms 只约束
    简报路径，网结构统计走离线快照口径。
    """
    bind_user(db, u)
    return compute_graph_stats(db, u.app_id, u.user_id)


@router.get("/consolidation/latest")
def consolidation_latest(u: UserContext = Depends(user_from_query),
                         db: Session = Depends(get_db)):
    """最新一次睡眠巩固报告（第二十一轮）：本轮合并了哪些、剪了哪些边、
    强化了哪些连接、起草了哪些原则。无报告时 report 为 None——面板显示
    “尚未巩固过”，而不是编一条空报告。
    """
    from app.sleep import report_to_dict

    bind_user(db, u)
    row = db.scalar(select(m.ConsolidationReport).where(
        m.ConsolidationReport.app_id == u.app_id,
        m.ConsolidationReport.user_id == u.user_id,
    ).order_by(m.ConsolidationReport.run_at.desc()))
    return {"report": report_to_dict(row)}


@router.patch("/insights/{insight_id}", response_model=InsightListItem)
def patch_insight(insight_id: str, req: InsightPatch,
                  u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """单条策展：改指导/适用情境、置顶、禁用。改匹配面（content/applies_when）会作废
    旧向量——宁可退化成关键词召回，也不能拿着旧向量假装语义命中；补向量走现成 CLI。
    不存在或不是本人的 → 404（与 feedback 同形，不泄露存在性）。

    修复第 7 轮：同意门——未同意/已暂停/同意过期时同样 404。改内容是
    实质记忆写，不适用“策展豁免”；纯标记改动（置顶/禁用）为求门语义
    简单可审计，不在同一端点内拆分豁免。门查在行查找之前。
    """
    bind_user(db, u)
    if not memory_enabled(db, u):
        raise HTTPException(status_code=404, detail="insight not found")
    row = db.scalar(select(m.MemoryInsight).where(
        *_mine(m.MemoryInsight, u), m.MemoryInsight.id == insight_id))
    if row is None:
        raise HTTPException(status_code=404, detail="insight not found")
    if req.content is not None and req.content != row.content:
        # 修复第 2 轮：同 correct，新内容命中内容墓碑时拒绝复活。
        from app.tombstone import is_tombstoned

        if is_tombstoned(db, u.app_id, u.user_id, "insight", row.subject, req.content):
            raise HTTPException(status_code=409, detail={"code": "tombstoned"})
        row.content = req.content
        row.embedding = None
        row.embed_model = None
    if req.applies_when is not None and req.applies_when != row.applies_when:
        row.applies_when = req.applies_when
        row.embedding = None
        row.embed_model = None
    if req.pinned is not None:
        row.pinned = req.pinned
    if req.muted is not None:
        row.muted = req.muted
    _audit(db, u, "insight_update", [row.id])
    db.commit()
    return _insight_item(row)


def _settings_out(s: m.UserSetting | None) -> SettingsOut:
    return SettingsOut(
        memory_enabled=bool(s and s.memory_enabled),
        consent_version=s.consent_version if s else None,
        consent_at=s.consent_at if s else None,
        current_consent_version=CONSENT_VERSION,
    )


@router.get("/settings", response_model=SettingsOut)
def read_settings(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    bind_user(db, u)
    return _settings_out(get_setting(db, u))


@router.put("/settings", response_model=SettingsOut)
def write_settings(req: SettingsIn, db: Session = Depends(get_db), request: Request = None):
    """开启记忆必须带当前隐私说明版本（= 用户在授权页点了同意）；关闭随时可以。"""
    u = bind_user(db, user_ctx(resolve_asserted(request, req.user_id)))
    s = get_setting(db, u)
    if req.memory_enabled:
        if req.consent_version != CONSENT_VERSION:
            raise HTTPException(409, detail={"code": "consent_required", "current_consent_version": CONSENT_VERSION})
        if not s:
            s = m.UserSetting(app_id=u.app_id, user_id=u.user_id)
            db.add(s)
        if s.consent_version != CONSENT_VERSION:
            s.consent_version, s.consent_at = CONSENT_VERSION, datetime.now(UTC)
        s.memory_enabled = True
    elif s:
        s.memory_enabled = False
    _audit(db, u, "settings", [])
    db.commit()
    return _settings_out(s)


@router.get("/jobs", response_model=list[JobOut])
def jobs(limit: int = Query(20, ge=1, le=100), u: UserContext = Depends(user_from_query),
         db: Session = Depends(get_db)):
    bind_user(db, u)
    rows = db.scalars(select(m.MemoryJob).where(*_mine(m.MemoryJob, u))
                      .order_by(m.MemoryJob.created_at.desc()).limit(limit))
    return [JobOut(job_id=j.id, session_id=j.session_id, status=j.status, attempts=j.attempts,
                   last_error=j.last_error, created_at=j.created_at) for j in rows]



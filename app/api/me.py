"""「我的记忆」面板用的 /v1/me/* 接口：查看、纠正、清空、导出、总开关、写入任务状态。

所有查询都带 user_id 条件，Postgres 上再由 FORCE RLS 兜底。
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models as m
from app.api.memories import _audit, get_db
from app.auth import UserContext, bind_user, user_ctx, user_from_query
from app.links import neighbors_of
from app.schemas import (
    CONSENT_VERSION,
    CorrectRequest,
    InsightListItem,
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


def _fact_item(f: m.MemoryFact) -> MemoryItem:
    return MemoryItem(id=f.id, kind="fact", subject=f.subject, content=f.content, version=f.version,
                      valid_from=f.valid_from, source_session_id=f.source_session_id)


def _pref_item(p: m.MemoryPreference) -> MemoryItem:
    return MemoryItem(id=p.id, kind="preference", subject=p.key, content=p.value, version=p.version,
                      valid_from=p.valid_from)


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
def correct(item_id: str, req: CorrectRequest, db: Session = Depends(get_db)):
    """纠正：旧版本 valid_to=now 失效，新建 version+1；召回只会看到新版本。"""
    u = bind_user(db, user_ctx(req.user_id))
    now = datetime.now(UTC)
    old = db.get(m.MemoryFact, item_id)
    if old and old.user_id == u.user_id and old.app_id == u.app_id and old.valid_to is None:
        old.valid_to = now
        new = m.MemoryFact(app_id=u.app_id, user_id=u.user_id, subject=old.subject, content=req.content,
                           version=old.version + 1, valid_from=now, source_session_id=None)
        # 用户手动纠正没有来源会话；向量由 Worker 后续补（embedding 为空时召回走关键词）
        db.add(new)
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
    经验与任务轨迹（第七轮补：此前清空后经验侧会残留，与「全部」二字不符）。

    v1 同步执行（单用户数据量小）；id 记入 forgotten_id，备份恢复后据此重删。
    """
    bind_user(db, u)
    fact_ids = list(db.scalars(select(m.MemoryFact.id).where(*_mine(m.MemoryFact, u))))
    pref_ids = list(db.scalars(select(m.MemoryPreference.id).where(*_mine(m.MemoryPreference, u))))
    insight_ids = list(db.scalars(select(m.MemoryInsight.id).where(*_mine(m.MemoryInsight, u))))
    for model in (m.MemoryFact, m.MemoryPreference, m.SessionSummary, m.SessionMessage, m.MemoryJob,
                  m.MemoryInsight, m.TaskEpisode):
        db.execute(delete(model).where(*_mine(model, u)))
    # 倒排索引随父行清（Core delete 不触发 ORM 事件；按本用户的父 id 精确删）
    if fact_ids:
        db.execute(delete(m.MemoryFactToken).where(m.MemoryFactToken.fact_id.in_(fact_ids)))
    if insight_ids:
        db.execute(delete(m.MemoryInsightToken).where(m.MemoryInsightToken.insight_id.in_(insight_ids)))
    # 关联边（第十五轮）全清：节点都没了，网自然不留
    db.execute(delete(m.MemoryLink).where(*_mine(m.MemoryLink, u)))
    for i in fact_ids:
        db.merge(m.ForgottenId(id=i, kind="fact"))
    for i in pref_ids:
        db.merge(m.ForgottenId(id=i, kind="preference"))
    for i in insight_ids:
        db.merge(m.ForgottenId(id=i, kind="insight"))
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
                   "valid_from": iso(f.valid_from), "valid_to": iso(f.valid_to),
                   "source_session_id": f.source_session_id} for f in facts],
        "preferences": [{"id": p.id, "key": p.key, "value": p.value, "version": p.version,
                         "valid_from": iso(p.valid_from), "valid_to": iso(p.valid_to)} for p in prefs],
        "insights": [{"id": i.id, "subject": i.subject, "content": i.content,
                      "applies_when": i.applies_when, "version": i.version,
                      "project": i.project, "confidence": i.confidence,
                      "pinned": bool(i.pinned), "muted": bool(i.muted),
                      "valid_from": iso(i.valid_from), "valid_to": iso(i.valid_to)} for i in insights],
        "episodes": [{"id": e.id, "source": e.source, "title": e.title, "outcome": e.outcome,
                      "steps": e.steps, "cost": e.cost, "distill_status": e.distill_status,
                      "created_at": iso(e.created_at)} for e in episodes],
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
    """把编辑过的 Markdown 导回（第十三轮）：按 id 应用编辑与新增；删除只报告不执行。"""
    from app.markdown_sync import apply_markdown_import

    files = payload.get("files")
    if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise HTTPException(422, "files 必须是 {路径: 内容} 的对象")
    if len(files) > 500:
        raise HTTPException(422, "文件数超限（500）")
    bind_user(db, u)
    return apply_markdown_import(db, u, files)


@router.get("/insights", response_model=list[InsightListItem])
def list_insights(u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """我的经验库（第十一轮）：现行经验全量可见——学到了什么、置信度多少、用过几次，
    不再是黑盒。含已被禁用的（muted 本人要能看见才能取消禁用）。"""
    bind_user(db, u)
    rows = db.scalars(select(m.MemoryInsight).where(
        *_mine(m.MemoryInsight, u), m.MemoryInsight.valid_to.is_(None))
        .order_by(m.MemoryInsight.valid_from.desc()))
    return [_insight_item(i) for i in rows]


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


@router.patch("/insights/{insight_id}", response_model=InsightListItem)
def patch_insight(insight_id: str, req: InsightPatch,
                  u: UserContext = Depends(user_from_query), db: Session = Depends(get_db)):
    """单条策展：改指导/适用情境、置顶、禁用。改匹配面（content/applies_when）会作废
    旧向量——宁可退化成关键词召回，也不能拿着旧向量假装语义命中；补向量走现成 CLI。
    不存在或不是本人的 → 404（与 feedback 同形，不泄露存在性）。"""
    bind_user(db, u)
    row = db.scalar(select(m.MemoryInsight).where(
        *_mine(m.MemoryInsight, u), m.MemoryInsight.id == insight_id))
    if row is None:
        raise HTTPException(status_code=404, detail="insight not found")
    if req.content is not None and req.content != row.content:
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
def write_settings(req: SettingsIn, db: Session = Depends(get_db)):
    """开启记忆必须带当前隐私说明版本（= 用户在授权页点了同意）；关闭随时可以。"""
    u = bind_user(db, user_ctx(req.user_id))
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



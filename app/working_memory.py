"""工作记忆：会话级短期缓冲（第 34 轮）。

删除一律走 Core 表删除：SQLite 读回的 datetime 是 naive，ORM 批量删除
会在会话内对已加载对象做 Python 侧条件求值，naive 与 aware 相减直接
TypeError；Core 删除只在 SQL 侧比较（两侧都是 UTC 墙钟串），无此问题。

缓冲是存量记忆上的视图，不是第二份事实：条目只存 id + 项目 +
版本快照，不存正文；读时按 id 回表取现行行，行不存在/已关闭/
被禁/版本变化时条目作废。全部操作 O(CAP)，与存量总行数无关。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app import models as m

WORKING_CAP = 20  # 每会话缓冲条数上限（条数是唯一诚实的容量单位）
WORKING_TTL_HOURS = 24  # 失活 TTL：超过这么久未碰的条目读时 purge
SESSION_ID_MAX = 64  # 与 String(64) 对齐


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_session_id(session_id) -> str | None:
    """会话身份归一化：非法（空/空白/超长/非字符串）→ None = 缓冲不启用。"""
    if not isinstance(session_id, str):
        return None
    sid = session_id.strip()
    if not sid or len(sid) > SESSION_ID_MAX:
        return None
    return sid


def buffer_enabled(db, app_id: str, user_id: str, session_id) -> str | None:
    """返回归一化后的 session_id，或 None（不启用）。

    后备闸：显式关过开关（或同意版本过期）的用户不读写缓冲。
    无设置行用户不拦——端点层对这类用户本就返回空简报，缓冲跑不到；
    直接调用的 legacy 路径不被严格语义悄悄改变（fix-4 巡检语义）。
    """
    sid = normalize_session_id(session_id)
    if sid is None:
        return None
    from app.api.me import switched_off_users

    if (app_id, user_id) in switched_off_users(db):
        return None
    return sid


def purge_stale(db, app_id: str, user_id: str, session_id: str, now: datetime) -> None:
    """删掉本会话失活条目（last_touched 超 TTL）。不 commit，调用方结账。"""
    cutoff = now - timedelta(hours=WORKING_TTL_HOURS)
    db.execute(
        delete(m.MemoryWorkingEntry.__table__).where(
            m.MemoryWorkingEntry.app_id == app_id,
            m.MemoryWorkingEntry.user_id == user_id,
            m.MemoryWorkingEntry.session_id == session_id,
            m.MemoryWorkingEntry.last_touched < cutoff,
        )
    )


def _entries(db, app_id: str, user_id: str, session_id: str):
    """本会话全部条目（填充顺序：最近碰过在前，复述多者优先）。"""
    return list(
        db.scalars(
            select(m.MemoryWorkingEntry)
            .where(
                m.MemoryWorkingEntry.app_id == app_id,
                m.MemoryWorkingEntry.user_id == user_id,
                m.MemoryWorkingEntry.session_id == session_id,
            )
            .order_by(m.MemoryWorkingEntry.last_touched.desc(), m.MemoryWorkingEntry.rehearsal_count.desc())
        )
    )


def fill_candidates(db, app_id: str, user_id: str, session_id: str, project, exclude_ids, slots: int, now: datetime):
    """补位候选：返回 ≤slots 个现行经验行（填充顺序）。

    校验逐条：行不存在/已关闭/被禁/版本变化 → 删条目，不端旧内容。
    分桶：只取缓冲时项目为空或等于本次简报 project 的条目；跨项目
    条目留在缓冲里，等它自己项目的简报。调用方负责 token 预算装包。
    """
    if slots <= 0:
        return []
    purge_stale(db, app_id, user_id, session_id, now)
    entries = _entries(db, app_id, user_id, session_id)
    if not entries:
        return []
    exclude = set(exclude_ids)
    wanted = [e for e in entries if e.insight_id not in exclude and (e.project is None or project is None or e.project == project)]
    if not wanted:
        return []
    rows = {
        r.id: r
        for r in db.scalars(
            select(m.MemoryInsight).where(
                m.MemoryInsight.app_id == app_id, m.MemoryInsight.user_id == user_id, m.MemoryInsight.id.in_([e.insight_id for e in wanted])
            )
        )
    }
    out = []
    drop_ids = []
    for e in wanted:
        r = rows.get(e.insight_id)
        if r is None or r.valid_to is not None or (r.muted is not None and r.muted) or (r.version or 1) != (e.version or 1):
            drop_ids.append(e.id)
            continue
        out.append(r)
        if len(out) >= slots:
            break
    if drop_ids:
        db.execute(delete(m.MemoryWorkingEntry.__table__).where(m.MemoryWorkingEntry.id.in_(drop_ids)))
    return out


def record_deliveries(db, app_id: str, user_id: str, session_id: str, rows, now: datetime) -> None:
    """简报递出后记缓冲：已在缓冲的复述 +1（last_touched 刷新），
    新行编码（rehearsal_count=0），随后强制删到 CAP（淘汰序：
    rehearsal_count asc, last_touched asc）。不 commit，调用方结账。

    rows 须带 id/project/version（简报轻量列已含 version，见
    brief_for_task 的 cols）。复述只动缓冲条目，不碰存量行计数与
    last_used_at——曝光类信号不进第 22 轮衰减因子。
    """
    purge_stale(db, app_id, user_id, session_id, now)
    if not rows:
        return
    existing = {
        e.insight_id: e
        for e in db.scalars(
            select(m.MemoryWorkingEntry).where(
                m.MemoryWorkingEntry.app_id == app_id,
                m.MemoryWorkingEntry.user_id == user_id,
                m.MemoryWorkingEntry.session_id == session_id,
                m.MemoryWorkingEntry.insight_id.in_([r.id for r in rows]),
            )
        )
    }
    for r in rows:
        e = existing.get(r.id)
        if e is not None:
            e.rehearsal_count = (e.rehearsal_count or 0) + 1
            e.last_touched = now
            e.version = r.version or 1
            e.project = r.project
        else:
            db.add(
                m.MemoryWorkingEntry(
                    app_id=app_id,
                    user_id=user_id,
                    session_id=session_id,
                    insight_id=r.id,
                    project=r.project,
                    version=r.version or 1,
                    rehearsal_count=0,
                    last_touched=now,
                )
            )
    db.flush()
    total = db.scalar(
        select(func.count())
        .select_from(m.MemoryWorkingEntry)
        .where(
            m.MemoryWorkingEntry.app_id == app_id, m.MemoryWorkingEntry.user_id == user_id, m.MemoryWorkingEntry.session_id == session_id
        )
    )
    excess = (total or 0) - WORKING_CAP
    if excess > 0:
        victim_ids = [
            r[0]
            for r in db.execute(
                select(m.MemoryWorkingEntry.id)
                .where(
                    m.MemoryWorkingEntry.app_id == app_id,
                    m.MemoryWorkingEntry.user_id == user_id,
                    m.MemoryWorkingEntry.session_id == session_id,
                )
                .order_by(m.MemoryWorkingEntry.rehearsal_count.asc(), m.MemoryWorkingEntry.last_touched.asc())
                .limit(excess)
            ).fetchall()
        ]
        if victim_ids:
            db.execute(delete(m.MemoryWorkingEntry.__table__).where(m.MemoryWorkingEntry.id.in_(victim_ids)))


def purge_for_insights(db, app_id: str, user_id: str, ids) -> int:
    """forget(insight) 用：清掉这些 id 在该用户全部会话里的缓冲条目。"""
    ids = set(ids)
    if not ids:
        return 0
    res = db.execute(
        delete(m.MemoryWorkingEntry.__table__).where(
            m.MemoryWorkingEntry.app_id == app_id, m.MemoryWorkingEntry.user_id == user_id, m.MemoryWorkingEntry.insight_id.in_(ids)
        )
    )
    return res.rowcount or 0

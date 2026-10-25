"""内容级遗忘墓碑（修复第 2 轮）：删掉的记忆不许以新 id 重发回来。

审计 S2 的根因：`ForgottenId` 只记 id、写入时从不被查——forget 后
以新幂等键重发同一对话，新行新 id，旧 tombstone 永远匹配不上。
本模块把 tombstone 升级到内容粒度：forget/clear 时按
`(app_id, user_id, kind, sha256(norm(subject) + "\\n" + norm(content)))`
记行；全部写路径在落库前查它，命中即跳过并审计。

口径取舍（见 fix2-design-draft.md）：主规则是无条件禁写，不用
`job.created_at < forgotten_at` 时间门——时间门修不了审计自己的
复现 3（新幂等键重发建新 job，时间门会放行）。代价是用户事后想
一字不差重新记住同一内容会被挡住，须改写措辞或等后续轮次的
“显式重新确认”语义。时间门只用在任务级：创建于用户最近一次
删除之前的旧任务整单丢弃（`user_forget_watermark`）。
"""

import hashlib
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models as m
from app.extract import norm

log = logging.getLogger(__name__)


def content_hash(subject: str, content: str) -> str:
    """与写路径同一 norm 口径的内容哈希：空白折叠 + casefold 后拼 subject/content。"""
    return hashlib.sha256(f"{norm(subject)}\n{norm(content)}".encode()).hexdigest()


def episode_hash(title: str, steps) -> str:
    """轨迹内容哈希：title 作 subject，steps 规范化 JSON 作 content。"""
    steps_text = json.dumps(steps, sort_keys=True, ensure_ascii=False, default=str)
    return content_hash(title or "", steps_text)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 回读 naive、PG 回读 aware；naive 按 UTC 计，两库可比。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def record_tombstone(db: Session, app_id: str, user_id: str, kind: str, subject: str, content: str) -> None:
    """记一条内容墓碑（已存在则刷新 forgotten_at；不提交，调用方提交）。"""
    db.merge(
        m.ForgottenContent(
            app_id=app_id,
            user_id=user_id,
            kind=kind,
            content_hash=content_hash(subject, content),
            forgotten_at=datetime.now(UTC),
        )
    )


def record_episode_tombstone(db: Session, app_id: str, user_id: str, title: str, steps) -> None:
    """轨迹墓碑：subject=title，content=规范化 steps JSON。"""
    db.merge(
        m.ForgottenContent(
            app_id=app_id,
            user_id=user_id,
            kind="episode",
            content_hash=episode_hash(title, steps),
            forgotten_at=datetime.now(UTC),
        )
    )


def is_tombstoned(db: Session, app_id: str, user_id: str, kind: str, subject: str, content: str) -> bool:
    """内容是否被遗忘过：命中即禁写（不论写任务新旧）。"""
    row = db.scalar(
        select(m.ForgottenContent).where(
            m.ForgottenContent.app_id == app_id,
            m.ForgottenContent.user_id == user_id,
            m.ForgottenContent.kind == kind,
            m.ForgottenContent.content_hash == content_hash(subject, content),
        )
    )
    return row is not None


def is_episode_tombstoned(db: Session, app_id: str, user_id: str, title: str, steps) -> bool:
    """轨迹内容是否被遗忘过（重提炼禁写判定用）。"""
    row = db.scalar(
        select(m.ForgottenContent).where(
            m.ForgottenContent.app_id == app_id,
            m.ForgottenContent.user_id == user_id,
            m.ForgottenContent.kind == "episode",
            m.ForgottenContent.content_hash == episode_hash(title, steps),
        )
    )
    return row is not None


def user_forget_watermark(db: Session, app_id: str, user_id: str) -> datetime | None:
    """用户最近一次遗忘时间（内容墓碑表的 max）；无遗忘记录则为 None。

    旧 id-tombstone（forgotten_id）无 app/user 列，算不出水位——
    部署前已存在的在途任务 realistic 上不存在（任务以秒级处理），
    接受此缺口；水位自本轮起由内容墓碑维护。
    """
    ts = db.scalar(
        select(func.max(m.ForgottenContent.forgotten_at)).where(
            m.ForgottenContent.app_id == app_id,
            m.ForgottenContent.user_id == user_id,
        )
    )
    return _aware(ts)


def block_if_tombstoned(db: Session, app_id: str, user_id: str, kind: str, subject: str, content: str) -> bool:
    """命中则记审计（action=tombstone/decision=blocked）并返回 True。

    审计行不含原文与哈希：哈希本身只活在 tombstone 表内，不可逆。
    """
    if not is_tombstoned(db, app_id, user_id, kind, subject, content):
        return False
    db.add(m.AuditLog(app_id=app_id, user_id=user_id, action="tombstone", fact_ids=[], decision="blocked"))
    log.info("tombstone block app=%s user=%s kind=%s", app_id, user_id, kind)
    return True

"""写入 Worker：领取 memory_job，脱敏后抽取，再按代码规则入库。

领取仍是 SKIP LOCKED + 处理超时 + 最多 3 次重试。
抽取失败（网络/5xx）回到 pending，超过次数进 dead；模型输出不合法则丢弃该批并完成任务。
向量嵌入仍留空，召回继续走关键词。缺 LLM 配置时进程直接退出，不用假抽取器。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.db import WorkerSession
from app.extract import PROMPT_VERSION, InvalidExtraction, decide, is_injection, norm, strip_injection
from app.redact import mask_user_id, redact

MAX_ATTEMPTS = 3
LOCK_SECONDS = 120
log = logging.getLogger("worker")


def claim_job(db: Session) -> m.MemoryJob | None:
    now = datetime.now(timezone.utc)
    q = (select(m.MemoryJob)
         .where(or_(m.MemoryJob.status == "pending",
                    (m.MemoryJob.status == "running") & (m.MemoryJob.locked_until < now)))
         .where(m.MemoryJob.attempts < MAX_ATTEMPTS)
         .order_by(m.MemoryJob.created_at)
         .limit(1)
         .with_for_update(skip_locked=True))
    job = db.scalar(q)
    if not job:
        return None
    job.status, job.attempts, job.locked_until = "running", job.attempts + 1, now + timedelta(seconds=LOCK_SECONDS)
    db.commit()
    return job


def fail_job(db: Session, job: m.MemoryJob, err: str) -> None:
    job.last_error = err[:2000]
    job.status = "dead" if job.attempts >= MAX_ATTEMPTS else "pending"
    job.locked_until = None
    db.commit()


def _call_extractor(extractor, text: str):
    fn = extractor.extract if hasattr(extractor, "extract") else extractor
    return list(fn(text))


def _memory_on(db: Session, job: m.MemoryJob) -> bool:
    from app.api.me import memory_enabled

    return memory_enabled(db, UserContext(app_id=job.app_id, user_id=job.user_id))


def _messages(db: Session, job: m.MemoryJob) -> list[m.SessionMessage]:
    return list(db.scalars(select(m.SessionMessage).where(
        m.SessionMessage.job_id == job.id,
        m.SessionMessage.app_id == job.app_id,
        m.SessionMessage.user_id == job.user_id,
    ).order_by(m.SessionMessage.created_at)))


def _render(messages: list[m.SessionMessage]) -> str:
    return "\n".join(f"[{msg.role}] {redact(msg.content)}" for msg in messages)


def _source_msg_id(messages: list[m.SessionMessage]) -> str | None:
    user_msgs = [msg for msg in messages if msg.role == "user"]
    picked = (user_msgs or messages)
    return picked[-1].msg_id if picked else None


def _finish(db: Session, job: m.MemoryJob, decision: str, fact_ids: list[str]) -> str:
    job.status = "done"
    job.locked_until = None
    job.last_error = None
    db.add(m.AuditLog(
        app_id=job.app_id, user_id=job.user_id, action="write",
        fact_ids=fact_ids, decision=decision,
    ))
    db.commit()
    log.info("job=%s user=%s decision=%s n=%s prompt=%s", job.id, mask_user_id(job.user_id), decision, len(fact_ids), PROMPT_VERSION)
    return decision


def _active_value(rows: list, content: str, attr: str) -> str | None:
    if not rows:
        return None
    if any(norm(getattr(row, attr)) == norm(content) for row in rows):
        return content
    return getattr(rows[0], attr)


def _apply_fact(db: Session, job: m.MemoryJob, subject: str, content: str, proposal: str,
                source_msg_id: str | None) -> tuple[str, str | None]:
    rows = list(db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.app_id == job.app_id,
        m.MemoryFact.user_id == job.user_id,
        m.MemoryFact.valid_to.is_(None),
    )))
    active = [row for row in rows if norm(row.subject) == norm(subject)]
    decision = decide(_active_value(active, content, "content"), content, proposal=proposal)
    if decision == "NOOP":
        return "noop", None
    now = datetime.now(timezone.utc)
    for row in active:
        row.valid_to = now
    version = max((row.version for row in active), default=0) + 1
    created = m.MemoryFact(
        app_id=job.app_id, user_id=job.user_id, subject=subject, content=content,
        version=version, valid_from=now, source_session_id=job.session_id, source_msg_id=source_msg_id,
    )
    db.add(created)
    db.flush()
    return decision.lower(), created.id


def _apply_preference(db: Session, job: m.MemoryJob, key: str, value: str, proposal: str) -> tuple[str, str | None]:
    rows = list(db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.app_id == job.app_id,
        m.MemoryPreference.user_id == job.user_id,
        m.MemoryPreference.valid_to.is_(None),
    )))
    active = [row for row in rows if norm(row.key) == norm(key)]
    decision = decide(_active_value(active, value, "value"), value, proposal=proposal)
    if decision == "NOOP":
        return "noop", None
    now = datetime.now(timezone.utc)
    for row in active:
        row.valid_to = now
    version = max((row.version for row in active), default=0) + 1
    created = m.MemoryPreference(
        app_id=job.app_id, user_id=job.user_id, key=key, value=value,
        version=version, valid_from=now,
    )
    db.add(created)
    db.flush()
    return decision.lower(), created.id


def _apply(db: Session, job: m.MemoryJob, candidate, source_msg_id: str | None) -> tuple[str, str | None]:
    subject = redact(candidate.subject).strip()
    content = redact(candidate.content).strip()
    if not subject or not content or is_injection(subject) or is_injection(content):
        return "noop", None
    if candidate.kind == "preference":
        return _apply_preference(db, job, subject, content, candidate.action)
    return _apply_fact(db, job, subject, content, candidate.action, source_msg_id)


def _summarize(decisions: list[str]) -> str:
    if "update" in decisions:
        return "update"
    if "add" in decisions:
        return "add"
    return "noop"


def _extract_and_write(db: Session, job: m.MemoryJob, extractor) -> tuple[str, list[str]]:
    if not _memory_on(db, job):
        return "skip", []
    messages = _messages(db, job)
    text = strip_injection(_render(messages)).strip()
    if not text:
        return "noop", []
    candidates = _call_extractor(extractor, text)
    source_msg_id = _source_msg_id(messages)
    decisions: list[str] = []
    ids: list[str] = []
    for candidate in candidates:
        decision, new_id = _apply(db, job, candidate, source_msg_id)
        decisions.append(decision)
        if new_id:
            ids.append(new_id)
    return _summarize(decisions), ids


def process_job(db: Session, job: m.MemoryJob, extractor) -> str:
    """处理一条已领取的任务。成功、跳过、丢弃非法输出都把 status 置为 done。可重试错误向外抛。"""
    job_id = job.id
    current = db.scalar(select(m.MemoryJob).where(m.MemoryJob.id == job_id).with_for_update())
    if current is None:
        return "missing"
    if current.status == "done":
        db.commit()
        return "done"
    try:
        decision, fact_ids = _extract_and_write(db, current, extractor)
    except InvalidExtraction:
        db.rollback()
        current = db.scalar(select(m.MemoryJob).where(m.MemoryJob.id == job_id).with_for_update())
        log.info("job=%s user=%s decision=discard prompt=%s", job_id, mask_user_id(current.user_id), PROMPT_VERSION)
        return _finish(db, current, "discard", [])
    except Exception:
        db.rollback()
        raise
    return _finish(db, current, decision, fact_ids)


def run_once(db: Session, extractor) -> m.MemoryJob | None:
    """领一条并处理。没有待处理任务时返回 None。失败写入 last_error，超限后 status=dead。"""
    job = claim_job(db)
    if not job:
        return None
    job_id = job.id
    try:
        process_job(db, job, extractor)
    except Exception as e:
        db.rollback()
        failed = db.get(m.MemoryJob, job_id)
        if failed is None:
            raise
        fail_job(db, failed, str(e))
        log.warning("job=%s attempts=%s status=%s error=%s", job_id, failed.attempts, failed.status, failed.last_error)
    return db.get(m.MemoryJob, job_id)


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.extract import build_extractor

    extractor = build_extractor()
    log.info("worker started prompt=%s", PROMPT_VERSION)
    while True:
        with WorkerSession() as db:
            job = run_once(db, extractor)
        if job is None:
            time.sleep(2)
        elif job.status == "pending":
            time.sleep(min(2 ** max(job.attempts, 1), 30))


if __name__ == "__main__":  # pragma: no cover
    main()

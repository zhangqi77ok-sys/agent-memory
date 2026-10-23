"""写入 Worker：领取 memory_job，脱敏后抽取，再按代码规则入库。

领取仍是 SKIP LOCKED + 处理超时 + 最多 3 次重试。
抽取失败（网络/5xx）回到 pending，超过次数进 dead；模型输出不合法则丢弃该批并完成任务。
事实入库时同步生成 Gemini 向量（未配 GEMINI_API_KEY 或调用失败则留空，召回自动走关键词）。
缺 LLM 配置时进程直接退出，不用假抽取器。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.db import WorkerSession
from app.extract import MAX_SUBJECT_LEN, PROMPT_VERSION, InvalidExtraction, decide, is_injection, norm, same_subject, strip_injection
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


def expire_memories(db: Session, ttl_days: int = 0, now: datetime | None = None) -> int:
    """定期将超过保留时长的有效记忆标记为过期失效（valid_to = now）。

    返回标记失效的条数。
    """
    if ttl_days <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    count = 0

    # 事实过期
    facts = db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.valid_to.is_(None),
        m.MemoryFact.valid_from < cutoff,
    )).all()
    for f in facts:
        f.valid_to = now
        count += 1

    # 偏好过期
    prefs = db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.valid_to.is_(None),
        m.MemoryPreference.valid_from < cutoff,
    )).all()
    for p in prefs:
        p.valid_to = now
        count += 1

    # 经验过期（第七轮补：新表此前不在 TTL 覆盖内，只进不出）
    insights = db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.valid_to.is_(None),
        m.MemoryInsight.valid_from < cutoff,
    )).all()
    for ins in insights:
        ins.valid_to = now
        count += 1

    # 轨迹过期：原始过程比经验更敏感，不做软失效，直接删行（已提炼的经验按上一条独立过期）
    old_episodes = db.scalars(select(m.TaskEpisode).where(
        m.TaskEpisode.created_at < cutoff,
    )).all()
    for ep in old_episodes:
        db.delete(ep)
        count += 1

    if count > 0:
        db.commit()
        log.info("expired memories count=%s ttl_days=%s", count, ttl_days)
    return count



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


def _embed_fact(embedder, fact: m.MemoryFact) -> None:
    """给一条新事实补向量。失败只记警告：向量是增强项，不能拖垮写入任务。"""
    try:
        vec = embedder.embed([f"{fact.subject} {fact.content}"], task_type="RETRIEVAL_DOCUMENT")[0]
    except Exception as e:
        log.warning("embed fact=%s failed: %s", fact.id, e)
        return
    fact.embedding = vec
    fact.embed_model = getattr(embedder, "model", None)


def _matched_facts(db: Session, job: m.MemoryJob, subject: str) -> list[m.MemoryFact]:
    """找与 subject 说同一件事的有效事实。先只查 (id, subject) 轻列做相似匹配，
    命中的才整行加载——不再把该用户的全部事实拉进内存逐条比。
    相似合并（same_subject）顺带治旧账：历史上字面不同、意思相同的重复行会在下一次写入时被一并失效。"""
    light = db.execute(select(m.MemoryFact.id, m.MemoryFact.subject).where(
        m.MemoryFact.app_id == job.app_id,
        m.MemoryFact.user_id == job.user_id,
        m.MemoryFact.valid_to.is_(None),
    )).all()
    ids = [fid for fid, subj in light if same_subject(subj, subject)]
    if not ids:
        return []
    return list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.id.in_(ids))))


def _matched_preferences(db: Session, job: m.MemoryJob, key: str) -> list[m.MemoryPreference]:
    light = db.execute(select(m.MemoryPreference.id, m.MemoryPreference.key).where(
        m.MemoryPreference.app_id == job.app_id,
        m.MemoryPreference.user_id == job.user_id,
        m.MemoryPreference.valid_to.is_(None),
    )).all()
    ids = [pid for pid, k in light if same_subject(k, key)]
    if not ids:
        return []
    return list(db.scalars(select(m.MemoryPreference).where(m.MemoryPreference.id.in_(ids))))


def _apply_fact(db: Session, job: m.MemoryJob, subject: str, content: str, proposal: str,
                source_msg_id: str | None, embedder=None) -> tuple[str, str | None]:
    active = _matched_facts(db, job, subject)
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
    if embedder is not None:
        _embed_fact(embedder, created)
    return decision.lower(), created.id


def _apply_preference(db: Session, job: m.MemoryJob, key: str, value: str, proposal: str) -> tuple[str, str | None]:
    active = _matched_preferences(db, job, key)
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


def _apply(db: Session, job: m.MemoryJob, candidate, source_msg_id: str | None, embedder=None) -> tuple[str, str | None]:
    subject = redact(candidate.subject).strip()
    content = redact(candidate.content).strip()
    if not subject or not content or len(subject) > MAX_SUBJECT_LEN or is_injection(subject) or is_injection(content):
        return "noop", None
    if candidate.kind == "preference":
        return _apply_preference(db, job, subject, content, candidate.action)
    return _apply_fact(db, job, subject, content, candidate.action, source_msg_id, embedder=embedder)


def _summarize(decisions: list[str]) -> str:
    if "update" in decisions:
        return "update"
    if "add" in decisions:
        return "add"
    return "noop"


def _lock_user(db: Session, job: m.MemoryJob) -> None:
    """同一用户的写入串行化。job 领取的 SKIP LOCKED 只锁任务行，两个 Worker 并发处理
    同一用户时 _apply 的读-改-写会竞态出双 active。Postgres 用事务级咨询锁兜底
    （随 commit/rollback 自动释放）；SQLite 单写者天然串行，跳过。"""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
                   {"k": f"{job.app_id}:{job.user_id}"})


def _extract_and_write(db: Session, job: m.MemoryJob, extractor, embedder=None) -> tuple[str, list[str]]:
    if not _memory_on(db, job):
        return "skip", []
    _lock_user(db, job)
    messages = _messages(db, job)
    text = strip_injection(_render(messages)).strip()
    if not text:
        return "noop", []
    candidates = _call_extractor(extractor, text)
    source_msg_id = _source_msg_id(messages)
    decisions: list[str] = []
    ids: list[str] = []
    for candidate in candidates:
        decision, new_id = _apply(db, job, candidate, source_msg_id, embedder=embedder)
        decisions.append(decision)
        if new_id:
            ids.append(new_id)
    return _summarize(decisions), ids


def process_job(db: Session, job: m.MemoryJob, extractor, embedder=None) -> str:
    """处理一条已领取的任务。成功、跳过、丢弃非法输出都把 status 置为 done。可重试错误向外抛。"""
    job_id = job.id
    current = db.scalar(select(m.MemoryJob).where(m.MemoryJob.id == job_id).with_for_update())
    if current is None:
        return "missing"
    if current.status == "done":
        db.commit()
        return "done"
    try:
        decision, fact_ids = _extract_and_write(db, current, extractor, embedder=embedder)
    except InvalidExtraction:
        db.rollback()
        current = db.scalar(select(m.MemoryJob).where(m.MemoryJob.id == job_id).with_for_update())
        log.info("job=%s user=%s decision=discard prompt=%s", job_id, mask_user_id(current.user_id), PROMPT_VERSION)
        return _finish(db, current, "discard", [])
    except Exception:
        db.rollback()
        raise
    return _finish(db, current, decision, fact_ids)


def run_once(db: Session, extractor, embedder=None) -> m.MemoryJob | None:
    """领一条并处理。没有待处理任务时返回 None。失败写入 last_error，超限后 status=dead。"""
    job = claim_job(db)
    if not job:
        return None
    job_id = job.id
    try:
        process_job(db, job, extractor, embedder=embedder)
    except Exception as e:
        db.rollback()
        failed = db.get(m.MemoryJob, job_id)
        if failed is None:
            raise
        fail_job(db, failed, str(e))
        log.warning("job=%s attempts=%s status=%s error=%s", job_id, failed.attempts, failed.status, failed.last_error)
    return db.get(m.MemoryJob, job_id)


def _post_run_wait(job, ep) -> float:
    """主循环一轮结束后的等待秒数（纯函数，可测试）。

    - job 仍 pending：指数退避，上限 30s；
    - 其余（无 job / job 已终态）：固定 2s，避免空转。
    """
    if job is not None and job.status == "pending":
        return float(min(2 ** max(job.attempts, 1), 30))
    return 2.0


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.config import settings
    from app.distill import build_distiller
    from app.embeddings import build_embedder
    from app.episodes import run_episode_once
    from app.extract import build_extractor

    extractor = build_extractor()
    distiller = build_distiller()
    embedder = build_embedder()
    log.info("worker started prompt=%s embedding=%s", PROMPT_VERSION,
             embedder.model if embedder is not None else "off")
    last_cleanup = 0.0
    while True:
        with WorkerSession() as db:
            if time.time() - last_cleanup > 3600:
                try:
                    if settings.memory_ttl_days > 0:
                        expire_memories(db, settings.memory_ttl_days)
                    # 经验生命周期巡检与 TTL 同一个小时节拍（第十一轮）：衰减/合并/矛盾计数
                    from app.episodes import consolidate_insights
                    stats = consolidate_insights(db)
                    if stats["decayed"] or stats["merged"] or stats["conflicts"]:
                        log.info("consolidate_insights: %s", stats)
                    last_cleanup = time.time()
                except Exception as e:
                    log.warning("cleanup error: %s", e)
            job = run_once(db, extractor, embedder=embedder)
            ep = None
            for _ in range(5):  # 一轮最多连提 5 条：历史回放批量导入时不至于一条一轮慢慢磨（第八轮补）
                ep = run_episode_once(db, distiller, embedder=embedder)
                if ep is None:
                    break
        time.sleep(_post_run_wait(job, ep))


if __name__ == "__main__":  # pragma: no cover
    main()

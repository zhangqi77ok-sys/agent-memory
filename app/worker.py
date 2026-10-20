"""写入 Worker：领取 memory_job。

v1 骨架只实现任务领取（SKIP LOCKED + 处理超时 + 最多 3 次重试）；
抽取→脱敏→嵌入→去重→判定 的管线在下一个提交实现，目前领取后不改写记忆。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import models as m
from app.db import SessionLocal

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


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    while True:
        with SessionLocal(info={"rls_bypass": True}) as db:
            job = claim_job(db)
            if not job:
                time.sleep(2)
                continue
            log.info("claimed job=%s user=%s (pipeline not implemented yet)", job.id, job.user_id)


if __name__ == "__main__":  # pragma: no cover
    main()

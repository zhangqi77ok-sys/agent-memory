from app import models as m
from app.worker import MAX_ATTEMPTS, claim_job, fail_job


def test_claim_and_dead_letter(db):
    db.add(m.MemoryJob(user_id="alice", idempotency_key="k", session_id="s"))
    db.commit()
    for i in range(MAX_ATTEMPTS):
        job = claim_job(db)
        assert job and job.attempts == i + 1 and job.status == "running"
        fail_job(db, job, "boom")
    assert job.status == "dead"
    assert claim_job(db) is None

from app import models as m


def test_models_have_no_tenant_id():
    for t in m.Base.metadata.tables.values():
        assert "tenant_id" not in t.c, t.name


def test_new_rows_default_app_id(db):
    f = m.MemoryFact(user_id="alice", subject="s", content="c")
    a = m.AuditLog(user_id="alice", action="recall", fact_ids=[])
    db.add_all([f, a])
    db.commit()
    db.refresh(f)
    db.refresh(a)
    assert f.app_id == "tiancode"
    assert a.app_id == "tiancode"


def test_parallel_constraints_exist():
    names = {c.name for c in m.MemoryJob.__table__.constraints} | {c.name for c in m.UserSetting.__table__.constraints}
    assert {"uq_job_idem_app", "uq_user_setting_app"} <= names
    assert "uq_job_idem" not in names and "uq_user_setting" not in names
    idx = {i.name for i in m.MemoryFact.__table__.indexes}
    assert "ix_fact_app_user_subject" in idx
    assert "ix_fact_user_subject" not in idx


def test_rows_written_via_api_carry_config_app_id(client, db):
    from tests.conftest import consent
    consent(client, "alice")
    r = client.post("/v1/memories", headers={"Idempotency-Key": "k"},
                    json={"user_id": "alice", "session_id": "s", "messages": [{"msg_id": "1", "role": "user", "content": "hi"}]})
    assert r.status_code == 202
    job = db.query(m.MemoryJob).one()
    assert (job.app_id, job.user_id) == ("tiancode", "alice")
    assert db.query(m.UserSetting).filter_by(user_id="alice").one().app_id == "tiancode"


def test_bearer_header_is_ignored(client):
    r = client.get("/v1/me/settings", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 422  # 只认 user_id，不解析任何 token


def test_other_app_rows_are_invisible(client, db):
    from tests.conftest import consent, uid
    consent(client, "alice")
    db.add(m.MemoryFact(app_id="other", user_id="alice", subject="s", content="别的应用的记忆"))
    db.commit()
    assert client.get("/v1/me/memories", params=uid("alice")).json()["total"] == 0


def test_worker_scopes_by_app_id(db, monkeypatch):
    from app import worker
    db.add(m.UserSetting(user_id="alice", memory_enabled=True, consent_version=__import__("app.schemas", fromlist=["x"]).CONSENT_VERSION))
    job = m.MemoryJob(user_id="alice", idempotency_key="k", session_id="s")
    db.add(job)
    db.flush()
    db.add(m.SessionMessage(user_id="alice", session_id="s", job_id=job.id, msg_id="1", role="user", content="我住在杭州"))
    db.add(m.SessionMessage(app_id="other", user_id="alice", session_id="s", job_id=job.id, msg_id="2", role="user", content="别的应用"))
    db.commit()
    assert [x.content for x in worker._messages(db, job)] == ["我住在杭州"]

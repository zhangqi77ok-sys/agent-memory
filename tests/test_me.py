"""「我的记忆」面板接口：同意、开关、查看、纠正、清空、导出、任务状态。"""
from app import models as m
from app.schemas import CONSENT_VERSION
from tests.conftest import consent, token


def _write(client, user, key="k1"):
    return client.post("/v1/memories", headers={**token(user), "Idempotency-Key": key},
                       json={"session_id": "s1", "messages": [{"msg_id": "m1", "role": "user", "content": "hi"}]})


def test_without_consent_nothing_is_written_or_recalled(client, db):
    r = _write(client, "alice")
    assert r.status_code == 202 and r.json()["skipped"] is True and r.json()["job_id"] is None
    assert db.query(m.SessionMessage).count() == 0 and db.query(m.MemoryJob).count() == 0
    db.add(m.MemoryFact(user_id="alice", subject="lang", content="喜欢 Python"))
    db.commit()
    assert client.post("/v1/memories/recall", headers=token("alice"), json={"query": "喜欢"}).json()["facts"] == []


def test_enable_requires_current_consent_version(client):
    h = token("alice")
    assert client.put("/v1/me/settings", headers=h, json={"memory_enabled": True}).status_code == 409
    assert client.put("/v1/me/settings", headers=h,
                      json={"memory_enabled": True, "consent_version": "old"}).status_code == 409
    r = client.put("/v1/me/settings", headers=h, json={"memory_enabled": True, "consent_version": CONSENT_VERSION})
    assert r.json()["memory_enabled"] is True and r.json()["consent_at"]


def test_pause_stops_write_and_recall(client, db):
    consent(client, "alice")
    db.add(m.MemoryFact(user_id="alice", subject="lang", content="喜欢 Python"))
    db.commit()
    assert client.post("/v1/memories/recall", headers=token("alice"), json={"query": "喜欢"}).json()["facts"]
    client.put("/v1/me/settings", headers=token("alice"), json={"memory_enabled": False})
    assert _write(client, "alice").json()["skipped"] is True
    assert client.post("/v1/memories/recall", headers=token("alice"), json={"query": "喜欢"}).json()["facts"] == []
    # 重新开启不需要再次同意（同一隐私说明版本）
    r = client.put("/v1/me/settings", headers=token("alice"),
                   json={"memory_enabled": True, "consent_version": CONSENT_VERSION})
    assert r.status_code == 200


def test_list_shows_only_mine_and_current_versions(client, db):
    from datetime import UTC, datetime
    db.add_all([m.MemoryFact(user_id="alice", subject="city", content="北京", version=1, valid_to=datetime.now(UTC)),
                m.MemoryFact(user_id="alice", subject="city", content="成都", version=2, source_session_id="s9"),
                m.MemoryPreference(user_id="alice", key="lang", value="zh"),
                m.MemoryFact(user_id="bob", subject="city", content="上海")])
    db.commit()
    body = client.get("/v1/me/memories", headers=token("alice")).json()
    assert body["total"] == 2
    assert {(i["kind"], i["content"]) for i in body["items"]} == {("fact", "成都"), ("preference", "zh")}
    assert next(i for i in body["items"] if i["kind"] == "fact")["source_session_id"] == "s9"
    only = client.get("/v1/me/memories?kind=preference", headers=token("alice")).json()
    assert [i["subject"] for i in only["items"]] == ["lang"]


def test_correct_creates_new_version_and_old_is_not_recalled(client, db):
    consent(client, "alice")
    f = m.MemoryFact(user_id="alice", subject="city", content="住在北京")
    db.add(f)
    db.commit()
    r = client.patch(f"/v1/me/memories/{f.id}", headers=token("alice"), json={"content": "住在成都"})
    assert r.status_code == 200 and r.json()["version"] == 2
    contents = [x["content"] for x in client.post("/v1/memories/recall", headers=token("alice"),
                                                    json={"query": "住在"}).json()["facts"]]
    assert contents == ["住在成都"]
    # 已失效的旧版本不能再纠正
    assert client.patch(f"/v1/me/memories/{f.id}", headers=token("alice"), json={"content": "x"}).status_code == 404


def test_cannot_correct_someone_elses_memory(client, db):
    f = m.MemoryFact(user_id="bob", subject="city", content="上海")
    db.add(f)
    db.commit()
    assert client.patch(f"/v1/me/memories/{f.id}", headers=token("alice"), json={"content": "x"}).status_code == 404
    db.expire_all()
    assert db.get(m.MemoryFact, f.id).content == "上海"


def test_clear_all_removes_everything_of_mine_only(client, db):
    consent(client, "alice")
    _write(client, "alice")
    db.add_all([m.MemoryFact(user_id="alice", subject="a", content="1"),
                m.MemoryPreference(user_id="alice", key="k", value="v"),
                m.SessionSummary(user_id="alice", session_id="s1", summary="sum"),
                m.MemoryFact(user_id="bob", subject="b", content="2")])
    db.commit()
    assert client.delete("/v1/me/memories", headers=token("alice")).status_code == 204
    db.expire_all()
    for model in (m.MemoryFact, m.MemoryPreference, m.SessionSummary, m.SessionMessage, m.MemoryJob):
        assert db.query(model).filter_by(user_id="alice").count() == 0
    assert db.query(m.MemoryFact).filter_by(user_id="bob").count() == 1
    assert db.query(m.ForgottenId).count() == 2


def test_export_contains_history_and_no_other_users(client, db):
    from datetime import UTC, datetime
    db.add_all([m.MemoryFact(user_id="alice", subject="city", content="北京", valid_to=datetime.now(UTC)),
                m.MemoryFact(user_id="alice", subject="city", content="成都", version=2),
                m.MemoryFact(user_id="bob", subject="city", content="上海")])
    db.commit()
    body = client.get("/v1/me/memories/export", headers=token("alice")).json()
    assert body["user_id"] == "alice"
    assert sorted(f["content"] for f in body["facts"]) == ["北京", "成都"]
    assert "embedding" not in body["facts"][0]


def test_jobs_status_lists_my_jobs(client):
    consent(client, "alice", "bob")
    _write(client, "alice")
    _write(client, "bob")
    jobs = client.get("/v1/me/jobs", headers=token("alice")).json()
    assert len(jobs) == 1 and jobs[0]["status"] == "pending"


def test_me_endpoints_require_token(client):
    for method, path in [("get", "/v1/me/memories"), ("get", "/v1/me/settings"), ("delete", "/v1/me/memories"),
                         ("get", "/v1/me/memories/export"), ("get", "/v1/me/jobs")]:
        assert getattr(client, method)(path).status_code == 401

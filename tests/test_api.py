from datetime import UTC

from app import models as m
from tests.conftest import token


def _write(client, user, key="k1"):
    return client.post("/v1/memories", headers={**token(user), "Idempotency-Key": key},
                       json={"session_id": "s1", "messages": [{"msg_id": "m1", "role": "user", "content": "我喜欢用 Python"}]})


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_requires_token(client):
    assert client.post("/v1/memories/recall", json={"query": "x"}).status_code == 401


def test_write_is_idempotent(client):
    a, b = _write(client, "alice"), _write(client, "alice")
    assert a.status_code == b.status_code == 202
    assert a.json()["job_id"] == b.json()["job_id"] and b.json()["duplicate"] is True


def test_body_cannot_carry_identity(client):
    r = client.post("/v1/memories/recall", headers=token("alice"), json={"query": "x", "user_id": "bob"})
    assert r.status_code == 422


def test_no_cross_user_recall(client, db):
    db.add(m.MemoryFact(user_id="bob", subject="lang", content="bob 喜欢 Go"))
    db.add(m.MemoryFact(user_id="alice", subject="lang", content="alice 喜欢 Python"))
    db.commit()
    facts = client.post("/v1/memories/recall", headers=token("alice"), json={"query": "喜欢"}).json()["facts"]
    assert facts and all("bob" not in f["content"] for f in facts)


def test_invalidated_fact_not_recalled(client, db):
    from datetime import datetime
    db.add(m.MemoryFact(user_id="alice", subject="city", content="住在北京", valid_to=datetime.now(UTC), version=1))
    db.add(m.MemoryFact(user_id="alice", subject="city", content="住在成都", version=2))
    db.commit()
    contents = [f["content"] for f in client.post("/v1/memories/recall", headers=token("alice"), json={"query": "住在"}).json()["facts"]]
    assert contents == ["住在成都"]


def test_forget_deletes_all_versions_and_hides_others(client, db):
    old = m.MemoryFact(user_id="alice", subject="city", content="住在北京", version=1)
    new = m.MemoryFact(user_id="alice", subject="city", content="住在成都", version=2)
    bobs = m.MemoryFact(user_id="bob", subject="city", content="住在上海")
    db.add_all([old, new, bobs])
    db.commit()
    old_id, new_id, bob_id = old.id, new.id, bobs.id
    assert client.delete(f"/v1/me/memories/{bob_id}", headers=token("alice")).status_code == 404
    assert client.delete(f"/v1/me/memories/{new_id}", headers=token("alice")).status_code == 204
    db.expire_all()
    assert db.query(m.MemoryFact).filter_by(user_id="alice").count() == 0
    assert {f.id for f in db.query(m.ForgottenId).all()} == {old_id, new_id}
    assert db.query(m.MemoryFact).filter_by(user_id="bob").count() == 1

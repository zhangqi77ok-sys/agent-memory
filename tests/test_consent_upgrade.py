
from app import schemas
from app.api import me as api_me


def test_consent_upgrade_lifecycle(client, monkeypatch):
    user_id = "alice"

    # 1. 初始状态：用户在 2026-10-v1 下同意
    r = client.put(
        "/v1/me/settings",
        json={"user_id": user_id, "memory_enabled": True, "consent_version": "2026-10-v1"},
    )
    assert r.status_code == 200
    assert r.json()["memory_enabled"] is True
    assert r.json()["consent_version"] == "2026-10-v1"

    # 写入一条事实并确认接受
    r = client.post(
        "/v1/memories",
        headers={"Idempotency-Key": "k-v1"},
        json={
            "user_id": user_id,
            "session_id": "s1",
            "messages": [{"msg_id": "m1", "role": "user", "content": "我喜欢喝黑咖啡"}],
        },
    )
    assert r.status_code == 202
    assert r.json()["skipped"] is False
    assert r.json()["job_id"] is not None

    # 2. 系统升级隐私协议版本至 2026-11-v1
    NEW_VERSION = "2026-11-v1"
    monkeypatch.setattr(schemas, "CONSENT_VERSION", NEW_VERSION)
    monkeypatch.setattr(api_me, "CONSENT_VERSION", NEW_VERSION)

    # 3. 升级后未重新同意：写入自动跳过（暂停落库，不阻断调用方）
    r = client.post(
        "/v1/memories",
        headers={"Idempotency-Key": "k-v2"},
        json={
            "user_id": user_id,
            "session_id": "s2",
            "messages": [{"msg_id": "m2", "role": "user", "content": "今天天气真好"}],
        },
    )
    assert r.status_code == 202
    assert r.json()["skipped"] is True
    assert r.json()["job_id"] is None

    # 召回返回空（暂停服务直至重新同意）
    r = client.post(
        "/v1/memories/recall",
        json={"user_id": user_id, "query": "咖啡"},
    )
    assert r.status_code == 200
    assert r.json()["facts"] == []
    assert r.json()["preferences"] == {}

    # 查看 settings，发现版本不匹配（提示重新同意）
    r = client.get(f"/v1/me/settings?user_id={user_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["consent_version"] == "2026-10-v1"
    assert data["current_consent_version"] == NEW_VERSION

    # 4. 如果用旧版本提交同意，报 409 Conflict
    r = client.put(
        "/v1/me/settings",
        json={"user_id": user_id, "memory_enabled": True, "consent_version": "2026-10-v1"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "consent_required"

    # 5. 用户在前端授权弹窗中同意新版本
    r = client.put(
        "/v1/me/settings",
        json={"user_id": user_id, "memory_enabled": True, "consent_version": NEW_VERSION},
    )
    assert r.status_code == 200
    assert r.json()["consent_version"] == NEW_VERSION
    assert r.json()["memory_enabled"] is True

    # 6. 重新同意后，写入恢复正常
    r = client.post(
        "/v1/memories",
        headers={"Idempotency-Key": "k-v3"},
        json={
            "user_id": user_id,
            "session_id": "s3",
            "messages": [{"msg_id": "m3", "role": "user", "content": "我重新同意了"}],
        },
    )
    assert r.status_code == 202
    assert r.json()["skipped"] is False
    assert r.json()["job_id"] is not None

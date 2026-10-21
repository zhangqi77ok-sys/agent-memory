from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, create_app


def test_missing_service_key_returns_401():
    raw_client = TestClient(app)
    # 读、写、导出、设置、召回、删除均受保护
    assert raw_client.get("/v1/me/settings?user_id=alice").status_code == 401
    assert raw_client.get("/v1/me/memories?user_id=alice").status_code == 401
    assert raw_client.get("/v1/me/memories/export?user_id=alice").status_code == 401
    assert raw_client.get("/v1/me/jobs?user_id=alice").status_code == 401
    assert raw_client.post("/v1/memories", json={"user_id": "alice"}).status_code == 401
    assert raw_client.post("/v1/memories/recall", json={"user_id": "alice", "query": "hello"}).status_code == 401
    assert raw_client.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": False}).status_code == 401
    assert raw_client.patch("/v1/me/memories/m-1", json={"user_id": "alice", "content": "x"}).status_code == 401
    assert raw_client.delete("/v1/me/memories/m-1?user_id=alice").status_code == 401
    assert raw_client.delete("/v1/me/memories?user_id=alice").status_code == 401


def test_wrong_service_key_returns_401():
    client = TestClient(app, headers={"X-Service-Key": "wrong-service-key"})
    res = client.get("/v1/me/settings?user_id=alice")
    assert res.status_code == 401
    assert res.json() == {"detail": "Unauthorized"}


def test_correct_service_key_returns_ok():
    client = TestClient(app, headers={"X-Service-Key": "test-service-key"})
    res = client.get("/v1/me/settings?user_id=alice")
    assert res.status_code == 200


def test_secondary_service_key_rotation_returns_ok():
    client = TestClient(app, headers={"X-Service-Key": "test-service-key-2"})
    res = client.get("/v1/me/settings?user_id=alice")
    assert res.status_code == 200


def test_health_endpoints_bypass_service_key():
    raw_client = TestClient(app)
    assert raw_client.get("/healthz").status_code == 200
    assert raw_client.get("/readyz").status_code == 200


def test_empty_service_keys_rejects_all():
    empty_app = create_app(Settings(service_keys=""))
    client = TestClient(empty_app, headers={"X-Service-Key": "any-key"})
    assert client.get("/v1/me/settings?user_id=alice").status_code == 401
    # 但健康检查依然正常
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

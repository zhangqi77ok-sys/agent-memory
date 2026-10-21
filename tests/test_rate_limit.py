from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_write_rate_limit_triggers_429():
    # 写接口阈值设为 2次/分钟
    s = Settings(
        rate_limit_enabled=True,
        rate_limit_write_per_min=2,
        rate_limit_read_per_min=10,
        service_keys="test-key",
    )
    custom_app = create_app(s)
    client = TestClient(custom_app, headers={"X-Service-Key": "test-key"})

    # 第 1 次写：未超限（202 或 422 均算进入并放行）
    r1 = client.post(
        "/v1/memories",
        json={"user_id": "user-lim-1", "session_id": "s1", "messages": []},
        headers={"Idempotency-Key": "k1"},
    )
    assert r1.status_code in (202, 422)

    # 第 2 次写：放行
    r2 = client.post(
        "/v1/memories",
        json={"user_id": "user-lim-1", "session_id": "s1", "messages": []},
        headers={"Idempotency-Key": "k2"},
    )
    assert r2.status_code in (202, 422)

    # 第 3 次写：超限触发 429
    r3 = client.post(
        "/v1/memories",
        json={"user_id": "user-lim-1", "session_id": "s1", "messages": []},
        headers={"Idempotency-Key": "k3"},
    )
    assert r3.status_code == 429
    assert r3.json() == {"detail": "Too Many Requests"}
    assert "Retry-After" in r3.headers


def test_export_strict_rate_limit():
    s = Settings(
        rate_limit_enabled=True,
        rate_limit_write_per_min=1,
        rate_limit_read_per_min=10,
        service_keys="test-key",
    )
    custom_app = create_app(s)
    client = TestClient(custom_app, headers={"X-Service-Key": "test-key"})

    # export 属于高消耗严格限流接口
    r1 = client.get("/v1/me/memories/export?user_id=exp-user")
    assert r1.status_code == 200

    r2 = client.get("/v1/me/memories/export?user_id=exp-user")
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


def test_read_rate_limit_and_user_isolation():
    s = Settings(
        rate_limit_enabled=True,
        rate_limit_write_per_min=10,
        rate_limit_read_per_min=2,
        service_keys="test-key",
    )
    custom_app = create_app(s)
    client = TestClient(custom_app, headers={"X-Service-Key": "test-key"})

    # user-A 读 2 次
    assert client.get("/v1/me/memories?user_id=user-A").status_code == 200
    assert client.get("/v1/me/memories?user_id=user-A").status_code == 200
    # user-A 第 3 次触发 429
    assert client.get("/v1/me/memories?user_id=user-A").status_code == 429

    # user-B 不受 user-A 影响（隔离）
    assert client.get("/v1/me/memories?user_id=user-B").status_code == 200


def test_rate_limit_disabled():
    s = Settings(
        rate_limit_enabled=False,
        rate_limit_write_per_min=1,
        service_keys="test-key",
    )
    custom_app = create_app(s)
    client = TestClient(custom_app, headers={"X-Service-Key": "test-key"})

    # 关闭限流时不被 429
    for _ in range(5):
        assert client.get("/v1/me/memories/export?user_id=unlimited-user").status_code == 200

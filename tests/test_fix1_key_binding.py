"""修复第 1 轮：未绑定服务密钥不得自断言他人身份（审计 E 清单第 1 组）。

口径（见 fix1-design-draft.md）：
- 默认（ALLOW_UNBOUND_ASSERTION 未设）：经中间件的远程断言一律 403，
  错误码 unbound_assertion_denied；无 key 仍 401（中间件在前）。
- 绑定密钥：查询与 body 里的 user_id 断言全部失效，身份钉死为绑定用户。
- 显式 opt-in（ALLOW_UNBOUND_ASSERTION=1）：恢复第 31 轮行为，单用户路径全通。
- 进程内直调（request=None，如 MCP）：调用方即进程身份，不受默认拒绝影响。
"""

from fastapi.testclient import TestClient

from app.auth import resolve_asserted, user_from_query
from app.config import Settings
from app.main import create_app
from app.schemas import CONSENT_VERSION


def _deny_app():
    # 注意：pydantic-settings 会用环境变量补未显式传的字段；测试环境 conftest 已设
    # ALLOW_UNBOUND_ASSERTION=1，故此处必须显式传 False，否则默认拒绝测不到。
    return create_app(Settings(service_keys="k1", app_env="test", allow_unbound_assertion=False))


def _bound_app():
    return create_app(Settings(service_keys="k1", service_key_users="k1:alice", app_env="test"))


def _optin_app():
    return create_app(Settings(service_keys="k1", allow_unbound_assertion=True, app_env="test"))


def _deny_client():
    return TestClient(_deny_app(), headers={"X-Service-Key": "k1", "idempotency-key": "fix1-test-key"})


def test_deny_unbound_assertion_read_write_delete_export():
    c = _deny_client()
    cases = [
        ("GET", "/v1/me/settings?user_id=alice", None),
        ("GET", "/v1/me/memories?user_id=alice", None),
        ("GET", "/v1/me/memories/export?user_id=alice", None),
        ("GET", "/v1/me/jobs?user_id=alice", None),
        (
            "POST",
            "/v1/memories",
            {"user_id": "alice", "session_id": "s-1", "messages": [{"msg_id": "m-1", "role": "user", "content": "hello"}]},
        ),
        ("POST", "/v1/memories/recall", {"user_id": "alice", "query": "hello"}),
        ("PUT", "/v1/me/settings", {"user_id": "alice", "memory_enabled": False}),
        ("PATCH", "/v1/me/memories/m-1", {"user_id": "alice", "content": "x"}),
        ("DELETE", "/v1/me/memories/m-1?user_id=alice", None),
        ("DELETE", "/v1/me/memories?user_id=alice", None),
    ]
    for method, url, body in cases:
        r = getattr(c, method.lower())(url, json=body) if body is not None else getattr(c, method.lower())(url)
        assert r.status_code == 403, (method, url, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "unbound_assertion_denied", (method, url, r.text)


def test_deny_still_401_without_key():
    raw = TestClient(_deny_app())
    assert raw.get("/v1/me/settings?user_id=alice").status_code == 401
    assert raw.get("/healthz").status_code == 200


def test_deny_missing_user_id_still_422():
    c = _deny_client()
    assert c.get("/v1/me/settings").status_code == 422


def test_bound_key_identity_pinned():
    c = TestClient(_bound_app(), headers={"X-Service-Key": "k1", "idempotency-key": "fix1-bound-key"})
    # 以 bob 的名义开同意，实际落到 alice 名下；再以 bob 的名义读，读到的也是 alice。
    r = c.put("/v1/me/settings", json={"user_id": "bob", "memory_enabled": True, "consent_version": CONSENT_VERSION})
    assert r.status_code == 200, r.text
    assert r.json()["memory_enabled"] is True
    r = c.get("/v1/me/settings?user_id=bob")
    assert r.status_code == 200
    assert r.json()["memory_enabled"] is True
    # bob 名下并无独立设置：绑定的身份钉死，不存在“bob 的记忆”。
    r = c.post(
        "/v1/memories", json={"user_id": "bob", "session_id": "s-1", "messages": [{"msg_id": "m-1", "role": "user", "content": "hi"}]}
    )
    assert r.status_code == 202, r.text
    assert r.json()["skipped"] is False


def test_optin_single_user_path_unchanged():
    c = TestClient(_optin_app(), headers={"X-Service-Key": "k1", "idempotency-key": "fix1-optin-key"})
    r = c.put("/v1/me/settings", json={"user_id": "alice", "memory_enabled": True, "consent_version": CONSENT_VERSION})
    assert r.status_code == 200, r.text
    r = c.get("/v1/me/settings?user_id=alice")
    assert r.status_code == 200
    assert r.json()["memory_enabled"] is True
    r = c.post(
        "/v1/memories", json={"user_id": "alice", "session_id": "s-1", "messages": [{"msg_id": "m-1", "role": "user", "content": "hi"}]}
    )
    assert r.status_code == 202, r.text
    assert r.json()["skipped"] is False


def test_process_direct_call_unaffected():
    # MCP 直调与旧兼容分支：request 不是 HTTP 请求，调用方即进程身份。
    assert resolve_asserted(None, "alice") == "alice"
    assert user_from_query("alice").user_id == "alice"

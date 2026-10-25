"""第二十五轮：面板身份 / 分页 / 溯源。

- 身份绑定（opt-in）：SERVICE_KEY_USERS 配了 key:user 后，该密钥即身份，
  查询与 body 里的 user_id 断言全部失效；未配置时行为与今天一致；映射里
  的 key 若不在 service_key_list 中，中间件先 401，绑定本身授不了权。
- 分页：GET /v1/me/insights 改为 InsightPage（与 MemoryPage 同构），面板
  不再静默截断在前 100 条。
- 溯源：GET /v1/me/episodes/{id} 返回轨迹详情；跨用户与不存在一律 404。
"""

from app import models as m
from app.config import settings
from tests.conftest import consent, uid

APP = settings.app_id


def _insight(db, subject, user="alice"):
    row = m.MemoryInsight(app_id=APP, user_id=user, kind="strategy",
                          subject=subject, content=f"{subject}的做法")
    db.add(row)
    db.flush()
    return row


def _episode(db, user="alice", title="部署服务"):
    ep = m.TaskEpisode(app_id=APP, user_id=user, title=title, outcome="success",
                       steps=[{"action": "探测端口", "observation": "8080 被占"},
                              {"action": "改用 8081", "observation": "启动成功"}],
                       cost={"tokens": 120})
    db.add(ep)
    db.flush()
    return ep


# ---------- 分页 ----------

def test_insights_pagination(client, db):
    for i in range(5):
        _insight(db, f"经验{i}")
    db.commit()
    page = client.get("/v1/me/insights", params={**uid("alice"), "limit": 2}).json()
    assert page["total"] == 5 and len(page["items"]) == 2 and page["offset"] == 0
    page2 = client.get("/v1/me/insights",
                       params={**uid("alice"), "limit": 2, "offset": 2}).json()
    assert len(page2["items"]) == 2 and page2["offset"] == 2
    page3 = client.get("/v1/me/insights",
                       params={**uid("alice"), "limit": 2, "offset": 4}).json()
    assert len(page3["items"]) == 1
    assert {it["subject"] for it in page["items"]} | \
        {it["subject"] for it in page2["items"]} | \
        {it["subject"] for it in page3["items"]} == {f"经验{i}" for i in range(5)}
    default = client.get("/v1/me/insights", params=uid("alice")).json()
    assert default["total"] == 5 and len(default["items"]) == 5  # 默认 limit 50
    assert client.get("/v1/me/insights", params=uid("bob")).json()["items"] == []


# ---------- 身份 ----------

def test_identity_unbound(client):
    body = client.get("/v1/me/identity", params=uid("alice")).json()
    assert body == {"app_id": APP, "user_id": "alice", "bound": False}


def test_identity_bound_overrides_assertion(client, db, monkeypatch):
    _insight(db, "只属于 alice 的经验")
    _insight(db, "只属于 bob 的经验", user="bob")
    db.commit()
    monkeypatch.setattr(settings, "service_key_users", "test-service-key:alice")
    body = client.get("/v1/me/identity", params=uid("bob")).json()
    assert body == {"app_id": APP, "user_id": "alice", "bound": True}
    page = client.get("/v1/me/insights", params=uid("bob")).json()
    assert page["total"] == 1
    assert page["items"][0]["subject"] == "只属于 alice 的经验"


def test_bound_key_alone_grants_nothing(client):
    # 映射里的 key 不在 service_key_list 中：中间件先 401
    r = client.get("/v1/me/identity", params=uid("alice"),
                   headers={"X-Service-Key": "bogus-key"})
    assert r.status_code == 401


def test_body_endpoint_bound(client, db, monkeypatch):
    consent(client, "alice")
    row = _insight(db, "绑定用户的经验")
    db.commit()
    # 未绑定：bob 断言碰 alice 的条目 → 404（不串户）
    r = client.post("/v1/insights/feedback",
                    json={"user_id": "bob", "insight_id": row.id, "helpful": True})
    assert r.status_code == 404
    # 绑定后：同一请求（body 仍断言 bob）按 alice 算 → 200
    monkeypatch.setattr(settings, "service_key_users", "test-service-key:alice")
    r = client.post("/v1/insights/feedback",
                    json={"user_id": "bob", "insight_id": row.id, "helpful": True})
    assert r.status_code == 200 and r.json()["insight_id"] == row.id


def test_missing_user_id_still_422_when_unbound(client):
    assert client.get("/v1/me/insights").status_code == 422
    assert client.get("/v1/me/identity").status_code == 422


# ---------- 端点直接调用契约（MCP 耦合，回归钉） ----------

def test_endpoint_positional_call_compat(client, db):
    """MCP 直接按旧位置签名调端点函数（write(req, Response(), key, db)）：
    request 参数必须在末位且默认 None，否则位置错位把幂等键当成 Request。
    本轮 SQLite 全套曾因此挂 3 个 MCP 测试。"""
    from fastapi import Response

    from app.api import memories as memories_api
    from app.schemas import WriteRequest

    consent(client, "alice")
    req = WriteRequest(user_id="alice", session_id="s-1",
                       messages=[{"msg_id": "m-1", "role": "user",
                                  "content": "我平时喝咖啡喜欢拿铁"}])
    accepted = memories_api.write(req, Response(), "k-direct-1", db)
    assert accepted.job_id is not None and accepted.duplicate is False


# ---------- 溯源 ----------

def test_episode_detail(client, db):
    ep = _episode(db)
    db.commit()
    body = client.get(f"/v1/me/episodes/{ep.id}", params=uid("alice")).json()
    assert body["title"] == "部署服务" and body["outcome"] == "success"
    assert len(body["steps"]) == 2 and body["steps"][0]["action"] == "探测端口"
    assert body["cost"] == {"tokens": 120} and body["distill_status"] == "pending"
    assert client.get(f"/v1/me/episodes/{ep.id}", params=uid("bob")).status_code == 404
    assert client.get("/v1/me/episodes/no-such-id", params=uid("alice")).status_code == 404


def test_insight_carries_source_episode_id(client, db):
    ep = _episode(db)
    row = _insight(db, "来自轨迹的经验")
    row.source_episode_id = ep.id
    db.commit()
    items = client.get("/v1/me/insights", params=uid("alice")).json()["items"]
    assert items[0]["source_episode_id"] == ep.id

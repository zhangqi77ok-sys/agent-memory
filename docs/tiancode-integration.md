# tiancode 业务方接入指南

本文档为 tiancode 业务后端调用 `BrainWeave Memory` 记忆微服务的接入指引与规范说明。

## 1. 架构定位与信任边界
1. **纯内部微服务**：`BrainWeave Memory` 部署于内部私有网络，仅放行业务服务器 IP 与内网网段，禁止公网直连。
2. **免登录体系**：本服务不维护用户账号密码，不发放认证令牌，亦不做用户身份认证。终端用户的真实身份校验由 tiancode 上游业务层完成；业务层完成鉴权后，在调用请求中传入 `user_id`。
3. **服务级调用鉴权**：所有 `/v1/*` 业务接口均必须携带服务凭据请求头：`X-Service-Key: <SERVICE_KEY>`。
4. **数据物理隔离**：基于 `app_id` (固定为 `tiancode`) + `user_id`，底层数据库强制启用行级安全策略（FORCE RLS），杜绝跨用户数据越权与串读。

---

## 2. 状态码与错误处理

| HTTP 状态码 | 业务含义 | 处理建议 |
|---|---|---|
| **200 / 202 / 204** | 操作成功 / 异步任务已受理 / 资源已删除 | 正常继续后续业务流程。 |
| **401 Unauthorized** | 缺失 `X-Service-Key` 或密钥错误 | 检查业务端配置的服务密钥是否与环境一致；支持多密钥轮换。 |
| **403 Forbidden** | 请求来源 IP 不在网关白名单内 | 检查业务服务器是否通过内网网段或已配置在网关允许列表中。 |
| **409 Conflict** | 隐私协议版本已升级，需用户重新同意 | 响应体返回 `{"code": "consent_required", "current_consent_version": "..."}`。需提示用户确认最新协议，再调用设置接口。 |
| **422 Unprocessable** | 请求参数错误（如 `user_id` 缺失或超长） | 检查入参格式。 |
| **429 Too Many Requests** | 触发限流阈值（针对单个 `user_id` 限速） | 读取响应头 `Retry-After: <秒数>`，在退避等待后重试。 |

---

## 3. 调用代码示例（Python）

以下示例使用 Python `requests` 库演示 tiancode 核心交互流程。

```python
import uuid
import requests

BASE_URL = "http://127.0.0.1:8090"  # 部署机或内网地址（如反代路径 /memory/v1）
SERVICE_KEY = "your-tiancode-service-key"
HEADERS = {
    "X-Service-Key": SERVICE_KEY,
    "Content-Type": "application/json",
}

def check_response(resp):
    if resp.status_code == 401:
        raise PermissionError("服务密钥无效或缺失")
    elif resp.status_code == 403:
        raise PermissionError("来源IP被网关拦截拒绝")
    elif resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "1")
        raise RuntimeError(f"请求过于频繁，请等待 {retry_after} 秒后重试")
    elif resp.status_code == 409:
        err = resp.json().get("detail", {})
        if err.get("code") == "consent_required":
            raise ValueError(f"隐私协议版本升级，当前版本：{err.get('current_consent_version')}")
    resp.raise_for_status()
    return resp

# 1. 用户隐私协议同意与开启记忆开关（初次或协议升级时调用）
def enable_memory_consent(user_id: str, consent_version: str = "2026-10-v1"):
    url = f"{BASE_URL}/v1/me/settings"
    payload = {
        "user_id": user_id,
        "memory_enabled": True,
        "consent_version": consent_version,
    }
    r = check_response(requests.put(url, json=payload, headers=HEADERS))
    return r.json()

# 2. 会话开始前：召回用户相关记忆（注入大模型提示词上下文）
def recall_user_memories(user_id: str, query: str, k: int = 8, token_budget: int = 1500):
    url = f"{BASE_URL}/v1/memories/recall"
    payload = {
        "user_id": user_id,
        "query": query,
        "k": k,
        "token_budget": token_budget,
    }
    r = check_response(requests.post(url, json=payload, headers=HEADERS))
    data = r.json()
    # data["preferences"]: 用户稳定偏好字典
    # data["facts"]: 匹配到的相关事实列表
    return data

# 3. 会话结束后：异步提交记忆提取写入任务
def submit_session_memory_write(user_id: str, session_id: str, messages: list[dict]):
    url = f"{BASE_URL}/v1/memories"
    idempotency_key = f"sess-{session_id}-{uuid.uuid4().hex[:8]}"
    headers = {**HEADERS, "Idempotency-Key": idempotency_key}
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "messages": messages,  # 格式: [{"msg_id": "...", "role": "user"|"assistant", "content": "..."}]
    }
    r = check_response(requests.post(url, json=payload, headers=headers))
    res = r.json()
    # 若用户未同意或已暂停记忆，返回 skipped=True，不阻塞调用方业务流程
    return res

# 4. 「我的记忆」面板：查询当前有效记忆列表
def list_user_memories(user_id: str, kind: str | None = None, offset: int = 0, limit: int = 50):
    url = f"{BASE_URL}/v1/me/memories"
    params = {"user_id": user_id, "offset": offset, "limit": limit}
    if kind:
        params["kind"] = kind  # fact 或 preference
    r = check_response(requests.get(url, params=params, headers=HEADERS))
    return r.json()

# 5. 用户手动纠正某条事实
def correct_memory_fact(user_id: str, item_id: str, new_content: str):
    url = f"{BASE_URL}/v1/me/memories/{item_id}"
    payload = {"user_id": user_id, "content": new_content}
    r = check_response(requests.patch(url, json=payload, headers=HEADERS))
    return r.json()

# 6. 用户删除/遗忘某条记忆
def forget_single_memory(user_id: str, item_id: str):
    url = f"{BASE_URL}/v1/me/memories/{item_id}"
    params = {"user_id": user_id}
    check_response(requests.delete(url, params=params, headers=HEADERS))
    return True

# 7. 用户清空全部个人记忆
def clear_all_user_memories(user_id: str):
    url = f"{BASE_URL}/v1/me/memories"
    params = {"user_id": user_id}
    check_response(requests.delete(url, params=params, headers=HEADERS))
    return True

# 8. 个人数据全量导出（符合个人信息保护法要求）
def export_user_data(user_id: str):
    url = f"{BASE_URL}/v1/me/memories/export"
    params = {"user_id": user_id}
    r = check_response(requests.get(url, params=params, headers=HEADERS))
    return r.json()
```

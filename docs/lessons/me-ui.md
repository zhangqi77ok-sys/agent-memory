# 「我的记忆」面板：四条合规演示

前端路径：`/memory/ui/?user_id=<用户>`，或在输入框填写 `user_id`。只调 `/v1/me/*`。

## 1. 未同意时写入不落库、召回为空

1. 新用户打开面板 → 看到授权同意页（隐私说明版本号来自 `GET /v1/me/settings` 的 `current_consent_version`）。
2. 在未点同意前，调用方 `POST /v1/memories` 返回 `skipped: true`，库里没有原文、也没有任务。
3. `POST /v1/memories/recall` 返回空。对应个保法「知情同意」与评测门禁「未授权不记」。

## 2. 纠正后旧版本失效，召回只见新值

1. 列表里点「纠正」，改内容后保存 → `PATCH /v1/me/memories/{id}`。
2. 旧行 `valid_to=now`，新行 `version+1`。召回只会看到新内容。
3. 对应「记错了吗」门禁：用户可纠偏，旧值不再被召回。

## 3. 清空或删除后，审计只留 id，查不到原文

1. 单条「删除」→ `DELETE /v1/me/memories/{id}`（事实删同 subject 全部版本；偏好删同 key 全部版本）。
2. 「清空全部」二次确认后 → `DELETE /v1/me/memories`。
3. id 记入 `forgotten_id`；审计日志只留 id，原文不可再查。对应遗忘权与备份恢复后的重删。

## 4. 关掉总开关后，新对话不再写入

1. 面板右上角总开关关闭 → `PUT /v1/me/settings {"memory_enabled": false}`。
2. 之后写入被跳过、召回为空；**已有数据保留**（要删用清空）。
3. 同一隐私说明版本再次开启，不必重新同意。

## 本地与 CI

- `cd web && npm ci && npm test && npm run build`
- Playwright：`npm run test:e2e`（mock `/memory/v1/me/*`，不依赖真后端）
- 镜像：`web/Dockerfile` → `ghcr.io/zhangqi77ok-sys/agent-memory-web`；compose 服务名 `web`，宿主机默认 `127.0.0.1:8091`
- 外层 Nginx 把 `/memory/ui/` 转到 web 容器，`/memory/` 转到 api（由 Docker运维 配置）

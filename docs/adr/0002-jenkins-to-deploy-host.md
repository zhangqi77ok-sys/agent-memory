# ADR 0002：Jenkins 放到固定 IP 的部署机，本机 SSH 部署

- 状态：已采纳（2026-10-20）

## 背景
原计划 Jenkins 在 InstaBox 的 proxy-3、应用在 proxy-4。实测：InstaBox 闲置 30 分钟会自动暂停，构建跑到一半机器睡了会直接断；InstaBox 机器只能通过各自 HTTPS 地址访问，Jenkins 到 proxy-4 的 SSH 不通。

## 决定
- Jenkins 改到一台固定公网 IP、不会自动暂停的云主机（4 核 / 7.7G / 97G，资源足够 Jenkins + 构建 + 应用）。
- 应用也部署在这台，Jenkins 通过 SSH 登录本机 `deploy` 用户执行 compose（理由见 `docs/deploy.md`）。地址只配在 Jenkins 的 `DEPLOY_HOST`，不进仓库。
- 对外统一走 nginx-gw 的 80 端口：`/jenkins/`、`/memory/`；Jenkins、api 都只绑 127.0.0.1。Jenkins 关闭匿名访问。

## 后果 / 风险
- **单点**：Jenkins、应用、数据库、本地备份都在一台机器上，这台机器坏了会一起没。缓解：`backup.sh` 支持 `BACKUP_REMOTE` 再存一份到别的机器；代码在 GitHub、镜像在 GHCR，可以在新机器上重建。
- **公网 HTTP**：当前是演示环境，Jenkins 登录走明文 HTTP。正式使用前要加 HTTPS 并限制来源 IP。
- Jenkins 容器挂了 docker.sock，等同宿主 root 权限；只给受信任的人 Jenkins 管理权限，并保持 LTS 更新。
- 将来应用迁到别的机器：只改 `DEPLOY_HOST` 和 `deploy-ssh`，流水线不变。

"""真实轨迹回放集（第十二轮）：比 E 类单测更长的、贴近真实干活的轨迹样本。

每个场景：一条完整轨迹（多步、中间出错、重试、绕路）→ 提炼 → 换一个后续任务
取简报。断言两件事，这正是本系统的价值主张：
1) 当初的坑/策略出现在后续任务的简报里（经验真的被复用了）；
2) 简报体量远小于原始轨迹的渲染体量（经验把「干活的过程」压缩成了「干活的知识」）。
"""
import pytest

from app.auth import UserContext
from app.config import settings
from app.distill import InsightCandidate
from app.episodes import _render_episode, brief_for_task, run_episode_once, submit_episode
from app.recall import _est_tokens

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")


class OneShotDistiller:
    """与既有测试的 FakeDistiller 同构：distiller 是鸭子类型，只需 distill(text)。"""

    def __init__(self, cands):
        self._c = cands

    def distill(self, rendered: str):
        return list(self._c)


def _steps(*items):
    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"action": it})
        else:
            out.append({"action": it[0], "error": it[1]})
    return out


SCENARIOS = [
    {
        "id": "deploy-port",
        "title": "给内部工具部署一个新服务",
        "outcome": "partial",
        "steps": _steps(
            "拉取最新代码并安装依赖，一切正常",
            "写好 systemd 单元，ExecStart 指向应用入口",
            ("在 8080 启动服务", "OSError: [Errno 98] Address already in use"),
            "netstat 看了一眼没看明白是谁占的，又重启了一次还是失败",
            ("换到 8081 再启动", "OSError: [Errno 98] Address already in use"),
            "改用 ss -tlnp 全量列出监听端口，发现 8080/8081/3000 都被占",
            "从空闲段里挑了 18080，服务一次启动成功",
            "顺带查清占用 8080 的是上周遗留的测试实例，跟同事确认后停掉回收",
            "在部署脚本里加了启动前端口预检：先列占用再选空闲段，不再靠试错",
            "curl 健康检查通过，但反向代理还没配，外部暂时访问不到",
        ),
        "cands": [InsightCandidate(
            kind="pitfall", subject="部署端口选择",
            content="部署前先用 ss -tlnp 探测端口占用，8080/8081/3000 常被占用，"
                    "逐个试错浪费两轮重启",
            applies_when="部署或启动本地服务、需要绑定端口时")],
        "task": "再部署一个新服务到这台机器",
        "must": "ss -tlnp",
    },
    {
        "id": "migration-pg-only",
        "title": "给用户表加一列并上线",
        "outcome": "partial",
        "steps": _steps(
            "写迁移：新增 display_name 列（VARCHAR(20)）",
            "本地 SQLite 测试全绿，提交",
            ("CI 的真库阶段挂了", "value too long for type character varying(20)"),
            "查数据发现有用户的显示名是 26 个字符，本地测试数据都是短名没测出来",
            "把列改成 VARCHAR(64) 并补了超长样本的回归测试",
            "又在真库的影子 schema 上完整演练了一遍迁移和回滚，确认没有锁表风险",
            "应用层也补上长度校验，让超长输入在进库前就报清楚的错",
            "真库阶段通过，部署完成",
            "复盘：本地库和真库的类型严格程度不一样，字符串长度、大小写敏感都可能翻车",
        ),
        "cands": [InsightCandidate(
            kind="lesson", subject="数据库迁移验证",
            content="迁移只在本地 SQLite 上绿不算数：列长度、大小写、锁语义必须在真库"
                    "（Postgres）上验一遍，并用接近生产分布的数据",
            applies_when="写数据库迁移或改表结构时")],
        "task": "给订单表加一列备注，走数据库迁移上线",
        "must": "真库",
    },
    {
        "id": "git-push-ssh",
        "title": "把本地分支推到 GitHub",
        "outcome": "success",
        "steps": _steps(
            "git push 走 HTTPS，弹出用户名密码，失败",
            "改用应用集成推分支，返回 403 Resource not accessible by integration",
            "查权限页，来回确认了两次，集成就是没有 Contents 写权限",
            "换思路：把本机 SSH 公钥加到 GitHub 账号",
            ("ssh 直连 22 端口超时，这台机器出网只有代理 443 通",
             "ssh: connect to host github.com port 22: Connection timed out"),
            "改用 ssh.github.com 的 443 端口并让 ssh 经代理 CONNECT 过去",
            ("第一次推送仍然 Permission denied",
             "git@ssh.github.com: Permission denied (publickey)"),
            "中途试过把访问令牌拼进 remote 地址，安全上说不通，自己否掉了",
            "发现 ssh 按 passwd 家目录找钥匙，显式 -i 指定本机钥匙路径后推送成功",
            "把可用的推送命令固化成仓库脚本，之后每次完成都照着跑",
        ),
        "cands": [InsightCandidate(
            kind="pitfall", subject="Git 推送通道",
            content="本机 git push 走不通时别死磕应用授权：SSH 经代理打 ssh.github.com:443"
                    " 最快；Permission denied 先查钥匙路径，ssh 按 passwd 家目录找钥匙，"
                    "要显式 -i",
            applies_when="在本机向 GitHub 推送失败时")],
        "task": "把另一个仓库的分支推送到 GitHub",
        "must": "ssh.github.com",
    },
    {
        "id": "rate-limit-retry",
        "title": "批量调用外部 API 生成摘要",
        "outcome": "partial",
        "steps": _steps(
            "写了批处理脚本，200 条数据并发 20 路调用",
            ("跑到第 30 多条开始大面积报错", "HTTP 429 Too Many Requests"),
            "以为是临时抖动，原样重跑，又在差不多的位置挂掉",
            "降到并发 5 并加固定 sleep 1s，能跑但慢得离谱",
            "读响应头发现有 Retry-After，改成按它退避 + 抖动",
            "并发回升到 10，全程再没触发限流，总耗时反而比 sleep 版短",
            "把每条的成败单独记状态，失败的只补跑失败集，不再整批重来",
            "还踩了一个小坑：重试时把已成功的也算进进度，数字一度超过总数",
            "并发与退避参数抽成配置，下次换接口只改参数不改代码",
            "跑完对账：200 条全部有结果，失败补跑 11 条，最终零丢失",
            "断点续跑靠的是每条结果落盘后写一行进度标记",
        ),
        "cands": [InsightCandidate(
            kind="strategy", subject="API 限流应对",
            content="遇到 429 不要原样重跑：优先服从 Retry-After 做指数退避并加抖动，"
                    "并发宁可低开再上调；进度逐条落盘保证可断点续跑",
            applies_when="批量调用有速率限制的外部 API 时")],
        "task": "再批量调用一次这个 API 处理新数据",
        "must": "Retry-After",
    },
    {
        "id": "secret-in-logs",
        "title": "排查为什么 staging 环境连不上数据库",
        "outcome": "success",
        "steps": _steps(
            "应用日志只报连接超时，看不出细节",
            "把连接串完整打印到调试日志里确认参数",
            "发现主机名写错一位，改对后连通",
            "收尾时才意识到调试日志里有明文口令，而且日志已经同步到共享平台",
            "立刻轮换了数据库口令，并把那段时间的日志做了清理申请",
            "顺手排查了其它几个服务的日志，还有两处也在打印完整连接串",
            "其中一处是第三方库的调试开关打开时自带的，关掉并锁了配置",
            "加了统一的日志脱敏处理，密钥、口令、令牌字段自动打码",
            "把这次的检查项写成上线前清单：搜连接串、令牌、口令的打印点",
            "之后约定：连接串一律脱敏打印，只留主机和库名",
        ),
        "cands": [InsightCandidate(
            kind="pitfall", subject="日志脱敏",
            content="调试时打印配置必须先脱敏：连接串、令牌、口令只留非敏感段；"
                    "日志一旦同步到共享平台，明文秘密等于公开，必须轮换",
            applies_when="打印配置、连接串或请求头做调试时")],
        "task": "排查测试环境调用失败，准备打印请求细节看问题",
        "must": "脱敏",
    },
]


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_replay_trajectory_becomes_brief(db, sc):
    ep = submit_episode(db, U, source="agent", title=sc["title"],
                        outcome=sc["outcome"], steps=sc["steps"],
                        cost={"tokens": 2600, "retries": 2})
    assert run_episode_once(db, OneShotDistiller(sc["cands"]),
                            embedder=None) is not None
    brief = brief_for_task(db, U, sc["task"], 5)
    assert brief, f"{sc['id']}: 后续任务没拿到任何简报"
    joined = " ".join(i.content for i in brief)
    assert sc["must"] in joined, f"{sc['id']}: 关键经验未出现在简报中：{joined}"
    # 压缩比：简报体量 ≤ 原始轨迹渲染体量的 1/4——经验的价值就是把过程压成知识。
    # 本集轨迹是 10~13 步的中等任务；真实长轨迹（几十步起）步数只多不少，压缩比更高。
    brief_tokens = sum(_est_tokens(i.content) + _est_tokens(i.applies_when) for i in brief)
    raw_tokens = _est_tokens(_render_episode(ep))
    assert brief_tokens * 4 <= raw_tokens, (
        f"{sc['id']}: 简报 {brief_tokens} tokens vs 轨迹 {raw_tokens} tokens，压缩不足")

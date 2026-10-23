"""A/B 演示：同一个部署任务，有经验记忆 vs 没有，步数与成本差多少。

用法：python scripts/episode_demo.py   （临时 SQLite，不需要任何外部服务）

流程：Agent A 蒙头部署，连续撞上两个被占用的端口才成功 → 全过程提交为 episode →
复盘提炼出一条「避坑」经验 → Agent B 接到同类新任务，先 brief 查经验 →
先探测端口、一次成功。最后对比两边的步数、失败次数与 token 成本。

诚实说明：这里的 Agent 是脚本化的策略函数，提炼器是规则版实现——演示的是
「采集 → 提炼 → 情境召回 → 行为改变 → 成本下降」这条闭环的管线与记账方式，
不是某个真实 LLM 的能力测评。接入真实环境时，把 RuleDistiller 换成
app.distill.LlmDistiller、把 run_deploy 的策略换成真实工具调用即可，管线不变。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import UserContext
from app.db import Base
from app.distill import InsightCandidate
from app.episodes import brief_for_task, run_episode_once, submit_episode
from app.models import MemoryInsight

PORTS = [8080, 8081, 3000]
OCCUPIED = {8080, 8081}
COST_FAIL = 150    # 一次失败尝试：报错输出 + 重新推理的 token
COST_OK = 100      # 一次成功启动
COST_PROBE = 30    # 一次端口探测


class RuleDistiller:
    """规则版提炼器：轨迹里出现端口占用报错，就提炼一条避坑经验。"""

    def distill(self, text: str) -> list[InsightCandidate]:
        if "EADDRINUSE" in text:
            return [InsightCandidate(
                kind="pitfall", subject="部署端口选择",
                content="部署前先探测端口占用：8080、8081 常被占用，先探测再选空闲端口（如 3000），"
                        "不要按默认顺序逐个试错。",
                applies_when="部署或启动本地服务、需要绑定端口时")]
        return []


def run_deploy(insights) -> tuple[list[dict], int, int]:
    """脚本化 Agent：没有经验就按默认顺序逐个试端口；简报里提到探测就先探测再直达。"""
    steps: list[dict] = []
    tokens = failures = 0
    guided = any("探测" in i.content for i in insights)
    order = PORTS
    if guided:
        steps.append({"action": "先探测端口占用（ss -tlnp）",
                      "observation": "8080、8081 已被占用，3000 空闲"})
        tokens += COST_PROBE
        order = [3000]
    for port in order:
        if port in OCCUPIED:
            steps.append({"action": f"在 {port} 端口启动服务",
                          "error": "OSError: [Errno 98] EADDRINUSE 地址已被占用"})
            tokens += COST_FAIL
            failures += 1
        else:
            steps.append({"action": f"在 {port} 端口启动服务",
                          "observation": "启动成功，健康检查通过"})
            tokens += COST_OK
            break
    return steps, tokens, failures


def main() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    u = UserContext(app_id="demo", user_id="demo-user")
    distiller = RuleDistiller()

    print("=" * 64)
    print("第一次：Agent A 部署服务（经验库为空）")
    steps_a, tokens_a, fail_a = run_deploy([])
    for s in steps_a:
        print(f"  - {s['action']}" + (f" → 出错：{s['error']}" if "error" in s else " → 成功"))
    ep_a = submit_episode(db, u, source="demo", title="把服务 A 部署到本机并启动",
                          outcome="success", steps=steps_a,
                          cost={"tokens": tokens_a, "failures": fail_a})
    run_episode_once(db, distiller)
    insights = list(db.scalars(select(MemoryInsight).where(MemoryInsight.valid_to.is_(None))))
    print(f"  过程已提交（episode={ep_a.id[:8]}…），复盘提炼出 {len(insights)} 条经验：")
    for i in insights:
        print(f"  [{i.kind}] {i.subject}：{i.content}（适用于：{i.applies_when}）")

    print("=" * 64)
    print("第二次：Agent B 部署另一个服务（先查经验简报）")
    brief = brief_for_task(db, u, "部署服务 B，需要绑定端口启动", 5)
    print(f"  brief 命中 {len(brief)} 条经验" + (f"，最高分 {brief[0].score}" if brief else ""))
    steps_b, tokens_b, fail_b = run_deploy(brief)
    for s in steps_b:
        print(f"  - {s['action']}" + (f" → {s.get('observation', '')}" if "error" not in s else f" → 出错：{s['error']}"))
    ep_b = submit_episode(db, u, source="demo", title="把服务 B 部署到本机并启动",
                          outcome="success", steps=steps_b,
                          cost={"tokens": tokens_b, "failures": fail_b})
    run_episode_once(db, distiller)
    total = len(list(db.scalars(select(MemoryInsight).where(MemoryInsight.valid_to.is_(None)))))
    print(f"  B 的干净过程也已提交（episode={ep_b.id[:8]}…）；无新教训可提炼，经验库仍 {total} 条，不灌水。")

    print("=" * 64)
    print(f"{'':10s}{'步数':>6s}{'失败':>6s}{'token 成本':>12s}")
    print(f"{'无经验':10s}{len(steps_a):>6d}{fail_a:>6d}{tokens_a:>12d}")
    print(f"{'有经验':10s}{len(steps_b):>6d}{fail_b:>6d}{tokens_b:>12d}")
    saved = tokens_a - tokens_b
    print(f"结论：步数 {len(steps_a)} → {len(steps_b)}，失败 {fail_a} → {fail_b}，"
          f"成本省下 {saved} token（{saved / tokens_a:.0%}）。"
          "这就是这套系统要考的：不是记住事实，是把过程变成下一次的便宜。")


if __name__ == "__main__":
    main()

"""第九轮（收尾三件套）回归：提炼输入限长、导出补经验与轨迹。

PG 真库 CI 一件不在本文件：Jenkinsfile 早已有 'Test on Postgres' 阶段，
本轮补的是本地等价脚本 scripts/test-pg.sh（docker 不可用时无法在单测里验证）。"""
from app import models as m
from app.episodes import MAX_RENDER_CHARS, _render_episode
from tests.conftest import consent, uid

APP = "app"


def _ep(steps, title="任务", outcome="success"):
    return m.TaskEpisode(app_id=APP, user_id="alice", title=title, outcome=outcome,
                         steps=steps, cost={})


def test_render_short_episode_unchanged():
    """短轨迹逐字保持旧格式（限长只许动长轨迹）。"""
    ep = _ep([{"action": "打开文件"}, {"action": "写补丁", "observation": "完成", "error": "小警告"}],
             title="修 bug", outcome="partial")
    ep.cost = {"tokens": 10}
    out = _render_episode(ep)
    assert out == ("任务：修 bug\n结果：partial\n成本：{\"tokens\": 10}\n过程：\n"
                   "1. 打开文件\n2. 写补丁 → 完成（出错：小警告）")


def _fat_steps(n=200, error_at=()):
    steps = []
    for i in range(1, n + 1):
        st = {"action": f"步骤{i}动作" + "动" * 490, "observation": "观" * 2000}
        if i in error_at:
            st["error"] = f"关键错误{i}号" + "错" * 50
        steps.append(st)
    steps[0]["action"] = "开头第一步" + "动" * 490
    steps[-1]["action"] = "收尾最后一步" + "动" * 488
    return steps


def test_render_cap_keeps_errors_head_tail():
    """最大合法轨迹（200 步 × 约 4.5K 字符/步）渲染必须 ≤ 上限，
    且出错步（含行尾的错误段）、首步、尾步都不许丢，省略要有标记。"""
    out = _render_episode(_ep(_fat_steps(error_at=(50, 120, 199))))
    assert len(out) <= MAX_RENDER_CHARS
    assert "开头第一步" in out and "收尾最后一步" in out
    for i in (50, 120, 199):
        assert f"关键错误{i}号" in out, f"出错步 {i} 的错误段被截断切掉"
    assert "省略第" in out


def test_render_cap_when_every_step_errors():
    """极端情况：200 步全出错、必留集本身就超限——仍须 ≤ 上限且首尾锚点在。"""
    out = _render_episode(_ep(_fat_steps(error_at=tuple(range(1, 201)))))
    assert len(out) <= MAX_RENDER_CHARS
    assert "开头第一步" in out and "收尾最后一步" in out


def test_render_cap_mid_episode_still_distills():
    """限长后的渲染仍可提炼（形状不变：标题/结果行在、步骤行在）。"""
    out = _render_episode(_ep(_fat_steps()))
    assert out.startswith("任务：任务\n结果：success\n")
    assert "\n1. " in out


def test_export_includes_insights_and_episodes(client, db):
    """导出补全：经验（含历史版本）与轨迹都在，且不串到别人名下。"""
    consent(client, "alice", "bob")
    db.add_all([
        m.MemoryInsight(user_id="alice", kind="lesson", subject="部署",
                        content="先看日志再重启", applies_when="服务异常时"),
        m.MemoryInsight(user_id="bob", kind="lesson", subject="部署",
                        content="别人的经验", applies_when=None),
        m.TaskEpisode(user_id="alice", source="codex", title="部署服务",
                      outcome="partial", steps=[{"action": "看日志"}], cost={"tokens": 42},
                      distill_status="done"),
        m.TaskEpisode(user_id="bob", source="codex", title="别人的轨迹",
                      outcome="success", steps=[], cost={}),
    ])
    db.commit()
    body = client.get("/v1/me/memories/export", params=uid("alice")).json()
    assert [i["subject"] for i in body["insights"]] == ["部署"]
    assert body["insights"][0]["content"] == "先看日志再重启"
    assert body["insights"][0]["applies_when"] == "服务异常时"
    assert [e["title"] for e in body["episodes"]] == ["部署服务"]
    assert body["episodes"][0]["steps"] == [{"action": "看日志"}]
    assert body["episodes"][0]["cost"] == {"tokens": 42}
    assert body["episodes"][0]["distill_status"] == "done"

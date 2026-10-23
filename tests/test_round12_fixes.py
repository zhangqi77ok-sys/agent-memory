"""第十二轮回归：经验侧 metrics、JSONL 导入器解析。

延迟基准是脚本（scripts/bench_brief.py），数字随机器变，不进单测；
轨迹回放集在 tests/test_replay_scenarios.py。"""
import importlib.util
import pathlib

from sqlalchemy import select

from app import models as m
from app.config import settings
from app.db import engine
from app.metrics import metrics_collector

APP = settings.app_id


def _load_importer():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "import_episodes.py"
    spec = importlib.util.spec_from_file_location("import_episodes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_experience_metrics_counts(db):
    db.add_all([
        m.TaskEpisode(app_id=APP, user_id="alice", title="待提炼", distill_status="pending"),
        m.TaskEpisode(app_id=APP, user_id="alice", title="已提炼", distill_status="done"),
        m.TaskEpisode(app_id=APP, user_id="alice", title="失败的", distill_status="failed"),
        m.MemoryInsight(app_id=APP, user_id="alice", kind="lesson", subject="主题",
                        content="内容", applies_when=""),
        m.AuditLog(app_id=APP, user_id="alice", action="brief", fact_ids=["x1"]),
        m.AuditLog(app_id=APP, user_id="alice", action="brief", fact_ids=[]),
        m.AuditLog(app_id=APP, user_id="alice", action="recall", fact_ids=["y1"]),
    ])
    db.commit()
    text = metrics_collector.generate_metrics_text(engine)
    assert 'agent_memory_episodes{status="pending"} 1' in text
    assert 'agent_memory_episodes{status="done"} 1' in text
    assert "agent_memory_episode_backlog 1" in text
    assert "agent_memory_insights_active 1" in text
    assert "agent_memory_brief_total 2" in text       # recall 的审计不算进来
    assert "agent_memory_brief_hits 1" in text        # 只有 fact_ids 非空的那次是命中


def test_importer_parse_and_payload():
    imp = _load_importer()
    text = "\n".join([
        '{"title": "部署服务", "outcome": "partial", "project": "repo-a", '
        '"steps": [{"action": "启动", "error": "EADDRINUSE"}], "idempotency_key": "k1"}',
        '{"title": "缺 outcome 时缺省 success"}',
        '{"outcome": "success"}',                       # 缺 title
        '{"title": "坏结果", "outcome": "unknown"}',    # outcome 非法
        "this is not json",
        '{"title": "步骤坏", "steps": "not-a-list"}',
        "",
    ])
    valid, errors = imp.parse_lines(text)
    assert len(valid) == 2 and len(errors) == 4
    first = valid[0]
    assert first["project"] == "repo-a" and first["idempotency_key"] == "k1"
    body = imp.build_payload(first, "alice")
    assert body["user_id"] == "alice" and body["project"] == "repo-a"
    # 无 project 的记录不带这个键（服务端缺省 None=通用）
    assert "project" not in imp.build_payload(valid[1], "alice")


def test_importer_dedupes_via_idempotency(client, db):
    """导入器与服务端合起来：同一 idempotency_key 导入两次只落一条（走真实 REST）。"""
    from tests.conftest import consent
    consent(client, "alice")
    imp = _load_importer()
    rec = {"title": "冷启动轨迹", "outcome": "success", "source": "import",
           "project": None, "steps": [{"action": "做事"}], "cost": {},
           "idempotency_key": "cold-1"}
    # 导入器经 REST 提交并带 Idempotency-Key 头——这里验证它依赖的服务端语义
    body = imp.build_payload(rec, "alice")
    r1 = client.post("/v1/episodes", json=body,
                     headers={"Idempotency-Key": "cold-1"})
    r2 = client.post("/v1/episodes", json=body,
                     headers={"Idempotency-Key": "cold-1"})
    assert r1.json()["episode_id"] == r2.json()["episode_id"]
    assert r2.json()["duplicate"] is True
    assert len(list(db.scalars(select(m.TaskEpisode)))) == 1

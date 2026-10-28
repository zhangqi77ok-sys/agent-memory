import os
import pytest
"""第 33 轮「类比召回」：动作序列即结构，表面换词不换类、顺序敏感、
领域名词剥离。探针口径与 27/31 轮同源（确定性、无嵌入）。

探针集（sim 分布见 docs/eval.md）：
- P 组（结构同、表面异，正例）：P1 部署失败切换镜像重建验证、
  P2 连接池耗尽隔离重启验证、P3 端口占用探测换段启动、
  P4 坏发布回滚验证、P5 缓存过期清理重建验证、P6 抖动测试隔离重试验证。
- N 组（表面同、结构异，反例）：N1 同工具检查清单、N2 计费说明、
  N3 升级指南、N4 通用注意事项。
- A 组（类比独占）：kw < MIN_SCORE 且无向量，行仅由类比臂进入简报。
"""
import time

from sqlalchemy import select

import app.analogy as analogy_mod
from app import models as m
from app.analogy import (
    ANALOGY_CANDIDATE_CAP,
    ANALOGY_MIN,
    analogy_sim,
    extract_moves,
)
from app.auth import UserContext
from app.config import settings
from app.episodes import brief_for_task
from app.recall import MIN_SCORE, _QueryPlan
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")
OTHER = UserContext(app_id=settings.app_id, user_id="bob")

Q_DEPLOY = "GitHub Actions 上线失败，切换 runner 镜像后重跑，冒烟通过"
Q_ROLLBACK = "新版本上线后异常，立即回滚到上一版，验证恢复"

P_CASES = [
    ("流水线发版异常处置",
     "Jenkins 发版异常：先看日志定位原因，切换 agent 镜像后重新构建，回归验证通过",
     "CI 流水线失败时"),
    ("连接池打满处置",
     "Redis 连接池打满：先限制连接数隔离异常调用方，重启实例后确认恢复",
     "缓存或数据库连接池耗尽时"),
    ("端口占用处置", "部署前先探测端口占用，换段后启动服务", "服务启动报地址占用时"),
    ("发版故障回退", "发版后故障：回滚上一版并回归确认", "发布出问题时"),
    ("缓存陈旧处置", "缓存陈旧：清空缓存重新部署后确认", "页面内容不更新时"),
    ("抖动测试处置", "抖动测试处理：先隔离不稳定用例，重跑三次后回归", "测试偶发失败时"),
]
P_QUERIES = [
    Q_DEPLOY,
    "数据库连接池耗尽，隔离超限连接后重启服务，验证恢复",
    "服务启动报地址占用，批量探测端口段后换个空闲端口启动",
    Q_ROLLBACK,
    "页面缓存过期不更新，清理缓存后重新构建，验证生效",
    "测试用例偶发失败，隔离抖动用例后重跑，确认通过",
]
N_CASES = [
    ("部署前检查清单",
     "GitHub Actions 部署前检查：确认 runner 镜像版本与端口占用，检查部署配置",
     "部署前"),
    ("计费说明", "GitHub Actions 计费说明：runner 按分钟计费，镜像拉取走缓存", "看账单时"),
    ("升级指南", "GitHub Actions 升级指南：升级 runner 版本到最新", "升级时"),
    ("部署注意事项", "部署注意事项：提前通知相关方，选业务低峰期操作", "部署前"),
]
# A 组：kw 严格 < MIN_SCORE（测试内直接断言），无向量，行只能由类比臂捞起。
A_CASES = [
    (Q_DEPLOY, "流水线发版异常处置",
     "先看日志定位原因，更换 agent 底座，重新构建并回归检查", "持续集成出故障时"),
    (Q_ROLLBACK, "发版故障回退", "撤销发布并回归确认", "发布出问题时"),
]


def _insight(db, subject, content, applies="", project=None, user=U,
             episode_id=None):
    row = m.MemoryInsight(app_id=user.app_id, user_id=user.user_id, kind="strategy",
                          subject=subject, content=content, applies_when=applies,
                          project=project, source_episode_id=episode_id)
    db.add(row)
    db.commit()
    return row


def _moves_of(db, insight_id):
    return [r[0] for r in db.execute(
        select(m.MemoryInsightMove.move).where(
            m.MemoryInsightMove.insight_id == insight_id
        ).order_by(m.MemoryInsightMove.position))]


def _winners(db, task, project=None):
    # brief_for_task 本身不查 consent（test_episodes.py 直接调用先例）；
    # consent 门禁在 API/MCP 写入口与巡检，不在简报读路径。
    return brief_for_task(db, U, task, embedder=None, project=project)


def test_extract_moves_ordered_longest_first_dedup():
    # 最长优先：“检查通过”压过“检查”；“测试”不被拆成“试”；连续重复去重。
    assert extract_moves("检查通过后再次检查") == ["verify", "diagnose"]
    assert extract_moves("测试用例测试") == ["verify"]
    assert extract_moves("查了又查日志") == ["diagnose"]
    # 领域名词零动作：工具名、业务词永远不进入签名。
    assert extract_moves("Jenkins GitHub Actions Docker 端口 8080") == []
    assert extract_moves("") == []


def test_lcs_properties():
    assert analogy_sim(["a", "b"], ["a", "b"]) == (1.0, 2)
    assert analogy_sim(["a", "b"], ["b", "a"])[0] < 1.0  # 顺序敏感
    assert analogy_sim([], ["a"]) == (0.0, 0)
    assert analogy_sim(["a"], ["a"]) == (1.0, 1)


def test_positive_probes_admitted_by_analogy(db):
    for (subj, content, applies), query in zip(P_CASES, P_QUERIES, strict=True):
        sim, lcs = analogy_sim(extract_moves(query),
                               extract_moves(f"{subj} {content} {applies}"))
        assert sim >= ANALOGY_MIN and lcs >= 2, (subj, sim, lcs)


def test_negative_probes_rejected_by_analogy(db):
    for subj, content, applies in N_CASES:
        sim, lcs = analogy_sim(extract_moves(Q_DEPLOY),
                               extract_moves(f"{subj} {content} {applies}"))
        assert not (sim >= ANALOGY_MIN and lcs >= 2), (subj, sim, lcs)


def test_positive_rows_recalled_via_brief(db):
    rows = [_insight(db, s, c, a) for s, c, a in P_CASES]
    for query, row in zip(P_QUERIES, rows, strict=True):
        got = _winners(db, query)
        assert row.id in {w.insight_id for w in got}, (query, row.subject)


def test_negative_rows_not_lifted_by_analogy(db):
    rows = [_insight(db, s, c, a) for s, c, a in N_CASES]
    got = _winners(db, Q_DEPLOY)
    by_id = {w.insight_id: w for w in got}
    for row in rows:
        # N1–N3 可能由关键词臂进入，但绝不能是类比臂抬进来的；
        # N4 表面重叠为零，两臂都不该捞。
        if row.id in by_id:
            assert by_id[row.id].reason != "analogical", row.subject


def test_analogy_only_rows_enter_with_analogical_reason(db):
    for query, subj, content, applies in A_CASES:
        row = _insight(db, subj, content, applies)
        assert _QueryPlan(query).kw_score(
            f"{subj} {content} {applies}") < MIN_SCORE
        got = _winners(db, query)
        hit = [w for w in got if w.insight_id == row.id]
        assert len(hit) == 1 and hit[0].reason == "analogical", (subj, got)


def test_keyword_admitted_row_keeps_original_reason(db):
    # 高关键词重叠行：关键词臂已准入，类比臂不得改写 reason（补充臂不改行）。
    row = _insight(db, "部署前检查清单",
                   "GitHub Actions 部署前检查：确认 runner 镜像版本与端口占用，"
                   "检查部署配置", "部署前")
    got = _winners(db, Q_DEPLOY)
    hit = [w for w in got if w.insight_id == row.id]
    assert hit and hit[0].reason in ("keyword", "hybrid")


def test_analogy_respects_user_boundary(db):
    other = _insight(db, "流水线发版异常处置",
                     "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                     "持续集成出故障时", user=OTHER)
    got = _winners(db, Q_DEPLOY)
    assert other.id not in {w.insight_id for w in got}


def test_analogy_respects_project_discount(db):
    same = _insight(db, "流水线发版异常处置",
                    "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                    "持续集成出故障时", project="proj-a")
    other = _insight(db, "流水线发版异常处置",
                     "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                     "持续集成出故障时", project="proj-b")
    got = _winners(db, Q_DEPLOY, project="proj-a")
    ids = [w.insight_id for w in got]
    assert same.id in ids and other.id in ids  # 跨项目是降权不是禁入
    assert ids.index(same.id) < ids.index(other.id)


def test_forgotten_insight_not_recalled(db, client):
    from tests.conftest import uid

    row = _insight(db, "流水线发版异常处置",
                   "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                   "持续集成出故障时")
    row_id = row.id  # 删后 ORM 对象过期，先取纯 id（fix-8 测试同款教训）。
    consent(client, "alice")
    assert client.delete(f"/v1/me/memories/{row_id}",
                         params=uid("alice")).status_code == 204
    db.expire_all()
    got = _winners(db, Q_DEPLOY)
    assert row_id not in {w.insight_id for w in got}


def test_move_table_sync_on_write_update_delete(db):
    row = _insight(db, "流水线发版异常处置",
                   "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                   "持续集成出故障时")
    assert _moves_of(db, row.id) == extract_moves(
        f"{row.subject} {row.content} {row.applies_when}")
    row.content = "部署注意事项：提前通知相关方"
    db.commit()
    assert _moves_of(db, row.id) == extract_moves(
        f"{row.subject} {row.content} {row.applies_when}")
    db.delete(row)
    db.commit()
    assert _moves_of(db, row.id) == []


def test_episode_skeleton_actions_extend_signature(db):
    ep = m.TaskEpisode(app_id=U.app_id, user_id=U.user_id, title="修流水线",
                       steps=[{"action": "排查日志"}, {"action": "切换镜像"},
                              {"action": "重新构建"}, {"action": "冒烟验证"}])
    db.add(ep)
    db.commit()
    row = _insight(db, "流水线处置", "通用注意事项", "部署前", episode_id=ep.id)
    moves = _moves_of(db, row.id)
    # 文本侧：applies_when“部署前”→ deploy；轨迹侧：“重新构建”→ retry + build。
    assert moves == ["deploy", "diagnose", "switch", "retry", "build", "verify"], moves


def test_candidate_count_bounded(db, monkeypatch):
    target = _insight(db, "流水线发版异常处置",
                      "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                      "持续集成出故障时")
    for i in range(2000):
        _insight(db, f"填充{i}", "上线后验证清单：冒烟验证通过才算完成", "部署后")
    calls = {"n": 0}
    orig = analogy_mod.lcs_len

    def counting(a, b):
        calls["n"] += 1
        return orig(a, b)

    monkeypatch.setattr(analogy_mod, "lcs_len", counting)
    got = _winners(db, Q_DEPLOY)
    assert target.id in {w.insight_id for w in got}
    # 2000 填充行共享 verify 动作：精确 LCS 只算到候选上限，不扫全量。
    assert calls["n"] <= ANALOGY_CANDIDATE_CAP, calls


def test_empty_query_moves_no_analogy(db):
    _insight(db, "流水线发版异常处置",
             "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
             "持续集成出故障时")
    assert brief_for_task(db, U, "！！！", embedder=None) == []


def test_rebuild_moves_script(db):
    from scripts.rebuild_moves import rebuild

    row = _insight(db, "流水线发版异常处置",
                   "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
                   "持续集成出故障时")
    assert rebuild(db, check_only=True)["drifted"] == 0
    db.execute(m.MemoryInsightMove.__table__.delete().where(
        m.MemoryInsightMove.insight_id == row.id))
    db.commit()
    assert rebuild(db, check_only=True)["drifted"] == 1
    assert rebuild(db, check_only=False)["drifted"] == 1
    assert rebuild(db, check_only=True)["drifted"] == 0


@pytest.mark.skipif(bool(os.environ.get("BUILD_NUMBER") or os.environ.get("CI")), reason="p95 latency budget is for dedicated hosts; Jenkins shared CPU is too noisy")
def test_analogy_latency_budget(db):
    for i in range(2000):
        _insight(db, f"填充{i}", "上线后验证清单：冒烟验证通过才算完成", "部署后")
    _insight(db, "流水线发版异常处置",
             "先看日志定位原因，更换 agent 底座，重新构建并回归检查",
             "持续集成出故障时")
    from app.analogy import analogy_candidates
    from app.analogy import extract_moves as em
    from app.models import MemoryInsight as _MI

    cols = [_MI.id, _MI.kind, _MI.subject, _MI.content, _MI.applies_when,
            _MI.confidence, _MI.project, _MI.pinned, _MI.valid_from,
            _MI.last_used_at, _MI.source_episode_id, _MI.verified, _MI.agent,
            _MI.evidence_chained]
    scope = (_MI.app_id == U.app_id, _MI.user_id == U.user_id,
             _MI.valid_to.is_(None))
    qm = em(Q_DEPLOY)
    ts = []
    for _ in range(100):
        t0 = time.perf_counter()
        analogy_candidates(db, cols, scope, qm)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    p95 = ts[int(0.95 * len(ts)) - 1]
    assert p95 <= 50, ts[-5:]
    ts = []
    for _ in range(50):
        t0 = time.perf_counter()
        brief_for_task(db, U, Q_DEPLOY, embedder=None)
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    p95 = ts[int(0.95 * len(ts)) - 1]
    assert p95 <= 200, ts[-5:]

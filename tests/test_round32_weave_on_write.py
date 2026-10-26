"""第 32 轮「写时织网」：新行落地即连边，且成本有界、失败不拖垮写入。

背景：15 轮已把织网接在提炼/抽取两处写路径上，但候选是全量扫描
（卡的只是建边数不是扫描成本），且无分桶过滤、无织内墓碑门、
失败可向上传播拖垮整批；Markdown 导回与纠正端点则全程不织网。
本轮：候选改成倒排锚点查询（与 14 轮倒排、10 轮巡检锚点同哲学），
同桶过滤、墓碑门、SAVEPOINT 失败隔离，并补齐两处接线。
"""
import time

from sqlalchemy import select

import app.links as links_mod
from app import models as m
from app.auth import UserContext
from app.config import settings
from app.db import WorkerSession
from app.extract import Candidate
from app.links import (
    WEAVE_ANCHOR_PEER_BUDGET,
    _weave_anchor_peers,
    weave_links,
    weave_on_write,
)
from app.markdown_sync import apply_markdown_import
from app.tombstone import record_tombstone
from app.worker import run_once
from tests.conftest import consent

U = UserContext(app_id=settings.app_id, user_id="alice")


def _insight(db, subject, content, applies="", vec=None, project=None, user=U):
    row = m.MemoryInsight(app_id=user.app_id, user_id=user.user_id, kind="strategy",
                          subject=subject, content=content, applies_when=applies,
                          project=project,
                          embedding=list(vec) if vec else None,
                          embed_model="fake-embedder" if vec else None)
    db.add(row)
    db.commit()
    return row


def _fact(db, subject, content, user=U):
    row = m.MemoryFact(app_id=user.app_id, user_id=user.user_id,
                       subject=subject, content=content)
    db.add(row)
    db.commit()
    return row


def _active_links(db, user=U):
    return list(db.scalars(select(m.MemoryLink).where(
        m.MemoryLink.user_id == user.user_id, m.MemoryLink.valid_to.is_(None))))


def test_write_weaves_same_subject_without_patrol(db):
    """新行落地即与同主题旧行建边，不等巡检。"""
    a = _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    assert weave_on_write(db, U.app_id, U.user_id, b) == 1
    db.commit()
    links = _active_links(db)
    assert len(links) == 1
    assert links[0].relation == "related" and links[0].origin == "rule"
    assert {links[0].src_id, links[0].dst_id} == {a.id, b.id}


def test_anchor_candidates_bounded(db, monkeypatch):
    """候选有界：300 行 peer 只有 2 行相关时，余弦计算次数远小于全量对数。"""
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    for i in range(300):
        _insight(db, f"peer{i}subject", f"peer{i}content")
    calls = {"n": 0}
    orig = links_mod._pair_cosine_rows

    def counting(a, c):
        calls["n"] += 1
        return orig(a, c)

    monkeypatch.setattr(links_mod, "_pair_cosine_rows", counting)
    assert weave_on_write(db, U.app_id, U.user_id, b) == 1
    db.commit()
    # 旧全量扫描要对 300 个 peer 逐个算余弦；锚点只命中同主题 1 行 + 分类复算。
    assert calls["n"] <= 20, calls
    assert len(_active_links(db)) == 1


def test_anchor_peer_budget_caps_giant_bucket(db):
    """病态巨桶：300 行共享同一 token 时，锚点 peer 数仍被预算卡住。"""
    for i in range(300):
        _insight(db, f"peer{i}subject", f"公共做法内容 peer{i}")
    new = _insight(db, "全新主题", "公共做法内容全新")
    peers = _weave_anchor_peers(db, m.MemoryInsight, m.MemoryInsightToken,
                                "insight_id", U.app_id, U.user_id, new,
                                {"公共", "做法", "内容", "全新"})
    assert len(peers) <= WEAVE_ANCHOR_PEER_BUDGET


def test_cross_project_no_edge_at_write_time(db):
    """写时分桶：同主题、不同 project 的经验行不连边；同 project 才连。"""
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务", project="p1")
    other = _insight(db, "部署端口", "本地调试时优先选 3000 段端口", project="p2")
    assert weave_on_write(db, U.app_id, U.user_id, other) == 0
    same = _insight(db, "部署端口", "灰度发布前先探测端口占用", project="p1")
    assert weave_on_write(db, U.app_id, U.user_id, same) >= 1
    db.commit()
    assert len(_active_links(db)) == 1


def test_tombstoned_peer_no_edge(db):
    """墓碑门：peer 内容已遗忘（行关闭 + 墓碑记录）后新行不与其连边；
    即使该内容因别的路径仍有现行行，织时也跳过它。"""
    old = _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    old.valid_to = m._now()
    record_tombstone(db, U.app_id, U.user_id, "insight",
                     old.subject, old.content)
    # 直接入库绕过写门、内容与墓碑相同的现行行：织时必须跳过。
    ghost = _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    assert ghost.valid_to is None
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    made = weave_on_write(db, U.app_id, U.user_id, b)
    db.commit()
    assert made == 0
    assert _active_links(db) == []


def test_tombstoned_new_row_weaves_nothing(db):
    """新行自身内容已墓碑时，织直接返回 0。"""
    record_tombstone(db, U.app_id, U.user_id, "insight",
                     "部署端口", "部署前先探测端口占用再启动服务")
    _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    ghost = _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    assert weave_on_write(db, U.app_id, U.user_id, ghost) == 0
    db.commit()
    assert _active_links(db) == []


def test_weave_anchor_failure_does_not_break_write(db, monkeypatch):
    """织失败隔离：锚点查询抛错时织返回 0，调用方会话照常可用、写入照常提交。"""
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")

    def boom(*a, **k):
        raise RuntimeError("anchor boom")

    monkeypatch.setattr(links_mod, "_weave_anchor_peers", boom)
    assert weave_on_write(db, U.app_id, U.user_id, b) == 0
    # 会话未被毒化：继续写、继续提交都正常。
    c = _insight(db, "部署端口", "灰度前先探测端口占用")
    assert c.id is not None
    assert db.get(m.MemoryInsight, c.id) is not None


def test_write_job_survives_failing_weave(client, db):
    """端到端：织网锚点失败时，写入任务照常 done、事实行照常落地。"""
    consent(client, "alice")
    r = client.post("/v1/memories", headers={"Idempotency-Key": "k-w32"},
                    json={"user_id": "alice", "session_id": "s1",
                          "messages": [{"msg_id": "m1", "role": "user",
                                        "content": "我喜欢用 Python"}]})
    assert r.status_code == 202

    def extractor(text):
        return [Candidate("fact", "编程", "喜欢用 Python", "ADD")]

    import app.links as lm
    orig = lm._weave_anchor_peers

    def boom(*a, **k):
        raise RuntimeError("anchor boom")

    lm._weave_anchor_peers = boom
    try:
        with WorkerSession() as wdb:
            job = run_once(wdb, extractor)
    finally:
        lm._weave_anchor_peers = orig
    assert job is not None and job.status == "done"
    db.expire_all()
    fact = db.query(m.MemoryFact).filter_by(user_id="alice").one()
    assert fact.content == "喜欢用 Python" and fact.valid_to is None


def test_patrol_after_write_weave_is_idempotent(db):
    """巡检幂等：先写时织、再跑巡检，边数不变、无异常。"""
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    b = _insight(db, "部署端口", "本地调试时优先选 3000 段端口避开常用占用")
    assert weave_on_write(db, U.app_id, U.user_id, b) == 1
    db.commit()
    before = len(_active_links(db))
    weave_links(db)
    assert len(_active_links(db)) == before == 1


def test_markdown_import_weaves_new_rows(db):
    """接线：Markdown 导回的新事实/新经验行在 commit 后即与同主题旧行连边。"""
    _fact(db, "部署", "先探测端口再部署")
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    files = {
        "facts/x.md": "---\ntype: facts\nsubject: 部署\n---\n\n# 事实 · 部署\n\n- 部署后检查端口监听状态\n",
        "insights/y.md": ("---\ntype: insights\nsubject: 部署端口\n---\n\n"
                         "<!-- new kind:lesson -->\n本地调试优先选 3000 段端口\n"),
    }
    report = apply_markdown_import(db, U, files)
    assert report["created"] == 2
    links = _active_links(db)
    assert len(links) == 2, links


def test_correct_weaves_new_fact_version(client, db):
    """接线：纠正产生的新事实版本行在 commit 后即与同主题旧行连边。"""
    consent(client, "alice")
    old = _fact(db, "部署", "先探测端口再部署")
    peer = _fact(db, "部署", "部署前检查端口占用情况")
    # 先让 peer 与旧行织上边（旧行随即被纠正关闭，边迁到新版本行是另一机制；
    # 这里只断言纠正的新行自身也被织上边）。
    r = client.patch(f"/v1/me/memories/{old.id}",
                     json={"user_id": "alice", "content": "部署后检查端口监听状态"})
    assert r.status_code == 200, r.text
    db.expire_all()
    new = db.query(m.MemoryFact).filter_by(user_id="alice", version=2).one()
    assert new.valid_to is None
    links = _active_links(db)
    assert any({lk.src_id, lk.dst_id} == {new.id, peer.id} for lk in links), links


def test_write_latency_budget(db):
    """写延迟预算：N=2000 peer、SQLite、无 LLM 判定器，织的 p95 < 200ms。
    实测 p50/p95 写进报告；断言用宽松预算防环境抖动。"""
    for i in range(2000):
        _insight(db, f"peer{i}subject", f"公共做法内容 peer{i}")
    _insight(db, "部署端口", "部署前先探测端口占用再启动服务")
    # b 的内容含“公共做法内容”token，与 2000 个 peer 全共享锚点，测的是 200 上限路径。
    b = _insight(db, "部署端口", "本地调试优先选 3000 段端口，公共做法内容先探测")
    times = []
    for _ in range(100):
        t0 = time.perf_counter()
        weave_on_write(db, U.app_id, U.user_id, b)
        times.append(time.perf_counter() - t0)
    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95) - 1]
    print(f"\nround32 latency: p50={p50 * 1000:.1f}ms p95={p95 * 1000:.1f}ms")
    assert p95 < 0.200, (p50, p95)

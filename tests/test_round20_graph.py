"""第二十轮：织网度量 + 边学习（第一阶段：纯计算，不接线）。

图结构（alice 用户）：
    A —— B
    |  /
    C —— D —— E        F ×——× A（矛盾边，不进合作图）   G（孤儿）
    三角 A/B/C 无桥；C-D、D-E 是桥；合作社群 {A,B,C,D,E}、{F}、{G}。
"""

from app import models as m
from app.config import settings
from app.edge_learning import (
    find_spread_edge,
    strength_after_idle,
    strength_on_applied,
    strength_on_rejected,
)
from app.graph_stats import compute_graph_stats

APP = settings.app_id


def _insight(db, subject):
    row = m.MemoryInsight(app_id=APP, user_id="alice", kind="strategy",
                          subject=subject, content=f"关于{subject}的经验")
    db.add(row)
    db.flush()
    return row


def _link(db, a, b, relation, strength=0.9):
    db.add(m.MemoryLink(app_id=APP, user_id="alice", src_kind="insight",
                        src_id=a.id, dst_kind="insight", dst_id=b.id,
                        relation=relation, strength=strength))
    db.flush()


def _graph(db):
    a = _insight(db, "部署端口选择")
    b = _insight(db, "端口占用排查")
    c = _insight(db, "服务启动检查")
    d = _insight(db, "日志定位")
    e = _insight(db, "回滚流程")
    f = _insight(db, "缓存策略")
    g = _insight(db, "冷门经验")
    _link(db, a, b, "supports")
    _link(db, b, c, "supports")
    _link(db, a, c, "supports")
    _link(db, c, d, "related")
    _link(db, d, e, "related")
    _link(db, f, a, "contradicts")
    db.commit()
    return a, b, c, d, e, f, g


def test_graph_stats_structure(db):
    a, b, c, d, e, f, g = _graph(db)
    st = compute_graph_stats(db, APP, "alice")
    assert st["node_count"] == 7
    assert st["edge_count"] == 5
    assert st["contradicts_count"] == 1
    assert st["density"] == round(2 * 5 / (7 * 6), 4)
    assert st["orphan_count"] == 1
    assert st["orphans"][0]["subject"] == "冷门经验"
    assert st["hubs"][0]["subject"] == "服务启动检查"
    assert st["hubs"][0]["degree"] == 3
    sizes = sorted(x["size"] for x in st["communities"])
    assert sizes == [1, 1, 5]
    assert len(st["bridges"]) == 2
    assert not st["bridges_skipped"]
    assert len(st["tensions"]) == 1
    assert st["tensions"][0]["same_community"] is False


def test_graph_stats_ignores_other_user(db):
    _graph(db)
    st = compute_graph_stats(db, APP, "bob")
    assert st["node_count"] == 0
    assert st["edge_count"] == 0


def test_edge_learning_rules():
    assert strength_on_applied(0.5) == 0.55
    assert strength_on_applied(1.0) == 1.0
    assert strength_on_rejected(0.5) == 0.45
    assert strength_on_rejected(0.05) == 0.10  # 下限保底，不直接归零
    assert strength_after_idle(1.0, 1) == 0.95
    assert strength_after_idle(0.05, 100) == 0.10
    assert strength_after_idle(0.5, 0) == 0.5


def test_find_spread_edge(db):
    a, b, c, d, e, f, g = _graph(db)
    assert find_spread_edge(db, APP, "alice", a.id, b.id) is not None
    # 对称关系反向也能找到（存储时按端点 id 序规范化过）
    assert find_spread_edge(db, APP, "alice", b.id, a.id) is not None
    # 矛盾边不扩散：找不到
    assert find_spread_edge(db, APP, "alice", f.id, a.id) is None
    # 没边就是没边
    assert find_spread_edge(db, APP, "alice", a.id, g.id) is None

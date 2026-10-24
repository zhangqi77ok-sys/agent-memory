"""第二十轮「织网度量」：算一张记忆网的结构，而不只是把它画出来。

反思第 2 条和第 6 条的落地：系统此前只存了边、画了边，从没读过网。
本模块只读不写，给出网的四个基本量——枢纽、社群、桥、孤儿——以及
密度和矛盾张力，供统计接口、管理端和「连接深度」验收使用。

定义（写进代码，免得日后口径漂移）：
- 节点 = 该用户名下所有有效行（valid_to 为空）的事实 + 经验。
- 合作图 = supports / refines / generalizes / related 四种边构成的无向图；
  contradicts 边不进合作图（矛盾边连起来的不是一个共识簇），单独报
  为张力，并标注两端是否落在同一个合作社群。
- 孤儿 = 没有任何有效边（合作边和矛盾边都没有）的节点。
- 桥 = 合作图中删掉它就会断开连通的边（Tarjan，无向图，O(V+E)）。
- 密度 = 2E / (V(V-1))，V 取全部节点（含孤儿）：孤儿越多密度越低，
  这正是「网没织密」的度量，不把孤儿排除在外粉饰数字。

性能边界：本函数是 O(V+E) 的纯内存计算，不在简报路径上（简报
50ms 的账不走这里）。桥的 Tarjan 在边数超过 MAX_EDGES_FOR_BRIDGES
时跳过并明说 skipped——千万级图的精确桥要离线任务算，不在一次
接口调用里硬算；合作社群（并查集，近线性）不受此限。
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import MemoryFact, MemoryInsight, MemoryLink

COOPERATIVE_RELATIONS = ("supports", "refines", "generalizes", "related")
MAX_EDGES_FOR_BRIDGES = 200_000
MAX_MEMBERS_LISTED = 50


class _UnionFind:
    """并查集：合作社群用，近线性。"""

    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, x):
        parent = self.parent
        if x not in parent:
            parent[x] = x
            return x
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _tarjan_bridges(adj):
    """迭代版 Tarjan 求无向图的桥，返回桥边的 link id 集合。

    adj: {node: [(neighbor, link_id), ...]}。不用递归：千万级图递归
    会爆栈；迭代栈里存 (节点, 父边 id, 邻居迭代器)，迭代器本身有
    断点续跑的位置，孩子返回后父节点从断点继续扫邻居。
    平行边（两节点间两条边）天然不成桥：判断只跳过父边 id，不跳
    过父节点，平行边会被当作回边把 low 值拉下来。
    """
    disc: dict = {}
    low: dict = {}
    timer = 0
    bridges: set = set()
    for start in adj:
        if start in disc:
            continue
        disc[start] = low[start] = timer
        timer += 1
        stack = [(start, None, iter(adj[start]))]
        while stack:
            node, parent_edge, it = stack[-1]
            descended = False
            for nb, eid in it:
                if eid == parent_edge:
                    continue
                if nb not in disc:
                    disc[nb] = low[nb] = timer
                    timer += 1
                    stack.append((nb, eid, iter(adj[nb])))
                    descended = True
                    break
                low[node] = min(low[node], disc[nb])
            if descended:
                continue
            stack.pop()
            if parent_edge is not None and stack:
                parent = stack[-1][0]
                low[parent] = min(low[parent], low[node])
                if low[node] > disc[parent]:
                    bridges.add(parent_edge)
    return bridges


def compute_graph_stats(db, app_id, user_id, hub_k=10):
    """算该用户记忆网的结构统计。只读；返回 JSON 友好的 dict。"""
    subjects = {}
    for row in db.execute(select(MemoryFact.id, MemoryFact.subject).where(
            MemoryFact.app_id == app_id, MemoryFact.user_id == user_id,
            MemoryFact.valid_to.is_(None))):
        subjects[("fact", row.id)] = row.subject
    for row in db.execute(select(MemoryInsight.id, MemoryInsight.subject).where(
            MemoryInsight.app_id == app_id, MemoryInsight.user_id == user_id,
            MemoryInsight.valid_to.is_(None))):
        subjects[("insight", row.id)] = row.subject

    links = list(db.execute(select(
        MemoryLink.id, MemoryLink.src_kind, MemoryLink.src_id,
        MemoryLink.dst_kind, MemoryLink.dst_id, MemoryLink.relation,
        MemoryLink.strength).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        MemoryLink.valid_to.is_(None))))

    adj: dict = {node: [] for node in subjects}
    degree: dict = {node: 0 for node in subjects}
    total_degree: dict = {node: 0 for node in subjects}
    uf = _UnionFind()
    cooperative = []
    tensions = []
    for row in links:
        src = (row.src_kind, row.src_id)
        dst = (row.dst_kind, row.dst_id)
        if src not in subjects or dst not in subjects:
            continue  # 端点已硬删的残边：不进图（forget 入口负责清边，这里兜底）
        total_degree[src] += 1
        total_degree[dst] += 1
        if row.relation == "contradicts":
            tensions.append((row, src, dst))
            continue
        if row.relation not in COOPERATIVE_RELATIONS:
            continue
        cooperative.append(row)
        adj[src].append((dst, row.id))
        adj[dst].append((src, row.id))
        degree[src] += 1
        degree[dst] += 1
        uf.union(src, dst)

    communities = {}
    for node in subjects:
        communities.setdefault(uf.find(node), []).append(node)
    community_of = {}
    for root, members in communities.items():
        for node in members:
            community_of[node] = root

    node_count = len(subjects)
    edge_count = len(cooperative)
    density = (2 * edge_count / (node_count * (node_count - 1))
               if node_count > 1 else 0.0)
    orphans = [node for node in subjects if total_degree[node] == 0]
    hubs = sorted(subjects, key=lambda n: (-degree[n], subjects[n]))[:hub_k]

    bridges_skipped = edge_count > MAX_EDGES_FOR_BRIDGES
    bridge_ids = set() if bridges_skipped else _tarjan_bridges(adj)
    link_by_id = {row.id: row for row in cooperative}

    def _node_view(node):
        return {"kind": node[0], "id": node[1], "subject": subjects[node]}

    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "contradicts_count": len(tensions),
        "density": round(density, 4),
        "orphan_count": len(orphans),
        "orphan_rate": round(len(orphans) / node_count, 4) if node_count else 0.0,
        "orphans": [_node_view(node) for node in orphans],
        "hubs": [{**_node_view(node), "degree": degree[node]} for node in hubs],
        "communities": [
            {"size": len(members),
             "members": [_node_view(node) for node in members[:MAX_MEMBERS_LISTED]]}
            for members in sorted(communities.values(), key=len, reverse=True)
        ],
        "bridges_skipped": bridges_skipped,
        "bridges": [
            {"link_id": link_id,
             "src": _node_view((link_by_id[link_id].src_kind,
                                link_by_id[link_id].src_id)),
             "dst": _node_view((link_by_id[link_id].dst_kind,
                                link_by_id[link_id].dst_id)),
             "relation": link_by_id[link_id].relation,
             "strength": link_by_id[link_id].strength}
            for link_id in sorted(bridge_ids)
        ],
        "tensions": [
            {"link_id": row.id,
             "src": _node_view(src),
             "dst": _node_view(dst),
             "same_community": community_of.get(src) == community_of.get(dst)}
            for row, src, dst in tensions
        ],
    }

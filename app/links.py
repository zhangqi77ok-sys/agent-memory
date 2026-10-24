"""连接层（第十五轮「织网」）：记忆与记忆之间的关联边。

定位（用户 2026-10-24 定调）：信息是点，智慧是线与网。此前的十四轮把
「点」做得又准又快，经验在库里却一行行互不认识——召回只能按与查询的
相似度捞散行。这层补的正是线：写入时连边、巡检时织密与抽象、召回时
沿边扩散。边界：只连记忆↔记忆，不做世界实体图谱（那不是本系统的活）。

边的出身分两等，origin 如实标注、消费方区别对待：
- rule：确定性规则（主题关系、向量余弦带、否定标记启发式）推出的结构边，
  便宜、可测、离线可跑，但只是结构提示；
- llm：配置了 LLM 时判定器给出的语义边（支持/矛盾/细化/泛化），贵而准。
未配 LLM 时判定器缺席，规则边照常生长——网先织起来，语义精度随配置提升。

边的生死跟随端点：端点版本滚动（decide 的 UPDATE 关旧行开新行）时，
supports/refines/generalizes/related 迁到新版本，contradicts 关边——
矛盾针对的是旧文本，新文本是否还矛盾要重新判定；端点被硬删（forget /
clear_all）时入口显式清边；指向已关闭端点的残边由 weave_links 巡检兜底。
"""
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.embeddings import cosine
from app.extract import norm, same_subject
from app.models import MemoryFact, MemoryInsight, MemoryLink, _now
from app.tokenize import tokens_of

log = logging.getLogger(__name__)

RELATIONS = ("supports", "contradicts", "refines", "generalizes", "related")
SYMMETRIC = {"supports", "contradicts", "related"}  # 语义对称：按端点序规范化存储
SPREAD_RELATIONS = ("supports", "refines", "generalizes", "related")  # 扩散激活走的边（矛盾边不扩散，只标记）
SPREAD_DISCOUNT = 0.5   # 沿边一跳的分数折扣：关联想到的不如直接命中的可信
MIN_SPREAD_STRENGTH = 0.5
WEAVE_COS_RELATED = 0.88    # 跨主题规则连 related 的余弦线（写入时与巡检时共用）
WEAVE_COS_SUPPORTS = 0.80   # 同主题规则连 supports 的余弦线
GENERALIZE_COS = 0.90       # 抽象聚类的同构余弦线
MAX_WEAVE_CANDIDATES = 8    # 写入时织网的候选上限：写路径不能为织网付出无界成本

# 否定/禁止标记与指令标记：同主题、高重叠、一边禁止一边要求 → 规则判矛盾。
# 这是启发式，只认最明确的对撞；拿不准的一律降为 related，等判定器或用户裁决。
_PROHIBIT = ("不要", "禁止", "切勿", "避免", "不得", "不能", "别再", "不许")
_DIRECTIVE = ("必须", "应该", "务必", "优先", "总是", "一定要")


@dataclass(frozen=True)
class NodeView:
    """织网判定用的轻量节点视图：事实与经验统一成这四个字段参与分类。"""
    kind: str            # fact|insight
    id: str
    subject: str
    content: str
    applies_when: str = ""
    project: str | None = None

    @property
    def text(self) -> str:
        return f"{self.subject} {self.content} {self.applies_when}".strip()


def view_of(row) -> NodeView:
    if isinstance(row, MemoryInsight):
        return NodeView("insight", row.id, row.subject, row.content,
                        row.applies_when or "", row.project)
    return NodeView("fact", row.id, row.subject, row.content)


def _pair_cosine_rows(a, b) -> float | None:
    """两行的向量余弦；任一缺向量/模型或维度不一致返回 None（与 episodes 同口径）。"""
    if getattr(a, "embedding", None) is None or getattr(b, "embedding", None) is None:
        return None
    if a.embed_model != b.embed_model:
        return None
    va = [float(x) for x in a.embedding]
    vb = [float(x) for x in b.embedding]
    if len(va) != len(vb):
        return None
    return cosine(va, vb)


def _overlap(a: NodeView, b: NodeView) -> float:
    """token 重叠率 |∩| / min(|A|, |B|)：短文本被长文本包含时接近 1。"""
    ta, tb = tokens_of(a.text), tokens_of(b.text)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _canon(relation: str, a: tuple[str, str], b: tuple[str, str]):
    """对称关系按 (kind, id) 序排端点，保证同一条边只有一个存法。"""
    if relation in SYMMETRIC and b < a:
        return b, a
    return a, b


def create_link(db: Session, app_id: str, user_id: str,
                a: tuple[str, str], b: tuple[str, str], relation: str,
                strength: float, origin: str) -> MemoryLink | None:
    """落一条边（只 flush 不 commit）。已有同端点同关系的现行边则不重复建，
    返回既有行；自环与非法关系直接返回 None。"""
    if relation not in RELATIONS or a == b:
        return None
    (ak, ai), (bk, bi) = _canon(relation, a, b)
    existing = db.scalar(select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        MemoryLink.src_kind == ak, MemoryLink.src_id == ai,
        MemoryLink.dst_kind == bk, MemoryLink.dst_id == bi,
        MemoryLink.relation == relation, MemoryLink.valid_to.is_(None)))
    if existing is not None:
        return existing
    link = MemoryLink(app_id=app_id, user_id=user_id, src_kind=ak, src_id=ai,
                      dst_kind=bk, dst_id=bi, relation=relation,
                      strength=max(0.0, min(1.0, float(strength))), origin=origin)
    db.add(link)
    db.flush()
    return link


def links_touching(db: Session, app_id: str, user_id: str, kind: str, node_id: str,
                   relations: tuple[str, ...] | None = None) -> list[MemoryLink]:
    """某节点的全部现行边（两个方向都算）。"""
    stmt = select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        MemoryLink.valid_to.is_(None),
        or_((MemoryLink.src_kind == kind) & (MemoryLink.src_id == node_id),
            (MemoryLink.dst_kind == kind) & (MemoryLink.dst_id == node_id)))
    if relations:
        stmt = stmt.where(MemoryLink.relation.in_(relations))
    return list(db.scalars(stmt))


def neighbors_of(db: Session, app_id: str, user_id: str, kind: str, node_id: str,
                 relations: tuple[str, ...] | None = None) -> list[tuple[MemoryLink, str, str]]:
    """邻里清单：[(边, 对端 kind, 对端 id)]，给 REST/MCP 的邻里查询用。"""
    out = []
    for link in links_touching(db, app_id, user_id, kind, node_id, relations):
        if link.src_kind == kind and link.src_id == node_id:
            out.append((link, link.dst_kind, link.dst_id))
        else:
            out.append((link, link.src_kind, link.src_id))
    return out


def delete_links_touching(db: Session, app_id: str, user_id: str, kind: str, node_id: str) -> int:
    """硬删某节点的全部边（forget 入口用：节点本身是硬删，边也不能留）。"""
    rows = list(db.scalars(select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        or_((MemoryLink.src_kind == kind) & (MemoryLink.src_id == node_id),
            (MemoryLink.dst_kind == kind) & (MemoryLink.dst_id == node_id)))))
    for r in rows:
        db.delete(r)
    return len(rows)


def delete_links_for_user(db: Session, app_id: str, user_id: str) -> int:
    rows = list(db.scalars(select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id)))
    for r in rows:
        db.delete(r)
    return len(rows)


def migrate_links(db: Session, app_id: str, user_id: str, kind: str,
                  old_id: str, new_id: str) -> int:
    """端点版本滚动时迁边：关联类边改指新版本；contradicts 关边（针对旧文本）。
    迁移后与既有边撞车（同端点同关系已有现行边）的关掉迁移行防重复。"""
    moved = 0
    for link in links_touching(db, app_id, user_id, kind, old_id):
        if link.relation == "contradicts":
            link.valid_to = _now()
            continue
        if link.src_kind == kind and link.src_id == old_id:
            link.src_id = new_id
        if link.dst_kind == kind and link.dst_id == old_id:
            link.dst_id = new_id
        if link.src_id == link.dst_id and link.src_kind == link.dst_kind:
            link.valid_to = _now()  # 迁完变自环（新旧版本互指）→ 关掉
            continue
        dup = db.scalar(select(MemoryLink).where(
            MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
            MemoryLink.id != link.id, MemoryLink.valid_to.is_(None),
            MemoryLink.src_kind == link.src_kind, MemoryLink.src_id == link.src_id,
            MemoryLink.dst_kind == link.dst_kind, MemoryLink.dst_id == link.dst_id,
            MemoryLink.relation == link.relation))
        if dup is not None:
            link.valid_to = _now()
        else:
            moved += 1
    return moved


def spread_from(db: Session, app_id: str, user_id: str,
                seeds: list[tuple[str, str, float]], kind: str) -> dict[str, tuple[float, str]]:
    """扩散激活一跳：从种子节点沿关联类边走到同类邻居。
    返回 {邻居 id: (折扣分, 来源种子 id)}；折扣 = 种子分 × SPREAD_DISCOUNT × 边强度，
    多个种子指向同一邻居取最高分。矛盾边不参与扩散（它由简报单独标记）。"""
    best: dict[str, tuple[float, str]] = {}
    for skind, sid, score in seeds:
        if skind != kind:
            continue
        for link, okind, oid in neighbors_of(db, app_id, user_id, skind, sid,
                                             relations=SPREAD_RELATIONS):
            if okind != kind or link.strength < MIN_SPREAD_STRENGTH:
                continue
            val = score * SPREAD_DISCOUNT * link.strength
            if oid not in best or val > best[oid][0]:
                best[oid] = (val, sid)
    return best


def induced_edges(db: Session, app_id: str, user_id: str,
                  ids: list[str], kind: str = "insight") -> list[dict]:
    """胜出集的诱导子图边（第二十轮「织网简报」）：两端都在给定节点集里的
    现行边。简报不再只递一张散列表——胜出经验之间怎么连着，一并递出；
    每条胜出经验的 linked_from 路径已在 InsightOut 上（见 brief_for_task）。
    """
    if len(ids) < 2:
        return []
    idset = set(ids)
    rows = db.scalars(select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        MemoryLink.valid_to.is_(None),
        MemoryLink.src_kind == kind, MemoryLink.dst_kind == kind,
        MemoryLink.src_id.in_(idset), MemoryLink.dst_id.in_(idset)))
    return [{"link_id": r.id, "src": r.src_id, "dst": r.dst_id,
             "relation": r.relation, "strength": r.strength} for r in rows]


def contradiction_pairs(db: Session, app_id: str, user_id: str,
                        ids: list[str], kind: str = "insight") -> dict[str, list[str]]:
    """给定一组节点 id，查其中现行 contradicts 边的配对：{id: [与之矛盾的 id]}。"""
    if len(ids) < 2:
        return {}
    idset = set(ids)
    rows = db.scalars(select(MemoryLink).where(
        MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
        MemoryLink.relation == "contradicts", MemoryLink.valid_to.is_(None),
        MemoryLink.src_kind == kind, MemoryLink.dst_kind == kind,
        MemoryLink.src_id.in_(idset), MemoryLink.dst_id.in_(idset)))
    out: dict[str, list[str]] = {}
    for link in rows:
        out.setdefault(link.src_id, []).append(link.dst_id)
        out.setdefault(link.dst_id, []).append(link.src_id)
    return out


# ---------- 边的分类：规则（默认）与 LLM 判定器（可选） ----------

def _opposing_markers(a: NodeView, b: NodeView) -> bool:
    """一边带禁止标记、一边带指令标记 → 疑似正面对撞（规则判矛盾的唯一入口）。"""
    def has(text, marks):
        return any(mk in text for mk in marks)
    return (has(a.content, _PROHIBIT) and has(b.content, _DIRECTIVE)) or \
           (has(b.content, _PROHIBIT) and has(a.content, _DIRECTIVE))


def classify_pair(a: NodeView, b: NodeView, cos: float | None,
                  judge=None) -> tuple[str, float, str] | None:
    """判定一对节点该连什么边，返回 (relation, strength, origin) 或 None（不连）。

    有判定器（LLM）先问判定器：它说 none 就尊重它（语义判断优先于规则猜测）；
    判定器缺席或调用失败才落规则。规则口径宁缺毋滥：判不了矛盾就不判，
    同主题不同内容至少连 related——先把「这两条有关系」记下来，
    关系性质可以日后由判定器或用户升级。
    """
    if norm(a.content) == norm(b.content) and same_subject(a.subject, b.subject):
        return None  # 同一条记忆本身（去重层的活），不连自环式边
    if judge is not None:
        try:
            verdict = judge(a, b)
        except Exception as e:
            log.warning("link judge failed: %s", e)
            verdict = None
        if verdict is not None:
            relation, strength = verdict
            if relation == "none":
                return None
            if relation in RELATIONS:
                return relation, strength, "llm"
    overlap = _overlap(a, b)
    if same_subject(a.subject, b.subject):
        if _opposing_markers(a, b) and overlap >= 0.3:
            return "contradicts", 0.6, "rule"
        if cos is not None and cos >= WEAVE_COS_SUPPORTS:
            return "supports", cos, "rule"
        return "related", max(overlap, 0.5), "rule"
    if cos is not None and cos >= WEAVE_COS_RELATED:
        # 跨主题高余弦：通用（无项目）与项目专属之间优先记泛化，其余记相关
        if a.project is None and b.project:
            return "generalizes", cos, "rule"  # a 泛化 b（create_link 按方向存）
        if b.project is None and a.project:
            return "generalizes", cos, "rule"  # 方向在 weave 里按端点摆正
        return "related", cos, "rule"
    return None


class LlmLinker:
    """关联判定器：与提炼器共用 OpenAI 兼容端点。只做三件事——判一对记忆的
    关系、在巡检抽象时把一组同构经验起草成一条通用原则、在矛盾分化时把
    一对条件分流的经验综合成一条原则。输出一律 JSON、解析失败按「无判定」
    处理，绝不让判定器故障污染写路径。"""

    _JUDGE_SYSTEM = """你是记忆关联判定器。给你两条记忆（主题/内容/适用情境），判断它们的关系：
supports（互相佐证同一做法）/ contradicts（做法正面对立，不能同时成立）/
refines（一条是另一条的细化或特例）/ generalizes（一条是另一条的一般化表述）/
related（相关但不属以上）/ none（无实质关系）。
refines 与 generalizes 要给出方向：from 是前一条（A），to 是后一条（B）时，
relation 仍按 A 相对 B 的关系填（A refines B 就填 refines）。
只输出 JSON：{"relation":"supports","strength":0.8}，strength 是 0 到 1 的把握度。"""

    _GENERALIZE_SYSTEM = """你是经验抽象器。给你一组来自不同项目的同构经验，
提炼出一条跨项目通用的原则：去掉项目专有细节，只留可迁移的做法。
只输出 JSON：{"subject":"稳定短主题","content":"一到两句可直接照着做的指导","applies_when":"适用情境"}。
若这组经验其实并不构成同一原则，输出 {"subject":""}。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _call(self, system: str, user: str) -> dict | None:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
            raw = payload["choices"][0]["message"]["content"]
            raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M)
            start, end = raw.find("{"), raw.rfind("}")
            return json.loads(raw[start:end + 1]) if 0 <= start < end else None
        except Exception as e:
            log.warning("linker call failed: %s", e)
            return None

    def judge(self, a: NodeView, b: NodeView) -> tuple[str, float] | None:
        out = self._call(self._JUDGE_SYSTEM,
                         f"A：主题={a.subject}；内容={a.content}；情境={a.applies_when}\n"
                         f"B：主题={b.subject}；内容={b.content}；情境={b.applies_when}")
        if not out:
            return None
        relation = str(out.get("relation") or "").strip()
        if relation not in RELATIONS and relation != "none":
            return None
        try:
            strength = float(out.get("strength", 0.7))
        except (TypeError, ValueError):
            strength = 0.7
        return relation, max(0.0, min(1.0, strength))

    def generalize(self, members: list[NodeView]) -> tuple[str, str, str] | None:
        text = "\n".join(f"- [{v.project or '通用'}] {v.subject}：{v.content}" for v in members)
        out = self._call(self._GENERALIZE_SYSTEM, text)
        if not out or not str(out.get("subject") or "").strip():
            return None
        return (str(out["subject"]).strip(), str(out.get("content") or "").strip(),
                str(out.get("applies_when") or "").strip())

    _SYNTHESIZE_SYSTEM = """你是经验综合器。给你一对曾经被判定为矛盾、但后来发现
各在不同情境下成立的经验（胜家与输家：主题/内容/适用情境/证据条数），起草
一条陈述条件分流的原则：明确说明哪条在什么情境下成立、另一条在什么情境
下成立，让后人不用再从两条经验里自己推条件。
只输出 JSON：{"subject":"稳定短主题","content":"一到两句，写清条件分流",
"applies_when":"两者情境的并集或上位情境"}。content 例如“A 在 X 情境成立、
B 在 Y 情境成立”。若两条经验其实是同一条件下的真矛盾（没有可分流的
情境差），输出 {"subject":""}。"""

    def synthesize_condition(self, winner: NodeView, loser: NodeView,
                             winner_evidence: int = 0, loser_evidence: int = 0,
                             ) -> tuple[str, str, str] | None:
        """分化即综合：胜家/输家各在不同情境下成立时，起草一条条件分流原则。
        空 subject 或空 content 都视为「综合不成」返回 None（调用方分化照常，
        不硬编自然语言）；调用失败同样返回 None（见 _call）。"""
        text = (f"胜家：主题={winner.subject}；内容={winner.content}；"
                f"情境={winner.applies_when}；证据={winner_evidence} 条\n"
                f"输家：主题={loser.subject}；内容={loser.content}；"
                f"情境={loser.applies_when}；证据={loser_evidence} 条")
        out = self._call(self._SYNTHESIZE_SYSTEM, text)
        if not out or not str(out.get("subject") or "").strip():
            return None
        content = str(out.get("content") or "").strip()
        if not content:
            return None
        return (str(out["subject"]).strip(), content,
                str(out.get("applies_when") or "").strip())


def build_linker(s: Settings = settings) -> LlmLinker | None:
    """与提炼器共用 LLM 配置；未配置返回 None（规则织网照常工作）。"""
    if not (s.llm_base_url and s.llm_api_key and s.llm_model):
        return None
    return LlmLinker(s.llm_base_url, s.llm_api_key, s.llm_model, timeout=s.llm_timeout_s)


# ---------- 织网动作：写入时、巡检时 ----------

def _active_rows(db: Session, model, app_id: str, user_id: str):
    return list(db.scalars(select(model).where(
        model.app_id == app_id, model.user_id == user_id, model.valid_to.is_(None))))


def weave_on_write(db: Session, app_id: str, user_id: str, row, linker=None) -> int:
    """新节点落库后织网：找候选邻居（同主题 + 向量最近若干），逐对分类连边。
    返回新建边数；只 flush 不 commit（调用方事务里顺带提交）。候选有界
    （MAX_WEAVE_CANDIDATES），写路径不为织网做无界扫描——存量关系靠巡检补。"""
    model = MemoryInsight if isinstance(row, MemoryInsight) else MemoryFact
    view = view_of(row)
    peers = [r for r in _active_rows(db, model, app_id, user_id) if r.id != row.id]
    if not peers:
        return 0
    scored: list[tuple[float, object]] = []
    for p in peers:
        if same_subject(p.subject, row.subject):
            scored.append((2.0, p))  # 同主题优先进候选
        else:
            c = _pair_cosine_rows(row, p)
            if c is not None and c >= WEAVE_COS_RELATED:
                scored.append((c, p))
    scored.sort(key=lambda t: t[0], reverse=True)
    made = 0
    judge = linker.judge if linker is not None else None
    for _, peer in scored[:MAX_WEAVE_CANDIDATES]:
        verdict = classify_pair(view, view_of(peer), _pair_cosine_rows(row, peer), judge=judge)
        if verdict is None:
            continue
        relation, strength, origin = verdict
        a, b = (view.kind, view.id), (peer_kind(peer), peer.id)
        if relation == "generalizes" and (view.project is None) != (peer.project is None):
            # 方向摆正：通用侧（无项目）→ 专属侧；两侧项目状态相同时尊重判定原序
            a, b = (a, b) if view.project is None else (b, a)
        if create_link(db, app_id, user_id, a, b, relation, strength, origin) is not None:
            made += 1
    return made


def peer_kind(row) -> str:
    return "insight" if isinstance(row, MemoryInsight) else "fact"


def _uf_find(parent: list[int], x: int) -> int:
    """并查集 find（带路径压缩），抽象聚类用。"""
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _close_dead_links(db: Session) -> int:
    """关掉指向已关闭/已删除端点的现行边，返回关边数（巡检兜底）。"""
    alive = {}
    for model, kind in ((MemoryFact, "fact"), (MemoryInsight, "insight")):
        alive[kind] = {r[0] for r in db.execute(
            select(model.id).where(model.valid_to.is_(None))).all()}
    closed = 0
    for link in db.scalars(select(MemoryLink).where(MemoryLink.valid_to.is_(None))):
        if link.src_id not in alive.get(link.src_kind, set()) or \
                link.dst_id not in alive.get(link.dst_kind, set()):
            link.valid_to = _now()
            closed += 1
    return closed


def weave_links(db: Session, linker=None) -> dict:
    """巡检织网（worker 每小时，与 consolidate_insights 同节拍），自己 commit：
    1. 关掉指向已关闭端点的残边；
    2. 同用户同类节点间补边：同主题未连的对子、同余弦够高的跨主题对子，
       逐对分类（有判定器问判定器，否则规则）——第十一轮「矛盾只计数」
       到这一轮落成结构：矛盾对在这里变成 contradicts 边，进简报会被标记；
    3. 抽象：跨 ≥2 个项目、规模 ≥3 的同构经验簇，若判定器能起草通用原则，
       落一条 project=None 的经验并连 generalizes 边（无判定器不硬造原则，
       簇内只连 related）。
    返回 {"pruned": n, "linked": n, "contradicts": n, "generalized": n}。"""
    from app.auth import UserContext
    from app.episodes import _apply_insight, _Cand  # 局部导入避免循环（episodes 也 import 本模块）

    stats = {"pruned": _close_dead_links(db), "linked": 0, "contradicts": 0,
             "generalized": 0, "decayed_edges": 0}
    judge = linker.judge if linker is not None else None
    users = db.execute(select(MemoryInsight.app_id, MemoryInsight.user_id).distinct()).all()
    users += [u for u in db.execute(select(MemoryFact.app_id, MemoryFact.user_id).distinct()).all()
              if u not in users]
    for app_id, user_id in users:
        linked_pairs: set[frozenset] = set()
        for link in db.scalars(select(MemoryLink).where(
                MemoryLink.app_id == app_id, MemoryLink.user_id == user_id,
                MemoryLink.valid_to.is_(None))):
            linked_pairs.add(frozenset(((link.src_kind, link.src_id), (link.dst_kind, link.dst_id))))
        insight_rows: list = []
        for model, kind in ((MemoryInsight, "insight"), (MemoryFact, "fact")):
            rows = _active_rows(db, model, app_id, user_id)
            if kind == "insight":
                insight_rows = rows
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    a, b = rows[i], rows[j]
                    key = frozenset(((kind, a.id), (kind, b.id)))
                    if key in linked_pairs:
                        continue
                    cos = _pair_cosine_rows(a, b)
                    if not same_subject(a.subject, b.subject) and \
                            (cos is None or cos < WEAVE_COS_RELATED):
                        continue
                    verdict = classify_pair(view_of(a), view_of(b), cos, judge=judge)
                    if verdict is None:
                        continue
                    relation, strength, origin = verdict
                    x, y = (kind, a.id), (kind, b.id)
                    va, vb = view_of(a), view_of(b)
                    if relation == "generalizes" and (va.project is None) != (vb.project is None):
                        x, y = (x, y) if va.project is None else (y, x)
                    # 这对端点此前无任何现行边（linked_pairs 已过滤），建出来即真新建
                    if create_link(db, app_id, user_id, x, y, relation, strength, origin):
                        stats["linked"] += 1
                        if relation == "contradicts":
                            stats["contradicts"] += 1
                        linked_pairs.add(key)
        # 抽象：同构跨项目簇 → 通用原则（需判定器起草；簇用并查集按余弦连通）
        members = [r for r in insight_rows if r.project]
        parent = list(range(len(members)))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                c = _pair_cosine_rows(members[i], members[j])
                if c is not None and c >= GENERALIZE_COS:
                    ri, rj = _uf_find(parent, i), _uf_find(parent, j)
                    if ri != rj:
                        parent[ri] = rj
        clusters: dict[int, list] = {}
        for i, r in enumerate(members):
            clusters.setdefault(_uf_find(parent, i), []).append(r)
        for cluster in clusters.values():
            if len(cluster) < 3 or len({r.project for r in cluster}) < 2:
                continue
            views = [view_of(r) for r in cluster]
            drafted = linker.generalize(views) if linker is not None else None
            if drafted is None:
                continue  # 无起草能力不硬造原则；簇内 related 已在上面补齐
            subject, content, applies_when = drafted
            if not content:
                continue
            u = UserContext(app_id, user_id)
            _, gid = _apply_insight(db, u, _Cand("principle", subject, content, applies_when),
                                    None, None, project=None)
            if gid:
                stats["generalized"] += 1
                for r in cluster:
                    create_link(db, app_id, user_id, ("insight", gid), ("insight", r.id),
                                "generalizes", 0.8, "llm")
    # 第二十轮「边学习」：闲置衰减结清。每月节拍但顺带调用安全——不足整月
    # 的边直接跳过，锚点保证同一段闲置只扣一次（见 decay_idle_edges）。
    # 局部导入：edge_learning 反向 import 本模块的 SPREAD_RELATIONS。
    from app.edge_learning import decay_idle_edges
    stats["decayed_edges"] = decay_idle_edges(db)
    db.commit()
    return stats

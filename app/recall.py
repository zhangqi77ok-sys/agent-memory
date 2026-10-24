"""召回：检索前预过滤（用户 + valid_to IS NULL），再打分、裁剪。

打分两路：关键词 bigram 重叠（始终可用）+ 向量余弦（配了嵌入服务且该事实存有向量时）。
两者都在时按 0.7 向量 + 0.3 关键词融合排序（reason=hybrid），让字面不同但语义相同的内容也能召回；
嵌入不可用或调用失败时整体退回纯关键词，行为与旧版一致。

入选门槛是分开判的，不看融合分：关键词路 kw ≥ MIN_SCORE，或向量路 cos ≥ COS_MIN。
原因是 Gemini 向量的余弦整体偏高（不相关句对也有 0.48–0.65），融合分 0.7·cos 随手就能
越过 0.10，只看融合分会把不相关的事实大批放进来。

第十四轮（索引化召回）：门槛与打分语义一个字不改，改的是「候选从哪来」。
旧实现把作用域内的每一行都搬进 Python 逐行分词打分，耗时随行数线性增长
（简报在 5000 条经验时 p50 约 186ms，几乎全花在重复分词上）。现在分三步：
1. 查询只分词一次（_QueryPlan），每条候选的关键词分改用「token 在不在文本里」的
   存在性测试，与分词集合交集严格等价（等价性有性质测试守着）；
2. 能确定候选超集时先用 SQL 通道把行数压下来再搬：关键词通道（命中 token 数
   在 SQL 里数到门槛所需的最少数，是门槛通过者的超集）∪ 置顶通道（经验侧）
   是一个分支；向量通道（仅 Postgres：余弦距离过线且模型对得上，阈值留浮点
   余量）单独成一个 UNION 分支——混在同一个 OR 里时规划器用不上 HNSW 索引；
   入选后仍在 Python 侧精确复核；
3. 压不下来的情形（SQLite 带向量、PG 上查询向量维度与列对不上）保持全量打分，
   语义一个不少——SQLite 是小量级部署，全量可接受；规模在 Postgres 上才发生。
"""
import re

from sqlalchemy import and_, case, or_, select, text, union
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.embeddings import cosine, vector_usable
from app.schemas import FactOut

MIN_SCORE = 0.10  # 基于50条陪伴评测集校准的召回阈值，兼顾长Query召回率与防误召回
VECTOR_WEIGHT = 0.7  # 融合时向量占权；关键词只作辅助，避免字面命中压过语义相关
# 余弦门槛：2026-10-23 用真实 Gemini 向量小样本校准（5 条事实 × 5 条查询，1536 维）：
# 命中对余弦 0.675–0.751，未命中对 0.476–0.647，分界取 0.66。小样本结论，后续用评测集复校。
COS_MIN = 0.66
_COS_EPS = 1e-3  # 向量通道 SQL 阈值的浮点余量：宁可多捞一行，不能漏掉压线命中

_CJK_LO, _CJK_HI = "一", "鿿"


def _tokens(s: str) -> set[str]:
    """英文按词；中文按相邻两字（bigram）切，避免「咖啡」和「咖喱」因为共享一个字而命中。

    单个汉字的连续段（如查询只有一个字）退回按字。空白、标点不参与计分。
    """
    s = s.lower()
    out = set(re.findall(r"[a-z0-9]+", s))
    for run in re.findall(r"[一-鿿]+", s):
        out |= {run} if len(run) == 1 else {run[i:i + 2] for i in range(len(run) - 1)}
    return out


class _QueryPlan:
    """查询侧一次性预处理：token 集合 + 每类 token 最便宜的存在性测试。

    与 _tokens 的集合语义严格等价：
    - 汉字 bigram（相邻两字）：命中 ⟺ 是原文子串（相邻两汉字必在同一连续段里）；
    - 孤字汉字（查询里自成一段）：必须是文本里的孤段——前后挨着汉字时，它只是
      别人 bigram 的一部分，不单独成 token；
    - 英文/数字词：必须是文本里的完整词——前后挨着 [a-z0-9] 时只是长词的一部分。
    """

    __slots__ = ("tokens", "_subs", "_patterns")

    def __init__(self, query: str):
        self.tokens = _tokens(query)
        self._subs: list[str] = []
        self._patterns: list[re.Pattern] = []
        for t in self.tokens:
            if len(t) == 2 and _CJK_LO <= t[0] <= _CJK_HI and _CJK_LO <= t[1] <= _CJK_HI:
                self._subs.append(t)
            elif len(t) == 1 and _CJK_LO <= t <= _CJK_HI:
                self._patterns.append(re.compile(
                    rf"(?<![{_CJK_LO}-{_CJK_HI}]){t}(?![{_CJK_LO}-{_CJK_HI}])"))
            else:
                self._patterns.append(re.compile(
                    rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])"))

    def kw_score(self, text: str) -> float:
        if not self.tokens:
            return 0.0
        low = text.lower()
        hits = sum(t in low for t in self._subs)
        hits += sum(p.search(low) is not None for p in self._patterns)
        return hits / len(self.tokens)


def _score(query: str, text: str) -> float:
    return _QueryPlan(query).kw_score(text)


def _keyword_channel(cols, plan: "_QueryPlan", dialect: str):
    """关键词通道：命中 token 数达到关键词门槛所需的最少数——在 SQL 里先把门槛筛掉。

    计数口径是 kw_score 的超集：token 在分词集合中 ⇒ 原文必含该子串；反过来子串
    命中可能多算（英文词嵌在长词里、孤字嵌在别人 bigram 里），多算只会多捞候选，
    精筛仍由 kw_score 精确做。拼接列时用空格分隔，与打分时 f"{a} {b}" 的断段一致。
    token 只含汉字或 [a-z0-9]，不含 LIKE 通配符，不用转义。
    大小写：token 已小写化，匹配必须大小写不敏感。SQLite 的 LIKE 对 ASCII 天生
    不敏感，直接用 LIKE（SQLAlchemy 的 ilike 会渲染成 lower(拼接列) LIKE，
    每个 token 重算一遍 lower，是真机实测的性能毒药）；Postgres 的 LIKE 敏感，
    用 ILIKE。
    """
    tokens = plan.tokens
    if not tokens:
        return None
    needed = 1
    while needed / len(tokens) < MIN_SCORE:
        needed += 1
    joined = cols[0]
    for c in cols[1:]:
        joined = joined + " " + c
    if dialect == "sqlite":
        hits = sum(case((joined.like(f"%{t}%"), 1), else_=0) for t in tokens)
    else:
        hits = sum(case((joined.ilike(f"%{t}%"), 1), else_=0) for t in tokens)
    return hits >= needed


_VECTOR_CANDIDATES = 200  # 向量分支的近邻上限：过线行数超过它时，更远的可能漏（见下）


def _vector_branch(model, cols, scope, qvec: list[float], embedder_model: str | None):
    """向量分支（仅 Postgres）：余弦距离过线且最近的 _VECTOR_CANDIDATES 条——走 HNSW。

    为什么是「过线 + 按距离排序 + LIMIT」而不是只写距离条件：真库 EXPLAIN 实测，
    纯阈值条件规划器估不出选择率、只肯顺序扫；带 ORDER BY 距离 + LIMIT 才走
    ix_fact_hnsw / ix_insight_hnsw 的 Index Scan。代价是近似性：过线行超过上限时
    更远的会被截掉（关键词与置顶分支不受影响，入选后 Python 侧仍精确复核；
    SQLite 路径不走这里、保持全量精确）。模型对得上才算数（与 vector_usable
    同口径：存量行没记模型的老数据放行）。
    距离表达式只能走 text 片段：embedding 列是 TypeDecorator，比较器不转发
    pgvector 的 cosine_distance()（真库 AttributeError）；改用 <=> 算子拼 ORM
    表达式时，查询向量的绑定值会被 pgvector 类型转换跑两遍（真库 ValueError）。
    向量以字符串字面量传参、由 PG 侧 CAST 成 vector，两个坑都绕开；阈值是本
    模块常数算出的浮点字面量，不是外部输入。
    返回 (语句, 绑定参数)。
    """
    table = model.__tablename__
    dist = f"{table}.embedding <=> CAST(:qvec_r14 AS vector)"
    cond = text(f"{dist} <= {1 - COS_MIN + _COS_EPS!r}")
    if embedder_model is not None:
        cond = and_(cond, or_(model.embed_model == embedder_model,
                              model.embed_model.is_(None)))
    stmt = (select(*cols).where(*scope, cond)
            .order_by(text(dist)).limit(_VECTOR_CANDIDATES))
    vec_literal = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
    return stmt, {"qvec_r14": vec_literal}


def _est_tokens(s: str) -> int:
    """粗略 token 估算：汉字约 1 token/字，其余字符约 4 字符/token。
    比 len//2 靠谱些，但仍是估算——预算只是防爆上限，不追求精确。"""
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿")
    return max(1, cjk + (len(s) - cjk) // 4)


def _query_vector(embedder, query: str) -> list[float] | None:
    """取查询向量。嵌入服务不可用或失败都返回 None，召回据此降级，不抛错。"""
    if embedder is None:
        return None
    try:
        vec = embedder.embed([query], task_type="RETRIEVAL_QUERY")[0]
    except Exception:
        return None
    return list(vec) if vec else None


def recall_facts(db: Session, u: UserContext, query: str, k: int, token_budget: int, embedder=None) -> list[FactOut]:
    k = max(1, int(k))  # 服务层兜底：负数/0 的 k 会让切片语义出错（MCP 等入口不走 schemas 校验）
    plan = _QueryPlan(query)
    qvec = _query_vector(embedder, query)
    # 无查询向量时不搬 embedding 列：SQLite 上它是 JSON，每行都要解码，纯属浪费
    cols = [m.MemoryFact.id, m.MemoryFact.subject, m.MemoryFact.content]
    if qvec is not None:
        cols += [m.MemoryFact.embedding, m.MemoryFact.embed_model]
    scope = (m.MemoryFact.app_id == u.app_id,
             m.MemoryFact.user_id == u.user_id,
             m.MemoryFact.valid_to.is_(None))
    kw_chan = _keyword_channel((m.MemoryFact.subject, m.MemoryFact.content), plan,
                               db.get_bind().dialect.name)
    # 向量通道只在 Postgres 且查询向量维度与列一致时才成立（HNSW 索引按列维度建）
    indexed_vector = (db.get_bind().dialect.name == "postgresql" and qvec is not None
                      and len(qvec) == settings.embedding_dim)
    params: dict = {}
    if qvec is None or indexed_vector:
        # 通道完备才过滤：无向量时关键词通道即候选全集；有向量时须向量通道在场，
        # 否则纯语义命中（零字面重叠）会被关键词通道漏掉。
        # 向量通道单独成一个 UNION 分支：与关键词条件 OR 在同一条语句里时，
        # 规划器无法对距离条件用 HNSW 索引（其余分支无索引可用），只能全表扫；
        # 分开后距离分支自己走索引，关键词分支扫表，结果按行合并去重。
        parts = []
        if kw_chan is not None:
            parts.append(select(*cols).where(*scope, kw_chan))
        if indexed_vector:
            branch, params = _vector_branch(m.MemoryFact, cols, scope, qvec,
                                            getattr(embedder, "model", None))
            parts.append(branch)
        if not parts:
            return []  # 无 token 又无向量：旧实现下每条都会被门槛挡掉，结果为空
        stmt = union(*parts) if len(parts) > 1 else parts[0]
    else:
        # SQLite 带向量 / PG 维度对不上：通道压不了候选，全量精确打分
        stmt = select(*cols).where(*scope)
    model_name = getattr(embedder, "model", None) if embedder is not None else None

    def _final(r) -> tuple[float, str, bool]:
        kw = plan.kw_score(f"{r.subject} {r.content}")
        emb = r.embedding if qvec is not None else None  # 无向量时该列根本没查
        if qvec is not None and vector_usable(r.embed_model, emb, qvec, model_name):
            cos = cosine(qvec, list(emb))
            blended = VECTOR_WEIGHT * cos + (1 - VECTOR_WEIGHT) * kw
            return blended, "hybrid", kw >= MIN_SCORE or cos >= COS_MIN
        return kw, "keyword", kw >= MIN_SCORE

    scored = sorted(((*_final(r), r) for r in db.execute(stmt, params)), key=lambda x: x[0], reverse=True)
    out, used = [], 0
    for s, reason, ok, r in scored:
        if not ok:
            continue
        if len(out) >= k:
            break
        cost = _est_tokens(r.content)
        if used + cost > token_budget:
            break
        used += cost
        out.append(FactOut(fact_id=r.id, subject=r.subject, content=r.content, score=round(s, 4),
                           reason=reason))
    return out

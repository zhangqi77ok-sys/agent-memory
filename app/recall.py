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

第十四轮补完（倒排索引）：第一版关键词通道把命中计数下推成 SQL 的 LIKE 计数门，
真机剖析发现地板在 ~60ms/5000 行——逐行 × 逐 token 的子串扫描，换 instr、
先拼文本都压不下去（剖析数据见 docs/eval.md）。正解是倒排索引：每行的 token
集合落表（memory_fact_token / memory_insight_token，models 里 ORM 事件同事务
维护），探针按 token IN + GROUP BY 计数，只走索引探查。倒排存的正是打分文本
的 token 集合，COUNT 出的命中数与 kw_score 的分子严格相等，探针行在 Python
侧直接采用、不必重算；超长 token 查询等索引不成立的情形退全量精确打分。

第二十轮（锚点探针，只走 Postgres）：倒排探针在 1M 行下现了原形——
耗时 ∝ 命中桶（top 查询桶 342150 → p50 7.9s），加 2000-cap 也只压到
1065ms（stage-1 聚合本身占主导：1M posting 扫描 + 24 万组 hash 聚合落盘）。
锚点探针（_anchor_probe）把成本压到 O(CAP)：按查询词序逐个取锚，
每锚取前 CAP 条 posting，LATERAL 按主键索引逐行验跨全部 token 的精确
命中数，累计 ≥ CAP 即停；找不够 CAP 时回落精确探针（正确优先）。
配套修补：置顶臂裸 pinned 以命中部分索引（IS TRUE 认不出谓词，
1M 行下 210ms 全表扫描 → 0.014ms 索引扫描）、赢家 use_count 一条批量
UPDATE、非文本更新跳过倒排同步。1M 行实测简报 p50 top 39.6ms /
mid 44.6ms（k=5），胜出分数与精确版逐字相同；千万级外推见 docs/eval.md。
"""
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import and_, bindparam, func, null, or_, select, text, union_all
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.embeddings import cosine, vector_usable
from app.schemas import FactOut
from app.tokenize import CJK_HI, CJK_LO, MAX_TOKEN_LEN, ordered_tokens
from app.tokenize import tokens_of as _tokens

MIN_SCORE = 0.10  # 基于50条陪伴评测集校准的召回阈值，兼顾长Query召回率与防误召回
VECTOR_WEIGHT = 0.7  # 融合时向量占权；关键词只作辅助，避免字面命中压过语义相关
# 余弦门槛：2026-10-23 用真实 Gemini 向量小样本校准（5 条事实 × 5 条查询，1536 维）：
# 命中对余弦 0.675–0.751，未命中对 0.476–0.647，分界取 0.66。小样本结论，后续用评测集复校。
COS_MIN = 0.66
_COS_EPS = 1e-3  # 向量通道 SQL 阈值的浮点余量：宁可多捞一行，不能漏掉压线命中

_CJK_LO, _CJK_HI = CJK_LO, CJK_HI


class _QueryPlan:
    """查询侧一次性预处理：token 集合 + 每类 token 最便宜的存在性测试。

    与 _tokens 的集合语义严格等价：
    - 汉字 bigram（相邻两字）：命中 ⟺ 是原文子串（相邻两汉字必在同一连续段里）；
    - 孤字汉字（查询里自成一段）：必须是文本里的孤段——前后挨着汉字时，它只是
      别人 bigram 的一部分，不单独成 token；
    - 英文/数字词：必须是文本里的完整词——前后挨着 [a-z0-9] 时只是长词的一部分。
    """

    __slots__ = ("query", "tokens", "_subs", "_patterns")

    def __init__(self, query: str):
        self.query = query  # 锚点探针用查询词序选锚（ordered_tokens）
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


def _needed_count(plan: "_QueryPlan") -> int:
    """关键词门槛换算成命中数下限：hits/|q| ≥ MIN_SCORE 的最小 hits。"""
    needed = 1
    while needed / len(plan.tokens) < MIN_SCORE:
        needed += 1
    return needed


_NULL_HITS = null().label("kw_hits")


def _probe_arm(model, token_model, id_key: str, cols, scope, plan: "_QueryPlan"):
    """关键词探针（倒排索引）：命中数在索引侧数完，只搬过门的行，并带回精确命中数。

    倒排表存的是打分文本的 token 集合（models 里的 ORM 事件与父行同事务维护），
    所以 COUNT 命中的 token 数与逐行 kw_score 的分子严格相等——探针给的是
    精确计数而非超集近似，探针行在 Python 侧直接采用这个数、不必重算。
    探针形状两方言一致（token IN + JOIN 父表 + GROUP BY 父 id +
    HAVING COUNT ≥ 门槛），靠 (token, id) 索引探查，不扫父表。
    查询里出现超长 token（> MAX_TOKEN_LEN，倒排不收）时探针不成立、返回
    None，调用方退全量精确打分——那种 token 只可能来自粘贴的 base64 之类。
    """
    if not plan.tokens or any(len(t) > MAX_TOKEN_LEN for t in plan.tokens):
        return None
    fk = getattr(token_model, id_key)
    # 两段式：先只在倒排表内按 (token, id) 索引数出过门 id，再按 id 回父表取列。
    # 一段式 JOIN 的计划不可控——SQLite 实测会选「按用户扫父表 5000 行、逐行
    # 回探倒排」，58ms；PG 规划器在统计不准时同样可能选错。两段式把贵的那段
    # 锁死在索引里（典型查询只碰几千个 posting 行），父表只剩主键点查。
    # 候选上限：过门行按命中数倒序只取前 _PROBE_CAP 条（见 _PROBE_CAP 注释）。
    # 没有上限时简报耗时与命中桶成正比：1M 行实测 top 查询桶 342150 → p50 7.9s、
    # mid 桶 164030 → p50 2.4s，离 50ms 上限差两个量级；k ≤ 20 时 2000 是 100 倍余量。
    hits = (select(fk.label("pid"), func.count().label("kw_hits"))
            .where(token_model.token.in_(plan.tokens))
            .group_by(fk)
            .having(func.count() >= _needed_count(plan))
            .order_by(func.count().desc())
            .limit(_PROBE_CAP)
            .subquery())
    return (select(*cols, hits.c.kw_hits)
            .select_from(hits.join(model, model.id == hits.c.pid))
            .where(*scope))


_PROBE_CAP = 2000  # 关键词探针的候选上限：过门行按命中数倒序截断。
# 与向量分支 200-cap 同类近似：过线行超过上限时，命中数更低的可能漏；
# 但简报 k ≤ 20，2000 是 100 倍余量，且截掉的是命中数最低的尾巴，
# 排序主因子（命中数）靠前的行不受影响。SQLite/PG 两方言都走这条探针。


_ANCHOR_CAP = 100  # PG 锚点探针：每锚采样数 = 累计到此即停的候选数。
# 20× 简报默认 k=5；简报只取 k 个胜出者，100 已是充分余量（全平分数据里
# 任取 100 个平分行与任取 500 个的胜出分数相同），且 stage-2 取行、验数、
# 打分都随 CAP 线性涨——CAP=200 时简报 p50 约 65ms，500 时约 103ms，
# 50ms 预算只容得下 100（找不够时回落精确探针，正确性不受 CAP 影响）。
# 锚点采样把探针成本从 O(命中桶) 压到 O(CAP)：1M 行实测，精确探针 top 查询
# 桶 342150 → p50 7.9s（加 2000-cap 后仍 1065ms，全花在 1M posting 扫描 +
# 24 万组 hash 聚合落盘上）。


def _anchor_probe(db, model, token_model, id_key: str, cols, scope, plan: "_QueryPlan"):
    """PG 锚点探针：返回 stage-2 行（带 kw_hits）；找不够 CAP 时返回 None（回落精确探针）。

    流程（全部有界，与命中桶大小无关）：
    1. 按查询词序逐个取锚：SELECT id FROM token表 WHERE token = 锚 LIMIT CAP
       ——(token, id) 索引上的纯索引扫描，暖时 <1ms；scope 不在这里过滤，
       留给 stage-2（与精确探针的 stage-2 同口径）；
    2. LATERAL 逐行验数：unnest(候选 id) × LATERAL(PK 索引按 id 切片数 token 命中)，
       命中数是跨全部查询 token 的精确计数（与 kw_score 分子同口径），
       HAVING 过门槛；规划器无法把它重写成全 token 扫描——ANY/JOIN 形会，
       1M 行实测 300ms+，LATERAL 形 500 行约 10ms；
    3. 累计过门行 ≥ CAP 即停；stage-2 按 id 回父表取列 + scope 过滤；
       stage-2 行 < CAP 说明采样没找够（针尖查询、跨 scope 稀释），返回 None
       让调用方回落精确探针——正确优先，慢只慢在罕见情形。
    近似性（与向量分支 200-cap 同类）：采样只取每锚前 CAP 条 posting
    （UUID 序≈随机样本），不含任一锚前 CAP 条的罕见高命中行可能漏；
    但锚点按查询词序全试一遍，且找不够时有精确回落兜底。
    SQLite 不走这里（小量级，精确探针足够快）；超长 token 时也不走（索引不成立）。
    """
    table = token_model.__tablename__
    ordered = ordered_tokens(plan.query)
    if not ordered or any(len(t) > MAX_TOKEN_LEN for t in ordered):
        return None
    needed = _needed_count(plan)
    verified: dict = {}
    for anchor in ordered:
        if len(verified) >= _ANCHOR_CAP:
            break
        ids = [r[0] for r in db.execute(
            text(f"SELECT {id_key} FROM {table} WHERE token = :a LIMIT :lim"),
            {"a": anchor, "lim": _ANCHOR_CAP}).fetchall()]
        fresh = [i for i in ids if i not in verified]
        if not fresh:
            continue
        # unnest 必须显式 CAST：psycopg3 传 list 时元素类型是 unknown，
        # PG 解不出 unnest(unknown)（AmbiguousFunction）；事实/经验 id 都是 String(32)，
        # 故统一 CAST AS text[]（若将来有整型 id 的 token 表，这里要跟着改）。
        rows = db.execute(
            text(f"SELECT c.id, s.hits FROM unnest(CAST(:ids AS text[])) AS c(id), LATERAL ("
                 f" SELECT COUNT(*) AS hits FROM {table}"
                 f" WHERE {id_key} = c.id AND token IN :toks) s"
                 f" WHERE s.hits >= :needed").bindparams(
                     bindparam("toks", expanding=True)),
            {"ids": fresh, "toks": tuple(plan.tokens), "needed": needed}).fetchall()
        for r in rows:
            verified[r[0]] = r[1]
    if len(verified) < _ANCHOR_CAP:
        return None
    top = sorted(verified.items(), key=lambda kv: kv[1], reverse=True)[:_ANCHOR_CAP]
    hits_by_id = dict(top)
    # stage-2 不用 VALUES 子查询拼 kw_hits 列：500 行 VALUES 的 SQLAlchemy
    # 编译要 76ms 且每次都重新编译（结构随数据变，缓存命中不了）。
    # 改用 id IN（postcompile 模板可缓存）+ Python 侧把精确命中数挂到
    # SimpleNamespace 上：下游只用属性访问（_score_rows/_rank/InsightOut），
    # 与精确探针的 Row 在消费口径上一致。
    stmt = select(*cols).where(model.id.in_([i for i, _ in top]), *scope)
    rows = [SimpleNamespace(**r._mapping, kw_hits=hits_by_id[r.id])
            for r in db.execute(stmt)]
    if len(rows) < _ANCHOR_CAP:
        return None
    return rows


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
    stmt = (select(*cols, _NULL_HITS).where(*scope, cond)
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


def _candidates(db: Session, model, token_model, id_key: str, cols, scope,
                plan: "_QueryPlan", qvec, embedder, with_pinned: bool):
    """候选通道装配（事实/经验共用）：返回 (rows, channel_complete) 或 None。

    通道完备（channel_complete=True）时 rows 就是候选全集：倒排探针（关键词
    过门）∪ 置顶臂（仅经验）∪ 向量臂（仅 PG 且维度对得上，HNSW kNN 形态）。
    通道不完备（SQLite 带向量、PG 维度对不上、超长 token）时返回全量行 +
    False，调用方照旧逐行精确打分，语义命中一条不丢。
    None = 通道完备但一条臂都没有（无 token 又无向量）：结果注定为空。
    多臂重复的行按 id 去重，带精确命中数（kw_hits）的探针行优先。
    """
    dialect = db.get_bind().dialect.name
    # 向量通道只在 Postgres 且查询向量维度与列一致时才成立（HNSW 索引按列维度建）
    indexed_vector = (dialect == "postgresql" and qvec is not None
                      and len(qvec) == settings.embedding_dim)
    if (qvec is None or indexed_vector) and dialect == "postgresql" and plan.tokens:
        # PG 锚点快路径：有界采样 + LATERAL 验数；找不够 CAP 时 _anchor_probe
        # 返回 None，本函数继续走精确探针（不改变原有语义分支）。
        anchor_rows = _anchor_probe(db, model, token_model, id_key, cols, scope, plan)
        if anchor_rows is not None:
            by_id: dict = {}
            for r in anchor_rows:
                by_id[r.id] = r
            anchor_parts = []
            if with_pinned:
                # 裸 pinned（不用 is_(True)）：部分索引 ix_insight_pinned_scope
                # 的谓词就是裸 pinned，PG 的谓词证明认不出 IS TRUE，导致
                # 1M 行实测从索引扫描 0.014ms 退化成并行全表扫描 210ms。
                anchor_parts.append(select(*cols, _NULL_HITS).where(
                    *scope, model.pinned))
            anchor_params: dict = {}
            if indexed_vector:
                branch, anchor_params = _vector_branch(
                    model, cols, scope, qvec, getattr(embedder, "model", None))
                anchor_parts.append(branch)
            for stmt in anchor_parts:
                for r in db.execute(stmt, anchor_params):
                    prev = by_id.get(r.id)
                    if prev is None or (prev.kw_hits is None and r.kw_hits is not None):
                        by_id[r.id] = r
            return list(by_id.values()), True
    probe = _probe_arm(model, token_model, id_key, cols, scope, plan)
    if (qvec is None or indexed_vector) and (probe is not None or not plan.tokens):
        parts = []
        if probe is not None:
            parts.append(probe)
        if with_pinned:
            # 置顶必达：单独一臂——置顶行可能与查询零重叠，探针捞不到它
            # 裸 pinned 而不用 is_(True)：同上，部分索引谓词匹配原因。
            parts.append(select(*cols, _NULL_HITS).where(
                *scope, model.pinned))
        params: dict = {}
        if indexed_vector:
            # 向量分支单独成 UNION 一支：与探针混在同一条语句里时规划器用不上
            # HNSW（其余分支无索引可用）；分开后距离分支自己走索引
            branch, params = _vector_branch(model, cols, scope, qvec,
                                            getattr(embedder, "model", None))
            parts.append(branch)
        if not parts:
            return None
        stmt = union_all(*parts) if len(parts) > 1 else parts[0]
        by_id: dict = {}
        for r in db.execute(stmt, params):
            prev = by_id.get(r.id)
            if prev is None or (prev.kw_hits is None and r.kw_hits is not None):
                by_id[r.id] = r
        return list(by_id.values()), True
    stmt = select(*cols, _NULL_HITS).where(*scope)
    return list(db.execute(stmt)), False


def _score_rows(rows, plan: "_QueryPlan", qvec, embedder_model,
                pinned_of, fields_of):
    """候选统一打分（事实/经验共用）：返回 [(final, reason, gated)]，与 rows 同序。

    门槛只看原始相关性（kw ≥ MIN_SCORE 或 cos ≥ COS_MIN），融合分 0.7·cos +
    0.3·kw 只决定排序（reason=hybrid）——2026-10-23 真实向量校准的口径：
    Gemini 余弦整体偏高，只看融合分会把不相关的条目大批放进来。
    kw 优先用探针带回的精确命中数（kw_hits，与 kw_score 分子同口径）；
    向量不可用（没存向量/模型或维度对不上，vector_usable 判）时退纯关键词。
    pinned_of(row) 决定「垫底必达」资格（事实侧恒 False）；fields_of(row)
    给 (subject, content[, applies]) 拼打分文本——仅 kw_hits 缺席时才拼。
    """
    out = []
    for row in rows:
        if row.kw_hits is not None:
            kw = row.kw_hits / len(plan.tokens)
        else:
            kw = plan.kw_score(fields_of(row))
        cos = None
        if qvec is not None and vector_usable(row.embed_model, row.embedding, qvec,
                                              embedder_model):
            cos = cosine(qvec, list(row.embedding))
        if cos is not None:
            gated = kw < MIN_SCORE and cos < COS_MIN
            final, reason = VECTOR_WEIGHT * cos + (1 - VECTOR_WEIGHT) * kw, "hybrid"
        else:
            gated = kw < MIN_SCORE
            final, reason = kw, "keyword"
        if gated and pinned_of(row):
            # 置顶必达：分数垫底入选，reason 标明来历，不冒充相关性命中
            final, reason = 0.0, "pinned"
        out.append((final, reason, gated))
    return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _rank_insights(rows, scored, now, project, conf_a: float = 0.5, conf_b: float = 0.9,
                   proj_same: float = 1.0, proj_diff: float = 0.85,
                   recency: bool = True, unverified_mult: float = 0.7):
    """经验排序乘子（第十/十一轮）：置信度 × 项目 × 新近性；置顶组恒在前。

    返回 [(final, row, reason)]，已按「置顶组内序 + 其余降序」排好待预算裁剪。
    乘子只影响排序不影响门槛（标定不被污染）；新近性按最后使用/生效时间算
    闲置天数，乘子区间 0.85–1.0——老经验不该被埋没，但新鲜的排前面。
    衰减按行首见顺序取整天数（与逐行现算同值），把逐行 datetime 相减
    换成字典查表：全员同龄的大库里这一项曾是逐行打分的近半开销。
    第十九轮：乘子全部参数化（默认值 = 现行口径，生产行为不变），供
    scripts/ranking_sensitivity.py 做敏感性分析——此前乘子是写死的，
    没人知道哪个在裸奔。
    第二十八轮：默认值改为联合标定值（scripts/ranking_calibration.py，
    合成基准验证集 NDCG@10 +0.0307，top5 0.560→0.760）：置信乘子
    0.5+0.9c（更陡）、同项目不再加分（1.0）、未验证折扣 0.7（更严）。
    注意：这是合成基准上的标定，不是真实反馈数据上的标定；真实反馈
    标定仍是未测量项，见 docs/eval.md。
    """
    ages: dict = {}
    ranked: list[tuple[float, object, str]] = []
    pinned_ranked: list[tuple[float, object, str]] = []
    for row, (final, reason, gated) in zip(rows, scored, strict=True):
        if gated and not row.pinned:
            continue
        conf = row.confidence if row.confidence is not None else 0.5
        final *= conf_a + conf_b * conf
        if getattr(row, "verified", True) is False:
            final *= unverified_mult  # 未验证经验（第十六轮）：证据没对上轨迹原文，排序打折
        if project:
            if row.project == project:
                final *= proj_same
            elif row.project:
                final *= proj_diff
        stamp = row.last_used_at or row.valid_from
        mult = ages.get(stamp)
        if mult is None:
            age_days = max(0, (now - _aware(stamp)).days)
            mult = ages[stamp] = (0.85 + 0.15 / (1 + age_days / 180)) if recency else 1.0
        final *= mult
        (pinned_ranked if row.pinned else ranked).append((final, row, reason))
    pinned_ranked.sort(key=lambda t: t[0], reverse=True)
    ranked.sort(key=lambda t: t[0], reverse=True)
    return pinned_ranked + ranked


def recall_facts(db: Session, u: UserContext, query: str, k: int, token_budget: int, embedder=None) -> list[FactOut]:
    k = max(1, int(k))  # 服务层兜底：负数/0 的 k 会让切片语义出错（MCP 等入口不走 schemas 校验）
    plan = _QueryPlan(query)
    qvec = _query_vector(embedder, query)
    # 无查询向量时不搬 embedding 列：SQLite 上它是 JSON，每行都要解码，纯属浪费
    cols = [m.MemoryFact.id, m.MemoryFact.subject, m.MemoryFact.content, m.MemoryFact.confidence]
    if qvec is not None:
        cols += [m.MemoryFact.embedding, m.MemoryFact.embed_model]
    scope = (m.MemoryFact.app_id == u.app_id,
             m.MemoryFact.user_id == u.user_id,
             m.MemoryFact.valid_to.is_(None))
    _ch = _candidates(db, m.MemoryFact, m.MemoryFactToken, "fact_id", cols, scope,
                      plan, qvec, embedder, with_pinned=False)
    if _ch is None:
        return []  # 无 token 又无向量：每条都会被门槛挡掉，结果为空
    rows, _ = _ch
    scored = _score_rows(rows, plan, qvec, getattr(embedder, "model", None),
                         pinned_of=lambda r: False,
                         fields_of=lambda r: f"{r.subject} {r.content}")
    # 佐证置信加权（第十七轮）：只动排序不动门槛——被反复证实的事实排前，
    # 乘子 0.85+0.3·confidence（0.5 中性先验时约 1.0，满佐证渐近 1.15）
    ranked = sorted(((f * (0.85 + 0.3 * (r.confidence if r.confidence is not None else 0.5)),
                      reason, gated, r)
                     for (f, reason, gated), r in zip(scored, rows, strict=True)),
                    key=lambda x: x[0], reverse=True)
    out, used = [], 0
    for s, reason, gated, r in ranked:
        if gated:
            continue
        if len(out) >= k:
            break
        cost = _est_tokens(r.content)
        # 第十八轮：与简报同口径，预算装包 break 改 skip——超预算条目跳过、
        # 后面更小的事实仍可递补，不再一超截断。
        if used + cost > token_budget:
            continue
        used += cost
        out.append(FactOut(fact_id=r.id, subject=r.subject, content=r.content, score=round(s, 4),
                           reason=reason,
                           confidence=r.confidence if r.confidence is not None else 0.5))
    # 扩散激活（第十五轮「织网」，与简报同口径）：名额没满时沿关联边带出相连事实，
    # reason='linked' 且 linked_from 标明来历，分数按边强度打折，不冒充直接命中。
    if len(out) < k:
        from app.links import spread_from

        have = {o.fact_id for o in out}
        spread = spread_from(db, u.app_id, u.user_id,
                             [("fact", o.fact_id, o.score) for o in out], "fact")
        cand_ids = [i for i in spread if i not in have]
        if cand_ids:
            extra = {r.id: r for r in db.scalars(select(m.MemoryFact).where(
                m.MemoryFact.id.in_(cand_ids),
                m.MemoryFact.app_id == u.app_id, m.MemoryFact.user_id == u.user_id,
                m.MemoryFact.valid_to.is_(None)))}
            for nid, (dscore, via) in sorted(spread.items(), key=lambda kv: kv[1][0],
                                             reverse=True):
                if len(out) >= k:
                    break
                r = extra.get(nid)
                if r is None:
                    continue
                cost = _est_tokens(r.content)
                if used + cost > token_budget:
                    continue
                used += cost
                out.append(FactOut(fact_id=r.id, subject=r.subject, content=r.content,
                                   score=round(dscore, 4), reason="linked", linked_from=via,
                                   confidence=r.confidence if r.confidence is not None else 0.5))
    return out

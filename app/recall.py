"""召回：检索前预过滤（用户 + valid_to IS NULL），再打分、裁剪。

打分两路：关键词 bigram 重叠（始终可用）+ 向量余弦（配了嵌入服务且该事实存有向量时）。
两者都在时按 0.7 向量 + 0.3 关键词融合（reason=hybrid），让字面不同但语义相同的内容也能召回；
嵌入不可用或调用失败时整体退回纯关键词，行为与旧版一致。

向量在 Postgres 上是 pgvector 列、SQLite 上是 JSON，融合在 Python 里算：个人记忆的量级下
可接受；数据量真正上来后，再把向量一路下推成 pgvector 的 SQL 排序。
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.embeddings import cosine
from app.schemas import FactOut

MIN_SCORE = 0.10  # 基于50条陪伴评测集校准的召回阈值，兼顾长Query召回率与防误召回
VECTOR_WEIGHT = 0.7  # 融合时向量占权；关键词只作辅助，避免字面命中压过语义相关


def _tokens(s: str) -> set[str]:
    """英文按词；中文按相邻两字（bigram）切，避免「咖啡」和「咖喱」因为共享一个字而命中。

    单个汉字的连续段（如查询只有一个字）退回按字。空白、标点不参与计分。
    """
    s = s.lower()
    out = set(re.findall(r"[a-z0-9]+", s))
    for run in re.findall(r"[一-鿿]+", s):
        out |= {run} if len(run) == 1 else {run[i:i + 2] for i in range(len(run) - 1)}
    return out


def _score(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
    return len(q & t) / (len(q) or 1)


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
    rows = db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.app_id == u.app_id,
        m.MemoryFact.user_id == u.user_id,
        m.MemoryFact.valid_to.is_(None),
    )).all()
    qvec = _query_vector(embedder, query)

    def _final(r) -> tuple[float, str]:
        kw = _score(query, f"{r.subject} {r.content}")
        emb = r.embedding
        if qvec is not None and emb is not None and len(emb) > 0:
            return VECTOR_WEIGHT * cosine(qvec, list(emb)) + (1 - VECTOR_WEIGHT) * kw, "hybrid"
        return kw, "keyword"

    scored = sorted(((*_final(r), r) for r in rows), key=lambda x: x[0], reverse=True)
    out, used = [], 0
    for s, reason, r in scored[:k]:
        if s < MIN_SCORE:
            break
        cost = max(1, len(r.content) // 2)  # 粗略 token 估算，后续换 tokenizer
        if used + cost > token_budget:
            break
        used += cost
        out.append(FactOut(fact_id=r.id, subject=r.subject, content=r.content, score=round(s, 4),
                           reason=reason))
    return out

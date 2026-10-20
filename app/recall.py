"""召回：检索前预过滤（用户 + valid_to IS NULL），再打分、裁剪。

v1 骨架先用关键词重叠打分；向量 + BM25 + RRF 在 Postgres 路径上实现后替换 _score。
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.schemas import FactOut


def _score(query: str, text: str) -> float:
    q = set(query.lower().split()) | set(query)
    t = set(text.lower().split()) | set(text)
    return len(q & t) / (len(q) or 1)


def recall_facts(db: Session, u: UserContext, query: str, k: int, token_budget: int) -> list[FactOut]:
    rows = db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.tenant_id == u.tenant_id,
        m.MemoryFact.user_id == u.user_id,
        m.MemoryFact.valid_to.is_(None),
    )).all()
    scored = sorted(((_score(query, r.content), r) for r in rows), key=lambda x: x[0], reverse=True)
    out, used = [], 0
    for s, r in scored[:k]:
        cost = max(1, len(r.content) // 2)  # 粗略 token 估算，后续换 tokenizer
        if used + cost > token_budget:
            break
        used += cost
        out.append(FactOut(fact_id=r.id, subject=r.subject, content=r.content, score=round(s, 4),
                           reason="keyword"))
    return out

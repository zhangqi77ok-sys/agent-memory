"""召回：检索前预过滤（用户 + valid_to IS NULL），再打分、裁剪。

v1 骨架先用关键词重叠打分；向量 + BM25 + RRF 在 Postgres 路径上实现后替换 _score。
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.schemas import FactOut

MIN_SCORE = 0.2  # 低于此分视为不相关，不返回（宁可少给，不要塞无关记忆进上下文）


def _tokens(s: str) -> set[str]:
    """英文按词、中文按字；去掉空白和标点，否则「的」「，」这类字符会让无关句子也有分。"""
    s = s.lower()
    words = {w for w in re.findall(r"[a-z0-9]+", s)}
    cjk = {c for c in s if "\u4e00" <= c <= "\u9fff"}
    return words | cjk


def _score(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
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
        if s < MIN_SCORE:
            break
        cost = max(1, len(r.content) // 2)  # 粗略 token 估算，后续换 tokenizer
        if used + cost > token_budget:
            break
        used += cost
        out.append(FactOut(fact_id=r.id, subject=r.subject, content=r.content, score=round(s, 4),
                           reason="keyword"))
    return out

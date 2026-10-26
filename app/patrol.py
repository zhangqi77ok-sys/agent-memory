"""巡检候选生成与巡检锁（修复第 10 轮，审计 F2/F3a）。

为什么巡检不再全配对：
- 旧实现每用户每轮把全部现行行两两配对，先算余弦再判主题（consolidate、
  weave），或 O(n²) 次 same_subject（sleep 事实合并）。审计实测 N=160 已
  6.25s，外推 N=5000 单轮约 1.9h，且巡检跑在任务处理之前、失败还 2s 热重试。
- 新实现只对“锚点候选对”算余弦/判主题：
  - 主题锚点是完备的：same_subject 为真 ⟹ 两主题规范化后相等、或包含
    子串、或 bigram Jaccard ≥ 0.5，三者都蕴含“共享 ≥1 个主题 bigram”
    （包含要求短侧 ≥2 字且为连续子串，其 bigram 全是长侧的 bigram；
    Jaccard ≥ 0.5 时交集非空；单字主题的 bigram 集就是 {字} 本身）。
    故主题 bigram 倒排 + 内容 token 倒排的并集包含全部 same_subject 真对，
    同主题分支的语义逐字不变。
  - 内容锚点是启发式的：跨主题高余弦对（consolidate ≥0.98、weave ≥0.88、
    抽象聚类 ≥0.90）可能零 token 重叠（纯换词/跨语言），那样的对本轮
    漏掉只意味着“未合并/未连边”，行与边都还在，不丢数据；这是与第十四轮
    倒排探针、第二十轮锚点探针同一哲学（有界近似、正确优先）。
- 候选对预算（PATROL_PAIR_BUDGET，默认 10 万/用户/函数）：防病态输入
  （全用户共享同一主题 bigram 且内容 token 也高度重合）把候选生成本身
  打回平方。超限时按 (id_a, id_b) 排序后取随 UTC 小时旋转的窗口，
  剩余对进 deferred 计数，靠多轮轮转覆盖——不是丢弃。
"""

import math
import time

from sqlalchemy import text

from app.extract import _bigrams, norm
from app.tokenize import tokens_of

PATROL_PAIR_BUDGET = 100_000  # 单用户单函数每轮最多比较的候选对数
PATROL_LOCK_KEY = "patrol"  # 全局巡检锁（_close_dead_links 与边衰减是跨用户全局步）


def subject_keys(subject: str | None) -> set[str]:
    """主题锚点：规范化主题的 bigram 集（与 same_subject 的判定口径同一来源）。"""
    na = norm(subject or "")
    if not na:
        return set()
    return set(_bigrams(na))


def content_keys(subject: str | None, content: str | None) -> set[str]:
    """内容锚点：主题 + 内容的 token 集（与召回倒排同一分词器）。"""
    return tokens_of(f"{subject or ''} {content or ''}")


def pair_window(pairs: list[tuple[int, int]], budget: int, now: float | None = None) -> tuple[list[tuple[int, int]], int]:
    """预算窗口：pairs 须已按 (i, j) 排序。预算内原样返回；超限时取随 UTC
    小时旋转的窗口，返回 (窗口对, deferred 数)。"""
    total = len(pairs)
    if total <= budget:
        return pairs, 0
    hour = int((time.time() if now is None else now) // 3600)
    start = (hour * budget) % total
    window = (pairs[start:] + pairs[:start])[:budget]
    return sorted(window), total - budget


def candidate_pairs(items, budget: int = PATROL_PAIR_BUDGET, now: float | None = None) -> tuple[list[tuple[int, int]], int]:
    """锚点候选对：items 须有 .subject/.content，返回 ((i, j) 序号对, deferred)。

    桶按规模升序展开（小众主题先行），pairset 达到 2×预算即停——候选生成
    本身也是有界的，不会被单个巨型桶打回平方。返回的序号对按 (i, j) 排序，
    与旧全配对循环的遍历顺序一致，小规模下行为逐字相同。"""
    buckets: dict[str, list[int]] = {}
    for idx, it in enumerate(items):
        for k in subject_keys(it.subject) | content_keys(it.subject, getattr(it, "content", None)):
            buckets.setdefault(k, []).append(idx)
    pairset: set[tuple[int, int]] = set()
    cap = budget * 2
    for idxs in sorted(buckets.values(), key=len):
        done = False
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                pairset.add((idxs[a], idxs[b]))
                if len(pairset) >= cap:
                    done = True
                    break
            if done:
                break
        if done:
            break
    pairs = sorted(pairset)
    return pair_window(pairs, budget, now=now)


def normalized_vecs(rows) -> dict:
    """每行一次归一化：{id: (embed_model, 单位向量或 None)}。

    缺向量/转 float 失败记 None；零向量保持零向量（点积为 0，与旧 cosine
    的 na==0 → 0.0 同值）。配对时只算点积，不再逐对重算范数。"""
    out = {}
    for r in rows:
        emb = getattr(r, "embedding", None)
        model = getattr(r, "embed_model", None)
        if emb is None:
            out[r.id] = (model, None)
            continue
        try:
            vec = [float(x) for x in emb]
        except (TypeError, ValueError):
            out[r.id] = (model, None)
            continue
        n = math.sqrt(sum(x * x for x in vec))
        out[r.id] = (model, vec if n == 0 else [x / n for x in vec])
    return out


def pair_cos(vecs: dict, a_id, b_id) -> float | None:
    """归一化向量间的余弦：模型不一致/缺向量/维度不一致返回 None（与旧
    _pair_cosine / _pair_cosine_rows 同口径）。"""
    ma, va = vecs[a_id]
    mb, vb = vecs[b_id]
    if va is None or vb is None or ma != mb or len(va) != len(vb):
        return None
    return sum(x * y for x, y in zip(va, vb, strict=True))


def try_patrol_lock(db) -> bool:
    """抢全局巡检咨询锁（PG 会话级，显式释放；见 release_patrol_lock）。

    抢到返回 True；另一副本持有时返回 False，调用方整轮跳过。
    SQLite 无咨询锁，与 _lock_user 同口径直接返回 True（单写者假设）。"""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return True
    row = db.execute(text("SELECT pg_try_advisory_lock(hashtext(:k)::bigint)"), {"k": PATROL_LOCK_KEY}).scalar()
    return bool(row)


def release_patrol_lock(db) -> None:
    """释放全局巡检咨询锁。非 PG 直接返回；释放失败静默（会话关闭时
    锁自动释放，不会死锁）。"""
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    try:
        db.execute(text("SELECT pg_advisory_unlock(hashtext(:k)::bigint)"), {"k": PATROL_LOCK_KEY})
    except Exception:
        pass

#!/usr/bin/env python3
"""排序乘子敏感性分析（第十九轮）：那一串乘子，到底谁在裸奔。

背景：简报排序 = 相关性 × 置信乘子（第二十八轮标定后 0.5+0.9c）× 项目乘子
（同项目 1.0 / 跨项目 0.85）× 新近性（0.85–1.0）× 未验证折扣（0.7）。每个单看都有道理，
但从没一起标定过。本脚本在固定合成语料上逐个拨动乘子，看 top-5 与全排序
抖动多大——抖动大的乘子就是标定优先级最高的。

语料：确定性伪随机 200 条经验（置信/项目/新近性/验证状态随机，相关性分数
固定），保证两次跑同一结果。指标：top-5 Jaccard（1.0 = 前五完全不动）、
全排序 Kendall tau（1.0 = 顺序完全不动）。

只做分析，不改阈值：抖动大不等于乘子错，只说明它值得拿真实反馈数据标定。

用法：python3 scripts/ranking_sensitivity.py
"""
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "sensitivity")


def _corpus(n=200, seed=7):
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    rows, scored = [], []
    for _i in range(n):
        rows.append(SimpleNamespace(
            pinned=False,
            confidence=round(rng.random(), 2),
            verified=rng.random() > 0.2,
            project=rng.choice(["p1", "p2", None]),
            last_used_at=None,
            valid_from=now - timedelta(days=rng.randrange(0, 365)),
        ))
        scored.append((rng.random(), "keyword", False))
    return rows, scored, now


def _order(rows, scored, now, **kw):
    from app.recall import _rank_insights
    return [r.subject if hasattr(r, "subject") else id(r)
            for _, r, _ in _rank_insights(rows, scored, now, "p1", **kw)]


def _jaccard_top5(a, b):
    sa, sb = set(a[:5]), set(b[:5])
    return round(len(sa & sb) / len(sa | sb), 3) if (sa | sb) else 1.0


def _kendall_tau(a, b):
    pos = {x: i for i, x in enumerate(b)}
    seq = [pos[x] for x in a]
    conc = disc = 0
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            if seq[i] < seq[j]:
                conc += 1
            else:
                disc += 1
    return round((conc - disc) / (conc + disc), 3) if (conc + disc) else 1.0


def main() -> int:
    rows, scored, now = _corpus()
    for i, r in enumerate(rows):
        r.subject = f"insight-{i}"
    base = _order(rows, scored, now)
    variants = {
        "置信乘子关掉 (0.5+0.9c -> 1.0)": {"conf_a": 1.0, "conf_b": 0.0},
        "置信乘子更陡 (0.5+0.9c -> 0.4+1.2c)": {"conf_a": 0.4, "conf_b": 1.2},
        "项目乘子关掉 (1.0/0.85 -> 1.0)": {"proj_same": 1.0, "proj_diff": 1.0},
        "新近性关掉": {"recency": False},
        "未验证折扣关掉 (0.7 -> 1.0)": {"unverified_mult": 1.0},
    }
    print(f"{'变体':<32} {'top5 Jaccard':>12} {'Kendall tau':>12}")
    for name, kw in variants.items():
        alt = _order(rows, scored, now, **kw)
        print(f"{name:<32} {_jaccard_top5(base, alt):>12} {_kendall_tau(base, alt):>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

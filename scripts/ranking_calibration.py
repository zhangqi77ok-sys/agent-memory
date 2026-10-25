#!/usr/bin/env python3
"""排序乘子联合标定（第二十八轮）：默认值到底是不是最优解。

背景：第十九轮把简报排序乘子参数化，只做了单乘子敏感性分析——置信
乘子关掉时 top-5 Jaccard 掉到 0.111，是主导杠杆，但各乘子从未一起
标定过。本脚本做坐标轮换标定，输出推荐值与采纳裁决。

诚实口径（必读）：仓库无真实轨迹与真实反馈标签，本脚本的标签语料是
合成基准，不是真实数据。隐藏价值分 = 公开的真实因子组合 + 确定性噪声；
真实因子故意偏离默认值（置信更陡、项目效应更小、新近性尺度更短），
标定若找不回偏离，方法本身就不合格。另设对照组：真实因子 = 现行默认
值，方法在对照组上必须“无幻觉提升”（验证集 NDCG 差距 < 0.01），否则
整个标定结论作废，脚本退出 1。

防过拟合：调参种子 1–5，验证种子 6–10。采纳规则：验证集 NDCG@10
提升 ≥ 0.01 且 top-5 命中率不下降，否则不动默认值，结论为“默认值已近优”。

回放场景（tests/test_replay_scenarios.py）只是回归护栏：k=5 且场景内
经验少，排序几乎不影响命中，不许把“回放仍 5/5”包装成标定证据。

第二十八轮后 DEFAULTS 已是标定值：重跑本脚本时主标定 verdict 应为
不动（即确认已采纳）；历史采纳证据（相对旧默认值的 +0.0307）见
docs/eval.md 与 tests/test_round28_calibration.py（OLD_DEFAULTS 对照）。

用法：python3 scripts/ranking_calibration.py
退出码：0 = 方法自检通过（采纳新默认或确认默认值）；1 = 自检失败。
"""
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "calibration")

DEFAULTS = {
    # 第二十八轮标定值（与 app/recall.py _rank_insights 默认值保持一致）。
    "conf_a": 0.5, "conf_b": 0.9,
    "proj_same": 1.0, "proj_diff": 0.85,
    "recency": True, "unverified_mult": 0.7,
}
GRID = {
    "conf_a": [0.5, 0.7, 0.9],
    "conf_b": [0.0, 0.3, 0.6, 0.9],
    "proj_same": [1.0, 1.15, 1.3],
    "proj_diff": [0.7, 0.85, 1.0],
    "recency": [True, False],
    "unverified_mult": [0.7, 0.85, 1.0],
}
# 真实因子：故意偏离默认值（见 docstring）；对照组用 DEFAULTS 本身。
TRUE_FACTORS = {
    "conf_a": 0.4, "conf_b": 1.2,
    "proj_same": 1.05, "proj_diff": 0.95,
    "recency_floor": 0.75, "recency_scale": 90.0,
    "unverified_mult": 0.75,
}
DEFAULT_FACTORS = {
    "conf_a": 0.5, "conf_b": 0.9,
    "proj_same": 1.0, "proj_diff": 0.85,
    "recency_floor": 0.85, "recency_scale": 180.0,
    "unverified_mult": 0.7,
}
TUNE_SEEDS = [1, 2, 3, 4, 5]
VALID_SEEDS = [6, 7, 8, 9, 10]
N_ROWS = 300
K = 10


def _corpus(seed, factors):
    """确定性语料：rows/scored 与按隐藏价值分算出的 utility 对齐返回。"""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    rows, scored, utility = [], [], []
    for i in range(N_ROWS):
        conf = round(rng.random(), 2)
        verified = rng.random() > 0.2
        project = rng.choice(["p1", "p2", None])
        age_days = rng.randrange(0, 365)
        relevance = rng.random()
        rows.append(SimpleNamespace(
            subject=f"insight-{i}", pinned=False, confidence=conf,
            verified=verified, project=project, last_used_at=None,
            valid_from=now - timedelta(days=age_days),
        ))
        scored.append((relevance, "keyword", False))
        u = relevance * (factors["conf_a"] + factors["conf_b"] * conf)
        if project == "p1":
            u *= factors["proj_same"]
        elif project:
            u *= factors["proj_diff"]
        u *= factors["recency_floor"] + (1 - factors["recency_floor"]) / (
            1 + age_days / factors["recency_scale"])
        if not verified:
            u *= factors["unverified_mult"]
        utility.append(max(0.0, u + rng.gauss(0, 0.05)))
    return rows, scored, now, utility


def _evaluate(params, seeds, factors):
    """给定乘子，在 seeds 上跑 _rank_insights，返回平均 (NDCG@10, top5 命中率)。"""
    from app.recall import _rank_insights

    ndcgs, hits = [], []
    for seed in seeds:
        rows, scored, now, utility = _corpus(seed, factors)
        order = [r for _, r, _ in _rank_insights(rows, scored, now, "p1", **params)]
        idx_of = {id(r): i for i, r in enumerate(rows)}
        ranked_utils = [utility[idx_of[id(r)]] for r in order]
        dcg = sum(g / math.log2(i + 2) for i, g in enumerate(ranked_utils[:K]))
        ideal = sorted(utility, reverse=True)[:K]
        idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
        ndcgs.append(dcg / idcg if idcg else 1.0)
        true_top5 = set(sorted(range(len(utility)),
                               key=lambda i: utility[i], reverse=True)[:5])
        got_top5 = {idx_of[id(r)] for r in order[:5]}
        hits.append(len(true_top5 & got_top5) / 5)
    return sum(ndcgs) / len(ndcgs), sum(hits) / len(hits)


def _coordinate_ascent(factors, seeds):
    params = dict(DEFAULTS)
    best, _ = _evaluate(params, seeds, factors)
    for _round in range(3):
        improved = False
        for key, grid in GRID.items():
            for value in grid:
                if value == params[key]:
                    continue
                trial = dict(params, **{key: value})
                score, _ = _evaluate(trial, seeds, factors)
                if score > best + 1e-9:
                    params, best, improved = trial, score, True
        if not improved:
            break
    return params, best


def main() -> int:
    print("== 对照组自检：真实因子 = 现行默认值，方法不应幻觉出提升 ==")
    ctrl_params, _ = _coordinate_ascent(DEFAULT_FACTORS, TUNE_SEEDS)
    ctrl_base = _evaluate(DEFAULTS, VALID_SEEDS, DEFAULT_FACTORS)
    ctrl_rec = _evaluate(ctrl_params, VALID_SEEDS, DEFAULT_FACTORS)
    print(f"对照组推荐值：{ctrl_params}")
    print(f"对照组验证集：默认 NDCG={ctrl_base[0]:.4f} top5={ctrl_base[1]:.3f} / "
          f"推荐 NDCG={ctrl_rec[0]:.4f} top5={ctrl_rec[1]:.3f} / "
          f"差距={ctrl_rec[0] - ctrl_base[0]:+.4f}")
    if abs(ctrl_rec[0] - ctrl_base[0]) >= 0.01:
        print("自检失败：对照组上方法幻觉出 ≥0.01 的提升，标定结论作废。")
        return 1

    print("== 主标定：真实因子偏离默认值 ==")
    rec_params, tune_score = _coordinate_ascent(TRUE_FACTORS, TUNE_SEEDS)
    base = _evaluate(DEFAULTS, VALID_SEEDS, TRUE_FACTORS)
    rec = _evaluate(rec_params, VALID_SEEDS, TRUE_FACTORS)
    print(f"调参集最优 NDCG={tune_score:.4f}")
    print(f"推荐值：{rec_params}")
    print(f"验证集：默认 NDCG={base[0]:.4f} top5={base[1]:.3f} / "
          f"推荐 NDCG={rec[0]:.4f} top5={rec[1]:.3f} / "
          f"提升={rec[0] - base[0]:+.4f}")
    if rec[0] - base[0] >= 0.01 and rec[1] >= base[1]:
        print("裁决：采纳推荐值（验证集提升 ≥0.01 且 top5 不下降）。")
    else:
        print("裁决：不动默认值（验证集提升不足或 top5 下降，默认值已近优）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

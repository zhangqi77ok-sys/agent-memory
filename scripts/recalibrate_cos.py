#!/usr/bin/env python3
"""COS_MIN 再校准（第十八轮）：只给建议，不改阈值。

背景：COS_MIN=0.66 是 2026-10-23 用 6 条经验 × 8 个任务标定的，命中对余弦
0.670–0.785、未命中对 0.488–0.656，间隔只有约 0.014——样本再扩大时阈值
可能要再动。审查 P2 要求生产环境再校准，但阈值是产品决策：这个脚本只算
分布、给建议值，改不改、改到多少由人定，不自动写回代码。

输入 JSONL（每行一个样本）：
  {"cos": 0.71, "label": "match"}        # 直接给余弦
  {"vec_a": [...], "vec_b": [...], "label": "match"}   # 或给向量对，现场算余弦
label 取 "match"（应命中）或 "nonmatch"（不应命中）。

输出：两类分布（个数/min/max/均值）、是否可分、可分时间隔中点建议、
重叠时 Youden J 最优点建议，以及当前 0.66 在这批样本上的表现
（误放/漏放数、间隔）。

用法：
  python scripts/recalibrate_cos.py --demo            # 确定性合成数据演示
  python scripts/recalibrate_cos.py --input pairs.jsonl
"""
import argparse
import json
import math
import sys

CURRENT_COS_MIN = 0.66


def cosine(a, b) -> float:
    dot = sum(float(x) * float(y) for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "min": None, "max": None, "mean": None}
    return {"n": len(values), "min": round(min(values), 4), "max": round(max(values), 4),
            "mean": round(sum(values) / len(values), 4)}


def suggest_threshold(pairs: list[tuple[float, bool]]) -> dict:
    """pairs: (cosine, is_match)。返回分布统计与建议阈值。"""
    match = sorted(c for c, m in pairs if m)
    non = sorted(c for c, m in pairs if not m)
    out = {"match": summarize(match), "nonmatch": summarize(non),
           "current": CURRENT_COS_MIN}
    if not match or not non:
        out.update(separable=None, suggested=None, note="两类样本缺一类，无法给建议")
        return out
    if max(non) < min(match):
        # 可分：间隔中点就是最稳的建议，离两边一样远
        out.update(separable=True, gap=round(min(match) - max(non), 4),
                   suggested=round((max(non) + min(match)) / 2, 4),
                   note="两类可分，建议取间隔中点")
    else:
        # 重叠：扫候选阈值，取 Youden J（真阳性率 - 假阳性率）最大者
        cands = sorted({round((a + b) / 2, 4)
                        for a, b in zip(sorted(c for c, _ in pairs)[:-1],
                                        sorted(c for c, _ in pairs)[1:], strict=True)})
        best_t, best_j = None, -2.0
        for t in cands:
            tpr = sum(1 for c in match if c >= t) / len(match)
            fpr = sum(1 for c in non if c >= t) / len(non)
            if (j := tpr - fpr) > best_j:
                best_t, best_j = t, j
        out.update(separable=False, suggested=best_t, youden_j=round(best_j, 4),
                   note="两类重叠，建议取 Youden J 最优点（仍需人工复核样本）")
    cur = CURRENT_COS_MIN
    out["current_eval"] = {
        "false_positive": sum(1 for c in non if c >= cur),
        "false_negative": sum(1 for c in match if c < cur),
    }
    return out


def load_pairs(path: str) -> list[tuple[float, bool]]:
    pairs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            label = row.get("label")
            if label not in ("match", "nonmatch"):
                raise ValueError(f"样本 label 非法：{label!r}（只接受 match/nonmatch）")
            if "cos" in row:
                cos = float(row["cos"])
            else:
                cos = cosine(row["vec_a"], row["vec_b"])
            pairs.append((cos, label == "match"))
    if not pairs:
        raise ValueError("输入为空")
    return pairs


def demo_pairs() -> list[tuple[float, bool]]:
    """确定性合成演示：命中簇 0.70–0.78、未命中簇 0.50–0.64，可分。"""
    return ([(0.70 + 0.01 * i, True) for i in range(9)]
            + [(0.50 + 0.02 * i, False) for i in range(8)])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="COS_MIN 再校准：只给建议，不改阈值")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", help="JSONL 样本文件")
    g.add_argument("--demo", action="store_true", help="用确定性合成数据演示")
    args = ap.parse_args(argv)
    try:
        pairs = demo_pairs() if args.demo else load_pairs(args.input)
        report = suggest_threshold(pairs)
    except (OSError, ValueError, KeyError) as e:
        print(f"输入错误：{e}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

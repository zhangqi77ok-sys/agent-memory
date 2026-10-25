#!/usr/bin/env python3
"""简报 A/B 度量（第十九轮建，第二十六轮补两臂与返工）：有简报 vs 无简报，
到底省了多少、有没有少走弯路。

两臂定义（写死，不含糊）：
- brief 臂：后续任务开始前调 brief_for_task，跟随者只读简报（k=5，
  token 预算 1500，与生产默认一致）。
- no-brief 臂：后续任务没有简报，跟随者只能通读原始轨迹渲染全文
  （_render_episode），即此前压缩比分母的那份文本。
- token 度量：两臂各自的输入体量（简报体量 vs 轨迹全文体量）与节省率。

返工度量（确定性模拟，假设写在明处，不许包装成实测）：
- 每个场景可带 pitfall 字段：当初踩过的那一步原文（如“在 8080 启动服务”）。
- 跟随规则：no-brief 臂的跟随者看不到提炼结论，必然重踩 pitfall
  （rework=1）；brief 臂当且仅当简报命中 must 串时跳过 pitfall（rework=0），
  未命中时同样重踩（rework=1）。
- 缺 pitfall 的场景不计返工（rework 字段为 null），只计 token 与命中。

默认跑 tests/test_replay_scenarios.py 的 5 个合成回放场景（有 gold 标签）。
真实任务度量：把真实轨迹按同样结构写成 JSONL（每行一个场景：
{"id", "title", "outcome", "steps": [{"action", "error"?}...],
 "cands": [{"kind", "subject", "content", "applies_when"}...],
 "task", "must", "pitfall"?}），用 --scenarios-file 传入；cands 就是你认可的
提炼结果，must 是后续任务简报里必须出现的关键串，pitfall 是当初踩过的
那一步（可选，缺失则该场景不计返工）。

诚实边界：本脚本只在临时 SQLite 库上跑，不碰生产库——brief_for_task 会
落曝光计数与交付记录，跑生产库会污染回流账本。合成场景的数字是下限参考，
真实任务的数字以你自己的 JSONL 为准；返工是模拟口径，不是真 agent 实测。

用法：
  python3 scripts/ab_eval.py
  python3 scripts/ab_eval.py --scenarios-file my_tasks.jsonl
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="ab-eval-"), "ab.db")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "ab-eval")


def _load_builtin():
    from tests.test_replay_scenarios import SCENARIOS, OneShotDistiller
    return SCENARIOS, OneShotDistiller


def _load_jsonl(path):
    from app.distill import InsightCandidate
    scs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["cands"] = [InsightCandidate(**c) for c in row["cands"]]
            scs.append(row)

    class OneShotDistiller:
        def __init__(self, cands):
            self._c = cands

        def distill(self, rendered: str):
            return list(self._c)

    return scs, OneShotDistiller


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="简报 A/B 度量：两臂 token + 返工")
    ap.add_argument("--scenarios-file", help="真实任务场景 JSONL（见脚本 docstring）")
    args = ap.parse_args(argv)

    from sqlalchemy.orm import sessionmaker

    from app.auth import UserContext
    from app.config import settings
    from app.db import Base, make_engine
    from app.episodes import _render_episode, brief_for_task, run_episode_once, submit_episode
    from app.recall import _est_tokens

    scenarios, distiller_cls = _load_jsonl(args.scenarios_file) if args.scenarios_file else _load_builtin()
    engine = make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    u = UserContext(app_id=settings.app_id, user_id="alice")

    results = []
    for sc in scenarios:
        db = Session()
        try:
            ep = submit_episode(db, u, source="agent", title=sc["title"],
                                outcome=sc["outcome"], steps=sc["steps"],
                                cost={"tokens": 2600, "retries": 2})
            run_episode_once(db, distiller_cls(sc["cands"]), embedder=None)
            # brief 臂：只读简报
            brief = brief_for_task(db, u, sc["task"], 5)
            joined = " ".join(i.content for i in brief)
            hit = sc["must"] in joined
            brief_tokens = sum(_est_tokens(i.content) + _est_tokens(i.applies_when) for i in brief)
            # no-brief 臂：只能通读轨迹全文
            no_brief_tokens = _est_tokens(_render_episode(ep))
            savings_pct = round(100 * (1 - brief_tokens / no_brief_tokens), 1) if no_brief_tokens else 0
            pitfall = sc.get("pitfall")
            if pitfall:
                rework_brief = 0 if hit else 1
                rework_no_brief = 1
            else:
                rework_brief = None
                rework_no_brief = None
            results.append({
                "id": sc["id"],
                "hit": hit,
                "brief_tokens": brief_tokens,
                "no_brief_tokens": no_brief_tokens,
                "savings_pct": savings_pct,
                "brief_n": len(brief),
                "pitfall": pitfall,
                "rework_brief": rework_brief,
                "rework_no_brief": rework_no_brief,
            })
        finally:
            db.close()

    hits = sum(1 for r in results if r["hit"])
    mean_sav = round(sum(r["savings_pct"] for r in results) / len(results), 1) if results else 0
    with_pitfall = [r for r in results if r["rework_brief"] is not None]
    rework_brief = sum(r["rework_brief"] for r in with_pitfall)
    rework_no_brief = sum(r["rework_no_brief"] for r in with_pitfall)
    report = {"scenarios": results,
              "hit_rate": f"{hits}/{len(results)}",
              "mean_savings_pct": mean_sav,
              "rework_brief": rework_brief,
              "rework_no_brief": rework_no_brief,
              "rework_avoided": rework_no_brief - rework_brief,
              "rework_scenarios": f"{len(with_pitfall)}/{len(results)}"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if hits == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

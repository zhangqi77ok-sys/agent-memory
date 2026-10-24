#!/usr/bin/env python3
"""简报 A/B 度量（第十九轮）：有简报 vs 无简报，到底省了多少、有没有漏。

度量两件事，对应系统的价值主张：
1) 命中率：后续任务的简报里有没有出现当初提炼出的关键经验（gold must 串）。
2) 压缩比：简报体量 vs 原始轨迹渲染体量——无简报时 agent 得通读整条轨迹。

默认跑 tests/test_replay_scenarios.py 的 5 个合成回放场景（有 gold 标签）。
真实任务度量：把真实轨迹按同样结构写成 JSONL（每行一个场景：
{"id", "title", "outcome", "steps": [{"action", "error"?}...],
 "cands": [{"kind", "subject", "content", "applies_when"}...],
 "task", "must"}），用 --scenarios-file 传入；cands 就是你认可的提炼结果，
must 是后续任务简报里必须出现的关键串。

诚实边界：本脚本只在临时 SQLite 库上跑，不碰生产库——brief_for_task 会
落曝光计数与交付记录，跑生产库会污染回流账本。合成场景的数字是下限参考，
真实任务的数字以你自己的 JSONL 为准。

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
    ap = argparse.ArgumentParser(description="简报 A/B 度量：命中率 + 压缩比")
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
            brief = brief_for_task(db, u, sc["task"], 5)
            joined = " ".join(i.content for i in brief)
            brief_tokens = sum(_est_tokens(i.content) + _est_tokens(i.applies_when) for i in brief)
            raw_tokens = _est_tokens(_render_episode(ep))
            results.append({
                "id": sc["id"],
                "hit": sc["must"] in joined,
                "brief_tokens": brief_tokens,
                "raw_tokens": raw_tokens,
                "savings_pct": round(100 * (1 - brief_tokens / raw_tokens), 1) if raw_tokens else 0,
                "brief_n": len(brief),
            })
        finally:
            db.close()

    hits = sum(1 for r in results if r["hit"])
    mean_sav = round(sum(r["savings_pct"] for r in results) / len(results), 1) if results else 0
    report = {"scenarios": results,
              "hit_rate": f"{hits}/{len(results)}",
              "mean_savings_pct": mean_sav}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if hits == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

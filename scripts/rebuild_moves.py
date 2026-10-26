#!/usr/bin/env python3
"""动作序列重整（第 33 轮类比召回）：按父表现状重算 memory_insight_move。

正常情况下动作序列由 ORM 事件与父行同事务维护，不需要这个脚本。
它兜三种情况：迁移 0026 后的存量回填、备份恢复后索引与父表对不上、
怀疑索引漂移时核查。--check 只报漂移行数不写库；缺省执行重整
（先删该父行的旧动作再按现状重插，与事件维护同一口径）。

用法：python3 scripts/rebuild_moves.py [--check]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select  # noqa: E402

from app.analogy import extract_moves  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import MemoryInsight, MemoryInsightMove, TaskEpisode  # noqa: E402


def _expected(db, row) -> list[str]:
    moves = extract_moves(f"{row.subject} {row.content} {row.applies_when}")
    if row.source_episode_id:
        ep = db.get(TaskEpisode, row.source_episode_id)
        if ep and ep.steps:
            actions = " ".join(str(st.get("action", "")) for st in ep.steps
                               if isinstance(st, dict))
            for mv in extract_moves(actions):
                if not moves or moves[-1] != mv:
                    moves.append(mv)
    return moves


def rebuild(db, check_only: bool) -> dict:
    drift = 0
    total = 0
    for row in db.scalars(select(MemoryInsight)):
        total += 1
        want = _expected(db, row)
        have = [r[0] for r in db.execute(
            select(MemoryInsightMove.move).where(
                MemoryInsightMove.insight_id == row.id
            ).order_by(MemoryInsightMove.position))]
        if have != want:
            drift += 1
            if not check_only:
                db.execute(delete(MemoryInsightMove).where(
                    MemoryInsightMove.insight_id == row.id))
                if want:
                    db.add_all([MemoryInsightMove(
                        insight_id=row.id, position=i, move=mv,
                        app_id=row.app_id, user_id=row.user_id)
                        for i, mv in enumerate(want)])
    if not check_only:
        db.commit()
    return {"rows": total, "drifted": drift}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        print(rebuild(db, args.check))
    finally:
        db.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""倒排索引重整（第十四轮补完）：按父表现状重算 memory_fact_token / memory_insight_token。

正常情况下倒排由 ORM 事件与父行同事务维护，不需要这个脚本。它兜三种情况：
备份恢复后索引与父表对不上、怀疑索引漂移时核查、迁移回填后抽查。
--check 只报漂移行数不写库；缺省执行重整（先删该父行的旧 token 再按现状重插，
与事件维护同一口径，走 scripts 时用的是管理连接，不经过事件以免逐行比对开销）。

用法：python3 scripts/rebuild_postings.py [--check]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import delete, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import MemoryFact, MemoryFactToken, MemoryInsight, MemoryInsightToken  # noqa: E402
from app.tokenize import MAX_TOKEN_LEN, tokens_of  # noqa: E402


def _expected(text: str) -> set[str]:
    return {t for t in tokens_of(text) if len(t) <= MAX_TOKEN_LEN}


def rebuild(db, check_only: bool) -> dict:
    stats = {}
    for parent, token_model, id_key, text_of in (
        (MemoryFact, MemoryFactToken, "fact_id", lambda r: f"{r.subject} {r.content}"),
        (MemoryInsight, MemoryInsightToken, "insight_id",
         lambda r: f"{r.subject} {r.content} {r.applies_when}"),
    ):
        fk = getattr(token_model, id_key)
        drift = 0
        total = 0
        for row in db.scalars(select(parent)):
            total += 1
            want = _expected(text_of(row))
            have = set(db.scalars(select(token_model.token).where(fk == row.id)))
            if have != want:
                drift += 1
                if not check_only:
                    db.execute(delete(token_model).where(fk == row.id))
                    if want:
                        db.add_all([token_model(**{id_key: row.id, "token": t,
                                                   "app_id": row.app_id, "user_id": row.user_id})
                                    for t in want])
        stats[parent.__tablename__] = {"rows": total, "drifted": drift}
    if not check_only:
        db.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报漂移行数，不写库")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        stats = rebuild(db, args.check)
    finally:
        db.close()
    for table, s in stats.items():
        print(f"{table}: rows={s['rows']} drifted={s['drifted']}"
              + (" (check only)" if args.check else " (rebuilt)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

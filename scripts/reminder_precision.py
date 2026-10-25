#!/usr/bin/env python3
"""前瞻记忆精度度量（第二十七轮）：武装的提醒撞上新任务，触得准不准。

背景：前瞻记忆（第十七轮）的触发一直没有精度数字——关键词路（条件 token
被任务覆盖 ≥ TRIGGER_OVERLAP_MIN 0.5）与向量路（余弦 ≥ COS_MIN 0.66）都
没跑过 precision/recall/false-trigger rate。本脚本补关键词路的度量。

度量方法：
- 标签集 tests/test_round27_reminders.py 的 REMINDER_CASES（14 组，条件 /
  动作 / 任务 / 应否触发，标签按意图定，不按分数凑）。
- 临时 SQLite 库上跑，不碰生产库——check_triggers 会落 fire_count 与
  fired 状态，跑生产库会污染提醒账本；提醒一律武装为常驻型（once=False），
  探针互不消耗。
- 逐任务调 check_triggers（embedder=None，纯关键词路），按命中的提醒条件
  与标签算 TP/FP/FN/TN、precision、recall、FPR，并做阈值扫掠（0.5/0.6/
  0.75/1.0，monkeypatch 模块常量；短条件 guard 在扫掠中固定不变）。

诚实边界：
- 只量关键词路：向量路需真实 embedder（离线量不了），换说法漏触与
  高重叠异意图误触是关键词路的固有短板，不靠调阈值修——扫掠表会把这点
  摊开给你看（阈值抬到 1.0 也救不回这两类，反而误杀真阳性）。
- 出厂口径（0.5 + 短条件 guard）下的数字：precision 5/6、recall 5/7、
  FPR 1/7；唯一 FP 是查看运行状态，唯一 FN 两例是换说法。

用法：
  python3 scripts/reminder_precision.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="reminder-precision-"), "test.db")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "reminder-precision")

import app.models  # noqa: F401  (建表需注册模型类)
import app.reminders as reminders
from app.auth import UserContext
from app.config import settings
from app.db import Base, SessionLocal, engine
from tests.test_round27_reminders import REMINDER_CASES, REMINDERS


def run_once(threshold):
    """一个阈值下的混淆矩阵（monkeypatch 模块常量，跑完还原；提醒由 main
    只建一次，常驻型探针互不消耗，扫掠不重复武装）。"""
    old = reminders.TRIGGER_OVERLAP_MIN
    reminders.TRIGGER_OVERLAP_MIN = threshold
    try:
        with SessionLocal() as db:
            u = UserContext(app_id=settings.app_id, user_id="alice")
            tp = fp = fn = tn = 0
            rows = []
            for cond, _action, task, want in REMINDER_CASES:
                fired = reminders.check_triggers(db, u, task, embedder=None)
                got = any(r.condition_text == cond for r in fired)
                rows.append((cond, task, want, got))
                if want and got:
                    tp += 1
                elif want and not got:
                    fn += 1
                elif not want and got:
                    fp += 1
                else:
                    tn += 1
        return (tp, fp, fn, tn), rows
    finally:
        reminders.TRIGGER_OVERLAP_MIN = old


def main():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        u = UserContext(app_id=settings.app_id, user_id="alice")
        for cond, action in REMINDERS:
            reminders.create_reminder(db, u, condition=cond, action=action,
                                      once=False)
    (tp, fp, fn, tn), rows = run_once(reminders.TRIGGER_OVERLAP_MIN)
    print(f"threshold={reminders.TRIGGER_OVERLAP_MIN} guard<=2token-full-match")
    print(f"TP={tp} FP={fp} FN={fn} TN={tn} "
          f"precision={tp / (tp + fp):.3f} recall={tp / (tp + fn):.3f} "
          f"FPR={fp / (fp + tn):.3f}")
    for cond, task, want, got in rows:
        mark = "OK  " if want == got else ("FP  " if got else "FN  ")
        print(f"{mark} want={int(want)} got={int(got)} cond={cond!r} task={task!r}")
    print("\nthreshold sweep (guard fixed):")
    for t in (0.5, 0.6, 0.75, 1.0):
        tp, fp, fn, tn = run_once(t)[0]
        print(f"  {t:.2f}: TP={tp} FP={fp} FN={fn} TN={tn} "
              f"precision={tp / (tp + fp):.3f} recall={tp / (tp + fn):.3f}")


if __name__ == "__main__":
    main()

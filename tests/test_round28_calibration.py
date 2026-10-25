"""第二十八轮：排序乘子联合标定。

钉住三件事：标定脚本确定性、推荐值落在网格内且与生产默认值一致、
对照组自检（真实因子 = 默认值时方法不幻觉提升）与主标定的验证集提升。
合成基准口径见 scripts/ranking_calibration.py docstring：这不是真实
反馈数据上的标定，真实反馈标定仍是未测量项。
"""
import importlib.util
import inspect
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.recall import _rank_insights

_HERE = os.path.dirname(__file__)
_SPEC = importlib.util.spec_from_file_location(
    "ranking_calibration",
    os.path.join(_HERE, "..", "scripts", "ranking_calibration.py"))
cal = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cal)

# 第二十八轮之前的生产默认值：历史采纳证据用它作“改动前”对照；
# 脚本里的 cal.DEFAULTS 已是标定后的新值，重跑主标定应 verdict 不动。
OLD_DEFAULTS = {
    "conf_a": 0.7, "conf_b": 0.6,
    "proj_same": 1.15, "proj_diff": 0.85,
    "recency": True, "unverified_mult": 0.85,
}


def test_calibration_deterministic():
    p1, _ = cal._coordinate_ascent(cal.TRUE_FACTORS, cal.TUNE_SEEDS)
    p2, _ = cal._coordinate_ascent(cal.TRUE_FACTORS, cal.TUNE_SEEDS)
    assert p1 == p2


def test_recommended_within_grid_and_matches_production_defaults():
    rec, _ = cal._coordinate_ascent(cal.TRUE_FACTORS, cal.TUNE_SEEDS)
    for key, grid in cal.GRID.items():
        assert rec[key] in grid, (key, rec[key])
    sig = inspect.signature(_rank_insights)
    for key, value in rec.items():
        assert sig.parameters[key].default == value, (key, value)


def test_control_self_check_no_hallucinated_gain():
    ctrl, _ = cal._coordinate_ascent(cal.DEFAULT_FACTORS, cal.TUNE_SEEDS)
    base = cal._evaluate(cal.DEFAULTS, cal.VALID_SEEDS, cal.DEFAULT_FACTORS)
    rec = cal._evaluate(ctrl, cal.VALID_SEEDS, cal.DEFAULT_FACTORS)
    assert abs(rec[0] - base[0]) < 0.01


def test_validation_gain_and_top5_not_worse():
    rec, _ = cal._coordinate_ascent(cal.TRUE_FACTORS, cal.TUNE_SEEDS)
    base = cal._evaluate(OLD_DEFAULTS, cal.VALID_SEEDS, cal.TRUE_FACTORS)
    got = cal._evaluate(rec, cal.VALID_SEEDS, cal.TRUE_FACTORS)
    assert got[0] - base[0] >= 0.01
    assert got[1] >= base[1]


def test_default_ranking_still_orders_clearly_better_first():
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(pinned=False, confidence=0.9, verified=True,
                        project="p1", last_used_at=None,
                        valid_from=now - timedelta(days=1)),
        SimpleNamespace(pinned=False, confidence=0.1, verified=False,
                        project="p2", last_used_at=None,
                        valid_from=now - timedelta(days=300)),
    ]
    scored = [(0.9, "keyword", False), (0.9, "keyword", False)]
    order = _rank_insights(rows, scored, now, "p1")
    assert order[0][1] is rows[0]

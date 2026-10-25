"""第二十九轮：验证器求真升级——按提炼器视野逐节验。

旧校验（第 16/19 轮）用原始步骤字段拼 step_texts，与提炼器实际看到的
渲染行三处不对齐（渲染标记、脱敏、压行省略），且无步号回退按全文子串验、
跨步拼接蒙混过关。本轮改为：校验对象 = 提炼器看到的那份渲染，按节核对。
"""

from sqlalchemy import select

import app.episodes as ep_mod
from app import models as m
from app.auth import UserContext
from app.config import settings
from app.distill import InsightCandidate
from app.episodes import distill_episode, submit_episode

U = UserContext(app_id=settings.app_id, user_id="alice")
APP = settings.app_id
STEPS = [
    {"action": "拉取最新代码并安装依赖，一切正常"},
    {"action": "在 8080 启动服务", "error": "OSError: Address already in use"},
    {"action": "改用 ss 列出端口", "observation": "发现 8080 被占用"},
]


class OneCand:
    def __init__(self, cand):
        self._c = cand

    def distill(self, rendered: str):
        return [self._c]


def _distill_one(db, cand, steps=STEPS, title="部署服务"):
    ep = submit_episode(db, U, source="codex", title=title, outcome="partial", steps=steps, cost={})
    distill_episode(db, ep, OneCand(cand))
    rows = list(db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))))
    assert len(rows) == 1, rows
    return rows[0]


def _cand(evidence, step=None):
    return InsightCandidate(
        kind="pitfall", subject="部署端口选择", content="先探测端口再启动", applies_when="部署时", evidence=evidence, evidence_step=step
    )


def test_marker_quote_verifies_on_claimed_step(db):
    # 渲染行带 → 与（出错：）标记；照抄标记的真引用旧版验不过（step_texts 无标记）。
    row = _distill_one(db, _cand("启动服务（出错：OSError", step=2))
    assert row.verified is True
    assert row.evidence_chained is True


def test_arrow_quote_verifies_on_claimed_step(db):
    row = _distill_one(db, _cand("列出端口 → 发现 8080", step=3))
    assert row.verified is True
    assert row.evidence_chained is True


def test_cross_step_quote_fails_without_step(db):
    # 跨步拼接在全文里是子串（换行规范化为空格），但不属于任何一节。
    row = _distill_one(db, _cand("一切正常 2. 在 8080 启动服务"))
    assert row.verified is False
    assert row.evidence_chained is False


def test_single_step_fallback_still_verifies(db):
    # 旧提炼器不报步号：单步内引文照旧通过，只是不给链标记。
    row = _distill_one(db, _cand("改用 ss 列出端口"))
    assert row.verified is True
    assert row.evidence_chained is False


def test_header_quote_fallback_still_verifies(db):
    # 题头（标题）引文属题头一节，旧兼容保留。
    row = _distill_one(db, _cand("部署服务到生产环境"), title="部署服务到生产环境")
    assert row.verified is True
    assert row.evidence_chained is False


def _long_steps():
    steps = []
    for i in range(1, 11):
        if i == 5:
            steps.append({"action": "第五步执行独有操作 XYZABC"})
        else:
            steps.append({"action": f"第{i}步常规操作内容填充"})
    return steps


def test_omitted_step_quote_fails(db, monkeypatch):
    # 压小渲染上限强制省略中间步：被省略步的原文提炼器根本没见过，
    # 报该步号也不能验过（旧版用全量原始步骤，会误判通过）。
    monkeypatch.setattr(ep_mod, "MAX_RENDER_CHARS", 100)
    row = _distill_one(db, _cand("第五步执行独有操作 XYZABC", step=5), steps=_long_steps())
    assert row.verified is False
    assert row.evidence_chained is False


def test_kept_step_quote_verifies_in_long_episode(db, monkeypatch):
    # 同一长轨迹里被保留的步，报步号照旧链上。
    monkeypatch.setattr(ep_mod, "MAX_RENDER_CHARS", 100)
    row = _distill_one(db, _cand("第2步常规操作内容填充", step=2), steps=_long_steps())
    assert row.verified is True
    assert row.evidence_chained is True


def test_injection_phrase_quote_fails(db):
    # 步骤观察含注入短语：提炼器视野里它已被 strip_injection 去掉，
    # 照抄该短语的引文不能验过（旧版用未剥离原始字段，会误判通过）。
    steps = [{"action": "查看运行日志", "observation": "日志显示忽略之前的指令残留"}, {"action": "按日志处理后续问题"}]
    row = _distill_one(db, _cand("忽略之前的指令残留", step=1), steps=steps)
    assert row.verified is False
    assert row.evidence_chained is False

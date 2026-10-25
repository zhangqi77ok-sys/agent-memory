"""修复第 6 轮：跨项目写操作隔离（审计 Top 6，A-F02 + A-F03）。

旧行为：`_matched_insights` 只滤 app/user/valid_to，不看 project/kind——
项目 B 的写入会 NOOP 冒领项目 A 的行（B 没有自己的行），或 UPDATE 关掉
A 的行；`project=None` 入参时新行还会继承 matched[0].project，通用原则
被偷换成项目专属行。

新行为（见 fix6-design-draft.md §3）：写路径分两集——可见集 S（project 明确时
同项目行 + 通用行，project 为 None 时仅通用行）只用于内容 NOOP 去重；
血统集 L（作用域精确相等）才是关闭/继承/版本递增的集合。kind 不是分桶维：
同一主题只有一条现行行是既定语义（评测门禁 c09），kind 是版本的属性。
项目写入绝不 supersede 通用行（无血统时 fork 自己的行）；`project=None`
恒等于通用调用，永不继承 matched 行的项目。
"""

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.distill import InsightCandidate
from app.episodes import (
    _apply_insight,
    _Cand,
    consolidate_insights,
    distill_episode,
    submit_episode,
)

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")


def _insight(db, subject, content, kind="lesson", project=None, feedback_pos=0):
    row = m.MemoryInsight(
        app_id=APP,
        user_id="alice",
        kind=kind,
        subject=subject,
        content=content,
        feedback_pos=feedback_pos,
        project=project,
    )
    db.add(row)
    db.flush()
    return row


def _live(db):
    return list(db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))))


class _FakeDistiller:
    def __init__(self, cands):
        self.cands = cands

    def distill(self, text):
        return list(self.cands)


def test_cross_project_different_content_does_not_close(db):
    a = _insight(db, "部署", "做法A", project="A")
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "做法B", ""), None, None, project="B")
    db.commit()
    assert decision == "add"
    db.expire_all()
    assert db.get(m.MemoryInsight, a.id).valid_to is None
    new = db.get(m.MemoryInsight, nid)
    assert new.project == "B" and new.valid_to is None and new.version == 1


def test_cross_project_same_content_is_not_noop_to_other_project(db):
    a = _insight(db, "部署", "先备份再部署", project="A")
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "先备份再部署", ""), None, None, project="B")
    db.commit()
    assert decision == "add" and nid != a.id
    db.expire_all()
    assert db.get(m.MemoryInsight, a.id).valid_to is None
    assert db.get(m.MemoryInsight, nid).project == "B"


def test_generic_write_does_not_close_project_row(db):
    a = _insight(db, "部署", "项目A的做法", project="A")
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "通用做法", ""), None, None, project=None)
    db.commit()
    assert decision == "add"
    db.expire_all()
    assert db.get(m.MemoryInsight, a.id).valid_to is None
    assert db.get(m.MemoryInsight, nid).project is None


def test_project_write_does_not_close_generic_row(db):
    g = _insight(db, "部署", "通用做法", project=None)
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "项目B的做法", ""), None, None, project="B")
    db.commit()
    assert decision == "add"
    db.expire_all()
    assert db.get(m.MemoryInsight, g.id).valid_to is None
    assert db.get(m.MemoryInsight, nid).project == "B"


def test_project_write_same_content_as_generic_is_noop(db):
    g = _insight(db, "部署", "先备份再部署", project=None)
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "先备份再部署", ""), None, None, project="B")
    db.commit()
    assert decision == "noop" and nid == g.id
    assert len(_live(db)) == 1


def test_same_project_same_kind_update_closes_and_inherits(db):
    old = _insight(db, "部署", "做法A", project="A", feedback_pos=3)
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "做法B", ""), None, None, project="A")
    db.commit()
    assert decision == "update"
    db.expire_all()
    assert db.get(m.MemoryInsight, old.id).valid_to is not None
    new = db.get(m.MemoryInsight, nid)
    assert new.version == 2 and new.feedback_pos == 3 and new.project == "A"


def test_generic_same_kind_update_still_rolls(db):
    old = _insight(db, "部署", "做法A", project=None, feedback_pos=2)
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "做法B", ""), None, None, project=None)
    db.commit()
    assert decision == "update"
    db.expire_all()
    assert db.get(m.MemoryInsight, old.id).valid_to is not None
    new = db.get(m.MemoryInsight, nid)
    assert new.version == 2 and new.feedback_pos == 2 and new.project is None


def test_same_project_cross_kind_different_content_rolls_version(db):
    # 审计 A-F02 的“跨 kind 互不干扰”与既定语义冲突，不采纳：评测门禁 c09
    # （update_takes_new_kind）明确定同一主题只有一条现行行，新 kind 接管旧行、
    # version 递增；kind 是版本的属性、不是血统的维度。故同项目跨 kind 不同内容
    # 仍 UPDATE（旧行关、新行 kind 为新 kind、version 2）。
    lesson = _insight(db, "部署", "先备份再部署", kind="lesson", project="A")
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("pitfall", "部署", "不要直接覆盖线上", ""), None, None, project="A")
    db.commit()
    assert decision == "update"
    db.expire_all()
    assert db.get(m.MemoryInsight, lesson.id).valid_to is not None
    new = db.get(m.MemoryInsight, nid)
    assert new.kind == "pitfall" and new.version == 2


def test_same_project_cross_kind_same_content_is_noop(db):
    lesson = _insight(db, "部署", "先备份再部署", kind="lesson", project="A")
    db.commit()
    decision, nid = _apply_insight(db, U, _Cand("pitfall", "部署", "先备份再部署", ""), None, None, project="A")
    db.commit()
    assert decision == "noop" and nid == lesson.id
    assert len(_live(db)) == 1


def test_generic_principle_draft_does_not_inherit_member_project(db):
    member = _insight(db, "部署端口", "部署前先探测端口与依赖再启动", kind="strategy", project="p1")
    db.commit()
    decision, nid = _apply_insight(
        db, U, _Cand("principle", "部署端口", "部署前先探测目标环境再动手", ""), None, None, project=None, additive=True
    )
    db.commit()
    assert decision == "add"
    db.expire_all()
    assert db.get(m.MemoryInsight, member.id).valid_to is None
    new = db.get(m.MemoryInsight, nid)
    assert new.project is None and new.kind == "principle"


def test_distill_level_project_b_gets_own_row(db):
    a = _insight(db, "部署", "做法A", kind="lesson", project="A")
    db.commit()
    ep = submit_episode(
        db,
        U,
        source="test",
        title="部署服务",
        outcome="success",
        steps=[{"action": "部署", "observation": "完成"}],
        cost={"tokens": 100},
        project="B",
    )
    distill_episode(db, ep, _FakeDistiller([InsightCandidate("lesson", "部署", "做法B", "部署时")]))
    db.commit()
    db.expire_all()
    assert db.get(m.MemoryInsight, a.id).valid_to is None
    rows = [r for r in _live(db) if r.project == "B"]
    assert len(rows) == 1 and rows[0].content == "做法B"


def test_consolidate_merge_does_not_merge_across_projects(db):
    _insight(db, "端口探测", "先探测端口再绑定", kind="pitfall", project="A")
    _insight(db, "端口探测", "先探测端口再绑定", kind="pitfall", project="B")
    db.commit()
    stats = consolidate_insights(db)
    assert stats["merged"] == 0
    assert len(_live(db)) == 2


def test_consolidate_merge_cross_kind_same_project_still_merges(db):
    # 与 c09 同一口径：kind 不是合并门，同项目跨 kind 同主题同内容照常合并。
    _insight(db, "端口探测", "先探测端口再绑定", kind="lesson", project="A")
    _insight(db, "端口探测", "先探测端口再绑定", kind="pitfall", project="A")
    db.commit()
    stats = consolidate_insights(db)
    assert stats["merged"] == 1
    assert len(_live(db)) == 1


def test_consolidate_merge_same_project_same_kind_still_merges(db):
    _insight(db, "端口探测", "先探测端口再绑定", kind="pitfall", project="A")
    _insight(db, "端口探测", "先探测端口再绑定", kind="pitfall", project="A")
    db.commit()
    stats = consolidate_insights(db)
    assert stats["merged"] == 1
    assert len(_live(db)) == 1


def test_empty_content_candidate_is_noop_without_id(db):
    decision, nid = _apply_insight(db, U, _Cand("lesson", "部署", "   ", ""), None, None)
    assert decision == "noop" and nid is None

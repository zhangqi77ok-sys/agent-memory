"""第九轮修复：审计 Top 9——向量路误触发。

三件改动（见 hidden_files/fix9-design-draft.md）：
1. 召回纯向量入选线 COS_MIN_PURE=0.78：kw < MIN_SCORE 的行需 cos ≥ 0.78
   才入选（审计复现 A：cos=0.77、kw=0 的行不得压过 kw=0.333 的真命中）。
2. 提醒触发线拆分 COS_MIN_REMINDER（初始 0.66，行为不变）：改提醒线
   不影响召回线。
3. 提醒两级触发：纯向量命中且 cos < REMINDER_VECTOR_CONFIDENT=0.80
   时，once 型提醒只展示、不落幕（灰区）；关键词命中（含双中）与确信
   向量命中仍按旧规则消耗。

固件：2026-10-26 重跑 scripts/reminder_vector_precision.py（真实
gemini-embedding-001）逐值复现第 31 轮的 14 探针逐案余弦；本文件用
DictEmbedder（条件向量 [1,0]、任务向量 [cos, √(1−cos²)]，精确复现逐案
余弦，零网络）把三臂矩阵钉进 CI。回放方式说明：每案单独武装本条件、
检查、解除——多条件同库时各条件向量都是 [1,0]，同一任务向量会同时撞
中所有条件，矩阵按“本条件是否触发”计，与第 31 轮脚本的逐案口径等价，
逐案隔离只是去掉交叉干扰，不改变矩阵。

假向量统一 2 维：PG 套件下 EMBEDDING_DIM=2，pgvector 列维度必须一致；
SQLite 下 JSON 列不限维，2 维同样通过。
"""

import math
from types import SimpleNamespace

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.recall import COS_MIN_PURE, MIN_SCORE, _QueryPlan, _score_rows
from app.reminders import (
    REMINDER_VECTOR_CONFIDENT,
    _trigger_detail,
    check_triggers,
    create_reminder,
    dismiss_reminder,
    list_reminders,
)
from app.tokenize import tokens_of

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")

# (condition, task, want, own-condition cosine)——2026-10-26 真实度量重跑值，
# 与第 31 轮逐值一致（见 docs/eval.md 第 31 轮一节 + fix9-design-draft §0）。
FIX9_MATRIX = [
    ("上线前", "上线前检查数据库迁移", True, 0.733),
    ("上线前", "今天上线，前端有问题", False, 0.769),
    ("上线前", "上线前通知测试团队", True, 0.775),
    ("部署服务时", "再部署一个新服务到这台机器", True, 0.781),
    ("部署服务时", "部署文档到内网", False, 0.718),
    ("部署服务时", "查看部署服务的运行状态", False, 0.757),
    ("数据库迁移回滚时", "数据库迁移失败，准备回滚", True, 0.827),
    ("数据库迁移回滚时", "回滚前端静态资源部署", False, 0.705),
    ("调用外部 API 批量处理时", "批量调用外部 API 处理新数据", True, 0.845),
    ("调用外部 API 批量处理时", "调用内部接口查询用户信息", False, 0.667),
    ("打印请求日志做调试时", "排查调用失败，准备打印请求细节", True, 0.805),
    ("打印请求日志做调试时", "打印月度报表给财务", False, 0.634),
    ("推送代码到 GitHub 时", "把分支推送到 GitHub", True, 0.777),
    ("推送代码到 GitHub 时", "从 GitHub 拉取代通知", False, 0.720),
]


class DictEmbedder:
    """按文本查表的假嵌入器：embed 返回预设向量，与 task_type 无关。"""

    def __init__(self, mapping, model="fake-embed-1"):
        self.mapping = mapping
        self.model = model

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [list(self.mapping[t]) for t in texts]


def _vec(cos):
    return [cos, math.sqrt(1 - cos * cos)]


def _confusion(rows):
    tp = fp = fn = tn = 0
    for _cond, _task, want, got in rows:
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
        elif not want and got:
            fp += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def test_matrix_three_arms_pinned(db):
    """固件回归（审计 A8）：三臂矩阵钉死，任一阈值/guard/权重/模型改动
    使矩阵漂移即失败。向量臂用 _trigger_detail + 空 task_tokens 隔离
    关键词干扰；联合臂走真实 check_triggers（once=False，探针互不消耗）；
    关键词臂走 check_triggers(embedder=None)，复核第 27 轮口径未漂移。"""
    vec_rows, comb_rows, kw_rows = [], [], []
    for cond, task, want, cos in FIX9_MATRIX:
        # 向量臂（内存行，不进库）：空 task_tokens → 纯向量判定。
        row = m.MemoryReminder(
            app_id=APP, user_id="alice", condition_text=cond, action_text="x", embedding=[1.0, 0.0], embed_model="fake-embed-1"
        )
        detail = _trigger_detail(row, set(), _vec(cos), "fake-embed-1")
        vec_rows.append((cond, task, want, detail is not None))
        # 联合臂 + 关键词臂：逐案武装本条件、检查、解除，避免多条件交叉触发。
        emb = DictEmbedder({cond: [1.0, 0.0], task: _vec(cos)})
        armed = create_reminder(db, U, condition=cond, action="x", once=False, embedder=emb)
        fired = check_triggers(db, U, task, embedder=emb)
        comb_rows.append((cond, task, want, any(r.condition_text == cond for r in fired)))
        fired = check_triggers(db, U, task, embedder=None)
        kw_rows.append((cond, task, want, any(r.condition_text == cond for r in fired)))
        dismiss_reminder(db, U, armed.id)
    assert _confusion(vec_rows) == (7, 6, 0, 1)
    assert _confusion(comb_rows) == (7, 6, 0, 1)
    assert _confusion(kw_rows) == (5, 1, 2, 6)


def test_pure_vector_line_blocks_audit_repro_a():
    """审计复现 A：cos=0.77、kw=0 的行不得入选、不得压过 kw=0.333 的真命中；
    有关键词佐证（kw ≥ MIN_SCORE）的向量行仍走 COS_MIN，不受纯向量线影响；
    纯向量 cos=0.79（线上）仍入选。"""
    plan = _QueryPlan("部署服务时端口被占用怎么处理")
    qvec = [1.0, 0.0]

    def _row(kw_hits, embedding):
        return SimpleNamespace(kw_hits=kw_hits, embedding=embedding, embed_model="fake-embed-1", pinned=False)

    # V：零关键词命中、cos=0.77 → 门内挡掉（旧口径下以 0.539 入选并压过 K）。
    scored = _score_rows([_row(0, _vec(0.77))], plan, qvec, "fake-embed-1", pinned_of=lambda r: False, fields_of=lambda r: "")
    assert scored[0][2] is True
    # K：关键词命中 4/13、无向量 → 入选（审计复现 A 记 12 token，
    # 实测分词 13 个，门结论相同：V 被挡、K 入选）。
    scored = _score_rows([_row(4, None)], plan, qvec, "fake-embed-1", pinned_of=lambda r: False, fields_of=lambda r: "")
    assert scored[0][2] is False
    assert abs(scored[0][0] - 4 / len(plan.tokens)) < 1e-9
    # 有佐证的向量行：kw=0.4、cos=0.77 → 仍入选（走 COS_MIN，不走纯向量线）。
    plan2 = _QueryPlan("端口占用处理")
    scored = _score_rows([_row(2, _vec(0.77))], plan2, qvec, "fake-embed-1", pinned_of=lambda r: False, fields_of=lambda r: "")
    assert 2 / len(plan2.tokens) >= MIN_SCORE
    assert scored[0][2] is False
    # 纯向量线上：cos=0.79 → 入选。
    scored = _score_rows([_row(0, _vec(0.79))], plan, qvec, "fake-embed-1", pinned_of=lambda r: False, fields_of=lambda r: "")
    assert scored[0][2] is False
    assert COS_MIN_PURE == 0.78


def test_reminder_line_independent_of_recall_line(monkeypatch):
    """审计 A6：改提醒线不影响召回线。提醒线抬到 0.90 后，0.77 的纯向量
    提醒不再触发；召回侧有佐证的行（kw ≥ MIN_SCORE、cos=0.77）仍入选，
    且 COS_MIN 本身仍是 0.66。"""
    import app.recall as recall_mod
    import app.reminders as reminders_mod

    monkeypatch.setattr(reminders_mod, "COS_MIN_REMINDER", 0.90)
    row = m.MemoryReminder(
        app_id=APP, user_id="alice", condition_text="上线前", action_text="x", embedding=[1.0, 0.0], embed_model="fake-embed-1"
    )
    assert _trigger_detail(row, set(), _vec(0.77), "fake-embed-1") is None
    plan = _QueryPlan("端口占用处理")
    scored = _score_rows(
        [SimpleNamespace(kw_hits=2, embedding=_vec(0.77), embed_model="fake-embed-1", pinned=False)],
        plan,
        [1.0, 0.0],
        "fake-embed-1",
        pinned_of=lambda r: False,
        fields_of=lambda r: "",
    )
    assert scored[0][2] is False
    assert recall_mod.COS_MIN == 0.66 and recall_mod.COS_MIN_REMINDER == 0.66


def test_once_gray_zone_shown_not_consumed(db):
    """灰区机制（审计 A2 二选一之二）：once 型提醒被纯向量灰区命中
    （0.769 < 0.80）时返回展示、fire_count+1，但 status 保持 armed，
    下次同类任务仍可再展示——意图不丢失；确信命中（0.85）则落幕。"""
    cond = "上线前"
    # 灰区任务：关键词覆盖 0.5 < guard 要求的 1.0（关键词路不中），
    # 向量 0.769 ≥ 提醒线但 < 确信线 → 纯向量灰区命中。
    task_gray = "今天上线，前端有问题"
    assert len(tokens_of(cond)) <= 2
    assert (
        _trigger_detail(
            m.MemoryReminder(app_id=APP, user_id="alice", condition_text=cond, action_text="x"), tokens_of(task_gray), None, None
        )
        is None
    )
    emb = DictEmbedder({cond: [1.0, 0.0], task_gray: _vec(0.769)})
    armed = create_reminder(db, U, condition=cond, action="检查数据库迁移", once=True, embedder=emb)
    fired = check_triggers(db, U, task_gray, embedder=emb)
    assert any(r.condition_text == cond for r in fired)
    again = list_reminders(db, U, include_closed=True)
    kept = [r for r in again if r.id == armed.id][0]
    assert kept.status == "armed" and kept.fire_count == 1
    # 未丢失：下次同类任务仍展示。
    fired = check_triggers(db, U, task_gray, embedder=emb)
    assert any(r.condition_text == cond for r in fired)
    dismiss_reminder(db, U, armed.id)
    # 确信命中：向量 0.85 ≥ 确信线 → 落幕，之后不再触发。任务特意选与
    # 条件零关键词重叠（“月度报表”与“上线前”无共享 bigram），测的是
    # 纯向量确信，不是双中。
    task_sure = "月度报表"
    assert not (tokens_of(cond) & tokens_of(task_sure))
    emb2 = DictEmbedder({cond: [1.0, 0.0], task_sure: _vec(0.85)})
    armed2 = create_reminder(db, U, condition=cond, action="检查数据库迁移", once=True, embedder=emb2)
    fired = check_triggers(db, U, task_sure, embedder=emb2)
    assert any(r.condition_text == cond for r in fired)
    done = [r for r in list_reminders(db, U, include_closed=True) if r.id == armed2.id][0]
    assert done.status == "fired"
    assert check_triggers(db, U, task_sure, embedder=emb2) == []


def test_once_keyword_hit_still_consumes(db):
    """关键词佐证即确信：关键词命中（含双中）不受确信线影响，once 型照常落幕。
    任务“上线前检查”关键词全中（1.0），向量即使只有 0.5（未过提醒线）也落幕；
    向量 0.77 双中时同样落幕（不是灰区）。"""
    cond = "上线前"
    task = "上线前检查"
    assert tokens_of(cond) & tokens_of(task)
    for task_cos in (0.50, 0.77):
        emb = DictEmbedder({cond: [1.0, 0.0], task: _vec(task_cos)})
        armed = create_reminder(db, U, condition=cond, action="检查数据库迁移", once=True, embedder=emb)
        fired = check_triggers(db, U, task, embedder=emb)
        assert any(r.condition_text == cond for r in fired)
        done = [r for r in list_reminders(db, U, include_closed=True) if r.id == armed.id][0]
        assert done.status == "fired"
        assert check_triggers(db, U, task, embedder=emb) == []


def test_gray_zone_does_not_touch_recurring(db):
    """常驻型提醒本来就不落幕：灰区命中只记 fire_count，status 保持 armed，
    与旧口径一致（本轮不改变常驻型语义）。"""
    cond = "上线前"
    task = "今天上线，前端有问题"
    emb = DictEmbedder({cond: [1.0, 0.0], task: _vec(0.769)})
    armed = create_reminder(db, U, condition=cond, action="检查数据库迁移", once=False, embedder=emb)
    fired = check_triggers(db, U, task, embedder=emb)
    assert any(r.condition_text == cond for r in fired)
    kept = [r for r in list_reminders(db, U, include_closed=True) if r.id == armed.id][0]
    assert kept.status == "armed" and kept.fire_count == 1
    assert REMINDER_VECTOR_CONFIDENT == 0.80

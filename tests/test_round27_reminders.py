"""第二十七轮：前瞻记忆误触发率度量与短条件 guard。

REMINDER_CASES 是带标签的探针集（条件 / 动作 / 任务 / 应否触发），关键词路
（embedder=None）下的 ground truth。标签按意图定，不按分数凑：
- “查看部署服务的运行状态”标 False：监控已部署的服务 ≠ 部署服务，关键词
  路仍会触（0.75）——这是关键词无解的一类，留给向量路，不是本轮的修复目标。
- “打印请求细节”“推送到 GitHub”标 True：换说法漏触同样是关键词路的固有
  短板（0.33），不靠降阈值修。
- “今天上线，前端有问题”标 False：短条件半中误触，是本轮 guard 修的那类。

脚本 scripts/reminder_precision.py 从这里导入同一组 case（与 ab_eval 导入
回放场景同方向），保证度量数字与测试钉的是同一批标签。
"""

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.reminders import (
    SHORT_COND_TOKENS,
    TRIGGER_OVERLAP_MIN,
    _trigger_score,
    check_triggers,
    create_reminder,
)

APP = settings.app_id
U = UserContext(app_id=APP, user_id="alice")

# (condition, action, task, should_fire)
REMINDER_CASES = [
    ("上线前", "检查数据库迁移", "上线前检查数据库迁移", True),
    ("上线前", "检查数据库迁移", "今天上线，前端有问题", False),
    ("上线前", "检查数据库迁移", "上线前通知测试团队", True),
    ("部署服务时", "部署前先探测端口占用", "再部署一个新服务到这台机器", True),
    ("部署服务时", "部署前先探测端口占用", "部署文档到内网", False),
    ("部署服务时", "部署前先探测端口占用", "查看部署服务的运行状态", False),
    ("数据库迁移回滚时", "确认回滚方案再执行", "数据库迁移失败，准备回滚", True),
    ("数据库迁移回滚时", "确认回滚方案再执行", "回滚前端静态资源部署", False),
    ("调用外部 API 批量处理时", "按 Retry-After 退避", "批量调用外部 API 处理新数据", True),
    ("调用外部 API 批量处理时", "按 Retry-After 退避", "调用内部接口查询用户信息", False),
    ("打印请求日志做调试时", "打印前先脱敏", "排查调用失败，准备打印请求细节", True),
    ("打印请求日志做调试时", "打印前先脱敏", "打印月度报表给财务", False),
    ("推送代码到 GitHub 时", "走 SSH 443 推送", "把分支推送到 GitHub", True),
    ("推送代码到 GitHub 时", "走 SSH 443 推送", "从 GitHub 拉取代通知", False),
]

REMINDERS = [
    ("上线前", "检查数据库迁移"),
    ("部署服务时", "部署前先探测端口占用"),
    ("数据库迁移回滚时", "确认回滚方案再执行"),
    ("调用外部 API 批量处理时", "按 Retry-After 退避"),
    ("打印请求日志做调试时", "打印前先脱敏"),
    ("推送代码到 GitHub 时", "走 SSH 443 推送"),
]


def _row(condition):
    row = m.MemoryReminder(app_id=APP, user_id="alice", condition_text=condition,
                           action_text="x")
    return row


def test_short_condition_guard_boundary():
    from app.tokenize import tokens_of
    # 2 token 条件：半中（0.5）不触，全中（1.0）触
    assert _trigger_score(_row("上线前"), tokens_of("今天上线，前端有问题"), None, None) is None
    assert _trigger_score(_row("上线前"), tokens_of("上线前检查"), None, None) == 1.0
    # 3 token 条件不受 guard 影响：2/3 在 0.5 下照常触
    assert _trigger_score(_row("上线时段"), tokens_of("上线时间"), None, None) is not None
    assert SHORT_COND_TOKENS == 2 and TRIGGER_OVERLAP_MIN == 0.5


def test_precision_recall_at_shipped_threshold(db):
    """端到端：常驻型提醒（once=False，探针互不消耗）全武装，逐任务撞。
    出厂口径下的度量：precision 5/6、recall 5/7、FPR 1/7——唯一 FP 是
    查看运行状态（关键词无解类），唯一 FN 两例是换说法类。"""
    for cond, action in REMINDERS:
        create_reminder(db, U, condition=cond, action=action, once=False)
    tp = fp = fn = tn = 0
    for cond, _action, task, want in REMINDER_CASES:
        fired = check_triggers(db, U, task, embedder=None)
        got = any(r.condition_text == cond for r in fired)
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
        elif not want and got:
            fp += 1
        else:
            tn += 1
    assert (tp, fp, fn, tn) == (5, 1, 2, 6)
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    fpr = fp / (fp + tn)
    assert abs(precision - 5 / 6) < 1e-9
    assert abs(recall - 5 / 7) < 1e-9
    assert abs(fpr - 1 / 7) < 1e-9


def test_known_keyword_gaps_documented(db):
    """三例已知缺口，现状即如此（不是回归，改了这里要同步改度量报告）：
    查看运行状态误触（高重叠异意图）；打印请求细节、推送到 GitHub 漏触
    （换说法）。向量路是它们的正解，关键词路修不了。"""
    for cond, action in REMINDERS:
        create_reminder(db, U, condition=cond, action=action, once=False)
    fired = check_triggers(db, U, "查看部署服务的运行状态", embedder=None)
    assert any(r.condition_text == "部署服务时" for r in fired)
    fired = check_triggers(db, U, "排查调用失败，准备打印请求细节", embedder=None)
    assert not any(r.condition_text == "打印请求日志做调试时" for r in fired)
    fired = check_triggers(db, U, "把分支推送到 GitHub", embedder=None)
    assert not any(r.condition_text == "推送代码到 GitHub 时" for r in fired)

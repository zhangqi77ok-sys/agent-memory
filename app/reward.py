"""奖励分（第 35 轮）：outcome 维度的巩固优先级，派生分，不存表。

只读五个已有计数器，不写任何字段——因此没有写路径延迟、没有
墓碑/遗忘 bookkeeping（行关闭后睡眠各路本来就按现行行过滤，
分自然读不到）；睡眠巩固永远不写这五个计数器，自生成行出生
时五项全 0，只能靠之后的真实反馈挣分，自证循环在构造上断掉。

权重论证：
- 人工反馈（feedback_pos/neg）：权重 1。逐行精确归因，REST 与
  MCP 两路入口都过 memory_enabled 同意闸。
- 人工上报的任务成功（success_count）：权重 1。与好评是两个
  独立信号——record_feedback 的 helpful 与 task_outcome 独立
  传入，一条经验可以在失败任务里帮上忙，也可以在成功任务里
  没帮上忙；两者各计一次不是同一事件计两次。
- 自动回流（auto_pos/neg）：权重 0.5。双重理由：第 16 轮置信度
  公式已确立自动信号半权先例；且 backflow 归因是粗的（整份
  交付的所有行同记一次），半权是对粗归因的折扣。

上限 ±10：超过 10 个净信号不再加权；刷反馈的效果本就被置信
度的拉普拉斯平滑压住，奖励上限是第二道 bound。

奖励分永不写入 confidence 列——置信度仍归反馈公式管；奖励
只管巩固队列顺序与衰减速度。同一信号出现在两个公式里不算
重复计算（重复计算是指同一公式内一事件计两次）；跨公式复用
已有先例：feedback_pos 今天同时进置信度公式与衰减复述计数。
"""

REWARD_CAP = 10.0  # 净信号超过这么多不再加权（双向对称）


def reward_from_counts(pos=0, neg=0, succ=0, auto_pos=0, auto_neg=0) -> float:
    """五项计数 → 奖励分（O(1)，None 安全）。"""
    raw = (pos or 0) + (succ or 0) + 0.5 * (auto_pos or 0) \
        - (neg or 0) - 0.5 * (auto_neg or 0)
    return max(-REWARD_CAP, min(REWARD_CAP, raw))


def reward_score(row) -> float:
    """经验行（ORM 对象）→ 奖励分。列查询场景由调用方直接调
    reward_from_counts，不走这里。"""
    return reward_from_counts(
        getattr(row, "feedback_pos", 0),
        getattr(row, "feedback_neg", 0),
        getattr(row, "success_count", 0),
        getattr(row, "auto_pos", 0),
        getattr(row, "auto_neg", 0),
    )

# 第二十三轮设计：事实级反馈 API——实施中

## 缺口

经验有反馈闭环（第十轮 record_feedback + POST /v1/insights/feedback +
MCP memory_feedback），事实与偏好没有：直接 memory_write 的用户写下
的事实，之后无法回报“这条到底准不准”——事实置信度只靠写入时佐证
计数单向累积，负信号无处可去。

## 置信度公式（关键抉择）

事实现有公式（第十七轮）：confidence = support/(support+1)，support =
独立佐证次数。反馈不能另起一套公式，否则两处口径打架；也不能只加
正信号（负反馈没地方记）。

采用拉普拉斯推广：confidence = (S+P)/(S+P+N+1)，其中 S = support_count、
P = feedback_pos、N = feedback_neg。

- 无反馈时恒等于旧公式：(S+0)/(S+0+0+1) = S/(S+1)——旧行数值不动，
  第十七轮测试不受影响。
- 好评 = 一次新佐证（S=1 时 0.5 → 0.667，与再被独立证实一次同值）；
  差评拉低（S=1 时 0.5 → 0.333）；正负相抵回中性（0.5）。
- 佐证路径必须同步：worker._bump_support 此前用 support_confidence
  重算——反馈后若再被佐证，旧算法会把负反馈洗掉（S=1,N=1 时 0.333，
  佐证到 S=2 后旧算法给 0.667）。本轮起 _bump_support 改用
  fact_confidence（无反馈时与旧公式同值，旧行为不变）。

## 接口（与经验反馈同形）

- 服务层（app/episodes.py，record_feedback 隔壁）：record_fact_feedback /
  record_preference_feedback——本人名下才认（跨用户/不存在 ValueError，
  REST 转 404，不泄露存在性）；P/N 计数 + 置信度重算 + 审计；事实与
  偏好都没有 last_used_at，不刷时间（经验才有闲置衰减时钟）。
- REST（与 /v1/insights/feedback 同文件）：POST /v1/facts/feedback
  {fact_id, helpful}、POST /v1/preferences/feedback {preference_id, helpful}；
  未同意/已暂停时与经验反馈一致地静默 404。
- MCP：memory_fact_feedback / memory_preference_feedback（同款守卫：
  memory_enabled → memory_disabled；ValueError → not_found）。
- 迁移 0023：memory_fact 与 memory_preference 各加 feedback_pos /
  feedback_neg（Integer，默认 0）。面板列表项（MemoryItem）一并带出
  两项计数；行组件加 有用/无用 按钮，点后重载列表（置信徽标即时动）。

## 本轮不做的事

- 事实反馈不连边权：事实没有扩散交付边（边学习只针对经验），反馈只
  动事实/偏好置信度，不碰 memory_link。
- 事实不参与闲置衰减与退役（第十一轮起只针对经验），反馈不刷任何
  时间字段；事实的“遗忘”仍是版本滚动与合并，不在本轮扩展。

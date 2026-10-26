"""类比召回（第 33 轮）：动作序列即结构。

一条经验/任务的“结构” = 按原文顺序提取的通用动作类别序列，
与领域名词、工具名、具体数值无关。相似度 = 动作序列的 LCS
（最长公共子序列）/ 较长序列长度——顺序敏感、词袋不敏感。

提取规则（与换皮关键词的分界）：
- 类别抽象：探测/排查/检查同属 diagnose，表面换词不换类；
- 顺序敏感：LCS 计顺序，词袋相同但顺序颠倒得分更低；
- 领域名词剥离：Jenkins/GitHub Actions/Docker/端口等词永远
  不进入签名，跨领域才可比；
- 最长优先 + 消耗式扫描：同一位置取最长表面词（“检查通过”
  压过“检查”，“测试”不被拆成“试”）；ASCII 表面词要求词边界
  （restart 不误含 start，restart 本身单列为 retry）；
- 连续重复去重：“查了又查”只算一次 diagnose。

候选有界（吸取 20 轮教训）：不做“move IN + GROUP BY 全桶聚合”
（342k 桶 7.9s 的教训）。每动作只取前 ANALOGY_PER_MOVE 条
posting（纯索引扫描，与桶大小无关），Python 侧按共享动作数
排序取前 ANALOGY_CANDIDATE_CAP 行算精确 LCS。成本 =
O(查询动作数 × 200 + 200 × LCS)，与总行数无关。
"""
from collections import Counter

from sqlalchemy import select

# 注意：本模块顶层不 import app.models（models.py 要 import 本模块的
# extract_moves，顶层互引会成环）。探针里用函数内延迟 import。

# 类别 -> 表面词。中文为主，英文为辅；提取时按表面词长度降序试配。
MOVE_LEXICON: dict[str, list[str]] = {
    "diagnose": ["排查", "诊断", "定位", "检查", "检测", "监测", "观察", "日志",
                 "探测", "探查", "扫描",
                 "debug", "diagnose", "inspect", "check", "checks", "checked", "checking"],
    "attempt": ["尝试", "试试", "试一试", "try"],
    "retry": ["重试", "再试", "重新", "重跑", "重启", "restart", "retry", "rerun"],
    "rollback": ["回滚", "回退", "撤销", "还原", "rollback", "revert"],
    "isolate": ["隔离", "屏蔽", "排除", "拆分", "最小复现", "isolate"],
    "verify": ["验证", "确认", "核对", "自测", "回归", "冒烟", "测试",
               "检查通过", "verify", "test", "tests", "tested", "testing"],
    "configure": ["配置", "设置", "参数", "环境变量", "config", "setup"],
    "deploy": ["部署", "发布", "上线", "发版", "deploy", "deployed", "deploying", "release"],
    "build": ["构建", "编译", "打包", "rebuild", "build", "builds", "compile"],
    "start": ["启动", "拉起", "运行", "start", "run"],
    "stop": ["停止", "停掉", "下线", "杀掉", "stop", "kill"],
    "clean": ["清理", "删除", "删掉", "清空", "释放", "clean", "purge"],
    "fix": ["修复", "解决", "搞定", "fix", "fixes", "fixed", "resolve"],
    "wait": ["等待", "延迟", "间隔", "wait", "sleep"],
    "upgrade": ["升级", "升版", "更新版本", "upgrade", "update"],
    "switch": ["切换", "迁移", "换源", "换镜像", "更换", "switch", "migrate"],
    "backup": ["备份", "快照", "导出", "backup", "snapshot"],
}

# 拍平并按长度降序：同一位置最长表面词先赢。
_SURFACES: list[tuple[str, str]] = sorted(
    ((s, cat) for cat, words in MOVE_LEXICON.items() for s in words),
    key=lambda kv: len(kv[0]), reverse=True)


def _is_ascii(s: str) -> bool:
    return s.isascii()


def extract_moves(text: str | None) -> list[str]:
    """从文本提取有序动作类别序列（连续重复去重）。"""
    if not text:
        return []
    low = text.lower()
    out: list[str] = []
    i, n = 0, len(low)
    while i < n:
        hit = None
        for surface, cat in _SURFACES:
            if not low.startswith(surface, i):
                continue
            if _is_ascii(surface):
                before = low[i - 1] if i > 0 else ""
                after = low[i + len(surface)] if i + len(surface) < n else ""
                if (before.isalnum() or after.isalnum()):
                    continue
            hit = (surface, cat)
            break
        if hit is None:
            i += 1
            continue
        surface, cat = hit
        if not out or out[-1] != cat:
            out.append(cat)
        i += len(surface)
    return out


def lcs_len(a: list[str], b: list[str]) -> int:
    """最长公共子序列长度。序列通常 ≤ 20，DP 足够。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[len(b)]


def analogy_sim(query_moves: list[str], row_moves: list[str]) -> tuple[float, int]:
    """返回 (sim, lcs)。空序列 sim = 0。"""
    if not query_moves or not row_moves:
        return 0.0, 0
    lcs = lcs_len(query_moves, row_moves)
    return lcs / max(len(query_moves), len(row_moves)), lcs


# 类比准入线：33 轮探针集上的分布见 docs/eval.md（正例 sim ≥ 0.571，
# 负例 sim ≤ 0.333，取间隔中点 0.45；探针数少，属方向性标定而非正式标定）。
ANALOGY_MIN = 0.45
ANALOGY_MIN_SHARED = 2  # 至少两个共享动作，单动作巧合不算类比。
ANALOGY_PER_MOVE = 200  # 每动作索引采样上限（与锚点探针同哲学）。
ANALOGY_CANDIDATE_CAP = 200  # 精确 LCS 的候选上限。


def analogy_candidates(db, cols, scope, query_moves: list[str]):
    """类比候选探针：返回 [(row, sim)]，row 带 cols 列，sim 已过门槛。

    有界：每查询动作只取前 ANALOGY_PER_MOVE 条 posting，Python 侧按
    共享动作数取前 ANALOGY_CANDIDATE_CAP 行算精确 LCS；scope（app /
    user / valid_to / muted）与简报同口径，探针取行时即过滤。
    """
    if not query_moves:
        return []
    from app import models as m  # 延迟 import，见模块头注释。

    wanted = set(query_moves)
    shared: Counter = Counter()
    for mv in wanted:
        ids = [r[0] for r in db.execute(
            select(m.MemoryInsightMove.insight_id).where(
                m.MemoryInsightMove.move == mv).limit(ANALOGY_PER_MOVE)).fetchall()]
        for i in ids:
            shared[i] += 1
    if not shared:
        return []
    top_ids = [i for i, _ in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0]))
               [:ANALOGY_CANDIDATE_CAP]]
    rows = list(db.execute(
        select(*cols).where(m.MemoryInsight.id.in_(top_ids), *scope)))
    seqs: dict[str, list[str]] = {}
    for rid, _pos, mv in db.execute(
            select(m.MemoryInsightMove.insight_id, m.MemoryInsightMove.position,
                   m.MemoryInsightMove.move).where(
                       m.MemoryInsightMove.insight_id.in_(top_ids)
                   ).order_by(m.MemoryInsightMove.insight_id,
                              m.MemoryInsightMove.position)):
        seqs.setdefault(rid, []).append(mv)
    out = []
    for row in rows:
        sim, lcs = analogy_sim(query_moves, seqs.get(row.id, []))
        if sim >= ANALOGY_MIN and lcs >= ANALOGY_MIN_SHARED:
            out.append((row, sim))
    return out

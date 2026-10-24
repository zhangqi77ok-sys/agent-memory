"""分词：召回打分与关键词倒排索引共用同一套口径，全仓只此一份实现。

口径（与召回门槛 MIN_SCORE 配套校准）：
- 英文/数字按完整词（[a-z0-9]+ 的极大连续段）；
- 中文按相邻两字（bigram）切，避免「咖啡」和「咖喱」因共享一字而误命中；
- 单个汉字的连续段（整段只有一个字）退回按字。
空白、标点不参与计分。倒排索引（memory_fact_token / memory_insight_token）存的
就是这里产出的 token 集合，所以「某行命中几个查询 token」在索引侧数得出来，
且与逐行打分严格一致——索引是精确计数，不是近似预筛。
"""
import re

CJK_LO, CJK_HI = "一", "鿿"


MAX_TOKEN_LEN = 128  # 倒排索引只收 ≤128 字符的 token：varchar 定长 + 索引行宽的工程上限；
# 更长的 [a-z0-9] 连续段（如 base64 块）本就没有检索意义。召回侧遇到超长查询
# token 时自动退全量精确打分，精确性不靠索引这一条路兜底（见 app/recall.py）。


def tokens_of(s: str) -> set[str]:
    s = s.lower()
    out = set(re.findall(r"[a-z0-9]+", s))
    for run in re.findall(r"[一-鿿]+", s):
        out |= {run} if len(run) == 1 else {run[i:i + 2] for i in range(len(run) - 1)}
    return out

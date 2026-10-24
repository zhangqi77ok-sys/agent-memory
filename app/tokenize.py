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


def ordered_tokens(s: str) -> list[str]:
    """与 tokens_of 同口径（同一集合），但按查询中首次出现顺序返回（去重）。

    锚点探针用查询词序选锚：查询前部的词通常就是主题词，先试它们最容易
    一次攒够候选；顺序本身不影响正确性（锚点只决定采样顺序，命中数仍精确验算）。
    """
    s = s.lower()
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"[a-z0-9]+|[一-鿿]+", s):
        run = m.group(0)
        toks = [run] if run.isascii() else (
            [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
        for t in toks:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out

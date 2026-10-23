"""向量嵌入：把事实文本转成向量，供语义召回使用。

供应商固定为 Google Gemini（gemini-embedding-001），走 generativelanguage.googleapis.com，
密钥从 GEMINI_API_KEY 环境变量读，只放在请求头 x-goog-api-key 里，不进日志、不进库。

与抽取（extract.py）不同，嵌入是增强项而非必需项：没配密钥时 build_embedder 返回 None，
写入不带向量、召回自动退回关键词，行为与旧版一致。调用失败抛 EmbeddingError，
由调用方决定降级（Worker 记警告后照常入库，召回退回关键词），不许拖垮主流程。
"""
import json
import math
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.config import Settings, settings

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
BATCH_LIMIT = 100  # batchEmbedContents 单次请求条数上限


class EmbeddingError(Exception):
    """嵌入调用失败（网络/HTTP/响应格式）。可降级，不应让写入或召回整体失败。"""


def vector_usable(row_model, row_vec, query_vec, embedder_model) -> bool:
    """一条存量向量能不能和本次查询向量比：非空、维度一致、且生成模型一致。

    第八轮补（此前静默降级）：维度或模型对不上时 cosine 只会给出 0，却仍被当作
    有效向量参与融合打分（分数被悄悄压低、reason 还误标 hybrid）。改配置或换模型后
    应把对不上的存量向量当作「没有向量」，走关键词路，并靠 backfill 补齐新向量。
    row_model 为空是早期未记模型的老数据：维度对得上就放行。
    """
    if not row_vec or not query_vec or len(row_vec) != len(query_vec):
        return False
    return row_model is None or embedder_model is None or row_model == embedder_model


def cosine(a, b) -> float:
    """纯 Python 余弦相似度。向量为空或长度不一致返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class GeminiEmbedder:
    """鸭子类型协议：embed(texts, task_type) -> list[list[float]]，另有 model 属性供记录来源。

    task_type：存入侧用 RETRIEVAL_DOCUMENT，查询侧用 RETRIEVAL_QUERY（Gemini 官方建议的非对称用法）。
    """

    def __init__(self, api_key: str, model: str, dim: int, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH_LIMIT):
            out.extend(self._embed_batch(texts[i:i + BATCH_LIMIT], task_type))
        return out

    def _embed_batch(self, texts: list[str], task_type: str) -> list[list[float]]:
        url = f"{API_BASE}/models/{self.model}:batchEmbedContents"
        body = {"requests": [
            {
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": t}]},
                "taskType": task_type,
                "outputDimensionality": self.dim,
            }
            for t in texts
        ]}
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise EmbeddingError(f"Gemini HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise EmbeddingError(f"Gemini 连接失败: {e.reason}") from e
        except (TimeoutError, ConnectionError, OSError) as e:
            raise EmbeddingError(f"Gemini 连接异常: {e}") from e
        except json.JSONDecodeError as e:
            raise EmbeddingError("Gemini 响应不是 JSON") from e
        try:
            vectors = [list(item["values"]) for item in payload["embeddings"]]
        except (KeyError, TypeError) as e:
            raise EmbeddingError("Gemini 响应缺少 embeddings") from e
        if len(vectors) != len(texts) or any(len(v) != self.dim for v in vectors):
            raise EmbeddingError("Gemini 返回的向量条数或维度与请求不一致")
        return vectors


def build_embedder(s: Settings | None = None, timeout: float | None = None) -> GeminiEmbedder | None:
    """构造嵌入器。未启用或未配密钥时返回 None（调用方据此降级），不抛错。

    timeout 不传时用 embedding_timeout_s（Worker 批量场景）；召回侧应传入 recall_timeout_s，
    避免一次慢查询拖住整个召回接口。
    """
    s = s or settings
    if (s.embedding_provider or "").strip().lower() != "gemini":
        return None
    if not (s.gemini_api_key and s.gemini_api_key.strip()):
        return None
    return GeminiEmbedder(
        api_key=s.gemini_api_key.strip(),
        model=s.embedding_model.strip(),
        dim=s.embedding_dim,
        timeout=timeout if timeout is not None else s.embedding_timeout_s,
    )


def backfill_embeddings(db: Session, embedder, limit: int = 200) -> int:
    """给尚无向量的有效事实批量补向量（上线切换时跑一次）。返回补齐条数。

    只处理 valid_to 为空的事实；已失效的历史版本不参与召回，不浪费额度。
    缺向量判断在 Python 里做：SQLite 的 JSON 列里显式写入的 None 是 JSON null 而非 SQL NULL，
    用 IS NULL 过滤会漏行；个人量级下全量过一遍可以接受。
    """
    active = db.scalars(select(m.MemoryFact).where(m.MemoryFact.valid_to.is_(None))).all()
    rows = [r for r in active if r.embedding is None][:limit]
    if not rows:
        return 0
    vectors = embedder.embed([f"{r.subject} {r.content}" for r in rows], task_type="RETRIEVAL_DOCUMENT")
    for row, vec in zip(rows, vectors, strict=True):  # 条数必须对齐，错位配对会把向量安到别人的事实上
        row.embedding = vec
        row.embed_model = getattr(embedder, "model", None)
    db.commit()
    return len(rows)


def backfill_insight_embeddings(db: Session, embedder, limit: int = 200) -> int:
    """给尚无向量的有效经验批量补向量（第八轮补：此前只有事实有补向量通道，
    无 key 期间攒下的经验在配好 key 后也永远没向量，brief 长期只有关键词一路）。
    匹配面与入库时一致：主题 + 指导 + 适用情境。返回补齐条数。"""
    active = db.scalars(select(m.MemoryInsight).where(m.MemoryInsight.valid_to.is_(None))).all()
    rows = [r for r in active if r.embedding is None][:limit]
    if not rows:
        return 0
    vectors = embedder.embed([f"{r.subject} {r.content} {r.applies_when}" for r in rows],
                             task_type="RETRIEVAL_DOCUMENT")
    for row, vec in zip(rows, vectors, strict=True):
        row.embedding = vec
        row.embed_model = getattr(embedder, "model", None)
    db.commit()
    return len(rows)


if __name__ == "__main__":  # pragma: no cover
    # 用法：GEMINI_API_KEY=... python -m app.embeddings [--limit N] [--target facts|insights|all]
    import sys

    from app.db import WorkerSession

    lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 200
    target = sys.argv[sys.argv.index("--target") + 1] if "--target" in sys.argv else "all"
    emb = build_embedder()
    if emb is None:
        raise SystemExit("未配置 GEMINI_API_KEY，无法补向量")
    with WorkerSession() as session:
        if target in ("facts", "all"):
            print(f"backfilled_facts={backfill_embeddings(session, emb, limit=lim)}")
        if target in ("insights", "all"):
            print(f"backfilled_insights={backfill_insight_embeddings(session, emb, limit=lim)}")

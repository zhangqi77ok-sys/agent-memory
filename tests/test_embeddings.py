"""向量嵌入：Gemini 客户端协议、开关降级、Worker 写入钩子、混合召回、存量补齐。"""
import json
import urllib.error
import urllib.request

import pytest

from app import models as m
from app.auth import UserContext
from app.config import Settings, settings
from app.embeddings import EmbeddingError, GeminiEmbedder, backfill_embeddings, build_embedder, cosine
from app.recall import recall_facts
from app.worker import _apply_fact


class FakeEmbedder:
    model = "fake-embed-1"

    def __init__(self, vec=(0.1, 0.9)):
        self.vec = list(vec)
        self.calls = []

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.calls.append((list(texts), task_type))
        return [list(self.vec) for _ in texts]


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_gemini_request_shape(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.get_header("X-goog-api-key")
        captured["body"] = json.loads(req.data.decode())
        return _Resp({"embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}, {"values": [0.5, 0.6, 0.7, 0.8]}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    emb = GeminiEmbedder(api_key="test-key", model="gemini-embedding-001", dim=4)
    out = emb.embed(["甲", "乙"], task_type="RETRIEVAL_QUERY")
    assert out == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    assert captured["url"].endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert captured["key"] == "test-key"
    req0 = captured["body"]["requests"][0]
    assert req0["model"] == "models/gemini-embedding-001"
    assert req0["taskType"] == "RETRIEVAL_QUERY"
    assert req0["outputDimensionality"] == 4
    assert req0["content"]["parts"] == [{"text": "甲"}]


def test_gemini_http_error_becomes_embedding_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    emb = GeminiEmbedder(api_key="bad-key", model="gemini-embedding-001", dim=4)
    with pytest.raises(EmbeddingError, match="403"):
        emb.embed(["甲"])


def test_gemini_dim_mismatch_rejected(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"embeddings": [{"values": [0.1]}]}))
    emb = GeminiEmbedder(api_key="k", model="gemini-embedding-001", dim=4)
    with pytest.raises(EmbeddingError, match="维度"):
        emb.embed(["甲"])


def test_build_embedder_gating():
    assert build_embedder(Settings(gemini_api_key="")) is None
    assert build_embedder(Settings(gemini_api_key="k", embedding_provider="")) is None
    emb = build_embedder(Settings(gemini_api_key="k"))
    assert isinstance(emb, GeminiEmbedder)
    assert emb.model == "gemini-embedding-001" and emb.dim == 1536


def test_cosine():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0


def _job(db, user="alice"):
    job = m.MemoryJob(user_id=user, idempotency_key="k1", session_id="s1")
    db.add(job)
    db.commit()
    return job


def test_worker_embeds_new_fact(db):
    fake = FakeEmbedder()
    decision, fid = _apply_fact(db, _job(db), "咖啡", "喜欢拿铁", "ADD", None, embedder=fake)
    assert decision == "add"
    fact = db.get(m.MemoryFact, fid)
    assert fact.embedding == [0.1, 0.9]
    assert fact.embed_model == "fake-embed-1"
    assert fake.calls[0][1] == "RETRIEVAL_DOCUMENT"


def test_worker_tolerates_embedding_failure(db):
    class Boom:
        model = "boom"

        def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
            raise RuntimeError("network down")

    decision, fid = _apply_fact(db, _job(db), "咖啡", "喜欢拿铁", "ADD", None, embedder=Boom())
    assert decision == "add"  # 向量失败不拖垮写入
    assert db.get(m.MemoryFact, fid).embedding is None


def _fact(db, subject, content, embedding=None, user="alice"):
    f = m.MemoryFact(user_id=user, subject=subject, content=content, embedding=embedding)
    db.add(f)
    db.commit()
    return f


def test_recall_hybrid_finds_semantic_match_without_keyword_overlap(db):
    a = _fact(db, "咖啡", "喜欢拿铁", embedding=[1.0, 0.0])
    _fact(db, "天气", "今天晴天", embedding=[0.0, 1.0])
    u = UserContext(app_id=settings.app_id, user_id="alice")
    out = recall_facts(db, u, "提神饮料", k=5, token_budget=1500, embedder=FakeEmbedder(vec=(1.0, 0.0)))
    assert [f.fact_id for f in out] == [a.id]  # 关键词重叠为 0，纯靠向量命中
    assert out[0].reason == "hybrid"
    assert out[0].score == pytest.approx(0.7)


def test_recall_falls_back_to_keyword_when_embedder_fails(db):
    a = _fact(db, "咖啡", "喜欢拿铁", embedding=[1.0, 0.0])
    u = UserContext(app_id=settings.app_id, user_id="alice")

    class Boom:
        model = "boom"

        def embed(self, texts, task_type="RETRIEVAL_QUERY"):
            raise RuntimeError("down")

    out = recall_facts(db, u, "拿铁", k=5, token_budget=1500, embedder=Boom())
    assert [f.fact_id for f in out] == [a.id]
    assert out[0].reason == "keyword"
    # 无关键词重叠时降级路径召不回（与旧版行为一致），不报错
    assert recall_facts(db, u, "提神饮料", k=5, token_budget=1500, embedder=Boom()) == []


def test_backfill_embeddings(db):
    f1 = _fact(db, "咖啡", "喜欢拿铁")
    _fact(db, "天气", "今天晴天", embedding=[0.0, 1.0])
    fake = FakeEmbedder()
    assert backfill_embeddings(db, fake) == 1
    db.refresh(f1)
    assert f1.embedding == [0.1, 0.9] and f1.embed_model == "fake-embed-1"
    assert backfill_embeddings(db, FakeEmbedder()) == 0

from fastapi.testclient import TestClient

from app import models as m
from app.config import settings
from app.db import engine
from app.main import app
from app.metrics import metrics_collector


def test_metrics_endpoint_bypasses_service_key():
    raw_client = TestClient(app)
    # /metrics 不需要 X-Service-Key
    r = raw_client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["Content-Type"]


def test_metrics_content_format_and_counters(client, db):
    metrics_collector.reset()

    # 发起一些请求
    client.get("/healthz")
    client.get("/v1/me/settings?user_id=alice")

    # 往数据库加一条 pending job
    job = m.MemoryJob(
        app_id=settings.app_id,
        user_id="alice",
        idempotency_key="metric-job-1",
        session_id="s1",
        status="pending",
    )
    db.add(job)
    db.commit()

    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text

    # 验证 Prometheus 结构与关键指标
    assert "# HELP http_requests_total" in body
    assert "# TYPE http_requests_total counter"
    assert 'http_requests_total{method="GET",path="/healthz",status="200"}' in body

    assert "# HELP http_requests_5xx_total" in body
    assert "http_requests_5xx_total 0" in body

    assert "# HELP memory_job_count" in body
    assert 'memory_job_count{status="pending"} 1' in body

    assert "# HELP memory_job_oldest_pending_age_seconds" in body
    assert "# HELP memory_readyz_status" in body
    assert "memory_readyz_status 1" in body


def test_metrics_tracks_job_metrics_directly():
    counts, oldest_age = metrics_collector.collect_job_metrics(engine)
    assert isinstance(counts, dict)
    assert "pending" in counts
    assert "running" in counts
    assert oldest_age >= 0.0

"""P0 热修复回归测试（细节评审 2026-10-23 的两条部署即坏项）。

- P0-1: app/distill.py build_distiller() 读取 settings.llm_timeout_s，
  但 Settings 没有该字段 -> AttributeError，Worker 启动即崩。
- P0-2: app/worker.py 主循环尾部 `elif job.status == "pending"`，
  在 job 为 None（本轮只有 episode、无 fact job）时 -> AttributeError。
"""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.distill import LlmDistiller, build_distiller
from app.worker import _post_run_wait


def _configured() -> Settings:
    return Settings(
        app_env="test",
        llm_base_url="http://host.docker.internal:8080/v1",
        llm_api_key="k",
        llm_model="demo",
    )


def test_p0_1_llm_timeout_field_exists_with_default():
    assert Settings(app_env="test").llm_timeout_s == 30.0


def test_p0_1_build_distiller_uses_llm_timeout():
    d = build_distiller(_configured())
    assert isinstance(d, LlmDistiller)
    assert d.timeout == 30.0


def test_p0_1_missing_llm_config_still_raises_runtime():
    with pytest.raises(RuntimeError, match="distiller not configured"):
        build_distiller(Settings(app_env="test", llm_base_url="", llm_api_key="", llm_model=""))


def test_p0_2_job_none_no_crash():
    # 回归：旧代码此处 `elif job.status` 在 job=None 时抛 AttributeError
    assert _post_run_wait(None, SimpleNamespace()) == 2.0
    assert _post_run_wait(None, None) == 2.0


def test_p0_2_pending_backoff_capped():
    assert _post_run_wait(SimpleNamespace(status="pending", attempts=1), None) == 2.0
    assert _post_run_wait(SimpleNamespace(status="pending", attempts=3), None) == 8.0
    assert _post_run_wait(SimpleNamespace(status="pending", attempts=9), None) == 30.0


def test_p0_2_terminal_job_short_wait():
    assert _post_run_wait(SimpleNamespace(status="done", attempts=2), None) == 2.0


def test_worker_startup_smoke_constructs_all_builders(monkeypatch):
    """启动冒烟：按 worker.main() 的原始顺序构造 extractor → distiller → embedder。

    P0-1 的崩溃就发生在这段构造里且当时无任何测试覆盖到它；本用例用全局 settings
    （三个 builder 的默认参数绑定的正是同一个对象）打桩 LLM 配置后整段走一遍，
    任何一个 builder 再抛 AttributeError / RuntimeError 都会在这里红。
    embedder 在未配 GEMINI_API_KEY 时返回 None 是合法降级（main 里记为 off），不算失败。
    """
    from app import config
    from app.embeddings import build_embedder
    from app.extract import LlmExtractor, build_extractor

    monkeypatch.setattr(config.settings, "llm_base_url", "http://host.docker.internal:8080/v1")
    monkeypatch.setattr(config.settings, "llm_api_key", "demo-key")
    monkeypatch.setattr(config.settings, "llm_model", "demo")

    extractor = build_extractor()
    distiller = build_distiller()
    embedder = build_embedder()

    assert isinstance(extractor, LlmExtractor)
    assert isinstance(distiller, LlmDistiller)
    assert embedder is None or hasattr(embedder, "embed")

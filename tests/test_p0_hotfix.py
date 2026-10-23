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

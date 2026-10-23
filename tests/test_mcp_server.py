"""MCP 门面：与 REST 共用同一条代码路径——同意门、幂等、写入管线、召回、删除都一致。"""
import pytest

pytest.importorskip("mcp")

import app.mcp_server as ms  # noqa: E402
from app.extract import Candidate  # noqa: E402
from app.worker import run_once  # noqa: E402
from tests.conftest import consent  # noqa: E402


class FakeExtractor:
    def extract(self, text):
        return [Candidate("fact", "咖啡", "喜欢拿铁", "ADD")]


def test_mcp_roundtrip_write_search_forget(client, db, monkeypatch):
    monkeypatch.setenv("MEMORY_USER_ID", "alice")
    consent(client, "alice")

    accepted = ms.memory_write("我平时喝咖啡喜欢拿铁", source="codex")
    assert accepted["skipped"] is False and accepted["duplicate"] is False and accepted["job_id"]

    # 幂等：同一 key 重复提交不产生新任务
    first = ms.memory_write("重复提交", idempotency_key="fixed-key-1")
    second = ms.memory_write("重复提交", idempotency_key="fixed-key-1")
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["job_id"] == second["job_id"]

    # Worker 处理后事实入库，MCP 搜索即可见（两个入口同一份数据）
    job = run_once(db, FakeExtractor())
    assert job is not None and job.status == "done"
    found = ms.memory_search("拿铁")
    assert [f["subject"] for f in found["facts"]] == ["咖啡"]
    assert found["facts"][0]["content"] == "喜欢拿铁"

    listed = ms.memory_list()
    assert listed["total"] >= 1
    fact_id = found["facts"][0]["fact_id"]

    assert ms.memory_forget(fact_id) == {"ok": True, "forgotten_id": fact_id}
    assert ms.memory_search("拿铁")["facts"] == []
    assert ms.memory_forget(fact_id)["ok"] is False  # 已删再删 → not_found


def test_mcp_consent_gate_blocks_without_consent(client, db, monkeypatch):
    monkeypatch.setenv("MEMORY_USER_ID", "bob")  # bob 从未同意
    accepted = ms.memory_write("不应被存下的内容")
    assert accepted["skipped"] is True and accepted["job_id"] is None
    assert ms.memory_search("内容")["facts"] == []
    assert ms.memory_list()["total"] == 0


def test_mcp_auto_consent_for_personal_use(client, db, monkeypatch):
    monkeypatch.setenv("MEMORY_USER_ID", "carol")
    monkeypatch.setenv("MEMORY_AUTO_CONSENT", "1")
    accepted = ms.memory_write("第一次使用就自动开启")
    assert accepted["skipped"] is False and accepted["job_id"]

"""第 39 轮：CLI 自动采集钩子（Codex / Grok / Gemini）。

设计见 round39-design-draft.md。fixture 均为按各家已公开内容类别
（prompt / tool call / 输出 / 错误）构造的通用形状，不冒充真实
CLI 的盘上文件；真实文件格式变化时解析回落粗粒度，不丢会话。
"""

import json
import os
import subprocess
import sys

from app.capture import (
    build_episode,
    dedup_key,
    derive_project,
    parse_codex_rollout,
    parse_gemini_transcript,
    parse_grok_session,
)
from scripts import capture_cli
from tests.conftest import consent

CODEX_ROLLOUT = "\n".join([
    json.dumps({"type": "session_meta", "payload": {"id": "s1", "cwd": "/tmp/x"}}),
    json.dumps({"type": "event_msg", "payload": {"item": {"type": "message", "role": "user",
                                                         "content": "修好登录接口的 500 错误"}}}),
    json.dumps({"type": "response_item", "payload": {"item": {"type": "function_call",
                                                             "name": "Bash",
                                                             "arguments": {"cmd": "pytest -q"}}}}),
    json.dumps({"type": "response_item", "payload": {"item": {"type": "function_call_output",
                                                             "output": "3 failed, 10 passed"}}}),
    json.dumps({"type": "response_item", "payload": {"item": {"type": "reasoning",
                                                             "content": "内部推理不采"}}}),
    json.dumps({"type": "turn_context", "payload": {"tokens": 123}}),
    "这行不是 JSON，必须跳过",
    json.dumps({"type": "response_item", "payload": {"item": {"type": "message",
                                                             "role": "assistant",
                                                             "content": "已定位空指针"}}}),
])

GEMINI_TRANSCRIPT = {
    "messages": [
        {"role": "user", "parts": [{"text": "给 median 加边界测试"}]},
        {"role": "model", "parts": [
            {"functionCall": {"name": "write_file", "args": {"file_path": "t.py"}}},
            {"functionResponse": {"name": "write_file", "response": "written"}}]},
        {"role": "model", "parts": [{"text": "测试已补齐"}]},
    ]
}

GROK_SESSION = [
    {"role": "user", "text": "解释这个模块为什么慢"},
    {"tool": "read_file", "input": {"path": "a.py"}, "output": "def f(): ..."},
    {"tool": "run_shell", "input": "pytest -q", "error": "ModuleNotFoundError: foo"},
]

SECRET_FIXTURE = "\n".join([
    json.dumps({"type": "event_msg", "payload": {"item": {"type": "message", "role": "user",
                                                         "content": "用这个 key 调接口 sk-abcdefghij1234567890"}}}),
    json.dumps({"type": "response_item", "payload": {"item": {"type": "function_call_output",
                                                             "output": "token=ghp_abcdefghij1234567890 password= hunter2"}}}),
])


def test_codex_rollout_extracts_prompt_tool_and_output():
    prompts, steps = parse_codex_rollout(CODEX_ROLLOUT)
    assert prompts == ["修好登录接口的 500 错误"]
    actions = [s["action"] for s in steps]
    assert any("Bash" in a for a in actions)
    assert any(s.get("observation") == "3 failed, 10 passed" for s in steps)
    assert not any("内部推理" in a for a in actions)  # reasoning 跳过
    assert not any("turn_context" in a for a in actions)  # 未知记录跳过


def test_gemini_transcript_extracts_function_call_and_response():
    prompts, steps = parse_gemini_transcript(GEMINI_TRANSCRIPT)
    assert prompts == ["给 median 加边界测试"]
    actions = [s["action"] for s in steps]
    assert any("write_file" in a for a in actions)
    assert any(s.get("observation") == "written" for s in steps)


def test_grok_session_extracts_tool_and_error():
    prompts, steps = parse_grok_session(GROK_SESSION)
    assert prompts == ["解释这个模块为什么慢"]
    assert any("read_file" in a["action"] for a in steps)
    assert any(s.get("error") == "ModuleNotFoundError: foo" for s in steps)


def test_coarse_fallback_never_empty():
    ep = build_episode("codex", "deadbeef-1234", "")
    assert ep["title"]
    assert len(ep["steps"]) >= 1
    assert ep["steps"][0]["action"]
    ep2 = build_episode("grok", "deadbeef-1234", {"nonsense": True})
    assert len(ep2["steps"]) >= 1


def test_outcome_heuristic():
    clean = build_episode("gemini", "s1", GEMINI_TRANSCRIPT)
    assert clean["outcome"] == "success"
    faulty = build_episode("grok", "s2", GROK_SESSION)
    assert faulty["outcome"] == "partial"
    failed = build_episode("codex", "w1", None, exit_code=1,
                           wrapper_output="boom", prompt="跑测试")
    assert failed["outcome"] == "failure"
    assert failed["title"] == "跑测试"


def test_redaction_before_submit_and_marker_has_no_content(tmp_path):
    ep = build_episode("codex", "s9", SECRET_FIXTURE)
    blob = json.dumps(ep, ensure_ascii=False)
    assert "sk-abcdefghij1234567890" not in blob
    assert "ghp_abcdefghij1234567890" not in blob
    assert "hunter2" not in blob


def test_dedup_key_stable_and_scoped():
    assert dedup_key("codex", "abc") == dedup_key("codex", "abc")
    assert dedup_key("codex", "abc") != dedup_key("codex", "abd")
    assert dedup_key("codex", "abc") != dedup_key("gemini", "abc")
    assert len(dedup_key("codex", "x" * 200)) <= 128


def test_project_derivation_git_and_fallback(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True)
    assert derive_project(str(repo)) == "myrepo"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert derive_project(str(plain)) == "plain"
    assert derive_project(None) is None


def test_caps_match_server_schema():
    rollout = "\n".join(
        json.dumps({"type": "response_item", "payload": {"item": {
            "type": "function_call", "name": "Bash",
            "arguments": {"cmd": f"echo {i}"}}}})
        for i in range(300))
    ep = build_episode("codex", "s1", rollout, prompt="任务")
    assert len(ep["steps"]) == 200  # 300 条工具步截断到服务端上限
    ep2 = build_episode("codex", "s1", None, wrapper_output="x" * 9000,
                        prompt="任务")
    assert len(ep2["steps"]) <= 200
    long_obs = build_episode(
        "gemini", "s1",
        {"messages": [{"role": "model", "parts": [
            {"functionResponse": {"name": "f", "response": "y" * 5000}}]}]})
    obs = [s for s in long_obs["steps"] if s.get("observation")]
    assert obs and len(obs[0]["observation"]) <= 2000


def _run_hook(tmp_path, cli, payload, env_extra=None):
    outbox = tmp_path / "outbox"
    env = dict(os.environ)
    env["BRAINWEAVE_CAPTURE_OUTBOX"] = str(outbox)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, capture_cli.HOOK_SCRIPT, cli],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=30)
    return proc, outbox


def test_hook_shim_writes_marker_and_always_exits_zero(tmp_path):
    proc, outbox = _run_hook(tmp_path, "codex",
                             {"session_id": "sess-1", "cwd": "/tmp/x"})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    markers = (outbox / "markers.jsonl").read_text(encoding="utf-8")
    assert "sess-1" in markers
    assert "修好登录" not in markers  # marker 不含会话内容


def test_hook_shim_gemini_transcript_path_and_grok_env(tmp_path):
    proc, outbox = _run_hook(tmp_path, "gemini",
                             {"session_id": "g-2", "cwd": "/tmp/y",
                              "transcript_path": "/tmp/y/t.json"})
    assert proc.returncode == 0
    assert "/tmp/y/t.json" in (outbox / "markers.jsonl").read_text()
    proc, _ = _run_hook(tmp_path, "grok", {},
                        env_extra={"GROK_SESSION_ID": "gr-3",
                                   "GROK_WORKSPACE_ROOT": "/tmp/z"})
    assert proc.returncode == 0
    assert "gr-3" in (outbox / "markers.jsonl").read_text()


def test_hook_shim_malformed_input_still_exits_zero(tmp_path):
    outbox = tmp_path / "outbox"
    env = dict(os.environ)
    env["BRAINWEAVE_CAPTURE_OUTBOX"] = str(outbox)
    proc = subprocess.run(
        [sys.executable, capture_cli.HOOK_SCRIPT, "codex"],
        input="{不是 json", capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    assert not (outbox / "markers.jsonl").exists()  # 无 session_id 不写 marker


def test_install_uninstall_merge_and_idempotent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    # 预置用户自己的 hook，install 必须保留
    codex_path = home / ".codex" / "hooks.json"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    assert capture_cli.install() == 0
    data = json.loads(codex_path.read_text())
    assert any("echo hi" in json.dumps(g) for g in data["hooks"]["PreToolUse"])
    assert any("bw_hook.py codex" in json.dumps(g)
               for g in data["hooks"]["SessionEnd"])
    before = codex_path.read_text()
    assert capture_cli.install() == 0
    assert codex_path.read_text() == before  # 幂等
    assert (home / ".grok" / "hooks" / "brainweave-capture.json").exists()
    assert capture_cli.uninstall() == 0
    data = json.loads(codex_path.read_text())
    assert "SessionEnd" not in data["hooks"]
    assert any("echo hi" in json.dumps(g) for g in data["hooks"]["PreToolUse"])
    assert not (home / ".grok" / "hooks" / "brainweave-capture.json").exists()


def _write_markers(outbox, markers):
    outbox.mkdir(parents=True, exist_ok=True)
    with open(outbox / "markers.jsonl", "w", encoding="utf-8") as f:
        for m in markers:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def test_harvest_verdict_routing(monkeypatch, tmp_path):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("BRAINWEAVE_CAPTURE_OUTBOX", str(outbox))
    monkeypatch.setattr(capture_cli, "resolve_transcript",
                        lambda m: ('{"messages": [{"role": "user", "parts": '
                                   '[{"text": "做 A"}]}]}', None))
    calls = []

    def fake_post(base, key, user, payload, idem):
        calls.append((payload["source"], idem))
        return "ok", {"episode_id": "e-1"}

    monkeypatch.setattr(capture_cli, "post_episode", fake_post)
    # 两条同 session marker：第一条提交进 ledger，第二条被 ledger 跳过
    markers = [
        {"cli": "gemini", "session_id": "s-ok", "cwd": ""},
        {"cli": "gemini", "session_id": "s-ok", "cwd": ""},
    ]
    _write_markers(outbox, markers)
    assert capture_cli.harvest("http://x", "k", "alice", False) == 0
    ledger = json.loads((outbox / "ledger.json").read_text())
    assert "bw-cap:gemini:s-ok" in ledger  # 第二条同 key 被 ledger 跳过
    assert len(calls) == 1


def test_harvest_skipped_tombstoned_failed_routing(monkeypatch, tmp_path):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("BRAINWEAVE_CAPTURE_OUTBOX", str(outbox))

    def fake_resolve(marker):
        if marker["session_id"] == "s-lost":
            return None, "没找到"
        return ('{"messages": [{"role": "user", "parts": [{"text": "做 B"}]}]}', None)

    monkeypatch.setattr(capture_cli, "resolve_transcript", fake_resolve)

    def fake_post(base, key, user, payload, idem):
        if "s-skip" in idem:
            return "skipped", {}
        if "s-tomb" in idem:
            return "tombstoned", {}
        if "s-net" in idem:
            raise ConnectionError("down")
        return "ok", {"episode_id": "e-9"}

    monkeypatch.setattr(capture_cli, "post_episode", fake_post)
    _write_markers(outbox, [
        {"cli": "codex", "session_id": "s-skip", "cwd": ""},
        {"cli": "codex", "session_id": "s-tomb", "cwd": ""},
        {"cli": "codex", "session_id": "s-net", "cwd": ""},
        {"cli": "codex", "session_id": "s-lost", "cwd": ""},
    ])
    assert capture_cli.harvest("http://x", "k", "alice", False) == 0
    assert (outbox / ".skipped").exists()
    assert (outbox / ".tombstoned").exists()
    assert (outbox / ".unresolved").exists()
    remaining = (outbox / "markers.jsonl").read_text()
    assert "s-net" in remaining  # 网络错留原地，下次再试
    assert "s-skip" not in remaining


def test_server_tombstone_flag_blocks_capture_only(client, db):
    from app.config import settings
    from app.tombstone import record_episode_tombstone
    consent(client, "alice")
    body = {"user_id": "alice", "source": "codex-hook", "title": "遗忘的任务",
            "outcome": "success", "steps": [{"action": "做 A"}]}
    # 先记一条内容墓碑（模拟用户遗忘过这条轨迹）
    record_episode_tombstone(db, settings.app_id, "alice", "遗忘的任务",
                             [{"action": "做 A"}])
    db.commit()
    r = client.post("/v1/episodes", json={**body, "tombstone_check": True})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "tombstoned"
    # 不带 flag 的手动提交走老路：行落库（提炼时才挡），不改现有语义
    r = client.post("/v1/episodes", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["episode_id"]


def test_server_tombstone_flag_blocks_rest_origin_tombstone(client, db):
    # 收尾补丁：REST 提交经 model_dump() 入库，步骤带 observation/error
    # 的 None 键；clear_all 的墓碑直接对入库形态哈希。只查剔除 None 形态
    # 会漏拦这类墓碑——检查端必须双形态都查。
    from app.config import settings
    from app.tombstone import record_episode_tombstone
    consent(client, "alice")
    body = {"user_id": "alice", "source": "codex-hook", "title": "遗忘的任务",
            "outcome": "success", "steps": [{"action": "做 A"}]}
    record_episode_tombstone(db, settings.app_id, "alice", "遗忘的任务",
                             [{"action": "做 A", "observation": None, "error": None}])
    db.commit()
    r = client.post("/v1/episodes", json={**body, "tombstone_check": True})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "tombstoned"


def test_post_episode_5xx_is_retryable(monkeypatch):
    # 收尾补丁：服务端 5xx 是瞬态，必须返回 retry（harvester 留原地重
    # 试），不能像 4xx 那样直接判 failed 进 .failed/ 永不再试。
    import io
    import urllib.error

    def boom_500(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(capture_cli.urllib.request, "urlopen", boom_500)
    verdict, _ = capture_cli.post_episode("http://x", "k", "alice",
                                          {"title": "t", "steps": []}, "id-1")
    assert verdict == "retry"

    def boom_400(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(capture_cli.urllib.request, "urlopen", boom_400)
    verdict, _ = capture_cli.post_episode("http://x", "k", "alice",
                                          {"title": "t", "steps": []}, "id-1")
    assert verdict == "failed"


def test_harvest_retry_verdict_kept(monkeypatch, tmp_path):
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("BRAINWEAVE_CAPTURE_OUTBOX", str(outbox))
    monkeypatch.setattr(capture_cli, "resolve_transcript",
                        lambda m: ('{"messages": [{"role": "user", "parts": [{"text": "做 C"}]}]}', None))
    monkeypatch.setattr(capture_cli, "post_episode",
                        lambda *a: ("retry", {"status": 500}))
    _write_markers(outbox, [{"cli": "gemini", "session_id": "s-5xx", "cwd": ""}])
    assert capture_cli.harvest("http://x", "k", "alice", False) == 0
    remaining = (outbox / "markers.jsonl").read_text()
    assert "s-5xx" in remaining
    assert json.loads(remaining.strip())["attempts"] == 1
    assert not (outbox / ".failed").exists()


def test_wrapper_run_posts_coarse_episode(monkeypatch, tmp_path):
    captured = {}

    def fake_post(base, key, user, payload, idem):
        captured.update(payload)
        captured["idem"] = idem
        return "ok", {"episode_id": "e-w"}

    monkeypatch.setattr(capture_cli, "post_episode", fake_post)
    rc = capture_cli.run_wrapper("http://x", "k", "alice", "gemini",
                                 ["gemini", "-p", "列出文件"], 60, False)
    assert rc == 0
    assert captured["title"] == "列出文件"
    assert captured["source"] == "gemini-wrap"
    assert captured["idem"].startswith("bw-cap:gemini:wrap:")

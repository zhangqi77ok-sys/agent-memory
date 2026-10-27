#!/usr/bin/env python3
"""BrainWeave CLI 采集 hook shim（第 39 轮）。

三家 CLI（Codex / Gemini / Grok）的 SessionEnd hook 都指向它。
只做一件事：把一条 marker（寻址信息，不含会话内容）追加到 outbox，
然后输出 {}、exit 0。解析与提交全在 harvester 里做——hook 内绝不做
重活：Codex SessionEnd 上限 3 秒，Grok 被动事件忽略 stdout，
Gemini 要求 stdout 是合法 JSON。

用法（由各家 hook 配置调用，不直接给人用）：
    python3 bw_hook.py codex    # stdin: hook 事件 JSON
    python3 bw_hook.py gemini
    python3 bw_hook.py grok

stdin 形状（各家都取得到 session 与 cwd 即可）：
- Codex/Gemini：{"session_id": ..., "cwd": ..., "transcript_path": ...}
- Grok：{"sessionId": ..., "cwd": ..., "workspaceRoot": ...}，
  另有 GROK_SESSION_ID / GROK_WORKSPACE_ROOT 环境变量兜底。

marker 文件：$BRAINWEAVE_CAPTURE_OUTBOX/markers.jsonl（默认
~/.brainweave/capture-outbox/markers.jsonl），每行一条：
{"cli","session_id","cwd","transcript_path","ended_at"}。
任何异常都只写 stderr，绝不以非 0 退出码干扰 CLI 会话。
"""

import json
import os
import sys
import time


def outbox_dir() -> str:
    return os.environ.get(
        "BRAINWEAVE_CAPTURE_OUTBOX",
        os.path.join(os.path.expanduser("~"), ".brainweave", "capture-outbox"),
    )


def extract(event: dict, cli: str) -> dict:
    session_id = (
        event.get("session_id") or event.get("sessionId")
        or os.environ.get("GROK_SESSION_ID") or ""
    )
    cwd = (
        event.get("cwd") or event.get("workspaceRoot")
        or os.environ.get("GROK_WORKSPACE_ROOT") or ""
    )
    transcript_path = event.get("transcript_path") or ""
    return {
        "cli": cli,
        "session_id": str(session_id),
        "cwd": str(cwd),
        "transcript_path": str(transcript_path),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main(argv) -> int:
    cli = argv[1] if len(argv) > 1 else "unknown"
    try:
        raw = sys.stdin.read()
        try:
            event = json.loads(raw) if raw.strip() else {}
        except ValueError:
            event = {}
        if not isinstance(event, dict):
            event = {}
        marker = extract(event, cli)
        if not marker["session_id"]:
            # 没有 session 标识的事件无法寻址：不写 marker，但仍 exit 0。
            print("{}", flush=True)
            return 0
        directory = outbox_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "markers.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(marker, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - hook 绝不能把异常抛给 CLI
        print(f"bw_hook: {e}", file=sys.stderr, flush=True)
    print("{}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

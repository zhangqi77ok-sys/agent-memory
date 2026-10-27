"""CLI 自动采集（第 39 轮）：把 Codex / Grok / Gemini 一次会话的轨迹
变成一条标准 episode，供 harvester 提交到 /v1/episodes。

设计要点（详见 round39-design-draft.md）：
- 纯函数：不碰 DB、不碰网络，测试可直接 import。
- 两档解析：细粒度抽取是 best-effort（各家 transcript 格式多为观测行为
  而非稳定契约），解不出时一定回落到粗粒度——会话绝不因格式变化而丢失。
- 脱敏前置：建 episode 时对 title/steps/cost 全量过 app.redact.redact；
  服务端 submit_episode 入库时还会再脱敏一次（纵深）。
- caps 与服务端 pydantic 对齐：title 500、steps 200、action 500、
  observation/error 2000、cost 20 键且序列化 4096 字符。
"""

import json
import os
import subprocess

SUPPORTED_CLIS = ("codex", "gemini", "grok")

MAX_TITLE = 500
MAX_STEPS = 200
MAX_ACTION = 500
MAX_OBSERVATION = 2000
MAX_COST_KEYS = 20
MAX_COST_BYTES = 4096


def dedup_key(cli: str, session_id: str) -> str:
    """同一会话重 harvest 的去重主键（经 REST Idempotency-Key header 递交）。"""
    return f"bw-cap:{cli}:{session_id}"[:128]


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n]


def derive_project(cwd: str | None) -> str | None:
    """project 取自 cwd：git 顶层目录 basename，失败回落 cwd basename。

    list 参数调 git，不走 shell；任何失败都回落，不抛错（采集不能因为
    project 推导失败而丢掉整条轨迹）。
    """
    if not cwd:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            name = os.path.basename(out.stdout.strip().rstrip("/"))
            if name:
                return name[:128]
    except Exception:
        pass
    name = os.path.basename(os.path.abspath(cwd).rstrip("/"))
    return name[:128] if name else None


def _redact_text(text: str) -> str:
    from app.redact import redact

    return redact(text)


def _step(action: str, observation: str | None = None,
          error: str | None = None) -> dict:
    step = {"action": _cap(_redact_text(action), MAX_ACTION)}
    if observation:
        step["observation"] = _cap(_redact_text(observation), MAX_OBSERVATION)
    if error:
        step["error"] = _cap(_redact_text(error), MAX_OBSERVATION)
    return step


def _first_user_text(candidates: list[str]) -> str | None:
    for c in candidates:
        if c and c.strip():
            return c.strip()
    return None


def _tool_summary(name: str, args) -> str:
    if isinstance(args, dict):
        keys = ", ".join(str(k) for k in list(args.keys())[:4])
        return f"调用 {name}（{keys}）" if keys else f"调用 {name}"
    if isinstance(args, str) and args.strip():
        return f"调用 {name}（{args.strip()[:80]}）"
    return f"调用 {name}"


# ---------------------------------------------------------------------------
# 细粒度抽取：三家 transcript 各走一个容忍式解析器，只认通用形状。
# ---------------------------------------------------------------------------

def parse_codex_rollout(text: str) -> tuple[list[str], list[dict]]:
    """Codex rollout JSONL（$CODEX_HOME/sessions/.../rollout-*.jsonl）。

    返回 (用户 prompt 列表, 原始步骤列表)。布局是观测行为：首行 session_meta
    之后混排 event_msg / response_item 等记录；不认识的记录与坏行一律跳过。
    """
    prompts: list[str] = []
    raw_steps: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        rtype = str(rec.get("type", ""))
        if rtype == "session_meta":
            continue
        payload = rec.get("payload", rec)
        if not isinstance(payload, dict):
            continue
        item = payload.get("item", payload)
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type", ""))
        if itype == "message":
            role = str(item.get("role", ""))
            content = item.get("content")
            txt = _flatten_text(content)
            if role == "user" and txt:
                prompts.append(txt)
            elif role == "assistant" and txt:
                raw_steps.append({"action": f"回复：{txt[:200]}"})
        elif itype == "function_call":
            name = str(item.get("name", "") or "工具")
            raw_steps.append({"action": _tool_summary(name, item.get("arguments"))})
        elif itype == "function_call_output":
            out = item.get("output")
            txt = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False) if out is not None else ""
            if txt:
                raw_steps.append({"action": "工具输出", "observation": txt})
        elif itype == "event_msg":
            msg = item.get("message")
            txt = msg if isinstance(msg, str) else _flatten_text(msg)
            if txt:
                raw_steps.append({"action": txt})
        # reasoning / turn_context / compacted / world_state 等：一律跳过
    return prompts, raw_steps


def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p.get("content"), str):
                    parts.append(p["content"])
        return "".join(parts)
    return ""


def parse_gemini_transcript(data) -> tuple[list[str], list[dict]]:
    """Gemini transcript JSON（hook 输入的 transcript_path 所指文件）。

    接受：对象（含 messages/turns/history 列表）或 JSONL 文本。
    每条按 role（user/model）与 parts（text / functionCall /
    functionResponse）抽取；结构对不上返回空，调用方回落粗粒度。
    """
    records: list[dict] = []
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return [], []
        try:
            obj = json.loads(text)
            records = _gemini_records(obj)
        except (json.JSONDecodeError, ValueError):
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    elif isinstance(data, dict):
        records = _gemini_records(data)
    elif isinstance(data, list):
        records = [r for r in data if isinstance(r, dict)]
    prompts: list[str] = []
    raw_steps: list[dict] = []
    for rec in records:
        role = str(rec.get("role", ""))
        parts = rec.get("parts", rec.get("content"))
        if isinstance(parts, str):
            parts = [{"text": parts}]
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str) and part["text"].strip():
                if role == "user":
                    prompts.append(part["text"])
                elif role == "model":
                    raw_steps.append({"action": f"回复：{part['text'][:200]}"})
            fc = part.get("functionCall") or part.get("function_call")
            if isinstance(fc, dict):
                raw_steps.append({"action": _tool_summary(
                    str(fc.get("name", "") or "工具"), fc.get("args"))})
            fr = part.get("functionResponse") or part.get("function_response")
            if isinstance(fr, dict):
                resp = fr.get("response")
                txt = resp if isinstance(resp, str) else (
                    json.dumps(resp, ensure_ascii=False) if resp is not None else "")
                if txt:
                    raw_steps.append({"action": "工具输出", "observation": txt})
    return prompts, raw_steps


def _gemini_records(obj) -> list[dict]:
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ("messages", "turns", "history", "transcript"):
        val = obj.get(key)
        if isinstance(val, list):
            return [r for r in val if isinstance(r, dict)]
    return [obj]


def parse_grok_session(data) -> tuple[list[str], list[dict]]:
    """Grok session 文件（~/.grok/sessions/ 下，含 prompts/responses/tool calls）。

    盘上格式未从官方文档确立，只接受通用形状：
    {"role","text"} / {"tool","input","output","error"} / {"prompt"} /
    {"response"}；其余一律跳过，解不出返回空 → 回落粗粒度。
    """
    records: list[dict] = []
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return [], []
        try:
            obj = json.loads(text)
            records = _grok_records(obj)
        except (json.JSONDecodeError, ValueError):
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    elif isinstance(data, dict):
        records = _grok_records(data)
    elif isinstance(data, list):
        records = [r for r in data if isinstance(r, dict)]
    prompts: list[str] = []
    raw_steps: list[dict] = []
    for rec in records:
        role = str(rec.get("role", ""))
        if isinstance(rec.get("prompt"), str) and rec["prompt"].strip():
            prompts.append(rec["prompt"])
        if isinstance(rec.get("text"), str) and rec["text"].strip():
            if role == "user":
                prompts.append(rec["text"])
            elif role in ("assistant", "model"):
                raw_steps.append({"action": f"回复：{rec['text'][:200]}"})
        if isinstance(rec.get("response"), str) and rec["response"].strip():
            raw_steps.append({"action": f"回复：{rec['response'][:200]}"})
        tool = rec.get("tool") or rec.get("tool_name")
        if tool:
            raw_steps.append({"action": _tool_summary(str(tool), rec.get("input") or rec.get("args"))})
            out = rec.get("output")
            if isinstance(out, str) and out.strip():
                raw_steps.append({"action": "工具输出", "observation": out})
            err = rec.get("error")
            if isinstance(err, str) and err.strip():
                raw_steps.append({"action": f"工具 {tool} 报错", "error": err})
    return prompts, raw_steps


def _grok_records(obj) -> list[dict]:
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ("messages", "turns", "history", "entries", "session"):
        val = obj.get(key)
        if isinstance(val, list):
            return [r for r in val if isinstance(r, dict)]
    return [obj]


PARSERS = {
    "codex": lambda text: parse_codex_rollout(text),
    "gemini": lambda data: parse_gemini_transcript(data),
    "grok": lambda data: parse_grok_session(data),
}


def build_episode(cli: str, session_id: str, transcript, cwd: str | None = None,
                  exit_code: int | None = None,
                  wrapper_output: str | None = None,
                  prompt: str | None = None) -> dict:
    """由 transcript（或 wrapper 输出）建成标准 episode 载荷。

    transcript 为 str 时按该 CLI 的解析器走；wrapper 路线传 exit_code 与
    wrapper_output，走粗粒度。返回 dict 可直接作 /v1/episodes 请求体
    （另加 Idempotency-Key header，见 dedup_key）。
    """
    prompts: list[str] = []
    raw_steps: list[dict] = []
    if transcript is not None:
        parser = PARSERS.get(cli)
        if parser is not None:
            try:
                prompts, raw_steps = parser(transcript)
            except Exception:
                prompts, raw_steps = [], []
    title = _first_user_text(prompts)
    if title is None and prompt and prompt.strip():
        title = prompt.strip()
    if title is None and wrapper_output:
        title = wrapper_output.strip().splitlines()[0] if wrapper_output.strip() else None
    if title is None:
        title = f"{cli} 会话 {session_id[:8]}"
    steps = [_step(s.get("action", ""), s.get("observation"), s.get("error"))
             for s in raw_steps if s.get("action")]
    if wrapper_output and wrapper_output.strip():
        steps.append(_step(f"执行 {cli} 完成",
                           observation=wrapper_output.strip()[-4000:]))
    if exit_code is not None and exit_code != 0:
        steps.append(_step(f"{cli} 退出码 {exit_code}", error="非 0 退出"))
    if not steps:
        steps = [_step(f"{cli} 会话 {session_id[:8]}")]
    steps = steps[:MAX_STEPS]
    has_error = any(s.get("error") for s in steps)
    if exit_code is not None and exit_code != 0:
        outcome = "failure"
    elif has_error:
        outcome = "partial"
    else:
        outcome = "success"
    cost: dict = {}
    payload = {
        "title": _cap(_redact_text(title), MAX_TITLE),
        "outcome": outcome,
        "steps": steps,
        "cost": cost,
        "project": derive_project(cwd),
        "source": f"{cli}-hook" if exit_code is None else f"{cli}-wrap",
    }
    return payload


def title_from_marker(cli: str, session_id: str) -> str:
    return f"{cli} 会话 {session_id[:8]}"

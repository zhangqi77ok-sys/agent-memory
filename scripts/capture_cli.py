#!/usr/bin/env python3
"""BrainWeave CLI 采集工具（第 39 轮）。

子命令：
  install    往三家 CLI 的用户层配置写 SessionEnd hook（opt-in 本身）
  uninstall  只删自家 hook 条目，不动用户自己的 hook
  harvest    按 outbox marker 找 transcript、建 episode、POST /v1/episodes
  run        wrapper：跑一次 CLI 子进程，把任务+结果拼成粗粒度 episode 提交

提交走 REST（SERVICE_KEY + --user），不依赖 MCP 服务在线。
服务端门禁直接复用：未同意/已暂停 → skipped（marker 进 .skipped/，
不再重试）；已遗忘内容 → 409 tombstoned（marker 进 .tombstoned/）。

环境变量：
  SERVICE_KEY                  服务密钥（必填，harvest/run）
  BRAINWEAVE_CAPTURE_OUTBOX    outbox 目录（默认 ~/.brainweave/capture-outbox）
  BRAINWEAVE_CAPTURE_OFF=1     harvest 只打印将提交什么，不 POST
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capture import SUPPORTED_CLIS, build_episode, dedup_key  # noqa: E402

HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "capture-hooks", "bw_hook.py")
MAX_ATTEMPTS = 5


def outbox_dir() -> str:
    return os.environ.get(
        "BRAINWEAVE_CAPTURE_OUTBOX",
        os.path.join(os.path.expanduser("~"), ".brainweave", "capture-outbox"),
    )


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _hook_command(cli: str) -> str:
    return f"python3 {HOOK_SCRIPT} {cli}"


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

def _codex_hooks_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "hooks.json")


def _gemini_settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".gemini", "settings.json")


def _grok_hook_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".grok", "hooks", "brainweave-capture.json")


def _group_has_command(group: dict, command: str) -> bool:
    for hook in group.get("hooks", []) if isinstance(group, dict) else []:
        if isinstance(hook, dict) and hook.get("command") == command:
            return True
    return False


def install() -> int:
    cmd_codex, cmd_gemini, cmd_grok = (_hook_command(c) for c in SUPPORTED_CLIS)
    # Codex: ~/.codex/hooks.json，SessionEnd 组（上限 3 秒，hook 只写 marker）
    path = _codex_hooks_path()
    data = _load_json(path)
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("SessionEnd", [])
    if not any(_group_has_command(g, cmd_codex) for g in groups):
        groups.append({"hooks": [{"type": "command", "command": cmd_codex, "timeout": 3}]})
        _save_json(path, data)
        print(f"codex: 已写入 {path}")
    else:
        print("codex: 已存在，跳过")
    # Gemini: ~/.gemini/settings.json，SessionEnd 组
    path = _gemini_settings_path()
    data = _load_json(path)
    hooks = data.setdefault("hooks", {})
    groups = hooks.setdefault("SessionEnd", [])
    if not any(_group_has_command(g, cmd_gemini) for g in groups):
        groups.append({"matcher": "*",
                       "hooks": [{"name": "brainweave-capture", "type": "command",
                                  "command": cmd_gemini}]})
        _save_json(path, data)
        print(f"gemini: 已写入 {path}")
    else:
        print("gemini: 已存在，跳过")
    # Grok: ~/.grok/hooks/brainweave-capture.json（整文件即自家，幂等写）
    path = _grok_hook_path()
    data = {"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command", "command": cmd_grok, "timeout": 5}]}]}}
    if _load_json(path) != data:
        _save_json(path, data)
        print(f"grok: 已写入 {path}")
    else:
        print("grok: 已存在，跳过")
    print("注意：Codex 的非托管 hook 须在 TUI 里用 /hooks 按哈希复核信任后"
          "才运行；codex exec 下未受信 hook 会被静默跳过。")
    print("采集默认只在 hook 装好后生效；关停用 uninstall，或在服务端关记忆开关。")
    return 0


def _strip_command(groups: list, command: str) -> list:
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        hooks = [h for h in group.get("hooks", [])
                 if not (isinstance(h, dict) and h.get("command") == command)]
        if hooks:
            group = dict(group)
            group["hooks"] = hooks
            kept.append(group)
    return kept


def uninstall() -> int:
    cmd_codex, cmd_gemini, _ = (_hook_command(c) for c in SUPPORTED_CLIS)
    for path, command in ((_codex_hooks_path(), cmd_codex),
                          (_gemini_settings_path(), cmd_gemini)):
        data = _load_json(path)
        hooks = data.get("hooks", {})
        if "SessionEnd" in hooks:
            hooks["SessionEnd"] = _strip_command(hooks["SessionEnd"], command)
            if not hooks["SessionEnd"]:
                del hooks["SessionEnd"]
            _save_json(path, data)
            print(f"已清理 {path}")
    path = _grok_hook_path()
    if os.path.exists(path):
        os.remove(path)
        print(f"已删除 {path}")
    return 0


# ---------------------------------------------------------------------------
# transcript 定位
# ---------------------------------------------------------------------------

def resolve_transcript(marker: dict):
    """按 marker 找 transcript 文本；找不到返回 (None, 原因)。"""
    cli = marker.get("cli", "")
    session_id = marker.get("session_id", "")
    path = marker.get("transcript_path", "")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), None
    if cli == "codex" and session_id:
        home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        sessions = os.path.join(home, "sessions")
        for root, _dirs, files in os.walk(sessions):
            for name in files:
                if name.startswith("rollout-") and session_id in name and name.endswith(".jsonl"):
                    full = os.path.join(root, name)
                    with open(full, encoding="utf-8", errors="replace") as f:
                        return f.read(), None
        return None, "codex rollout 未找到"
    if cli == "grok" and session_id:
        sessions = os.path.join(os.path.expanduser("~"), ".grok", "sessions")
        for root, _dirs, files in os.walk(sessions):
            for name in files:
                if session_id in name:
                    full = os.path.join(root, name)
                    with open(full, encoding="utf-8", errors="replace") as f:
                        return f.read(), None
        return None, "grok session 文件未找到"
    if cli == "gemini":
        return None, "gemini transcript_path 缺失或文件不存在"
    return None, f"{cli} transcript 未定位"


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------

def post_episode(base: str, key: str, user_id: str, payload: dict,
                 idem: str) -> tuple[str, dict]:
    """返回 (结论, 服务端 JSON)：ok / skipped / duplicate / tombstoned / failed。"""
    body = dict(payload)
    body["user_id"] = user_id
    body["tombstone_check"] = True
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/episodes",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "X-Service-Key": key,
                 "Idempotency-Key": idem},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode())
        except Exception:
            data = {}
        if e.code == 409 and isinstance(data.get("detail"), dict) \
                and data["detail"].get("code") == "tombstoned":
            return "tombstoned", data
        if 500 <= e.code < 600:
            # 服务端 5xx 是瞬态：交 harvester 留原地重试，不直接判 failed。
            return "retry", {"status": e.code, "detail": data}
        return "failed", {"status": e.code, "detail": data}
    if data.get("skipped"):
        return "skipped", data
    if data.get("duplicate"):
        return "duplicate", data
    return "ok", data


def _move_marker(directory: str, marker: dict, sub: str, reason: str = "") -> None:
    target = os.path.join(directory, sub)
    os.makedirs(target, exist_ok=True)
    name = f"{marker.get('cli', 'x')}-{marker.get('session_id', 'unknown')[:16]}.json"
    if reason:
        marker = dict(marker)
        marker["reason"] = reason
    with open(os.path.join(target, name), "w", encoding="utf-8") as f:
        json.dump(marker, f, ensure_ascii=False, indent=2)


def harvest(base: str, key: str, user_id: str, dry_run: bool) -> int:
    directory = outbox_dir()
    markers_path = os.path.join(directory, "markers.jsonl")
    ledger_path = os.path.join(directory, "ledger.json")
    ledger = _load_json(ledger_path)
    try:
        with open(markers_path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        print("outbox 为空：没有 marker")
        return 0
    remaining = []
    counts = {"ok": 0, "duplicate": 0, "skipped": 0, "tombstoned": 0,
              "failed": 0, "unresolved": 0, "kept": 0}
    for line in lines:
        try:
            marker = json.loads(line)
        except ValueError:
            continue
        if not isinstance(marker, dict):
            continue
        cli, session_id = marker.get("cli", ""), marker.get("session_id", "")
        skey = dedup_key(cli, session_id) if cli and session_id else ""
        if skey and skey in ledger:
            counts["duplicate"] += 1
            continue
        transcript, reason = resolve_transcript(marker)
        if transcript is None:
            _move_marker(directory, marker, ".unresolved", reason or "未定位")
            counts["unresolved"] += 1
            continue
        payload = build_episode(cli, session_id, transcript, cwd=marker.get("cwd") or None)
        if dry_run:
            print(f"[dry-run] {cli} {session_id[:16]}: {payload['title'][:80]} "
                  f"({payload['outcome']}, {len(payload['steps'])} 步, key={skey})")
            remaining.append(line)
            counts["kept"] += 1
            continue
        try:
            verdict, data = post_episode(base, key, user_id, payload, skey)
        except Exception as e:  # 网络错：留原地，下次再试
            marker["attempts"] = int(marker.get("attempts", 0)) + 1
            if marker["attempts"] >= MAX_ATTEMPTS:
                _move_marker(directory, marker, ".failed", f"提交失败 {MAX_ATTEMPTS} 次：{e}")
                counts["failed"] += 1
            else:
                remaining.append(json.dumps(marker, ensure_ascii=False))
                counts["kept"] += 1
            continue
        if verdict == "retry":  # 服务端 5xx：与网络错同款处理，不直接判 failed
            marker["attempts"] = int(marker.get("attempts", 0)) + 1
            if marker["attempts"] >= MAX_ATTEMPTS:
                _move_marker(directory, marker, ".failed",
                             f"服务端 5xx {MAX_ATTEMPTS} 次：{data}")
                counts["failed"] += 1
            else:
                remaining.append(json.dumps(marker, ensure_ascii=False))
                counts["kept"] += 1
            continue
        if verdict == "ok":
            ledger[skey] = {"episode_id": data.get("episode_id")}
            counts["ok"] += 1
            print(f"已提交 {cli} {session_id[:16]} -> {data.get('episode_id')} "
                  f"(key={skey})")
        elif verdict == "duplicate":
            ledger[skey] = {"episode_id": data.get("episode_id")}
            counts["duplicate"] += 1
        elif verdict == "skipped":
            _move_marker(directory, marker, ".skipped", "记忆开关关闭")
            counts["skipped"] += 1
        elif verdict == "tombstoned":
            _move_marker(directory, marker, ".tombstoned", "内容已被遗忘")
            counts["tombstoned"] += 1
        else:
            _move_marker(directory, marker, ".failed", str(data))
            counts["failed"] += 1
    with open(markers_path, "w", encoding="utf-8") as f:
        for ln in remaining:
            f.write(ln + "\n")
    _save_json(ledger_path, ledger)
    print(f"harvest 完成：{counts}")
    return 0


def run_wrapper(base: str, key: str, user_id: str, cli: str, cmd: list,
                timeout: int, dry_run: bool) -> int:
    prompt = _extract_prompt(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out, code = proc.stdout or "", proc.returncode
        err = proc.stderr or ""
    except subprocess.TimeoutExpired:
        out, code, err = "", 124, f"wrapper 超时（{timeout}s）"
    except OSError as e:
        # CLI 未安装等：记成 failure episode，不让 wrapper 自身崩溃
        out, code, err = "", 127, f"无法启动 {cmd[0]}：{e}"
    combined = (out + ("\n" + err if code != 0 and err else "")).strip()
    import hashlib
    cmd_digest = hashlib.sha256(" ".join(cmd).encode()).hexdigest()[:16]
    payload = build_episode(cli, f"wrap-{cmd_digest}",
                            None, cwd=os.getcwd(), exit_code=code,
                            wrapper_output=combined[-8000:] or None,
                            prompt=prompt)
    # wrapper 没有稳定 session_id：key 取命令摘要，同命令重跑视为重复提交；
    # 不同命令的摘要冲突概率可忽略。见设计稿 §7 边界。
    skey = f"bw-cap:{cli}:wrap:{cmd_digest}"
    if dry_run:
        print(f"[dry-run] {cli}: {payload['title'][:80]} "
              f"({payload['outcome']}, {len(payload['steps'])} 步)")
        return 0
    try:
        verdict, data = post_episode(base, key, user_id, payload, skey)
    except Exception as e:
        print(f"提交失败：{e}")
        return 1
    print(f"wrapper 提交：{verdict} {data}")
    return 0 if verdict in ("ok", "duplicate") else 1


def _extract_prompt(cmd: list) -> str | None:
    for i, arg in enumerate(cmd):
        if arg in ("-p", "--prompt") and i + 1 < len(cmd):
            return cmd[i + 1]
    for arg in cmd[1:]:
        if not arg.startswith("-"):
            return arg
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BrainWeave CLI 采集工具")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("harvest")
    run_p = sub.add_parser("run")
    run_p.add_argument("--cli", choices=SUPPORTED_CLIS, required=True)
    run_p.add_argument("--timeout", type=int, default=600)
    run_p.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    key = os.environ.get("SERVICE_KEY", "")
    if not key:
        print("需要 SERVICE_KEY 环境变量", file=sys.stderr)
        return 2
    if not args.user:
        print("需要 --user", file=sys.stderr)
        return 2
    dry_run = os.environ.get("BRAINWEAVE_CAPTURE_OFF") == "1"
    if args.command == "harvest":
        return harvest(args.base, key, args.user, dry_run)
    if args.command == "run":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("run 需要 CLI 命令", file=sys.stderr)
            return 2
        return run_wrapper(args.base, key, args.user, args.cli, cmd,
                           args.timeout, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

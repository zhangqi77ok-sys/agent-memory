#!/usr/bin/env python3
"""轨迹批量导入器（第十二轮）：冷启动用——把已有的任务记录一次性灌成 episode，
经验库第一天就不是空的，不必靠工具慢慢攒。

输入是 JSONL，每行一条轨迹（本系统自己的提交形状，各工具的私有日志格式请先自行转成它）：
  {"title": "部署服务", "outcome": "partial", "project": "repo-a",
   "steps": [{"action": "在 8080 启动", "error": "EADDRINUSE"}, {"action": "改用 3000"}],
   "cost": {"tokens": 400}, "idempotency_key": "ci-run-1234"}
title 必填；outcome 只认 success|failure|partial（缺省 success）；steps/cost 缺省空。
带 idempotency_key 的行重复导入不会重复落库，放心重跑。

用法：
  SERVICE_KEY=<服务密钥> python3 scripts/import_episodes.py \
      --base http://127.0.0.1:8000 --user alice --file history.jsonl [--dry-run]
导入后由 Worker 正常提炼；进度看 /metrics 的 agent_memory_episode_backlog。
"""
import argparse
import json
import os
import sys
import urllib.request

OUTCOMES = ("success", "failure", "partial")


def parse_lines(text: str) -> tuple[list[dict], list[str]]:
    """逐行解析校验。坏行不整批失败：返回 (有效记录, 错误说明列表)。"""
    valid, errors = [], []
    for no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"第 {no} 行：不是合法 JSON（{e.msg}）")
            continue
        if not isinstance(rec, dict) or not str(rec.get("title") or "").strip():
            errors.append(f"第 {no} 行：缺 title")
            continue
        outcome = rec.get("outcome") or "success"
        if outcome not in OUTCOMES:
            errors.append(f"第 {no} 行：outcome 只认 {'|'.join(OUTCOMES)}")
            continue
        steps = rec.get("steps") or []
        if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
            errors.append(f"第 {no} 行：steps 必须是对象数组")
            continue
        valid.append({
            "title": str(rec["title"])[:500],
            "outcome": outcome,
            "source": str(rec.get("source") or "import")[:32],
            "project": (str(rec["project"])[:128] if rec.get("project") else None),
            "steps": steps[:200],
            "cost": rec.get("cost") if isinstance(rec.get("cost"), dict) else {},
            "idempotency_key": (str(rec["idempotency_key"])[:128]
                                if rec.get("idempotency_key") else None),
        })
    return valid, errors


def build_payload(rec: dict, user_id: str) -> dict:
    body = {"user_id": user_id, "source": rec["source"], "title": rec["title"],
            "outcome": rec["outcome"], "steps": rec["steps"], "cost": rec["cost"]}
    if rec.get("project"):
        body["project"] = rec["project"]
    return body


def post_episode(base: str, key: str, rec: dict, user_id: str) -> dict:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/episodes",
        data=json.dumps(build_payload(rec, user_id), ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-Service-Key": key},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="批量导入任务轨迹（JSONL）为 episode")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址")
    ap.add_argument("--user", required=True, help="记忆归属的 user_id")
    ap.add_argument("--file", required=True, help="JSONL 文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只校验不提交")
    args = ap.parse_args(argv)

    text = open(args.file, encoding="utf-8").read()
    valid, errors = parse_lines(text)
    for e in errors:
        print(f"[skip] {e}", file=sys.stderr)
    print(f"有效 {len(valid)} 条，跳过 {len(errors)} 条")
    if args.dry_run:
        return 0 if valid or not errors else 1
    key = os.environ.get("SERVICE_KEY") or os.environ.get("SERVICE_KEYS", "").split(",")[0]
    if not key:
        print("缺服务密钥：设环境变量 SERVICE_KEY", file=sys.stderr)
        return 2
    ok = dup = skipped = failed = 0
    for rec in valid:
        try:
            r = post_episode(args.base, key, rec, args.user)
        except Exception as e:  # 网络/服务端错误逐条报，不中断整批
            failed += 1
            print(f"[fail] {rec['title'][:40]}: {e}", file=sys.stderr)
            continue
        if r.get("skipped"):
            skipped += 1
        elif r.get("duplicate"):
            dup += 1
        else:
            ok += 1
    print(f"导入完成：新建 {ok}，重复 {dup}，未开启记忆跳过 {skipped}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

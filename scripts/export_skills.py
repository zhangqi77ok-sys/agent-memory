#!/usr/bin/env python3
"""把经验编译成 Skills 文件落盘（第十三轮）。

从服务拉取高置信经验渲染好的 SKILL.md / AGENTS.md，写进目标目录（通常是某个
代码仓库根目录）：编程工具（Claude Code / Codex / Gemini CLI 等）能直接把这些
从历史任务提炼出的教训、策略、避坑当作原生 skill / 项目说明加载。

用法：
  SERVICE_KEY=<服务密钥> python3 scripts/export_skills.py \
      --base http://127.0.0.1:8000 --user alice --out /path/to/repo [--project repo-a]
重复运行会覆盖同名文件（内容由经验库当前状态决定），可放进定时任务保持新鲜。
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request


def fetch_files(base: str, key: str, user_id: str,
                project: str | None, min_confidence: float) -> dict:
    q = {"user_id": user_id, "min_confidence": str(min_confidence)}
    if project:
        q["project"] = project
    url = f"{base.rstrip('/')}/v1/me/skills?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"X-Service-Key": key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["files"]


def write_files(files: dict, out_dir: str) -> list[str]:
    root = pathlib.Path(out_dir)
    written = []
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把高置信经验导出为 Skills 文件")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址")
    ap.add_argument("--user", required=True, help="记忆归属的 user_id")
    ap.add_argument("--out", required=True, help="落盘目录（通常是仓库根目录）")
    ap.add_argument("--project", default=None, help="只导出某个项目（缺省全部）")
    ap.add_argument("--min-confidence", type=float, default=0.5, help="入选置信度门槛")
    args = ap.parse_args(argv)

    key = os.environ.get("SERVICE_KEY") or os.environ.get("SERVICE_KEYS", "").split(",")[0]
    if not key:
        print("缺服务密钥：设环境变量 SERVICE_KEY", file=sys.stderr)
        return 2
    files = fetch_files(args.base, key, args.user, args.project, args.min_confidence)
    if not files:
        print("没有符合条件的经验（调低 --min-confidence 或先攒一些经验）")
        return 0
    written = write_files(files, args.out)
    for rel in written:
        print(f"[write] {rel}")
    print(f"完成：{len(written)} 个文件 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

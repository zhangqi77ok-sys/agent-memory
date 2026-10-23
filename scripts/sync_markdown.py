#!/usr/bin/env python3
"""记忆 Markdown 同步（第十三轮）：在服务与本地目录之间同步人可读的记忆文件。

  --pull  从服务拉取最新 .md 写进目录（事实按主题、偏好单文件、经验按主题，均带条目 id）
  --push  把目录里的 .md 导回服务：在编辑器里改过的内容按 id 生效；新写的事实行 /
          偏好行 / `<!-- new -->` 经验块会新建；从文件里删掉的条目**只报告不删库**
          （真删除请走面板或 forget，避免误删文件丢记忆）。

目录本身可以是一个 git 仓库——记忆的变化从此有 diff、有历史。

用法：
  SERVICE_KEY=<服务密钥> python3 scripts/sync_markdown.py --pull \
      --base http://127.0.0.1:8000 --user alice --dir ./my-memory
  SERVICE_KEY=<服务密钥> python3 scripts/sync_markdown.py --push \
      --base http://127.0.0.1:8000 --user alice --dir ./my-memory
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request


def _req(base: str, key: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=data, method="POST" if data else "GET",
        headers={"X-Service-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def pull(base: str, key: str, user: str, out_dir: str) -> int:
    q = urllib.parse.urlencode({"user_id": user})
    files = _req(base, key, f"/v1/me/markdown?{q}")["files"]
    root = pathlib.Path(out_dir)
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        print(f"[pull] {rel}")
    print(f"完成：{len(files)} 个文件 → {out_dir}")
    return 0


def push(base: str, key: str, user: str, in_dir: str) -> int:
    root = pathlib.Path(in_dir)
    files = {str(p.relative_to(root)): p.read_text(encoding="utf-8")
             for p in sorted(root.rglob("*.md"))}
    if not files:
        print(f"{in_dir} 下没有 .md 文件", file=sys.stderr)
        return 1
    q = urllib.parse.urlencode({"user_id": user})
    report = _req(base, key, f"/v1/me/markdown/import?{q}", {"files": files})
    print(f"导回完成：更新 {report['updated']}，新建 {report['created']}，"
          f"未变 {report['unchanged']}，脱敏改写 {report['redacted']}")
    if report["missing_in_files"]:
        print(f"注意：{len(report['missing_in_files'])} 条现行记忆不在文件里"
              "（只报告，未删除）——要真删请走面板或 forget")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="记忆 Markdown 双向同步")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址")
    ap.add_argument("--user", required=True, help="记忆归属的 user_id")
    ap.add_argument("--dir", required=True, help="本地记忆目录")
    ap.add_argument("--pull", action="store_true", help="服务 → 目录")
    ap.add_argument("--push", action="store_true", help="目录 → 服务")
    args = ap.parse_args(argv)
    if args.pull == args.push:
        print("必须且只能选 --pull 或 --push", file=sys.stderr)
        return 2
    key = os.environ.get("SERVICE_KEY") or os.environ.get("SERVICE_KEYS", "").split(",")[0]
    if not key:
        print("缺服务密钥：设环境变量 SERVICE_KEY", file=sys.stderr)
        return 2
    return pull(args.base, key, args.user, args.dir) if args.pull else \
        push(args.base, key, args.user, args.dir)


if __name__ == "__main__":
    raise SystemExit(main())

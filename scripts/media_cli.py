#!/usr/bin/env python3
"""媒体备忘提交脚本（第 40 轮，stdlib-only，与 capture_cli.py 同款）。

用法：SERVICE_KEY=... python3 scripts/media_cli.py submit \
    --kind screenshot --origin ocr --title "登录接口 500" \
    --text-file /tmp/ocr.txt --original /tmp/err.png --user <user_id>

- --text-file：文本层文件（.txt 原文；.srt/.vtt 剥时间戳行与序号行，只留文本）。
- --original：原文件路径（可选）。只在本地算 sha256 + 大小 + 按扩展名猜
  mime 作元数据提交；原文件字节永不上传（服务端不存 blob，与设计一致）。
- 提交走 REST（SERVICE_KEY + --user），不依赖 MCP 服务在线。
- 读不到文本层文件时直接非 0 退出并打印错误，不提交空备忘。
"""

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request

BASE_ENV = "BRAINWEAVE_BASE_URL"
DEFAULT_BASE = "http://127.0.0.1:8000"

_SRT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
_SRT_INDEX = re.compile(r"^\d+$")


def strip_transcript_lines(text: str) -> str:
    """剥 .srt/.vtt 的时间戳行、序号行与 WEBVTT 头，只留文本行。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or _SRT_INDEX.match(s) or _SRT_TIME.match(s):
            continue
        if s.startswith("NOTE ") or s.startswith("STYLE"):
            continue
        out.append(s)
    return "\n".join(out)


def read_text_layer(path: str) -> str:
    """读文本层文件：.srt/.vtt 先剥时间戳，.txt 原文。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith((".srt", ".vtt")):
        text = strip_transcript_lines(text)
    return text


def original_meta(path: str) -> dict:
    """原文件元数据（本地算，不上传字节）：大小、sha256、扩展名猜 mime。"""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    mime, _enc = mimetypes.guess_type(path)
    return {"filename": os.path.basename(path), "mime": mime,
            "byte_size": size, "sha256": h.hexdigest()}


def post_media(base: str, key: str, user_id: str, payload: dict,
               idempotency_key: str | None = None) -> tuple[int, dict]:
    body = json.dumps({**payload, "user_id": user_id}).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(f"{base.rstrip('/')}/v1/media", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="提交一条媒体备忘（截图/语音复盘）")
    p.add_argument("cmd", choices=["submit"])
    p.add_argument("--kind", required=True, choices=["screenshot", "voice"])
    p.add_argument("--origin", required=True,
                   choices=["ocr", "transcript", "note", "external"])
    p.add_argument("--title", required=True)
    p.add_argument("--text-file", required=True)
    p.add_argument("--text", default="")
    p.add_argument("--original", default="")
    p.add_argument("--outcome", default="partial",
                   choices=["success", "failure", "partial"])
    p.add_argument("--project", default="")
    p.add_argument("--user", required=True)
    p.add_argument("--base", default=os.environ.get(BASE_ENV, DEFAULT_BASE))
    p.add_argument("--idempotency-key", default="")
    args = p.parse_args(argv)
    key = os.environ.get("SERVICE_KEY", "")
    if not key:
        print("需要 SERVICE_KEY 环境变量", file=sys.stderr)
        return 2
    try:
        text = read_text_layer(args.text_file) if args.text_file else args.text
    except OSError as e:
        print(f"读不到文本层文件：{e}", file=sys.stderr)
        return 2
    if not text.strip():
        print("文本层为空，不提交空备忘", file=sys.stderr)
        return 2
    payload = {"kind": args.kind, "origin": args.origin, "title": args.title,
               "text": text, "outcome": args.outcome,
               "project": args.project or None}
    if args.original:
        try:
            payload.update(original_meta(args.original))
        except OSError as e:
            print(f"读不到原文件（只影响元数据，仍提交）：{e}", file=sys.stderr)
    status, body = post_media(args.base, key, args.user, payload,
                              args.idempotency_key or None)
    print(json.dumps({"status": status, "body": body}, ensure_ascii=False))
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# 前端冒烟：经网关访问 /memory/ui/（不直连 web 容器端口）。
# 要求：页面 200、HTML 含应用标题「我的记忆」（不是 nginx 默认页）、其中一个 assets/*.js 返回 200。
# 失败返回非 0，由 Jenkins 触发回滚。
set -euo pipefail
URL="${SMOKE_UI_URL:-http://127.0.0.1/memory/ui/}"
TRIES="${SMOKE_UI_TRIES:-20}"
SLEEP="${SMOKE_UI_SLEEP:-3}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

check() {
  local code js origin
  code="$(curl -s -o "$TMP" -w '%{http_code}' "$URL" || true)"
  [ "$code" = 200 ] || { echo "ui page $URL -> $code"; return 1; }
  grep -q '我的记忆' "$TMP" || { echo "ui page 不含标题「我的记忆」（可能是 nginx 默认页）"; return 1; }
  js="$(grep -oE '[^"'"'"' ]*assets/[^"'"'"' ]+\.js' "$TMP" | head -n1 || true)"
  [ -n "$js" ] || { echo "ui page 里没有 assets/*.js"; return 1; }
  case "$js" in
    http://*|https://*) ;;
    /*) origin="$(printf '%s' "$URL" | grep -oE '^https?://[^/]+')"; js="$origin$js" ;;
    *) js="${URL%/}/$js" ;;
  esac
  code="$(curl -s -o /dev/null -w '%{http_code}' "$js" || true)"
  [ "$code" = 200 ] || { echo "asset $js -> $code"; return 1; }
  echo "smoke ui OK: $URL (asset $js)"
}

for i in $(seq 1 "$TRIES"); do
  if check; then exit 0; fi
  echo "smoke ui retry $i/$TRIES"; sleep "$SLEEP"
done
exit 1

#!/usr/bin/env bash
# 本地一键真库测试：与 Jenkinsfile 的 'Test on Postgres' 阶段同一口径——
# 临时 pgvector/pgvector:pg16 容器 + TEST_DATABASE_URL 让 tests/conftest.py 切到真 Postgres
# （alembic 迁移建表、memsvc_app/memsvc_worker 双角色、RLS/咨询锁/SKIP LOCKED 真实生效）。
#
# 为什么需要它：默认 pytest 跑 SQLite，PG 专属行为（列长度 VARCHAR、行级权限、锁语义）
# 在 SQLite 上永远测不出来——本项目的 P0 事故（第五、六轮）全是这一类。
# CI 里有同名阶段，但本地改完代码先跑一遍，能在推送前就拦住。
#
# 用法：scripts/test-pg.sh [pytest 的额外参数…]   需要本机 docker。
set -euo pipefail
cd "$(dirname "$0")/.."

PGC="amem-local-pg-$$"
PORT="${PG_TEST_PORT:-55432}"
cleanup() { docker rm -f "$PGC" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --rm --name "$PGC" -p "127.0.0.1:${PORT}:5432" \
  -e POSTGRES_USER=memory -e POSTGRES_PASSWORD=memory -e POSTGRES_DB=memory_test \
  pgvector/pgvector:pg16 >/dev/null

ok=0
for _ in $(seq 1 30); do
  if docker exec "$PGC" pg_isready -U memory -d memory_test >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "本地 CI Postgres 未就绪"; exit 1; }
sleep 2

if [ -x .venv/bin/activate ]; then . .venv/bin/activate; fi
# EMBEDDING_DIM=2 必带：测试里的假嵌入器是 2 维向量，PG 的 vector 列按配置维度建表，
# 不对齐会全员 DataError（expected 1536 dimensions, not 2）——2026-10-23 本机真库首跑
# 的 12 个失败全是这个。生产用自己的配置（缺省 1536，与 Gemini 输出一致），不受影响。
TEST_DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:${PORT}/memory_test" \
  EMBEDDING_DIM=2 \
  python3 -m pytest -q "$@"

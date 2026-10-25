#!/usr/bin/env bash
# 备份/恢复演练独立运行器（第三十轮，修复第 3 轮扩展）：自起自停
# scratch 集群，跑完即删。跑 tests/test_round30_drill.py（临时库
# memory_drill_*）与 tests/test_fix3_restore.py（临时库 memory_fix3_*，
# 直接跑生产 deploy/restore.sh 本文件），绝不碰生产库。
# 用法：./scripts/restore_drill.sh
# 注意：需本机 apt PostgreSQL（postgres 用户）；VM 替换后若用户丢失，
# 按 AGENTS.md 配方重装后再跑，不要改本脚本去绕。
set -u
rm -rf /tmp/pgdata-drill /tmp/pg-drill.log /tmp/pg_init-drill.log
if ! id postgres >/dev/null 2>&1; then
  echo "DRILL_FAILED: postgres user missing (VM likely replaced); reinstall per AGENTS.md recipe, then rerun"
  exit 1
fi
su postgres -c '/usr/lib/postgresql/16/bin/initdb -D /tmp/pgdata-drill -U postgres --auth=trust' > /tmp/pg_init-drill.log 2>&1 || { echo DRILL_FAILED; tail -5 /tmp/pg_init-drill.log; exit 1; }
su postgres -c '/usr/lib/postgresql/16/bin/pg_ctl -D /tmp/pgdata-drill -o "-p 55436 -k /tmp" -l /tmp/pg-drill.log start'
sleep 2
/usr/lib/postgresql/16/bin/psql -h /tmp -p 55436 -U postgres -c "CREATE USER memory SUPERUSER" -c "CREATE DATABASE memory_test OWNER memory"
cd "$(dirname "$0")/.." || exit 1
TEST_DATABASE_URL=postgresql+psycopg://memory@127.0.0.1:55436/memory_test EMBEDDING_DIM=2 .venv/bin/python -m pytest tests/test_round30_drill.py tests/test_fix3_restore.py -q
DRILL_EXIT=$?
su postgres -c '/usr/lib/postgresql/16/bin/pg_ctl -D /tmp/pgdata-drill stop' || true
rm -rf /tmp/pgdata-drill
echo "DRILL_EXIT=$DRILL_EXIT"
exit $DRILL_EXIT

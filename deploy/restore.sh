#!/usr/bin/env bash
# 恢复：pg_restore → 合并墓碑（线上库 + sidecar + 备份自带三源取并集）
#   → 按墓碑重删 → alembic 校验 → readyz。
# 为什么要合并线上墓碑：备份可能早于某次遗忘，备份里的墓碑表不含这次
# 遗忘，只用备份自己的表会让已遗忘的数据「复活」。活库损坏时线上取数
# 为空，sidecar（备份时与 dump 分离导出的墓碑文件）就是最后的防线。
#
# 重删口径（修复第 3 轮）：
#   - id 段（精确主路径）：forgotten_id 中五类（fact / preference /
#     insight / episode / reminder）的 id 命中即删，并同删派生数据：
#     事实/经验的 token 行、触及被删事实/经验节点的关联边、指向被删
#     经验的交付边。偏好无派生表；轨迹不级联删派生经验（经验是独立
#     记忆，各有自己的墓碑）；提醒无派生表。brief_delivery 的 JSON
#     账本不改，与 forget 入口行为一致。
#   - 哈希段（第二道）：forgotten_content 的内容哈希命中即删，覆盖
#     “遗忘后以新 id 重发同一内容”的行。SQL 用 pgcrypto 的 digest +
#     lower/空白折叠近似 Python norm（casefold 与 Unicode 空白有残差，
#     见 docs/deploy.md；常见 ASCII + 中文语料逐字节一致）。轨迹的
#     steps JSON 规范化在 SQL 里无法复现，轨迹哈希段不做，只走 id 段。
#   - 目标库无 forgotten_content 表（0024 前备份）时哈希段整段跳过
#     并打印警告，只走 id 段；pgcrypto 建不出来时同样只走 id 段并
#     打印警告——恢复可用性优先，但缺口必须可见，不静默。
#
# 用法：restore.sh <dump> [目标库名，默认 memory] [sidecar 文件]
# 环境变量（默认 = 现有 docker 路径，生产用法零改动）：
#   RESTORE_PSQL / RESTORE_PG_RESTORE：直连演练时覆盖为本地 psql /
#     pg_restore（含 -h/-p/-U）；LIVE_DB：线上库名（默认 memory）；
#   ADMIN_DB：管理操作连接库（默认 postgres，原脚本连 memory 库，
#     目标库恰为 memory 时 DROP 会自锁）；SIDECAR：sidecar 路径
#     （与第 3 参数等价）。演练时传临时库名，不影响线上库。
# 注意：sidecar JSON 非法时合并直接失败（ON_ERROR_STOP），不静默
# 跳过——对删除语义必须 fail-closed，操作员可去掉 SIDECAR 重跑。
set -euo pipefail
cd "$(dirname "$0")"
dump=${1:?usage: restore.sh <dump> [dbname] [sidecar]}
DB=${2:-memory}
SIDECAR=${3:-${SIDECAR:-}}
C="docker compose -p agent-memory"
PSQL=${RESTORE_PSQL:-"$C exec -T db psql -U memory -v ON_ERROR_STOP=1 -qAt"}
PG_RESTORE=${RESTORE_PG_RESTORE:-"$C exec -T db pg_restore -U memory"}
LIVE_DB=${LIVE_DB:-memory}
ADMIN_DB=${ADMIN_DB:-postgres}
# 1) 先把线上库现有的墓碑取出来（线上库已损坏/不存在时允许为空）
fid=$($PSQL -d "$LIVE_DB" -c "COPY (SELECT id, kind, forgotten_at FROM forgotten_id) TO STDOUT WITH CSV" 2>/dev/null || true)
fc=$($PSQL -d "$LIVE_DB" -c "COPY (SELECT app_id, user_id, kind, content_hash, forgotten_at FROM forgotten_content) TO STDOUT WITH CSV" 2>/dev/null || true)
echo "live forgotten_id rows: $(printf '%s' "$fid" | grep -c . || true)"
echo "live forgotten_content rows: $(printf '%s' "$fc" | grep -c . || true)"
if [ "$DB" != memory ]; then
  $PSQL -d "$ADMIN_DB" -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
fi
# 2) 恢复
$PG_RESTORE -d "$DB" --clean --if-exists --no-owner < "$dump"
echo "facts after restore: $($PSQL -d "$DB" -c 'SELECT count(*) FROM memory_fact')"
# 目标库有无内容墓碑表（0024 前备份没有）：决定内容合并与哈希段是否跑
HAS_FC=$($PSQL -d "$DB" -tAc "SELECT to_regclass('public.forgotten_content')" 2>/dev/null || true)
# 3a) 合并线上库墓碑
{ echo "CREATE TEMP TABLE f(id text, kind text, at timestamptz); COPY f FROM STDIN WITH CSV;"
  printf '%s\n' "$fid" | grep . || true; echo '\.'
  echo "INSERT INTO forgotten_id(id,kind,forgotten_at) SELECT id,kind,at FROM f ON CONFLICT (id) DO NOTHING;"
} | $PSQL -d "$DB"
if [ -n "$HAS_FC" ]; then
  { echo "CREATE TEMP TABLE fc(app_id text, user_id text, kind text, hash text, at timestamptz); COPY fc FROM STDIN WITH CSV;"
    printf '%s\n' "$fc" | grep . || true; echo '\.'
    echo "INSERT INTO forgotten_content(app_id,user_id,kind,content_hash,forgotten_at) SELECT app_id,user_id,kind,hash,at FROM fc ON CONFLICT (app_id, user_id, kind, content_hash) DO NOTHING;"
  } | $PSQL -d "$DB"
fi
# 3b) 合并 sidecar 墓碑（JSON 只含 id/哈希/时间戳；单引号转义后拼 SQL）
if [ -n "$SIDECAR" ]; then
  [ -f "$SIDECAR" ] || { echo "SIDECAR not found: $SIDECAR"; exit 1; }
  SJ=$(sed "s/'/''/g" "$SIDECAR")
  SC_SQL=$(mktemp)
  {
    echo "INSERT INTO forgotten_id (id, kind, forgotten_at) SELECT e->>'id', e->>'kind', (e->>'forgotten_at')::timestamptz FROM jsonb_array_elements(COALESCE(NULLIF('$SJ'::jsonb->'forgotten_id', 'null'::jsonb), '[]'::jsonb)) AS e ON CONFLICT (id) DO NOTHING;"
    if [ -n "$HAS_FC" ]; then
      echo "INSERT INTO forgotten_content (app_id, user_id, kind, content_hash, forgotten_at) SELECT e->>'app_id', e->>'user_id', e->>'kind', e->>'content_hash', (e->>'forgotten_at')::timestamptz FROM jsonb_array_elements(COALESCE(NULLIF('$SJ'::jsonb->'forgotten_content', 'null'::jsonb), '[]'::jsonb)) AS e ON CONFLICT (app_id, user_id, kind, content_hash) DO NOTHING;"
    fi
  } > "$SC_SQL"
  $PSQL -d "$DB" < "$SC_SQL"
  rm -f "$SC_SQL"
  echo "sidecar merged: $SIDECAR"
fi
echo "target forgotten_id rows: $($PSQL -d "$DB" -c 'SELECT count(*) FROM forgotten_id')"
if [ -n "$HAS_FC" ]; then
  echo "target forgotten_content rows: $($PSQL -d "$DB" -c 'SELECT count(*) FROM forgotten_content')"
fi
# 4) 重删：id 段 +（有 pgcrypto 且有内容墓碑表时）哈希段。整段跑在
# 单次 psql 会话里，临时表跨语句共享；每行输出「表|删掉的行数」。
HASH_OK=0
if [ -n "$HAS_FC" ]; then
  if $PSQL -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto" >/dev/null 2>&1; then
    HASH_OK=1
    echo "pgcrypto available: hash re-delete enabled"
  else
    echo "WARNING: pgcrypto unavailable, hash re-delete skipped (id re-delete still applies)"
  fi
else
  echo "WARNING: target has no forgotten_content table (pre-0024 backup), hash re-delete skipped"
fi
RD_SQL=$(mktemp)
{
  echo "CREATE TEMP TABLE doom_fact_id AS SELECT id FROM forgotten_id WHERE kind='fact';"
  echo "CREATE TEMP TABLE doom_pref_id AS SELECT id FROM forgotten_id WHERE kind='preference';"
  echo "CREATE TEMP TABLE doom_ins_id AS SELECT id FROM forgotten_id WHERE kind='insight';"
  echo "CREATE TEMP TABLE doom_ep_id AS SELECT id FROM forgotten_id WHERE kind='episode';"
  echo "CREATE TEMP TABLE doom_rem_id AS SELECT id FROM forgotten_id WHERE kind='reminder';"
  if [ "$HASH_OK" = 1 ]; then
    # norm 近似：lower + \s+ 折叠 + 去首尾空格；与 Python norm
    # （extract.py:58，casefold + Unicode 空白折叠）的残差见文档。
    echo "CREATE TEMP TABLE doom_fact_hash AS SELECT f.id FROM memory_fact f JOIN forgotten_content c ON c.kind='fact' AND c.app_id=f.app_id AND c.user_id=f.user_id AND c.content_hash = encode(digest(btrim(regexp_replace(lower(f.subject), '\s+', ' ', 'g')) || E'\n' || btrim(regexp_replace(lower(f.content), '\s+', ' ', 'g')), 'sha256'), 'hex');"
    echo "CREATE TEMP TABLE doom_pref_hash AS SELECT p.id FROM memory_preference p JOIN forgotten_content c ON c.kind='preference' AND c.app_id=p.app_id AND c.user_id=p.user_id AND c.content_hash = encode(digest(btrim(regexp_replace(lower(p.key), '\s+', ' ', 'g')) || E'\n' || btrim(regexp_replace(lower(p.value), '\s+', ' ', 'g')), 'sha256'), 'hex');"
    echo "CREATE TEMP TABLE doom_ins_hash AS SELECT i.id FROM memory_insight i JOIN forgotten_content c ON c.kind='insight' AND c.app_id=i.app_id AND c.user_id=i.user_id AND c.content_hash = encode(digest(btrim(regexp_replace(lower(i.subject), '\s+', ' ', 'g')) || E'\n' || btrim(regexp_replace(lower(i.content), '\s+', ' ', 'g')), 'sha256'), 'hex');"
    echo "CREATE TEMP TABLE doom_rem_hash AS SELECT r.id FROM memory_reminder r JOIN forgotten_content c ON c.kind='reminder' AND c.app_id=r.app_id AND c.user_id=r.user_id AND c.content_hash = encode(digest(btrim(regexp_replace(lower(r.condition_text), '\s+', ' ', 'g')) || E'\n' || btrim(regexp_replace(lower(r.action_text), '\s+', ' ', 'g')), 'sha256'), 'hex');"
  else
    echo "CREATE TEMP TABLE doom_fact_hash AS SELECT id FROM doom_fact_id WHERE false;"
    echo "CREATE TEMP TABLE doom_pref_hash AS SELECT id FROM doom_pref_id WHERE false;"
    echo "CREATE TEMP TABLE doom_ins_hash AS SELECT id FROM doom_ins_id WHERE false;"
    echo "CREATE TEMP TABLE doom_rem_hash AS SELECT id FROM doom_rem_id WHERE false;"
  fi
  echo "CREATE TEMP TABLE doom_link AS SELECT id FROM memory_link WHERE (src_kind='fact' AND src_id IN (SELECT id FROM doom_fact_id UNION SELECT id FROM doom_fact_hash)) OR (dst_kind='fact' AND dst_id IN (SELECT id FROM doom_fact_id UNION SELECT id FROM doom_fact_hash)) OR (src_kind='insight' AND src_id IN (SELECT id FROM doom_ins_id UNION SELECT id FROM doom_ins_hash)) OR (dst_kind='insight' AND dst_id IN (SELECT id FROM doom_ins_id UNION SELECT id FROM doom_ins_hash));"
  echo "WITH d AS (DELETE FROM memory_fact_token WHERE fact_id IN (SELECT id FROM doom_fact_id UNION SELECT id FROM doom_fact_hash) RETURNING 1) SELECT 'fact_tokens|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM memory_insight_token WHERE insight_id IN (SELECT id FROM doom_ins_id UNION SELECT id FROM doom_ins_hash) RETURNING 1) SELECT 'insight_tokens|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM brief_delivery_edge WHERE insight_id IN (SELECT id FROM doom_ins_id UNION SELECT id FROM doom_ins_hash) RETURNING 1) SELECT 'delivery_edges_by_insight|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM brief_delivery_edge WHERE link_id IN (SELECT id FROM doom_link) RETURNING 1) SELECT 'delivery_edges_by_link|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM memory_link WHERE id IN (SELECT id FROM doom_link) RETURNING 1) SELECT 'links|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM memory_fact WHERE id IN (SELECT id FROM doom_fact_id UNION SELECT id FROM doom_fact_hash) RETURNING 1) SELECT 'facts|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM memory_preference WHERE id IN (SELECT id FROM doom_pref_id UNION SELECT id FROM doom_pref_hash) RETURNING 1) SELECT 'preferences|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM memory_insight WHERE id IN (SELECT id FROM doom_ins_id UNION SELECT id FROM doom_ins_hash) RETURNING 1) SELECT 'insights|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM task_episode WHERE id IN (SELECT id FROM doom_ep_id) RETURNING 1) SELECT 'episodes|' || count(*) FROM d;"
  echo "WITH d AS (DELETE FROM memory_reminder WHERE id IN (SELECT id FROM doom_rem_id UNION SELECT id FROM doom_rem_hash) RETURNING 1) SELECT 'reminders|' || count(*) FROM d;"
} > "$RD_SQL"
$PSQL -d "$DB" < "$RD_SQL"
rm -f "$RD_SQL"
echo "facts now: $($PSQL -d "$DB" -c 'SELECT count(*) FROM memory_fact')"
if [ "$DB" = memory ]; then
  $C run --rm --no-deps api alembic current
  P=$(grep ^API_PORT= .env | cut -d= -f2)
  curl -fsS "http://127.0.0.1:${P:-8000}/readyz"; echo
fi

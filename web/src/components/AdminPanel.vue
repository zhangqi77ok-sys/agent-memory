<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  ApiError,
  adminOverview,
  adminUserMemories,
  getAdminKey,
  setAdminKey,
} from '../api'
import type { AdminUserMemories, AdminUserRow } from '../types'

// 管理端（第十七轮）：凭管理密钥跨用户只读总览所有人的记忆。
// 密钥只存 sessionStorage（关页即失），不进 localStorage、不随用户数据导出。
const keyDraft = ref(getAdminKey())
const users = ref<AdminUserRow[]>([])
const selected = ref<AdminUserMemories | null>(null)
const busy = ref(false)
const error = ref('')

async function loadOverview() {
  busy.value = true
  error.value = ''
  try {
    setAdminKey(keyDraft.value.trim())
    const data = await adminOverview()
    users.value = data.users
    selected.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
    users.value = []
  } finally {
    busy.value = false
  }
}

async function openUser(userId: string) {
  busy.value = true
  error.value = ''
  try {
    selected.value = await adminUserMemories(userId)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function line(r: Record<string, unknown>): string {
  const head = (r.subject as string) || (r.key as string) || (r.title as string) || (r.condition as string) || ''
  const body = (r.content as string) || (r.value as string) || (r.action as string) || ''
  const tail = r.valid_to ? '（已失效）' : r.status ? `（${String(r.status)}）` : ''
  return `${head}：${body}${tail}`
}

onMounted(() => {
  if (keyDraft.value) void loadOverview()
})
</script>

<template>
  <section class="card" data-testid="admin-panel">
    <div class="row" style="margin-bottom: 12px">
      <h2 style="margin: 0">管理端 · 全员记忆总览</h2>
      <span class="muted">只读 · 需服务端配置 ADMIN_KEYS 并在此填入管理密钥</span>
    </div>
    <div class="row" style="gap: 8px; margin-bottom: 12px">
      <input v-model="keyDraft" type="password" placeholder="管理密钥 X-Admin-Key" data-testid="admin-key" style="flex: 1 1 220px" />
      <button type="button" class="btn btn-primary" :disabled="busy" data-testid="admin-load" @click="loadOverview">
        进入管理端
      </button>
    </div>
    <div v-if="error" class="error" data-testid="admin-error">{{ error }}</div>

    <table v-if="users.length" style="width: 100%; border-collapse: collapse" data-testid="admin-users">
      <thead>
        <tr>
          <th style="text-align: left">用户</th>
          <th>事实</th>
          <th>偏好</th>
          <th>经验</th>
          <th>轨迹</th>
          <th>武装中的前瞻记忆</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="u in users" :key="u.user_id">
          <td>{{ u.user_id }}</td>
          <td style="text-align: center">{{ u.facts }}</td>
          <td style="text-align: center">{{ u.preferences }}</td>
          <td style="text-align: center">{{ u.insights }}</td>
          <td style="text-align: center">{{ u.episodes }}</td>
          <td style="text-align: center">{{ u.reminders_armed }}</td>
          <td>
            <button type="button" class="btn btn-ghost" :disabled="busy" :data-testid="`admin-open-${u.user_id}`" @click="openUser(u.user_id)">
              查看全部记忆
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="selected" style="margin-top: 16px" data-testid="admin-user-detail">
      <h3 style="margin: 0 0 8px">{{ selected.user_id }} 的全部记忆</h3>
      <template v-for="sec in (['facts', 'preferences', 'insights', 'episodes', 'reminders'] as const)" :key="sec">
        <h4 style="margin: 12px 0 4px">
          {{ { facts: '事实', preferences: '偏好', insights: '经验', episodes: '轨迹', reminders: '前瞻记忆' }[sec] }}
          （{{ selected[sec].length }}）
        </h4>
        <div v-if="!selected[sec].length" class="muted">暂无</div>
        <div v-for="(r, idx) in selected[sec]" :key="idx" class="muted" style="margin: 2px 0">
          {{ line(r as Record<string, unknown>) }}
          <span v-if="sec === 'insights' && (r as Record<string, unknown>).verified === false"> · 未验证</span>
          <span v-if="(r as Record<string, unknown>).confidence != null">
            · 置信 {{ Math.round(Number((r as Record<string, unknown>).confidence) * 100) }}%
          </span>
        </div>
      </template>
    </div>
  </section>
</template>

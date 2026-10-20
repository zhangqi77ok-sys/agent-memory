<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ApiError,
  clearAllMemories,
  correctMemory,
  deleteMemory,
  exportMemories,
  getSettings,
  listJobs,
  listMemories,
  needsConsent,
  updateSettings,
} from './api'
import { clearToken, resolveToken } from './auth'
import type { Job, MemoryItem, Settings } from './types'
import ConfirmDialog from './components/ConfirmDialog.vue'
import ConsentPage from './components/ConsentPage.vue'
import JobsList from './components/JobsList.vue'
import MemoryRow from './components/MemoryRow.vue'

const token = ref<string | null>(null)
const settings = ref<Settings | null>(null)
const items = ref<MemoryItem[]>([])
const total = ref(0)
const jobs = ref<Job[]>([])
const busy = ref(false)
const error = ref('')
const notice = ref('')
const showClearConfirm = ref(false)
const filter = ref<'all' | 'fact' | 'preference'>('all')

const showConsent = computed(() => settings.value != null && needsConsent(settings.value))

async function loadAll() {
  error.value = ''
  busy.value = true
  try {
    settings.value = await getSettings()
    if (!showConsent.value) {
      const kind = filter.value === 'all' ? undefined : filter.value
      const page = await listMemories({ kind, limit: 100 })
      items.value = page.items
      total.value = page.total
      jobs.value = await listJobs(20)
    } else {
      items.value = []
      total.value = 0
      jobs.value = []
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function agree() {
  if (!settings.value) return
  busy.value = true
  error.value = ''
  try {
    settings.value = await updateSettings({
      memory_enabled: true,
      consent_version: settings.value.current_consent_version,
    })
    await loadAll()
    notice.value = '已开启记忆' 
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function toggleEnabled() {
  if (!settings.value || showConsent.value) return
  const next = !settings.value.memory_enabled
  busy.value = true
  error.value = ''
  try {
    settings.value = await updateSettings(
      next
        ? { memory_enabled: true, consent_version: settings.value.current_consent_version }
        : { memory_enabled: false },
    )
    notice.value = next ? '记忆已开启' : '记忆已暂停：新对话不再写入、也不再召回'
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onCorrect(id: string, content: string) {
  busy.value = true
  error.value = ''
  try {
    await correctMemory(id, content)
    await loadAll()
    notice.value = '已纠正，旧版本已失效' 
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onDelete(id: string) {
  busy.value = true
  error.value = ''
  try {
    await deleteMemory(id)
    await loadAll()
    notice.value = '已删除' 
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onClear() {
  showClearConfirm.value = false
  busy.value = true
  error.value = ''
  try {
    await clearAllMemories()
    await loadAll()
    notice.value = '已清空全部记忆（审计只保留 id）' 
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onExport() {
  busy.value = true
  error.value = ''
  try {
    const blob = await exportMemories()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `my-memories-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
    notice.value = '已开始下载导出文件'
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function logout() {
  clearToken()
  token.value = null
  settings.value = null
  items.value = []
  jobs.value = []
  error.value = '已退出本地登录态。请通过 ?token= 重新进入。'
}

onMounted(async () => {
  token.value = resolveToken()
  if (token.value) await loadAll()
  else error.value = '未登录：请在 URL 加上 ?token=<JWT>（由 tiancode 签发）。'
})
</script>

<template>
  <header class="row" style="margin-bottom: 16px">
    <div>
      <h1>我的记忆</h1>
      <p class="muted" style="margin: 0">查看、纠正、删除与导出你保存在记忆服务中的数据</p>
    </div>
    <span class="spacer" />
    <button v-if="token" type="button" class="btn" data-testid="logout" @click="logout">退出</button>
  </header>

  <div v-if="error" class="error" data-testid="error">{{ error }}</div>
  <div v-if="notice" class="ok" data-testid="notice">{{ notice }}</div>

  <ConsentPage
    v-if="token && settings && showConsent"
    :settings="settings"
    :busy="busy"
    @agree="agree"
  />

  <template v-else-if="token && settings">
    <section class="card" data-testid="settings-panel">
      <div class="row">
        <div>
          <h2 style="margin-bottom: 4px">记忆总开关</h2>
          <p class="muted" style="margin: 0">
            {{ settings.memory_enabled ? '已开启：可写入与召回' : '已暂停：不写入、不召回（已有数据保留）' }}
          </p>
        </div>
        <span class="spacer" />
        <button
          type="button"
          class="toggle"
          :class="{ on: settings.memory_enabled }"
          :aria-pressed="settings.memory_enabled"
          data-testid="master-toggle"
          :disabled="busy"
          @click="toggleEnabled"
        />
      </div>
      <p class="muted" style="margin-top: 10px">
        同意版本 {{ settings.consent_version || '—' }}
        <span v-if="settings.consent_at"> · {{ new Date(settings.consent_at).toLocaleString('zh-CN', { hour12: false }) }}</span>
      </p>
    </section>

    <section class="card" data-testid="memory-panel">
      <div class="row" style="margin-bottom: 12px">
        <h2 style="margin: 0">记忆列表</h2>
        <span class="muted">共 {{ total }} 条</span>
        <span class="spacer" />
        <select v-model="filter" data-testid="kind-filter" @change="loadAll">
          <option value="all">全部</option>
          <option value="fact">事实</option>
          <option value="preference">偏好</option>
        </select>
        <button type="button" class="btn" :disabled="busy" data-testid="refresh" @click="loadAll">刷新</button>
        <button type="button" class="btn" :disabled="busy" data-testid="export-btn" @click="onExport">导出 JSON</button>
        <button type="button" class="btn btn-danger" :disabled="busy" data-testid="clear-btn" @click="showClearConfirm = true">
          清空全部
        </button>
      </div>

      <div v-if="!items.length" class="muted" data-testid="empty-list">暂无记忆</div>
      <MemoryRow
        v-for="item in items"
        :key="item.id"
        :item="item"
        :busy="busy"
        @correct="(c) => onCorrect(item.id, c)"
        @remove="onDelete(item.id)"
      />
    </section>

    <JobsList :jobs="jobs" />
  </template>

  <ConfirmDialog
    v-if="showClearConfirm"
    title="确认清空全部记忆？"
    message="将删除你的事实、偏好、摘要与原始消息。此操作不可撤销，导出请先点「导出 JSON」。"
    confirm-label="确认清空"
    dangerous
    @confirm="onClear"
    @cancel="showClearConfirm = false"
  />
</template>

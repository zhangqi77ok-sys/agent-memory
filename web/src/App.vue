<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ApiError,
  clearAllMemories,
  correctMemory,
  createReminder,
  deleteMemory,
  dismissReminder,
  exportMemories,
  getServiceKey,
  getSettings,
  listInsights,
  listJobs,
  listMemories,
  listReminders,
  needsConsent,
  patchInsight,
  setServiceKey,
  updateSettings,
} from './api'
import { clearUserId, getUserId, setUserId } from './user'
import type { InsightItem, Job, MemoryItem, Reminder, Settings } from './types'
import AdminPanel from './components/AdminPanel.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import ConsentPage from './components/ConsentPage.vue'
import GraphStatsPanel from './components/GraphStatsPanel.vue'
import InsightsPanel from './components/InsightsPanel.vue'
import JobsList from './components/JobsList.vue'
import MemoryRow from './components/MemoryRow.vue'
import RemindersPanel from './components/RemindersPanel.vue'

const userId = ref<string | null>(null)
const draft = ref('')
const settings = ref<Settings | null>(null)
const items = ref<MemoryItem[]>([])
const total = ref(0)
const jobs = ref<Job[]>([])
const busy = ref(false)
const error = ref('')
const notice = ref('')
const showClearConfirm = ref(false)
const filter = ref<'all' | 'fact' | 'preference'>('all')
const query = ref('')
const serviceKeyDraft = ref('')
const serviceKeySaved = ref(false)

const filteredItems = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter((i) => `${i.subject} ${i.content}`.toLowerCase().includes(q))
})
const filteredInsights = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return insights.value
  return insights.value.filter((i) => `${i.subject} ${i.content}`.toLowerCase().includes(q))
})

function saveServiceKey() {
  setServiceKey(serviceKeyDraft.value.trim())
  serviceKeySaved.value = !!serviceKeyDraft.value.trim()
  if (serviceKeySaved.value) {
    notice.value = '服务密钥已保存（仅存于本页会话，关页即失）'
    void loadAll()
  } else {
    notice.value = '服务密钥已清除'
  }
}
const view = ref<'memories' | 'insights' | 'reminders' | 'graph' | 'admin'>('memories')
const insights = ref<InsightItem[]>([])
const reminders = ref<Reminder[]>([])

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
      insights.value = await listInsights()
      reminders.value = await listReminders(true)
    } else {
      items.value = []
      total.value = 0
      jobs.value = []
      insights.value = []
      reminders.value = []
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

async function onTogglePin(item: InsightItem) {
  busy.value = true
  error.value = ''
  try {
    await patchInsight(item.id, { pinned: !item.pinned })
    insights.value = await listInsights()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onToggleMute(item: InsightItem) {
  busy.value = true
  error.value = ''
  try {
    await patchInsight(item.id, { muted: !item.muted })
    insights.value = await listInsights()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onCreateReminder(body: {
  condition: string
  action: string
  project?: string | null
  once?: boolean
}) {
  busy.value = true
  error.value = ''
  try {
    await createReminder(body)
    reminders.value = await listReminders(true)
    notice.value = '前瞻记忆已武装'
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function onDismissReminder(item: Reminder) {
  busy.value = true
  error.value = ''
  try {
    await dismissReminder(item.reminder_id)
    reminders.value = await listReminders(true)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function useUser() {
  const id = draft.value.trim()
  if (!id) {
    error.value = '请填写 user_id'
    return
  }
  setUserId(id)
  userId.value = id
  void loadAll()
}

function switchUser() {
  clearUserId()
  userId.value = null
  settings.value = null
  items.value = []
  jobs.value = []
  error.value = '请填写 user_id'
}

onMounted(async () => {
  serviceKeySaved.value = !!getServiceKey()
  serviceKeyDraft.value = ''
  userId.value = getUserId()
  draft.value = userId.value || ''
  if (userId.value) await loadAll()
  else error.value = '请填写 user_id'
})
</script>

<template>
  <header class="row" style="margin-bottom: 16px">
    <div>
      <h1>我的记忆</h1>
      <p class="muted" style="margin: 0">查看、纠正、删除与导出你保存在记忆服务中的数据</p>
    </div>
    <span class="spacer" />
    <input data-testid="user-id" v-model="draft" placeholder="user_id" />
    <button type="button" class="btn" data-testid="use-user" @click="useUser">使用</button>
    <button v-if="userId" type="button" class="btn" data-testid="logout" @click="switchUser">切换用户</button>
  </header>

  <div v-if="error" class="error" data-testid="error">{{ error }}</div>
  <div v-if="notice" class="ok" data-testid="notice">{{ notice }}</div>

  <ConsentPage
    v-if="userId && settings && showConsent"
    :settings="settings"
    :busy="busy"
    @agree="agree"
  />

  <template v-else-if="userId && settings">
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

    <section v-if="view === 'memories'" class="card" data-testid="memory-panel">
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

      <div v-if="busy" class="muted" data-testid="loading">加载中…</div>
      <div v-else-if="!filteredItems.length" class="muted" data-testid="empty-list">
        {{ query.trim() ? '没有匹配的记忆' : '暂无记忆' }}
      </div>
      <MemoryRow
        v-for="item in filteredItems"
        :key="item.id"
        :item="item"
        :busy="busy"
        @correct="(c) => onCorrect(item.id, c)"
        @remove="onDelete(item.id)"
      />
    </section>

    <div class="row" style="gap: 8px; margin: 4px 0 12px">
      <input
        v-model="serviceKeyDraft"
        type="password"
        placeholder="服务密钥（X-Service-Key，关页即失）"
        data-testid="service-key"
        autocomplete="off"
      />
      <button type="button" class="btn" data-testid="save-service-key" @click="saveServiceKey">
        {{ serviceKeySaved ? '密钥已保存' : '保存密钥' }}
      </button>
      <input
        v-model="query"
        placeholder="搜索主题或内容"
        data-testid="search"
      />
    </div>

    <div class="row" style="gap: 8px; margin: 4px 0 12px" data-testid="view-tabs">
      <button type="button" class="btn" :class="{ 'btn-primary': view === 'memories' }" data-testid="tab-memories" @click="view = 'memories'">记忆</button>
      <button type="button" class="btn" :class="{ 'btn-primary': view === 'insights' }" data-testid="tab-insights" @click="view = 'insights'">经验</button>
      <button type="button" class="btn" :class="{ 'btn-primary': view === 'reminders' }" data-testid="tab-reminders" @click="view = 'reminders'">前瞻记忆</button>
      <button type="button" class="btn" :class="{ 'btn-primary': view === 'graph' }" data-testid="tab-graph" @click="view = 'graph'">网结构</button>
      <button type="button" class="btn" :class="{ 'btn-primary': view === 'admin' }" data-testid="tab-admin" @click="view = 'admin'">管理端</button>
    </div>

    <InsightsPanel
      v-if="view === 'insights'"
      :insights="filteredInsights"
      :busy="busy"
      @toggle-pin="onTogglePin"
      @toggle-mute="onToggleMute"
    />
    <RemindersPanel
      v-if="view === 'reminders'"
      :reminders="reminders"
      :busy="busy"
      @create="onCreateReminder"
      @dismiss="onDismissReminder"
    />
    <GraphStatsPanel v-if="view === 'graph'" />
    <AdminPanel v-if="view === 'admin'" />

    <JobsList v-if="view === 'memories'" :jobs="jobs" />
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

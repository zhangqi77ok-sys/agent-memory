<script setup lang="ts">
import { ref } from 'vue'
import type { Reminder } from '../types'

defineProps<{ reminders: Reminder[]; busy?: boolean }>()
const emit = defineEmits<{
  create: [body: { condition: string; action: string; project?: string | null; once?: boolean }]
  dismiss: [item: Reminder]
}>()

const condition = ref('')
const action = ref('')
const project = ref('')
const once = ref(true)

function submit() {
  const c = condition.value.trim()
  const a = action.value.trim()
  if (!c || !a) return
  emit('create', {
    condition: c,
    action: a,
    project: project.value.trim() || null,
    once: once.value,
  })
  condition.value = ''
  action.value = ''
  project.value = ''
}

const STATUS_LABEL: Record<string, string> = {
  armed: '武装中',
  fired: '已触发',
  dismissed: '已解除',
}
</script>

<template>
  <section class="card" data-testid="reminders-panel">
    <div class="row" style="margin-bottom: 12px">
      <h2 style="margin: 0">前瞻记忆</h2>
      <span class="muted">「当某事发生时，提醒我做某事」——新任务开简报时自动撞触发</span>
    </div>

    <div class="row" style="gap: 8px; flex-wrap: wrap; margin-bottom: 12px">
      <input v-model="condition" placeholder="当……时（触发条件）" data-testid="reminder-condition" style="flex: 1 1 180px" />
      <input v-model="action" placeholder="提醒我做……（要做的事）" data-testid="reminder-action" style="flex: 1 1 180px" />
      <input v-model="project" placeholder="项目（可空）" data-testid="reminder-project" style="flex: 0 1 120px" />
      <label class="muted"><input v-model="once" type="checkbox" data-testid="reminder-once" /> 只触发一次</label>
      <button type="button" class="btn btn-primary" :disabled="busy || !condition.trim() || !action.trim()" data-testid="reminder-create" @click="submit">
        武装
      </button>
    </div>

    <div v-if="!reminders.length" class="muted" data-testid="reminders-empty">暂无前瞻记忆</div>
    <article v-for="r in reminders" :key="r.reminder_id" class="memory-item" :data-testid="`reminder-${r.reminder_id}`">
      <div class="row">
        <span class="badge">{{ STATUS_LABEL[r.status] || r.status }}</span>
        <span>当 <strong>{{ r.condition }}</strong> 时 → {{ r.action }}</span>
        <span class="spacer" />
        <button
          v-if="r.status === 'armed'"
          type="button"
          class="btn btn-ghost"
          :disabled="busy"
          @click="emit('dismiss', r)"
        >
          解除
        </button>
      </div>
      <div class="muted" style="margin-top: 4px">
        {{ r.once ? '一次性' : '常驻' }} · 已触发 {{ r.fire_count }} 次
        <span v-if="r.project"> · 项目 {{ r.project }}</span>
        <span v-if="r.hold_count"> · 暂缓 {{ r.hold_count }} 次</span>
        <span v-if="r.last_timing === 'context'"> · 由会话上下文触发</span>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { MemoryItem } from '../types'

const props = defineProps<{ item: MemoryItem; busy?: boolean }>()
const emit = defineEmits<{
  correct: [content: string]
  remove: []
}>()

const editing = ref(false)
const draft = ref(props.item.content)

function startEdit() {
  draft.value = props.item.content
  editing.value = true
}

function save() {
  const next = draft.value.trim()
  if (!next || next === props.item.content) {
    editing.value = false
    return
  }
  emit('correct', next)
  editing.value = false
}

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}
</script>

<template>
  <article class="memory-item" :data-testid="`memory-${item.id}`">
    <div class="row">
      <span class="badge" :class="item.kind === 'preference' ? 'badge-pref' : 'badge-fact'">
        {{ item.kind === 'preference' ? '偏好' : '事实' }}
      </span>
      <strong>{{ item.subject }}</strong>
      <span class="muted">v{{ item.version }}</span>
      <span class="spacer" />
      <button type="button" class="btn btn-ghost" :disabled="busy" data-testid="edit-btn" @click="startEdit">纠正</button>
      <button type="button" class="btn btn-ghost" :disabled="busy" data-testid="delete-btn" @click="emit('remove')">删除</button>
    </div>

    <div v-if="!editing" style="margin-top: 6px">{{ item.content }}</div>
    <div v-else style="margin-top: 8px">
      <textarea v-model="draft" rows="3" data-testid="edit-input" />
      <div class="row" style="margin-top: 8px; justify-content: flex-end">
        <button type="button" class="btn" @click="editing = false">取消</button>
        <button type="button" class="btn btn-primary" data-testid="edit-save" @click="save">保存</button>
      </div>
    </div>

    <div class="muted" style="margin-top: 6px">
      <span>{{ formatTime(item.valid_from) }}</span>
      <span v-if="item.source_session_id"> · 来源会话 {{ item.source_session_id }}</span>
    </div>
  </article>
</template>

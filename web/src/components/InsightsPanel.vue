<script setup lang="ts">
import type { InsightItem } from '../types'

defineProps<{ insights: InsightItem[]; busy?: boolean }>()
const emit = defineEmits<{
  togglePin: [item: InsightItem]
  toggleMute: [item: InsightItem]
}>()

const KIND_LABEL: Record<string, string> = {
  lesson: '教训',
  strategy: '打法',
  pitfall: '避坑',
  cost_saver: '省钱招',
}
</script>

<template>
  <section class="card" data-testid="insights-panel">
    <div class="row" style="margin-bottom: 12px">
      <h2 style="margin: 0">经验库</h2>
      <span class="muted">共 {{ insights.length }} 条现行经验 · 曝光 = 被简报递出，应用 = 真被后续任务用上（回流记账）</span>
    </div>
    <div v-if="!insights.length" class="muted" data-testid="insights-empty">暂无经验</div>
    <article v-for="i in insights" :key="i.id" class="memory-item" :data-testid="`insight-${i.id}`">
      <div class="row">
        <span class="badge">{{ KIND_LABEL[i.kind] || i.kind }}</span>
        <strong>{{ i.subject }}</strong>
        <span class="badge" data-testid="insight-confidence">置信 {{ Math.round(i.confidence * 100) }}%</span>
        <span v-if="i.verified" class="badge" data-testid="verified-badge">✓ 证据已验</span>
        <span v-else class="badge badge-pref" data-testid="unverified-badge">未验证 · 已降权</span>
        <span v-if="i.pinned" class="badge">置顶</span>
        <span v-if="i.muted" class="badge badge-pref">已禁用</span>
        <span class="spacer" />
        <button type="button" class="btn btn-ghost" :disabled="busy" @click="emit('togglePin', i)">
          {{ i.pinned ? '取消置顶' : '置顶' }}
        </button>
        <button type="button" class="btn btn-ghost" :disabled="busy" @click="emit('toggleMute', i)">
          {{ i.muted ? '取消禁用' : '禁用' }}
        </button>
      </div>
      <div style="margin-top: 6px">{{ i.content }}</div>
      <div v-if="i.applies_when" class="muted" style="margin-top: 4px">适用：{{ i.applies_when }}</div>
      <div class="muted" style="margin-top: 6px">
        曝光 {{ i.use_count }} · 应用 {{ i.applied_count }} · 成功关联 {{ i.success_count }}
        · 人工反馈 +{{ i.feedback_pos }}/-{{ i.feedback_neg }}
        · 自动回流 +{{ i.auto_pos }}/-{{ i.auto_neg }}
        <span v-if="i.project"> · 项目 {{ i.project }}</span>
        <span v-if="i.last_used_at"> · 最近应用 {{ new Date(i.last_used_at).toLocaleString('zh-CN', { hour12: false }) }}</span>
      </div>
    </article>
  </section>
</template>

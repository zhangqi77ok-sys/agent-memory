<script setup lang="ts">
import type { EpisodeDetail } from '../types'

defineProps<{ episode: EpisodeDetail }>()
const emit = defineEmits<{ close: [] }>()

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}
</script>

<template>
  <section class="card" data-testid="episode-detail-panel">
    <div class="row">
      <h2 style="margin: 0">来源轨迹</h2>
      <span class="badge">{{ episode.outcome }}</span>
      <span v-if="episode.skeletonized" class="badge">已骨架化</span>
      <span class="spacer" />
      <button type="button" class="btn btn-ghost" data-testid="episode-close" @click="emit('close')">关闭</button>
    </div>
    <div style="margin-top: 8px">
      <strong>{{ episode.title }}</strong>
    </div>
    <div class="muted" style="margin-top: 4px">
      来源 {{ episode.source }}
      <span v-if="episode.project"> · 项目 {{ episode.project }}</span>
      <span> · 提炼 {{ episode.distill_status }}</span>
      <span> · 显著性 {{ episode.salience }}</span>
      <span> · {{ formatTime(episode.created_at) }}</span>
    </div>
    <ol style="margin: 12px 0 0; padding-left: 20px">
      <li v-for="(s, idx) in episode.steps" :key="idx" style="margin-bottom: 8px">
        <div>{{ s.action }}</div>
        <div v-if="s.observation" class="muted">{{ s.observation }}</div>
        <div v-if="s.error" class="error">{{ s.error }}</div>
      </li>
    </ol>
    <div v-if="!episode.steps.length" class="muted" style="margin-top: 8px">该轨迹无步骤记录</div>
  </section>
</template>

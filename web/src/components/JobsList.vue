<script setup lang="ts">
import type { Job } from '../types'

defineProps<{ jobs: Job[] }>()

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function statusLabel(s: string) {
  return ({ pending: '处理中', running: '处理中', done: '完成', dead: '失败' } as Record<string, string>)[s] || s
}
</script>

<template>
  <section class="card jobs" data-testid="jobs-list">
    <h2>写入任务</h2>
    <p class="muted">最近写入记忆的处理状态（pending / running / done / dead）。</p>
    <div v-if="!jobs.length" class="muted">暂无任务</div>
    <table v-else>
      <thead>
        <tr>
          <th>状态</th>
          <th>会话</th>
          <th>重试</th>
          <th>时间</th>
          <th>错误</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="j in jobs" :key="j.job_id" :data-testid="`job-${j.job_id}`">
          <td :class="`status-${j.status}`">{{ statusLabel(j.status) }}</td>
          <td>{{ j.session_id }}</td>
          <td>{{ j.attempts }}</td>
          <td>{{ formatTime(j.created_at) }}</td>
          <td class="muted">{{ j.last_error || '—' }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

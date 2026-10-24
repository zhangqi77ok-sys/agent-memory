<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, graphStats } from '../api'
import type { GraphStats } from '../types'

const stats = ref<GraphStats | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    stats.value = await graphStats()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="card" data-testid="graph-stats-panel">
    <div class="row" style="margin-bottom: 12px">
      <h3 style="margin: 0">网结构</h3>
      <button type="button" class="btn" :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="loading" class="muted">正在扫描记忆网……</p>
    <p v-else-if="error" class="muted">{{ error }}</p>
    <template v-else-if="stats">
      <p class="muted" style="margin: 0 0 12px">
        {{ stats.node_count }} 个节点 · {{ stats.edge_count }} 条关联边 ·
        密度 {{ stats.density }} · 孤点 {{ stats.orphan_count }}（{{ stats.orphan_rate }}）·
        矛盾张力 {{ stats.contradicts_count }} 条
      </p>
      <h4 style="margin: 12px 0 6px">枢纽（连接最多的记忆）</h4>
      <ul v-if="stats.hubs.length" style="margin: 0; padding-left: 20px">
        <li v-for="h in stats.hubs" :key="h.id">{{ h.subject }}（{{ h.degree }} 条边）</li>
      </ul>
      <p v-else class="muted">暂无枢纽——还没有边。</p>
      <h4 style="margin: 12px 0 6px">社群</h4>
      <ul v-if="stats.communities.length" style="margin: 0; padding-left: 20px">
        <li v-for="(c, i) in stats.communities" :key="i">
          {{ c.size }} 个节点：{{ c.members.map((m) => m.subject).join('、') }}
        </li>
      </ul>
      <p v-else class="muted">暂无社群。</p>
      <h4 style="margin: 12px 0 6px">桥（断了就分裂的边）</h4>
      <p v-if="stats.bridges_skipped" class="muted">边数超过 20 万，桥分析已跳过。</p>
      <ul v-else-if="stats.bridges.length" style="margin: 0; padding-left: 20px">
        <li v-for="b in stats.bridges" :key="b.link_id">
          {{ b.src.subject }} — {{ b.relation }} — {{ b.dst.subject }}
        </li>
      </ul>
      <p v-else class="muted">暂无桥。</p>
    </template>
  </section>
</template>

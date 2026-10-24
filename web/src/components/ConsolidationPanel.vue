<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, consolidationReport } from '../api'
import type { ConsolidationReport } from '../types'

const report = ref<ConsolidationReport | null>(null)
const loaded = ref(false)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await consolidationReport()
    report.value = res.report
    loaded.value = true
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="card" data-testid="consolidation-panel">
    <div class="row" style="margin-bottom: 12px">
      <h3 style="margin: 0">睡眠巩固</h3>
      <button type="button" class="btn" :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="loading" class="muted">正在读取巩固报告……</p>
    <p v-else-if="error" class="muted">{{ error }}</p>
    <p v-else-if="loaded && !report" class="muted">
      尚未巩固过——空闲巡检跑过一轮后，这里会显示它合并了哪些、剪了哪些边。
    </p>
    <template v-else-if="report">
      <p class="muted" style="margin: 0 0 12px">
        上次巩固：{{ report.run_at || '未知时间' }} · 合并 {{ report.merged }} 条 ·
        剪边 {{ report.pruned }} 条 · 强化 {{ report.strengthened }} 条 ·
        新建共用边 {{ report.linked }} 条 · 抽象原则 {{ report.generalized }} 条
      </p>
      <h4 style="margin: 12px 0 6px">合并的重复记忆</h4>
      <ul v-if="report.merged_pairs.length" style="margin: 0; padding-left: 20px">
        <li v-for="p in report.merged_pairs" :key="p.loser_id">
          {{ p.loser_subject }} → 并入 {{ p.keeper_subject }}
        </li>
      </ul>
      <p v-else class="muted">本轮没有可合并的重复记忆。</p>
      <h4 style="margin: 12px 0 6px">抽象出的原则</h4>
      <ul v-if="report.generalized_ids.length" style="margin: 0; padding-left: 20px">
        <li v-for="g in report.generalized_ids" :key="g.id">{{ g.subject }}</li>
      </ul>
      <p v-else class="muted">本轮没有从共用经验里抽象出新原则。</p>
    </template>
  </section>
</template>

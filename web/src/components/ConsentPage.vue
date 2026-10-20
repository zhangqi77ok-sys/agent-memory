<script setup lang="ts">
import { ref } from 'vue'
import type { Settings } from '../types'

const props = defineProps<{ settings: Settings; busy?: boolean }>()
const emit = defineEmits<{ agree: [] }>()
const checked = ref(false)
</script>

<template>
  <section class="card" data-testid="consent-page">
    <h1>开启「我的记忆」</h1>
    <p>
      开启后，对话助手可以记住你的事实与偏好，用来提供更贴合的回复。
      你可以随时查看、纠正、删除或导出这些数据。
    </p>
    <div class="privacy">
      <strong>隐私说明（版本 {{ settings.current_consent_version }}）</strong>
      <ul>
        <li>仅保存与你相关的事实、偏好和必要的来源会话标识，不会出售给第三方。</li>
        <li>你可以随时纠正单条、删除单条、清空全部，或导出 JSON 副本。</li>
        <li>关闭总开关后，新对话不再写入、也不再召回；已有数据会保留，直到你主动清空。</li>
        <li>隐私说明改版时会要求你重新同意（版本号变更）。</li>
      </ul>
    </div>
    <label class="row" style="margin: 16px 0">
      <input v-model="checked" type="checkbox" data-testid="consent-check" />
      <span>我已阅读并同意上述隐私说明</span>
    </label>
    <button
      type="button"
      class="btn btn-primary"
      data-testid="consent-submit"
      :disabled="!checked || busy"
      @click="emit('agree')"
    >
      同意并开启记忆
    </button>
  </section>
</template>

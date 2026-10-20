<script setup lang="ts">
defineProps<{
  title: string
  message: string
  confirmLabel?: string
  dangerous?: boolean
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <div class="dialog-backdrop" role="dialog" aria-modal="true" data-testid="confirm-dialog">
    <div class="dialog">
      <h3>{{ title }}</h3>
      <p>{{ message }}</p>
      <div class="row" style="justify-content: flex-end; margin-top: 16px">
        <button type="button" class="btn" data-testid="confirm-cancel" @click="emit('cancel')">取消</button>
        <button
          type="button"
          class="btn"
          :class="dangerous ? 'btn-danger' : 'btn-primary'"
          data-testid="confirm-ok"
          @click="emit('confirm')"
        >
          {{ confirmLabel || '确认' }}
        </button>
      </div>
    </div>
  </div>
</template>

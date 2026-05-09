<script setup lang="ts">
import type { AutoAdjustMode } from '../types';

defineProps<{
  modelValue: AutoAdjustMode;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', value: AutoAdjustMode): void;
}>();

function onChange(event: Event): void {
  emit('update:modelValue', (event.target as HTMLSelectElement).value as AutoAdjustMode);
}
</script>

<template>
  <label class="run-mode-picker small muted">
    调参模式
    <select :value="modelValue" :disabled="disabled" @change="onChange">
      <option value="fresh_fit">fresh_fit（重新建立受控基线）</option>
      <option value="refine_current">refine_current（基于当前材质继续）</option>
    </select>
    <span class="run-mode-note">
      {{ modelValue === 'refine_current' ? '保留当前材质状态' : '先隔离干扰项再搜索' }}
    </span>
  </label>
</template>

<style scoped>
.run-mode-picker { display: flex; align-items: center; gap: 6px; }
.run-mode-picker select {
  background: var(--bg-panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
}
.run-mode-note { color: var(--text-muted); }
</style>

<script setup lang="ts">
import type { JobState } from '../types';

defineProps<{
  activeJob: JobState | null;
  log: string;
}>();
</script>

<template>
  <section class="section">
    <h3 class="section-title">Job Runner</h3>
    <p class="muted small">
      当前运行状态：
      <span class="mono">{{ activeJob?.status ?? 'idle' }}</span>
    </p>
    <pre v-if="log" class="log-box">{{ log }}</pre>
  </section>
</template>

<script setup lang="ts">
import type { LayaControlGroup } from '../types';

defineProps<{
  groups: LayaControlGroup[];
}>();
</script>

<template>
  <section class="section">
    <h3 class="section-title">Laya Control Schema</h3>
    <p class="muted small">共 {{ groups.length }} 个控件分组。</p>
  </section>
</template>

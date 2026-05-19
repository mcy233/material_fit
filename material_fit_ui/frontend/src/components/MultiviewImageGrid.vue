<script setup lang="ts">
import { computed, ref } from 'vue';
import type { IterationMultiviewImage } from '../types';

const props = defineProps<{
  items: IterationMultiviewImage[];
  title?: string;
}>();

const resolvedTitle = computed(() => props.title || `多视角图像对比 · ${props.items.length} views`);
const preview = ref<{ src: string; title: string } | null>(null);

function fmt(value: unknown, digits = 4): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

function openPreview(src: string | null, title: string): void {
  if (!src) return;
  preview.value = { src, title };
}

function closePreview(): void {
  preview.value = null;
}
</script>

<template>
  <section v-if="items.length" class="section multiview-section">
    <h3 class="section-title">{{ resolvedTitle }}</h3>
    <div class="multiview-grid">
      <div v-for="item in items" :key="item.view_id" class="view-card">
        <div class="view-title">
          <span class="mono">{{ item.view_id }}</span>
          <span class="view-score mono">
            research {{ fmt(item.research_score, 1) }}
            <span v-if="item.research_valid === false" class="invalid">invalid</span>
            · fit {{ fmt(item.fit_score) }} · mae {{ fmt(item.diff_score) }}
          </span>
        </div>
        <div class="view-triplet">
          <figure>
            <figcaption>Unity</figcaption>
            <button v-if="item.reference" class="image-button" type="button" @click="openPreview(item.reference, `${item.view_id} · Unity`)">
              <img :src="item.reference" :alt="`${item.view_id} unity reference`" loading="lazy" />
            </button>
            <span v-else class="empty">无</span>
          </figure>
          <figure>
            <figcaption>Laya</figcaption>
            <button v-if="item.candidate" class="image-button" type="button" @click="openPreview(item.candidate, `${item.view_id} · Laya`)">
              <img :src="item.candidate" :alt="`${item.view_id} laya candidate`" loading="lazy" />
            </button>
            <span v-else class="empty">无</span>
          </figure>
          <figure>
            <figcaption>Diff</figcaption>
            <button v-if="item.diff" class="image-button" type="button" @click="openPreview(item.diff, `${item.view_id} · Diff`)">
              <img :src="item.diff" :alt="`${item.view_id} diff`" loading="lazy" />
            </button>
            <span v-else class="empty">无</span>
          </figure>
        </div>
      </div>
    </div>
    <Teleport to="body">
      <div v-if="preview" class="image-modal" @click.self="closePreview">
        <div class="image-modal-panel">
          <header class="image-modal-header">
            <span class="mono">{{ preview.title }}</span>
            <button type="button" class="close-btn" @click="closePreview">关闭</button>
          </header>
          <img :src="preview.src" :alt="preview.title" class="image-modal-img" />
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.multiview-section { overflow: hidden; }
.multiview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 10px;
}
.view-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px;
}
.view-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  color: var(--text-muted);
  margin-bottom: 6px;
  font-size: 12px;
}
.view-score {
  color: var(--text-dim);
  white-space: nowrap;
}
.invalid {
  color: var(--bad);
  margin-left: 4px;
}
.view-triplet {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}
figure { margin: 0; min-width: 0; }
.image-button {
  display: block;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: zoom-in;
}
figcaption {
  color: var(--text-dim);
  font-size: 11px;
  margin-bottom: 3px;
}
img {
  width: 100%;
  max-height: 160px;
  object-fit: contain;
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.image-modal {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: rgba(0, 0, 0, 0.72);
}
.image-modal-panel {
  width: min(96vw, 1280px);
  max-height: 94vh;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 20px 80px rgba(0, 0, 0, 0.45);
  overflow: hidden;
}
.image-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
}
.close-btn {
  border: 1px solid var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text);
  padding: 4px 10px;
  cursor: pointer;
}
.image-modal-img {
  display: block;
  width: 100%;
  max-height: calc(94vh - 48px);
  object-fit: contain;
  border: 0;
  border-radius: 0;
}
.empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 80px;
  color: var(--text-dim);
  border: 1px dashed var(--border);
  border-radius: 4px;
}
</style>

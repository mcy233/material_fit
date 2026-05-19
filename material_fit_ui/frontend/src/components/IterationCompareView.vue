<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { IterationDetail, IterationSummary, ParamChange } from '../types';
import { fetchIterationDetail } from '../api';
import ImageComparison from './ImageComparison.vue';
import MultiviewImageGrid from './MultiviewImageGrid.vue';

const props = defineProps<{ caseId: string; iterations: IterationSummary[] }>();

const leftId = ref<string>('');
const rightId = ref<string>('');
const leftDetail = ref<IterationDetail | null>(null);
const rightDetail = ref<IterationDetail | null>(null);
const error = ref<string | null>(null);

watch(
  () => props.iterations,
  (iters) => {
    if (!iters.length) {
      leftId.value = '';
      rightId.value = '';
      return;
    }
    if (!iters.some((i) => i.iter_id === leftId.value)) {
      leftId.value = iters[0].iter_id;
    }
    if (!iters.some((i) => i.iter_id === rightId.value)) {
      rightId.value = iters[Math.min(iters.length - 1, 1)].iter_id ?? iters[0].iter_id;
    }
  },
  { immediate: true },
);

async function loadSide(side: 'left' | 'right'): Promise<void> {
  const id = side === 'left' ? leftId.value : rightId.value;
  const target = side === 'left' ? leftDetail : rightDetail;
  if (!props.caseId || !id) {
    target.value = null;
    return;
  }
  try {
    target.value = await fetchIterationDetail(props.caseId, id);
    error.value = null;
  } catch (err) {
    target.value = null;
    error.value = err instanceof Error ? err.message : String(err);
  }
}

watch([() => props.caseId, leftId], () => { void loadSide('left'); }, { immediate: true });
watch([() => props.caseId, rightId], () => { void loadSide('right'); }, { immediate: true });

interface CompareRow {
  param: string;
  leftValue: unknown;
  rightValue: unknown;
  leftDelta?: string;
  rightDelta?: string;
}

const allChanges = computed<CompareRow[]>(() => {
  const left = leftDetail.value?.candidate_params ?? null;
  const right = rightDetail.value?.candidate_params ?? null;
  if (!left && !right) return [];
  const keys = new Set<string>([
    ...(left ? Object.keys(left) : []),
    ...(right ? Object.keys(right) : []),
  ]);
  const rows: CompareRow[] = [];
  for (const key of keys) {
    const leftValue = left ? left[key] : undefined;
    const rightValue = right ? right[key] : undefined;
    if (deepEqual(leftValue, rightValue)) continue;
    rows.push({ param: key, leftValue, rightValue });
  }
  rows.sort((a, b) => a.param.localeCompare(b.param));
  return rows;
});

const leftChangeMap = computed(() => mapChanges(leftDetail.value?.decision?.decision?.changes ?? []));
const rightChangeMap = computed(() => mapChanges(rightDetail.value?.decision?.decision?.changes ?? []));

function mapChanges(changes: ParamChange[]): Map<string, ParamChange> {
  const map = new Map<string, ParamChange>();
  for (const change of changes) {
    map.set(change.param, change);
  }
  return map;
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((item, idx) => deepEqual(item, b[idx]));
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a as Record<string, unknown>);
    const kb = Object.keys(b as Record<string, unknown>);
    if (ka.length !== kb.length) return false;
    return ka.every((key) => deepEqual((a as Record<string, unknown>)[key], (b as Record<string, unknown>)[key]));
  }
  return false;
}

function formatValue(value: unknown): string {
  if (value === undefined) return '—';
  if (value === null) return 'null';
  if (typeof value === 'number') return formatNumber(value);
  if (Array.isArray(value)) {
    return '[' + value.map((item) => (typeof item === 'number' ? formatNumber(item) : String(item))).join(', ') + ']';
  }
  return JSON.stringify(value);
}

function formatNumber(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  if (Math.abs(value) < 0.0001 && value !== 0) return value.toExponential(2);
  return Number(value).toFixed(4);
}

function fmt(value: unknown, digits = 4): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

function isColorLike(name: string): boolean {
  return /color|tint|shadow|emission|specular|fresnel|matcap|ibl/i.test(name);
}

function colorCss(value: unknown): string {
  if (!Array.isArray(value) || value.length < 3) return 'transparent';
  const clamp = (v: unknown): number => {
    const n = typeof v === 'number' ? v : 0;
    return Math.max(0, Math.min(1, n));
  };
  const r = Math.round(clamp(value[0]) * 255);
  const g = Math.round(clamp(value[1]) * 255);
  const b = Math.round(clamp(value[2]) * 255);
  return `rgb(${r}, ${g}, ${b})`;
}

const leftSummary = computed(() => summarize(leftDetail.value));
const rightSummary = computed(() => summarize(rightDetail.value));

function summarize(detail: IterationDetail | null) {
  if (!detail) return null;
  const dec = detail.decision;
  const inner = dec?.decision;
  return {
    iter: detail.iter_id,
    fit: dec?.fit_score_before ?? null,
    mae: dec?.diff_score_before ?? detail.diff_analysis?.score ?? null,
    worstView: dec?.multiview_analysis?.summary?.worst_view_id ?? null,
    worstFit: dec?.multiview_analysis?.summary?.worst_fit_score ?? null,
    stage: dec?.selected_stage ?? null,
    changes: inner?.changes?.length ?? 0,
    stop: inner?.stop_reason ?? null,
  };
}
</script>

<template>
  <div class="compare-view">
    <header class="compare-header">
      <h3 class="section-title" style="margin: 0;">迭代对比</h3>
      <span class="muted small">从下面选两轮，左右展示图像、决策、参数差异</span>
    </header>

    <div class="compare-pickers">
      <label class="compare-picker">
        <span class="muted small">left</span>
        <select v-model="leftId">
          <option v-for="iter in iterations" :key="iter.iter_id" :value="iter.iter_id">
            #{{ iter.iteration }} · {{ iter.iter_id }} · {{ iter.selected_stage ?? iter.kind }}
          </option>
        </select>
      </label>
      <label class="compare-picker">
        <span class="muted small">right</span>
        <select v-model="rightId">
          <option v-for="iter in iterations" :key="iter.iter_id" :value="iter.iter_id">
            #{{ iter.iteration }} · {{ iter.iter_id }} · {{ iter.selected_stage ?? iter.kind }}
          </option>
        </select>
      </label>
    </div>

    <div v-if="error" class="error-banner">{{ error }}</div>

    <div class="compare-grid">
      <section class="compare-side">
        <div class="compare-side-head">
          <span class="mono">{{ leftSummary?.iter ?? '—' }}</span>
          <span v-if="leftSummary" class="muted small">
            stage <span class="mono">{{ leftSummary.stage ?? '—' }}</span>
          </span>
        </div>
        <div class="iter-summary" v-if="leftSummary">
          <span class="stat-pill">fit <strong>{{ fmt(leftSummary.fit) }}</strong></span>
          <span class="stat-pill">mae <strong>{{ fmt(leftSummary.mae) }}</strong></span>
          <span v-if="leftSummary.worstView" class="stat-pill">worst <strong>{{ leftSummary.worstView }} · {{ fmt(leftSummary.worstFit) }}</strong></span>
          <span class="stat-pill">changes <strong>{{ leftSummary.changes }}</strong></span>
          <span v-if="leftSummary.stop" class="stat-pill">stop <strong>{{ leftSummary.stop }}</strong></span>
        </div>
        <MultiviewImageGrid
          v-if="leftDetail?.multiview_images?.length"
          :items="leftDetail.multiview_images"
          :title="`${leftDetail.iter_id} · 多视角对比 · ${leftDetail.multiview_images.length} views`"
        />
        <ImageComparison v-else-if="leftDetail" :images="leftDetail.images" :context-label="leftDetail.iter_id" />
      </section>

      <section class="compare-side">
        <div class="compare-side-head">
          <span class="mono">{{ rightSummary?.iter ?? '—' }}</span>
          <span v-if="rightSummary" class="muted small">
            stage <span class="mono">{{ rightSummary.stage ?? '—' }}</span>
          </span>
        </div>
        <div class="iter-summary" v-if="rightSummary">
          <span class="stat-pill">fit <strong>{{ fmt(rightSummary.fit) }}</strong></span>
          <span class="stat-pill">mae <strong>{{ fmt(rightSummary.mae) }}</strong></span>
          <span v-if="rightSummary.worstView" class="stat-pill">worst <strong>{{ rightSummary.worstView }} · {{ fmt(rightSummary.worstFit) }}</strong></span>
          <span class="stat-pill">changes <strong>{{ rightSummary.changes }}</strong></span>
          <span v-if="rightSummary.stop" class="stat-pill">stop <strong>{{ rightSummary.stop }}</strong></span>
        </div>
        <MultiviewImageGrid
          v-if="rightDetail?.multiview_images?.length"
          :items="rightDetail.multiview_images"
          :title="`${rightDetail.iter_id} · 多视角对比 · ${rightDetail.multiview_images.length} views`"
        />
        <ImageComparison v-else-if="rightDetail" :images="rightDetail.images" :context-label="rightDetail.iter_id" />
      </section>
    </div>

    <section class="section">
      <h3 class="section-title">参数差异（仅展示左右不同的参数）</h3>
      <p v-if="!allChanges.length" class="muted small">两轮 candidate_params 完全一致。</p>
      <table v-else class="compare-table">
        <thead>
          <tr>
            <th>参数</th>
            <th>{{ leftId }}</th>
            <th>{{ rightId }}</th>
            <th>左侧本轮决策</th>
            <th>右侧本轮决策</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in allChanges" :key="row.param">
            <td><span class="mono">{{ row.param }}</span></td>
            <td class="numeric">
              <span v-if="isColorLike(row.param) && Array.isArray(row.leftValue)" class="color-swatch" :style="{ background: colorCss(row.leftValue) }" />
              <span class="mono">{{ formatValue(row.leftValue) }}</span>
            </td>
            <td class="numeric">
              <span v-if="isColorLike(row.param) && Array.isArray(row.rightValue)" class="color-swatch" :style="{ background: colorCss(row.rightValue) }" />
              <span class="mono">{{ formatValue(row.rightValue) }}</span>
            </td>
            <td class="muted small">
              {{ leftChangeMap.get(row.param) ? leftChangeMap.get(row.param)!.reason ?? '已变更' : '—' }}
            </td>
            <td class="muted small">
              {{ rightChangeMap.get(row.param) ? rightChangeMap.get(row.param)!.reason ?? '已变更' : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.compare-view { display: flex; flex-direction: column; gap: 12px; padding-bottom: 24px; }
.compare-header { display: flex; align-items: baseline; gap: 12px; }
.compare-pickers {
  display: flex;
  gap: 16px;
  padding: 8px 12px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.compare-picker { display: flex; flex-direction: column; gap: 2px; flex: 1; }
.compare-picker select { padding: 4px 6px; }
.compare-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.compare-side {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.compare-side-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 6px;
}
.iter-summary { display: flex; flex-wrap: wrap; gap: 8px; }
.compare-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.compare-table th, .compare-table td {
  border-bottom: 1px solid var(--border);
  padding: 5px 8px;
  text-align: left;
  vertical-align: top;
}
.compare-table th { color: var(--text-muted); font-weight: 500; }
.compare-table td.numeric { font-family: var(--mono); }
.color-swatch {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: 4px;
  vertical-align: -2px;
  border: 1px solid var(--border-strong);
  border-radius: 2px;
}
@media (max-width: 1100px) {
  .compare-grid { grid-template-columns: 1fr; }
}
</style>

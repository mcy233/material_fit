<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  externalPreviewUrl,
  fetchLastLayaRefreshPreflight,
  runLayaRefreshPreflight,
} from '../api';
import type { PreflightResult } from '../types';

const props = defineProps<{ projectId: string; lmatPath: string | null; regionFilled: boolean }>();

const result = ref<PreflightResult | null>(null);
const running = ref(false);
const error = ref<string | null>(null);
const probeParam = ref('u_BaseColor');
const changeThreshold = ref('');
const restoreThreshold = ref('');
// Probe writes to fixed paths (preflight/{baseline,probe,restored}.png),
// so its image URLs stay constant between runs and the browser
// happily serves the previous run's cached pixels. Bumping this
// after each successful probe forces an unconditional reload by
// changing the URL query string.
const cacheBust = ref(Date.now());

const canRun = computed(() => !running.value && !!props.lmatPath && props.regionFilled);

function bustedSrc(path: string | null | undefined): string {
  if (!path) return '';
  const base = externalPreviewUrl(path);
  if (!base) return '';
  const sep = base.includes('?') ? '&' : '?';
  return `${base}${sep}t=${cacheBust.value}`;
}

async function load(): Promise<void> {
  if (!props.projectId) return;
  try {
    result.value = await fetchLastLayaRefreshPreflight(props.projectId);
    syncThresholdInputs(result.value);
    cacheBust.value = Date.now();
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function run(): Promise<void> {
  if (!canRun.value) return;
  running.value = true;
  error.value = null;
  try {
    const change = parseThreshold(changeThreshold.value);
    const restore = parseThreshold(restoreThreshold.value);
    result.value = await runLayaRefreshPreflight(props.projectId, {
      probe_param: probeParam.value || 'u_BaseColor',
      ...(change == null ? {} : { mean_diff_change_threshold: change }),
      ...(restore == null ? {} : { mean_diff_restore_threshold: restore }),
    });
    syncThresholdInputs(result.value);
    cacheBust.value = Date.now();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    running.value = false;
  }
}

watch(() => props.projectId, () => { void load(); });
onMounted(() => { void load(); });

function pct(value: number | undefined | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

function fmtDiff(value: number | undefined | null): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return value.toFixed(2);
}

function parseThreshold(value: unknown): number | null {
  const trimmed = String(value ?? '').trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function syncThresholdInputs(next: PreflightResult | null): void {
  if (!next) return;
  if (!changeThreshold.value && typeof next.mean_diff_change_threshold === 'number') {
    changeThreshold.value = fmtDiff(next.mean_diff_change_threshold);
  }
  if (!restoreThreshold.value && typeof next.mean_diff_restore_threshold === 'number') {
    restoreThreshold.value = fmtDiff(next.mean_diff_restore_threshold);
  }
}

const anyFocusFailed = computed(
  () => Boolean(result.value?.focus_log?.some((e) => !e.success)),
);

const diffOverChange = computed(() => {
  const r = result.value;
  if (!r) return false;
  const v = r.mean_diff_baseline_probe;
  const t = r.mean_diff_change_threshold;
  return typeof v === 'number' && typeof t === 'number' && v >= t;
});

const diffUnderRestore = computed(() => {
  const r = result.value;
  if (!r) return false;
  const v = r.mean_diff_baseline_restored;
  const t = r.mean_diff_restore_threshold;
  return typeof v === 'number' && typeof t === 'number' && v <= t;
});
</script>

<template>
  <section class="preflight section">
    <header class="ph-head">
      <div>
        <h3 class="section-title" style="margin: 0 0 4px;">验证 Laya 是否真的在刷新（推荐先点这个）</h3>
        <p class="muted small" style="margin: 0;">
          往 <span class="mono">{{ probeParam || 'u_BaseColor' }}</span> 写一个洋红色（{{ '[1, 0, 1, 1]' }}），等
          rerender_wait_ms 后截屏，再还原 .lmat 后再截屏。
          下面用 <strong>逐像素平均色差</strong>判定：probe 跟 baseline 显著不同 + restored 又回到 baseline 附近 = 通过。
          这个判定对各种贴图材质都鲁棒，不依赖"显著变红"。
        </p>
      </div>
      <div class="ph-controls">
        <input v-model="probeParam" class="probe-input" placeholder="u_BaseColor" :disabled="running" />
        <input
          v-model="changeThreshold"
          class="probe-input threshold-input"
          type="number"
          min="0"
          step="0.1"
          placeholder="变化阈值 0.5"
          title="baseline 到 probe 的最小平均色差；设为 0 表示任意非零色差都算刷新"
          :disabled="running"
        />
        <input
          v-model="restoreThreshold"
          class="probe-input threshold-input"
          type="number"
          min="0"
          step="0.1"
          placeholder="还原阈值 2.5"
          title="baseline 到 restored 的平均色差必须小于等于该值"
          :disabled="running"
        />
        <button class="primary" @click="run" :disabled="!canRun">
          {{ running ? '探测中…' : '运行 Laya 刷新探针' }}
        </button>
      </div>
    </header>

    <p v-if="!props.lmatPath" class="muted small">先在上面的"输入文件"里选好 Laya .lmat 才能跑探针。</p>
    <p v-else-if="!props.regionFilled" class="muted small">先在"Laya 截图区域"里框选好截图矩形才能跑探针。</p>
    <p v-if="error" class="error-banner">{{ error }}</p>

    <div v-if="result" class="ph-body">
      <div class="ph-status" :class="{ ok: result.success, bad: !result.success }">
        <span class="ph-status-icon">{{ result.success ? '✓' : '✗' }}</span>
        <span class="ph-status-text">{{ result.success ? '通过：Laya 在 .lmat 写入后真的刷新了' : '未通过：' + result.reason }}</span>
      </div>
      <div v-if="result.success" class="muted small" style="margin-top: 4px;">{{ result.reason }}</div>

      <table class="ph-table">
        <thead>
          <tr>
            <th>baseline（原始）</th>
            <th>probe（洋红探针）</th>
            <th>restored（还原）</th>
          </tr>
        </thead>
        <tbody>
          <tr class="ph-imgrow">
            <td>
              <img v-if="result.captures.baseline" :src="bustedSrc(result.captures.baseline)" alt="baseline capture" />
              <span v-else class="muted small">无</span>
            </td>
            <td>
              <img v-if="result.captures.probe" :src="bustedSrc(result.captures.probe)" alt="probe capture" />
              <span v-else class="muted small">无</span>
            </td>
            <td>
              <img v-if="result.captures.restored" :src="bustedSrc(result.captures.restored)" alt="restored capture" />
              <span v-else class="muted small">无</span>
            </td>
          </tr>
          <tr class="ph-ratiorow">
            <td>
              <span class="muted small">基准帧</span>
              <br />
              <span class="muted small">洋红像素占比：{{ pct(result.magenta_ratio_baseline) }}</span>
            </td>
            <td>
              色差 vs baseline：
              <strong :class="{ 'metric-good': diffOverChange, 'metric-bad': !diffOverChange }">
                {{ fmtDiff(result.mean_diff_baseline_probe) }}
              </strong>
              <span class="muted small"> / 阈值 {{ fmtDiff(result.mean_diff_change_threshold) }}</span>
              <br />
              <span class="muted small">洋红像素占比 {{ pct(result.magenta_ratio_probe) }}（辅助）</span>
            </td>
            <td>
              色差 vs baseline：
              <strong :class="{ 'metric-good': diffUnderRestore, 'metric-bad': !diffUnderRestore }">
                {{ fmtDiff(result.mean_diff_baseline_restored) }}
              </strong>
              <span class="muted small"> / 阈值 ≤ {{ fmtDiff(result.mean_diff_restore_threshold) }}</span>
              <br />
              <span class="muted small">洋红像素占比 {{ pct(result.magenta_ratio_restored) }}（辅助）</span>
            </td>
          </tr>
        </tbody>
      </table>

      <p v-if="result.error" class="muted small" style="margin-top: 4px;">
        内部错误：<span class="mono">{{ result.error }}</span>
      </p>
      <ul v-if="result.notes && result.notes.length" class="ph-notes muted small">
        <li v-for="(note, i) in result.notes" :key="i">{{ note }}</li>
      </ul>

      <div v-if="result.focus_log && result.focus_log.length" class="ph-focus">
        <div class="ph-focus-head">
          <strong>Laya 窗口聚焦日志</strong>
          <span class="muted small" :class="{ 'focus-bad': anyFocusFailed }">
            {{ anyFocusFailed
              ? '部分步骤未能成功聚焦 — 探针可能拿到了旧帧'
              : '5 个步骤都已聚焦到 Laya 窗口' }}
          </span>
        </div>
        <table class="ph-focus-table">
          <thead>
            <tr>
              <th>步骤</th>
              <th>结果</th>
              <th>命中窗口</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(entry, i) in result.focus_log" :key="i" :class="{ 'focus-bad-row': !entry.success }">
              <td class="mono">{{ entry.step }}</td>
              <td>{{ entry.success ? '✓' : '✗' }}</td>
              <td class="mono">{{ entry.process_name || '—' }} / {{ entry.title || '—' }}</td>
              <td>{{ entry.reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped>
.preflight {
  border-left: 3px solid var(--accent, #c79a3d);
  padding-left: 10px;
}
.ph-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}
.ph-controls { display: flex; align-items: center; gap: 8px; }
.probe-input {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: var(--mono);
  width: 160px;
}
.threshold-input { width: 112px; }
.ph-body { margin-top: 12px; }
.ph-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  font-weight: 600;
}
.ph-status.ok { background: rgba(80, 200, 120, 0.12); color: var(--good); }
.ph-status.bad { background: rgba(220, 90, 90, 0.12); color: var(--bad, #d96060); }
.ph-status-icon { font-family: var(--mono); }
.ph-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
}
.ph-table th {
  text-align: left;
  padding: 4px 8px;
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.ph-imgrow td { padding: 6px; vertical-align: top; }
.ph-imgrow img {
  max-width: 100%;
  max-height: 220px;
  border: 1px solid var(--border);
  background: #000;
  image-rendering: pixelated;
}
.ph-ratiorow td { padding: 6px 8px; font-family: var(--mono); font-size: 12px; vertical-align: top; }
.metric-good { color: var(--good); }
.metric-bad { color: var(--bad, #d96060); }
.ph-notes { margin: 8px 0 0; padding-left: 16px; }
.ph-focus { margin-top: 12px; }
.ph-focus-head {
  display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
  margin-bottom: 6px;
}
.focus-bad { color: var(--bad, #d96060); }
.ph-focus-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.ph-focus-table th, .ph-focus-table td {
  text-align: left;
  padding: 4px 8px;
  border-bottom: 1px solid var(--border);
}
.ph-focus-table th {
  font-weight: 600;
  background: var(--bg-elevated);
}
.focus-bad-row td { color: var(--bad, #d96060); }
.primary { background: var(--accent-strong); border-color: var(--accent-strong); color: white; }
.primary:disabled { opacity: 0.5; }
</style>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { IterationDetail } from '../types';
import ImageComparison from './ImageComparison.vue';
import MultiviewImageGrid from './MultiviewImageGrid.vue';
import ParamChangesTable from './ParamChangesTable.vue';
import ChannelMetricsTable from './ChannelMetricsTable.vue';
import ResearchMetricsPanel from './ResearchMetricsPanel.vue';

const props = defineProps<{ detail: IterationDetail }>();

type TabKey = 'decision' | 'channels' | 'params' | 'lmat' | 'capture';
const activeTab = ref<TabKey>('decision');

const tabs = computed<Array<{ key: TabKey; label: string }>>(() => {
  const list: Array<{ key: TabKey; label: string }> = [];
  if (props.detail.kind === 'auto_adjust') {
    list.push({ key: 'decision', label: '决策与变化' });
  }
  if (props.detail.diff_analysis) {
    list.push({ key: 'channels', label: '通道分析' });
  }
  if (props.detail.candidate_params) {
    list.push({ key: 'params', label: '本轮参数' });
  }
  if (props.detail.candidate_lmat_text) {
    list.push({ key: 'lmat', label: '候选 .lmat' });
  }
  if (props.detail.capture_request) {
    list.push({ key: 'capture', label: 'capture request' });
  }
  return list;
});

watch(
  () => props.detail.iter_id,
  () => {
    const first = tabs.value[0]?.key;
    if (first) activeTab.value = first;
  },
  { immediate: true },
);

const decision = computed(() => props.detail.decision);
const innerDecision = computed(() => decision.value?.decision ?? null);
const changes = computed(() => innerDecision.value?.changes ?? []);
const stage = computed(() => innerDecision.value?.stage ?? null);

const fitScore = computed(() => decision.value?.fit_score_before ?? null);
const diffScore = computed(() => decision.value?.diff_score_before ?? null);
const targetScore = computed(() => decision.value?.target_score ?? null);
const gain = computed(() => innerDecision.value?.iteration_gain ?? null);

// E-009: surface the per-iteration perceptual signals so the user can
// see what's driving fit_score (channel-weighted MAE / SSIM / mask
// coverage) instead of trusting a single composite scalar.
const perceptualSignals = computed(() => {
  const value = (decision.value as Record<string, unknown> | null | undefined)?.perceptual_signals;
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
});
const perceptualWeightedMae = computed(() => {
  const v = perceptualSignals.value?.weighted_mae;
  return typeof v === 'number' ? v : null;
});
const perceptualSsim = computed(() => {
  const v = perceptualSignals.value?.ssim;
  return typeof v === 'number' ? v : null;
});
const perceptualForegroundRatio = computed(() => {
  const am = perceptualSignals.value?.auto_mask as Record<string, unknown> | undefined;
  const v = am?.foreground_ratio;
  return typeof v === 'number' ? v : null;
});
const humanAcceptSignals = computed(() => {
  const value = perceptualSignals.value?.human_accept;
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
});
const humanAcceptScore = computed(() => {
  const v = humanAcceptSignals.value?.score;
  return typeof v === 'number' ? v : null;
});
const humanAcceptComponents = computed(() => {
  const value = humanAcceptSignals.value?.components;
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
});
const multiviewSummary = computed(() => decision.value?.multiview_analysis?.summary ?? null);
const multiviewViews = computed(() => decision.value?.multiview_analysis?.views ?? []);
const multiviewCount = computed(() => decision.value?.multiview_analysis?.pair_count ?? props.detail.multiview_images?.length ?? 0);
const researchSignals = computed(() => {
  const fromDecision = perceptualSignals.value?.research_metrics;
  if (fromDecision && typeof fromDecision === 'object') return fromDecision as Record<string, unknown>;
  const fromAnalysis = props.detail.diff_analysis?.research_metrics;
  return fromAnalysis && typeof fromAnalysis === 'object' ? fromAnalysis as Record<string, unknown> : null;
});

const candidateParamsJson = computed(() => {
  if (!props.detail.candidate_params) return '';
  return JSON.stringify(props.detail.candidate_params, null, 2);
});

const captureRequestJson = computed(() => {
  if (!props.detail.capture_request) return '';
  return JSON.stringify(props.detail.capture_request, null, 2);
});

const screenCapture = computed(() => {
  const value = decision.value?.screen_capture_after_apply;
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
});

const diffAnalysisOnly = computed(() => {
  return !!props.detail.diff_analysis && !decision.value;
});

const diffScoreFromAnalysis = computed(() => {
  const score = props.detail.diff_analysis?.score;
  return typeof score === 'number' ? score : null;
});

function fmt(value: unknown, digits = 4): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

const headerStage = computed(() => {
  if (props.detail.kind === 'probe') return 'probe candidate';
  if (props.detail.kind === 'diff_only') return 'root diff';
  return decision.value?.selected_stage ?? '—';
});

const headerNote = computed(() => props.detail._note ?? null);
</script>

<template>
  <div>
    <MultiviewImageGrid
      v-if="detail.multiview_images?.length"
      :items="detail.multiview_images"
      :title="`${detail.iter_id} · 多视角图像对比 · ${detail.multiview_images.length} views`"
    />
    <ImageComparison v-else :images="detail.images" />

    <section class="section">
      <h3 class="section-title">本轮 · {{ detail.iter_id }} · {{ headerStage }}</h3>

      <!-- auto_adjust full summary -->
      <div v-if="detail.kind === 'auto_adjust'" class="iter-summary">
        <span class="stat-pill">fit before <strong>{{ fmt(fitScore) }}</strong></span>
        <span class="stat-pill">RGB MAE <strong>{{ fmt(diffScore) }}</strong></span>
        <span class="stat-pill">target <strong>{{ fmt(targetScore, 3) }}</strong></span>
        <span class="stat-pill">gain <strong>{{ fmt(gain, 3) }}</strong></span>
        <span class="stat-pill">stop <strong>{{ innerDecision?.stop_reason ?? '—' }}</strong></span>
      </div>

      <div v-if="detail.kind === 'auto_adjust' && multiviewSummary" class="iter-summary perceptual">
        <span class="stat-pill stat-pill--muted small">多视角聚合 · {{ multiviewCount }} views</span>
        <span class="stat-pill">mean fit <strong>{{ fmt(multiviewSummary.mean_fit_score) }}</strong></span>
        <span class="stat-pill">worst <strong>{{ multiviewSummary.worst_view_id ?? '—' }}</strong></span>
        <span class="stat-pill">worst fit <strong>{{ fmt(multiviewSummary.worst_fit_score) }}</strong></span>
        <span class="stat-pill">p90 loss <strong>{{ fmt(multiviewSummary.p90_loss) }}</strong></span>
      </div>

      <ResearchMetricsPanel
        :metrics="researchSignals"
        :multiview-summary="multiviewSummary"
        :multiview-views="multiviewViews"
        :multiview-count="multiviewCount"
      />

      <!-- E-009 perceptual signals: only present once a run has executed
           with the new metric. Older decision.json entries don't have this
           block and the row gracefully hides itself. -->
      <div v-if="detail.kind === 'auto_adjust' && perceptualSignals" class="iter-summary perceptual">
        <span v-if="humanAcceptScore != null" class="stat-pill stat-pill--accent" title="人类可接受度评分：当前默认优化目标">
          human <strong>{{ fmt(humanAcceptScore) }}</strong>
        </span>
        <span class="stat-pill stat-pill--accent" title="加权 MAE = sum(channel_w * channel_mae)，去背景后的 model 像素 MAE">
          weighted MAE <strong>{{ fmt(perceptualWeightedMae) }}</strong>
        </span>
        <span class="stat-pill stat-pill--accent" title="结构相似性，对 1px 位移有容忍">
          SSIM <strong>{{ fmt(perceptualSsim, 3) }}</strong>
        </span>
        <span
          class="stat-pill stat-pill--accent"
          :title="`auto-mask 识别出的前景占比（candidate bg 占 ${fmt((perceptualSignals.auto_mask as any)?.candidate_bg_ratio, 3)}）`"
        >
          fg ratio <strong>{{ fmt(perceptualForegroundRatio, 3) }}</strong>
        </span>
        <span class="stat-pill stat-pill--muted small">E-009 指标</span>
      </div>

      <div v-if="detail.kind === 'auto_adjust' && humanAcceptComponents" class="iter-summary perceptual">
        <span class="stat-pill stat-pill--muted small">human components</span>
        <span
          v-for="(value, key) in humanAcceptComponents"
          :key="String(key)"
          class="stat-pill stat-pill--accent"
        >
          {{ key }} <strong>{{ fmt(value) }}</strong>
        </span>
      </div>

      <!-- diff_only summary -->
      <div v-else-if="detail.kind === 'diff_only'" class="iter-summary">
        <span class="stat-pill">RGB MAE <strong>{{ fmt(diffScoreFromAnalysis) }}</strong></span>
        <span v-if="diffScoreFromAnalysis != null" class="stat-pill">
          fit (=1−MAE) <strong>{{ fmt(1 - diffScoreFromAnalysis) }}</strong>
        </span>
      </div>

      <!-- probe summary -->
      <div v-else-if="detail.kind === 'probe'" class="iter-summary">
        <span class="stat-pill">仅 candidate params，<strong>无截图无评分</strong></span>
      </div>

      <p v-if="headerNote" class="muted small" style="margin-top: 6px;">{{ headerNote }}</p>

      <p v-if="stage?.description" class="muted small" style="margin-top: 6px;">{{ stage.description }}</p>
      <p v-if="innerDecision?.applied_lmat" class="muted small">
        已写入 <span class="mono">{{ innerDecision.applied_lmat }}</span>
      </p>
      <p v-if="innerDecision?.backup_lmat" class="muted small">
        备份 <span class="mono">{{ innerDecision.backup_lmat }}</span>
      </p>
      <p v-if="screenCapture && screenCapture.output_path" class="muted small">
        重渲染后截图 <span class="mono">{{ screenCapture.output_path }}</span>
      </p>
    </section>

    <section class="section">
      <div v-if="tabs.length" class="tab-bar">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="tab-btn"
          :class="{ 'is-active': activeTab === tab.key }"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
        </button>
      </div>

      <div v-show="activeTab === 'decision' && detail.kind === 'auto_adjust'">
        <ParamChangesTable :changes="changes" />
      </div>

      <div v-show="activeTab === 'channels' && detail.diff_analysis">
        <p v-if="detail.kind === 'auto_adjust' && decision?.multiview_analysis" class="muted small" style="margin-bottom: 8px;">
          这是多视角聚合通道分析；优化器使用的也是这份聚合信号，不再只取第一视角。
        </p>
        <p v-if="diffAnalysisOnly" class="muted small" style="margin-bottom: 8px;">
          这是一次性 <span class="mono">analyze_diff</span> 的产物，没有 decision，仅展示通道分析。
        </p>
        <ChannelMetricsTable :diff-analysis="detail.diff_analysis" />
      </div>

      <div v-show="activeTab === 'params' && detail.candidate_params">
        <pre class="params-pane">{{ candidateParamsJson }}</pre>
      </div>

      <div v-show="activeTab === 'lmat' && detail.candidate_lmat_text">
        <pre class="params-pane">{{ detail.candidate_lmat_text }}</pre>
      </div>

      <div v-show="activeTab === 'capture' && detail.capture_request">
        <pre class="params-pane">{{ captureRequestJson }}</pre>
      </div>

      <p v-if="!tabs.length" class="muted small">本轮没有可显示的详情数据。</p>
    </section>
  </div>
</template>

<style scoped>
.iter-summary { display: flex; gap: 8px; flex-wrap: wrap; }
.iter-summary.perceptual { margin-top: 6px; }
.stat-pill--accent { background: rgba(53, 132, 228, 0.12); border-color: rgba(53, 132, 228, 0.3); }
.stat-pill--muted { opacity: 0.6; }
.mono { font-family: var(--mono); }
</style>

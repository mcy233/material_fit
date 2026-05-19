<script setup lang="ts">
import { computed, ref } from 'vue';

const props = defineProps<{
  metrics: Record<string, unknown> | null;
  multiviewSummary?: unknown;
  multiviewViews?: unknown[];
  multiviewCount?: number;
}>();

interface MetricDetailSpec {
  path?: string[];
  viewKey?: string;
  vector?: boolean;
}

interface MetricItem {
  label: string;
  value: number | string | null;
  unit?: string;
  digits?: number;
  hint: string;
  tone?: 'good' | 'warn' | 'bad' | 'neutral';
  detail?: MetricDetailSpec;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null;
}

function numberAt(record: Record<string, unknown> | null | undefined, key: string): number | null {
  const value = record?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function vectorAt(record: Record<string, unknown> | null | undefined, key: string): number[] | null {
  const value = record?.[key];
  if (!Array.isArray(value)) return null;
  const numbers = value.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
  return numbers.length === value.length && numbers.length > 0 ? numbers : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function fmt(value: MetricItem['value'], digits = 3): string {
  if (value == null) return '—';
  if (typeof value === 'string') return value;
  return value.toFixed(digits);
}

function valueAtPath(root: unknown, path: string[] | undefined): unknown {
  let cur = root;
  for (const key of path ?? []) {
    cur = asRecord(cur)?.[key];
  }
  return cur;
}

function fmtDetailValue(value: unknown, item: MetricItem): string {
  if (typeof value === 'boolean') return value ? '通过' : '失败';
  if (typeof value === 'number' && Number.isFinite(value)) return value.toFixed(item.digits ?? 3);
  if (Array.isArray(value)) {
    const numbers = value.filter((entry): entry is number => typeof entry === 'number' && Number.isFinite(entry));
    if (numbers.length === value.length && numbers.length > 0) {
      return `[${numbers.map((entry) => entry.toFixed(3)).join(', ')}]`;
    }
  }
  if (typeof value === 'string' && value) return value;
  return '—';
}

const hasMetrics = computed(() => !!props.metrics);

const scientific = computed(() => {
  const value = props.metrics?.scientific ?? props.metrics?.aggregated_scientific;
  return asRecord(value);
});

const colorAccuracy = computed(() => asRecord(scientific.value?.color_accuracy));
const luminanceStructure = computed(() => asRecord(scientific.value?.luminance_structure));
const highlightReflection = computed(() => asRecord(scientific.value?.highlight_reflection));
const detailTexture = computed(() => asRecord(scientific.value?.detail_texture));
const perceptualOptional = computed(() => asRecord(scientific.value?.perceptual_optional));
const validity = computed(() => asRecord(props.metrics?.validity));
const masks = computed(() => asRecord(props.metrics?.masks));
const components = computed(() => asRecord(props.metrics?.components));

function directOrAggregate(section: Record<string, unknown> | null, directKey: string, aggregateKey: string): number | null {
  const direct = numberAt(section, directKey);
  if (direct != null) return direct;
  return numberAt(asRecord(scientific.value?.[aggregateKey]), 'mean');
}

function directOrAggregateVector(section: Record<string, unknown> | null, directKey: string, aggregateKey: string): string | null {
  const direct = vectorAt(section, directKey);
  const aggregate = vectorAt(asRecord(scientific.value?.[aggregateKey]), 'mean');
  const value = direct ?? aggregate;
  return value ? `[${value.map((item) => item.toFixed(3)).join(', ')}]` : null;
}

const researchScore = computed(() => {
  const fromMetrics = numberAt(props.metrics, 'score');
  return fromMetrics ?? numberAt(asRecord(props.multiviewSummary), 'research_score');
});

const researchLoss = computed(() => {
  const fromMetrics = numberAt(props.metrics, 'loss');
  return fromMetrics ?? numberAt(asRecord(props.multiviewSummary), 'research_loss');
});

const validityPassed = computed(() => {
  const value = validity.value?.passed;
  return typeof value === 'boolean' ? value : null;
});

const validityReasons = computed(() => stringList(validity.value?.reasons));
const scoreUsesInvalidViews = computed(() => props.metrics?.score_uses_invalid_views === true);
const activeMetricLabel = ref<string | null>(null);
const multiviewRecords = computed(() => (props.multiviewViews ?? [])
  .map((item) => asRecord(item))
  .filter((item): item is Record<string, unknown> => !!item));

function toggleMetric(item: MetricItem): void {
  if (!item.detail || multiviewRecords.value.length === 0) return;
  activeMetricLabel.value = activeMetricLabel.value === item.label ? null : item.label;
}

function viewMetricRows(item: MetricItem): Array<{ id: string; value: string; valid: string }> {
  if (!item.detail) return [];
  return multiviewRecords.value.map((view, index) => {
    const research = asRecord(view.research_metrics);
    const raw = item.detail?.viewKey
      ? view[item.detail.viewKey]
      : valueAtPath(research, item.detail?.path);
    const valid = view.research_valid;
    return {
      id: String(view.view_id ?? `view_${index}`),
      value: fmtDetailValue(raw, item),
      valid: typeof valid === 'boolean' ? (valid ? '通过' : '失败') : '—',
    };
  });
}

const layer1 = computed<MetricItem[]>(() => [
  {
    label: '有效性',
    value: !hasMetrics.value ? '未生成' : validityPassed.value == null ? null : validityPassed.value ? '通过' : '失败',
    hint: '先判断截图、透明区域和视角是否可信；失败时不能把误差归因于材质。',
    tone: !hasMetrics.value ? 'warn' : validityPassed.value === false ? 'bad' : 'good',
    detail: { viewKey: 'research_valid' },
  },
  {
    label: 'Mask IoU',
    value: numberAt(validity.value, 'mask_iou'),
    digits: 4,
    hint: 'Unity/Laya 前景遮罩重合度，低于阈值通常说明角度、裁剪、透明通道或截图有问题。',
    detail: { path: ['validity', 'mask_iou'] },
  },
  {
    label: 'BBox 中心误差',
    value: numberAt(validity.value, 'bbox_center_error_px'),
    unit: 'px',
    digits: 2,
    hint: '前景包围盒中心偏移，辅助发现视角错位或模型位置不一致。',
    detail: { path: ['validity', 'bbox_center_error_px'] },
  },
  {
    label: '前景占比',
    value: numberAt(masks.value, 'core_ratio'),
    digits: 3,
    hint: 'core_mask 占整张图的比例；过低可能说明 mask 或截图范围异常。',
    detail: { path: ['masks', 'core_ratio'] },
  },
]);

const layer2 = computed<MetricItem[]>(() => [
  {
    label: '平均色差 ΔE00',
    value: directOrAggregate(colorAccuracy.value, 'mean_deltaE00', 'mean_deltaE00'),
    digits: 2,
    hint: '整体颜色准确性主指标，越低越接近；比 RGB MAE 更接近人眼色差感知。',
    detail: { path: ['scientific', 'color_accuracy', 'mean_deltaE00'] },
  },
  {
    label: 'P95 色差 ΔE00',
    value: directOrAggregate(colorAccuracy.value, 'p95_deltaE00', 'p95_deltaE00'),
    digits: 2,
    hint: '局部严重色差指标，防止少数高光、暗部或贴图区域被均值掩盖。',
    detail: { path: ['scientific', 'color_accuracy', 'p95_deltaE00'] },
  },
  {
    label: 'RGB 色彩偏差',
    value: directOrAggregateVector(colorAccuracy.value, 'rgb_bias_candidate_minus_reference', 'rgb_bias_candidate_minus_reference'),
    hint: 'candidate - reference 的 RGB 平均偏差，正值表示 Laya 在该通道偏亮。',
    detail: { path: ['scientific', 'color_accuracy', 'rgb_bias_candidate_minus_reference'], vector: true },
  },
  {
    label: 'Lab 色彩偏差',
    value: directOrAggregateVector(colorAccuracy.value, 'lab_bias_candidate_minus_reference', 'lab_bias_candidate_minus_reference'),
    hint: 'candidate - reference 的 Lab 平均偏差，用来判断明度、红绿、黄蓝方向的系统性偏色。',
    detail: { path: ['scientific', 'color_accuracy', 'lab_bias_candidate_minus_reference'], vector: true },
  },
  {
    label: '亮度 MAE',
    value: directOrAggregate(luminanceStructure.value, 'luminance_mae', 'luminance_mae'),
    digits: 4,
    hint: '整体明暗差异，主要反映曝光、阴影、AO 和亮度响应。',
    detail: { path: ['scientific', 'luminance_structure', 'luminance_mae'] },
  },
  {
    label: '亮度 Bias',
    value: directOrAggregate(luminanceStructure.value, 'luminance_bias', 'luminance_bias'),
    digits: 4,
    hint: 'candidate - reference 的平均亮度偏差，正值偏亮，负值偏暗。',
    detail: { path: ['scientific', 'luminance_structure', 'luminance_bias'] },
  },
  {
    label: 'P95 亮度误差',
    value: directOrAggregate(luminanceStructure.value, 'p95_luminance_abs_error', 'p95_luminance_abs_error'),
    digits: 4,
    hint: '局部严重明暗误差，避免均值掩盖暗部或高光区域问题。',
    detail: { path: ['scientific', 'luminance_structure', 'p95_luminance_abs_error'] },
  },
  {
    label: '亮度结构 SSIM-L',
    value: directOrAggregate(luminanceStructure.value, 'ssim_l', 'ssim_l'),
    digits: 3,
    hint: '亮度通道结构相似性，主要反映阴影层次、轮廓结构和大块明暗是否一致。',
    tone: 'neutral',
    detail: { path: ['scientific', 'luminance_structure', 'ssim_l'] },
  },
  {
    label: '高光色差 ΔE00',
    value: directOrAggregate(highlightReflection.value, 'highlight_deltaE00', 'highlight_deltaE00'),
    digits: 2,
    hint: 'P1 指标，参与 research_loss；衡量参考高光区域内的高光颜色差异。',
    detail: { path: ['scientific', 'highlight_reflection', 'highlight_deltaE00'] },
  },
  {
    label: '高光面积误差',
    value: directOrAggregate(highlightReflection.value, 'highlight_area_error', 'highlight_area_error'),
    digits: 3,
    hint: 'P1 指标，参与 research_loss；衡量高光区域过宽、过窄或缺失。',
    detail: { path: ['scientific', 'highlight_reflection', 'highlight_area_error'] },
  },
  {
    label: '高光亮度 MAE',
    value: directOrAggregate(highlightReflection.value, 'highlight_luminance_mae', 'highlight_luminance_mae'),
    digits: 4,
    hint: 'P1 指标，参与 research_loss；衡量参考高光区域内的亮度差异。',
    detail: { path: ['scientific', 'highlight_reflection', 'highlight_luminance_mae'] },
  },
  {
    label: '峰值亮度误差',
    value: directOrAggregate(highlightReflection.value, 'peak_luminance_error', 'peak_luminance_error'),
    digits: 4,
    hint: 'P1 指标，参与 research_loss；衡量最亮高光峰值是否匹配。',
    detail: { path: ['scientific', 'highlight_reflection', 'peak_luminance_error'] },
  },
  {
    label: '梯度细节误差',
    value: directOrAggregate(detailTexture.value, 'gradient_loss', 'gradient_loss'),
    digits: 4,
    hint: 'P1 指标，参与 research_loss；衡量纹理、法线、边缘和局部对比度差异。',
    detail: { path: ['scientific', 'detail_texture', 'gradient_loss'] },
  },
  {
    label: 'Laplacian 细节误差',
    value: directOrAggregate(detailTexture.value, 'laplacian_loss', 'laplacian_loss'),
    digits: 4,
    hint: 'P1 指标，参与 research_loss；衡量更高频的纹理锐度和边缘细节差异。',
    detail: { path: ['scientific', 'detail_texture', 'laplacian_loss'] },
  },
]);

const layer3 = computed<MetricItem[]>(() => [
  {
    label: '研究总分',
    value: scoreUsesInvalidViews.value ? '不可采信' : researchScore.value,
    unit: scoreUsesInvalidViews.value ? undefined : '/100',
    digits: 1,
    hint: '由科学主指标归一化后聚合得到，用于报告和跨轮次比较。',
    tone: scoreUsesInvalidViews.value ? 'bad' : scoreTone(researchScore.value),
    detail: { viewKey: 'research_score' },
  },
  {
    label: '研究 Loss',
    value: researchLoss.value,
    digits: 4,
    hint: '越低越好；多视角时使用 mean + p90 + max 聚合，避免差视角被平均掩盖。',
    detail: { viewKey: 'research_loss' },
  },
  {
    label: '有效视角',
    value: validViewText.value,
    hint: '通过 validity 检查的视角数量。若存在 invalid view，先检查截图链路再判断材质。',
    tone: invalidViewCount.value > 0 ? 'warn' : 'good',
    detail: { viewKey: 'research_valid' },
  },
]);

const validViewCount = computed(() => numberAt(asRecord(props.multiviewSummary), 'research_valid_view_count'));
const invalidViewCount = computed(() => numberAt(asRecord(props.multiviewSummary), 'research_invalid_view_count') ?? 0);
const validViewText = computed(() => {
  if (validViewCount.value == null) return null;
  return `${validViewCount.value} / ${props.multiviewCount ?? validViewCount.value}`;
});

const layer4 = computed<MetricItem[]>(() => [
  {
    label: '颜色均值项',
    value: numberAt(components.value, 'color_mean'),
    digits: 4,
    hint: 'research_loss 中的归一化颜色均值误差分量。',
    detail: { path: ['components', 'color_mean'] },
  },
  {
    label: '颜色 P95 项',
    value: numberAt(components.value, 'color_p95'),
    digits: 4,
    hint: 'research_loss 中的局部严重色差分量。',
    detail: { path: ['components', 'color_p95'] },
  },
  {
    label: '亮度项',
    value: numberAt(components.value, 'luminance_mae'),
    digits: 4,
    hint: 'research_loss 中的亮度误差分量。',
    detail: { path: ['components', 'luminance_mae'] },
  },
  {
    label: '结构项',
    value: numberAt(components.value, 'structure_ssim_l'),
    digits: 4,
    hint: 'research_loss 中的 SSIM-L 结构误差分量。',
    detail: { path: ['components', 'structure_ssim_l'] },
  },
  {
    label: '高光项',
    value: numberAt(components.value, 'highlight'),
    digits: 4,
    hint: 'P1，高光/反射误差在 research_loss 中的归一化分量。',
    detail: { path: ['components', 'highlight'] },
  },
  {
    label: '细节项',
    value: numberAt(components.value, 'detail_texture'),
    digits: 4,
    hint: 'P1，梯度和 Laplacian 细节误差在 research_loss 中的归一化分量。',
    detail: { path: ['components', 'detail_texture'] },
  },
  {
    label: 'FLIP-like 误差',
    value: directOrAggregate(perceptualOptional.value, 'flip_like_error', 'flip_like_error'),
    digits: 4,
    hint: 'P2，仅供人工查看；这是轻量 FLIP-like 误差，不进入 research_loss。',
    detail: { path: ['scientific', 'perceptual_optional', 'flip_like_error'] },
  },
  {
    label: 'LPIPS',
    value: perceptualOptional.value?.lpips_status === 'unavailable'
      ? '未安装'
      : directOrAggregate(perceptualOptional.value, 'lpips', 'lpips'),
    digits: 4,
    hint: 'P2，仅供人工查看；需要额外深度模型依赖，当前不进入优化目标。',
    detail: { path: ['scientific', 'perceptual_optional', 'lpips'] },
  },
  {
    label: 'DISTS',
    value: perceptualOptional.value?.dists_status === 'unavailable'
      ? '未安装'
      : directOrAggregate(perceptualOptional.value, 'dists', 'dists'),
    digits: 4,
    hint: 'P2，仅供人工查看；需要额外深度模型依赖，当前不进入优化目标。',
    detail: { path: ['scientific', 'perceptual_optional', 'dists'] },
  },
]);

const layers = computed(() => [
  {
    id: 'validity',
    index: '01',
    title: '有效性检查层',
    subtitle: '先确认这张图能不能被评分',
    items: layer1.value,
  },
  {
    id: 'scientific',
    index: '02',
    title: '科学主指标层',
    subtitle: 'P0 + P1：颜色、亮度、结构、高光和细节；P1 已纳入 research loss',
    items: layer2.value,
  },
  {
    id: 'score',
    index: '03',
    title: '研究总分层',
    subtitle: '用于跨轮次、多视角聚合和报告排序',
    items: layer3.value,
  },
  {
    id: 'diagnostic',
    index: '04',
    title: '诊断辅助层',
    subtitle: '解释 loss 来源；P2 感知指标仅供人工查看，暂不参与优化',
    items: layer4.value,
  },
]);

function scoreTone(score: number | null): MetricItem['tone'] {
  if (score == null) return 'neutral';
  if (score >= 90) return 'good';
  if (score >= 75) return 'warn';
  return 'bad';
}
</script>

<template>
  <section class="research-panel" :class="{ 'is-missing': !hasMetrics }">
    <header class="research-header">
      <div>
        <h4>分层评价指标</h4>
        <p>按照有效性检查、科学主指标、研究总分、诊断辅助四层展示，避免把不同语义的数字混在一起。</p>
      </div>
      <div class="score-badge" :class="`tone-${scoreTone(researchScore) || 'neutral'}`">
        <span>研究总分</span>
        <strong>{{ scoreUsesInvalidViews ? '不可采信' : fmt(researchScore, 1) }}</strong>
      </div>
    </header>

    <div v-if="!hasMetrics" class="missing-banner">
      当前这轮产物还没有 <span class="mono">research_metrics</span> 字段。通常说明它是在 P0 分层指标接入前生成的旧运行结果；
      需要重新运行一次调参/重新评分后，下面四层指标才会填入真实数值。
    </div>
    <div v-else-if="scoreUsesInvalidViews" class="missing-banner danger">
      当前没有任何视角通过有效性检查。下方数值仅用于诊断，不应作为材质相似度结论；
      请优先检查 reference/candidate 是否为同一视角、alpha mask 是否一致、模型大小/裁剪是否一致。
    </div>

    <div class="research-layers">
      <article v-for="layer in layers" :key="layer.id" class="metric-layer">
        <div class="layer-title">
          <span class="layer-index">{{ layer.index }}</span>
          <div>
            <h5>{{ layer.title }}</h5>
            <p>{{ layer.subtitle }}</p>
          </div>
        </div>
        <div class="metric-grid">
          <div
            v-for="item in layer.items"
            :key="item.label"
            class="metric-card"
            :class="[item.tone ? `tone-${item.tone}` : '', item.detail && multiviewRecords.length ? 'is-clickable' : '', activeMetricLabel === item.label ? 'is-active' : '']"
            :title="item.hint"
            @click="toggleMetric(item)"
          >
            <span class="metric-label">{{ item.label }}</span>
            <strong>{{ fmt(item.value, item.digits ?? 3) }}<small v-if="item.unit">{{ item.unit }}</small></strong>
            <span class="metric-hint">{{ item.hint }}</span>
          </div>
        </div>
        <div v-if="activeMetricLabel && layer.items.some((item) => item.label === activeMetricLabel)" class="view-breakdown">
          <div class="breakdown-title">
            <strong>{{ activeMetricLabel }}</strong>
            <span>各视角明细</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>视角</th>
                <th>数值</th>
                <th>有效性</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in viewMetricRows(layer.items.find((item) => item.label === activeMetricLabel)!)"
                :key="row.id"
              >
                <td class="mono">{{ row.id }}</td>
                <td class="mono">{{ row.value }}</td>
                <td>{{ row.valid }}</td>
              </tr>
            </tbody>
          </table>
          <p
            v-if="viewMetricRows(layer.items.find((item) => item.label === activeMetricLabel)!).every((row) => row.value === '—')"
            class="breakdown-note"
          >
            当前运行结果没有保存该指标的子视角明细；重新运行后会写入每个 view 的 research_metrics。
          </p>
        </div>
      </article>
    </div>

    <p v-if="validityReasons.length" class="validity-warning">
      有效性警告：{{ validityReasons.join('；') }}
    </p>
  </section>
</template>

<style scoped>
.research-panel {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid rgba(92, 184, 92, 0.28);
  border-radius: var(--radius);
  background: linear-gradient(180deg, rgba(92, 184, 92, 0.08), rgba(92, 184, 92, 0.03));
}
.research-panel.is-missing {
  border-color: rgba(245, 166, 35, 0.35);
  background: linear-gradient(180deg, rgba(245, 166, 35, 0.08), rgba(245, 166, 35, 0.03));
}
.research-header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 12px;
}
.research-header h4 {
  margin: 0 0 4px;
  font-size: 13px;
}
.research-header p,
.layer-title p,
.metric-hint {
  margin: 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.35;
}
.score-badge {
  min-width: 96px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  text-align: right;
  background: var(--bg-panel);
}
.missing-banner {
  margin-bottom: 12px;
  padding: 9px 10px;
  border: 1px solid rgba(245, 166, 35, 0.38);
  border-radius: 6px;
  background: rgba(245, 166, 35, 0.10);
  color: var(--text);
  font-size: 12px;
  line-height: 1.45;
}
.missing-banner.danger {
  border-color: rgba(220, 53, 69, 0.40);
  background: rgba(220, 53, 69, 0.10);
}
.mono {
  font-family: var(--mono);
}
.score-badge span,
.metric-label {
  display: block;
  color: var(--text-muted);
  font-size: 11px;
}
.score-badge strong {
  font-size: 22px;
  line-height: 1.1;
}
.research-layers {
  display: grid;
  gap: 10px;
}
.metric-layer {
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.025);
}
.layer-title {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 8px;
}
.layer-index {
  flex: 0 0 auto;
  min-width: 28px;
  padding: 2px 6px;
  border-radius: 999px;
  background: rgba(92, 184, 92, 0.18);
  color: var(--text);
  font-family: var(--mono);
  font-size: 11px;
  text-align: center;
}
.layer-title h5 {
  margin: 0 0 2px;
  font-size: 12px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
}
.metric-card {
  min-height: 76px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg-elevated);
}
.metric-card.is-clickable {
  cursor: pointer;
  transition: border-color 120ms ease, transform 120ms ease, background 120ms ease;
}
.metric-card.is-clickable:hover,
.metric-card.is-active {
  border-color: rgba(92, 184, 92, 0.60);
  background: rgba(92, 184, 92, 0.08);
}
.metric-card.is-clickable:hover {
  transform: translateY(-1px);
}
.metric-card strong {
  display: block;
  margin: 3px 0;
  font-family: var(--mono);
  font-size: 16px;
}
.metric-card small {
  margin-left: 2px;
  font-size: 10px;
  color: var(--text-muted);
}
.tone-good {
  border-color: rgba(92, 184, 92, 0.42);
  background: rgba(92, 184, 92, 0.10);
}
.tone-warn {
  border-color: rgba(245, 166, 35, 0.45);
  background: rgba(245, 166, 35, 0.10);
}
.tone-bad {
  border-color: rgba(220, 53, 69, 0.45);
  background: rgba(220, 53, 69, 0.10);
}
.validity-warning {
  margin: 10px 0 0;
  padding: 8px;
  border: 1px solid rgba(220, 53, 69, 0.35);
  border-radius: 6px;
  color: var(--bad);
  background: rgba(220, 53, 69, 0.08);
  font-size: 12px;
}
.view-breakdown {
  margin-top: 10px;
  padding: 8px;
  border: 1px solid rgba(92, 184, 92, 0.24);
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.12);
}
.breakdown-title {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
  color: var(--text);
  font-size: 12px;
}
.breakdown-title span,
.breakdown-note {
  color: var(--text-muted);
}
.view-breakdown table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
.view-breakdown th,
.view-breakdown td {
  padding: 5px 6px;
  border-top: 1px solid var(--border);
  text-align: left;
}
.view-breakdown th {
  color: var(--text-muted);
  font-weight: 500;
}
.breakdown-note {
  margin: 6px 0 0;
  font-size: 11px;
}
</style>

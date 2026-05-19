<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { fetchPreanalysis, fetchProject, runPreanalysis, setManualMapping } from '../api';
import type { ParamMappingRow, PreanalysisPayload, ProjectDetail } from '../types';
import LayaControlSchemaPanel from './LayaControlSchemaPanel.vue';

const props = defineProps<{ projectId: string }>();

const data = ref<PreanalysisPayload | null>(null);
const project = ref<ProjectDetail | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref<string | null>(null);
const useLlm = ref(true);
const filter = ref<'all' | 'manual' | 'curated' | 'exact' | 'fuzzy' | 'unity_only' | 'laya_only'>('all');
const editingRow = ref<string | null>(null);
const editTarget = ref('');

async function load(): Promise<void> {
  if (!props.projectId) return;
  loading.value = true;
  try {
    const [pre, proj] = await Promise.all([
      fetchPreanalysis(props.projectId).catch((e: unknown) => {
        if (e instanceof Error && e.message.includes('404')) return null;
        throw e;
      }),
      fetchProject(props.projectId),
    ]);
    data.value = pre;
    project.value = proj;
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function rerun(): Promise<void> {
  loading.value = true;
  try {
    data.value = await runPreanalysis(props.projectId, { use_llm: useLlm.value });
    project.value = await fetchProject(props.projectId);
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function startEdit(row: ParamMappingRow): void {
  if (!row.unity_name) return;
  editingRow.value = row.unity_name;
  editTarget.value = (project.value?.manual_param_mapping?.[row.unity_name] ?? row.laya_name) ?? '';
}

function cancelEdit(): void {
  editingRow.value = null;
  editTarget.value = '';
}

async function saveEdit(unityName: string, target: string): Promise<void> {
  saving.value = true;
  try {
    const current = { ...(project.value?.manual_param_mapping ?? {}) };
    current[unityName] = target;
    data.value = await setManualMapping(props.projectId, current);
    project.value = await fetchProject(props.projectId);
    editingRow.value = null;
    editTarget.value = '';
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

async function clearOverride(unityName: string): Promise<void> {
  saving.value = true;
  try {
    const current = { ...(project.value?.manual_param_mapping ?? {}) };
    delete current[unityName];
    data.value = await setManualMapping(props.projectId, current);
    project.value = await fetchProject(props.projectId);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

async function markSkip(unityName: string): Promise<void> {
  await saveEdit(unityName, '');
}

const layaParamOptions = computed<{ name: string; type: string | null }[]>(() => {
  if (!data.value) return [];
  return data.value.laya_shader.params.map((p) => ({ name: p.name, type: p.param_type ?? null }));
});

function statusOrder(status: ParamMappingRow['status']): number {
  return ['manual', 'curated', 'exact', 'fuzzy', 'unity_only', 'laya_only', 'manual_skip'].indexOf(status);
}

watch(() => props.projectId, () => { void load(); });
onMounted(() => { void load(); });

const rows = computed<ParamMappingRow[]>(() => {
  if (!data.value) return [];
  const sorted = [...data.value.param_mapping].sort(
    (a, b) => statusOrder(a.status) - statusOrder(b.status),
  );
  if (filter.value === 'all') return sorted;
  return sorted.filter((row) => row.status === filter.value);
});

const counts = computed(() => {
  const out = { manual: 0, curated: 0, exact: 0, fuzzy: 0, unity_only: 0, laya_only: 0, manual_skip: 0 };
  if (!data.value) return out;
  for (const r of data.value.param_mapping) {
    if (r.status in out) (out as Record<string, number>)[r.status]++;
  }
  return out;
});

const llmResult = computed(() => data.value?.llm_semantics ?? null);
const llmValidated = computed(() => llmResult.value?.validated ?? null);
const llmParamGroups = computed(() => {
  const out: Record<string, number> = {};
  for (const sem of llmValidated.value?.param_semantics ?? []) {
    out[sem.group] = (out[sem.group] ?? 0) + 1;
  }
  return Object.entries(out).sort((a, b) => b[1] - a[1]);
});
const modulePlan = computed(() => data.value?.module_plan ?? []);
const unityFeatures = computed(() => data.value?.unity_feature_summary ?? llmValidated.value?.unity_feature_summary ?? []);
const modulePlanStats = computed(() => {
  const entries = modulePlan.value;
  return {
    total: entries.length,
    suggested: entries.filter((item) => item.suggested_by_unity).length,
    active: entries.filter((item) => item.current_active).length,
    probe: entries.filter((item) => item.probe_required).length,
    searchParams: new Set(entries.flatMap((item) => item.search_params)).size,
  };
});

function statusLabel(status: ParamMappingRow['status']): string {
  switch (status) {
    case 'manual': return 'manual';
    case 'manual_skip': return 'skipped';
    case 'curated': return 'curated';
    case 'exact': return 'exact';
    case 'fuzzy': return 'fuzzy';
    case 'unity_only': return 'unity-only';
    case 'laya_only': return 'laya-only';
    default: return status;
  }
}

function isOverridden(row: ParamMappingRow): boolean {
  if (!row.unity_name) return false;
  return Object.prototype.hasOwnProperty.call(
    project.value?.manual_param_mapping ?? {},
    row.unity_name,
  );
}

function percent(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(0)}%`;
}

function onSchemaSaved(next: PreanalysisPayload): void {
  data.value = next;
  void fetchProject(props.projectId).then((nextProject) => {
    project.value = nextProject;
  }).catch(() => {});
}
</script>

<template>
  <div class="preanalysis-view">
    <header class="pa-head">
      <h2 class="section-title" style="margin: 0;">预分析</h2>
      <span class="muted small">解析两侧 shader、生成语义先验、预测 stage 计划</span>
      <label class="small llm-toggle">
        <input type="checkbox" v-model="useLlm" />
        启用 LLM 语义
      </label>
      <button @click="rerun" :disabled="loading">{{ loading ? '运行中…' : data ? '重新分析' : '运行分析' }}</button>
    </header>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <p v-if="!data && !loading" class="muted small">还没有预分析结果，点上面的按钮触发。</p>

    <template v-if="data">
      <section class="section">
        <h3 class="section-title">总览</h3>
        <div class="stats">
          <span class="stat-pill">Unity 功能 <strong>{{ unityFeatures.length }}</strong></span>
          <span class="stat-pill">Laya 模块 <strong>{{ modulePlanStats.total }}</strong></span>
          <span class="stat-pill">建议探索 <strong>{{ modulePlanStats.suggested }}</strong></span>
          <span class="stat-pill">当前激活 <strong>{{ modulePlanStats.active }}</strong></span>
          <span class="stat-pill">需探针 <strong>{{ modulePlanStats.probe }}</strong></span>
          <span class="stat-pill">搜索参数 <strong>{{ modulePlanStats.searchParams }}</strong></span>
        </div>
        <p class="muted small" style="margin-top: 4px;">
          ran_at <span class="mono">{{ data.ran_at }}</span> ·
          unity shader <span class="mono">{{ data.unity_shader?.name ?? '(未提供)' }}</span> ·
          laya shader <span class="mono">{{ data.laya_shader.name }}</span>
        </p>
      </section>

      <LayaControlSchemaPanel
        v-if="data.laya_control_groups?.length"
        :project-id="props.projectId"
        :groups="data.laya_control_groups"
        :module-plan="modulePlan"
        :manual-schema="data.manual_laya_control_schema"
        @saved="onSchemaSaved"
      />

      <section v-if="unityFeatures.length" class="section">
        <h3 class="section-title">Unity 功能模块摘要</h3>
        <table class="mapping-table">
          <thead>
            <tr>
              <th>功能</th>
              <th>启用</th>
              <th>置信度</th>
              <th>候选 Laya 组</th>
              <th>控制类型 / 证据</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="feature in unityFeatures" :key="feature.feature + feature.laya_candidate_groups.join(',')">
              <td class="mono">{{ feature.feature }}</td>
              <td>{{ feature.enabled ? 'yes' : 'no' }}</td>
              <td>{{ percent(feature.confidence) }}</td>
              <td class="mono small">{{ feature.laya_candidate_groups.join(', ') || '—' }}</td>
              <td class="muted small">
                <div>{{ feature.controls.join(', ') || '—' }}</div>
                <div>{{ feature.evidence.slice(0, 2).join(' / ') }}</div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="data.warnings.length" class="section">
        <h3 class="section-title">警告</h3>
        <ul class="warning-list">
          <li v-for="(w, i) in data.warnings" :key="i">{{ w }}</li>
        </ul>
      </section>

      <section v-if="llmResult" class="section">
        <h3 class="section-title">LLM 语义先验</h3>
        <div class="stats">
          <span class="stat-pill">status <strong>{{ llmResult.status }}</strong></span>
          <span v-if="llmResult.runtime?.model" class="stat-pill">model <strong>{{ llmResult.runtime.model }}</strong></span>
          <span v-if="llmValidated" class="stat-pill">features <strong>{{ unityFeatures.length }}</strong></span>
          <span v-if="llmValidated" class="stat-pill">phenomena <strong>{{ llmValidated.unity_phenomena.length }}</strong></span>
          <span v-if="llmValidated" class="stat-pill">semantics <strong>{{ llmValidated.param_semantics.length }}</strong></span>
        </div>
        <p v-if="llmResult.status === 'skipped'" class="muted small">{{ llmResult.reason }}</p>
        <p v-else-if="llmResult.status === 'not_configured'" class="muted small">
          未找到 OpenAI-compatible 配置：{{ llmResult.error }}
        </p>
        <p v-else-if="llmResult.status === 'failed'" class="error-text small">
          LLM 调用失败：{{ llmResult.error }}
        </p>
        <p v-else-if="llmResult.status !== 'ok'" class="muted small">
          LLM 未产出可用 Unity 功能模块，将只展示 Laya 侧 fallback 模块结构。
        </p>
        <template v-if="llmValidated">
          <div v-if="llmValidated.unity_phenomena.length" class="phenomenon-grid">
            <article
              v-for="item in llmValidated.unity_phenomena"
              :key="item.name + item.laya_candidate_groups.join(',')"
              class="phenomenon-card"
            >
              <div class="phenomenon-title">
                <strong>{{ item.name }}</strong>
                <span class="status-pill status-fuzzy">{{ percent(item.confidence) }}</span>
              </div>
              <p class="muted small">{{ item.note || 'Unity 参数只作为视觉现象证据，不直接迁移数值。' }}</p>
              <p class="muted small">
                候选 Laya 组：
                <span v-if="item.laya_candidate_groups.length" class="mono">{{ item.laya_candidate_groups.join(', ') }}</span>
                <span v-else>—</span>
              </p>
              <ul v-if="item.unity_evidence.length" class="evidence-list">
                <li v-for="evidence in item.unity_evidence" :key="evidence">{{ evidence }}</li>
              </ul>
            </article>
          </div>
          <div v-if="llmParamGroups.length" class="semantic-groups">
            <span
              v-for="[group, count] in llmParamGroups"
              :key="group"
              class="stat-pill"
            >{{ group }} <strong>{{ count }}</strong></span>
          </div>
          <table v-if="llmValidated.initial_laya_param_suggestions.length" class="rec-table">
            <thead>
              <tr>
                <th>初始参考参数</th>
                <th>建议值</th>
                <th>置信度</th>
                <th>Unity 证据</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="suggestion in llmValidated.initial_laya_param_suggestions" :key="suggestion.laya_param">
                <td class="mono">{{ suggestion.laya_param }}</td>
                <td class="mono small">{{ JSON.stringify(suggestion.suggested_value) }}</td>
                <td>{{ percent(suggestion.confidence) }}</td>
                <td class="mono small">{{ suggestion.source_unity_params.join(', ') || '—' }}</td>
                <td class="muted small">{{ suggestion.reason }}</td>
              </tr>
            </tbody>
          </table>
          <ul v-if="(llmResult.warnings?.length ?? 0) > 0" class="warning-list">
            <li v-for="warning in llmResult.warnings" :key="warning">{{ warning }}</li>
          </ul>
        </template>
      </section>

      <details class="section debug-section">
        <summary>
          <span class="section-title">参数映射（人工参考）</span>
          <span class="muted small">旧表仅用于查漏补缺，不再作为预分析主结论。</span>
        </summary>
        <ul v-if="data.mapping_notes?.length" class="warning-list">
          <li v-for="note in data.mapping_notes" :key="note">{{ note }}</li>
        </ul>
        <p class="muted small" style="margin: 0 0 6px;">
          流水线优先级：<span class="kbd">manual</span> ▸ <span class="kbd">curated 字典</span> ▸ <span class="kbd">归一化精确</span> ▸ <span class="kbd">类型兼容的 fuzzy ≥0.85</span>。
          类型不兼容（Range vs Color 之类）一律拒绝。任何一行点"改一下"都可以手动覆盖，存到 <span class="mono">project.json.manual_param_mapping</span>。
        </p>
        <div class="filter-row">
          <button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部 ({{ data.param_mapping.length }})</button>
          <button :class="{ active: filter === 'manual' }" @click="filter = 'manual'">manual ({{ counts.manual }})</button>
          <button :class="{ active: filter === 'curated' }" @click="filter = 'curated'">curated ({{ counts.curated }})</button>
          <button :class="{ active: filter === 'exact' }" @click="filter = 'exact'">exact ({{ counts.exact }})</button>
          <button :class="{ active: filter === 'fuzzy' }" @click="filter = 'fuzzy'">fuzzy ({{ counts.fuzzy }})</button>
          <button :class="{ active: filter === 'unity_only' }" @click="filter = 'unity_only'">unity-only ({{ counts.unity_only }})</button>
          <button :class="{ active: filter === 'laya_only' }" @click="filter = 'laya_only'">laya-only ({{ counts.laya_only }})</button>
        </div>
        <table class="mapping-table">
          <thead>
            <tr>
              <th>Unity</th>
              <th>type</th>
              <th>Laya</th>
              <th>type</th>
              <th>status</th>
              <th>score</th>
              <th>原因 / 操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in rows" :key="i" :class="[`row-${row.status}`, { overridden: isOverridden(row) }]">
              <td><span class="mono">{{ row.unity_name ?? '—' }}</span></td>
              <td class="muted small">{{ row.unity_type ?? '—' }}</td>
              <td>
                <template v-if="editingRow === row.unity_name">
                  <select v-model="editTarget" class="laya-pick">
                    <option value="">— 标记为不映射 —</option>
                    <option
                      v-for="opt in layaParamOptions"
                      :key="opt.name"
                      :value="opt.name"
                    >{{ opt.name }} ({{ opt.type ?? '?' }})</option>
                  </select>
                </template>
                <span v-else class="mono">{{ row.laya_name ?? '—' }}</span>
              </td>
              <td class="muted small">{{ row.laya_type ?? '—' }}</td>
              <td>
                <span class="status-pill" :class="`status-${row.status}`">{{ statusLabel(row.status) }}</span>
              </td>
              <td class="numeric mono">{{ row.score.toFixed(2) }}</td>
              <td class="muted small action-cell">
                <template v-if="editingRow === row.unity_name">
                  <button class="primary mini" :disabled="saving || !row.unity_name" @click="row.unity_name && saveEdit(row.unity_name, editTarget)">保存</button>
                  <button class="mini" @click="cancelEdit">取消</button>
                </template>
                <template v-else>
                  <span class="reason-text">{{ row.reason }}</span>
                  <span v-if="row.unity_name" class="action-buttons">
                    <button class="mini" @click="startEdit(row)">改一下</button>
                    <button v-if="isOverridden(row)" class="mini ghost" @click="row.unity_name && clearOverride(row.unity_name)">清除手改</button>
                    <button v-else-if="row.status !== 'manual_skip'" class="mini ghost" @click="row.unity_name && markSkip(row.unity_name)">置空</button>
                  </span>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!rows.length" class="muted small">没有匹配的行。</p>
      </details>

      <section v-if="data.initial_recommendations.length" class="section">
        <h3 class="section-title">旧版初值参考（Unity → Laya）</h3>
        <p class="muted small">这些来自传统参数映射，只能作为人工参考；Unity 与 Laya 数值通常不等价，不应把它当作最终参数答案。</p>
        <table class="rec-table">
          <thead>
            <tr>
              <th>Laya 参数</th>
              <th>Unity 参数</th>
              <th>当前 Laya 值</th>
              <th>建议值</th>
              <th>类型</th>
              <th>范围</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(rec, i) in data.initial_recommendations" :key="i">
              <td><span class="mono">{{ rec.laya_param }}</span></td>
              <td class="muted"><span class="mono">{{ rec.unity_param }}</span></td>
              <td class="mono small">{{ JSON.stringify(rec.current_laya_value) }}</td>
              <td class="mono small">{{ JSON.stringify(rec.suggested_value) }}</td>
              <td class="muted small">{{ rec.type ?? '—' }}</td>
              <td class="muted small">
                {{ rec.range[0] ?? '—' }} … {{ rec.range[1] ?? '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="section">
        <h3 class="section-title">预测的 stage 计划</h3>
        <ol class="stage-list">
          <li v-for="stage in data.stage_plan" :key="stage.name">
            <strong class="mono">{{ stage.name }}</strong>
            <span class="muted small"> · target {{ stage.target_score.toFixed(3) }} · max {{ stage.max_iterations }} iters · {{ stage.params.length }} params</span>
            <p class="muted small" style="margin: 2px 0;">{{ stage.description }}</p>
            <code class="param-list">{{ stage.params.join(', ') || '—' }}</code>
          </li>
        </ol>
      </section>
    </template>
  </div>
</template>

<style scoped>
.preanalysis-view { display: flex; flex-direction: column; gap: 12px; padding-bottom: 24px; }
.pa-head { display: flex; align-items: baseline; gap: 12px; }
.llm-toggle { display: inline-flex; align-items: center; gap: 4px; color: var(--text-muted); }
.stats { display: flex; gap: 8px; flex-wrap: wrap; }
.stats.compact { gap: 4px; margin: 6px 0; }
.warning-list { margin: 4px 0 0; padding-left: 22px; color: var(--warn); }
.error-text { color: var(--bad); }
.phenomenon-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; margin-top: 8px; }
.phenomenon-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
}
.phenomenon-title { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
.evidence-list { margin: 6px 0 0; padding-left: 18px; color: var(--text-muted); font-size: 12px; }
.semantic-groups { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.debug-section summary { cursor: pointer; display: flex; align-items: baseline; gap: 8px; }
.filter-row { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
.filter-row button.active { background: var(--bg-hover); color: var(--accent); border-color: var(--accent); }
.mapping-table, .rec-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.mapping-table th, .mapping-table td, .rec-table th, .rec-table td {
  border-bottom: 1px solid var(--border);
  padding: 4px 8px;
  text-align: left;
  vertical-align: top;
}
.mapping-table th, .rec-table th { color: var(--text-muted); font-weight: 500; }
.numeric { text-align: right; }
.status-pill {
  display: inline-block;
  font-family: var(--mono);
  font-size: 11px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid;
}
.status-manual { color: #d2a8ff; border-color: #d2a8ff; }
.status-manual_skip { color: var(--text-dim); border-color: var(--border-strong); text-decoration: line-through; }
.status-curated { color: var(--good); border-color: var(--good); background: rgba(63, 185, 80, 0.08); }
.status-exact { color: var(--good); border-color: var(--good); }
.status-fuzzy { color: var(--accent); border-color: var(--accent); }
.status-unity_only { color: var(--warn); border-color: var(--warn); }
.status-laya_only { color: var(--text-dim); border-color: var(--border-strong); }
.row-unity_only { background: rgba(210, 153, 34, 0.04); }
.row-laya_only { background: rgba(110, 118, 129, 0.04); }
.row-manual { background: rgba(210, 168, 255, 0.05); }
.row-manual_skip { opacity: 0.5; }
tr.overridden td:first-child::before {
  content: "✎ ";
  color: #d2a8ff;
  font-weight: 700;
}
.action-cell { min-width: 220px; }
.reason-text { display: block; line-height: 1.4; }
.action-buttons { display: inline-flex; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.mini { padding: 1px 6px; font-size: 11px; }
.mini.primary { background: var(--accent-strong); border-color: var(--accent-strong); color: white; }
.mini.ghost { background: transparent; border: 1px dashed var(--border-strong); }
.laya-pick {
  background: var(--bg-elevated);
  border: 1px solid var(--accent);
  color: var(--text);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 11px;
  width: 100%;
  max-width: 220px;
}
.stage-list { margin: 0; padding-left: 22px; }
.stage-list li + li { margin-top: 6px; }
.param-list {
  display: block;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--text-muted);
}
</style>

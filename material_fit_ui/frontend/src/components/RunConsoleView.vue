<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import {
  applyLayaControlSchemaPreset,
  cancelJob,
  deleteLayaControlSchemaPreset,
  fetchPreanalysis,
  fetchLayaControlSchemaPresets,
  fetchJob,
  fetchJobLog,
  fetchProject,
  listJobs,
  patchProject,
  renameLayaControlSchemaPreset,
  runPreanalysis,
  saveLayaControlSchema,
  saveLayaControlSchemaPreset,
  startJob,
} from '../api';
import RunModePicker from './RunModePicker.vue';
import type {
  JobState,
  LayaControlGroup,
  LayaControlSchemaPreset,
  LayaControlSchemaPresetList,
  ManualLayaControlSchema,
  PreanalysisPayload,
  ProjectDetail,
  AutoAdjustMode,
} from '../types';

const props = defineProps<{ projectId: string }>();
const emit = defineEmits<{
  (e: 'job-progress'): void;
  (e: 'open-iter', iterId: string): void;
}>();

const project = ref<ProjectDetail | null>(null);
const jobs = ref<JobState[]>([]);
const activeJob = ref<JobState | null>(null);
const preanalysis = ref<PreanalysisPayload | null>(null);
const log = ref('');
const error = ref<string | null>(null);
const starting = ref(false);
const cancelling = ref(false);
const savingGroup = ref<string | null>(null);
const savingSchema = ref(false);
const applyingPreset = ref(false);
const editingSchema = ref(false);
const presetList = ref<LayaControlSchemaPresetList>({ active_preset_id: 'auto', presets: [] });

let pollHandle: ReturnType<typeof setInterval> | null = null;

async function loadProject(): Promise<void> {
  if (!props.projectId) return;
  try {
    project.value = await fetchProject(props.projectId);
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function loadJobs(): Promise<void> {
  if (!props.projectId) return;
  try {
    jobs.value = await listJobs(props.projectId);
    const latestId = project.value?.active_job_id ?? project.value?.last_job_id ?? jobs.value[0]?.job_id;
    if (latestId) {
      activeJob.value = await fetchJob(latestId);
    } else {
      activeJob.value = null;
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function loadPreanalysis(): Promise<void> {
  if (!props.projectId) return;
  try {
    preanalysis.value = await fetchPreanalysis(props.projectId);
  } catch (fetchErr) {
    try {
      preanalysis.value = await runPreanalysis(props.projectId, { use_llm: false });
      error.value = null;
    } catch {
      preanalysis.value = null;
      error.value = fetchErr instanceof Error ? fetchErr.message : String(fetchErr);
    }
  }
}

async function loadPresets(): Promise<void> {
  if (!props.projectId) return;
  try {
    presetList.value = await fetchLayaControlSchemaPresets(props.projectId);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function tick(): Promise<void> {
  if (!activeJob.value) return;
  try {
    const updated = await fetchJob(activeJob.value.job_id);
    const prev = activeJob.value;
    activeJob.value = updated;
    if (
      prev.iterations_observed !== updated.iterations_observed ||
      prev.last_iter_id !== updated.last_iter_id ||
      prev.status !== updated.status
    ) {
      emit('job-progress');
      void refreshLog();
    }
    if (['completed', 'failed', 'cancelled'].includes(updated.status)) {
      stopPolling();
      void loadProject();
    }
  } catch {
    /* network blip; keep polling */
  }
}

function startPolling(): void {
  stopPolling();
  pollHandle = setInterval(() => { void tick(); }, 1500);
}

function stopPolling(): void {
  if (pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}

async function start(): Promise<void> {
  if (!props.projectId) return;
  starting.value = true;
  error.value = null;
  try {
    const job = await startJob(props.projectId);
    activeJob.value = job;
    jobs.value = [job, ...jobs.value];
    await loadProject();
    startPolling();
    void refreshLog();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    starting.value = false;
  }
}

async function setAutoAdjustMode(mode: AutoAdjustMode): Promise<void> {
  if (!props.projectId || !project.value) return;
  try {
    project.value = await patchProject(props.projectId, {
      algorithm_config: {
        auto_adjust_mode: mode,
      },
    });
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function setGroupEnabled(groupName: string, enabled: boolean): Promise<void> {
  if (!props.projectId || !project.value) return;
  savingGroup.value = groupName;
  try {
    const currentAlgo = project.value.algorithm_config;
    const current = { ...(currentAlgo.laya_control_group_overrides ?? {}) };
    current[groupName] = { enabled };
    project.value = await patchProject(props.projectId, {
      algorithm_config: {
        laya_control_group_overrides: current,
      },
    });
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    savingGroup.value = null;
  }
}

function currentManualSchema(): ManualLayaControlSchema {
  const source = preanalysis.value?.manual_laya_control_schema ?? project.value?.manual_laya_control_schema;
  return {
    schema_version: 1,
    base_auto_hash: source?.base_auto_hash ?? '',
    groups: { ...(source?.groups ?? {}) },
    controls: { ...(source?.controls ?? {}) },
    deleted_groups: [...(source?.deleted_groups ?? [])],
    hidden_controls: [...(source?.hidden_controls ?? [])],
  };
}

function hasManualSchemaEdits(): boolean {
  const schema = currentManualSchema();
  return !!(
    Object.keys(schema.groups).length ||
    Object.keys(schema.controls).length ||
    schema.deleted_groups.length ||
    schema.hidden_controls.length
  );
}

async function saveManualSchema(schema: ManualLayaControlSchema): Promise<void> {
  if (!props.projectId) return;
  savingSchema.value = true;
  try {
    preanalysis.value = await saveLayaControlSchema(props.projectId, schema as unknown as Record<string, unknown>);
    project.value = await fetchProject(props.projectId);
    await loadPresets();
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    savingSchema.value = false;
  }
}

async function applyPreset(presetId: string): Promise<void> {
  if (!props.projectId || applyingPreset.value) return;
  if (hasManualSchemaEdits()) {
    const ok = window.confirm('套用预设会替换当前人工分类调整，是否继续？');
    if (!ok) return;
  }
  applyingPreset.value = true;
  try {
    const payload = await applyLayaControlSchemaPreset(props.projectId, presetId);
    preanalysis.value = payload.laya_control_groups ? payload : await runPreanalysis(props.projectId, { use_llm: false });
    await loadProject();
    await loadPresets();
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    applyingPreset.value = false;
  }
}

async function saveCurrentPreset(): Promise<void> {
  if (!props.projectId) return;
  const name = window.prompt('预设名称，例如 FishStandard - 低成本搜索');
  if (!name?.trim()) return;
  applyingPreset.value = true;
  try {
    presetList.value = await saveLayaControlSchemaPreset(props.projectId, { name: name.trim() });
    await loadProject();
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    applyingPreset.value = false;
  }
}

async function renameCurrentPreset(): Promise<void> {
  const preset = activePreset.value;
  if (!props.projectId || !preset || preset.builtin) return;
  const name = window.prompt('预设显示名称', preset.name);
  if (!name?.trim() || name.trim() === preset.name) return;
  applyingPreset.value = true;
  try {
    presetList.value = await renameLayaControlSchemaPreset(props.projectId, preset.id, {
      name: name.trim(),
      description: preset.description,
    });
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    applyingPreset.value = false;
  }
}

async function deleteCurrentPreset(): Promise<void> {
  const preset = activePreset.value;
  if (!props.projectId || !preset || preset.builtin) return;
  const ok = window.confirm(`删除预设“${preset.name}”？当前分类会回到自动分类。`);
  if (!ok) return;
  applyingPreset.value = true;
  try {
    presetList.value = await deleteLayaControlSchemaPreset(props.projectId, preset.id);
    preanalysis.value = await runPreanalysis(props.projectId, { use_llm: false });
    await loadProject();
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    applyingPreset.value = false;
  }
}

function slugifyGroup(label: string): string {
  const slug = label.trim().toLowerCase().replace(/[^a-z0-9_\u4e00-\u9fa5]+/g, '_').replace(/^_+|_+$/g, '');
  return slug || `group_${Date.now()}`;
}

async function addGroup(): Promise<void> {
  const label = window.prompt('新分类名称，例如 Normal / 法线');
  if (!label?.trim()) return;
  const schema = currentManualSchema();
  let id = slugifyGroup(label);
  let suffix = 2;
  const existing = new Set(layaControlGroups.value.map((group) => group.group));
  while (existing.has(id) || schema.groups[id]) {
    id = `${slugifyGroup(label)}_${suffix}`;
    suffix += 1;
  }
  schema.groups[id] = {
    label: label.trim(),
    enabled: true,
    locked: true,
    order: (layaControlGroups.value.length + 1) * 10,
    created_by_user: true,
  };
  await saveManualSchema(schema);
}

async function renameGroup(group: LayaControlGroup): Promise<void> {
  const label = window.prompt('分类显示名称', group.label);
  if (!label?.trim() || label.trim() === group.label) return;
  const schema = currentManualSchema();
  schema.groups[group.group] = {
    ...(schema.groups[group.group] ?? {}),
    label: label.trim(),
    locked: true,
  };
  await saveManualSchema(schema);
}

async function moveControl(paramName: string, groupId: string): Promise<void> {
  if (!groupId) return;
  const schema = currentManualSchema();
  schema.controls[paramName] = {
    ...(schema.controls[paramName] ?? {}),
    group: groupId,
    locked_fields: Array.from(new Set([...(schema.controls[paramName]?.locked_fields as string[] ?? []), 'group'])),
  };
  await saveManualSchema(schema);
}

async function setControlSearchable(paramName: string, searchable: boolean): Promise<void> {
  const schema = currentManualSchema();
  schema.controls[paramName] = {
    ...(schema.controls[paramName] ?? {}),
    searchable,
    locked_fields: Array.from(new Set([...(schema.controls[paramName]?.locked_fields as string[] ?? []), 'searchable'])),
  };
  await saveManualSchema(schema);
}

async function doCancel(): Promise<void> {
  if (!activeJob.value) return;
  cancelling.value = true;
  try {
    await cancelJob(activeJob.value.job_id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    cancelling.value = false;
  }
}

async function refreshLog(): Promise<void> {
  if (!activeJob.value) return;
  try {
    const result = await fetchJobLog(activeJob.value.job_id, 64);
    log.value = result.text;
  } catch {
    /* ignore */
  }
}

watch(() => props.projectId, async () => {
  stopPolling();
  await loadProject();
  await loadPreanalysis();
  await loadPresets();
  await loadJobs();
  await refreshLog();
  if (activeJob.value && activeJob.value.status === 'running') {
    startPolling();
  }
});

onMounted(async () => {
  await loadProject();
  await loadPreanalysis();
  await loadPresets();
  await loadJobs();
  await refreshLog();
  if (activeJob.value && activeJob.value.status === 'running') {
    startPolling();
  }
});

onBeforeUnmount(() => stopPolling());

const isRunning = computed(() => activeJob.value?.status === 'running' || activeJob.value?.status === 'cancelling');
const inputsReady = computed(() => {
  const inputs = project.value?.inputs;
  if (!inputs) return false;
  return !!inputs.laya_shader_path && !!inputs.laya_material_lmat_path;
});
const layaControlGroups = computed<LayaControlGroup[]>(() => preanalysis.value?.laya_control_groups ?? []);
const groupOptions = computed(() => layaControlGroups.value.map((group) => ({
  id: group.group,
  label: group.label,
})));
const enabledGroups = computed(() => {
  const overrides = project.value?.algorithm_config.laya_control_group_overrides ?? {};
  return layaControlGroups.value.filter((group) => groupEnabled(group.group, overrides));
});
const enabledSearchableCount = computed(() => (
  enabledGroups.value.reduce((total, group) => total + group.searchable_count, 0)
));
const activePresetId = computed(() => presetList.value.active_preset_id || project.value?.active_laya_control_schema_preset_id || 'auto');
const activePreset = computed<LayaControlSchemaPreset | null>(() => (
  presetList.value.presets.find((preset) => preset.id === activePresetId.value) ?? null
));
const selectedPresetDescription = computed(() => activePreset.value?.description || '当前人工分类尚未保存为预设。');

function groupEnabled(groupName: string, overrides = project.value?.algorithm_config.laya_control_group_overrides ?? {}): boolean {
  return overrides[groupName]?.enabled !== false;
}

function groupDefaultHint(group: LayaControlGroup): string {
  if (group.suggested_by_unity) return 'Unity 建议探索';
  if (group.current_active) return 'Laya 当前激活';
  return '低优先级，可按经验开启';
}

function percent(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(0)}%`;
}

function fmt(value: number | null): string {
  if (value == null || Number.isNaN(value)) return '—';
  return value.toFixed(4);
}

function statusColor(status: string): string {
  switch (status) {
    case 'running': return 'running';
    case 'completed': return 'ok';
    case 'failed': return 'bad';
    case 'cancelled': return 'warn';
    case 'cancelling': return 'warn';
    default: return 'muted';
  }
}

function pickIter(iterId: string | null): void {
  if (iterId) emit('open-iter', iterId);
}
</script>

<template>
  <div class="run-console">
    <header class="rc-head">
      <h2 class="section-title" style="margin: 0;">运行控制台</h2>
      <span class="muted small">驱动 fit_material.py 子进程，实时收集每轮迭代</span>
    </header>

    <div v-if="error" class="error-banner">{{ error }}</div>

    <section class="section actions">
      <RunModePicker
        :model-value="project?.algorithm_config.auto_adjust_mode ?? 'fresh_fit'"
        :disabled="isRunning || starting"
        @update:model-value="setAutoAdjustMode"
      />
      <button class="primary" :disabled="!inputsReady || isRunning || starting" @click="start">
        {{ starting ? '启动中…' : isRunning ? '运行中' : '开始自动调参' }}
      </button>
      <button :disabled="!isRunning || cancelling" @click="doCancel">
        {{ cancelling ? '取消中…' : '取消运行' }}
      </button>
      <button @click="loadJobs">刷新</button>
      <span v-if="!inputsReady" class="muted small">⚠ 项目配置里的必选输入还没填齐</span>
    </section>
    <p class="muted small run-mode-note">
      fresh_fit 会先压低发光/高光/边缘光等干扰项；refine_current 会继承当前 .lmat，适合在已有结果上继续微调。
    </p>

    <section class="section">
      <div class="run-section-head">
        <div>
          <h3 class="section-title" style="margin: 0;">本次优化范围：Laya Shader 控件组</h3>
          <p class="muted small" style="margin: 4px 0 0;">
            开跑前由人类工程师选择哪些 shader 板块参与搜索。关闭的组会在生成 fit_config 时从 semantic_group / CMA active search space 中移除。
          </p>
        </div>
        <div class="scope-actions">
          <label class="preset-picker small muted">
            预设模板
            <select
              :value="activePresetId"
              :disabled="applyingPreset || savingSchema"
              @change="applyPreset(($event.target as HTMLSelectElement).value)"
            >
              <option
                v-for="preset in presetList.presets"
                :key="preset.id"
                :value="preset.id"
              >
                {{ preset.name }}{{ preset.builtin ? '（内置）' : '' }}
              </option>
            </select>
          </label>
          <button class="ghost" @click="loadPreanalysis">刷新控件组</button>
          <button class="ghost" :disabled="applyingPreset" @click="saveCurrentPreset">另存为预设</button>
          <button
            class="ghost"
            :disabled="!activePreset || activePreset.builtin || applyingPreset"
            @click="renameCurrentPreset"
          >
            重命名预设
          </button>
          <button
            class="ghost"
            :disabled="!activePreset || activePreset.builtin || applyingPreset"
            @click="deleteCurrentPreset"
          >
            删除预设
          </button>
          <button class="ghost" :disabled="!layaControlGroups.length" @click="editingSchema = !editingSchema">
            {{ editingSchema ? '退出编辑分类' : '编辑分类' }}
          </button>
          <button v-if="editingSchema" class="ghost" :disabled="savingSchema" @click="addGroup">
            新增分类
          </button>
        </div>
      </div>
      <p class="muted small preset-desc">
        当前预设：<strong>{{ activePreset?.name ?? '未保存的自定义分类' }}</strong> · {{ selectedPresetDescription }}
      </p>
      <div v-if="!layaControlGroups.length" class="muted small empty-panel">
        还没有 Laya 控件分组。运行控制台会尝试自动做一次轻量 shader 解析；如果仍为空，请检查项目里的 Laya shader 与 lmat 路径。
      </div>
      <template v-else>
        <div class="job-stats scope-stats">
          <span class="stat-pill">启用组 <strong>{{ enabledGroups.length }}/{{ layaControlGroups.length }}</strong></span>
          <span class="stat-pill">启用可搜控件 <strong>{{ enabledSearchableCount }}</strong></span>
          <span class="stat-pill">optimizer <strong>{{ project?.algorithm_config.optimizer ?? '—' }}</strong></span>
        </div>
        <div class="control-scope-grid">
          <article
            v-for="group in layaControlGroups"
            :key="group.group"
            class="scope-card"
            :class="{ disabled: !groupEnabled(group.group), suggested: group.suggested_by_unity }"
          >
            <label class="scope-toggle">
              <input
                type="checkbox"
                :checked="groupEnabled(group.group)"
                :disabled="isRunning || savingGroup === group.group"
                @change="setGroupEnabled(group.group, ($event.target as HTMLInputElement).checked)"
              />
              <span>
                <strong>{{ group.label }}</strong>
                <span class="mono muted small"> · {{ group.group }}</span>
              </span>
            </label>
            <div v-if="editingSchema" class="schema-edit-row">
              <button class="mini ghost" :disabled="savingSchema" @click="renameGroup(group)">重命名分类</button>
              <span v-if="group.source === 'manual'" class="muted small">manual</span>
            </div>
            <p class="muted small scope-desc">{{ groupDefaultHint(group) }} · {{ group.description }}</p>
            <div class="job-stats compact">
              <span class="stat-pill">控件 <strong>{{ group.controls.length }}</strong></span>
              <span class="stat-pill">可搜 <strong>{{ group.searchable_count }}</strong></span>
              <span class="stat-pill">gate <strong>{{ group.gate_count }}</strong></span>
              <span class="stat-pill">priority <strong>{{ percent(group.search_priority) }}</strong></span>
              <span v-if="group.probe_required" class="stat-pill">需探针 <strong>yes</strong></span>
              <span v-if="savingGroup === group.group" class="stat-pill">saving <strong>...</strong></span>
            </div>
            <details class="control-details" :open="editingSchema">
              <summary class="muted small">查看该组暴露参数</summary>
              <div class="control-list">
                <div
                  v-for="control in group.controls"
                  :key="control.name"
                  class="control-chip"
                  :class="{ fixed: !control.searchable, gate: control.is_gate }"
                >
                  <div>
                    <span class="mono">{{ control.name }}</span>
                    <span class="muted"> {{ control.role }}</span>
                    <span v-if="control.source === 'manual'" class="manual-mark">manual</span>
                  </div>
                  <div v-if="editingSchema" class="control-edit-tools">
                    <label class="small muted">
                      <input
                        type="checkbox"
                        :checked="control.searchable"
                        :disabled="savingSchema"
                        @change="setControlSearchable(control.name, ($event.target as HTMLInputElement).checked)"
                      />
                      搜索
                    </label>
                    <select
                      :value="group.group"
                      :disabled="savingSchema"
                      @change="moveControl(control.name, ($event.target as HTMLSelectElement).value)"
                    >
                      <option
                        v-for="option in groupOptions"
                        :key="option.id"
                        :value="option.id"
                      >移到 {{ option.label }}</option>
                    </select>
                  </div>
                </div>
              </div>
            </details>
          </article>
        </div>
      </template>
    </section>

    <section v-if="activeJob" class="section">
      <div class="job-card">
        <div class="job-head">
          <span class="mono">{{ activeJob.job_id }}</span>
          <span class="status-pill" :class="statusColor(activeJob.status)">{{ activeJob.status }}</span>
        </div>
        <div class="job-stats">
          <span class="stat-pill">iters <strong>{{ activeJob.iterations_observed }}</strong></span>
          <span v-if="activeJob.last_iter_id" class="stat-pill clickable" @click="pickIter(activeJob.last_iter_id)">
            last <strong>{{ activeJob.last_iter_id }}</strong>
          </span>
          <span v-if="activeJob.last_decision_summary?.fit_score_before != null" class="stat-pill">
            fit <strong>{{ fmt(activeJob.last_decision_summary?.fit_score_before ?? null) }}</strong>
          </span>
          <span v-if="activeJob.last_decision_summary?.human_accept_score != null" class="stat-pill">
            human <strong>{{ fmt(activeJob.last_decision_summary?.human_accept_score ?? null) }}</strong>
          </span>
          <span v-if="activeJob.last_decision_summary?.perceptual_fit_score != null" class="stat-pill">
            strict <strong>{{ fmt(activeJob.last_decision_summary?.perceptual_fit_score ?? null) }}</strong>
          </span>
          <span v-if="activeJob.last_decision_summary?.diff_score_before != null" class="stat-pill">
            mae <strong>{{ fmt(activeJob.last_decision_summary?.diff_score_before ?? null) }}</strong>
          </span>
          <span v-if="activeJob.last_decision_summary?.selected_stage" class="stat-pill">
            stage <strong>{{ activeJob.last_decision_summary?.selected_stage }}</strong>
          </span>
          <span v-if="activeJob.last_decision_summary?.stop_reason" class="stat-pill">
            stop <strong>{{ activeJob.last_decision_summary?.stop_reason }}</strong>
          </span>
        </div>
        <p class="muted small" style="margin: 4px 0;">
          started {{ activeJob.started_at ?? '—' }}
          <span v-if="activeJob.ended_at"> · ended {{ activeJob.ended_at }}</span>
          <span v-if="activeJob.return_code != null"> · exit {{ activeJob.return_code }}</span>
          <span v-if="activeJob.error" class="bad"> · {{ activeJob.error }}</span>
        </p>
        <details class="cli-details">
          <summary class="muted small">展开命令行参数</summary>
          <pre class="params-pane">{{ activeJob.args.join(' ') }}</pre>
        </details>
      </div>
    </section>

    <section class="section">
      <div style="display: flex; align-items: baseline; gap: 8px;">
        <h3 class="section-title" style="margin: 0;">日志（tail 64 KB）</h3>
        <button @click="refreshLog" class="ghost">刷新日志</button>
      </div>
      <pre class="log-pane">{{ log || '(no log yet)' }}</pre>
    </section>

    <section v-if="jobs.length > 1" class="section">
      <h3 class="section-title">历史作业</h3>
      <table class="job-table">
        <thead>
          <tr>
            <th>job</th>
            <th>status</th>
            <th>started</th>
            <th>ended</th>
            <th>iters</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.job_id" :class="{ active: j.job_id === activeJob?.job_id }">
            <td><span class="mono">{{ j.job_id }}</span></td>
            <td><span class="status-pill" :class="statusColor(j.status)">{{ j.status }}</span></td>
            <td class="muted small">{{ j.started_at ?? '—' }}</td>
            <td class="muted small">{{ j.ended_at ?? '—' }}</td>
            <td class="numeric mono">{{ j.iterations_observed }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.run-console { display: flex; flex-direction: column; gap: 12px; padding-bottom: 24px; }
.rc-head { display: flex; align-items: baseline; gap: 12px; }
.actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.primary { background: var(--accent-strong); border-color: var(--accent-strong); color: white; }
.primary:disabled { opacity: 0.5; }
.ghost { background: transparent; border: 1px dashed var(--border-strong); }
.run-mode-picker { display: flex; align-items: center; gap: 6px; }
.run-mode-picker select {
  background: var(--bg-panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 6px;
}
.run-mode-note { margin: -4px 0 0; }
.run-section-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.scope-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.preset-picker { display: flex; align-items: center; gap: 6px; }
.preset-picker select {
  background: var(--bg-panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 6px;
  max-width: 220px;
}
.preset-desc { margin: 8px 0 0; }
.empty-panel {
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  padding: 10px 12px;
  margin-top: 8px;
}
.scope-stats { margin-top: 8px; }
.job-stats.compact { gap: 4px; margin-top: 6px; }
.control-scope-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px;
  margin-top: 10px;
  max-height: min(68vh, 720px);
  overflow: auto;
  padding-right: 4px;
  align-items: start;
}
.scope-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  max-height: 520px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.scope-card.suggested { border-color: rgba(63, 185, 80, 0.55); }
.scope-card.disabled { opacity: 0.58; background: rgba(110, 118, 129, 0.04); }
.scope-toggle { display: flex; gap: 8px; align-items: flex-start; cursor: pointer; }
.scope-toggle input { margin-top: 3px; }
.schema-edit-row { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.scope-desc { margin: 6px 0 0; min-height: 34px; }
.control-details { margin-top: 8px; min-height: 0; }
.control-details summary { cursor: pointer; }
.control-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  max-height: 250px;
  overflow: auto;
  padding-right: 4px;
  align-content: flex-start;
}
.control-chip {
  display: inline-block;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 4px 7px;
  font-size: 11px;
  background: var(--bg-elevated);
}
.control-chip.gate { border-color: var(--warn); }
.control-chip.fixed { color: var(--text-dim); border-style: dashed; }
.manual-mark { margin-left: 4px; color: #d2a8ff; font-family: var(--mono); }
.control-edit-tools { display: flex; gap: 6px; align-items: center; margin-top: 4px; }
.control-edit-tools select {
  background: var(--bg-panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 11px;
  max-width: 180px;
}
.mini { padding: 1px 6px; font-size: 11px; }

.job-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
}
.job-head { display: flex; align-items: center; gap: 10px; }
.job-head .mono { font-size: 13px; font-weight: 600; }
.job-stats { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.stat-pill.clickable { cursor: pointer; }
.stat-pill.clickable:hover { background: var(--bg-hover); border-color: var(--accent); }

.status-pill {
  display: inline-block;
  font-family: var(--mono);
  font-size: 11px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid;
}
.status-pill.running { color: var(--accent); border-color: var(--accent); animation: pulse 1.6s infinite; }
.status-pill.ok { color: var(--good); border-color: var(--good); }
.status-pill.bad { color: var(--bad); border-color: var(--bad); }
.status-pill.warn { color: var(--warn); border-color: var(--warn); }
.status-pill.muted { color: var(--text-muted); border-color: var(--border-strong); }
@keyframes pulse {
  0% { opacity: 0.6; } 50% { opacity: 1; } 100% { opacity: 0.6; }
}

.bad { color: var(--bad); }
.cli-details summary { cursor: pointer; }
.log-pane {
  background: #0d1117;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 8px 12px;
  border-radius: var(--radius);
  font-family: var(--mono);
  font-size: 11px;
  white-space: pre-wrap;
  max-height: 280px;
  overflow: auto;
  line-height: 1.5;
}

.job-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.job-table th, .job-table td {
  border-bottom: 1px solid var(--border);
  padding: 4px 8px;
  text-align: left;
}
.job-table th { color: var(--text-muted); font-weight: 500; }
.job-table tr.active { background: var(--bg-hover); }
.job-table .numeric { text-align: right; font-family: var(--mono); }
</style>

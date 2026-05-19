<script setup lang="ts">
import { computed, ref } from 'vue';
import { saveLayaControlSchema } from '../api';
import type { LayaControlGroup, ManualLayaControlSchema, ModulePlanEntry, PreanalysisPayload } from '../types';

const props = defineProps<{
  projectId: string;
  groups: LayaControlGroup[];
  modulePlan?: ModulePlanEntry[];
  manualSchema?: ManualLayaControlSchema;
}>();
const emit = defineEmits<{ (e: 'saved', payload: PreanalysisPayload): void }>();

const editing = ref(false);
const saving = ref(false);
const error = ref<string | null>(null);

const groupOptions = computed(() => props.groups.map((group) => ({ id: group.group, label: group.label })));
const searchableCount = computed(() => props.groups.reduce((total, group) => total + group.searchable_count, 0));
const modulePlanByGroup = computed(() => {
  const out = new Map<string, ModulePlanEntry>();
  for (const entry of props.modulePlan ?? []) {
    out.set(entry.group, entry);
  }
  return out;
});

function currentManualSchema(): ManualLayaControlSchema {
  const source = props.manualSchema;
  return {
    schema_version: 1,
    base_auto_hash: source?.base_auto_hash ?? '',
    groups: { ...(source?.groups ?? {}) },
    controls: { ...(source?.controls ?? {}) },
    deleted_groups: [...(source?.deleted_groups ?? [])],
    hidden_controls: [...(source?.hidden_controls ?? [])],
  };
}

async function persist(schema: ManualLayaControlSchema): Promise<void> {
  saving.value = true;
  try {
    const payload = await saveLayaControlSchema(props.projectId, schema as unknown as Record<string, unknown>);
    emit('saved', payload);
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

function slugifyGroup(label: string): string {
  const slug = label.trim().toLowerCase().replace(/[^a-z0-9_\u4e00-\u9fa5]+/g, '_').replace(/^_+|_+$/g, '');
  return slug || `group_${Date.now()}`;
}

async function addGroup(): Promise<void> {
  const label = window.prompt('新分组名称，例如 Normal / 法线');
  if (!label?.trim()) return;
  const schema = currentManualSchema();
  let id = slugifyGroup(label);
  let suffix = 2;
  const existing = new Set(props.groups.map((group) => group.group));
  while (existing.has(id) || schema.groups[id]) {
    id = `${slugifyGroup(label)}_${suffix}`;
    suffix += 1;
  }
  schema.groups[id] = {
    label: label.trim(),
    enabled: true,
    locked: true,
    order: (props.groups.length + 1) * 10,
    created_by_user: true,
  };
  await persist(schema);
}

async function renameGroup(group: LayaControlGroup): Promise<void> {
  const label = window.prompt('分组显示名称', group.label);
  if (!label?.trim() || label.trim() === group.label) return;
  const schema = currentManualSchema();
  schema.groups[group.group] = {
    ...(schema.groups[group.group] ?? {}),
    label: label.trim(),
    locked: true,
  };
  await persist(schema);
}

async function deleteGroup(group: LayaControlGroup): Promise<void> {
  const ok = window.confirm(`删除分组“${group.label}”？组内参数不会删除，但会回到自动分类或等待你移动到其它分组。`);
  if (!ok) return;
  const schema = currentManualSchema();
  if (!schema.deleted_groups.includes(group.group)) {
    schema.deleted_groups.push(group.group);
  }
  delete schema.groups[group.group];
  await persist(schema);
}

async function moveControl(paramName: string, groupId: string): Promise<void> {
  if (!groupId) return;
  const schema = currentManualSchema();
  schema.controls[paramName] = {
    ...(schema.controls[paramName] ?? {}),
    group: groupId,
    locked_fields: Array.from(new Set([...(schema.controls[paramName]?.locked_fields as string[] ?? []), 'group'])),
  };
  await persist(schema);
}

async function setControlSearchable(paramName: string, searchable: boolean): Promise<void> {
  const schema = currentManualSchema();
  schema.controls[paramName] = {
    ...(schema.controls[paramName] ?? {}),
    searchable,
    is_search_param: searchable,
    locked_fields: Array.from(new Set([...(schema.controls[paramName]?.locked_fields as string[] ?? []), 'searchable'])),
  };
  await persist(schema);
}

function percent(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(0)}%`;
}

function actionLabel(action: string | null | undefined): string {
  switch (action) {
    case 'optimize_group': return '直接优化';
    case 'activate_gate_then_probe': return '激活后探针';
    case 'probe_optional': return '可选探针';
    case 'skip_low_confidence': return '低优先级';
    case 'disabled_by_human': return '已停用';
    default: return action || '自动分组';
  }
}

function planFor(group: LayaControlGroup): ModulePlanEntry | undefined {
  return modulePlanByGroup.value.get(group.group);
}

function mergedSearchPriority(group: LayaControlGroup): number {
  return planFor(group)?.search_priority ?? group.search_priority;
}

function mergedUnityFeatures(group: LayaControlGroup): string[] {
  return planFor(group)?.unity_features ?? [];
}

function mergedGateNames(group: LayaControlGroup): string[] {
  const plan = planFor(group);
  return [
    ...(plan?.define_gates ?? group.define_gates ?? []),
    ...(plan?.gate_params ?? group.gate_params ?? []),
  ];
}

function mergedSearchParams(group: LayaControlGroup): string[] {
  return planFor(group)?.search_params ?? group.controls.filter((control) => control.searchable).map((control) => control.name);
}

function mergedEvidence(group: LayaControlGroup): string[] {
  return planFor(group)?.evidence ?? (group.reason ? [group.reason] : []);
}
</script>

<template>
  <section class="section">
    <div class="schema-head">
      <div>
        <h3 class="section-title" style="margin: 0;">Laya 参数分组与优化范围</h3>
        <p class="muted small" style="margin: 4px 0 0;">
          这里是后续运行时优化列表的主要来源。可新建/删除分组、移动参数、决定某个参数是否参与搜索。
        </p>
      </div>
      <div class="schema-actions">
        <button class="ghost" @click="editing = !editing">{{ editing ? '退出编辑' : '编辑分组' }}</button>
        <button v-if="editing" class="ghost" :disabled="saving" @click="addGroup">新增分组</button>
      </div>
    </div>
    <div v-if="error" class="error-banner">{{ error }}</div>
    <div class="stats">
      <span class="stat-pill">分组 <strong>{{ groups.length }}</strong></span>
      <span class="stat-pill">可搜参数 <strong>{{ searchableCount }}</strong></span>
      <span v-if="saving" class="stat-pill">saving <strong>...</strong></span>
    </div>
    <div class="control-grid">
      <article v-for="group in groups" :key="group.group" class="control-card">
        <div class="group-title">
          <span>
            <strong>{{ group.label }}</strong>
            <span class="mono muted small"> · {{ group.group }}</span>
          </span>
          <span
            class="status-pill"
            :class="group.probe_required ? 'status-unity_only' : group.suggested_by_unity ? 'status-curated' : 'status-laya_only'"
          >
            {{ actionLabel(planFor(group)?.action) }}
          </span>
        </div>
        <div v-if="editing" class="edit-row">
          <button class="mini ghost" :disabled="saving" @click="renameGroup(group)">重命名</button>
          <button class="mini ghost" :disabled="saving" @click="deleteGroup(group)">删除分组</button>
        </div>
        <p class="muted small desc">{{ group.description || group.reason || '自动从 Laya shader 参数推断的分组。' }}</p>
        <div class="stats compact">
          <span class="stat-pill">控件 <strong>{{ group.controls.length }}</strong></span>
          <span class="stat-pill">可搜 <strong>{{ group.searchable_count }}</strong></span>
          <span class="stat-pill">priority <strong>{{ percent(mergedSearchPriority(group)) }}</strong></span>
          <span class="stat-pill">active <strong>{{ group.current_active ? 'yes' : 'no' }}</strong></span>
        </div>
        <div class="plan-meta">
          <p class="muted small">
            Unity 功能：
            <span class="mono">{{ mergedUnityFeatures(group).join(', ') || '—' }}</span>
          </p>
          <p class="muted small">
            gate：
            <span class="mono">{{ mergedGateNames(group).join(', ') || '—' }}</span>
          </p>
          <p class="muted small">
            建议优化参数：
            <span class="mono">{{ mergedSearchParams(group).join(', ') || '—' }}</span>
          </p>
        </div>
        <ul v-if="mergedEvidence(group).length" class="evidence-list">
          <li v-for="evidence in mergedEvidence(group).slice(0, 3)" :key="evidence">{{ evidence }}</li>
        </ul>
        <div class="control-list">
          <div
            v-for="control in group.controls"
            :key="control.name"
            class="control-chip"
            :class="{ fixed: !control.searchable, gate: control.is_gate }"
          >
            <div>
              <span class="mono">{{ control.name }}</span>
              <span class="muted"> {{ control.param_type }} / {{ control.role }}</span>
            </div>
            <div v-if="editing" class="control-tools">
              <label class="small muted">
                <input
                  type="checkbox"
                  :checked="control.searchable"
                  :disabled="saving"
                  @change="setControlSearchable(control.name, ($event.target as HTMLInputElement).checked)"
                />
                参与优化
              </label>
              <select
                :value="group.group"
                :disabled="saving"
                @change="moveControl(control.name, ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="option in groupOptions" :key="option.id" :value="option.id">
                  移到 {{ option.label }}
                </option>
              </select>
            </div>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.schema-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.schema-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.ghost { background: transparent; border: 1px dashed var(--border-strong); }
.stats { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.stats.compact { gap: 4px; margin: 6px 0; }
.stat-pill {
  display: inline-block;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 11px;
  color: var(--text-muted);
}
.control-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 10px;
  margin-top: 10px;
}
.control-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
}
.group-title { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.edit-row { display: flex; gap: 6px; margin-top: 6px; }
.mini { padding: 1px 6px; font-size: 11px; }
.desc { margin: 6px 0 0; min-height: 32px; }
.plan-meta { margin-top: 6px; }
.plan-meta p { margin: 2px 0; }
.evidence-list { margin: 4px 0 0; padding-left: 18px; color: var(--text-muted); font-size: 11px; }
.control-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; max-height: 260px; overflow: auto; }
.control-chip {
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  border-radius: 6px;
  padding: 4px 6px;
  font-size: 11px;
  min-width: 170px;
}
.control-chip.fixed { opacity: 0.62; }
.control-chip.gate { border-color: rgba(210, 153, 34, 0.65); }
.control-tools { display: flex; align-items: center; gap: 6px; margin-top: 4px; flex-wrap: wrap; }
.control-tools select {
  background: var(--bg-panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 4px;
  max-width: 170px;
}
</style>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import {
  deleteProject,
  externalPreviewUrl,
  fetchLayaSceneNodes,
  fetchProject,
  importProjectInputFile,
  importUnityReferenceFiles,
  patchProject,
  pickFile,
  unityReferenceFiles as fetchUnityReferenceFiles,
} from '../api';
import type { FileInfo, LayaSceneNode, LayaSceneNodesPayload, ProjectDetail, ProjectInputs } from '../types';
import RefreshPreflightCard from './RefreshPreflightCard.vue';

const props = defineProps<{ projectId: string }>();
const emit = defineEmits<{
  (e: 'changed'): void;
  (e: 'deleted'): void;
}>();

const project = ref<ProjectDetail | null>(null);
const error = ref<string | null>(null);
const saving = ref(false);
const unityReferenceFiles = ref<FileInfo[]>([]);
const sceneNodes = ref<LayaSceneNodesPayload | null>(null);

interface Slot {
  key: keyof ProjectInputs;
  label: string;
  hint: string;
  required: boolean;
  filetypes: [string, string][];
  image?: boolean;
  isDir?: boolean;
  mode: 'import_file' | 'real_path' | 'unity_refs';
}

type ImportableInputKey = 'laya_shader_path' | 'unity_shader_path' | 'unity_material_params_path';

const slots: Slot[] = [
  {
    key: 'laya_shader_path',
    label: 'Laya 着色器',
    hint: '.shader / .vs / .fs',
    required: true,
    filetypes: [['Laya shader', '*.shader *.vs *.fs'], ['All files', '*.*']],
    mode: 'import_file',
  },
  {
    key: 'laya_material_lmat_path',
    label: 'Laya .lmat 写入目标',
    hint: '调参会写入此文件（自动备份 .bak）',
    required: true,
    filetypes: [['Laya material', '*.lmat'], ['All files', '*.*']],
    mode: 'real_path',
  },
  {
    key: 'unity_shader_path',
    label: 'Unity 着色器',
    hint: 'Unity ShaderLab .shader',
    required: false,
    filetypes: [['Unity shader', '*.shader'], ['All files', '*.*']],
    mode: 'import_file',
  },
  {
    key: 'unity_material_params_path',
    label: 'Unity 材质参数 JSON',
    hint: '可选：Editor 工具导出的实际参数；未提供时不阻塞 Laya 预分析/探针',
    required: false,
    filetypes: [['JSON', '*.json'], ['All files', '*.*']],
    mode: 'import_file',
  },
  {
    key: 'unity_reference_dir_path',
    label: 'Unity 多视角参考截图',
    hint: '多选参考截图并复制到项目 inputs/unity_references/；正式评分只使用这组项目内副本',
    required: false,
    filetypes: [['Images', '*.png *.jpg *.jpeg *.bmp *.webp'], ['All files', '*.*']],
    mode: 'unity_refs',
  },
  {
    key: 'laya_project_path',
    label: 'Laya 项目目录',
    hint: '包含 assets/ 的 Laya 项目根目录；用于推导资源相对路径和 command 位置',
    required: false,
    filetypes: [],
    isDir: true,
    mode: 'real_path',
  },
];

async function load(): Promise<void> {
  if (!props.projectId) return;
  try {
    const data = await fetchProject(props.projectId);
    project.value = data;
    await Promise.all([loadUnityReferenceFiles(data), loadLayaSceneNodes()]);
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

watch(() => props.projectId, () => { void load(); });
onMounted(() => { void load(); });

async function pickSlot(slot: Slot): Promise<void> {
  if (!project.value) return;
  try {
    const result = await pickFile({
      mode: slot.mode === 'unity_refs' ? 'open_many' : slot.isDir ? 'directory' : 'open',
      title: slot.label,
      initial_dir: slotInitialDir(slot.key),
      filetypes: slot.isDir ? undefined : slot.filetypes,
    });
    if (result.error) { error.value = result.error; return; }
    if (slot.mode === 'unity_refs') {
      const paths = result.paths ?? [];
      if (!paths.length) return;
      await importUnityReferences(paths);
      return;
    }
    if (!result.path) return;
    if (slot.mode === 'import_file') {
      await importInputFile(slot.key as ImportableInputKey, result.path);
      return;
    }
    await save({ inputs: { [slot.key]: result.path } });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function importInputFile(inputKey: ImportableInputKey, sourcePath: string): Promise<void> {
  if (!project.value) return;
  saving.value = true;
  try {
    project.value = await importProjectInputFile(project.value.id, inputKey, sourcePath);
    await Promise.all([loadUnityReferenceFiles(project.value), loadLayaSceneNodes()]);
    emit('changed');
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

async function importUnityReferences(sourcePaths: string[]): Promise<void> {
  if (!project.value) return;
  saving.value = true;
  try {
    project.value = await importUnityReferenceFiles(project.value.id, sourcePaths);
    await Promise.all([loadUnityReferenceFiles(project.value), loadLayaSceneNodes()]);
    emit('changed');
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

async function clearSlot(key: keyof ProjectInputs): Promise<void> {
  await save({ inputs: { [key]: null } });
}

async function saveReferenceGlob(value: string): Promise<void> {
  await save({ inputs: { unity_reference_glob: value || 'unity_ref_v*_yaw*_pitch*.png' } });
}

async function saveCaptureName(key: 'laya_capture_camera_name' | 'laya_capture_target_name', value: string): Promise<void> {
  await save({ inputs: { [key]: value.trim() || null } });
  await loadLayaSceneNodes();
}

async function save(patch: Record<string, unknown>): Promise<void> {
  if (!project.value) return;
  saving.value = true;
  try {
    project.value = await patchProject(project.value.id, patch);
    await Promise.all([loadUnityReferenceFiles(project.value), loadLayaSceneNodes()]);
    emit('changed');
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

async function onDelete(): Promise<void> {
  if (!project.value) return;
  if (!confirm(`确认删除项目 "${project.value.id}"？该目录会被移动到 output/.trash/`)) return;
  try {
    await deleteProject(project.value.id);
    emit('deleted');
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

function slotInitialDir(key: keyof ProjectInputs): string | undefined {
  const value = project.value?.inputs[key];
  if (typeof value === 'string' && value) {
    const idx = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'));
    return idx > 0 ? value.slice(0, idx) : undefined;
  }
  return undefined;
}

const requiredFilled = computed(
  () => !!project.value?.inputs.laya_shader_path && !!project.value?.inputs.laya_material_lmat_path,
);

function shorten(value: string | null | undefined): string {
  if (!value) return '未选择';
  if (value.length <= 100) return value;
  return value.slice(0, 30) + ' … ' + value.slice(-65);
}

const editorCaptureEnabled = computed(() => true);

const referencePreviewItems = computed(() => {
  if (unityReferenceFiles.value.length > 0) {
    return unityReferenceFiles.value.slice(0, 3).map((file) => ({
      name: file.name || file.path,
      url: externalPreviewUrl(file.path),
    }));
  }
  return [];
});

async function loadUnityReferenceFiles(data: ProjectDetail | null): Promise<void> {
  if (!data) return;
  try {
    const result = await fetchUnityReferenceFiles(
      data.inputs.unity_reference_dir_path || '',
      data.inputs.unity_reference_glob || 'unity_ref_v*_yaw*_pitch*.png',
      32,
    );
    unityReferenceFiles.value = result.files.filter((file) => /\.(png|jpe?g|bmp)$/i.test(file.name || file.path));
  } catch {
    unityReferenceFiles.value = [];
  }
}

async function loadLayaSceneNodes(): Promise<void> {
  if (!props.projectId) return;
  try {
    sceneNodes.value = await fetchLayaSceneNodes(props.projectId);
  } catch {
    sceneNodes.value = null;
  }
}

const targetOptions = computed<LayaSceneNode[]>(() => (
  (sceneNodes.value?.nodes ?? [])
    .filter((node) => node.name && node.type !== 'Camera')
    .sort((a, b) => Number(b.active) - Number(a.active) || a.name.localeCompare(b.name))
));

const cameraOptions = computed<LayaSceneNode[]>(() => (
  (sceneNodes.value?.nodes ?? [])
    .filter((node) => node.name && node.type === 'Camera')
    .sort((a, b) => Number(b.active) - Number(a.active) || a.name.localeCompare(b.name))
));

const captureTargetName = computed(() => (
  project.value?.inputs.laya_capture_target_name || sceneNodes.value?.recommended_target_name || ''
));

const captureCameraName = computed(() => (
  project.value?.inputs.laya_capture_camera_name || sceneNodes.value?.recommended_camera_name || 'Capture Camera'
));
</script>

<template>
  <div class="project-config">
    <div v-if="error" class="error-banner">{{ error }}</div>
    <p v-if="!project" class="muted small">加载中…</p>
    <template v-else>
      <header class="pc-head">
        <div>
          <h2>{{ project.name }}</h2>
          <p class="muted small">
            id <span class="mono">{{ project.id }}</span> ·
            创建 {{ project.created_at }} ·
            最后更新 {{ project.updated_at }}
          </p>
        </div>
        <span class="status-pill" :class="{ ok: requiredFilled, pending: !requiredFilled }">
          {{ requiredFilled ? '✓ 必选输入就绪' : '⚠ 缺必选输入' }}
        </span>
      </header>

      <section class="section">
        <h3 class="section-title">输入文件</h3>
        <div class="slot-grid">
          <div v-for="slot in slots" :key="slot.key" class="slot">
            <div class="slot-head">
              <span class="slot-label">
                {{ slot.label }}
                <span v-if="slot.required" class="required">*</span>
              </span>
              <div class="slot-actions">
                <button @click="pickSlot(slot)">{{ slot.mode === 'real_path' ? '选择…' : '导入…' }}</button>
                <button v-if="project.inputs[slot.key]" class="ghost" @click="clearSlot(slot.key)">清除</button>
              </div>
            </div>
            <div class="slot-hint muted small">{{ slot.hint }}</div>
            <div class="slot-value mono small" :class="{ filled: !!project.inputs[slot.key] }">
              {{ shorten(typeof project.inputs[slot.key] === 'string' ? (project.inputs[slot.key] as string) : null) }}
            </div>
          </div>
        </div>
      </section>

      <section class="section">
        <h3 class="section-title">Laya 截图相机与旋转目标</h3>
        <p class="muted small" style="margin: 0 0 8px;">
          探针和正式多视角截图都会使用这里的设置。规范场景中 target_name 固定为 model；多视角模式会旋转该节点。
        </p>
        <div class="capture-grid">
          <label class="capture-field">
            <span>截图相机 camera_name</span>
            <div class="capture-row">
              <select
                :value="captureCameraName"
                :disabled="saving"
                @change="saveCaptureName('laya_capture_camera_name', ($event.target as HTMLSelectElement).value)"
              >
                <option value="">自动推荐</option>
                <option
                  v-for="node in cameraOptions"
                  :key="node.path"
                  :value="node.name"
                >
                  {{ node.name }}{{ node.active ? '' : '（inactive）' }}
                </option>
              </select>
              <input
                :value="captureCameraName"
                placeholder="Capture Camera"
                @change="saveCaptureName('laya_capture_camera_name', ($event.target as HTMLInputElement).value)"
              />
            </div>
          </label>
          <label class="capture-field">
            <span>旋转目标 target_name <strong class="required">*</strong></span>
            <div class="capture-row">
              <select
                :value="captureTargetName"
                :disabled="saving"
                @change="saveCaptureName('laya_capture_target_name', ($event.target as HTMLSelectElement).value)"
              >
                <option value="">自动推荐</option>
                <option
                  v-for="node in targetOptions"
                  :key="node.path"
                  :value="node.name"
                >
                  {{ node.name }}{{ node.active ? '' : '（inactive）' }}
                </option>
              </select>
              <input
                :value="captureTargetName"
                placeholder="model"
                @change="saveCaptureName('laya_capture_target_name', ($event.target as HTMLInputElement).value)"
              />
            </div>
          </label>
        </div>
        <p class="muted small" style="margin: 8px 0 0;">
          场景：<span class="mono">{{ sceneNodes?.scene_path || '未解析到 game.ls' }}</span> ·
          推荐 target：<span class="mono">{{ sceneNodes?.recommended_target_name || '—' }}</span>
        </p>
      </section>

      <section v-if="referencePreviewItems.length" class="section">
        <h3 class="section-title">Unity 参考图预览</h3>
        <p v-if="unityReferenceFiles.length" class="muted small" style="margin: 0 0 8px;">
          当前使用项目内 Unity 多视角参考图，共 {{ unityReferenceFiles.length }} 张；这里只预览前三张，正式评分会按文件名里的 view id 匹配 Laya 多视角截图。
        </p>
        <div class="ref-preview" :class="{ 'ref-preview-grid': referencePreviewItems.length > 1 }">
          <figure v-for="item in referencePreviewItems" :key="item.url" class="ref-item">
            <img :src="item.url" :alt="item.name" />
            <figcaption class="mono small">{{ item.name }}</figcaption>
          </figure>
        </div>
      </section>

      <section class="section">
        <h3 class="section-title">多视角参考匹配</h3>
        <p class="muted small" style="margin: 0 0 8px;">
          启用 Laya Editor 后台多视角截图时，工具会从项目 inputs/unity_references 按 glob 找参考图，
          并用文件名里的 <span class="mono">v000_yaw0_pitch0</span> 这类 view id 匹配 Laya 截图。
        </p>
        <label class="window-field">
          <span class="window-label">unity_reference_glob</span>
          <input
            :value="project.inputs.unity_reference_glob || 'unity_ref_v*_yaw*_pitch*.png'"
            @change="(e) => saveReferenceGlob((e.target as HTMLInputElement).value)"
            class="window-input"
            placeholder="unity_ref_v*_yaw*_pitch*.png"
            :disabled="saving"
          />
        </label>
      </section>

      <RefreshPreflightCard
        :project-id="project.id"
        :lmat-path="project.inputs.laya_material_lmat_path"
        :editor-capture-enabled="editorCaptureEnabled"
      />

      <section class="section">
        <h3 class="section-title">危险区</h3>
        <button class="danger" @click="onDelete">删除项目</button>
        <span class="muted small" style="margin-left: 8px;">
          会被移动到 <span class="mono">output/.trash/</span>，可手动恢复。
        </span>
      </section>
    </template>
  </div>
</template>

<style scoped>
.project-config { display: flex; flex-direction: column; gap: 14px; padding-bottom: 24px; }
.pc-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.pc-head h2 { margin: 0 0 4px; font-size: 16px; }
.status-pill {
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-family: var(--mono);
  border: 1px solid;
}
.status-pill.ok { color: var(--good); border-color: var(--good); }
.status-pill.pending { color: var(--warn); border-color: var(--warn); }

.slot-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.slot {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
  display: flex; flex-direction: column; gap: 4px;
}
.slot-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.slot-label { font-weight: 600; font-size: 13px; }
.required { color: var(--bad); margin-left: 2px; }
.slot-hint { line-height: 1.4; }
.slot-actions .ghost { background: transparent; border: 1px dashed var(--border-strong); }
.slot-value {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  padding: 4px 8px;
  border-radius: 4px;
  word-break: break-all;
  color: var(--text-dim);
}
.slot-value.filled { color: var(--good); }

.ref-preview { background: #0d1117; padding: 8px; border-radius: var(--radius); border: 1px solid var(--border); }
.ref-preview-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.ref-item { margin: 0; }
.ref-item img { max-width: 100%; max-height: 360px; display: block; margin: 0 auto; }
.ref-preview-grid .ref-item img { max-height: 180px; }
.ref-item figcaption { margin-top: 4px; color: var(--text-dim); text-align: center; word-break: break-all; }

.capture-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.capture-field { display: flex; flex-direction: column; gap: 6px; }
.capture-field > span { font-weight: 600; font-size: 13px; }
.capture-row { display: grid; grid-template-columns: minmax(140px, 0.7fr) minmax(140px, 1fr); gap: 6px; }
.capture-row select,
.capture-row input {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 12px;
  min-width: 0;
}
@media (max-width: 900px) {
  .capture-grid,
  .capture-row { grid-template-columns: 1fr; }
}

.region-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.region-row .primary { background: var(--accent-strong); border-color: var(--accent-strong); color: white; }
.region-row .primary:disabled { opacity: 0.6; }
.region-row .ghost { background: transparent; border: 1px dashed var(--border-strong); }
.region-display-inline {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 10px;
  flex: 1;
  min-height: 30px;
}
.region-pill {
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  padding: 1px 10px;
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--mono);
}
.region-pill strong { color: var(--good); margin-left: 4px; }

.anchor-row {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 8px 10px;
}
.anchor-toggle { display: flex; align-items: flex-start; gap: 8px; cursor: pointer; }
.anchor-toggle input[type="checkbox"] { margin-top: 4px; }
.anchor-status { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
.anchor-status.ok { color: var(--text); }
.anchor-status.pending { color: var(--warn); }

.window-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 140px;
  gap: 8px;
}
.window-field { display: flex; flex-direction: column; gap: 4px; }
.window-label {
  font-size: 11px;
  color: var(--text-dim);
  font-family: var(--mono);
}
.window-input {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 12px;
}
.window-input--num { width: 100%; }
@media (max-width: 900px) { .window-grid { grid-template-columns: 1fr; } }

.danger {
  background: rgba(248, 81, 73, 0.12);
  border-color: rgba(248, 81, 73, 0.4);
  color: var(--bad);
}
.danger:hover { background: rgba(248, 81, 73, 0.2); }
@media (max-width: 900px) { .slot-grid { grid-template-columns: 1fr; } }
</style>

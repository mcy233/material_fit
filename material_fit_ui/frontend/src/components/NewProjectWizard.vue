<script setup lang="ts">
import { computed, ref } from 'vue';
import {
  createProject,
  importProjectInputFile,
  importUnityReferenceFiles,
  inspectLayaSceneNodes,
  patchProject,
  pickFile,
} from '../api';
import type { LayaSceneNode, LayaSceneNodesPayload, ProjectInputs } from '../types';

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'created', projectId: string): void;
}>();

const step = ref<1 | 2>(1);

const id = ref('');
const name = ref('');
const description = ref('');

const inputs = ref<ProjectInputs>({
  unity_shader_path: null,
  unity_material_params_path: null,
  unity_reference_image_path: null,
  unity_reference_dir_path: null,
  unity_reference_glob: 'unity_ref_v*_yaw*_pitch*.png',
  laya_shader_path: null,
  laya_material_lmat_path: null,
  laya_project_path: null,
  laya_capture_command_path: null,
  laya_capture_camera_name: 'Capture Camera',
  laya_capture_target_name: 'model',
  laya_capture_region: null,
  laya_capture_dir: null,
  laya_capture_state_file: null,
  laya_capture_prefix: 'laya_candidate',
});

const submitting = ref(false);
const error = ref<string | null>(null);
const sceneNodes = ref<LayaSceneNodesPayload | null>(null);
const unityReferenceSourcePaths = ref<string[]>([]);

const isIdValid = computed(() => /^[a-zA-Z0-9_\-]{1,64}$/.test(id.value));
const requiredFilled = computed(
  () => !!inputs.value.laya_shader_path && !!inputs.value.laya_material_lmat_path,
);

type ImportableInputKey = 'laya_shader_path' | 'unity_shader_path' | 'unity_material_params_path';

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

const slots: Slot[] = [
  {
    key: 'laya_shader_path',
    label: 'Laya 着色器（必选）',
    hint: '工程里实际使用的 .shader 文件，用于解析 uniformMap/defines',
    required: true,
    filetypes: [
      ['Laya shader', '*.shader *.vs *.fs'],
      ['All files', '*.*'],
    ],
    mode: 'import_file',
  },
  {
    key: 'laya_material_lmat_path',
    label: 'Laya 材质 .lmat（必选，写入目标）',
    hint: '自动调参会把候选参数写入这里（每轮先备份 .bak）',
    required: true,
    filetypes: [
      ['Laya material', '*.lmat'],
      ['All files', '*.*'],
    ],
    mode: 'real_path',
  },
  {
    key: 'unity_shader_path',
    label: 'Unity 着色器（可选）',
    hint: 'Unity ShaderLab .shader 文件；提供后才能做参数对照',
    required: false,
    filetypes: [
      ['Unity shader', '*.shader'],
      ['All files', '*.*'],
    ],
    mode: 'import_file',
  },
  {
    key: 'unity_material_params_path',
    label: 'Unity 材质参数 JSON（可选）',
    hint: 'Editor 工具导出的 unity 材质实际参数（params/properties dict）',
    required: false,
    filetypes: [
      ['JSON', '*.json'],
      ['All files', '*.*'],
    ],
    mode: 'import_file',
  },
  {
    key: 'unity_reference_dir_path',
    label: 'Unity 多视角参考截图',
    hint: '一次选择多张 unity_ref_v*_yaw*_pitch*.png；创建后会复制到项目 inputs/unity_references/',
    required: false,
    filetypes: [['Images', '*.png *.jpg *.jpeg *.bmp *.webp'], ['All files', '*.*']],
    mode: 'unity_refs',
  },
  {
    key: 'laya_project_path',
    label: 'Laya 项目目录（推荐）',
    hint: '包含 assets/ 的 Laya 项目根目录，用于定位脚本 command 文件',
    required: false,
    filetypes: [],
    isDir: true,
    mode: 'real_path',
  },
];

function close(): void {
  emit('close');
}

async function pick(slot: Slot): Promise<void> {
  error.value = null;
  try {
    const isDir = !!slot.isDir;
    const result = await pickFile({
      mode: slot.mode === 'unity_refs' ? 'open_many' : isDir ? 'directory' : 'open',
      title: slot.label,
      initial_dir: getInitialDir(slot.key),
      filetypes: isDir ? undefined : slot.filetypes,
    });
    if (result.error) {
      error.value = result.error;
      return;
    }
    if (slot.mode === 'unity_refs') {
      unityReferenceSourcePaths.value = result.paths ?? [];
      inputs.value = { ...inputs.value, unity_reference_dir_path: null };
      return;
    }
    if (result.path) {
      inputs.value = { ...inputs.value, [slot.key]: result.path } as ProjectInputs;
      if (slot.key === 'laya_project_path' || slot.key === 'laya_material_lmat_path') {
        await loadSceneNodes();
      }
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

function clearSlot(key: keyof ProjectInputs): void {
  if (key === 'unity_reference_dir_path') {
    unityReferenceSourcePaths.value = [];
  }
  inputs.value = { ...inputs.value, [key]: null } as ProjectInputs;
  if (key === 'laya_project_path' || key === 'laya_material_lmat_path') {
    void loadSceneNodes();
  }
}

function getInitialDir(key: keyof ProjectInputs): string | undefined {
  const value = inputs.value[key];
  if (typeof value === 'string' && value) {
    const idx = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'));
    return idx > 0 ? value.slice(0, idx) : undefined;
  }
  return undefined;
}

async function next(): Promise<void> {
  if (!isIdValid.value) {
    error.value = 'project id 只能用字母数字、下划线、短横线，长度 1-64';
    return;
  }
  step.value = 2;
}

async function submit(): Promise<void> {
  if (!requiredFilled.value) {
    error.value = '至少需要选择 Laya shader 与 Laya .lmat 两个文件';
    return;
  }
  submitting.value = true;
  error.value = null;
  try {
    const projectId = id.value.trim();
    await createProject({
      id: projectId,
      name: name.value.trim() || projectId,
      description: description.value.trim(),
    });
    await patchProject(projectId, {
      inputs: {
        laya_material_lmat_path: inputs.value.laya_material_lmat_path,
        laya_project_path: inputs.value.laya_project_path,
        laya_capture_camera_name: inputs.value.laya_capture_camera_name,
        laya_capture_target_name: inputs.value.laya_capture_target_name,
        unity_reference_glob: inputs.value.unity_reference_glob || '*.*',
      },
    });
    await importProjectInputFile(projectId, 'laya_shader_path', inputs.value.laya_shader_path || '');
    for (const key of ['unity_shader_path', 'unity_material_params_path'] as ImportableInputKey[]) {
      const source = inputs.value[key];
      if (source) {
        await importProjectInputFile(projectId, key, source);
      }
    }
    if (unityReferenceSourcePaths.value.length) {
      await importUnityReferenceFiles(projectId, unityReferenceSourcePaths.value);
    }
    emit('created', projectId);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    submitting.value = false;
  }
}

function shorten(value: string | null): string {
  if (!value) return '未选择';
  if (value.length <= 80) return value;
  return value.slice(0, 30) + ' … ' + value.slice(-45);
}

function slotValue(slot: Slot): string | null {
  if (slot.mode === 'unity_refs') {
    return unityReferenceSourcePaths.value.length ? `已选择 ${unityReferenceSourcePaths.value.length} 张参考图` : null;
  }
  const value = inputs.value[slot.key];
  return typeof value === 'string' ? value : null;
}

async function loadSceneNodes(): Promise<void> {
  try {
    sceneNodes.value = await inspectLayaSceneNodes(inputs.value);
    if (!inputs.value.laya_capture_camera_name && sceneNodes.value.recommended_camera_name) {
      inputs.value = { ...inputs.value, laya_capture_camera_name: sceneNodes.value.recommended_camera_name };
    }
    if (!inputs.value.laya_capture_target_name && sceneNodes.value.recommended_target_name) {
      inputs.value = { ...inputs.value, laya_capture_target_name: sceneNodes.value.recommended_target_name };
    }
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
</script>

<template>
  <Teleport to="body">
    <div v-if="props.open" class="wizard-overlay" @click.self="close">
      <div class="wizard">
        <header class="wizard-head">
          <h2>新建调参项目 <span class="muted small">step {{ step }}/2</span></h2>
          <button class="wizard-close" @click="close" aria-label="close">×</button>
        </header>

        <div v-if="error" class="error-banner">{{ error }}</div>

        <section v-if="step === 1" class="wizard-body">
          <p class="muted small">
            为这个调参任务起一个标识符。它会被用作 <span class="kbd">tools/material_fit/output/</span> 下的目录名。
          </p>
          <label class="field">
            <span>项目 ID（[a-zA-Z0-9_-]，1-64）</span>
            <input v-model="id" placeholder="例如 fish_2025_body" />
          </label>
          <label class="field">
            <span>显示名称</span>
            <input v-model="name" placeholder="可选，默认与 ID 相同" />
          </label>
          <label class="field">
            <span>说明</span>
            <textarea v-model="description" rows="2" placeholder="可选" />
          </label>
          <footer class="wizard-foot">
            <span class="muted small">下一步：选择文件与 Laya Editor command 配置</span>
            <button class="primary" :disabled="!isIdValid" @click="next">下一步</button>
          </footer>
        </section>

        <section v-else class="wizard-body">
          <div class="slot-grid">
            <div v-for="slot in slots" :key="slot.key" class="slot">
              <div class="slot-head">
                <span class="slot-label">
                  {{ slot.label }}
                  <span v-if="slot.required" class="required">*</span>
                </span>
                <div class="slot-actions">
                  <button @click="pick(slot)">{{ slot.mode === 'real_path' ? '选择…' : '导入…' }}</button>
                  <button v-if="slotValue(slot)" class="ghost" @click="clearSlot(slot.key)">清除</button>
                </div>
              </div>
              <div class="slot-hint muted small">{{ slot.hint }}</div>
              <div class="slot-value mono small" :class="{ filled: !!slotValue(slot) }">
                {{ shorten(slotValue(slot)) }}
              </div>
            </div>
          </div>

          <section class="inline-config">
            <h3>Laya 截图目标</h3>
            <p class="muted small">
              后续探针和正式多视角截图都会使用这两个名称。规范场景中 target_name 固定为 <span class="mono">model</span>。
            </p>
            <div class="inline-grid">
              <label class="field">
                <span>camera_name</span>
                <select v-if="cameraOptions.length" v-model="inputs.laya_capture_camera_name">
                  <option
                    v-for="node in cameraOptions"
                    :key="node.path"
                    :value="node.name"
                  >{{ node.name }}{{ node.active ? '' : '（inactive）' }}</option>
                </select>
                <input v-model="inputs.laya_capture_camera_name" placeholder="Capture Camera" />
              </label>
              <label class="field">
                <span>target_name（旋转目标）</span>
                <select v-if="targetOptions.length" v-model="inputs.laya_capture_target_name">
                  <option
                    v-for="node in targetOptions"
                    :key="node.path"
                    :value="node.name"
                  >{{ node.name }}{{ node.active ? '' : '（inactive）' }}</option>
                </select>
                <input v-model="inputs.laya_capture_target_name" placeholder="model" />
              </label>
            </div>
            <p class="muted small" style="margin: 8px 0 0;">
              场景：<span class="mono">{{ sceneNodes?.scene_path || '选择 Laya 项目目录后自动解析' }}</span>
            </p>
          </section>

          <footer class="wizard-foot">
            <button @click="step = 1">上一步</button>
            <span class="muted small" :class="{ ok: requiredFilled }">
              {{ requiredFilled ? '✓ 必选输入已就绪' : '⚠ Laya shader + .lmat 必填' }}
            </span>
            <button class="primary" :disabled="!requiredFilled || submitting" @click="submit">
              {{ submitting ? '创建中…' : '创建项目' }}
            </button>
          </footer>
        </section>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.wizard-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
}
.wizard {
  width: min(820px, 92vw);
  max-height: 88vh;
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}
.wizard-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
}
.wizard-head h2 { margin: 0; font-size: 15px; font-weight: 600; }
.wizard-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 18px;
  cursor: pointer;
  padding: 0 8px;
}
.wizard-body {
  padding: 14px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.field { display: flex; flex-direction: column; gap: 4px; }
.field input, .field textarea, .field select {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 10px;
  border-radius: var(--radius);
  font-family: inherit;
}
.slot-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.slot {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.slot-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.slot-label { font-weight: 600; font-size: 13px; }
.required { color: var(--bad); margin-left: 2px; }
.slot-hint { line-height: 1.4; }
.slot-actions { display: flex; gap: 4px; }
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

.inline-config {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
}
.inline-config h3 { margin: 0 0 4px; font-size: 13px; }
.inline-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }

.region-block {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 10px;
}
.region-display {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 6px 10px;
  min-height: 28px;
  align-items: center;
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

.wizard-foot {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.wizard-foot .ok { color: var(--good); }
.wizard-foot .primary {
  background: var(--accent-strong);
  border-color: var(--accent-strong);
  color: white;
}
.wizard-foot .primary:disabled { opacity: 0.5; }
@media (max-width: 720px) {
  .slot-grid,
  .inline-grid { grid-template-columns: 1fr; }
}
</style>

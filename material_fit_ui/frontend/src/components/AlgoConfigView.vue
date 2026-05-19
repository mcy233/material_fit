<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { fetchProject, patchProject } from '../api';
import type { AlgorithmConfig, OptimizerKind, ProjectDetail } from '../types';

const props = defineProps<{ projectId: string }>();
const emit = defineEmits<{ (e: 'changed'): void }>();

const project = ref<ProjectDetail | null>(null);
const error = ref<string | null>(null);
const saving = ref(false);
const ok = ref(false);

function defaultConfig(): AlgorithmConfig {
  return {
    max_iterations: 6,
    target_score: 0.5,
    apply_lmat: true,
    capture_screen_after_apply: false,
    use_laya_editor_capture: true,
    laya_editor_capture: {
      reload_scene_after_reimport: true,
      refresh_after_reimport_delay_ms: 800,
      timeout_s: 90,
      capture_mode: 'rotate_target',
    },
    rerender_wait_ms: 900,
    use_capture_contract: false,
    dry_run: false,
    fit_score_mode: 'human_accept',
    auto_adjust_mode: 'fresh_fit',
    optimizer: 'semantic_group',
    cma_es: {
      mode: 'warm',
      warm_start_iters: 12,
      population_size: null,
      sigma: null,
      seed: null,
      hint_bias_mix_ratio: 0.30,
    },
  };
}

const form = reactive<AlgorithmConfig>(defaultConfig());

const isCma = computed(() => form.optimizer === 'cma_cold' || form.optimizer === 'cma_warm');

const optimizerHelp: Record<OptimizerKind, string> = {
  heuristic: '旧的固定 stage 反馈控制器。可解释但没有组级回滚，适合作为对照基线。',
  cma_cold: '黑盒 CMA-ES，从初始 .lmat 开始无任何 prior。适合作为 cma_warm 的对照基线；高维下 200 轮以内可能比 random 还差。',
  cma_warm: 'Warm-Started CMA-ES (Nomura et al., AAAI 2021)。把已有迭代的 (params, fit_score) 当 prior 初始化协方差，合成实验中比 cma_cold 快 2~3×。需要 ≥2 轮历史，否则自动降级到 cma_cold。',
  semantic_group: '推荐路径。按运行控制台的控件预设缩小搜索空间，做组级探针、接受/拒绝回滚和组内 pattern search。',
};

async function load(): Promise<void> {
  if (!props.projectId) return;
  try {
    const data = await fetchProject(props.projectId);
    project.value = data;
    const merged: AlgorithmConfig = {
      ...defaultConfig(),
      ...data.algorithm_config,
      capture_screen_after_apply: false,
      use_laya_editor_capture: true,
      cma_es: { ...defaultConfig().cma_es, ...(data.algorithm_config?.cma_es ?? {}) },
      laya_editor_capture: {
        ...defaultConfig().laya_editor_capture,
        ...(data.algorithm_config?.laya_editor_capture ?? {}),
      },
    };
    Object.assign(form, merged);
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

watch(() => props.projectId, () => { void load(); });
onMounted(() => { void load(); });

async function save(): Promise<void> {
  if (!project.value) return;
  saving.value = true;
  ok.value = false;
  try {
    const payload: AlgorithmConfig = {
      ...form,
      capture_screen_after_apply: false,
      use_laya_editor_capture: true,
      cma_es: { ...form.cma_es, mode: form.optimizer === 'cma_cold' ? 'cold' : 'warm' },
    };
    const result = await patchProject(project.value.id, {
      algorithm_config: payload,
    });
    project.value = result;
    const merged: AlgorithmConfig = {
      ...defaultConfig(),
      ...result.algorithm_config,
      capture_screen_after_apply: false,
      use_laya_editor_capture: true,
      cma_es: { ...defaultConfig().cma_es, ...(result.algorithm_config?.cma_es ?? {}) },
      laya_editor_capture: {
        ...defaultConfig().laya_editor_capture,
        ...(result.algorithm_config?.laya_editor_capture ?? {}),
      },
    };
    Object.assign(form, merged);
    ok.value = true;
    setTimeout(() => { ok.value = false; }, 1500);
    emit('changed');
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="algo-config">
    <header style="display: flex; align-items: baseline; gap: 12px;">
      <h2 class="section-title" style="margin: 0;">算法配置</h2>
      <span class="muted small">控制 fit_material.py 的核心 CLI 行为</span>
    </header>

    <div v-if="error" class="error-banner">{{ error }}</div>

    <section class="section" v-if="project">
      <table class="cfg-table">
        <tbody>
          <tr>
            <td>
              <label for="cfg-optimizer">optimizer</label>
              <p class="muted small">{{ optimizerHelp[form.optimizer] }}</p>
            </td>
            <td>
              <select id="cfg-optimizer" v-model="form.optimizer">
                <option value="semantic_group">semantic_group（推荐 / 语义分组搜索）</option>
                <option value="heuristic">heuristic（旧 stage 基线）</option>
                <option value="cma_warm">cma_warm（Warm-Started CMA-ES）</option>
                <option value="cma_cold">cma_cold（vanilla CMA-ES）</option>
              </select>
            </td>
          </tr>
          <tr v-if="isCma" class="cma-block">
            <td colspan="2">
              <h3 class="sub">CMA-ES 调参（仅当 optimizer 为 cma_* 时生效）</h3>
              <table class="cfg-subtable">
                <tbody>
                  <tr>
                    <td>
                      <label for="cma-warm-iters">warm_start_iters</label>
                      <p class="muted small">cma_warm 时，最多用多少轮历史 (params, fit_score) 作为 prior。&lt;2 自动降级为 cold。</p>
                    </td>
                    <td>
                      <input id="cma-warm-iters" type="number" min="0" max="200" step="1"
                        v-model.number="form.cma_es.warm_start_iters" :disabled="form.optimizer !== 'cma_warm'" />
                    </td>
                  </tr>
                  <tr>
                    <td>
                      <label for="cma-pop">population_size</label>
                      <p class="muted small">每代采样数。空 = 库默认（4 + 3·ln(dim)，d≈30 时约 14）。真实 Laya 跑 ≤8 较合算。</p>
                    </td>
                    <td>
                      <input id="cma-pop" type="number" min="1" max="64" step="1" placeholder="auto"
                        v-model.number="form.cma_es.population_size" />
                    </td>
                  </tr>
                  <tr>
                    <td>
                      <label for="cma-sigma">sigma（normalized）</label>
                      <p class="muted small">初始步长，[0,1] 归一化空间下。空 = 0.30。0.1 太保守，0.5 太发散。</p>
                    </td>
                    <td>
                      <input id="cma-sigma" type="number" min="0.01" max="1" step="0.05" placeholder="0.30"
                        v-model.number="form.cma_es.sigma" />
                    </td>
                  </tr>
                  <tr>
                    <td>
                      <label for="cma-seed">seed</label>
                      <p class="muted small">复现实验时设固定种子；空 = 不固定。</p>
                    </td>
                    <td>
                      <input id="cma-seed" type="number" min="0" max="2147483647" step="1" placeholder="random"
                        v-model.number="form.cma_es.seed" />
                    </td>
                  </tr>
                  <tr>
                    <td>
                      <label for="cma-hint-bias">hint_bias_mix_ratio <span class="badge">E-010</span></label>
                      <p class="muted small">
                        把 image_analysis 给出的"暗部应增/减、emission 应增/减"等 channel 级建议混入 CMA-ES 每轮提议的力度。
                        <strong>0</strong> = 完全不偏置（旧版行为）；
                        <strong>0.30</strong> 推荐起步；
                        <strong>0.50+</strong> 偏置主导，适合快速 sanity check 不适合精细收敛。
                      </p>
                    </td>
                    <td>
                      <input id="cma-hint-bias" type="number" min="0" max="1" step="0.05" placeholder="0.30"
                        v-model.number="form.cma_es.hint_bias_mix_ratio" />
                    </td>
                  </tr>
                </tbody>
              </table>
            </td>
          </tr>
          <tr>
            <td>
              <label for="cfg-max-iter">max_iterations</label>
              <p class="muted small">
                最多迭代多少轮才停止。
                <span v-if="isCma">CMA-ES 模式下相当于评估预算（每轮 = 1 次 ask/render/tell）。</span>
                <span v-else>启发式模式下，阶段切换不会重置计数。</span>
              </p>
            </td>
            <td>
              <input id="cfg-max-iter" type="number" min="1" max="500" v-model.number="form.max_iterations" />
            </td>
          </tr>
          <tr>
            <td>
              <label for="cfg-target">target_score</label>
              <p class="muted small">
                fit_score 达到该值即终止。human_accept 是当前默认目标；perceptual / linear 主要用于诊断对照。
              </p>
            </td>
            <td>
              <input id="cfg-target" type="number" step="0.01" min="0" max="1" v-model.number="form.target_score" />
            </td>
          </tr>
          <tr>
            <td>
              <label for="cfg-mode">fit_score_mode</label>
              <p class="muted small">
                <strong>human_accept</strong>（推荐）：弱化姿态/视角微差带来的像素惩罚，重点比较前景颜色分布和材质统计。
                <br/>
                <strong>perceptual</strong>：更严格的通道加权 MAE + SSIM，用于诊断。
                <br/>
                <strong>linear</strong>（旧逻辑）：<span class="mono">1 - MAE</span>，非常宽松，仅用于对照。
              </p>
            </td>
            <td>
              <select id="cfg-mode" v-model="form.fit_score_mode">
                <option value="human_accept">human_accept</option>
                <option value="perceptual">perceptual</option>
                <option value="linear">linear (legacy)</option>
              </select>
            </td>
          </tr>
          <tr>
            <td>
              <label for="cfg-rerender">rerender_wait_ms</label>
              <p class="muted small">apply 后等待 Laya 编辑器重渲染的毫秒数。</p>
            </td>
            <td>
              <input id="cfg-rerender" type="number" min="0" max="60000" step="100" v-model.number="form.rerender_wait_ms" />
            </td>
          </tr>
          <tr>
            <td>
              <label><input type="checkbox" v-model="form.apply_lmat" /> apply_lmat</label>
              <p class="muted small">勾选后，每轮真实写入 .lmat 并自动备份 .bak。否则只写候选副本。</p>
            </td>
            <td class="muted small mono">{{ form.apply_lmat ? '--apply-lmat --write-candidate-lmat' : '(不写真 .lmat)' }}</td>
          </tr>
          <tr class="legacy-disabled">
            <td>
              <label><input type="checkbox" :checked="false" disabled /> capture_screen_after_apply（旧屏幕截图，已禁用）</label>
              <p class="muted small">当前自动化工具统一使用 Laya Editor 脚本截图，不再唤醒前端窗口做固定区域截图。</p>
            </td>
            <td class="muted small mono">(disabled)</td>
          </tr>
          <tr>
            <td>
              <label><input type="checkbox" :checked="true" disabled /> use_laya_editor_capture</label>
              <p class="muted small">
                使用 Laya Editor 扩展后台执行 reimport / reload scene / 相机截图 / 多视角 RenderTexture 截图。这是当前唯一维护的截图路径。
              </p>
            </td>
            <td class="muted small mono">laya_editor_capture.enabled=true</td>
          </tr>
          <tr class="editor-capture-block">
            <td colspan="2">
              <h3 class="sub">Laya Editor 后台截图</h3>
              <table class="cfg-subtable">
                <tbody>
                  <tr>
                    <td>
                      <label>
                        <input type="checkbox" v-model="form.laya_editor_capture.reload_scene_after_reimport" />
                        reload_scene_after_reimport
                      </label>
                      <p class="muted small">材质 reimport 后重载当前场景，确保场景实例拿到最新 .lmat。</p>
                    </td>
                    <td class="muted small mono">
                      {{ form.laya_editor_capture.reload_scene_after_reimport ? 'true' : 'false' }}
                    </td>
                  </tr>
                  <tr>
                    <td>
                      <label for="editor-refresh-delay">refresh_after_reimport_delay_ms</label>
                      <p class="muted small">reimport/reload 后等待多少毫秒再截图。Laya 项目大时可提高到 1200~2000。</p>
                    </td>
                    <td>
                      <input
                        id="editor-refresh-delay"
                        type="number"
                        min="0"
                        max="10000"
                        step="100"
                        v-model.number="form.laya_editor_capture.refresh_after_reimport_delay_ms"
                      />
                    </td>
                  </tr>
                  <tr>
                    <td>
                      <label for="editor-timeout">timeout_s</label>
                      <p class="muted small">Python 等待 Laya 写出多视角 report 的最长秒数。</p>
                    </td>
                    <td>
                      <input
                        id="editor-timeout"
                        type="number"
                        min="5"
                        max="600"
                        step="5"
                        v-model.number="form.laya_editor_capture.timeout_s"
                      />
                    </td>
                  </tr>
                </tbody>
              </table>
            </td>
          </tr>
          <tr>
            <td>
              <label><input type="checkbox" v-model="form.use_capture_contract" /> use_capture_contract</label>
              <p class="muted small">使用 RenderDriver 的 capture 契约（Puppeteer 走 capture_laya.js 时启用）。</p>
            </td>
            <td class="muted small mono">{{ form.use_capture_contract ? '--capture' : '(legacy render_candidate)' }}</td>
          </tr>
          <tr>
            <td>
              <label><input type="checkbox" v-model="form.dry_run" /> dry_run</label>
              <p class="muted small">不调外部渲染器，只走分析+写候选；用于不污染 .lmat 的演练。</p>
            </td>
            <td class="muted small mono">{{ form.dry_run ? '--dry-run' : '(真实跑)' }}</td>
          </tr>
        </tbody>
      </table>
      <footer style="display: flex; align-items: center; gap: 12px; margin-top: 8px;">
        <button class="primary" @click="save" :disabled="saving">{{ saving ? '保存中…' : '保存配置' }}</button>
        <span v-if="ok" class="muted small ok">✓ 已保存</span>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.algo-config { display: flex; flex-direction: column; gap: 14px; padding-bottom: 24px; }
.cfg-table { width: 100%; border-collapse: collapse; }
.cfg-table td {
  padding: 10px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.cfg-table td:first-child { width: 65%; }
.cfg-table td:last-child {
  text-align: right;
  font-family: var(--mono);
  white-space: nowrap;
}
.cfg-table label { font-weight: 600; }
.cfg-table p { margin: 2px 0 0; }
.cfg-table input[type="number"] {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: var(--mono);
  width: 120px;
  text-align: right;
}
.cfg-table input[type="checkbox"] { vertical-align: -2px; margin-right: 6px; }
.cfg-table select {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: var(--mono);
  min-width: 240px;
}
.cma-block td,
.editor-capture-block td {
  background: rgba(255, 200, 80, 0.04);
  border-left: 3px solid var(--accent, #c79a3d);
  padding-left: 12px;
}
.sub {
  margin: 0 0 8px;
  font-size: 13px;
  color: var(--accent, #c79a3d);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.cfg-subtable { width: 100%; border-collapse: collapse; }
.cfg-subtable td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.cfg-subtable td:first-child { width: 65%; }
.cfg-subtable td:last-child {
  text-align: right;
  font-family: var(--mono);
  white-space: nowrap;
}
.cfg-subtable input[type="number"] {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: var(--mono);
  width: 120px;
  text-align: right;
}
.cfg-subtable input[type="number"]:disabled { opacity: 0.5; }
.primary { background: var(--accent-strong); border-color: var(--accent-strong); color: white; }
.primary:disabled { opacity: 0.5; }
.ok { color: var(--good); }
.badge {
  display: inline-block;
  margin-left: 6px;
  padding: 0 6px;
  font-size: 10px;
  border-radius: 999px;
  background: rgba(53, 132, 228, 0.18);
  color: #3584e4;
  vertical-align: middle;
  font-weight: 600;
  letter-spacing: 0.05em;
}
</style>

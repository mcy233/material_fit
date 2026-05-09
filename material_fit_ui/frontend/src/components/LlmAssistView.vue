<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { fetchProject, patchProject } from '../api';
import type { ProjectDetail } from '../types';

const props = defineProps<{ projectId: string }>();
const project = ref<ProjectDetail | null>(null);
const error = ref<string | null>(null);
const enabled = ref(false);
const provider = ref('');

async function load(): Promise<void> {
  if (!props.projectId) return;
  try {
    project.value = await fetchProject(props.projectId);
    enabled.value = !!project.value.llm_config?.enabled;
    provider.value = project.value.llm_config?.provider ?? '';
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

watch(() => props.projectId, () => { void load(); });
onMounted(() => { void load(); });

async function save(): Promise<void> {
  if (!project.value) return;
  try {
    project.value = await patchProject(project.value.id, {
      llm_config: { enabled: enabled.value, provider: provider.value || null },
    });
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}
</script>

<template>
  <div class="llm-view">
    <header style="display: flex; gap: 12px; align-items: baseline;">
      <h2 class="section-title" style="margin: 0;">LLM 助手</h2>
      <span class="muted small">OpenAI-compatible · 预分析阶段读取 shader 生成语义先验</span>
    </header>

    <div v-if="error" class="error-banner">{{ error }}</div>

    <section class="section">
      <h3 class="section-title">规划用途</h3>
      <ul class="plan-list">
        <li>
          <strong>预分析阶段</strong>：把 Unity/Laya shader 源 + 解析后 uniformMap 喂给 LLM，
          要求生成 Unity 参考现象、Laya 效果组、gate/define、参数角色和初版参数参考。
        </li>
        <li>
          <strong>闭环调参</strong>：暂时不让 LLM 进入每轮优化循环。模型输出只作为语义先验，
          后续由探针、pattern search 和 CMA-ES 用真实截图评分。
        </li>
        <li>
          <strong>停滞救援</strong>：当 fit_score 连续 N 轮无明显改进，算法层主动调用 LLM 提议
          一个跨阶段的"突破策略"，并以 <span class="kbd">--apply-llm-suggestion</span> 的方式
          灌回参数空间。
        </li>
        <li>
          <strong>报告强化</strong>：完成后把 report.md + 全部迭代摘要交给 LLM，让它写一段
          "为什么这次跑成功/失败"的归因总结放在 report 顶部。
        </li>
      </ul>
    </section>

    <section class="section" v-if="project">
      <h3 class="section-title">本项目的 LLM 设置</h3>
      <p class="muted small">
        后端从仓库根目录的 <span class="mono">.env</span> 读取
        <span class="mono">OPENAI_BASE_URL</span>、<span class="mono">OPENAI_API_KEY</span>、
        <span class="mono">OPENAI_MODEL</span>。API key 不会存入项目配置。
      </p>
      <div class="llm-form">
        <label>
          <input type="checkbox" v-model="enabled" />
          启用 LLM 语义预分析
        </label>
        <label>
          provider
          <select v-model="provider">
            <option value="">未选择</option>
            <option value="openai-compatible">OpenAI-compatible</option>
          </select>
        </label>
        <button class="primary" @click="save">保存设置</button>
      </div>
    </section>

    <section class="section">
      <h3 class="section-title">后续硬缺口对照</h3>
      <p class="muted small">
        这些项目落实之后，本面板里 "运行中"按钮会真正点亮：
      </p>
      <ul class="gap-list">
        <li>
          <span class="kbd">优先级 P0</span> 真实 Laya 渲染回路：把
          <span class="mono">render_driver.RenderDriver</span> 跟你的 Laya 工程的
          预览页 / Editor 真实连起来（通过 capture_laya.js 或直接读 LayaAir 编辑器的截图区域），
          否则 capture_screen_after_apply 等于看的还是上一帧。
        </li>
        <li>
          <span class="kbd">优先级 P0</span> 真正的搜索算法：当前
          <span class="mono">optimizer/adjustment_algorithm.py</span> 是反馈控制器，没有 rollback/分支搜索。
          要么加上 best-of-N 多候选并行评估，要么挂到 <span class="mono">scipy.optimize</span> /
          <span class="mono">scikit-optimize</span> 之类的实现。
        </li>
        <li>
          <span class="kbd">优先级 P1</span> 语义输出校验：LLM 负责理解 shader，确定性代码负责拒绝未知参数、
          未知 define 和非法 gate。
        </li>
        <li>
          <span class="kbd">优先级 P1</span> 区域语义分析：图像 diff 现在是全图 RGB MAE。
          要根据材质语义（高光/阴影/边缘）按 mask 加权评分，避免高频噪声拉低分数。
        </li>
        <li>
          <span class="kbd">优先级 P2</span> 结果缓存和对照评估：缓存每次 LLM 语义结果，并比较
          LLM 主路径与命名 fallback 在真实 shader 上的差异。
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.llm-view { display: flex; flex-direction: column; gap: 14px; padding-bottom: 24px; }
.plan-list, .gap-list { padding-left: 22px; margin: 4px 0; }
.plan-list li, .gap-list li { margin: 6px 0; line-height: 1.55; }
.gap-list .kbd {
  display: inline-block;
  margin-right: 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  color: var(--accent);
  padding: 1px 6px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 11px;
}
.llm-form {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 8px 12px;
  border-radius: var(--radius);
}
.llm-form select {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 6px;
  border-radius: 4px;
}
.primary { background: var(--accent-strong); border-color: var(--accent-strong); color: white; }
</style>

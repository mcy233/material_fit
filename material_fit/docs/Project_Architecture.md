# Material Fit 项目架构说明

本文档说明 `tools/material_fit/` 与 `tools/material_fit_ui/` 的当前项目结构、运行数据流和文件清理边界。

注意：用户口头提到的 `material_ui` 在当前仓库中实际对应目录是 `material_fit_ui/`。

## 一句话总览

`material_fit` 是算法与引擎适配层，负责解析 Unity/Laya 材质与 Shader、改写 Laya `.lmat`、触发 Laya Editor 截图、计算图像相似度并执行自动调参循环。

`material_fit_ui` 是本地浏览器控制台，负责项目配置、预分析、探针测试、启动/取消自动调参任务、展示每轮迭代图片和报告。它不复制算法，而是通过 FastAPI 后端调用 `material_fit` 的 Python 模块或以子进程启动 `fit_material.py`。

核心数据落点是：

```text
tools/material_fit/output/<project_id>/
```

这里既保存 UI 项目状态，也保存探针、任务日志、每次 run、每轮迭代截图和候选材质。

## 顶层关系

```text
tools/
  material_fit/              # 算法、CLI、Laya/Unity 适配、视觉评分、运行产物
  material_fit_ui/           # FastAPI 后端 + Vue 前端
  Editor/                    # Laya Editor 扩展脚本模板，需同步到 Laya 工程 assets/Editor/
```

当前自动截图链路还依赖 Laya 项目中的脚本副本：

```text
D:/project_data/laya/laya_research/laya_project/assets/Editor/CameraCapture.ts
D:/project_data/laya/laya_research/laya_project/assets/Editor/CameraCaptureEnv.ts
D:/project_data/laya/laya_research/laya_project/assets/material_fit_capture_command.json
```

其中 `tools/Editor/` 更像工具仓库中的“源模板”，Laya 项目的 `assets/Editor/` 是 Laya Editor 实际加载运行的版本。修改截图脚本后，需要确保两边同步，否则会出现“代码看起来改了，但 Laya 实际跑旧脚本”的问题。

## `material_fit/` 目录

### 核心职责

`material_fit/` 是可脱离 UI 运行的算法包。它可以通过 CLI 直接执行，也可以被 UI 后端 import。

主要职责包括：

- 读取配置 `fit_config.json`。
- 解析 Laya Shader3D 和 `.lmat`。
- 解析 Unity ShaderLab 和 Unity 导出的材质参数 JSON。
- 建立 Laya 可调参数空间与语义分组。
- 写入候选 `.lmat` 并备份原文件。
- 触发 Laya Editor 脚本截图。
- 对 Unity 参考图和 Laya 候选图做单视角/多视角评分。
- 根据评分结果选择下一组参数。
- 写出每轮 `decision.json`、截图、diff 分析和总结报告。

### 入口文件

```text
material_fit/fit_material.py
```

这是 CLI 主入口，也是 UI 后端启动自动调参子进程时调用的模块：

```text
python -m tools.material_fit.fit_material --config <run_dir>/fit_config.json --auto-adjust ...
```

它串联以下模块：

- `laya/lmat_io.py`
- `laya/shader_parser.py`
- `unity/shader_parser.py`
- `optimizer/*`
- `auto_adjust/*`
- `vision/*`
- `laya_capture/editor_bridge.py`

### 重要子目录

```text
material_fit/
  auto_adjust/
  docs/
  experiments/
  laya/
  laya_capture/
  optimizer/
  output/
  shared/
  tests/
  unity/
  vision/
```

### `auto_adjust/`

自动调参循环的辅助层。

```text
auto_adjust/history.py       # warm-start 历史样本读取
auto_adjust/image_pairs.py   # 组织 Unity/Laya 图像对
auto_adjust/loop.py          # 自动调参循环相关逻辑
auto_adjust/scoring.py       # diff score、fit score、human_accept 等评分汇总
```

它不是 UI 层，而是 `fit_material.py` 的内部算法支持模块。

### `optimizer/`

参数搜索策略与语义搜索空间。

```text
optimizer/strategy.py                  # 优化器统一接口和构造
optimizer/heuristic_strategy.py         # 启发式策略
optimizer/cma_es_optimizer.py           # CMA-ES 包装
optimizer/semantic_group_strategy.py    # 语义分组搜索
optimizer/semantic_graph.py             # Shader 参数语义图
optimizer/parameter_search.py           # 初始参数、stage plan、probe 候选
optimizer/adjustment_algorithm.py       # stage/policy/停止条件等传统调参逻辑
```

当前 UI 默认配置倾向使用 `semantic_group`，但仍保留 `heuristic`、`cma_cold`、`cma_warm` 用于对照和回退。

### `laya/`

Laya 侧本地文件和旧渲染链路适配。

```text
laya/shader_parser.py      # 解析 Laya Shader3D uniformMap / defines
laya/lmat_io.py            # 读取、备份、写入、校验 .lmat
laya/refresh_probe.py      # 探针：改颜色、截图、恢复、截图，验证 Laya 是否刷新
laya/render_driver.py      # 旧的渲染驱动抽象
laya/window_focus.py       # 旧桌面截图时代的窗口聚焦工具
```

说明：

- `lmat_io.py` 仍是核心模块，因为工具真实改写的是 Laya `.lmat`。
- `refresh_probe.py` 仍是核心模块，但现在探针截图应走 Laya Editor 脚本相机截图。
- `window_focus.py` 和部分桌面截图逻辑属于历史兼容代码。当前项目模式默认不再依赖“唤醒 Laya 前台 + 屏幕区域截图”。

### `laya_capture/`

Laya Editor 自动截图桥接层。

```text
laya_capture/editor_bridge.py             # Python 写 command JSON，等待 Laya Editor 报告文件
laya_capture/capture_command.example.json # Laya command JSON 示例
laya_capture/capture_server.py            # 早期/备用 HTTP 接收截图方案
```

当前维护中的主链路是 `editor_bridge.py`：

1. Python 更新 `material_fit_capture_command.json`，写入新的 `nonce`。
2. Laya Editor 扩展脚本轮询到新命令。
3. Laya Editor 重导入材质、可选重载场景。
4. Laya Editor 用 `Capture Camera` 或多视角命令渲染截图。
5. Laya Editor 在输出目录写 `laya_editor_selected_camera_report.json` 或 `laya_editor_multiview_report.json`。
6. Python 等到报告出现后继续评分。

### `vision/`

图像预处理、diff 和评分。

```text
vision/diff_analysis.py          # 当前主图像分析入口
vision/perceptual_score.py       # 感知评分、mask、通道指标
vision/human_accept_score.py     # 更贴近人工接受度的评分
vision/background_normalize.py   # 背景归一化
vision/analyze_diff.py           # 单次 diff CLI
vision/screen_capture.py         # 旧桌面截图模块
vision/image_score.py            # 老全图 RGB MAE 保底实现
```

当前自动拟合主链路更关注 `diff_analysis.py`、`perceptual_score.py`、`human_accept_score.py`。`screen_capture.py` 保留为 legacy/测试兼容，不应再作为项目默认截图方式。

### `unity/`

Unity 侧解析和参考资源。

```text
unity/shader_parser.py
unity/unity_material_exporter.cs
unity/unity_shader/
unity/test __________/
```

- `shader_parser.py` 用于解析 Unity ShaderLab 参数。
- `unity_material_exporter.cs` 是 Unity Editor 中导出材质实例参数的脚本。
- `unity_shader/` 保存参考 shader。
- `unity/test __________/` 里有大量 PNG/JSON，更像历史测试基准和实验数据，不是核心库代码。

### `shared/`

跨模块共享结构。

```text
shared/models.py
shared/report.py
```

用于报告写出和跨模块数据结构。

### `tests/`

pytest 测试，包括：

- `.lmat` 读写。
- 图像评分。
- 优化器。
- 自动调参循环模拟。
- 旧屏幕截图/窗口聚焦兼容测试。

这些测试不参与用户实际运行时流程，也不会被 UI 后端直接调用；但它们是开发和重构时防止回归的验证资产。结论是：**不能说“已经没用了”**。如果只想瘦身运行目录，不应删除 `tests/`；只有在明确不再维护这套工具、且不关心回归验证时，才考虑移除。

### `experiments/` 与 `experiments_out/`

`experiments/` 是实验脚本目录，目前主要包含：

```text
experiments/cma_es_warm_start_benchmark.py
```

该脚本用于比较 CMA-ES cold start、warm start、noisy warm start 等策略，不是 UI 主链路，也不是 `fit_material.py` 自动运行时依赖。它属于研究/消融实验代码，建议保留，除非确定后续论文或算法对照不再需要。

`experiments_out/` 是实验脚本的默认输出目录。`cma_es_warm_start_benchmark.py` 会把结果写到：

```text
material_fit/experiments_out/cma_es_warm_start_benchmark/<timestamp>/
```

当前若该目录为空，可以删除；之后再次运行实验脚本会重新创建。

### `docs/`

设计、实验和项目说明文档。本文档也放在这里。

比较重要的现有文档：

```text
docs/File_Layout_And_Artifacts.md
docs/Laya_Multiview_Capture.md
docs/Optimizer_Current_State_And_Next_Plan.md
docs/CrossEngineMaterialFit_Research.md
```

### `output/`

这是最容易变乱的目录，也是当前文件很多的主要来源。

```text
material_fit/output/<project_id>/
  project.json
  preanalysis.json
  fit_config.json
  inputs/
  jobs/
  preflight/
  runs/
  unity_reference/
```

它是运行产物目录，不是源码目录。

但是不要简单整目录删除，因为里面可能有项目配置和实验结果。

## `material_fit_ui/` 目录

### 核心职责

`material_fit_ui/` 是本地 Web 应用。它把 `material_fit` 的能力包装成浏览器界面。

它负责：

- 创建和管理项目。
- 保存输入路径和算法配置。
- 执行预分析。
- 执行 Laya 探针。
- 启动/取消自动调参 job。
- 展示每轮截图、diff、参数变化、日志和报告。

它不直接做材质优化算法，算法仍在 `material_fit/` 中。

### 目录结构

```text
material_fit_ui/
  backend/
  frontend/
  launch.py
  launch.bat
  requirements.txt
  README.md
```

### `backend/`

FastAPI 后端。

```text
backend/main.py
backend/case_loader.py
backend/project_store.py
backend/preanalysis.py
backend/preflight.py
backend/job_manager.py
backend/file_dialog.py
backend/llm_client.py
backend/preanalysis_parts/
backend/routers/
```

#### `main.py`

FastAPI 应用入口，挂载所有 router：

```text
cases
projects
files
preanalysis
preflight
jobs
```

#### `project_store.py`

UI 项目的事实来源。

它维护：

```text
material_fit/output/<project_id>/project.json
```

`project.json` 保存：

- 用户输入路径：Unity shader、Unity 材质参数、Unity 参考图目录、Laya shader、Laya `.lmat`、Laya 项目路径、Laya command JSON。
- 算法配置：最大迭代数、目标分、优化器、是否写 `.lmat`、是否使用 Laya Editor 截图。
- 手工参数映射。
- Laya 控制面板 schema。
- 当前 job 和最近 job。
- 当前 run 和最近 run。

它还负责把项目状态派生成 `fit_config.json`，供 `fit_material.py` 使用。

当前项目模式中，`derive_fit_config()` 会强制使用 Laya Editor 截图路径作为默认主链路。旧桌面截图字段在 `project.json` 里可能还存在，但主要是历史兼容数据。

#### `job_manager.py`

自动调参任务管理器。

启动 job 时会：

1. 读取 `project.json`。
2. 派生本次运行的 `fit_config.json`。
3. 创建独立 run 目录。
4. 以子进程启动：

```text
python -m tools.material_fit.fit_material --config <run_dir>/fit_config.json --auto-adjust ...
```

5. 把 stdout/stderr 写到 `jobs/<job_id>.log`。
6. 把状态写到 `jobs/<job_id>.json`。
7. 后台 watcher 观察 `runs/<run_id>/auto_adjust/iter_*/decision.json`，供前端轮询展示。

#### `preanalysis.py` 与 `preanalysis_parts/`

预分析入口和拆分模块。

负责：

- 解析 Unity/Laya shader 参数。
- 读取 `.lmat` 当前参数。
- 建立 Unity 参数与 Laya 参数的候选映射。
- 生成 Laya 控制 schema。
- 处理手工映射和预设。
- 可选调用 LLM 辅助语义分析。

预分析结果写到：

```text
material_fit/output/<project_id>/preanalysis.json
```

#### `preflight.py`

UI 里的 Laya 刷新探针。

当前探针流程是：

1. 通过 `laya.refresh_probe` 临时改写 `.lmat` 的探针颜色。
2. 调用 `laya_capture.editor_bridge.trigger_editor_single_view_capture()`。
3. Laya Editor 使用 `Capture Camera` 输出单张图。
4. 恢复 `.lmat`。
5. 再截图。
6. 对比变化和恢复程度。

结果写到：

```text
material_fit/output/<project_id>/preflight/
  baseline.png
  probe.png
  restored.png
  last.json
  laya_editor_selected_camera_report.json
```

#### `case_loader.py`

只读加载器，用于兼容历史 case。

它能识别：

- `project`：有 `project.json` 的新 UI 项目。
- `auto_adjust`：老式 `auto_adjust/` 产物。
- `probe`：只有 probe candidates 的旧产物。
- `diff_only`：只有单次 diff 的产物。
- `empty`：无法展示的空目录。

这也是为什么 UI 列表里可能能看到很多不一定是“当前项目”的目录。

#### `routers/`

HTTP API 分层。

```text
routers/cases.py        # case 列表、概览、迭代详情、报告
routers/projects.py     # project CRUD
routers/files.py        # 文件选择、预览、Unity reference 搜索
routers/preanalysis.py  # 预分析、手工映射、Laya 控制 schema
routers/preflight.py    # Laya 刷新探针
routers/jobs.py         # 启动/取消 job、读取日志和状态
routers/common.py       # 共享 LoaderConfig
```

### `frontend/`

Vue 3 + Vite + TypeScript 前端。

```text
frontend/package.json
frontend/vite.config.ts
frontend/src/main.ts
frontend/src/App.vue
frontend/src/api.ts
frontend/src/api/
frontend/src/types.ts
frontend/src/types/
frontend/src/components/
frontend/src/composables/
frontend/src/styles.css
```

#### `App.vue`

主壳组件。

它负责：

- 加载 case/project 列表。
- 记住当前选择。
- 切换视图。
- 轮询运行中的 job。
- 加载当前迭代详情。

主要视图包括：

```text
__overview__
__report__
__compare__
__project_config__
__preanalysis__
__algo_config__
__run__
__llm__
iter_0000 / iter_0001 / ...
```

#### `api.ts` 和 `src/api/`

前端访问后端的封装层。

主要 API：

- `/api/cases`
- `/api/cases/{case_id}/overview`
- `/api/cases/{case_id}/iterations`
- `/api/projects`
- `/api/projects/{project_id}/preanalyze`
- `/api/projects/{project_id}/preflight/laya_refresh`
- `/api/projects/{project_id}/jobs`
- `/api/jobs/{job_id}`
- `/api/jobs/{job_id}/log`

#### `components/`

当前主要组件：

```text
CaseSelector.vue               # case/project 选择
NewProjectWizard.vue           # 新建项目
ProjectConfigView.vue          # 输入路径和项目配置
PreanalysisView.vue            # 预分析与参数映射
LayaControlSchemaPanel.vue     # Laya 控制组/控制项
AlgoConfigView.vue             # 算法配置
RefreshPreflightCard.vue       # Laya 探针
JobRunnerPanel.vue             # 启动/停止 job
RunConsoleView.vue             # job 日志和状态
IterationList.vue              # 左侧迭代列表
IterationDetail.vue            # 单轮详情
IterationCompareView.vue       # 多视角对比
MultiviewImageGrid.vue         # 多视角图片网格
ImageComparison.vue            # 单视角图像对比
ScoreCurve.vue                 # 分数曲线
ReportView.vue                 # Markdown 报告
LlmAssistView.vue              # LLM 辅助占位/入口
```

#### `node_modules/`

前端依赖安装目录，不是源码。可以通过 `npm install` 重建。

## 当前主数据流

### 项目创建与配置

```text
Vue 前端
  -> FastAPI /api/projects
  -> project_store.py
  -> material_fit/output/<project_id>/project.json
```

### 预分析

```text
Vue 前端
  -> /api/projects/<id>/preanalyze
  -> backend/preanalysis.py
  -> import tools.material_fit.laya / unity / optimizer
  -> material_fit/output/<id>/preanalysis.json
```

### Laya 探针

```text
Vue 前端 RefreshPreflightCard
  -> /api/projects/<id>/preflight/laya_refresh
  -> backend/preflight.py
  -> material_fit.laya.refresh_probe
  -> material_fit.laya_capture.editor_bridge.trigger_editor_single_view_capture
  -> Laya 项目 assets/material_fit_capture_command.json
  -> Laya Editor assets/Editor/CameraCapture*.ts
  -> material_fit/output/<id>/preflight/
```

### 自动调参 job

```text
Vue 前端 JobRunnerPanel
  -> /api/projects/<id>/jobs
  -> backend/job_manager.py
  -> material_fit/output/<id>/runs/<run_id>/fit_config.json
  -> subprocess: python -m tools.material_fit.fit_material --auto-adjust
  -> 写 .lmat
  -> Laya Editor 多视角截图
  -> vision diff/score
  -> material_fit/output/<id>/runs/<run_id>/auto_adjust/iter_*/
  -> 前端轮询 job + iterations 展示
```

## 重要文件格式

### `project.json`

位置：

```text
material_fit/output/<project_id>/project.json
```

用途：

- UI 项目持久化。
- 保存用户输入路径。
- 保存算法配置。
- 保存当前/最近 job 和 run。
- 保存手工映射和 Laya 控制 schema。

这是 UI 项目的核心文件，不能轻易删除。

### `fit_config.json`

位置可能有两个：

```text
material_fit/output/<project_id>/fit_config.json
material_fit/output/<project_id>/runs/<run_id>/fit_config.json
```

项目根的 `fit_config.json` 是派生配置；run 目录下的 `fit_config.json` 是本次 job 的冻结配置，更适合用于复现实验。

### `material_fit_capture_command.json`

位置：

```text
<LayaProject>/assets/material_fit_capture_command.json
```

用途：

- Python 和 Laya Editor 扩展之间的命令文件。
- `nonce` 是触发 Laya 自动截图的关键字段。
- `capture_kind` 区分 `selected_camera` 和 `multiview`。
- `output_dir` 决定截图和报告写到哪里。

### `decision.json`

位置：

```text
material_fit/output/<project_id>/runs/<run_id>/auto_adjust/iter_0000/decision.json
```

用途：

- 记录本轮评分。
- 记录选中的调参 stage 或语义组。
- 记录参数变化。
- 记录候选 `.lmat` 路径。
- 记录截图和 diff 输入。

前端迭代列表和详情页高度依赖它。

## 输出目录详解

推荐的新结构：

```text
material_fit/output/<project_id>/
  project.json
  preanalysis.json
  inputs/
  jobs/
    job_*.json
    job_*.log
  preflight/
    baseline.png
    probe.png
    restored.png
    last.json
  runs/
    <run_id>/
      fit_config.json
      laya_shader_params.json
      laya_material_params.json
      initial_params.json
      stage_plan.json
      adjustment_policies.json
      auto_adjust/
        preflight.json
        state.json
        auto_adjust_result.json
        iter_0000/
          decision.json
          image_analysis/
          current/
            laya_multiview/
          candidate/
            params.json
            <material>.lmat
            laya_multiview/
      external_backups/
      report.md
  unity_reference/
```

当前仓库里还能看到一些手工测试目录，例如：

```text
material_fit/output/fish_1580/runs/manual_laya_editor_capture_v3/
material_fit/output/fish_1580/runs/agent_light_test_20260513_*/
```

这类目录通常是调试截图链路或验证问题时留下的实验产物，不是源码。

### `fish_1580` 当前目录状态说明

以当前 `material_fit/output/fish_1580/` 为例，存在三类容易混淆的迭代目录：

```text
material_fit/output/fish_1580/auto_adjust/
material_fit/output/fish_1580/iterations/
material_fit/output/fish_1580/runs/<run_id>/auto_adjust/
```

它们不是同一个阶段的产物：

- `auto_adjust/`：项目根级旧产物。当前查到有 331 个 `iter_0000` 到 `iter_0330`，约 92MB。它来自较早的自动调参流程或旧 CLI 输出。
- `iterations/`：旧 probe/dry-run 候选参数目录。当前查到有 330 个 `iter_*`，每个主要是 `params.json`，约 0.96MB。UI 的 legacy `probe` case 逻辑能读它，但新项目运行不应再往这里写。
- `runs/<run_id>/auto_adjust/`：当前推荐结构。每次 UI job 创建一个独立 `runs/<run_id>/`，并在里面写本次 run 的 `fit_config.json`、`auto_adjust/iter_*`、报告和备份。

当前 `fish_1580/project.json` 里：

```text
last_run_id = 20260513_093553-semantic-group-human-accept-mode-6d6e
active_run_id = null
```

因此 UI/后端读取这个 project 的迭代时，会优先使用：

```text
material_fit/output/fish_1580/runs/20260513_093553-semantic-group-human-accept-mode-6d6e/auto_adjust/
```

只有当 `project.json` 没有有效 `active_run_id/last_run_id`，或对应 run 目录不存在时，才会退回项目根级 `auto_adjust/` 或 `iterations/` 这类 legacy 目录。

当前 `fish_1580/runs/` 下还有一批 `manual_*` 和 `agent_light_test_*` 目录，这些是手工链路测试或本次排查截图差异时的实验产物，不是正式 job run。保留与否取决于是否还需要复现对应问题。

## 源码、运行产物、缓存的清理边界

### 明确属于源码或重要配置

不要删除：

```text
material_fit/*.py
material_fit/auto_adjust/
material_fit/laya/
material_fit/laya_capture/
material_fit/optimizer/
material_fit/shared/
material_fit/unity/*.py
material_fit/unity/*.cs
material_fit/unity/unity_shader/
material_fit/vision/
material_fit/tests/
material_fit/docs/
material_fit/fit_config.example.json

material_fit_ui/backend/
material_fit_ui/frontend/src/
material_fit_ui/frontend/package.json
material_fit_ui/frontend/package-lock.json
material_fit_ui/frontend/vite.config.ts
material_fit_ui/frontend/tsconfig*.json
material_fit_ui/requirements.txt
material_fit_ui/launch.py
material_fit_ui/launch.bat
```

### 明确属于可重建缓存

通常可以删除：

```text
**/__pycache__/
**/*.pyc
material_fit_ui/frontend/node_modules/
material_fit_ui/frontend/dist/
material_fit_ui/frontend/.vite/
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

删除 `node_modules/` 后需要在 `material_fit_ui/frontend/` 重新执行 `npm install`。

### 属于运行产物，删除前要确认

谨慎删除：

```text
material_fit/output/
```

其中大量 PNG、JSON、log、run 目录确实会让空间变乱，但这里也包含关键项目状态。

特别不要误删：

```text
material_fit/output/<project_id>/project.json
material_fit/output/<project_id>/preanalysis.json
material_fit/output/<project_id>/runs/<run_id>/fit_config.json
material_fit/output/<project_id>/runs/<run_id>/auto_adjust/iter_*/decision.json
material_fit/output/<project_id>/runs/<run_id>/external_backups/
```

如果只是想清理临时截图实验，优先清理命名明显的手工测试 run，例如：

```text
runs/manual_*/
runs/agent_light_test_*/
runs/*debug*/
```

但清理前仍建议确认这些 run 不再用于论文截图、问题复现或对比证据。

### 历史兼容模块

这些文件不一定是当前主链路，但还可能被测试、旧 case 或 fallback 引用，不建议在没有全局搜索和测试前删除：

```text
material_fit/vision/screen_capture.py
material_fit/laya/window_focus.py
material_fit/laya/render_driver.py
material_fit/laya_capture/capture_server.py
material_fit/vision/image_score.py
```

当前项目默认截图链路已经转向 Laya Editor 脚本截图，但这些旧模块还承担兼容和对照价值。

## 当前主截图链路

当前维护目标是：

- 探针：`Capture Camera` 单视角截图。
- 自动调参：按命令多视角截图，通常 8 个 yaw 视角。
- 截图不再依赖把 Laya 窗口唤到前台后截屏幕区域。

链路如下：

```text
Python editor_bridge.py
  写 material_fit_capture_command.json
  等待 report JSON

Laya Editor CameraCapture.ts
  轮询 command nonce
  reimport 材质
  可选 reload scene
  调用 CameraCaptureEnv

Laya Editor CameraCaptureEnv.ts
  找 Capture Camera
  找目标模型
  selected_camera 或 multiview
  RenderTexture 截图
  写 PNG + report JSON
```

目前已知问题：

- Laya Editor 脚本截图能拿到 `DirectionLight`，但离屏截图仍比 Laya 预览/运行时偏暗。
- 这更像是离屏 RenderTexture 颜色空间、gamma/sRGB 或后处理链路差异，不是“灯没有进场景”。

## UI 页面与后端模块对应关系

```text
NewProjectWizard.vue
  -> routers/projects.py
  -> project_store.py

ProjectConfigView.vue
  -> routers/projects.py, routers/files.py
  -> project.json

PreanalysisView.vue / LayaControlSchemaPanel.vue
  -> routers/preanalysis.py
  -> preanalysis.py
  -> preanalysis.json / project.json

RefreshPreflightCard.vue
  -> routers/preflight.py
  -> preflight.py
  -> preflight/

AlgoConfigView.vue
  -> routers/projects.py
  -> project.json algorithm_config

JobRunnerPanel.vue / RunConsoleView.vue
  -> routers/jobs.py
  -> job_manager.py
  -> jobs/ and runs/

IterationList.vue / IterationDetail.vue / IterationCompareView.vue
  -> routers/cases.py
  -> case_loader.py
  -> auto_adjust/iter_*/
```

## 推荐阅读顺序

如果需要继续维护代码，建议按以下顺序读：

1. `material_fit/docs/Project_Architecture.md`
2. `material_fit/docs/File_Layout_And_Artifacts.md`
3. `material_fit_ui/backend/project_store.py`
4. `material_fit_ui/backend/job_manager.py`
5. `material_fit/laya_capture/editor_bridge.py`
6. `tools/Editor/CameraCapture.ts`
7. `tools/Editor/CameraCaptureEnv.ts`
8. `material_fit/fit_material.py`
9. `material_fit/auto_adjust/scoring.py`
10. `material_fit/vision/diff_analysis.py`

## 后续整理建议

当前文件“多且杂”的主要原因不是源码结构混乱，而是运行产物、手工测试截图、历史兼容代码和当前源码混在视野里。

建议分三步整理：

1. 先不要动源码，只清理确定可重建的缓存：`__pycache__`、`*.pyc`、`node_modules/.vite`、`frontend/dist`。
2. 对 `material_fit/output/<project_id>/runs/` 按 run 名称筛选，删除明确的临时测试 run，保留正式实验 run。
3. 等当前 Laya Editor 截图链路稳定后，再决定是否移除旧桌面截图模块和相关测试。

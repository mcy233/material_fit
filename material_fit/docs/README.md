# Material Fit 文档索引

本文档用于作为后续修改时的入口索引，避免在多个阶段总结和实验文档之间反复搜索。

## 评分机制

- [Scoring_Mechanism_Design.md](Scoring_Mechanism_Design.md)：当前评分标准、`human_accept_score` 设计与变更记录。
- [Metric_Validation.md](Metric_Validation.md)：历史指标验证与背景/前景 mask 实验。

## 优化算法

- [Optimizer_New_Directions_2026_05_08.md](Optimizer_New_Directions_2026_05_08.md)：最新发散方案、有效边界、人工样本启发。
- [Optimization_Algorithm_Redesign.md](Optimization_Algorithm_Redesign.md)：优化器重构设计。
- [Optimizer_Current_State_And_Next_Plan.md](Optimizer_Current_State_And_Next_Plan.md)：阶段性现状和下一步路线。
- [Phase_Summary_2026_05_08.md](Phase_Summary_2026_05_08.md)：一次完整阶段总结。

## FishStandard 与控件体系

- [FishStandard_Shader_Grouping_Review.md](FishStandard_Shader_Grouping_Review.md)：FishStandard 参数分组与审核。
- [Editable_Laya_Control_Schema_Design.md](Editable_Laya_Control_Schema_Design.md)：可编辑 Laya 控件 schema 设计。

## 工具和实验

- [File_Layout_And_Artifacts.md](File_Layout_And_Artifacts.md)：测试产物、run 目录、`.lmat` 备份与输出文件管理约定。
- [Laya_Multiview_Capture.md](Laya_Multiview_Capture.md)：Laya 相机离屏多视角截图脚本与本地接收服务。
- [MaterialAutoFitTool.md](MaterialAutoFitTool.md)：工具总体说明。
- [ExperimentLog.md](ExperimentLog.md)：实验编号、修复记录和关键决策。
- [Experiment_Phase1_CMA_ES_WarmStart.md](Experiment_Phase1_CMA_ES_WarmStart.md)：CMA-ES warm start 实验。

## 当前代码入口

- `material_fit/fit_material.py`：CLI 和高层编排入口。
- `material_fit/auto_adjust/scoring.py`：自动调参 headline score 选择与迭代诊断摘取。
- `material_fit/auto_adjust/image_pairs.py`：自动调参图对收集。
- `material_fit/auto_adjust/history.py`：warm-start 历史样本读取。
- `material_fit/auto_adjust/loop.py`：自动调参 loop 目标边界与 helper re-export。
- `material_fit/vision/diff_analysis.py`：图像差异报告装配入口。
- `material_fit/vision/human_accept_score.py`：`human_accept_score` v1/v2 分量计算。
- `material_fit/optimizer/effective_bounds.py`：FishStandard 有效视觉搜索边界。
- `material_fit/optimizer/{base,factory,heuristic_strategy,cma_strategy,semantic_group_strategy}.py`：优化策略分层入口。
- `material_fit_ui/backend/main.py`：FastAPI app 初始化。
- `material_fit_ui/backend/routers/`：后端路由拆分入口。
- `material_fit_ui/backend/preanalysis.py`：预分析兼容 facade。
- `material_fit_ui/backend/preanalysis_parts/`：预分析拆分出的支持模块。
- `material_fit_ui/frontend/src/api/`：前端 API 按 cases/projects/files/preanalysis/jobs 分区。
- `material_fit_ui/frontend/src/types/`：前端类型按 case/project/job/preanalysis/scoring 分区。
- `material_fit_ui/frontend/src/components/RunModePicker.vue`：运行模式选择控件。
- `material_fit_ui/frontend/src/components/{JobRunnerPanel,LayaControlSchemaPanel}.vue`：运行控制台拆分目标组件。
- `material_fit_ui/frontend/src/composables/`：运行控制台可复用状态逻辑。

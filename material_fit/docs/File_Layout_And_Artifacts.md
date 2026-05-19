# 文件框架与测试产物管理

本文档记录材质拟合工具的文件落点约定，目标是尽量减少对原 Unity/Laya 项目的侵入，并避免多次测试互相覆盖。

## 总原则

- 原 Unity 项目只放必须由 Unity Editor 编译/执行的脚本，例如 `Assets/Editor/unity_multiview_capture.cs`。
- 原 Laya 项目只写必须实时生效的目标 `.lmat` 文件。
- 自动调参产生的截图、候选参数、候选 `.lmat`、报告、备份文件都应写在本仓库 `tools/material_fit/output/` 下。
- 每次 UI job 使用独立 run 目录，历史实验默认保留，不再覆盖上一轮 `auto_adjust/iter_*`。

## UI 项目目录

```text
tools/material_fit/output/<project_id>/
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
    20260511_153012-semantic-group-human-accept-fresh-fit-a1b2/
      fit_config.json
      laya_shader_params.json
      laya_material_params.json
      initial_params.json
      stage_plan.json
      adjustment_policies.json
      auto_adjust/
        preflight.json
        preflight_captures/
        state.json
        auto_adjust_result.json
        iter_0000/
          decision.json
          image_analysis/
          current/
            laya_multiview/
          candidate/
            params.json
            1580_body.lmat
            laya_multiview/
      captures/
        laya_candidate_00.png
        .capture_region.json
      external_backups/
        1580_body.lmat.auto_adjust_0000.bak
        1580_body.lmat.refresh_probe.bak
      report.md
```

`runs/<run_id>/` 的命名格式是：

```text
YYYYMMDD_HHMMSS-<optimizer>-<fit_score_mode>-<auto_adjust_mode>-<short_random>
```

例如：

```text
20260511_153012-semantic-group-human-accept-fresh-fit-a1b2
```

## Laya `.lmat` 备份

以前自动调参会在原 Laya 材质同级目录生成：

```text
1580_body.lmat.auto_adjust_0000.bak
1580_body.lmat.refresh_probe.bak
```

现在这些备份会写到当前 run 的：

```text
runs/<run_id>/external_backups/
```

真实 `.lmat` 仍会被覆盖，因为 Laya 必须读取该文件才能实时刷新材质。但所有备份和诊断产物都不再堆在原 Laya 资源目录。

## 截图与迭代产物

每个 run 拥有自己的 `captures/`，因此候选截图不会跨 run 复用或覆盖。UI 仍默认显示当前项目的 `active_run_id`，如果没有运行中的任务则显示 `last_run_id`。

旧的项目根级 `auto_adjust/` 目录仍可被 legacy CLI 产物读取，但 UI 新启动的 job 会写入 `runs/<run_id>/auto_adjust/`。

## Laya Editor 多视角截图

启用 `laya_editor_capture.enabled` 后，每轮迭代会把多视角截图写到该迭代目录下，而不是写到手工测试目录：

```text
runs/<run_id>/auto_adjust/iter_0000/current/laya_multiview/
runs/<run_id>/auto_adjust/iter_0000/candidate/laya_multiview/
```

- `current/laya_multiview/`：本轮评分前的当前材质截图，通常只在第一轮需要。
- `candidate/laya_multiview/`：本轮写入候选 `.lmat`、触发 Laya `reimport` 后的截图，用作下一轮评分候选图。
- `laya_editor_multiview_report.json`：Laya 扩展生成的完成信号，Python 等到它出现后再继续评分。

# Laya 多视角离屏截图方案

目标：让 Laya 端像 Unity Editor 截图脚本一样，由指定相机直接渲染出多视角 PNG，而不是依赖桌面区域截图或 MCP 的运行视图截图。

优先使用 `Editor/CameraCaptureEnv.ts` 的编辑器扩展方案；它不需要启动预览，能直接在 Laya 编辑器场景环境里刷新并截图。`material_fit/laya_capture/laya/MaterialFitCapture.ts` 是运行时备用方案。

## 文件

- `material_fit/laya_capture/laya/MaterialFitCapture.ts`  
  放到 Laya 项目的 `src/` 下，并挂到测试场景中的任意 3D 节点或目标相机上。

- `material_fit/laya_capture/capture_server.py`  
  本项目内的本地 HTTP 服务，负责下发截图命令并接收 Laya POST 回来的 PNG。

- `material_fit/laya_capture/editor_command.py`  
  生成编辑器扩展读取的 `material_fit_capture_command.json`。

- `Editor/CameraCapture.ts` / `Editor/CameraCaptureEnv.ts`  
  Laya 编辑器扩展。菜单 `App/tools/按命令多视角截图` 会读取命令 JSON，在非运行时完成多视角截图。

## 推荐：编辑器非运行时截图

先生成命令文件：

```powershell
python -m material_fit.laya_capture.editor_command `
  --laya-project "D:/project_data/laya/laya_research/laya_project" `
  --unity-metadata "D:/project_data/laya/laya_research/tools/material_fit/unity/test __________/unity_ref_multiview_metadata.json" `
  --output-dir "D:/project_data/laya/laya_research/tools/material_fit/output/fish_1580/runs/manual_laya_editor_capture/laya_multiview" `
  --camera-name "Capture Camera" `
  --target-name "1580_lvbu_skin_prefab" `
  --width 900 `
  --height 700 `
  --capture-mode auto `
  --fov 35 `
  --refresh-delay-ms 80
```

这会在 Laya 工程根目录写入：

```text
<LayaProject>/material_fit_capture_command.json
```

然后在 Laya 编辑器菜单中执行：

```text
App/tools/按命令多视角截图
```

编辑器扩展会：

- 读取 `material_fit_capture_command.json`
- 如果命令里有 `refresh_assets`，UI 扩展会先调用 `Editor.assetDb.reimport(...)`，再可选重载当前场景
- 在 `EditorEnv.scene.scene3D` 中查找 `camera_name` 和 `target_name`
- 每个角度先用正常材质黑底渲染 RGB，再用纯白 `UnlitMaterial` 临时替换目标材质渲染 silhouette mask
- 将 mask 写入 RGB 图的 alpha 通道，再用编辑器 Node 环境的 `sharp` 保存透明 PNG
- 在输出目录生成 `laya_editor_multiview_report.json`

如果目标模型挂在 `Capture Camera` 下，`capture-mode auto` 会自动使用模型旋转模式，保持相机和模型相对位置不变。

## 自动调参闭环

自动调参不再依赖 Laya 窗口前台刷新。每轮迭代流程是：

```text
Python 写入候选参数到目标 .lmat
Python 更新 material_fit_capture_command.json 的 nonce / output_dir / refresh_assets
Laya UI 扩展轮询到新 nonce
Laya UI 扩展 reimport refresh_assets，并按需 reload 当前场景
Laya Scene 扩展执行现有多视角离屏截图
Python 等待 laya_editor_multiview_report.json，读取 8 张候选图评分
```

`material_fit_capture_command.json` 需要包含：

```json
{
  "auto_capture": true,
  "nonce": "每轮唯一任务编号",
  "refresh_assets": [
    "resources/play/fish/1580/mat/1580_body.lmat"
  ],
  "reload_scene_after_reimport": true,
  "refresh_after_reimport_delay_ms": 800,
  "alpha_source": "silhouette_mask",
  "render_texture_srgb": true
}
```

`fit_config.json` 中对应的自动闭环配置：

```json
{
  "laya_editor_capture": {
    "enabled": true,
    "laya_project": "D:/project_data/laya/laya_research/laya_project",
    "reference_dir": "D:/project_data/laya/laya_research/tools/material_fit/unity/test __________/unity_ref_inverse_yaw_renamed_current",
    "reference_glob": "unity_ref_v*_yaw*_pitch*.png",
    "refresh_assets": [
      "resources/play/fish/1580/mat/1580_body.lmat"
    ],
    "reload_scene_after_reimport": true,
    "refresh_after_reimport_delay_ms": 800,
    "timeout_s": 90
  }
}
```

自动运行时，每轮截图会落在：

```text
runs/<run_id>/auto_adjust/iter_0000/candidate/laya_multiview/
```

## 原理

Laya 脚本使用以下 API：

- `Camera.renderTarget`
- `RenderTexture`
- `RenderTexture.getData()` / `getDataAsync()`
- `Transform3D.lookAt()`

流程：

```text
Python server 生成 command
Laya 脚本轮询 /material-fit/capture-command
绑定指定 Camera/Target
每个 view 设置 yaw/pitch；默认自动选择相机环绕或模型旋转
Camera 渲染到 RenderTexture
RenderTexture 读回像素
Canvas 编码 PNG
POST /material-fit/capture-result
Python 保存 PNG
```

编辑器扩展方案的核心流程不同：

```text
Python 写入 material_fit_capture_command.json
Laya 编辑器菜单触发 CameraCaptureEnv.captureMultiviewFromCommand
EditorEnv.d3Manager.refresh()
Laya.Camera.drawRenderTextureByScene(camera, EditorEnv.scene.scene3D, tempRT)
RenderTexture 读回像素
sharp 保存 PNG
```

## Laya 项目接入

1. 将脚本复制到 Laya 项目：

```text
<LayaProject>/src/MaterialFit/MaterialFitCapture.ts
```

2. 在测试场景中挂载 `MaterialFitCapture` 组件。

建议挂到截图相机上；如果挂到其它节点，需要在组件属性里填写 `Default Camera Name`。

目标模型可以和相机同级，也可以放在截图相机下面：

```text
Scene3D
└─ Capture Camera
   └─ 1580_lvbu_skin_prefab
```

当目标是相机子节点时，脚本会自动使用 `rotate_target` 模式：保持相机和模型的相对位置不变，只临时旋转模型根节点来输出多视角；拍完后恢复模型原始局部旋转。

3. 组件属性：

- `Server Base URL`: 默认 `http://127.0.0.1:8787`
- `Default Camera Name`: 截图相机节点名，可由 Python 命令覆盖
- `Default Target Name`: 目标模型 root 节点名，可由 Python 命令覆盖
- `Auto Poll`: 默认开启

## 启动本地服务

示例：

```powershell
python -m material_fit.laya_capture.capture_server `
  --unity-metadata "D:/project_data/laya/laya_research/tools/material_fit/unity/test __________/unity_ref_multiview_metadata.json" `
  --output-dir "D:/project_data/laya/laya_research/tools/material_fit/output/fish_1580/runs/manual_laya_capture/laya_multiview" `
  --camera-name "MaterialFitCamera" `
  --target-name "1580_lvbu_skin_prefab" `
  --width 900 `
  --height 700 `
  --capture-mode auto `
  --distance-scale 2.2 `
  --fov 35
```

然后通过 MCP 或手动启动 Laya 预览。脚本会自动拉取命令并保存图片。

### 运行时透明底 + 材质实时 patch

运行时方案现在支持在截图前直接修改目标模型材质实例，而不是依赖运行中重写 `.lmat` 自动刷新。命令里可以带：

```json
{
  "transparent_background": true,
  "zero_transparent_rgb": true,
  "alpha_from_rgb": true,
  "alpha_source": "silhouette_mask",
  "mask_alpha_mode": "binary",
  "flip_y": false,
  "render_texture_srgb": true,
  "material_patch": {
    "target_name": "model",
    "values": {
      "u_Color": [1.0, 0.0, 1.0, 1.0],
      "u_SpecularIntensity": 0.8
    }
  }
}
```

运行时脚本会：

- 找到 `target_name` 下所有 `MeshRenderer / SkinnedMeshRenderer` 的材质；
- 对 Inspector 暴露的数值、布尔、Color、Vector2/3/4 参数调用 `material.setFloat / setBool / setColor / setVector*`；
- 临时把截图相机 `clearColor` 设为 `alpha = 0`，渲染到 RGBA `RenderTexture`；
- 默认使用 sRGB `RenderTexture`，更接近运行时屏幕输出；如需排查线性 RT，可用 `--linear-render-texture`；
- 默认不做 Y 翻转；如遇到目标平台读回方向相反，可用 `--flip-y`；
- 默认使用 `alpha_source = "silhouette_mask"`：先黑底正常渲染得到预览 RGB，再临时把目标模型材质替换成纯白 `UnlitMaterial` 渲染 mask，最后只用二值 mask 去掉背景；
- 默认 `mask_alpha_mode = "binary"`：mask 命中的模型区域写成 `alpha=255`，未命中的背景写成 `alpha=0`，不会把 mask 灰度作为模型透明度；
- `alpha_source = "alpha_from_rgb"` 仍保留为诊断/回退模式，用于把 `alpha=0` 但 RGB 非零的像素转成可见透明像素；如需关闭可用 `--no-alpha-from-rgb`；
- 读回像素并保存 PNG，`alpha = 0` 的像素默认清空 RGB，避免透明边缘脏色；
- 截图完成后恢复相机原本的 `renderTarget / fieldOfView / clearColor` 和目标模型旋转。

`capture_server.py` 可直接下发 patch：

```powershell
python -m tools.material_fit.laya_capture.capture_server `
  --command-json "D:/project_data/laya/laya_research/tools/material_fit/output/fish_1504/runtime_capture_test/runtime_command_8views.json" `
  --material-patch-json "D:/project_data/laya/laya_research/tools/material_fit/output/fish_1504/runtime_capture_test/material_patch_magenta.json" `
  --output-dir "D:/project_data/laya/laya_research/tools/material_fit/output/fish_1504/runtime_capture_test/out" `
  --timeout-sec 120
```

然后启动 Laya 运行预览。挂在场景中的 `MaterialFitCapture` 会轮询 `http://127.0.0.1:8787/material-fit/capture-command`，执行运行时材质 patch 和多视角透明底截图，并把 PNG 回传给 Python server。

## 参数对齐

Unity metadata 提供 `yaw`/`pitch`/`fieldOfView`/分辨率。Laya 坐标轴如不一致，可以用：

```powershell
--yaw-offset 180
--pitch-offset 0
```

如果目标挂在相机下面，`auto` 会自动启用模型旋转。默认模型旋转使用与相机环绕相反的 yaw/pitch 符号；如果输出方向左右/上下反了，可以调整：

```powershell
--target-yaw-sign 1
--target-pitch-sign 1
```

如果自动包围盒不准确，可手动指定：

```powershell
--center "0,1.2,0"
--target-size "6.7,8.0,4.2"
```

## 当前边界

- 这是运行时离屏渲染，不使用 MCP 的 `RuntimeManagement.screenshot`。
- 如果预览态不能热更新外部 `.lmat` 文件，后续需要把“当前候选参数”也通过 command 传给脚本，由脚本直接设置运行时材质。
- 第一版先验证相机多视角与 PNG 回传；材质参数实时注入可作为下一步接入。

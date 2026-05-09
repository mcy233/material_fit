# 材质拟合评分机制设计

> 状态：评分机制设计与维护文档。  
> 创建时间：2026-05-09。  
> 目的：记录 Laya/Unity 跨引擎材质拟合中的评分标准、设计理由、已知问题、后续修改方向。后续任何评分函数、权重、阈值、mask、区域划分或人工校准规则发生变化，都应继续追加到本文。  
> 论文用途：本文可作为后续论文中“评价指标 / Scoring Metric / Human-aligned Evaluation”章节的基础材料。

---

## 1. 评分目标

本项目的目标不是追求逐像素完全一致，而是在同一个 Laya shader（当前重点是 `FishStandard.shader`）内，通过调整 `.lmat` 参数，使 Laya 渲染结果在视觉上尽可能接近 Unity 参考图。

因此评分机制需要同时满足两类用途：

1. **优化驱动**：给自动调参算法一个稳定、可比较、可上升的目标。
2. **人类解释**：让工程师知道差异来自哪里，例如基础色、暗部、高光、发光、Fresnel、背景、姿态或截图对齐。

关键原则：

> 材质参数无法修正的误差，不应强烈惩罚优化器。

例如相机姿态、截图视角、模型轮廓轻微偏移、武器位置、骨骼姿态差异、高光落点轻微偏移，并不一定能靠 `.lmat` 参数修正。如果评分函数把这些误差当作主要损失，优化器会被误导。

---

## 2. 当前已实现评分

当前实现主要位于：

- `material_fit/vision/diff_analysis.py`
- `material_fit/vision/perceptual_score.py`
- 设计记录：`material_fit/docs/Metric_Validation.md`

当前评分的核心输出包括：

- `score`：前景区域的 RGB MAE，越低越好。
- `perceptual_fit_score`：当前主 fit 分数，越高越好。
- `material_channels`：按材质语义拆分的通道诊断。
- `auto_mask`：背景识别和前景比例。
- `bg_normalize`：背景归一化信息。

当前 `perceptual_fit_score` 近似为：

```text
weighted_mae = channel_weighted_mae(material_channels)
mae_branch = exp(-4 * weighted_mae)
ssim_branch = SSIM(reference, candidate)
perceptual_fit_score = 0.7 * mae_branch + 0.3 * ssim_branch
```

其中 `weighted_mae` 使用材质通道权重：

| 通道 | 当前含义 |
| --- | --- |
| `base_color_main_texture` | 主体中间调 / 基础色 |
| `metallic_smoothness_specular` | 高光 / 金属 / 光滑度 |
| `environment_reflection_matcap` | 环境反射 / Matcap |
| `fresnel_rim` | 边缘光 / Fresnel |
| `emission` | 自发光 |
| `shadow_occlusion` | 暗部 / 遮蔽 |
| `color_grading_hsv_contrast` | 全局色彩与对比 |

---

## 3. 当前评分暴露的问题

### 3.1 对姿态和截图视角过于敏感

即使 Unity 与 Laya 的材质观感接近，如果两者存在轻微姿态、相机、缩放或轮廓偏移，逐像素 MAE 和 SSIM 都会明显下降。

人类会把这种情况理解为“模型位置稍有不同，但材质差不多”；但逐像素算法会认为大量像素不匹配。

这会造成两个后果：

- 人工可接受图的分数偏低。
- 优化器试图修正无法由材质参数修正的误差。

### 3.2 小亮部差异被强惩罚

高光、发光、Fresnel 区域面积很小，但对当前评分权重较高。这样做有合理性，因为这些区域确实影响材质观感；但如果亮点位置有偏移，当前区域划分仍按像素位置比较，就会产生较大惩罚。

例如人工可接受样本中，主机体整体观感接近，但高光/发光区域与 Unity 参考的位置和强度不同，导致：

```text
highlight_specular_reflection rgb_mae ≈ 0.545
emission rgb_mae ≈ 0.627
```

这些局部分量会显著拉低最终分数。

### 3.3 SSIM 不适合作为强权重主目标

SSIM 对结构位置、局部纹理、轮廓和亮斑分布敏感。对同一渲染截图的 1-2 像素偏移，它有一定鲁棒性；但对 Unity/Laya 之间的相机视角、姿态、骨骼或截图裁剪差异，它仍然会给出很低分。

因此，SSIM 更适合作为诊断项或低权重结构参考，不应主导优化。

### 3.4 当前目标分数不可直接等同于人类接受度

用户提供的人类工程师可接受样本，使用当前配置中的 Unity reference 对比后得到：

```text
perceptual_fit_score ≈ 0.202
foreground RGB MAE ≈ 0.216
```

如果该参考图与候选图确实对应同一案例，这说明：

- 当前 `0.9` target score 不适合作为真实闭环硬目标。
- 人类“可接受”的结果在当前 strict score 下可能只有 `0.20-0.30`。
- 评分机制需要增加“人类可接受分”，而不是只依赖严格像素分。

---

## 4. 新评分机制方向

建议把评分拆成两套：

### 4.1 Strict Pixel Score

严格像素分保留，用于诊断和回归测试。

用途：

- 检查背景 mask 是否正常。
- 检查截图是否错位。
- 检查某次参数是否导致明显画面破坏。
- 对完全对齐的测试样本做精确比较。

不建议作为唯一优化目标。

### 4.2 Human Acceptability Score

人类可接受分用于自动调参主目标。它应对姿态、截图、局部高光位置差异更宽容，更关注材质整体观感。

建议第一版结构：

```text
human_accept_score =
  0.35 * foreground_color_distribution_score
+ 0.25 * material_channel_statistics_score
+ 0.15 * relaxed_structure_score
+ 0.15 * perceptual_feature_score
+ 0.10 * strict_pixel_score
```

其中：

- `foreground_color_distribution_score`：比较前景颜色、亮度、饱和度的分布，而不是逐像素对应。
- `material_channel_statistics_score`：比较高光、发光、边缘光、暗部等通道的覆盖率、强度和颜色趋势。
- `relaxed_structure_score`：低频结构相似度，允许小位移和轻微姿态变化。
- `perceptual_feature_score`：可选 LPIPS/DISTS 或轻量图像特征距离。
- `strict_pixel_score`：保留少量严格像素约束，防止完全跑偏。

---

## 5. 关键设计

### 5.1 前景对齐

在评分前，先通过 foreground mask 的 bounding box 做粗对齐：

```text
reference foreground bbox
candidate foreground bbox
  -> estimate scale / translation
  -> align candidate to reference canvas
  -> score aligned foreground
```

这一步用于抵消截图裁剪、模型位置和缩放差异。它不追求精确配准，只做粗粒度归一化。

### 5.2 轮廓宽容区

对前景 mask 边缘生成 `ignore_band`，例如 3-8 像素：

```text
foreground_core = erode(foreground_mask, radius=3)
foreground_band = foreground_mask - foreground_core
```

严格像素 MAE 主要在 `foreground_core` 上计算；轮廓带只用于诊断或低权重评分。

原因：姿态和视角微差最容易出现在轮廓边缘，但这些区域通常不是材质参数能修正的。

### 5.3 颜色分布评分

对前景或前景核心区域计算：

- OKLab / Lab 均值差。
- OKLab / Lab 方差差。
- Luma 直方图距离。
- Saturation 直方图距离。
- Hue 直方图距离。

这些指标比 RGB 逐像素 MAE 更接近人类对“整体颜色是否像”的判断。

### 5.4 高光 / 发光 / Fresnel 统计

对高光、发光、边缘光不要求像素位置完全一致，而比较统计特征：

- 覆盖率：亮区面积是否接近。
- 强度：亮区平均亮度 / P95 是否接近。
- 色相：亮区主色是否接近。
- 分布：是否落在前景、边缘或局部热点区域。

这样可以避免“高光点偏了几个像素就重罚”的问题。

### 5.5 低频结构相似

将图像降采样或模糊后再计算结构差异：

```text
blurred_ref = gaussian_blur(ref, sigma=3)
blurred_cand = gaussian_blur(cand, sigma=3)
low_freq_ssim = SSIM(blurred_ref, blurred_cand)
```

低频结构用于确认整体明暗块面和材质层次，不用于要求轮廓和亮点严格对齐。

### 5.6 人工样本校准

人工可接受样本应进入校准集：

```text
expert_sample:
  reference: unity_reference.png
  candidate: engineer_adjusted.png
  human_label: acceptable
  strict_score: 0.202
  expected_human_accept_score: 0.65-0.75
```

设计目标是：如果工程师认为结果可接受，则 `human_accept_score` 应进入可接受区间，而不是停留在极低分。

---

## 6. 推荐分数解释

第一版可以采用以下解释区间：

| `human_accept_score` | 解释 |
| ---: | --- |
| `0.85-1.00` | 非常接近，适合严格验收 |
| `0.70-0.85` | 人类通常认为接近 |
| `0.55-0.70` | 大致可接受，但明显有差异 |
| `0.35-0.55` | 风格方向接近，但仍需调参 |
| `<0.35` | 明显不匹配 |

这些阈值必须由人工样本继续校准，不能作为固定真理。

---

## 7. 优化器如何使用评分

优化器不应只看一个总分。建议每轮输出：

```json
{
  "strict_pixel_score": 0.20,
  "human_accept_score": 0.68,
  "foreground_color_score": 0.72,
  "material_channel_statistics_score": 0.63,
  "relaxed_structure_score": 0.70,
  "perceptual_feature_score": 0.66,
  "strict_diagnostics": {
    "rgb_mae": 0.216,
    "ssim": 0.047
  }
}
```

接受/拒绝逻辑应改成：

- 主目标：`human_accept_score` 上升。
- 阶段目标：当前调参组对应的子分量上升。
- 护栏：其他关键分量不能大幅下降。
- 严格像素分：只作为回归诊断，低权重参与。

---

## 8. 与论文表达的关系

论文中可以把评分机制描述为两层：

1. **Diagnostic Pixel Metric**：用于分析和可重复实验，包含 foreground mask、channel-weighted MAE、SSIM。
2. **Human-aligned Acceptance Metric**：用于实际优化目标，通过前景分布、材质通道统计、低频结构和人工样本校准，降低对不可控姿态差异的敏感性。

论文论点可以是：

> Cross-engine material fitting should not optimise pure pixel error, because camera, pose and renderer-level discrepancies are not controllable by material parameters. A human-aligned metric should emphasise foreground material appearance and discount non-material spatial mismatch.

---

## 9. 待实现项

优先级建议：

1. [已实现 v1] 新增 `human_accept_score` 数据结构，不替换旧分数。
2. [已实现 v2 初版] 实现 foreground bbox 粗对齐诊断。
3. [已实现 v2 初版] 实现轮廓 ignore band 估计。
4. [已实现 v1] 实现前景颜色/亮度/饱和度分布评分；当前先用 mask 后全局统计，未做空间重采样。
5. [已实现 v1] 实现高光/发光/Fresnel 等材质通道统计；当前复用 `material_channels` 的 channel MAE。
6. [已实现 v1] 降低原 SSIM 权重；当前用 `relaxed_structure = 0.60 + 0.40 * SSIM`，只作为 15% 分量。
7. [待实现 v2] 建立人工样本校准集。
8. [已实现 v1] 在 iteration detail 中同时显示 strict/perceptual 与 human score；Run Console 摘要仍显示当前优化用的 `fit_score`。

---

## 10. 变更记录

### 2026-05-09：落地 `human_accept_score` v2 初版

实现内容：

- 从 `diff_analysis.py` 抽出 `vision/human_accept_score.py`，让评分机制独立维护。
- 新增 `build_foreground_alignment`，基于 reference/candidate 的背景色检测前景 bbox，输出中心偏移、尺度差异和 `alignment_score`。
- 新增轮廓 ignore band 估计，输出 `ignore_band_pixels` 与 `ignore_band_ratio_of_foreground`，用于解释姿态/轮廓造成的像素误差。
- `human_accept_score` 从 v1 的 5 分量扩展为 6 分量，加入 `foreground_bbox_alignment`。

当前公式：

```text
human_accept_score =
  0.32 * foreground_color_distribution
+ 0.24 * material_channel_statistics
+ 0.14 * relaxed_structure
+ 0.15 * material_feature_statistics
+ 0.05 * foreground_bbox_alignment
+ 0.10 * strict_pixel_guardrail
```

当前限制：

- v2 初版暂不对 candidate 图像做真实重采样，只记录粗对齐误差并作为低权重分量。
- ignore band 当前用于诊断解释，尚未回写到 weighted MAE 的像素权重里。
- 后续若测试证明姿态/视角差异仍强烈干扰分数，再把 ignore band 接入 strict/perceptual 分支的 mask 权重。

### 2026-05-09：落地 `human_accept_score` v1

实现内容：

- `analyze_image_diff` 新增顶层 `human_accept_score` 与 `human_accept` 分解块。
- `fit_material` 新增 `fit_score_mode = human_accept`，并作为默认优化目标；`perceptual` 和 `linear` 保留为诊断/对照。
- `human_accept_score` v1 由 5 个分量组成：前景颜色分布、材质通道统计、宽松结构、材质特征统计、严格像素 guardrail。
- `IterationDetail` 展示 human score 及其分量，方便判断自动调参到底改善了哪类视觉差异。

公式：

```text
human_accept_score =
  0.35 * foreground_color_distribution
+ 0.25 * material_channel_statistics
+ 0.15 * relaxed_structure
+ 0.15 * material_feature_statistics
+ 0.10 * strict_pixel_guardrail
```

当前限制：

- 尚未实现 foreground bbox 粗对齐与轮廓 ignore band。
- 尚未引入 LPIPS/DISTS 或人工偏好校准集。
- 当前 v1 主要解决“像素过严导致优化无梯度/分数解释困难”的问题，后续 v2 再处理姿态/轮廓的空间对齐问题。

### 2026-05-09：创建评分机制文档

背景：

- 人工工程师调出的 Laya FishStandard 样本在肉眼上可接受，但当前 `perceptual_fit_score` 只有约 `0.202`。
- 分析发现低分主要来自高光/发光局部差异、SSIM 对姿态/视角敏感、以及逐像素区域比较过于严格。

结论：

- 保留 strict pixel score 作为诊断。
- 新增 human acceptability score 作为未来优化主目标。
- 后续评分机制修改必须继续追加到本文。

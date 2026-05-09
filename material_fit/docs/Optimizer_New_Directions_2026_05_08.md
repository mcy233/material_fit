# 材质拟合新方向发散方案

> 状态：发散设计文档。  
> 时间：2026-05-08。  
> 背景：最新自动调参实验的分数仍然偏低。本文刻意跳出“继续微调 `semantic_group` 坐标搜索”的思路，从评分、参数可控性、搜索策略、人类反馈、渲染域可达性五个层面重新展开方案。当前工程目标明确限定为：**在既有 Laya FishStandard shader 上尽可能拟合 Unity 参考效果**，优先追求可接受的视觉近似和更高分数，而不是追求逐像素完全一致。
> 文档索引：评分、优化、FishStandard 和 UI 数据流入口见 [README.md](README.md)。

---

## 1. 重新定义问题

当前低分不应该直接等价为“优化器不够强”。更合理的判断是：这个系统同时存在五类可能失败源，任何一类没有被验证清楚，都会让后续算法显得无效。

| 层级 | 可能失败 | 典型现象 | 应优先验证的问题 |
| --- | --- | --- | --- |
| 评分信号 | `fit_score` 没有奖励肉眼相似 | 肉眼略有变化但分数不动，或分数提升但画面更怪 | 分数拆分后，前景、颜色、结构、高光是否同向改善 |
| 参数可控性 | Laya 参数对当前截图没有可测影响 | 大幅改参数后截图几乎不变 | 这个参数是否被 gate/define/贴图/视角遮蔽 |
| 参数边界 | 优化器搜索范围不是视觉有效搜索区间 | 人工能调到的效果，算法永远搜不到，或数字继续变大但画面只变极端/饱和 | 每个参数的有效区间、饱和区间和危险区间分别在哪里 |
| 搜索算法 | 在错误空间里低效探索 | 多轮 reject、组很快 exhausted | active 参数子空间是否足够小，候选是否足够多样 |
| 渲染域差异 | Unity 效果在当前 Laya shader 中无法完全一致 | 参数都可控，但仍有残余差异 | 在不改 shader 的前提下，最高可接受分数和人工可接受阈值在哪里 |

这意味着下一阶段不应只追求“更强的优化器”，而应先建立一套实验闭环：

```text
score sanity
  -> parameter sensitivity
  -> effective bounds discovery
  -> active subspace
  -> low-budget optimizer
  -> human preference check
  -> same-shader limit report
```

---

## 2. 方向一：参数可控性实验（Sensitivity Map）

### 2.1 目标

先回答一个比“怎么优化”更基础的问题：**Laya 端每个控件组、每个参数，是否真的能在当前截图里改变画面？**

如果一个参数强扰动后也没有可测变化，那么它不应该进入优化器；如果一个参数只影响背景或极小高光区域，那么它应被放到对应区域评分里，而不是用全局 `fit_score` 判定。

### 2.2 实验单元

以运行控制台启用的 Laya 控件组为单位，每组生成强扰动候选：

- float/range：取 `min/max`、当前值上下 30%-50% 的 safe step。
- color：生成 brighten、darken、warm、cool、desaturate、zero-emission 等方向。
- gate/intensity：生成 `0`、small nonzero、high visible value。
- enum/define：第一版只记录，不自动改；后续作为离散动作探针。

每个候选只问两个问题：

- 和基线截图相比，是否有足够可见变化。
- 变化落在哪些视觉区域：主体色、暗部、高光、边缘光、发光、背景、轮廓外。

### 2.3 输出结构

建议新增 `auto_adjust/sensitivity_map.json`：

```json
{
  "baseline_capture": "auto_adjust/sensitivity/baseline.png",
  "groups": [
    {
      "group": "fresnel",
      "status": "visible",
      "max_mean_diff": 8.4,
      "affected_regions": ["rim", "edge_highlight"],
      "effective_params": [
        {
          "name": "u_FresnelIntensity",
          "candidate": "high",
          "mean_diff": 8.4,
          "foreground_diff": 7.9,
          "background_diff": 0.2,
          "score_delta": -0.013,
          "recommended_role": "gate"
        }
      ]
    }
  ]
}
```

这里的 `score_delta` 不是主要目标。sensitivity 的首要目标是验证“能不能控制画面”，不是验证“是否更像 Unity”。

### 2.4 UI 展示

运行控制台的控件组卡片可以增加一个小状态：

- `可见`：强扰动导致前景可测变化。
- `弱可见`：有变化但低于主优化阈值。
- `不可见`：变化低于噪声，暂不搜索。
- `背景污染`：变化主要落在背景，应检查 mask/截图区域。
- `疑似门控`：color 参数无效，但 intensity/gate 有效。

### 2.5 对优化器的影响

sensitivity map 应成为所有优化器的前置过滤器：

- `semantic_group` 只搜索 `visible/weak_visible` 组。
- CMA-ES 的 `param_whitelist` 只包含 `effective_params`。
- 对 `疑似门控` 的组，先做 gate 激活，再搜索 color/intensity。
- 对 `不可见` 的组，除非人工强制启用，否则跳过。

---

## 3. 方向二：专家样本与参数边界发现

### 3.1 人工调参样本暴露的问题

用户提供的人工调好材质 `1580_body.lmat` 给出一个非常重要的反例：它的成功效果不是靠大幅修改 `u_BaseColor`，而是保持基础层中性，同时极大强化效果层。

关键人工值包括：

| 参数 | 人工值 | 当前通用边界问题 | 含义 |
| --- | ---: | --- | --- |
| `u_BaseColor` | `[1, 1, 1, 1]` | 无问题 | 基础贴图保持原貌，不靠底色硬染 |
| `u_Gamma_Power` | `1` | 无问题 | 不通过 gamma 大幅改变贴图 |
| `u_GIIntensity` | `2` | shader range `[0, 2]` | 暗部/环境亮度顶到上限 |
| `u_IBLMapIntensity` | `5` | 无 shader range，fallback `[0, 8]` 可覆盖 | 青蓝环境反射是主效果层 |
| `u_IBLMapPower` | `3.543` | fallback `[0, 10]` 可覆盖 | 提高 IBL 对比/非线性 |
| `u_MatcapStrength` | `15` | fallback `[0, 8]` 覆盖不到 | Matcap 是高杠杆主效果 |
| `u_SpecularIntensity` | `5` | fallback `[0, 8]` 可覆盖 | 高光强度很高 |
| `u_SpecularThreshold` | `5` | fallback `[0, 1]` 覆盖不到 | FishStandard 的 threshold 并不总是 0-1 |
| `u_SpecularSmooth` | `3` | fallback `[0, 1]` 覆盖不到 | 高光过渡范围明显超过 1 |
| `u_FresnelIntensity` | `15` | fallback `[0, 8]` 覆盖不到 | 强边缘光是最终观感关键 |
| `u_FresnelPow` | `5` | fallback `[0, 10]` 可覆盖 | 轮廓光形状控制 |
| `u_EmissionScale` | `5` | fallback `[0, 8]` 可覆盖 | 自发光参与整体风格 |

这说明当前失败可能不只是“搜索策略差”，还有一个更直接的问题：**搜索空间本身被我们错误截断了**。算法即使方向正确，也无法抵达人工可达的 Matcap/Fresnel/Specular 区域。

### 3.2 当前边界来源核实与修正

当前系统的边界来源分三层：

1. `material_fit/laya/shader_parser.py` 会解析 Laya shader `uniformMap` 里的 `range: [min, max]`，写入 `ShaderParam.range_min/range_max`。
2. `SemanticGroupStrategy._bounds_for_value` 和 `ParameterEncoder._default_bounds` 会优先使用 shader range。
3. 如果 shader 没写 range，就落到名字规则 fallback，例如：
   - `intensity/strength/scale` -> `[0, 8]`
   - `threshold/smooth/metallic/occlusion` -> `[0, 1]`
   - `pow/power` -> `[0, 10]`
   - `gamma` -> `[0.05, 10]`

这个 fallback 对通用 PBR 材质看似合理，但对 FishStandard 这类风格化 shader 不可靠。`FishStandard.shader` 里很多关键参数没有声明 `range`，而 Laya Inspector 或运行时材质值实际允许超过这个范围。人工 `.lmat` 已经证明：

- `u_MatcapStrength = 15`
- `u_FresnelIntensity = 15`
- `u_SpecularThreshold = 5`
- `u_SpecularSmooth = 3`

因此，**没有 shader range 不等于参数只能用通用 fallback**。fallback 只能是安全初值，不应被当作 Laya 的真实可写范围或视觉有效范围。

用户后续在 Laya 中实际测试后补充了更准确的事实：这些参数很多在 Laya Inspector 中并没有显示硬性范围，手工可以输入任意数字，引擎也不会主动报错或拒绝。随着数值继续增大，会出现两类情况：

- **效果越来越极端**：例如边缘光、Matcap 或高光强到破坏画面。
- **效果进入饱和或无变化区**：继续加大数值，但截图变化很小，优化器只是在浪费搜索空间。

所以这里所谓“边界”不应理解成 Laya 引擎的硬限制，而应理解成**有效搜索边界**：在这个区间内，参数变化仍然能产生可解释、可控、对目标有帮助的视觉变化；超出后要么过度极端，要么边际收益接近 0。

### 3.3 主动获取有效搜索边界的方案

建议新增一个 `laya_effective_bounds_discovery` 机制，为每个 shader 生成可审计的有效边界表。来源按可信度排序：

1. **显式 shader range**：如果 `uniformMap` 写了 `range: [min, max]`，先作为 UI 建议区间，标记 `source=shader_range`。但它仍不一定等于视觉有效上界。
2. **现有材质语料统计**：扫描项目里使用同一个 shader 的所有 `.lmat`，收集每个参数的 min/max/P95/P99。人工样本里的 15 会立刻扩展 `MatcapStrength/FresnelIntensity` 的候选上界，标记 `source=material_corpus`。
3. **Laya Inspector/运行时可写性检查**：如果 Inspector 无硬限制，记录为 `editable_unbounded=true`，说明算法必须自己定义安全搜索区间。
4. **视觉响应曲线探针**：对无硬 range 参数，按指数或分段序列写入值，例如 `0, 0.5, 1, 2, 5, 10, 15, 20, 30`，每次截图并计算相对基线的 foreground diff、区域 diff 和主评分变化。若边际变化低于阈值，标记为饱和；若画面爆白、过曝、全屏污染或结构崩坏，标记为危险。
5. **专家预设覆盖**：对已知 shader（如 FishStandard）保存人工有效区间模板，标记 `source=curated_expert`。
6. **名字规则 fallback**：只有以上都没有时才使用，标记 `source=name_fallback_low_confidence`。

边界发现输出建议写为：

```json
{
  "shader": "Custom/Fish/FishStandard",
  "params": {
    "u_MatcapStrength": {
      "type": "Float",
      "editable_unbounded": true,
      "suggested_search_min": 0.0,
      "suggested_search_max": 20.0,
      "observed_min": 0.0,
      "observed_max": 15.0,
      "saturation_start": 20.0,
      "danger_start": 30.0,
      "source": ["material_corpus", "curated_expert"],
      "confidence": 0.85,
      "notes": [
        "expert 1580_body.lmat uses 15",
        "Laya accepts arbitrary numeric input; bounds are visual search bounds, not engine limits"
      ]
    }
  }
}
```

### 3.4 对算法的直接影响

有效边界发现应早于 sensitivity 和优化器：

```text
parse shader range
  -> scan material corpus
  -> check editor/runtime writability
  -> probe visual response curve
  -> build param_effective_bounds.json
  -> sensitivity map uses effective bounds
  -> optimizer uses effective bounds
```

对 FishStandard 的第一版 curated bounds 可以直接设为：

- `u_MatcapStrength: [0, 20]`
- `u_FresnelIntensity: [0, 20]`
- `u_SpecularThreshold: [0, 8]`
- `u_SpecularSmooth: [0, 5]`
- `u_IBLMapIntensity: [0, 8]`
- `u_EmissionScale: [0, 8]`
- `u_GIIntensity: [0, 2]`（来自 shader range）

这会改变后续所有搜索：高杠杆参数不再被错误截断，sensitivity map 才能看到真正强扰动下的可控性，BO/CMA 也才可能接近人工样本。同时，算法也不会因为“引擎允许任意数字”就无限扩大搜索空间，而是会在视觉仍然有效、不过度极端的区间内搜索。

### 3.5 对调参策略的启发

人工样本显示 FishStandard 的有效路径更像：

```text
keep base texture neutral
  -> lift GI / IBL
  -> add strong Matcap
  -> shape Specular
  -> add strong Fresnel rim
  -> add localized Emission
```

因此 `fresh_fit` 的主动隔离只能作为诊断阶段，不能让算法长期停留在“压低效果层后调 base color”。更合理的是：

- 先快速确认 `base_color/gamma` 是否需要明显调整。
- 如果 base 变化收益低，尽快切入 `IBL/Matcap/Specular/Fresnel/Emission`。
- 对这些高杠杆组使用更宽边界、更大 step、更高采样优先级。
- 将人工 `.lmat` 作为 expert prior，生成一批“强效果层组合候选”，而不是只做单轴微扰。

---

## 4. 方向三：多目标评分拆分

> 专门评分机制文档见 `Scoring_Mechanism_Design.md`。后续任何评分权重、阈值、mask、human accept score 或校准样本变化，都应继续追加到该文档。

### 4.1 为什么不能只看一个 `fit_score`

单一分数适合排序，但不适合诊断。低分可能来自：

- 背景颜色不一致。
- 轮廓位置差 1-2 像素。
- 主体整体偏暗。
- 高光形状不一致。
- Fresnel 边缘过强。
- 发光区域颜色错。

这些问题对参数的归因完全不同。如果都压成一个 scalar，优化器不知道应优先调 base color、specular、rim 还是曝光。

### 4.2 建议拆分项

保留 `fit_score` 作为总排序，但每轮同时输出以下分量：

| 分量 | 用途 | 可能关联参数 |
| --- | --- | --- |
| `foreground_color_score` | 前景整体颜色 | base color、gamma、global tint |
| `luma_score` | 明暗/曝光 | gamma、GI、occlusion、diffuse ramp |
| `hue_score` | 色相偏差 | HSV、base color、tint |
| `specular_score` | 高光强度和范围 | specular、smoothness、metallic |
| `rim_score` | 边缘光/Fresnel | Fresnel intensity/color/power |
| `emission_score` | 自发光区域 | emission color/scale |
| `structure_score` | 轮廓/局部结构 | 截图对齐、模型姿态、SSIM |
| `background_score` | 背景污染诊断 | 不参与主优化，只报警 |

### 4.3 评分面板

运行控制台和迭代详情可以把每轮结果显示为“雷达式诊断表”：

```text
fit_score            0.230
foreground_color     0.410  improving
luma                 0.180  worse
specular             0.050  unchanged
rim                  0.620  good
background_delta     high   ignored
```

这样工程师能立刻看出低分到底低在哪里，优化器也能按最差分量选择控件组。

### 4.4 接受/拒绝逻辑改变

当前候选基本按总 `fit_score` 接受。下一版应支持“主目标 + 护栏”：

- 主目标：本阶段关心的分量必须改善，例如 base 阶段看 `foreground_color_score/luma_score`。
- 护栏：其他关键分量不能明显崩坏，例如调 base color 时不能让 rim/emission 爆掉。
- 总分：作为最终排序，但不作为唯一接受标准。

这更接近人类工程师的调参方式：先让基础色接近，再恢复高光/边缘光，而不是要求每一步都让全局分数上升。

---

## 5. 方向四：从“坐标搜索”升级为“实验设计”

### 5.1 当前搜索的局限

当前 `SemanticGroupStrategy` 已经比旧版好很多：它有分组顺序、主动隔离、accept/reject、rollback、combo candidates。但它本质仍是单候选坐标搜索：

- 一轮只试一个方向。
- 被拒后才换方向或换轴。
- 很难知道参数之间的耦合关系。
- 不能高效利用历史样本。

对真实 Laya 闭环而言，评估次数很贵，因此算法应从“尝试下一个步子”升级为“用每一次评估最大化信息量”。

### 5.2 推荐路线

先做 sensitivity，再做低维优化：

```text
enabled control groups
  -> sensitivity map
  -> visible active subspace
  -> group-level surrogate / BO
  -> cross-group refinement
  -> final human preference check
```

### 5.3 组内 Surrogate / BO

每个控件组单独建一个小模型。输入是归一化后的参数向量，输出可以是：

- 当前阶段主评分。
- 多目标分量加权得分。
- 人类偏好选择结果。

第一版不必上复杂 BoTorch，可以先从简单、可解释的模型开始：

- `random + elite replay`：随机探索少量候选，围绕最优样本缩小范围。
- `TPE / forest surrogate`：适合低预算、非平滑、混合变量。
- `Gaussian Process BO`：适合 2-8 维组内连续参数。

### 5.4 候选生成方式

每次不再只生成一个“下一步”，而是生成候选池，再串行评估最有价值的一个：

```text
candidate_pool =
  sensitivity_high_axes
  + inverse_error_direction_candidates
  + local_mutations_around_best
  + occasional_exploration

next = acquisition(candidate_pool)
```

这样即使 Laya 只能一次评估一个候选，算法内部仍有“计划感”。

### 5.5 Active Subspace CMA-ES

CMA-ES 仍然有价值，但位置应后移：

1. sensitivity 剔除无效参数。
2. 组内优化找出初步可接受结果。
3. 只把 `effective_params` 合并为 active subspace。
4. 用历史真实样本 warm-start CMA-ES 做跨组耦合收尾。

这比全维 `cma_warm` 更符合真实约束：低维、已激活、已有好样本。

---

## 6. 方向五：人类工程师闭环

### 6.1 为什么需要人类反馈

Unity/Laya 材质匹配的目标是“肉眼像”，而不是严格像素一致。很多情况机器评分会犹豫：

- A 候选颜色更准，但高光差。
- B 候选结构更像，但整体偏暗。
- C 候选分数略低，但肉眼更接近 Unity 风格。

这类判断非常适合让工程师参与，尤其在早期算法还没校准好时。

### 6.2 候选对比模式

在运行控制台增加一个可选的 `human_review` 阶段：

- 系统连续生成 3-5 个候选。
- UI 显示 Unity reference、current best、候选 A/B/C。
- 每张候选显示主要参数变化和评分分量。
- 工程师选择“最像的一张”或“都不好”。

选择结果写入 `auto_adjust/human_preferences.json`：

```json
{
  "round": 3,
  "reference": "unity.png",
  "candidates": ["cand_a.png", "cand_b.png", "cand_c.png"],
  "selected": "cand_b.png",
  "reason_tags": ["better_base_color", "less_overbright"],
  "param_delta": {
    "u_BaseColor": [0.74, 0.62, 0.55, 1.0]
  }
}
```

### 6.3 用人类反馈训练搜索偏好

短期可以简单使用：

- 被选中的候选强制成为 group best。
- 被连续否定的方向降低采样概率。
- 工程师 reason tags 影响下一阶段权重，例如 `too_bright` 提高 luma 权重。

长期可以做偏好模型：

- 输入：参数差值 + 评分分量。
- 输出：被人类选择的概率。
- 用于 acquisition 函数，补足 `fit_score` 不可靠的部分。

---

## 7. 方向六：判断同 shader 调参上限

### 7.1 为什么仍要做上限判断

用户补充的人工调参截图说明：即便与 Unity 参考仍有差别，只要整体观感接近、关键材质特征接近，在实际需求里就可以接受。因此当前策略应明确调整为：

- **不优先建议修改 shader**。
- **不把“完全一致”作为必要目标**。
- **在当前 FishStandard shader 内尽可能搜索到高分、肉眼可接受的结果**。

上限判断的目的不是催促改 shader，而是回答两个更实际的问题：

- 当前 shader + 当前参数空间最高能达到什么分数。
- 人类工程师认为“可接受”的截图，在我们的评分系统里大概是多少分。

如果人工可接受截图的分数并不高，说明评分函数还需要校准；如果人工可接受截图分数明显高于算法结果，说明算法和搜索空间仍有改进空间。

### 7.2 上限实验

建议做一个“可达性压力测试”：

- 对 active subspace 做高预算随机/BO 搜索，例如 300-500 eval。
- 允许参数走到视觉有效边界，但避免危险区。
- 记录历史最高分、肉眼最佳候选、人工可接受样本分数。
- 如果算法最高分仍明显低于人工样本，标记为 `optimizer_underperforming_expert`。
- 如果算法最高分接近人工样本但仍低于 Unity，标记为 `same_shader_limit_reached`。

这两个结论的含义不同：

- `optimizer_underperforming_expert`：继续改算法、边界、候选生成和 expert prior。
- `same_shader_limit_reached`：当前 shader 下已经接近人工可接受上限，后续只做小幅微调或评分校准。

### 7.3 人工可接受样本的价值

人工调出的可接受截图应进入系统，成为 `expert_reference_candidate`：

1. 用现有 `analyze_image_diff` 跑 Unity reference vs 人工截图。
2. 记录它的总分、前景色分、亮度分、高光分、rim 分、emission 分。
3. 把该分数作为第一阶段现实目标，而不是一开始就要求 `target_score=0.9`。
4. 从人工 `.lmat` 提取 expert prior，生成强效果层组合候选。
5. 用人工样本校准评分：如果人觉得可接受但分数很低，优先修 metric；如果分数高，优先让算法追上这个分数。

如果未来确实要讨论 shader 迁移，也应作为独立研究方向，而不是当前主线。当前主线是：**同 shader、同材质系统、尽可能逼近 Unity 参考**。

### 7.4 当前人工样本的初步评分

使用项目 `fish_1580` 中配置的 Unity reference，与用户提供的人工调参截图做了一次 `analyze_image_diff` 对比，输出位于：

```text
tools/material_fit/output/fish_1580/manual_expert_compare/diff_analysis.json
```

如果这张 Unity reference 与用户截图确实属于同一个案例，那么这次人工可接受样本的关键分数是：

- `score` / foreground RGB MAE：`0.2156`
- `perceptual_fit_score`：`0.2019`
- `foreground_ratio`：`0.1558`
- `bg_normalize_applied`：`true`
- legacy MAE branch 仍会饱和，但当前 exp decay MAE branch 没有饱和。

这个结果对算法目标有直接影响：

- 现阶段 `target_score=0.9` 不应作为真实闭环硬目标；它更像长期理想值。
- 如果人类认为 `0.20` 左右已经“基本可接受”，那么第一阶段算法目标应该先定为“稳定追上人工样本”，例如 `0.20-0.30` 区间。
- 如果后续人工样本在肉眼上更接近 Unity，但评分仍不高，说明 metric 仍需要校准，而不是简单认为材质失败。
- 自动优化的第一目标应变为：在同 shader、同截图条件下超过当前人工样本分数，或者至少达到同等级观感。

---

## 8. 建议的下一阶段实施顺序

### Step 1：先发现有效搜索边界

新增 `laya_effective_bounds_discovery`，从 shader range、项目 `.lmat` 语料、Laya Inspector/运行时可写性、视觉响应曲线探针、专家预设中合并出 `param_effective_bounds.json`。目标不是寻找引擎硬限制，而是避免优化器在错误的视觉有效区间里搜索。

验收标准：

- FishStandard 的 `u_MatcapStrength/u_FresnelIntensity/u_SpecularThreshold/u_SpecularSmooth` 不再被通用 fallback 截断。
- 每个边界都有 `editable_unbounded/source/confidence/notes`，工程师能审计。
- optimizer 和 sensitivity map 都读取同一份 effective bounds。

### Step 2：只做诊断，不优化

新增一个 `sensitivity` 运行模式，先对每个启用组做强扰动，生成 `sensitivity_map.json` 和缩略图。目标是确认参数可控性。

验收标准：

- 能列出每组 `visible/weak_visible/invisible/gated`。
- 能列出每组影响的视觉区域。
- 能生成 active param whitelist。

### Step 3：评分面板拆分

把 `fit_score` 拆成多分量诊断并显示在迭代详情中。目标是让低分原因可读。

验收标准：

- 每轮能看到前景、亮度、色相、高光、rim、emission、背景污染分量。
- `decision.json` 记录候选是因哪个分量被接受或拒绝。

### Step 4：基于 sensitivity 的优化器

新增 `semantic_bo` 或 `semantic_experiment` 策略。它不直接全维搜索，而是读取 active subspace 和多分量评分。

验收标准：

- 同样 50 eval 下，比当前 `semantic_group` 有更明显的前景分量提升。
- 无效组不会被反复搜索。
- 每轮候选能说明来自哪种 proposal：explore、local_best、human_hint、surrogate。

### Step 5：候选对比和人工选择

在运行控制台加入候选对比模式，让工程师每隔若干轮选择更像的候选。

验收标准：

- 人工选择可写入历史。
- 被选中候选可作为下一轮 best。
- reason tags 能影响后续权重。

### Step 6：同 shader 上限测试

当诊断和优化都完成后，做高预算上限测试，并与人工可接受样本对比。

验收标准：

- 输出 `same_shader_limit_report.md`。
- 明确结论是“算法仍低于人工样本”还是“已接近当前 shader 可接受上限”。
- 给出下一阶段是继续优化搜索，还是降低目标分数/校准 metric。

---

## 9. 近期推荐优先级

我建议下一次真正动代码时，优先级如下：

1. `laya_effective_bounds_discovery`：先修正视觉有效搜索边界，避免算法永远搜不到人工可达区域，也避免在饱和/危险区浪费预算。
2. `sensitivity_map`：在有效搜索边界内验证哪些参数真的可控。
3. `metric_split_panel`：让工程师能看懂每轮到底哪里变好了，哪里变坏了。
4. `semantic_experiment`：基于 active subspace 做低预算实验设计，替换当前单轴坐标搜索。
5. `human_candidate_review`：把肉眼判断变成系统可记录的优化信号。
6. `same_shader_limit_report`：判断算法是否追上人工可接受样本，而不是默认转向 shader 迁移。

一句话结论：**下一阶段不应继续盲目追求更复杂的优化器，而应先证明“评分可信、参数可控、搜索空间有效、目标在同 shader 内可接受”。只有这四件事成立，BO/CMA/LLM 才真正有用。**

---

## 10. 2026-05-09 架构整理落点

本轮开始将前面的方案落实到更清晰的模块边界：

- `material_fit/auto_adjust/scoring.py`：自动调参主分数选择与迭代诊断摘取。
- `material_fit/vision/human_accept_score.py`：`human_accept_score` v1/v2 的评分分量、bbox 对齐诊断和轮廓 ignore band 估计。
- `material_fit/optimizer/effective_bounds.py`：FishStandard 有效视觉搜索边界的单一来源。
- `material_fit_ui/backend/routers/`：后端 HTTP 路由拆分，`main.py` 只保留 app 初始化。
- `material_fit_ui/backend/preanalysis_parts/fish_standard.py`：FishStandard UI schema 边界 metadata。
- `material_fit_ui/frontend/src/api/client.ts`：前端 API 请求基础设施。
- `material_fit_ui/frontend/src/components/RunModePicker.vue`：运行策略选择控件。

这次整理的目的不是改变调参行为，而是避免后续继续把评分、边界、UI schema、运行控制都堆进单个长文件。后续新增 BO、人类偏好样本或更复杂的 foreground alignment 时，应优先放到这些边界明确的模块内。

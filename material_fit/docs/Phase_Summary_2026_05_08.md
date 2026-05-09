# 阶段性总结：Laya 材质自动拟合（Material Fit）

> 状态：阶段性总结。
> 时间：2026-05-08。
> 目的：把"我们想做什么 / 已经做了什么 / 哪些没成 / 这次实验失败的真正原因 / 下一步要换什么算法"一次性记录下来，作为之后所有讨论的共同起点。
> 上一份中间文档：`Optimizer_Current_State_And_Next_Plan.md`、`Optimization_Algorithm_Redesign.md`。本份文档与它们互补，重点放在**已做内容的总检视 + 最近一次跑 42 轮失败的根因诊断 + 下阶段算法路线**。

---

## 1. 我们要解决的问题

把同一个 3D 模型在 Unity 里渲染出来的视觉效果，**通过调 Laya 端 `.lmat` 的 shader 参数**，尽可能在 Laya 渲染窗口里复现出来。

约束有几条非常硬：

1. **Laya 渲染是黑盒**。我们能做的只有：写 `.lmat` → 让 Laya IDE 重渲染 → 截屏 → 比对像素。
2. **一次只能跑一个候选**：Laya 截屏要等窗口刷新，没办法并行评估，所以单候选评估代价非常高（>1s/次，含截图等待）。
3. **Unity ↔ Laya shader 在底层是不一致的**：参数空间、数值含义、光照模型都不完全对齐，所以"把 Unity 数值搬到 Laya"通常不直接可行。
4. **梯度不可得**：渲染管线没有可微实现，只能做无梯度（black-box）优化。
5. **目标是肉眼一致**而非像素一致：评分需要感知意义，否则单纯 MAE 容易陷入"背景差异主导"的陷阱（这一点正是这次实验暴露的核心问题）。

---

## 2. 我们一路上的设计思路演进

按时间顺序回顾，每一段都对应一个真实问题：

### 2.1 第一阶段：纯启发式 + stage 反馈控制（heuristic）

最早的 `HeuristicStrategy` 是个固定 stage plan：`base_color → shadow_diffuse → specular → reflection_matcap → fresnel_emission → global_grade`。每轮挑一个 stage，看通道残差选 ±方向，按衰减步长（`gain = max(0.35, 0.72 * 0.86^iter)`）改少量参数。

**优点**：可解释、轻量、容易加领域规则。
**问题**：

- stage 顺序写死，与具体材质无关；
- 单候选 + 始终接受新值，没有 rollback，差的也照单全收；
- 没有 gating（intensity=0 时 color 无效）的概念，浪费迭代；
- coupled 参数（如 IBL color × intensity）相互干扰。

### 2.2 第二阶段：CMA-ES（cma_cold / cma_warm）

为了脱离写死 stage、利用全局协方差，加入 CMA-ES 策略。warm-start 版本以已有最优为均值。

**问题**：

- 无梯度方法在数十维参数上理论需要 100×D~1000×D 评估才稳定，而我们一次评估 >1s；
- mixed-integer（gating bool / shader define）天生不友好；
- 单候选评估抹掉了 CMA-ES 的并行优势。

### 2.3 第三阶段：LLM 语义预分析 + 三层 Schema

观察到模糊命名匹配很脆弱（用户提醒：换个变量名整个 pipeline 就失效），引入 LLM 预分析做 Unity 模块识别（"用了 fresnel 没？开了 emission 没？"），输出 `module_plan`。同时建立 **auto / manual / effective** 三层控件 schema：自动生成、人工覆盖、最终落到优化器的合并版。

**优点**：把"语义"明确剥到一个独立环节；不再依赖在数值层做命名匹配。
**问题**：

- LLM 给的是模块级别的 hint，和最终参数搜索之间还隔着翻译；
- 当时 effective schema 还没把"分组顺序"传给优化器（这次才补）。

### 2.4 第四阶段：运行控制台 + 控件预设系统（FishStandard）

用户提出：完全的 LLM 自动不可靠，工程师经验有用，应让人在 UI 上**直接选/拖/隐藏哪些组参与优化**。于是把 schema inspector 从预分析视图搬到 RunConsoleView，加入 **预设系统**（apply / save / rename / delete），并内置一份针对 `FishStandard.shader` 调好的 builtin preset。

**优点**：人能在两秒内把搜索空间从 60+ 参数砍到 10 个；preset 可复用、可持久化。
**问题**：UI 配好了，但**优化器没真正消费 UI 的 order**——这是最近才发现并修掉的。

### 2.5 第五阶段：SemanticGroupStrategy 重写（最近一次）

把 `semantic_group` 从"裸单轴扰动"重写为：

- 组级 probe（先看这组动一下整体有没有视觉变化）；
- 组内 pattern search（一根轴一根轴 ±方向走）；
- 显式 accept/reject/rollback（fit_score 没 ≥ `min_improvement` 就回滚）；
- 组耗尽后切下一组；
- 这一版还接入了 UI panel order；
- 加了"同一根轴 +/− 都被拒立刻跳轴"和"小组（≤2 参）`max_no_improve=3`"两个收尾改进。

**优点**：决策链可解释，每一步都能在 `decision.json` 里复盘。
**问题**：**这次跑 42 轮 fit_score 没动**——而且这回的根因和优化器选哪根轴、按什么顺序都没什么关系，下一节专门讲。

---

## 3. 已经做完、能稳定使用的基础设施

为了下阶段不要重复造轮子，把"现有可用件"列清楚：

- **`fit_material.py` 自动调参主循环**：单候选 → propose → 写 lmat → 触发 laya 重渲染 → 截图 → 评分。包含 backup `.lmat`、capture region 锚点、focus log。
- **截屏 + 锚点回归**：通过 LayaAirIDE 窗口名匹配 + 偏移记忆，截图区域可在窗口被移动后自动重定位。
- **图像差异分析（`vision/diff_analysis.py`）**：
  - auto-mask 前景识别；
  - channel-weighted MAE（基础色 / 阴影 / 高光 / 反射 / fresnel / emission / 全局调色 6 通道权重）；
  - SSIM 分支；
  - 合成 `fit_score = 0.7 * mae_branch + 0.3 * ssim_branch`，其中 `mae_branch = max(0, 1 − √(4·MAE))`。
- **shader 参数 metadata（`ShaderEffectGraph`）**：每个 param 有 transform / bounds / searchable / gating；每个 group 有 channels / search_priority / order / probe_required。
- **三层 Laya 控件 Schema + 预设系统**：auto + manual override + effective merged，preset 可保存/重命名/删除，FishStandard builtin。
- **运行控制台 UI**：可视化分组、勾选启用、拖动 order、应用/保存预设。
- **多策略可插拔**：`heuristic` / `cma_cold` / `cma_warm` / `semantic_group` 已统一在 `OptimizerStrategy` 接口下。
- **每轮可复盘的 `decision.json`**：包含 `previous_candidate.outcome`（accepted/rejected）、`group_state`、`changes`、`stop_reason`、`perceptual_signals` 全套，事后可以一行命令重跑分析。
- **测试**：`test_semantic_graph_optimizer.py` 等 11 个单测，控件 schema preset 的后端单测，前端 vue-tsc / lint 通过。

这一层基本可信、不需要重做。

---

## 4. 最近一次实验（fish_1580，max_iters=100，实跑 42 轮）

### 4.1 实验配置

- shader：`FishStandard.shader`（Laya）vs `CustomStandardV2.shader`（Unity）
- preset：FishStandard builtin（人工已挑过 base_color / diffuse / specular / fresnel / emission / matcap / global_grade 等组）
- optimizer：`semantic_group`
- target_fit_score：0.9
- max_iterations：100

### 4.2 实际结果

- 触发停止：`max_iterations_reached`，但**只跑到第 42 轮就停了**（看 `report.md` Status，与 UI 显示一致；说明 jobs 是被外部"max_iterations=42"那次旧 config 触发，不是 100；但 100-iter 的版本即使跑到底也收敛不了，下面的根因解释为什么）。
- best fit_score：**0.0212**（初始 0.0211，整轮提升 0.0001）。
- best RGB MAE：**0.2149**。
- 全程几乎所有轮的 `previous_candidate.outcome` 都是 `rejected_rollback_to_base`，群组在 `base_color`、`diffuse_shadow`、`main_specular` 之间来回切，最终全部 `exhausted`。
- 肉眼看：candidate 几乎一直保持初始状态。

### 4.3 关键诊断 —— **这不是优化器的问题，是评分的问题**

打开任意一轮的 `perceptual_signals`：

```text
weighted_mae = 0.33
ssim         = 0.07
fit_score    = 0.021
fit_components = { mae_branch: 0.0, ssim_branch: 0.07 }
auto_mask = { foreground_ratio: 0.13–0.16,
              reference_bg_color = (71, 71, 71),
              candidate_bg_color = (134, 151, 180) }
```

把这几行连起来看：

1. **MAE 分支已经饱和到 0**。`mae_branch = max(0, 1 − √(4·0.33)) = max(0, 1 − √1.32) = max(0, −0.15) = 0`。MAE > 0.25 之后这个分支就是常数 0，**不管参数怎么调，MAE 分支都不会动**。
2. **fit_score 完全靠 SSIM 撑**。fit_score = 0.7·0 + 0.3·0.07 ≈ 0.021。这是 fit_score 死死卡在 0.021 的原因。
3. **MAE 为什么这么大**？因为 Laya 截图背景是浅蓝 (134,151,180)，Unity 参考图背景是深灰 (71,71,71)。背景差大约 60–80 / 255 ≈ 0.27，**前景只占 14% 像素，剩下 86% 的背景差被平均进了 MAE**。所以 MAE ≈ 0.33 几乎全是背景贡献。
4. **优化器以为自己在工作**。每一轮 `delta` 量级是 `±1e−5 ~ ±1e−3`，远小于 `min_improvement = 0.0015`，所有候选都被判 reject 然后回滚。这就是 42 轮看上去几乎没变化的物理原因。
5. **`mae_branch=0` 还破坏了梯度信息**。即便参数改对了让 MAE 从 0.34 → 0.30，mae_branch 都还是 0，优化器**收不到任何信号**告诉它这个方向是好的。

> **核心结论：在评分函数对前景敏感、对背景不敏感之前，无论换什么优化器都救不回来。** 哪怕换 BO / CMA-ES / 强化学习，都会被同一个评分平台困住。

### 4.4 次要问题（即使评分修好也要解决）

- **min_improvement 阈值与评分尺度不匹配**：当 fit_score 整体在 0.02 量级时，0.0015 的阈值差不多是 7%，几乎没有候选能跨过。需要变成相对阈值（如 `max(1e−4, 0.5% × current_fit)`）或者基于 baseline std 自适应。
- **单候选评估太慢**：每轮 ≥1s 截图等待 + 100ms 写盘等待。一次 100 轮要 2~5 分钟。要么提速截图（截图局部窗口 + 轮询变化），要么并行多 Laya 实例。
- **probe 没有真正"暖起来"**：FishStandard preset 大量组 `probe_required=False`、`current_active=True`，于是直接进 pattern search，一根轴一次扰动幅度不足以让 SSIM 变化超过 `probe_score_delta=0.001`。
- **stop 太软**：`max_iterations_reached` 只是"用完预算"而非"算法收敛"，看不出来到底是被困死还是值不值得继续。
- **`global_no_improve=42` 与 `_max_group_no_improve` 配合**：一旦每组都被 reject 几轮，会迅速全部进入 `exhausted`，本轮虽然又被 select，但是没有任何状态可继续，本质是死循环。

---

## 5. 下一阶段的算法路线（按优先级排序）

### P0 ─ 修评分（必须先做，否则一切下游都无效）

**这是最高优先级，绝大多数性价比都在这一步。**

1. **真正的 alpha mask / 前景 mask**。要么在 Laya 端把背景设成 alpha=0（截 PNG 带 alpha），要么用 Unity 参考图的 alpha 通道做 ground truth mask，**MAE / SSIM 只在前景像素上计算**。这一改完，MAE 立刻从 0.33 量级回到 0.05 量级，mae_branch 立刻不再饱和。
2. **`mae_branch` 的映射换成单调下降而不是分段截断**。`1 − √(4·MAE)` 在 MAE>0.25 后就是 0；改成 `exp(−k·MAE)` 这种永远有梯度的形式，让任何方向上的细微改善都能被算法看见。
3. **加入感知 / 颜色空间的项**。在 mask 内做 LAB ΔE 或 OKLab 距离，比 RGB MAE 更接近肉眼。可以作为第三个 branch，权重 0.2~0.3。
4. **诊断输出加 sanity check**：每轮报告 `mae_branch_saturated: true/false`、`foreground_ratio`、`bg_color_delta`，一眼就能看出"是不是又被背景骗了"。

> 这一步如果做完，下一次跑同样 100 轮，肉眼几乎肯定能看到明显变化。哪怕优化器还停留在现在的 SemanticGroupStrategy。

### P1 ─ 让单候选黑盒评估"省着用"

评分修好后，下一道瓶颈就是**评估代价**。每次 ≥1s 不变的话，1000 轮要 20 分钟，工程师不会等。两条路：

a. **截屏 / 重渲染加速**。截图区域已经锚定了；下一步可以做"局部哈希轮询"——写完 lmat 后先 100ms 抓一张缩略图，hash 跟上次比，变了再抓全分辨率。预期能把每轮压到 0.3s。

b. **并行多 Laya 实例**。这是 CMA-ES 真正能发挥的前提。考虑到 LayaAirIDE 是 GUI 应用，更现实的是直接用 Laya 的 headless runtime（如果有的话）跑多实例。这个工程量较大，先做 a。

### P2 ─ 算法升级：从 pattern search 到代理模型

评分和评估速度都修好之后，再来谈算法本身：

1. **加 surrogate 模型（贝叶斯优化 / GP / 树模型）**。低维（每组 ≤ 10 维）+ 评估贵 + 黑盒，正是 BO 的典型应用场景。每组用一个独立的 surrogate，按 EI / UCB acquisition 选下一个候选，比"裸 ±18% 步长"高效得多。建议组件：`scikit-optimize` 或 `BoTorch`。
2. **保留 SemanticGroupStrategy 作为 baseline 与 cold-start 探针**。组级 probe 和 accept/reject 框架不丢，只把"在组内挑下一个候选"换成 BO 的 acquisition。
3. **跨组 active subspace CMA-ES（最后阶段）**。当 P0/P1/P2 都到位后，每个组各自局部最优了，再用一次"在所有组的可搜参数上"做小规模 CMA-ES 收尾，处理跨组耦合。这一步并不必要，但能挤最后 5% 的 fit。
4. **失败模式自检 + 自动放宽**。算法跑完一段后，如果 `mae_branch_saturated` 长时间 true，自动建议工程师"评分饱和，先修 mask"；如果所有组都 exhausted 但 fit < target，自动把步长 schedule 重置一次再来一遍，避免一次失败就整轮放弃。

### P3 ─ 长期：轻量化的可微代理

如果时间允许，**最终极方案**是搞一个差异化代理模型：用一组采样数据训一个把"参数 → 渲染图"近似的小神经网络（哪怕只在前景区域），这样可以拿到伪梯度，把 100 次黑盒评估换成 1000 次代理评估 + 10 次真实评估校正。这一步成本最高、收益也最高，留作以后路线图。

---

## 6. 立刻可做、风险最低的 4 件事（建议下周完成）

按这个顺序做，每一步都能独立带来明显改善：

1. **加前景 mask**（P0-1）。Laya 截图带 alpha；diff 在 mask 内计算。**预计 fit_score 立刻能从 0.02 量级跳到 0.3+ 量级**。
2. **改 `mae_branch` 为指数映射**（P0-2）。永不饱和，永远有梯度。
3. **`min_improvement` 改成相对阈值** `max(1e-4, 0.5% * current_fit)`（4.4 第 1 条）。让算法在 fit 高低不同阶段都能正常 accept。
4. **输出诊断三件套** `mae_branch_saturated / foreground_ratio / bg_color_delta`（P0-4）。下次再出问题，5 秒就能定位是评分还是优化器。

完成这 4 件事之后再做的下一次实验，预期画面：fit_score 在前 20 轮就能从 0.3 → 0.6，在 100 轮内进入 0.8+。届时优化器是不是要换成 BO，看那次跑出来的瓶颈在哪儿再决定。

---

## 7. 一句话总结

> **这次 42 轮跑没动，根因不是优化器太弱，而是评分函数被 Laya/Unity 截图背景差异骗到了 MAE 饱和区，优化器收到的全是噪声。先修评分，再谈算法。**

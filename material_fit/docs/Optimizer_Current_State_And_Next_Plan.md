# 调整优化算法现状与下一阶段方案

> 状态：中间设计文档。  
> 目的：在继续动优化器代码前，先把当前算法实际怎么运行、为什么效果仍然有限、以及下一步应该按什么顺序改清楚。  
> 范围：本文只讨论真实 Laya 闭环中的参数优化算法。LLM 预分析只作为搜索空间先验，不作为最终调参器。

## 1. 当前算法实际运行过程

当前自动调参主流程在 `fit_material.py::_run_auto_adjustment` 中，整体是一个“单候选、单截图、单评分、单策略提议”的闭环：

```text
initial .lmat params
  -> build strategy
  -> collect current reference/candidate image pair
  -> analyze_image_diff
  -> strategy.propose next_params
  -> write candidate params / .lmat
  -> optionally apply .lmat to real Laya
  -> wait and capture next screenshot
  -> next iteration uses that screenshot as candidate
```

每一轮的关键输入是当前截图与 Unity 参考图的图像差异。`diff_analysis` 会输出：

- `diff_score`：底层差异分数。
- `fit_score`：当前主评分，已经过 E-009 改成 auto-mask + channel-weighted MAE + SSIM。
- `material_channels`：按基础色、阴影、高光、反射、Fresnel、自发光、调色等通道拆开的诊断。
- `adjustment_hints`：每个视觉通道建议增加/减少哪些相关参数。

然后 `strategy.propose(...)` 根据当前 optimizer 生成下一组参数。

## 2. 当前可用的 optimizer

### 2.1 `heuristic`

`heuristic` 是默认生产路径，核心在 `optimizer/adjustment_algorithm.py`。

它先建立固定 stage 表：

- `base_color`
- `shadow_diffuse`
- `specular_smoothness`
- `reflection_matcap`
- `fresnel_emission`
- `global_color_grade`

每轮 `choose_stage` 按 stage 顺序、通道分数、stage 迭代次数和 no-improve 状态选择一个 stage。之后 `propose_next_params` 根据该 stage 的通道 bias 做确定性修正，例如：

- base color 偏亮/偏暗，反向调 `u_BaseColor` 和 `u_Gamma_Power`。
- 暗部偏差调 `u_OcclusionStrength`、`u_GIIntensity`、diffuse ramp。
- 高光偏差调 specular / metallic / smoothness。
- Fresnel / emission stage 最后再处理边缘光和自发光。

它的优点是可解释、稳定、每轮改动少。缺点也很明显：它不是搜索算法，而是反馈控制器。

### 2.2 `cma_cold` / `cma_warm`

CMA-ES 由 `optimizer/cma_es_optimizer.py` 和 `optimizer/strategy.py::CmaesStrategy` 提供。

`ParameterEncoder` 会把 `.lmat` 参数字典编码为连续向量：

- 跳过贴图、`*_ST`、bool、黑名单参数。
- 颜色只暴露 RGB，不动 alpha。
- 根据 shader range 或 fallback range 建边界。
- 支持 `linear`、`log`、`circular`、`color_rgb` 等 transform。

`cma_cold` 从当前 `.lmat` 起步。`cma_warm` 会读取历史 `auto_adjust/iter_*` 的 `(params, fit_score)` 作为 warm-start 样本。

已有合成实验 `Experiment_Phase1_CMA_ES_WarmStart.md` 证明：在异尺度、耦合、多峰的合成目标上，warm-start CMA-ES 明显优于 cold CMA-ES 和 random search。但这只是算法类验证，不等于真实 Laya 闭环已经验证成功。

### 2.3 `semantic_group`

`semantic_group` 是最近加入的原型策略。它依赖 `ShaderEffectGraph`：

- 每个 Laya 参数有 `ParamSemantics`：group、role、transform、gate、dependencies、searchable。
- 每个效果组有 params、gate_params、define_gates、channels、active 状态。
- 新增的模块计划把 Unity 功能模块转成 `suggested_by_unity`、`probe_required`、`search_priority`、`search_params`。

当前 `SemanticGroupStrategy` 做的是低维组内扰动：

1. 按 Unity 建议和 priority 选择效果组。
2. 如果组 inactive 且有 gate，则优先扰动 gate。
3. 否则在组内参数上选一个轴做小步变化。

它现在更像“语义约束的坐标搜索原型”，还不是完整状态机。

## 3. 当前主要问题

### 3.1 仍然是单候选串行搜索

主循环每轮只提出一个 `next_params`，然后等真实 Laya 截图评分。这对昂贵闭环很自然，但会带来两个问题：

- 如果该候选刚好是坏方向，一整轮预算就浪费了。
- 无法比较“同一组参数的多个方向”，所以很难可靠判断一个组是否有效。

对真实 Laya 这种每次评估都贵的黑盒问题，更合适的是小批量候选：例如一个组内正/负方向各 1 个，再加当前 best 回滚。

### 3.2 heuristic 是固定 stage，不是按当前材质动态生成的搜索计划

虽然 `module_plan` 已经能识别 Unity 开启了哪些功能模块，但旧 heuristic 仍使用固定 stage 表。它不知道：

- Unity 是否真的启用了某个模块。
- Laya 对应模块当前是否被 gate/define 关闭。
- 某个 stage 在当前截图里是否可见。
- 某个组应该先激活、先探针，还是直接优化。

这意味着它仍可能把预算花在“Laya 有这个参数，但 Unity 参考不需要”或“Unity 需要，但 Laya 当前没激活”的错误状态上。

### 3.3 `semantic_group` 还没有真正的组级探针闭环

`optimizer/group_probe.py` 已经有候选生成和评估数据结构，但尚未接入 `fit_material.py` 的真实循环。

现在的 `semantic_group` 能在 inactive 组里优先改 gate，但它没有执行完整流程：

```text
activate_gate -> render probe -> measure visual effect -> keep or reject group
```

因此它仍无法回答最关键的问题：某个 Laya 模块打开后，在当前模型、当前截图、当前 shader path 下是否真的影响画面。

### 3.4 缺少回滚和 best-so-far 策略

主循环会记录 `state.best_params`，但候选失败后并不会系统性地回滚到 best 参数继续搜索。很多黑盒优化方法都需要“接受/拒绝”机制：

- 候选提升：接受为 current。
- 候选退化：拒绝，回到 best 或 stage-best。
- 组内连续无效：标记 stuck，换组。

当前流程更像“每轮从上轮候选继续走”，容易把一次坏改动带到后续所有迭代里。

### 3.5 连续变量和离散 gate/define 混在一起处理不足

研究综述已经指出，真实材质拟合是 mixed-integer optimization：

- define、bool、feature 开关、枚举是离散变量。
- uniform float/vector/color 是连续变量。

当前系统对 define/gate 有记录，但尚未把“离散激活”和“连续优化”分成两个阶段。结果是：

- 连续优化可能在关闭的模块里白跑。
- gate 参数可能被当成普通 float 小步优化，而不是“先越过生效阈值再探针”。
- define 还没有真正作为可控动作进入优化状态机。

### 3.6 CMA-ES 仍然不适合直接全局接管低预算真实闭环

合成实验说明 WS-CMA-ES 方向是对的，但也说明 cold CMA-ES 在低预算早期很慢。真实 Laya 中更难：

- 参数维度可能 30-60 轴。
- 每次评估需要写 `.lmat`、等待刷新、截图、打分。
- 截图有噪声，loss 有 domain gap。
- 许多参数有 gate 或强耦合。

所以 CMA-ES 不应该一开始全维盲搜。它更适合作为：

- 组内小维度优化器。
- 已验证 active subspace 的全局收尾。
- 由 heuristic / module probes / history warm-start 的后半段优化器。

### 3.7 评分指标已经明显改善，但仍不是最终答案

E-009 修复了旧 RGB MAE 的核心问题，引入 auto-mask、channel-weighted MAE、SSIM。`Metric_Validation.md` 也指出仍有未解决点：

- region partition 仍主要基于参考图和启发式阈值。
- Fresnel rim 仍缺少真正 silhouette mask。
- severity 阈值未做人类评分校准。
- LPIPS / DISTS 未接入。
- 单视角截图 underdetermines 材质。

这意味着优化算法不能过度相信单一 scalar fit_score，应该更多利用 channel 诊断、组级探针和多轮趋势。

## 4. 根据调研应当采用的下一阶段路线

综合 `RelatedWork_Survey.md`、`Experiment_Phase1_CMA_ES_WarmStart.md`、`Metric_Validation.md` 和当前代码，下一阶段不应是“把 heuristic 换成 CMA-ES”，而应是：

```text
Unity feature prior
  -> module_plan
  -> group activation/probe
  -> group-level pattern search
  -> accepted active subspace
  -> warm-start CMA-ES refinement
```

### 4.1 用 `module_plan` 生成优化状态机

预分析现在能产出：

- Unity 开启了哪些功能模块。
- Laya 哪些模块候选。
- 哪些模块当前 active。
- 哪些模块需要先激活/探针。
- 每组可搜索参数白名单和 priority。

下一步应该让 auto-adjust 不再只按 optimizer 名字直接开始调参，而是先创建一个 `OptimizationPlanState`：

```jsonc
{
  "groups": [
    {
      "group": "fresnel",
      "state": "need_activation",
      "priority": 0.98,
      "gate_params": ["u_FresnelIntensity"],
      "search_params": ["u_FresnelIntensity", "u_FresnelColor", "u_FresnelPow"],
      "probe_result": null
    }
  ]
}
```

### 4.2 先做组级探针，而不是直接优化

对 `suggested_by_unity=true` 的组，尤其是 inactive 组，应先做探针：

1. 从当前 best params 出发。
2. 对 gate 或高杠杆参数做安全扰动。
3. 写 `.lmat`、截图、评分。
4. 如果 mean diff / perceptual diff 超过阈值，标记组为 `probe_passed`。
5. 否则标记为 `inactive_or_invisible`，本轮不再优化它。

这个步骤的目标不是追求更像 Unity，而是验证“这个组对画面有控制权”。

### 4.3 组内优化先用 pattern search / coordinate search

组级探针通过后，先不要上 CMA-ES。组内维度通常 3-8 个，最稳的第一版应是 pattern search：

```text
for group in priority_order:
  for step_size in [large, medium, small]:
    evaluate +step for one or several axes
    evaluate -step for one or several axes
    accept best candidate if improves
    shrink step if no direction improves
```

原因：

- 比当前单轴坐标搜索更可靠。
- 比 CMA-ES 更省样本。
- 每个候选都有明确解释。
- 可以自然支持接受/拒绝和回滚。

### 4.4 CMA-ES 作为 active subspace 收尾

当若干组已经通过探针并完成初步 pattern search 后，再把这些组的有效参数合并为 active subspace。

此时跑 `cma_warm` 才合理：

- 参数维度更低。
- gate 已经打开。
- 无效组已经剔除。
- 初始样本来自真实组内搜索历史。
- covariance 学到的是有效参数之间的耦合，而不是所有 shader 参数的噪声。

这与 Nomura 2021 WS-CMA-ES 的思想一致：关键不是“有 CMA-ES”，而是“有好的 prior 和有意义的初始分布”。

### 4.5 define / gate 作为离散动作单独处理

短期不需要完整 DiffMat v2 式 mixed-integer optimizer，但应该把离散动作显式化：

- `activate_define`
- `deactivate_define`
- `set_gate_min_nonzero`
- `probe_discrete_feature`
- `commit_or_revert`

连续优化器只处理已经确认开启并有效的 uniform 参数。

### 4.6 指标层继续作为优化前提，而不是算法内部魔法

在算法比较前，必须满足：

- Laya refresh probe 通过。
- 背景统一或 E-011 背景归一化开启。
- 使用 E-009 perceptual score。
- target_score 重新校准，不沿用旧 RGB MAE 时代的 0.9。

否则任何 optimizer 对比都会被错误指标污染。

## 5. 推荐下一步实施顺序

### Step 1：落地真实组级探针闭环

这是优先级最高的一步，因为它直接减少无效搜索空间。

产物：

- `auto_adjust/group_probe.json`
- 每组 `probe_candidate`
- 每组 `probe_result`
- UI 展示“已验证生效 / 无可测变化 / 被门控 / 当前视角不可见”

验收标准：

- `fish_1580` 能对 `emission`、`fresnel`、`color_grade`、`specular_smoothness` 等组生成探针。
- 探针结果能改变后续 optimizer 的组选择。

### Step 2：把 `semantic_group` 改成状态机

当前 `semantic_group` 是“每轮选组 + 单轴小步”。下一版应该是：

```text
init
  -> activate_gate
  -> probe_group
  -> optimize_group_pattern_search
  -> accept_or_reject
  -> mark_done_or_stuck
  -> next_group
```

验收标准：

- 每个组有明确状态。
- 候选失败会回滚。
- 连续失败会换组。
- `decision.json` 能解释每一步为什么改这个参数。

### Step 3：实现组内 pattern search

先从每组 3-8 个参数做低维搜索，不急着全局 CMA。

建议第一版：

- 每轮只评估一个组。
- 每个 step 对 1-2 个高优先级轴做正/负候选。
- 接受最佳提升，否则 shrink step。
- stage/group 级别保存 best params。

验收标准：

- 相同 eval budget 下，优于当前 `semantic_group` 单轴扰动。
- 不出现明显破坏性过调。

### Step 4：active subspace CMA-ES 收尾

在组级探针和 pattern search 之后，才启动 CMA-ES。

验收标准：

- CMA 的 `param_whitelist` 只包含 probe passed 的组。
- warm-start history 来自刚刚的真实组内搜索。
- 与全维 `cma_warm` 做同预算对照。

### Step 5：做真实对照实验

在 `fish_1580` 上跑相同预算，例如 50 eval：

- heuristic
- current cma_warm
- current semantic_group
- new semantic_group_state_machine
- new semantic_group_state_machine + active_subspace_cma

比较：

- 最终 fit_score。
- 每 10 eval 提升曲线。
- 人眼截图。
- 改动参数数量。
- 回滚次数。
- 探针剔除的组数量。

## 6. 一句话结论

当前系统已经具备真实闭环、评分、CMA-ES、语义图、LLM 功能模块先验这些组件，但它们还没有组合成一个真正高效的优化算法。

下一步最重要的不是继续让 LLM 更聪明，也不是直接让 CMA-ES 全维搜索，而是把 `module_plan` 变成一个真实执行的优化状态机：

> 先验证哪些组真的生效，再在有效组里做低维可回滚搜索，最后用 warm-start CMA-ES 在 active subspace 中收尾。

这条路线最符合我们当前问题的约束：真实 Laya 评估昂贵、参数强耦合、gate/define 离散、Unity/Laya 数值不可直接迁移。

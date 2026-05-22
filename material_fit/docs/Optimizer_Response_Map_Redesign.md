# 优化器框架复盘与参数-指标响应图重构方案

> 状态：设计分析文档  
> 目的：回答当前自动材质拟合优化器到底如何工作、使用了哪些研究方法、为什么现阶段仍然显得零散，以及下一步如何把这些模块统一为更有机的参数-指标响应模型。  
> 范围：本文只讨论非神经、真实 Laya 在环的黑盒优化。神经网络可作为长期先验或代理模型，但不作为当前主路径。

---

## 1. 当前问题的本质

我们的任务可以形式化为：

```text
theta* = arg max score( R_laya(theta), I_ref )
```

其中：

- `theta` 是 Laya shader/material 的可写参数。
- `R_laya` 是真实 Laya 渲染器，在当前工具链里不可导、评估昂贵。
- `I_ref` 是 Unity 或已知正确 Laya 参数渲染出来的参考图。
- `score` 是多视角、多指标聚合后的视觉相似度。

这不是普通的“调几个滑条”，而是一个 **昂贵、不可导、多指标、高耦合、带门控的黑盒优化问题**。

当前测试模板理论上可达 100%，说明：

1. 渲染闭环和评分上限是存在的。
2. 目前卡住不是因为目标不可达。
3. 主要问题在搜索策略：算法不知道如何从当前参数走向正确参数。

---

## 2. 当前完整优化框架

当前系统大致由六层组成。

### 2.0 三路优化器对照组

为了避免继续把所有改动叠进同一个复杂算法，当前工具显式支持三条 optimizer 路线：

| optimizer | 定位 | 主要用途 |
| --- | --- | --- |
| `semantic_group` | 当前 `ResponseMap + ExperimentPlanner` response scheduler。新增单参预算上限、no-effect cooldown、近期参数预算占比审计，并在 plateau 时优先 `archive_restart / subspace_batch / pair_probe`。 | 验证响应图调度是否能避免单参塌缩。 |
| `semantic_group_legacy_081` | 从旧高分时期恢复的 pattern-search 语义组算法，保留 `probe_group / pattern_search / cross_group_combo`，不接入 ResponseMap。 | 复现 0.8 附近历史基线，作为稳定对照组。 |
| `subspace_cma_es` | 昂贵但效果优先的黑盒路线，在当前 active semantic subspace 内运行 CMA-ES ask/tell。 | 用 500-1000 轮预算测试更强局部联合搜索能力。 |

这三条路线的研究意义不同：`semantic_group` 用来验证新的参数-指标响应模型，`semantic_group_legacy_081` 用来确认我们没有丢失旧框架的有效性，`subspace_cma_es` 用来评估更昂贵黑盒算法在同一真实渲染预算下的上限。

### 2.1 输入与项目层

由 UI/backend 管理项目：

- Unity 参考图、多视角截图。
- Laya shader / material / `.lmat` 参数。
- Laya 可搜索控件 schema。
- auto-adjust job 和每轮 iteration 记录。

关键目标是把实验组织成可复现的 job/run/iteration 结构。

### 2.2 语义预分析层

相关代码：

- `optimizer/semantic_graph.py`
- `optimizer/llm_semantics.py`
- `backend/preanalysis.py`
- `backend/project_store.py`

它会把原始 shader 参数转换成 `ShaderEffectGraph`：

```text
raw shader params
  -> ParamSemantics
  -> ShaderEffectGroup
  -> searchable params / active params / gated params
```

每个参数会被标注：

- 所属 group，例如 `base_color`、`shadow_diffuse`、`specular_smoothness`、`fresnel`。
- role，例如 color、intensity、shape、gate。
- transform，例如 linear、log、circular、color_rgb。
- searchable 与否。
- gate / dependency。

这层借鉴的是 **shader semantic prior / mixed search space construction** 的思想：先把无结构参数表转成有结构的搜索空间。

### 2.3 图像评分与指标层

相关代码：

- `vision/research_metrics.py`
- `vision/diff_analysis.py`
- `auto_adjust/scoring.py`

当前评分不再只看 RGB MAE，而是包含：

- CIEDE2000 / Lab 颜色差。
- luminance MAE / bias。
- SSIM-L。
- gradient / detail texture。
- highlight / worst view。
- soft-saturating guidance score。

研究方法来源：

- perceptual image metrics。
- multi-objective diagnostic metrics。
- soft normalization / saturating loss。

这层已经比早期合理很多，但当前优化器仍然没有充分利用“哪个指标被哪个参数改善”这一信息。

### 2.4 候选生成层

相关代码：

- `optimizer/response_driven_strategy.py`
- `optimizer/experiment_planner.py`
- `optimizer/candidate_builder.py`
- `optimizer/scheduler_types.py`

当前候选大致来自几类：

1. `calibration_probe` 参数方向探针。
2. `single_param` 单参数局部搜索。
3. `pair_probe` 参数对联合探针。
4. `subspace_batch` 低维稀疏联合候选。
5. `archive_restart` 多起点分支重启。

这些方法分别借鉴了：

- coordinate search / response surface exploration。
- archive-based restart。
- low-dimensional subspace search。

问题是：这些候选现在主要由启发式规则串起来，缺少一个统一的“为什么选这些参数”的核心模型。

### 2.5 经验记录与排序层

相关代码：

- `optimizer/search_evidence.py`
- `ParamInfluenceTracker`
- `InfluenceTracker`
- `TopKArchive`
- `AcceptancePolicy`

当前已经有一些 evidence：

- group 对 metric component 的 EMA 影响。
- parameter 的 accepted count、attempt count、fit gain EMA、risk EMA。
- Top-K archive 记录历史好点。
- acceptance policy 判断是否接受候选。

这层借鉴了：

- multi-armed bandit 的 exploration/exploitation。
- online influence tracking。
- archive-based black-box optimization。

但它目前仍偏“记账”，不是完整的响应模型。它知道某个参数“过去可能有用”，但不知道：

- 这个参数主要影响哪个指标。
- 正方向还是负方向有效。
- 在什么上下文下有效。
- 哪些参数必须组合才有效。
- 组合贡献如何归因。

### 2.6 搜索状态与保护层

相关代码：

- `optimizer/branch_guard.py`
- `optimizer/response_driven_strategy.py` 中 checkpoint / branch drift 逻辑。

当前做过几轮改动：

1. 最初使用 best guard，低于 best 就容易回滚。
2. 后来将 best 降级为 checkpoint，允许 branch 暂时低于 best。
3. 加入 drift guard，防止 branch 跑飞。

研究方法来源：

- trust-region methods。
- incumbent / checkpoint guard。
- restart strategies。

但最新实验说明：只靠 checkpoint/drift 不能判断“穿越低谷”还是“越走越偏”。它只能控制风险，不能提供方向知识。

---

## 3. 当前使用过的核心研究方法

### 3.1 黑盒优化

因为 Laya renderer 不可导，每次评估需要真实截图，所以主问题是 black-box optimization。

对应方法：

- CMA-ES。
- pattern search。
- coordinate search。
- trust-region branch。
- archive restart。

当前使用方式：混合启发式调用，而不是统一建模。

### 3.2 Warm-start / archive

CMA-ES warm-start 参考 Nomura et al. AAAI 2021 的 Warm-Started CMA-ES 思路。我们也有 Top-K archive 来保存历史好解。

当前问题：archive 主要用于 restart 和展示，不足以指导“下一步该往哪个方向走”。

### 3.3 Multi-armed bandit

参数 ranking 中使用：

- 尝试次数。
- accepted 次数。
- 历史收益。
- risk。
- exploration bonus。

这类似 bandit 中的 exploration/exploitation 平衡。

当前问题：bandit 适合选 arm，但不擅长学习连续参数空间里的方向和耦合。

### 3.4 Trust region

trust region 的思想是：不要全空间乱跑，而是在局部半径内探索；成功则扩大，失败则缩小。

当前问题：我们实现了 branch/radius 的概念，但没有真正建立局部响应面。因此 trust region 仍然是“规则化探索”，不是“模型化局部优化”。

### 3.5 Multi-objective / component-aware scoring

当前评分已经拆成多个 component：

- color。
- luminance。
- structure。
- highlight。
- detail。
- worst view。

当前问题：优化器最后仍然经常退化为“总分涨没涨”。component 没有变成参数选择的核心依据。

### 3.6 Active subspace

我们已经有“从高优先级参数中选小子空间”的思路。

当前问题：子空间选择主要来自语义和历史排序，没有通过批量实验验证这个子空间是否真的包含上升方向。

---

## 4. 为什么当前设计显得零散

当前模块很多，但它们没有围绕一个统一中心工作。

可以概括为：

```text
semantic graph 负责解释参数
metrics 负责评分
ranking 负责排序
archive 负责存历史好点
branch guard 负责防止跑飞
breakthrough 负责尝试联合优化
```

问题是：这些模块之间缺少一张共享的“因果/响应表”。

它们没有共同回答这个核心问题：

```text
如果我改变参数 p，指标 m 会往哪个方向变化，变化幅度大概是多少，风险是什么？
```

因此算法表现为：

- group 阶段看起来有逻辑，但容易和真实瓶颈脱节。
- ranking 有分数，但证据来源分散且弱。
- checkpoint 能保护结果，但不能告诉算法下一步怎么走。
- breakthrough/subspace batch 能生成组合，但缺少选择和解释依据。
- archive 记录了历史好解，但没有形成局部响应面。

最终就是“组件很多，但没有形成学习闭环”。

---

## 5. 下一步应该改成什么

下一步不建议继续增加阶段，而应该建立统一的：

```text
Parameter-Metric Response Map
参数-指标响应图
```

它应该成为优化器的中心状态。

### 5.1 响应图记录什么

每次真实渲染之后，都记录：

```text
context:
  current params
  active gates
  current bottleneck metrics
  current score band

trial:
  changed params
  delta params
  direction
  step size
  candidate kind

result:
  delta score
  delta metric components
  worst view change
  accepted/rejected
```

然后更新：

```text
param -> metric -> response statistics
```

例如：

```text
u_Saturation:
  color_mean:
    positive_dir_gain_ema: +0.012
    negative_dir_gain_ema: -0.004
    success_rate: 0.58
    risk_to_luminance: 0.006

u_SpecularPower:
  highlight:
    positive_dir_gain_ema: +0.020
    risk_to_structure: 0.009
```

### 5.2 证据分级

不同候选的归因可信度不同。

| 证据类型 | 可信度 | 用途 |
|---|---:|---|
| 单参数 +/- 探针 | 高 | 更新 `param -> metric` 主响应 |
| 两参数 pair | 中 | 学习耦合与交互 |
| 3-6 参数 subspace batch | 中低 | 估计局部响应面 |
| 大范围 archive restart | 低 | 只记录结果，不强归因 |

不能把所有 trial 一视同仁。否则联合候选会污染单参数响应图。

### 5.3 用响应图替代阶段调度

当前流程：

```text
select group/stage
  -> choose param
  -> generate candidate
  -> accept/reject
```

建议改为：

```text
compute current bottleneck metrics
  -> query response map for params likely to improve bottleneck
  -> choose candidate type
  -> render/evaluate
  -> update response map
```

group 不再是主调度单位，只作为语义辅助：

- 限定参数是否 active。
- 提供 transform/range。
- 解释参数含义。
- 避免无意义组合。

真正的主调度应由 response map 决定。

---

## 6. 新优化器推荐架构

建议拆成四个核心模块。

### 6.1 `ResponseMap`

职责：

- 记录每个参数对每个 metric 的方向性影响。
- 区分单参数证据、pair 证据、subspace 证据。
- 维护成功率、平均收益、风险、最近趋势。

接口示例：

```python
response_map.observe_trial(trial, before_metrics, after_metrics)
response_map.rank_params(bottleneck, active_params)
response_map.rank_pairs(bottleneck, active_params)
response_map.summarize_param(param_name)
```

### 6.2 `ExperimentPlanner`

职责：

- 根据当前瓶颈和 response map 选择候选。
- 决定是做单参数、pair、还是 subspace batch。
- 决定方向和步长。

策略：

```text
早期：覆盖式单参数 probe
中期：响应图驱动的 coordinate / pair search
停滞：局部 subspace batch
失败：换子空间或 archive restart
```

### 6.3 `LocalResponseModel`

职责：

- 对最近一批 subspace batch 样本做轻量拟合。
- 不需要神经网络，可先用 ridge regression / weighted linear model。
- 估计局部上升方向。

示意：

```text
X = normalized parameter deltas
y = metric/score deltas
fit y ~= X beta
```

输出：

- 哪些方向可能提升总分。
- 哪些方向改善主瓶颈但风险高。
- 当前子空间是否值得继续。

### 6.4 `SearchController`

职责：

- 管理 checkpoint。
- 管理 branch budget。
- 管理 stop/restart。
- 但不负责“猜参数”。

这样 checkpoint 只是保护层，不再是优化知识来源。

---

## 7. 新主循环

建议改成：

```text
for each iteration:
  1. analyze current render -> MetricVector
  2. update ResponseMap with previous trial result
  3. determine bottleneck metrics
  4. ExperimentPlanner selects next trial:
       - probe
       - coordinate
       - pair
       - subspace batch member
       - archive restart
  5. apply candidate and render
  6. checkpoint only records best / detects catastrophic drift
```

关键变化：

- 不再围绕 group stage 旋转。
- 不再靠 best guard 判断方向。
- 不再靠 breakthrough 作为特殊阶段。
- 所有搜索行为都服务于 response map 的建立和利用。

---

## 8. 如何判断“穿越低谷”还是“越走越偏”

单靠分数阈值无法可靠判断。

必须看证据：

### 8.1 低谷可继续的条件

即使总分下降，也必须满足至少一个条件：

1. 主瓶颈 metric 持续改善。
2. LocalResponseModel 预测继续沿某方向有恢复机会。
3. subspace batch 中存在同方向更高分样本。
4. 最近几轮形成恢复斜率。

否则不能称为“穿越低谷”。

### 8.2 应停止分支的条件

以下情况说明更像“越走越偏”：

1. 总分下降，主瓶颈也没有改善。
2. response map 对当前方向的收益估计为负。
3. 最近 K 轮只有微小 delta 或近似无变化。
4. 多个核心指标同时恶化。
5. 子空间 batch 样本整体低于 checkpoint 且没有局部上升方向。

也就是说，应该停止的不是“低分”，而是“低分且没有方向证据”。

---

## 9. 相关工作依据

### 9.1 Inverse Rendering / Appearance-Driven Optimization

相关工作如 nvdiffmodeling、nvdiffrec、appearance-driven simplification 都说明：

- 图像监督可以驱动材质/外观参数拟合。
- 需要让视觉 loss 和可解释参数空间之间建立桥梁。

区别是：这些方法多依赖可微 renderer，而我们当前是不可导生产引擎在环。因此不能直接照搬梯度下降，只能借鉴“图像监督 + 参数化外观模型”的问题设定。

### 9.2 CMA-ES 与 Warm-start

CMA-ES 是经典无梯度连续优化方法，适合非凸、非线性、不可导问题。Warm-start CMA-ES 说明历史好样本能显著减少搜索成本。

我们的启示：

- CMA-ES 不适合一开始全维盲搜。
- 更适合在 response map 选出的低维 active subspace 内使用。

### 9.3 Pattern Search / MADS / Direct Search

Pattern search 和 MADS 适合昂贵黑盒问题，核心是：

- 在一组方向上试探。
- 有改善则沿方向扩展。
- 无改善则缩小步长或换方向。

我们的启示：

- 单参数坐标搜索应变成 response-map-guided direct search。
- 方向集合不能固定，应由参数-指标响应图动态生成。

### 9.4 Bayesian Optimization / TuRBO

Bayesian optimization 和 TuRBO 的思想是：

- 用少量样本建立局部 surrogate。
- 在 trust region 内优化 acquisition。
- 局部失败则收缩或重启。

我们的启示：

- 当前不必立刻引入完整 GP。
- 可以先用 ridge regression / local linear response model 替代。
- 子空间 batch 是建立 surrogate 的最小前提。

### 9.5 Sensitivity Analysis / Response Surface Methodology

响应面方法和灵敏度分析关注：

- 输入变量变化如何影响输出指标。
- 哪些参数是一阶主效应。
- 哪些参数有交互效应。

这正是我们现在缺的东西。

我们的 `Parameter-Metric Response Map` 本质上就是一个在线、低预算、面向 shader 参数的响应面/灵敏度模型。

### 9.6 Multi-armed Bandit

Bandit 方法适合做参数选择：

- 哪个参数值得继续试。
- 哪个参数尝试不足。
- 哪个参数历史收益高。

但 bandit 不足以解决连续方向和参数耦合。因此它应作为 `ExperimentPlanner` 的排序证据之一，而不是主算法。

---

## 10. 推荐实施路线

### P0：保留现有系统，但新增 ResponseMap

不立即推翻现有策略，先新增模块：

```text
optimizer/response_map.py
```

并让每轮 trial 更新它。

目标：

- UI 能看到 `param -> metric` 映射。
- ranking 来源变成 response map。
- 当前 ParamInfluenceTracker 逐步迁移进去。

### P1：重写参数选择逻辑

把 `_param_ranking()` 改成：

```text
active params
  + current bottleneck
  + response map
  + exploration bonus
  + risk penalty
```

输出：

- top single params。
- top pair candidates。
- uncertain params needing probe。

### P2：建立局部 subspace batch

当单参数收益停滞时：

1. 从 response map 选 4-6 个参数。
2. 生成 16-32 个低维候选。
3. 用真实渲染得到 batch samples。
4. 拟合 LocalResponseModel。
5. 选择下一步方向或放弃子空间。

注意：这不是“大突破阶段”，而是常规优化循环的一部分。

### P3：简化阶段系统

当前实现已经把主调度从旧的 group/phase machine 切换为统一的
`ExperimentPlanner`。`SemanticGroupStrategy` 现在只是兼容外观层，负责
接收上一轮真实渲染结果、更新 `ResponseMap` / archive / drift guard，并把
下一轮请求交给 planner。

已经从主路径下线：

- fixed group cycle。
- breakthrough phase。
- group exhausted。

保留并重新定位：

- semantic graph。
- gate activation。
- parameter transforms。
- checkpoint/archive。

主调度转为：

```text
bottleneck -> response map -> candidate planner -> trial
```

实际 trial 类型为：

- `calibration_probe`：给高优先级 active 参数补正/负方向证据。
- `single_param`：沿已验证的稳定方向继续局部搜索。
- `pair_probe`：当单参数响应弱、上下文敏感或存在交互嫌疑时测试参数对。
- `subspace_batch`：plateau 时测试稀疏低维联合候选。
- `archive_restart`：长期 plateau 或 drift 后从 Top-K archive 选择新分支。

这意味着 breakthrough 不再是一个特殊“后期阶段”，而是被拆成普通的
`pair_probe`、`subspace_batch` 和 `archive_restart` 实验类型；每种实验都
必须能解释选择依据和预算状态。

### P4：UI 可解释化

新增一个面板：

```text
参数-指标响应图
```

展示：

- 当前最大瓶颈。
- Top 参数为什么被选中。
- 参数主要改善哪些指标。
- 风险指标是什么。
- 单参数证据/组合证据数量。
- 当前子空间是否可靠。

这样用户能确认算法不是“黑盒乱试”。

---

## 11. 最终建议

当前系统最应该改变的不是某个阈值或某个阶段，而是优化器的核心抽象。

旧核心抽象：

```text
stage/group -> candidate -> accept/reject
```

建议的新核心抽象：

```text
parameter-metric response -> candidate planning -> local evidence update
```

简言之：

> 不要再让算法主要回答“下一阶段调哪组参数”。  
> 应该让算法持续回答“哪个参数在当前上下文下最可能改善当前瓶颈指标，并且风险最低”。

这会把已有模块有机连接起来：

- semantic graph 提供参数含义和有效性。
- metrics 提供目标分解。
- response map 学习参数到指标的影响。
- candidate planner 用 response map 生成候选。
- checkpoint/archive 只负责安全与重启。
- subspace batch 负责学习耦合，而不是作为特殊突破阶段。

如果这个重构完成，优化器才会真正从“规则堆叠”进入“证据驱动搜索”。


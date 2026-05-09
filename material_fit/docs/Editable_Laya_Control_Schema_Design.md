# 可编辑 Laya Shader 控件分类 Schema 设计

> 状态：设计文档。  
> 目的：把“程序/LLM 自动初筛 + 人类 shader 工程师修正 + 下游优化器消费”的完整控制空间设计固定下来。  
> 背景：当前工具已经能在运行控制台显示 Laya shader 暴露参数的语义分组，并允许按组启停。但 `FishStandard` 评审已经证明自动分类仍会出现错分、漏分、注释误读、gate 表达不足等问题。因此下一阶段需要把控件分类从只读结果升级为可编辑、可保存、可复用的 schema。

## 1. 核心判断

这个工具不应假设自动分类永远正确。更合理的工程模式是：

```text
程序规则 / curated schema / LLM
  -> auto_laya_control_schema
  -> 人类 shader 工程师校正
  -> manual_laya_control_schema
  -> effective_laya_control_schema
  -> group probe / optimizer / fit_config
```

自动分类只是一份草稿。只要人类工程师做过修正，下游优化算法必须以人类确认后的 `effective_laya_control_schema` 为准。

这不是退回纯人工，而是让人工经验参与“控制空间定义”，而不是参与每一轮参数数值猜测。人工只需要做少量高价值判断：

- 这个参数属于哪个功能组。
- 这个功能组是否应该参与本次优化。
- 这个参数是否可搜索，还是只展示。
- 这个参数是否是 gate / define / mask / shared texture。
- 是否需要新增一个自动分类没识别出来的组。

## 2. 设计目标

### 2.1 必须支持

- 参数可以从一个分类移动到另一个分类。
- 分类可以重命名。
- 分类可以新增。
- 分类可以删除或隐藏。
- 每个分类可以启用/禁用本次优化。
- 每个参数可以设置是否参与搜索。
- 每个参数可以标记 role，例如 `color`、`intensity`、`shape`、`gate`、`texture`、`mask`、`shared`、`fixed`。
- 人工修正必须持久化到 `project.json`，不能因为重新 preanalysis 丢失。
- 重新运行自动分类或 LLM 时，不能覆盖人工锁定结果。
- 下游 optimizer 只消费 effective schema，不直接消费自动 schema。

### 2.2 暂时不做

- 不在第一版里直接编辑 `.lmat` 参数值。
- 不在第一版里实现复杂 node graph UI。
- 不强制一开始就做真正拖拽；可以先用“移动到”下拉框完成，后续再升级拖拽。
- 不要求自动分类一次达到 100% 准确。

## 3. 三层数据模型

### 3.1 `auto_laya_control_schema`

来源：

- 当前 `preanalysis.laya_control_groups`
- `ShaderEffectGraph`
- shader parser 的 uniformMap / defines / hidden
- curated schema
- LLM 输出

特点：

- 可重复生成。
- 不手动编辑。
- 每次 preanalysis 可以覆盖。
- 用于给人类一个初始草稿。

建议结构：

```jsonc
{
  "schema_version": 1,
  "source": {
    "kind": "auto",
    "generator": "rules+llm+curated",
    "shader_name": "Custom/Fish/FishStandard",
    "shader_path": "..."
  },
  "groups": [
    {
      "id": "fresnel",
      "label": "Fresnel / 边缘光",
      "description": "控制轮廓边缘光和视角相关亮边",
      "enabled": true,
      "locked": false,
      "order": 40,
      "controls": [
        {
          "name": "u_FresnelIntensity",
          "role": "gate",
          "searchable": true,
          "transform": "log",
          "range": [0, 8],
          "gates": [],
          "dependencies": [],
          "reason": "auto: name prefix fresnel"
        }
      ]
    }
  ]
}
```

### 3.2 `manual_laya_control_schema`

来源：

- 人类在 UI 上移动参数、改名、添加/删除组、修改 searchable、启停组。

特点：

- 存在 `project.json`。
- 不因 preanalysis 自动覆盖。
- 记录的是人工 override，不一定需要复制完整自动 schema。
- 需要记录哪些字段被人工锁定。

建议结构：

```jsonc
{
  "schema_version": 1,
  "base_auto_hash": "sha256-of-auto-schema",
  "groups": {
    "fresnel": {
      "label": "Fresnel / 边缘光",
      "enabled": true,
      "locked": true,
      "order": 40
    },
    "normal": {
      "label": "Normal / 法线",
      "enabled": false,
      "locked": true,
      "order": 90,
      "created_by_user": true
    }
  },
  "controls": {
    "u_FresnelSmooth": {
      "group": "fresnel",
      "role": "shape",
      "searchable": true,
      "locked_fields": ["group", "role"],
      "note": "人工修正：该参数属于 Fresnel，不属于 specular_smoothness"
    },
    "u_MluAlbedoColor": {
      "group": "specular_smoothness",
      "role": "intensity",
      "locked_fields": ["group"],
      "note": "实际用于 specular color 与 albedo 混合"
    },
    "u_MAER": {
      "group": "packed_maer",
      "role": "shared_texture",
      "searchable": false,
      "locked_fields": ["group", "role", "searchable"]
    }
  },
  "deleted_groups": ["misc_auto_old"],
  "hidden_controls": ["u_EmissionPower"]
}
```

### 3.3 `effective_laya_control_schema`

来源：

```text
auto_laya_control_schema + manual_laya_control_schema
```

特点：

- 下游唯一可信输入。
- 写入 `preanalysis.json` 和 `fit_config.json`。
- optimizer、group probe、UI 运行控制台都读它。
- 可以每次生成，不需要人工直接编辑。

合并原则：

1. 自动 schema 先生成完整控件全集。
2. 人工新增 group 合并进去。
3. 人工重命名 group 覆盖自动 label。
4. 人工移动 control 覆盖自动 group。
5. 人工 role/searchable/gate 覆盖自动字段。
6. 人工隐藏的 control 不进入 optimizer，但可以保留在 UI 的“隐藏/已排除”区。
7. 删除 group 不应删除参数本身，而应把组内参数移动到 `unassigned` 或标记 hidden，防止数据丢失。
8. 下游 optimizer 只读取 `enabled=true && searchable=true` 的 controls。

## 4. UI 设计

### 4.1 位置

主入口应放在 `运行控制台`，因为它是开跑前决定“这次优化范围”的地方。

预分析页可以保留自动分类摘要，但不应作为主要编辑入口。预分析页回答“自动理解结果是什么”，运行控制台回答“本次实际让优化器调什么”。

### 4.2 运行控制台的两个模式

#### 简洁模式：优化范围选择

默认展示：

- group 开关。
- group label。
- 参数数量 / 可搜索数量 / gate 数量。
- Unity/LLM 建议标记。
- 当前 active / probe_required。
- 展开后显示参数 chip。

这个模式服务“快速开始自动调参”。

#### 编辑模式：Schema Editor

点击“编辑分类”进入：

- 左侧：分类列表。
- 右侧：当前分类内参数列表。
- 参数可拖动到其他分类。
- 每个参数可点开编辑 role/searchable/transform/gate/note。
- 分类可重命名、新增、删除、调整顺序。
- 支持搜索参数名。
- 支持“只看未确认 / 只看 misc / 只看自动低置信度”。

第一版可以不做拖拽，用这些按钮替代：

- `移动到...`
- `新建分类并移动`
- `标记不可搜索`
- `标记为 gate`
- `锁定此参数分类`

后续再把“移动到”升级为 drag-and-drop。

### 4.3 人工修改提示

每个参数旁边应显示来源：

- `auto`：自动分类。
- `llm`：LLM 建议。
- `curated`：项目内置规则。
- `manual`：人工修正。

如果 manual 覆盖了 auto，UI 应显示：

```text
manual: fresnel
auto: specular_smoothness
```

这能帮助工程师知道自己改过什么，也能帮助后续 debug 自动分类质量。

### 4.4 防误操作

- 删除 group 前提示：组内参数会移动到 `unassigned`，不会删除 shader 参数。
- 隐藏参数前提示：隐藏参数不会进入 optimizer。
- 对 texture、ST、alpha/cutoff、light direction 这类高风险参数，默认 `searchable=false`。
- 提供“恢复自动分类”按钮，但要二次确认。
- 提供“只恢复未人工锁定字段”按钮，用于重新跑 LLM 后吸收新建议。

## 5. 后端 API 设计

### 5.1 保存人工 schema

```http
PUT /api/projects/{project_id}/laya_control_schema
```

请求：

```jsonc
{
  "manual_laya_control_schema": {
    "groups": {},
    "controls": {},
    "deleted_groups": [],
    "hidden_controls": []
  }
}
```

返回：

```jsonc
{
  "auto_laya_control_schema": {},
  "manual_laya_control_schema": {},
  "effective_laya_control_schema": {}
}
```

### 5.2 局部编辑 API

也可以提供更细粒度 API，方便 UI 逐项保存：

```http
PATCH /api/projects/{project_id}/laya_control_schema/control/{param_name}
PATCH /api/projects/{project_id}/laya_control_schema/group/{group_id}
POST  /api/projects/{project_id}/laya_control_schema/groups
DELETE /api/projects/{project_id}/laya_control_schema/groups/{group_id}
```

第一版为了简单，可以只做一个 `PUT` 全量保存。

### 5.3 重新生成有效 schema

每次以下事件发生时重新生成：

- 运行 preanalysis。
- 保存 manual schema。
- 生成 fit_config。
- 运行控制台加载项目。

## 6. Project JSON 持久化

建议在 `project.json` 新增：

```jsonc
{
  "manual_laya_control_schema": {
    "schema_version": 1,
    "base_auto_hash": "...",
    "groups": {},
    "controls": {},
    "deleted_groups": [],
    "hidden_controls": []
  },
  "algorithm_config": {
    "laya_control_group_overrides": {
      "fresnel": { "enabled": true },
      "normal": { "enabled": false }
    }
  }
}
```

其中：

- `manual_laya_control_schema` 表达长期分类修正。
- `algorithm_config.laya_control_group_overrides` 表达“本次运行是否启用某组”。

两者不要混在一起。原因是：

- 分类修正是知识资产，应该长期保留。
- 本次启停是实验配置，可能每次运行不同。

## 7. Preanalysis 输出

`preanalysis.json` 应包含：

```jsonc
{
  "auto_laya_control_schema": {},
  "manual_laya_control_schema": {},
  "effective_laya_control_schema": {},
  "laya_control_groups": []
}
```

兼容策略：

- `laya_control_groups` 可以继续保留，作为旧 UI 的扁平视图。
- 新 UI 应优先读 `effective_laya_control_schema`。
- 如果没有 manual schema，则 effective 等于 auto。

## 8. Fit Config 输出

`fit_config.json` 应包含：

```jsonc
{
  "effective_laya_control_schema": {},
  "effect_graph": {},
  "module_plan": {}
}
```

生成规则：

- 从 effective schema 生成 `effect_graph.params[*].group`。
- 从 effective schema 生成 `effect_graph.params[*].searchable`。
- 从 enabled groups 生成 optimizer whitelist。
- disabled group 的 searchable 参数全部置 false。
- hidden controls 不进入 optimizer。
- gate controls 可以进入“离散/激活状态机”，但不应被普通连续优化器无脑小步搜索。

## 9. Optimizer 消费规则

### 9.1 通用过滤

任何 optimizer 都应先拿到：

```text
searchable_params = controls where group.enabled && control.searchable
```

### 9.2 `heuristic`

旧 heuristic stage 表应过滤掉不在 `searchable_params` 的参数。

如果一个 stage 过滤后没有参数，则跳过该 stage。

### 9.3 `semantic_group`

`semantic_group` 应以 effective schema 的 group 顺序和 enabled 状态为准。

状态机顺序：

```text
enabled group
  -> gate controls
  -> probe controls
  -> searchable controls
  -> done/stuck
```

### 9.4 `cma_warm` / `cma_cold`

CMA 的 `ParameterEncoder.param_whitelist` 应来自 effective schema，而不是自动 `effect_graph.active_search_params()`。

### 9.5 Group Probe

组级探针只对 enabled group 生成候选。

如果 group 有 gate controls，探针先测试 gate。

如果 group 是 shared texture / alpha / normal 等默认风险组，除非人工启用，否则不探针。

## 10. Schema 合并算法

伪代码：

```python
def build_effective_schema(auto_schema, manual_schema, run_overrides):
    effective = deepcopy(auto_schema)

    for group_id, patch in manual_schema.groups.items():
        if group_id not in effective.groups:
            effective.groups[group_id] = new_group(group_id)
        merge_group_fields(effective.groups[group_id], patch)

    for param_name, patch in manual_schema.controls.items():
        control = find_control_or_create_unassigned(effective, param_name)
        if "group" in patch:
            move_control(effective, param_name, patch["group"])
        merge_control_fields(control, patch)
        control.source = "manual" if patch else control.source

    for group_id in manual_schema.deleted_groups:
        move_controls_to_unassigned(effective, group_id)
        hide_group(effective, group_id)

    for param_name in manual_schema.hidden_controls:
        mark_hidden(effective, param_name)

    for group_id, override in run_overrides.items():
        effective.groups[group_id].enabled = bool(override.enabled)

    return effective
```

需要注意：

- 不允许丢失 shader 中真实存在的参数。
- 如果 manual 引用的参数在新 shader 中不存在，保留为 orphan override，并在 UI 中报警。
- 如果新 shader 增加了参数，自动进入 auto schema，等待人工确认。

## 11. 版本与复用

### 11.1 项目内复用

同一个 project 的 manual schema 持久保存。

重新 preanalysis 后：

- 人工锁定字段不变。
- 新自动字段可补充未锁定信息。

### 11.2 跨项目复用

后续可以把人工修正导出为 shader schema preset：

```text
material_fit/presets/laya_control_schemas/Custom_Fish_FishStandard.json
```

这个 preset 可以作为 curated schema，服务同类 shader。

### 11.3 Schema hash

建议对 auto schema 计算 hash：

- shader path
- shader name
- uniform names/types/default/ranges/hidden
- defines

如果 hash 改变，UI 提示：

```text
shader 控件结构已变化，人工 schema 可能需要重新检查。
```

## 12. 推荐实施顺序

### Step 1：数据结构先落地

- 新增 `auto_laya_control_schema`
- 新增 `manual_laya_control_schema`
- 新增 `effective_laya_control_schema`
- 保留旧 `laya_control_groups` 作为兼容视图

验收：

- 没有人工修正时，effective 等于 auto。
- 人工移动一个参数后，effective 反映移动结果。

### Step 2：运行控制台添加编辑模式

先不做拖拽，做按钮和下拉：

- 新建分类
- 重命名分类
- 参数移动到分类
- 参数 searchable 开关
- 分类 enabled 开关

验收：

- 能把 `u_FresnelSmooth` 从 specular 移到 fresnel。
- 能把 `u_MAER` 移到 `packed_maer` 并设为不可搜索。
- 能新增 `normal` 分类，把 `u_Bump*` 移进去。

### Step 3：fit_config 和 optimizer 改读 effective schema

验收：

- 禁用某组后，heuristic / semantic_group / CMA 都不再搜索该组参数。
- 移动参数后，`decision.json` 中的 group/stage 能反映新分类。

### Step 4：增加 curated FishStandard schema

把 `FishStandard_Shader_Grouping_Review.md` 中的专业判断固化成 preset。

验收：

- `u_FresnelSmooth` 默认归 fresnel。
- `u_MluAlbedoColor` 默认归 specular。
- `u_EmissionPower` 不再出现。
- `u_MAER` / `u_Mask` / `u_Bump*` 不再落入 misc。

### Step 5：再做 drag-and-drop

当数据结构稳定后，再把移动参数升级为拖拽交互。

原因：拖拽是交互优化，不是核心能力。先保证保存、合并、消费正确。

## 13. 成功标准

第一阶段成功标准不是“自动分类完美”，而是：

- 人类工程师能在 3-5 分钟内修正一个 shader 的主要分类错误。
- 修正结果能保存并复用。
- 重新运行 preanalysis 不会覆盖人工结果。
- 下游 optimizer 实际使用人工确认后的控制空间。
- 错误分类不会再直接污染搜索空间。

对当前 `FishStandard`，最小验收样例：

```text
u_FresnelSmooth -> fresnel
u_MluAlbedoColor -> specular_smoothness
u_MAER -> packed_maer, searchable=false
u_Mask -> effect_mask, searchable=false
u_BumpMap/u_BumpScale -> normal
u_SelfLightDir -> lighting_direction
u_Alpha/u_Cutoff -> alpha_cutoff
```

如果这些都能被人工修正并被 optimizer 尊重，这个设计就达到了阶段目标。

## 14. 结论

可编辑 Laya 控件 schema 是后续优化算法的上层输入合同。它的价值不只是让 UI 更像 Inspector，而是把 shader 工程师的专业判断变成结构化、可保存、可被优化器消费的数据。

短期我们依赖人工修正保证可靠性；中期用 curated schema 和 LLM 提高自动分类质量；长期目标是让人工只做审核，而不是修正大量错误。

因此下一步最优先的不是继续堆优化器，而是先让这个 schema editor 成为可信的控制空间编辑器。只有控制空间准确，后续 group probe、pattern search、CMA-ES 才有意义。

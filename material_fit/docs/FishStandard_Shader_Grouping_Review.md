# FishStandard Shader 功能分析与当前控件分类评审

> 状态：评审文档。  
> 评审对象：`d:\project_data\laya\laya外包\assets\resources\shader\FishStandard.shader`。  
> 目的：从 shader 工程师视角，先还原该 shader 的实际功能和执行顺序，再评价当前工具截图中的 Laya 控件分组是否合理，以及下一步应该如何修正。

## 1. 总体结论

当前工具的分组方向是对的：它已经把大部分高层效果拆成了基础色、高光/金属/光滑度、Fresnel、自发光、环境反射/Matcap、阴影/漫反射、HSV/对比度、其他控件。这比平铺所有 uniform 更适合人类工程师参与选择优化范围。

但从实际 `FishStandard.shader` 看，当前分类仍然是“基于名称的粗分组”，还没有达到 shader 工程师会接受的最终控制面板标准。主要问题有四类：

- 一些参数被错误分组，例如 `u_FresnelSmooth` 被归入高光/金属/光滑度，`u_MluAlbedoColor` 被归入基础色。
- 一些核心共享贴图和 mask 被归入“其他控件”，但它们实际决定多个效果模块的输入权重。
- define gate 和参数 gate 没有被充分展示，尤其是 `ADJUST_HSV`、`ENABLE_CONTRAST`、`SPECULARSECOND`、`ENABLE_FRESNEL_METALLIC`、`ENABLE_DIRECTIONAL_FRESNEL`。
- 解析器把注释掉的 `u_EmissionPower` 也显示出来了，这属于明确 bug，不是分类问题。

因此，截图里的分组可以作为第一版人机协作面板，但不能直接作为最终优化空间的权威分类。下一步应改成“shader 实际执行链路 + uniformMap 元数据 + define/hidden 条件 + 少量人工规则”的组合分类。

## 2. Shader 暴露参数与宏结构

该 shader 是一个 Forward Pass 的 Laya `Shader3D`，名字是 `Custom/Fish/FishStandard`。它暴露了大量材质控件，主要分为以下来源：

### 2.1 基础贴图与基础色

相关参数：

- `u_BaseColor`
- `u_BaseMap`
- `u_BaseMap_ST`
- `u_Gamma_Power`
- `u_Alpha`
- `u_Cutoff`

实际作用：

- `u_BaseMap` 经 `u_BaseMap_ST` 采样。
- `u_Gamma_Power` 对 base texture 做 `pow`。
- `s.alpha = baseTex.a * u_BaseColor.a * u_Alpha`。
- `s.albedo = baseTex.rgb * s.occlusion * u_BaseColor.rgb`。
- 在主流程后段，shader 又重新采样一次 base texture，并在 metallic 区域把 `color` 再乘上 `baseTex.rgb * u_BaseColor.rgb`。

重要判断：`u_Gamma_Power` 不只是基础色参数。它还作用于 emission texture、Matcap texture、MatcapAdd texture，因此更像“全局纹理线性化/伽马修正”控件。

### 2.2 MAER 打包贴图

相关参数：

- `u_MAER`
- `u_MAER_ST`
- `u_Metallic`
- `u_MetallicRemapMin`
- `u_MetallicRemapMax`
- `u_Smoothness`
- `u_SmoothnessRemapMin`
- `u_SmoothnessRemapMax`
- `u_OcclusionStrength`

实际作用：

- `SampleMAER` 采样 `u_MAER`。
- `m.r` 被 remap 后作为 metallic 输入。
- `m.a` 被 remap 后作为 smoothness 输入。
- `m.g` 通过 `u_OcclusionStrength` 混合成 occlusion。
- `m.b` 在 `EMISSION` 开启时参与 emission：`u_EmissionColor.rgb * m.b`。

重要判断：`u_MAER` 不是普通 misc texture。它是 metallic / occlusion / emission / roughness/smoothness 的打包核心贴图，应单独列为“打包材质图 / MAER”，或至少作为多个组的共享依赖。

### 2.3 Normal / TBN

相关参数：

- `u_BumpMap`
- `u_BumpMap_ST`
- `u_BumpScale`
- `TANGENT` define

实际作用：

- fragment 中采样 normal map。
- 通过 tangent/binormal/normal 组成 TBN，把 tangent-space normal 转到 world-space。
- `u_BumpScale` 调整法线扰动强度。

重要判断：这组不应放在“其他控件”。它是独立的 Normal 组，会影响高光、Fresnel、Matcap、IBL、diffuse，属于全局几何法线输入。

### 2.4 漫反射 / 阴影 ramp

相关参数：

- `u_DiffuseThreshold`
- `u_DiffuseSmoothness`
- `u_ShadowColor`
- `u_GIIntensity`
- `u_OcclusionStrength`

实际作用：

- `DirectStylized` 里用 `calculateRamp(u_DiffuseThreshold, lightStrength, u_DiffuseSmoothness)` 得到 toon diffuse ramp。
- `finalDiffuse = mix(u_ShadowColor.rgb * brdf.diffuse, brdf.diffuse, ramp)`。
- `u_GIIntensity` 和 `u_OcclusionStrength` 更早在 surface 初始化中影响 `s.occlusion`，进而影响 `s.albedo`。

重要判断：当前“阴影 / 漫反射层次”组基本合理。但 `u_OcclusionStrength` 与 `u_GIIntensity` 同时影响基础明暗和 diffuse，不只是局部阴影参数。

### 2.5 BRDF、高光、金属和光滑度

相关参数：

- `u_Metallic`
- `u_MetallicRemapMin`
- `u_MetallicRemapMax`
- `u_Smoothness`
- `u_SmoothnessRemapMin`
- `u_SmoothnessRemapMax`
- `u_SpecularColor`
- `u_SpecularIntensity`
- `u_SpecularThreshold`
- `u_SpecularSmooth`
- `u_GGXSpecular`
- `u_MluAlbedoColor`
- `u_SpecularLightOffset`
- `u_SpecularHighlights`

实际作用：

- `InitBRDF` 使用 albedo、metallic、smoothness 生成 diffuse/specular/roughness。
- `DirectStylized` 中计算 direct specular。
- `u_SpecularThreshold` / `u_SpecularSmooth` 控制 stylized specular 的 smoothstep。
- `u_GGXSpecular` 在 stylized specular 与物理 specular term 之间混合。
- `u_MluAlbedoColor` 控制 specular 是否被 albedo 染色。
- `u_SpecularHighlights` 是一个 0/1-ish 高光开关。

重要判断：高光/金属/光滑度作为大组是合理的，但内部应再拆：

- metallic/smoothness/MAER 子组。
- main specular lobe 子组。
- specular toggle/offset 子组。

尤其 `u_MluAlbedoColor` 不应在基础色组，它实际在 specular 颜色混合里使用。

### 2.6 第二高光

相关参数：

- `SPECULARSECOND`
- `u_SpecularSecondColor`
- `u_SpecularSecondIntensity`
- `u_SpecularSecondThreshold`
- `u_SpecularSecondLightOffset`

实际作用：

- 只有 `SPECULARSECOND` define 开启时执行。
- 在主流程 direct stylized 之后额外叠加一层第二高光。

重要判断：这不应该混在普通 specular 主组里不加区分。它是独立可选 feature，应显示为“第二高光 / Secondary Specular”，并清楚标记 define gate。

### 2.7 IBL / 环境反射

相关参数：

- `u_IBLMap`
- `u_IBLMapColor`
- `u_IBLMapIntensity`
- `u_IBLMapPower`
- `u_IBLMapRotateX`
- `u_IBLMapRotateY`
- `u_IBLMapRotateZ`
- `u_EnvironmentReflections`
- `u_Mask.r`

实际作用：

- `GlossyEnv` 用 reflection vector 采样 cube map。
- `u_EnvironmentReflections` 是环境反射开关。
- IBL 结果再乘 `brdf.specular` 和 `envStrength`。
- 主流程中通过 `maskTex.r` 混合环境反射影响。

重要判断：当前把 IBL 和 Matcap 合成“环境反射 / Matcap”是可以接受的第一版，但从工程控制上建议拆成 IBL、Matcap、MatcapAdd 三个子组，因为它们采样不同贴图、用不同 mask channel、表现和优化方向不同。

### 2.8 Matcap 与 MatcapAdd

相关参数：

- `u_MatcapMap`
- `u_MatcapMap_ST`
- `u_MatcapAngle`
- `u_MatcapStrength`
- `u_MatcapPow`
- `u_MatcapColor`
- `u_MatcapAddMap`
- `u_MatcapAddMap_ST`
- `u_MatcapAddAngle`
- `u_MatcapAddStrength`
- `u_MatcapAddPow`
- `u_MatcapAddColor`
- `u_Mask.g`
- `u_Mask.b`

实际作用：

- `Matcap` 以 view-space normal 生成 UV，采样 `u_MatcapMap`，乘 color/pow/strength，通过 `maskTex.g` 混入。
- `MatcapAdd` 采样第二张 add map，通过 `maskTex.b` 额外加亮。

重要判断：当前组内把这些参数放一起基本合理。但 `u_MatcapStrength` 和 `u_MatcapAddStrength` 是真实 gate；若为 0，贴图、颜色、pow、angle 很多变化都不可见。

### 2.9 Emission

相关参数：

- `EMISSION`
- `u_EmissionColor`
- `u_EmissionTexture`
- `u_EmissionTexture_ST`
- `u_EmissionScale`
- `u_MAER.b`

实际作用：

- 只有 `EMISSION` define 开启时，才计算 `s.emission`。
- emission 由两部分组成：`u_EmissionColor * emissionTexture * u_EmissionScale`，以及 `u_EmissionColor * m.b`。

重要判断：当前 emission 组大体合理，但截图中出现的 `u_EmissionPower` 是错误的，因为 shader 中该 uniformMap 行被注释掉了。当前 parser 应该先剥离 `//` 和块注释，否则 UI 会显示不存在的控件，后续写 `.lmat` 也可能产生脏数据。

### 2.10 Fresnel / 边缘光

相关参数：

- `u_FresnelColor`
- `u_fresnelOffset`
- `u_FresnelThreshold`
- `u_FresnelSmooth`
- `u_FresnelIntensity`
- `u_FresnelUesF0`
- `u_FresnelPow`
- `u_FresnelUseMoldeNormal`
- `ENABLE_FRESNEL_METALLIC`
- `ENABLE_DIRECTIONAL_FRESNEL`
- `u_Mask.a`
- vertex color `.r`

实际作用：

- `u_FresnelIntensity < 0.001` 时直接返回 0，这是强 gate。
- `u_FresnelPow`、threshold、smooth 控制 rim shape。
- `ENABLE_FRESNEL_METALLIC` 决定 F0 是否从 baseColor/metallic 派生。
- `ENABLE_DIRECTIONAL_FRESNEL` 决定是否乘光照方向项。
- 主流程通过 `maskTex.a` 把 Fresnel 加到最终 color。
- 顶点色 r 也会乘到 Fresnel 上。

重要判断：当前 Fresnel 组方向正确，但有明显错分：`u_FresnelSmooth` 被放进了“高光 / 金属 / 光滑度”。这是因为当前规则看到 `smooth` 就先归 specular/smoothness，而没有优先识别 `fresnel` 前缀。对 shader 工程师来说，这是必须修的分类错误。

### 2.11 HSV / Contrast

相关参数：

- `ADJUST_HSV`
- `ENABLE_CONTRAST`
- `u_AdjustHue`
- `u_AdjustSaturation`
- `u_AdjustLightness`
- `u_saturationProtection`
- `u_ContrastScale`

实际作用：

- `ADJUST_HSV` 开启后，在所有 lighting、emission、Matcap、Fresnel 叠加之后，对最终 color 做 HSL 调整。
- `ENABLE_CONTRAST` 嵌套在 `ADJUST_HSV` 内部，只有 HSV 分支启用时才会执行 contrast。

重要判断：当前“HSV / 对比度调色”组合理，但截图中 gate 显示为 0 不够准确。这里至少有两个 define gate：`ADJUST_HSV` 和 `ENABLE_CONTRAST`。即便 UI 不把 define 算进 gate 数，也应该单独展示 define gate，否则人会误以为这些参数总是生效。

### 2.12 Alpha / Cutoff

相关参数：

- `u_Alpha`
- `u_Cutoff`
- `ALPHATEST`

实际作用：

- `u_Alpha` 乘到最终 alpha。
- `ALPHATEST` 开启时，如果 `s.alpha < u_Cutoff` 就 discard。

重要判断：它们不应简单放进“其他控件”。Alpha/Cutoff 是透明裁剪组，通常不应该被外观拟合算法随意调整，除非目标是匹配透明边界。

### 2.13 自定义光照方向

相关参数：

- `u_SelfLightDir`
- `CUSTOMLIGHT`
- `DIRECTIONLIGHT`

实际作用：

- 有方向光时，`L0 = normalize(-dirLight.direction + u_SelfLightDir.xyz)`。
- `CUSTOMLIGHT` 开启时，直接使用 `u_SelfLightDir.xyz` 作为光照方向。

重要判断：`u_SelfLightDir` 不应该是“其他控件”。它会影响 diffuse、高光、Fresnel directional 分支，是一个全局 lighting control，调错会让整个材质方向性变化。

## 3. Fragment 实际执行顺序

该 shader 的 fragment 主要执行顺序如下：

```text
InitCustomSurface
  -> base texture / gamma / alpha
  -> MAER: metallic, smoothness, occlusion, emission mask
  -> optional emission
  -> normal map and TBN
  -> viewDir / vertexColor

InitBRDF
  -> diffuse/specular/roughness/grazing terms

main lighting
  -> get Laya DirectionLight or CUSTOMLIGHT
  -> DirectStylized diffuse + main specular
  -> GlossyEnv IBL, masked by u_Mask.r
  -> add emission
  -> optional SPECULARSECOND
  -> Matcap, masked by u_Mask.g
  -> MatcapAdd, masked by u_Mask.b
  -> metallic-region base texture recolor
  -> Fresnel, masked by u_Mask.a
  -> optional HSL adjustment
  -> optional contrast
  -> optional alpha test
  -> fog
  -> outputTransform
```

这个顺序对分类非常重要。它说明某些参数虽然名字看起来像一个组，但实际影响的是更晚阶段或更全局的结果：

- `u_Gamma_Power` 影响 base、emission、Matcap、MatcapAdd 的采样结果。
- `u_Mask` 四个通道分别控制 IBL、Matcap、MatcapAdd、Fresnel。
- `u_MAER` 同时服务 metallic、smoothness、occlusion、emission。
- HSV/Contrast 是最终调色，影响所有前面叠加后的结果。
- Normal 会影响 diffuse/specular/IBL/Matcap/Fresnel。

## 4. 对截图中当前分类的评价

### 4.1 基础色 / 主体亮度

截图包含：

- `u_BaseColor`
- `u_Gamma_Power`
- `u_MluAlbedoColor`
- `u_BaseMap`
- `u_BaseMap_ST`

评价：部分合理。

合理点：

- `u_BaseColor`、`u_BaseMap`、`u_BaseMap_ST` 属于基础色输入。
- `u_Gamma_Power` 与基础色关系很强。

不足：

- `u_MluAlbedoColor` 不属于基础色。它实际在 specular 颜色混合中使用，应放到高光/specular 组。
- `u_Gamma_Power` 是全局 texture gamma/pow，影响 emission 和 Matcap，不应只归基础色。更合理做法是标为 `global_texture_gamma`，同时显示它会影响多个组。

结论：该组作为人类开关可保留，但内部需要把 `u_MluAlbedoColor` 移出，并给 `u_Gamma_Power` 加“跨组共享参数”标记。

### 4.2 Fresnel / 边缘光

截图包含大部分 Fresnel 参数，但缺少 `u_FresnelSmooth`。

评价：方向正确，但存在关键漏分。

合理点：

- `u_FresnelIntensity`、`u_FresnelColor`、`u_FresnelPow`、`u_FresnelThreshold`、`u_FresnelUesF0`、`u_FresnelUseMoldeNormal`、`u_fresnelOffset` 都属于 Fresnel。
- `u_FresnelIntensity` 被标成 gate 是正确的。

不足：

- `u_FresnelSmooth` 被错分到了 specular/smoothness 组。
- `ENABLE_FRESNEL_METALLIC` 和 `ENABLE_DIRECTIONAL_FRESNEL` 没有在 UI 中清楚展示为 Fresnel 的公式变体 define。
- `u_Mask.a` 是 Fresnel 的强依赖，但当前在 misc 里，没有显示成 Fresnel mask dependency。

结论：Fresnel 组的高层分类合理，但必须修正 `u_FresnelSmooth`，并补上 define gate 与 mask.a 依赖。

### 4.3 HSV / 对比度调色

截图包含：

- `u_AdjustHue`
- `u_AdjustLightness`
- `u_AdjustSaturation`
- `u_ContrastScale`
- `u_saturationProtection`

评价：功能归类合理，但 gate 表达不足。

合理点：

- 这些参数确实在最终 color 阶段做 HSL/contrast 后处理。

不足：

- UI 显示 gate 0 不准确。`ADJUST_HSV` 和 `ENABLE_CONTRAST` 都是 define gate。
- `u_ContrastScale` 只有在 `ADJUST_HSV` 和 `ENABLE_CONTRAST` 同时满足时才执行，应该显示嵌套 gate。

结论：保留此组，但必须改进 define gate 展示。

### 4.4 其他控件

截图包含：

- `u_BumpScale`
- `u_SelfLightDir`
- `u_Alpha`
- `u_BumpMap`
- `u_BumpMap_ST`
- `u_Cutoff`
- `u_MAER`
- `u_MAER_ST`
- `u_Mask`
- `u_Mask_ST`

评价：这是当前分组里最需要优化的部分。

问题：

- `u_Bump*` 应该是 Normal 组。
- `u_SelfLightDir` 应该是 Lighting Direction 组。
- `u_Alpha` / `u_Cutoff` 应该是 Alpha / Cutoff 组。
- `u_MAER` 是 packed material map，不能算 misc。
- `u_Mask` 是 effect mask，四个通道分别控制 IBL、Matcap、MatcapAdd、Fresnel，也不能算 misc。

结论：“其他控件”目前混入了太多高影响共享输入。它不应该被用户随手开/关，因为里面每个参数影响的模块完全不同。建议拆成 Normal、PackedMap、EffectMask、Alpha、Lighting。

### 4.5 高光 / 金属 / 光滑度

截图包含主高光、metallic/smoothness、大量 specular 参数，也包含 `u_FresnelSmooth`。

评价：大方向合理，但内部过宽，且有错分。

合理点：

- metallic、smoothness、specular color/intensity/threshold/smooth、GGX、specular offset/highlights 都属于这个大组。

不足：

- `u_FresnelSmooth` 明确不属于这里。
- 第二高光参数应作为 `SPECULARSECOND` 子组，不应该和主高光混在一起。
- metallic/smoothness 来自 `u_MAER`，但 `u_MAER` 本身被放到 misc，导致依赖关系断裂。

结论：大组可保留，但建议拆成 MetallicSmoothness、MainSpecular、SecondarySpecular 三层，至少 UI 上要显示子块。

### 4.6 自发光

截图包含：

- `u_EmissionScale`
- `u_EmissionColor`
- `u_EmissionPower`
- `u_EmissionTexture`
- `u_EmissionTexture_ST`

评价：除 `u_EmissionPower` 外基本合理。

问题：

- `u_EmissionPower` 在 shader 中是注释掉的，不应出现。
- `EMISSION` define 是硬 gate，应明确显示。
- `u_MAER.b` 也是 emission 输入，应作为 shared dependency 标记。

结论：自发光组方向正确，但 parser 必须忽略注释，UI 必须展示 `EMISSION` define gate。

### 4.7 环境反射 / Matcap

截图包含 IBL、Matcap、MatcapAdd 参数。

评价：作为第一层大组可以接受，但作为优化开关过粗。

合理点：

- 这些都属于 view/reflection/material highlight 相关效果。
- `u_MatcapStrength`、`u_MatcapAddStrength` 被识别为 gate 是正确的。

不足：

- IBL、Matcap、MatcapAdd 是三条不同路径，分别受 `u_Mask.r/g/b` 控制。
- `u_EnvironmentReflections` 是 IBL toggle，不是 Matcap。
- 如果用户只想调 Matcap，不一定应该一起调 IBL cube map 和旋转。

结论：建议拆成 `ibl_reflection`、`matcap_multiply`、`matcap_add` 三个子组；大 UI 可以折叠成环境反射/Matcap 总组。

### 4.8 阴影 / 漫反射层次

截图包含：

- `u_DiffuseSmoothness`
- `u_DiffuseThreshold`
- `u_GIIntensity`
- `u_OcclusionStrength`
- `u_ShadowColor`

评价：当前分类基本合理。

补充：

- `u_GIIntensity` 和 `u_OcclusionStrength` 在 surface 初始化阶段影响 `s.albedo`，不是只在 diffuse ramp 内生效。
- 调这组时会改变整体亮度，因此它和基础色存在耦合。

结论：保留此组，但 UI 说明应标注它会影响整体明暗，不只是“阴影颜色”。

## 5. 当前分类的总体评分

按 shader 工程可用性评估：

| 维度 | 评分 | 说明 |
|---|---:|---|
| 高层语义方向 | 7/10 | 大组方向基本接近 shader 功能块 |
| 参数归属准确性 | 5/10 | 有 `u_FresnelSmooth`、`u_MluAlbedoColor`、`u_EmissionPower` 等明确问题 |
| gate/define 表达 | 4/10 | 参数 gate 有一点，define gate 展示明显不足 |
| 优化空间可控性 | 6/10 | 已能让人按大组开关，但若 misc 不拆，会误伤或漏控关键参数 |
| shader 执行链路理解 | 5/10 | 目前仍偏名字规则，未充分表达 MAER/Mask/Gamma/Normal 的跨组依赖 |

综合评价：当前截图是一个可用的第一版人机协作入口，但还不能作为自动调参的最终搜索空间定义。它最大的价值是让人能快速排除明显不需要的效果组；最大的风险是把共享输入和错分参数隐藏在错误组里，导致用户关闭/开启组时产生非预期影响。

## 6. 建议的下一版分组

推荐把 FishStandard 的 UI 分组改成两层：第一层是人类快速开关，第二层是 shader 工程细分子组。

### 6.1 第一层快速开关

- Base / Albedo
- Diffuse / Shadow / Occlusion
- Metallic / Smoothness
- Main Specular
- Secondary Specular
- IBL Reflection
- Matcap
- Matcap Add
- Emission
- Fresnel / Rim
- Color Adjust / Contrast
- Normal
- Alpha / Cutoff
- Lighting Direction
- Shared Maps

### 6.2 Shared Maps 只做依赖，不建议直接优化

`u_MAER`、`u_Mask`、`u_BaseMap`、`u_BumpMap`、`u_MatcapMap`、`u_IBLMap`、`u_EmissionTexture` 这些 texture slot 通常不应被数值优化器修改。它们应显示为依赖资源，而不是普通搜索参数。

尤其：

- `u_MAER.r` -> metallic
- `u_MAER.g` -> occlusion
- `u_MAER.b` -> emission mask
- `u_MAER.a` -> smoothness
- `u_Mask.r` -> IBL
- `u_Mask.g` -> Matcap
- `u_Mask.b` -> MatcapAdd
- `u_Mask.a` -> Fresnel

UI 如果能展示这些通道语义，会比单纯显示 `u_Mask texture` 有用得多。

## 7. 需要立即修正的规则

### 7.1 先剥离注释再解析 uniformMap

当前出现 `u_EmissionPower`，说明 parser 把 `//u_EmissionPower` 当成真实参数。应在 `_parse_laya_uniform_map` 前剥离注释，至少处理：

- `// ...`
- `/* ... */`

这是 P0 级别问题，因为不存在的参数不应进入 UI，更不应进入 `.lmat` 候选写入。

### 7.2 分组优先级要从“关键词任意命中”改成“前缀/完整语义优先”

当前 `u_FresnelSmooth` 被 `smooth` 抢走，说明规则优先级不对。建议：

1. 先匹配强前缀/强模块名：`fresnel`、`emission`、`matcap`、`ibl`、`specularsecond`。
2. 再匹配通用形容词：`smooth`、`threshold`、`color`、`power`。
3. 最后用弱 token 兜底。

### 7.3 `hidden: "!data.X"` 应转成 define gate

uniformMap 里已经明确写了：

- `hidden: "!data.EMISSION"`
- `hidden: "!data.SPECULARSECOND"`
- `hidden: "!data.ADJUST_HSV"`
- `hidden: "!data.ENABLE_CONTRAST"`

这比名字推断更可靠。工具应优先解析 hidden 表达式作为 define gate。

### 7.4 增加项目专属 FishStandard 分组规则

这类生产 shader 不适合完全靠通用命名推断。建议给 `Custom/Fish/FishStandard` 加一个 curated schema：

- 明确每个参数属于哪个 group/subgroup。
- 明确 gate 和 define。
- 明确 shared texture channel 语义。
- 明确哪些参数可搜索、哪些只展示。

LLM 可以辅助生成初版 schema，但最终最好允许人类 shader 工程师在 UI 中修正并保存。

## 8. 对当前工具方向的判断

你的想法是正确的：在运行控制台里按 shader 暴露控件分组，并让人类决定哪些板块参与优化，这是比“全参数盲搜”更符合生产实际的路线。

但这个面板要真正有价值，分组必须逐步从“名字像什么”升级到“shader 实际怎么执行”。对 `FishStandard.shader` 来说，最应该优先修的不是优化算法，而是控制空间定义：

1. 修 parser，去掉注释参数。
2. 修明显错分：`u_FresnelSmooth`、`u_MluAlbedoColor`。
3. 从 misc 拆出 Normal、Packed MAER、Mask、Alpha、Lighting。
4. 展示 define gate 和 hidden 条件。
5. 把 IBL / Matcap / MatcapAdd 拆成可单独开关的子组。

这些完成后，人类工程师就能用这个面板非常快地告诉优化器：“这次只调基础色、阴影、高光和 Fresnel；不要碰 normal、alpha、light direction、texture slots。” 这会比任何全维优化器都更直接地减少无效搜索。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from ..shared.models import ShaderDefine, ShaderParam


@dataclass(frozen=True)
class ParamGate:
    """A condition that must hold before a parameter has visual effect."""

    kind: str  # "define" or "param_nonzero"
    name: str
    expected: Any = True
    reason: str = ""


@dataclass(frozen=True)
class ParamSemantics:
    """Machine-readable meaning of one editable shader parameter."""

    name: str
    param_type: str
    group: str
    role: str
    transform: str
    range_min: float | None = None
    range_max: float | None = None
    gates: list[ParamGate] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    searchable: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gates"] = [asdict(gate) for gate in self.gates]
        return payload


@dataclass(frozen=True)
class ShaderEffectGroup:
    """A small effect-level search space, e.g. Fresnel or emission."""

    name: str
    params: list[str]
    gate_params: list[str] = field(default_factory=list)
    define_gates: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    active: bool = True
    current_active: bool = True
    suggested_by_unity: bool = False
    probe_required: bool = False
    search_priority: float = 0.0
    search_params: list[str] = field(default_factory=list)
    unity_features: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    reason: str = ""
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ShaderEffectGraph:
    """Effect groups plus per-param semantics for an optimizer run."""

    params: dict[str, ParamSemantics]
    groups: dict[str, ShaderEffectGroup]
    defines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": {name: sem.to_dict() for name, sem in self.params.items()},
            "groups": {name: group.to_dict() for name, group in self.groups.items()},
            "defines": list(self.defines),
        }

    def active_search_params(self) -> list[str]:
        active: list[str] = []
        for name, sem in self.params.items():
            group = self.groups.get(sem.group)
            if not sem.searchable:
                continue
            if group is None:
                active.append(name)
                continue
            if group.current_active or group.suggested_by_unity:
                if group.search_params and name not in group.search_params and name not in group.gate_params:
                    continue
                active.append(name)
        return active


def build_shader_effect_graph(
    shader_params: Sequence[ShaderParam],
    *,
    shader_defines: Sequence[ShaderDefine] | Sequence[dict[str, Any]] = (),
    material_params: dict[str, Any] | None = None,
    material_defines: Iterable[str] = (),
    llm_semantics: dict[str, Any] | None = None,
) -> ShaderEffectGraph:
    """Infer a conservative effect graph from shader metadata.

    The result intentionally combines simple name rules with optional
    LLM-supplied semantics. LLM data can refine group/role/transform/gates,
    but cannot make unknown parameters appear.
    """

    material_params = material_params or {}
    material_define_set = {str(item) for item in material_defines}
    shader_define_names = {_define_name(item) for item in shader_defines}
    llm_by_param = _index_llm_param_semantics(llm_semantics)

    params: dict[str, ParamSemantics] = {}
    for param in shader_params:
        base = _infer_param_semantics(
            param,
            material_params=material_params,
            shader_define_names=shader_define_names,
        )
        params[param.name] = _merge_llm_semantics(base, llm_by_param.get(param.name))

    groups = _build_groups(params, material_params, material_define_set, llm_semantics)
    return ShaderEffectGraph(params=params, groups=groups, defines=sorted(shader_define_names))


def graph_to_dict(graph: ShaderEffectGraph) -> dict[str, Any]:
    return graph.to_dict()


def graph_from_dict(payload: dict[str, Any] | None) -> ShaderEffectGraph | None:
    if not isinstance(payload, dict):
        return None
    raw_params = payload.get("params")
    raw_groups = payload.get("groups")
    if not isinstance(raw_params, dict) or not isinstance(raw_groups, dict):
        return None
    params: dict[str, ParamSemantics] = {}
    for name, raw in raw_params.items():
        if not isinstance(raw, dict):
            continue
        gates = [
            ParamGate(
                kind=str(g.get("kind", "")),
                name=str(g.get("name", "")),
                expected=g.get("expected", True),
                reason=str(g.get("reason", "")),
            )
            for g in raw.get("gates", [])
            if isinstance(g, dict)
        ]
        params[str(name)] = ParamSemantics(
            name=str(raw.get("name", name)),
            param_type=str(raw.get("param_type", "")),
            group=str(raw.get("group", "misc")),
            role=str(raw.get("role", "value")),
            transform=str(raw.get("transform", "linear")),
            range_min=_optional_float(raw.get("range_min")),
            range_max=_optional_float(raw.get("range_max")),
            gates=gates,
            dependencies=[str(item) for item in raw.get("dependencies", []) if isinstance(item, str)],
            searchable=bool(raw.get("searchable", True)),
            reason=str(raw.get("reason", "")),
        )
    groups: dict[str, ShaderEffectGroup] = {}
    for name, raw in raw_groups.items():
        if not isinstance(raw, dict):
            continue
        groups[str(name)] = ShaderEffectGroup(
            name=str(raw.get("name", name)),
            params=[str(item) for item in raw.get("params", []) if isinstance(item, str)],
            gate_params=[str(item) for item in raw.get("gate_params", []) if isinstance(item, str)],
            define_gates=[str(item) for item in raw.get("define_gates", []) if isinstance(item, str)],
            channels=[str(item) for item in raw.get("channels", []) if isinstance(item, str)],
            active=bool(raw.get("active", True)),
            current_active=bool(raw.get("current_active", raw.get("active", True))),
            suggested_by_unity=bool(raw.get("suggested_by_unity", False)),
            probe_required=bool(raw.get("probe_required", False)),
            search_priority=float(raw.get("search_priority", 0.0) or 0.0),
            search_params=[str(item) for item in raw.get("search_params", []) if isinstance(item, str)],
            unity_features=[str(item) for item in raw.get("unity_features", []) if isinstance(item, str)],
            evidence=[str(item) for item in raw.get("evidence", []) if isinstance(item, str)],
            reason=str(raw.get("reason", "")),
            order=int(raw.get("order", 0) or 0),
        )
    return ShaderEffectGraph(
        params=params,
        groups=groups,
        defines=[str(item) for item in payload.get("defines", []) if isinstance(item, str)],
    )


def _infer_param_semantics(
    param: ShaderParam,
    *,
    material_params: dict[str, Any],
    shader_define_names: set[str],
) -> ParamSemantics:
    name = param.name
    lower = name.lower()
    group = _infer_group(lower)
    role = _infer_role(lower, param.param_type)
    transform = _infer_transform(lower, param.param_type)
    gates = _infer_gates(name, lower, group, shader_define_names)
    dependencies = [gate.name for gate in gates if gate.kind == "param_nonzero"]
    searchable = _is_searchable(param, lower, role)
    reason = "name/type heuristic"
    if role == "gate":
        reason = "effect gate parameter"
    elif not searchable:
        reason = "fixed because it is texture, tiling, alpha/cutoff, bool, or hidden"
    if role == "gate" and _number(material_params.get(name), 1.0) <= 0.0:
        reason = "gate parameter is currently zero; group may need activation"
    return ParamSemantics(
        name=name,
        param_type=param.param_type,
        group=group,
        role=role,
        transform=transform,
        range_min=param.range_min,
        range_max=param.range_max,
        gates=gates,
        dependencies=dependencies,
        searchable=searchable,
        reason=reason,
    )


def _build_groups(
    params: dict[str, ParamSemantics],
    material_params: dict[str, Any],
    material_defines: set[str],
    llm_semantics: dict[str, Any] | None,
) -> dict[str, ShaderEffectGroup]:
    grouped: dict[str, list[ParamSemantics]] = {}
    for sem in params.values():
        grouped.setdefault(sem.group, []).append(sem)

    out: dict[str, ShaderEffectGroup] = {}
    for group_name, items in sorted(grouped.items()):
        gate_params = sorted({gate.name for sem in items for gate in sem.gates if gate.kind == "param_nonzero"})
        define_gates = sorted({gate.name for sem in items for gate in sem.gates if gate.kind == "define"})
        missing_defines = [name for name in define_gates if name not in material_defines]
        zero_gates = [name for name in gate_params if _number(material_params.get(name), 0.0) <= 0.0]
        active = not missing_defines and not zero_gates
        reason = "active"
        if missing_defines:
            reason = f"missing defines: {', '.join(missing_defines)}"
        elif zero_gates:
            reason = f"zero gate params: {', '.join(zero_gates)}"
        unity_hint = _group_unity_hint(group_name, llm_semantics)
        searchable_params = [item.name for item in items if item.searchable]
        search_params = [
            name for name in unity_hint["params"]
            if name in params and params[name].searchable and params[name].group == group_name
        ] or searchable_params
        suggested = bool(unity_hint["features"])
        out[group_name] = ShaderEffectGroup(
            name=group_name,
            params=[item.name for item in items],
            gate_params=gate_params,
            define_gates=define_gates,
            channels=_channels_for_group(group_name),
            active=active,
            current_active=active,
            suggested_by_unity=suggested,
            probe_required=suggested and not active,
            search_priority=unity_hint["priority"],
            search_params=search_params,
            unity_features=unity_hint["features"],
            evidence=unity_hint["evidence"],
            reason=reason,
        )
    return out


def _group_unity_hint(group_name: str, llm_semantics: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"features": [], "evidence": [], "params": [], "priority": 0.0}
    if not isinstance(llm_semantics, dict):
        return out

    def add_feature(feature: str, confidence: Any, evidence: list[str], params: list[str]) -> None:
        if feature and feature not in out["features"]:
            out["features"].append(feature)
        out["priority"] = max(out["priority"], _number(confidence, 0.0))
        for item in evidence:
            if item not in out["evidence"]:
                out["evidence"].append(item)
        for param in params:
            if param not in out["params"]:
                out["params"].append(param)

    for feature in llm_semantics.get("unity_feature_summary", []):
        if not isinstance(feature, dict):
            continue
        groups = feature.get("laya_candidate_groups", [])
        if isinstance(groups, list) and group_name in groups:
            add_feature(
                str(feature.get("feature") or feature.get("name") or "other"),
                feature.get("confidence"),
                [str(item) for item in feature.get("evidence", []) if isinstance(item, str)][:5],
                [],
            )
    for phenomenon in llm_semantics.get("unity_phenomena", []):
        if not isinstance(phenomenon, dict):
            continue
        groups = phenomenon.get("laya_candidate_groups", [])
        if isinstance(groups, list) and group_name in groups:
            add_feature(
                str(phenomenon.get("name") or "other"),
                phenomenon.get("confidence"),
                [str(item) for item in phenomenon.get("unity_evidence", []) if isinstance(item, str)][:5],
                [],
            )
    for candidate in llm_semantics.get("laya_module_candidates", []):
        if not isinstance(candidate, dict) or candidate.get("group") != group_name:
            continue
        add_feature(
            str(candidate.get("feature") or "other"),
            candidate.get("confidence"),
            [str(candidate.get("reason", ""))] if candidate.get("reason") else [],
            [str(item) for item in candidate.get("params", []) if isinstance(item, str)],
        )
    out["evidence"] = out["evidence"][:10]
    return out


def _infer_group(lower: str) -> str:
    if any(token in lower for token in ("base", "albedo", "gamma")):
        return "base_color"
    if any(token in lower for token in ("shadow", "occlusion", "diffuse", "gi")):
        return "shadow_diffuse"
    if any(token in lower for token in ("specular", "smooth", "metallic", "roughness", "ggx")):
        return "specular_smoothness"
    if any(token in lower for token in ("ibl", "reflect", "matcap", "environment")):
        return "reflection_matcap"
    if "fresnel" in lower or "rim" in lower:
        return "fresnel"
    if "emission" in lower or "emissive" in lower:
        return "emission"
    if any(token in lower for token in ("hue", "saturation", "lightness", "contrast")):
        return "color_grade"
    return "misc"


def _infer_role(lower: str, param_type: str) -> str:
    type_l = str(param_type).lower()
    if "texture" in type_l or "sampler" in type_l:
        return "texture"
    if "color" in lower or type_l == "color":
        return "color"
    if any(token in lower for token in ("intensity", "strength", "scale")):
        return "gate" if any(token in lower for token in ("fresnel", "emission", "matcap")) else "intensity"
    if any(token in lower for token in ("threshold", "smooth", "pow", "power", "remap")):
        return "shape"
    if "hue" in lower:
        return "angle"
    return "value"


def _infer_transform(lower: str, param_type: str) -> str:
    type_l = str(param_type).lower()
    if "color" in lower or type_l == "color":
        return "color_rgb"
    if "hue" in lower or "angle" in lower or "rotation" in lower:
        return "circular"
    if any(token in lower for token in ("pow", "power", "gamma", "intensity", "strength", "scale")):
        return "log"
    return "linear"


def _infer_gates(name: str, lower: str, group: str, shader_define_names: set[str]) -> list[ParamGate]:
    gates: list[ParamGate] = []
    if group == "emission" and "EMISSION" in shader_define_names:
        gates.append(ParamGate("define", "EMISSION", True, "emission uniforms require EMISSION"))
        if "scale" not in lower and "intensity" not in lower:
            gates.append(ParamGate("param_nonzero", "u_EmissionScale", True, "emission scale gates this group"))
    if group == "color_grade" and "ADJUST_HSV" in shader_define_names:
        gates.append(ParamGate("define", "ADJUST_HSV", True, "HSV uniforms require ADJUST_HSV"))
    if "contrast" in lower and "ENABLE_CONTRAST" in shader_define_names:
        gates.append(ParamGate("define", "ENABLE_CONTRAST", True, "contrast requires ENABLE_CONTRAST"))
    if group == "fresnel" and name != "u_FresnelIntensity":
        gates.append(ParamGate("param_nonzero", "u_FresnelIntensity", True, "Fresnel intensity gates Fresnel shape/color"))
    if group == "reflection_matcap" and "matcap" in lower and "strength" not in lower:
        gates.append(ParamGate("param_nonzero", "u_MatcapStrength", True, "Matcap strength gates Matcap color/map"))
    return gates


def _is_searchable(param: ShaderParam, lower: str, role: str) -> bool:
    type_l = str(param.param_type).strip().lower()
    if role == "texture":
        return False
    if type_l in {"bool", "boolean"}:
        return False
    if lower.endswith("_st") or lower.endswith("st"):
        return False
    if lower in {"u_alpha", "u_cutoff"}:
        return False
    hidden = str(param.hidden or "").lower()
    if hidden in {"true", "1", "yes"}:
        return False
    return True


def _channels_for_group(group: str) -> list[str]:
    return {
        "base_color": ["base_color_main_texture"],
        "shadow_diffuse": ["shadow_occlusion", "base_color_main_texture"],
        "specular_smoothness": ["metallic_smoothness_specular"],
        "reflection_matcap": ["environment_reflection_matcap"],
        "fresnel": ["fresnel_rim", "center_vs_edge_balance"],
        "emission": ["emission"],
        "color_grade": ["color_grading_hsv_contrast"],
    }.get(group, [])


def _index_llm_param_semantics(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("param_semantics")
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = item
    return out


def _merge_llm_semantics(base: ParamSemantics, hint: dict[str, Any] | None) -> ParamSemantics:
    if not hint:
        return base
    gates = list(base.gates)
    for gate in hint.get("gates", []):
        if isinstance(gate, dict) and isinstance(gate.get("name"), str):
            gates.append(
                ParamGate(
                    kind=str(gate.get("kind", "define")),
                    name=str(gate["name"]),
                    expected=gate.get("expected", True),
                    reason=str(gate.get("reason", "LLM semantic hint")),
                )
            )
    return ParamSemantics(
        name=base.name,
        param_type=base.param_type,
        group=str(hint.get("group") or base.group),
        role=str(hint.get("role") or base.role),
        transform=str(hint.get("transform") or base.transform),
        range_min=base.range_min,
        range_max=base.range_max,
        gates=gates,
        dependencies=[str(item) for item in hint.get("dependencies", base.dependencies) if isinstance(item, str)],
        searchable=bool(hint.get("searchable", base.searchable)),
        reason=str(hint.get("reason") or f"{base.reason}; LLM refined"),
    )


def _define_name(item: ShaderDefine | dict[str, Any]) -> str:
    if isinstance(item, ShaderDefine):
        return item.name
    if isinstance(item, dict):
        return str(item.get("name", ""))
    return ""


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

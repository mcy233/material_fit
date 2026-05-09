from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class LlmSemanticGate:
    kind: str
    name: str
    expected: Any = True
    reason: str = ""


@dataclass(frozen=True)
class LlmParamSemantic:
    name: str
    group: str
    role: str
    transform: str = "linear"
    gates: list[LlmSemanticGate] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    searchable: bool = True
    reason: str = ""


@dataclass(frozen=True)
class LlmUnityPhenomenon:
    name: str
    confidence: float
    unity_evidence: list[str] = field(default_factory=list)
    laya_candidate_groups: list[str] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class LlmUnityFeature:
    feature: str
    enabled: bool
    confidence: float
    evidence: list[str] = field(default_factory=list)
    unity_params: list[str] = field(default_factory=list)
    textures: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    laya_candidate_groups: list[str] = field(default_factory=list)
    risk: str = ""


@dataclass(frozen=True)
class LlmLayaModuleCandidate:
    feature: str
    group: str
    confidence: float
    params: list[str] = field(default_factory=list)
    define_gates: list[str] = field(default_factory=list)
    param_gates: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class LlmInitialParamSuggestion:
    laya_param: str
    suggested_value: Any
    confidence: float = 0.0
    reason: str = ""
    source_unity_params: list[str] = field(default_factory=list)


def build_llm_semantics_context(
    *,
    laya_shader: dict[str, Any],
    laya_material_params: dict[str, Any],
    laya_material_defines: list[str],
    unity_shader: dict[str, Any] | None = None,
    unity_material_params: dict[str, Any] | None = None,
    visual_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic input package for a shader-semantics LLM call."""

    return {
        "task": {
            "goal": "infer_unity_feature_modules_for_laya_search",
            "allowed_output": "strict_json_semantic_prior",
            "forbidden_actions": ["write_files", "modify_lmat", "run_optimizer"],
            "notes": [
                "Unity material values are visual-effect evidence, not directly transferable Laya targets.",
                "Primary goal: identify enabled Unity feature modules and the evidence for each module.",
                "Do not output per-parameter Laya semantics unless explicitly required.",
            ],
        },
        "laya_shader": laya_shader,
        "laya_material": {
            "params": laya_material_params,
            "defines": laya_material_defines,
        },
        "unity_shader": unity_shader,
        "unity_material": unity_material_params or {},
        "optional_visual_feedback": visual_feedback or {},
        "output_schema": {
            "unity_feature_summary": [
                {
                    "feature": "base_color|normal|occlusion|metallic_smoothness|specular|secondary_specular|matcap_reflection|rim_or_fresnel|emission|color_grade|alpha|other",
                    "enabled": True,
                    "confidence": 0.0,
                    "evidence": ["keyword/texture/value/formula evidence"],
                    "unity_params": ["Unity property names that prove this feature"],
                    "textures": ["texture slots or texture names used by this feature"],
                    "controls": ["color|intensity|shape|texture|mask|remap|gate"],
                    "laya_candidate_groups": [
                        "base_color|shadow_diffuse|specular_smoothness|reflection_matcap|fresnel|emission|color_grade|misc"
                    ],
                    "risk": "brief uncertainty or cross-engine mismatch risk",
                }
            ],
            "laya_module_candidates": [],
            "unity_phenomena": [
                {
                    "name": "rim_or_fresnel|emission|matcap|specular|base_color|color_grade|other",
                    "confidence": 0.0,
                    "unity_evidence": ["brief Unity shader/material evidence"],
                    "laya_candidate_groups": [
                        "base_color|shadow_diffuse|specular_smoothness|reflection_matcap|fresnel|emission|color_grade|misc"
                    ],
                    "note": "brief explanation",
                }
            ],
            "param_semantics": [],
            "initial_laya_param_suggestions": [],
        },
    }


def validate_llm_semantics_output(
    payload: dict[str, Any],
    *,
    allowed_params: set[str],
    allowed_defines: set[str],
) -> dict[str, Any]:
    """Keep only valid semantic hints; never trust model output directly."""

    if not isinstance(payload, dict):
        return {
            "unity_feature_summary": [],
            "laya_module_candidates": [],
            "unity_phenomena": [],
            "param_semantics": [],
            "initial_laya_param_suggestions": [],
            "warnings": ["LLM output is not an object"],
        }
    raw = payload.get("param_semantics")
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    has_module_output = isinstance(payload.get("unity_feature_summary"), list) or isinstance(payload.get("unity_phenomena"), list)
    if not isinstance(raw, list):
        if not has_module_output:
            warnings.append("LLM output missing param_semantics list")
        raw = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in allowed_params:
            warnings.append(f"ignored unknown param {name!r}")
            continue
        gates: list[dict[str, Any]] = []
        for gate in item.get("gates", []):
            if not isinstance(gate, dict):
                continue
            gate_name = gate.get("name")
            gate_kind = str(gate.get("kind", "define"))
            if not isinstance(gate_name, str):
                continue
            if gate_kind == "define" and gate_name not in allowed_defines:
                warnings.append(f"ignored unknown define gate {gate_name!r} for {name}")
                continue
            if gate_kind == "param_nonzero" and gate_name not in allowed_params:
                warnings.append(f"ignored unknown param gate {gate_name!r} for {name}")
                continue
            gates.append(
                asdict(
                    LlmSemanticGate(
                        kind=gate_kind,
                        name=gate_name,
                        expected=gate.get("expected", True),
                        reason=str(gate.get("reason", "")),
                    )
                )
            )
        sem = LlmParamSemantic(
            name=name,
            group=_enum(item.get("group"), _GROUPS, "misc"),
            role=_enum(item.get("role"), _ROLES, "value"),
            transform=_enum(item.get("transform"), _TRANSFORMS, "linear"),
            gates=[LlmSemanticGate(**gate) for gate in gates],
            dependencies=[
                dep for dep in item.get("dependencies", [])
                if isinstance(dep, str) and dep in allowed_params
            ],
            searchable=bool(item.get("searchable", True)),
            reason=str(item.get("reason", "")),
        )
        out.append(asdict(sem))
    return {
        "unity_feature_summary": _validate_unity_features(payload.get("unity_feature_summary"), warnings),
        "laya_module_candidates": _validate_laya_module_candidates(
            payload.get("laya_module_candidates"),
            allowed_params=allowed_params,
            allowed_defines=allowed_defines,
            warnings=warnings,
        ),
        "unity_phenomena": _validate_unity_phenomena(payload.get("unity_phenomena"), warnings),
        "param_semantics": out,
        "initial_laya_param_suggestions": _validate_initial_suggestions(
            payload.get("initial_laya_param_suggestions"),
            allowed_params=allowed_params,
            warnings=warnings,
        ),
        "warnings": warnings,
    }


def run_llm_semantics_provider(
    context: dict[str, Any],
    provider: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    allowed_params: set[str],
    allowed_defines: set[str],
) -> dict[str, Any]:
    """Call an injected provider and validate that it only returns semantics."""

    raw = provider(context)
    return validate_llm_semantics_output(
        raw,
        allowed_params=allowed_params,
        allowed_defines=allowed_defines,
    )


_GROUPS = {
    "base_color",
    "shadow_diffuse",
    "specular_smoothness",
    "reflection_matcap",
    "fresnel",
    "emission",
    "color_grade",
    "misc",
}
_ROLES = {"color", "intensity", "gate", "shape", "angle", "texture", "value"}
_TRANSFORMS = {"linear", "log", "circular", "color_rgb"}


def _validate_unity_features(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("ignored unity_feature_summary because it is not a list")
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        groups = [
            _enum(group, _GROUPS, "misc")
            for group in item.get("laya_candidate_groups", [])
            if isinstance(group, str)
        ]
        out.append(
            asdict(
                LlmUnityFeature(
                    feature=str(item.get("feature") or item.get("name") or "other"),
                    enabled=bool(item.get("enabled", True)),
                    confidence=_confidence(item.get("confidence")),
                    evidence=_string_list(item.get("evidence") or item.get("unity_evidence"), limit=10),
                    unity_params=_string_list(item.get("unity_params"), limit=12),
                    textures=_string_list(item.get("textures"), limit=8),
                    controls=_string_list(item.get("controls"), limit=12),
                    laya_candidate_groups=sorted(set(groups)),
                    risk=str(item.get("risk", "")),
                )
            )
        )
    return out


def _validate_laya_module_candidates(
    raw: Any,
    *,
    allowed_params: set[str],
    allowed_defines: set[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("ignored laya_module_candidates because it is not a list")
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        params = [name for name in _string_list(item.get("params"), limit=64) if name in allowed_params]
        define_gates = [name for name in _string_list(item.get("define_gates"), limit=32) if name in allowed_defines]
        param_gates = [name for name in _string_list(item.get("param_gates"), limit=32) if name in allowed_params]
        out.append(
            asdict(
                LlmLayaModuleCandidate(
                    feature=str(item.get("feature") or "other"),
                    group=_enum(item.get("group"), _GROUPS, "misc"),
                    confidence=_confidence(item.get("confidence")),
                    params=params,
                    define_gates=define_gates,
                    param_gates=param_gates,
                    reason=str(item.get("reason", "")),
                )
            )
        )
    return out


def _validate_unity_phenomena(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("ignored unity_phenomena because it is not a list")
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        candidate_groups = [
            _enum(group, _GROUPS, "misc")
            for group in item.get("laya_candidate_groups", [])
            if isinstance(group, str)
        ]
        out.append(
            asdict(
                LlmUnityPhenomenon(
                    name=str(item.get("name") or "other"),
                    confidence=_confidence(item.get("confidence")),
                    unity_evidence=[
                        str(evidence)
                        for evidence in item.get("unity_evidence", [])
                        if isinstance(evidence, str)
                    ][:8],
                    laya_candidate_groups=sorted(set(candidate_groups)),
                    note=str(item.get("note", "")),
                )
            )
        )
    return out


def _validate_initial_suggestions(
    raw: Any,
    *,
    allowed_params: set[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("ignored initial_laya_param_suggestions because it is not a list")
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("laya_param")
        if not isinstance(name, str) or name not in allowed_params:
            warnings.append(f"ignored initial suggestion for unknown param {name!r}")
            continue
        out.append(
            asdict(
                LlmInitialParamSuggestion(
                    laya_param=name,
                    suggested_value=item.get("suggested_value"),
                    confidence=_confidence(item.get("confidence")),
                    reason=str(item.get("reason", "")),
                    source_unity_params=[
                        str(param)
                        for param in item.get("source_unity_params", [])
                        if isinstance(param, str)
                    ][:8],
                )
            )
        )
    return out


def _enum(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)][:limit]

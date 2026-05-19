"""Pre-analysis pipeline.

Given a project's ``inputs`` block, parse the Unity and Laya shaders, build
a Unity↔Laya parameter mapping, and predict which stages of the existing
adjustment plan apply. Results are persisted to ``preanalysis.json`` under
the project directory and surfaced to the UI.

Mapping pipeline (in priority order)
------------------------------------

1. **Manual override** — entries from ``project.json.manual_param_mapping``.
   Highest priority; user-curated, never overridden by anything else.
2. **Curated dictionary** — hand-rolled translations for common Unity
   shaders (Standard / URP Lit / Toon) into the Laya FishStandard idiom.
3. **Exact normalized name** — strip ``_``/``u_`` prefix, lowercase,
   alphanumeric-only; if both sides collapse to the same key it counts as
   ``exact``.
4. **Type-aware fuzzy** — for the leftovers we run a token similarity score,
   but require type compatibility (Color↔Color, Float/Range/Int↔scalar,
   Vector↔Vector, 2D↔2D, Cube↔Cube) and threshold ≥0.85. This is a deliberate
   tightening from the previous 0.6 threshold which produced false positives
   like ``_ColorScale (Range)`` ↔ ``u_BaseColor (Color)``.
5. ``unity_only`` / ``laya_only`` — leftovers on either side; the UI lets the
   user manually pair them and saves the result back as a manual override.

The mapping is **also** persisted into ``project.json.effective_param_mapping``
so the runtime ``fit_material.py`` can use it for value anchoring (currently
disabled there because the bare exact-name path was unreachable across engines).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
from typing import Any

from .case_loader import LoaderConfig, PROJECT_ROOT, _to_rel_posix
from .preanalysis_parts.fish_standard import fish_standard_effective_bounds_for_schema
from .project_store import get_project, project_paths


def run_preanalysis(
    project_id: str,
    config: LoaderConfig | None = None,
    *,
    use_llm: bool | None = None,
) -> dict[str, Any]:
    config = config or LoaderConfig()
    project = get_project(project_id, config)
    inputs = project.get("inputs") or {}
    manual_mapping = project.get("manual_param_mapping") or {}

    laya_shader_path = inputs.get("laya_shader_path") or ""
    unity_shader_path = inputs.get("unity_shader_path") or ""
    laya_lmat_path = inputs.get("laya_material_lmat_path") or ""

    if not laya_shader_path:
        raise ValueError("preanalysis requires inputs.laya_shader_path")

    laya_info = _parse_laya(laya_shader_path)
    unity_info = _parse_unity(unity_shader_path) if unity_shader_path else None
    laya_material_params = _read_lmat_params(laya_lmat_path) if laya_lmat_path else {}
    laya_material_defines = _read_lmat_defines(laya_lmat_path) if laya_lmat_path else []
    unity_material_params = _read_unity_params(inputs.get("unity_material_params_path") or "")

    mapping_rows = _build_param_mapping(unity_info, laya_info, manual_mapping=manual_mapping)
    stage_plan = _predict_stage_plan(laya_info)
    llm_semantics_context = _build_llm_semantics_context(
        laya_info=laya_info,
        laya_material_params=laya_material_params,
        laya_material_defines=laya_material_defines,
        unity_info=unity_info,
        unity_material_params=unity_material_params,
    )
    llm_semantics = _run_llm_semantics(
        project,
        context=llm_semantics_context,
        laya_info=laya_info,
        config=config,
        use_llm=use_llm,
    )
    effect_graph = _build_effect_graph(
        laya_info,
        material_params=laya_material_params,
        material_defines=laya_material_defines,
        llm_semantics=llm_semantics.get("validated") if isinstance(llm_semantics, dict) else None,
    )
    module_plan = _build_module_plan(
        llm_semantics=llm_semantics.get("validated") if isinstance(llm_semantics, dict) else None,
        effect_graph=effect_graph,
    )
    auto_laya_control_groups = _build_laya_control_groups(
        laya_info=laya_info,
        effect_graph=effect_graph,
        material_params=laya_material_params,
        material_defines=laya_material_defines,
        module_plan=module_plan["module_plan"],
    )
    auto_laya_control_schema = _control_groups_to_schema(
        auto_laya_control_groups,
        source={
            "kind": "auto",
            "generator": "rules+llm",
            "shader_name": laya_info.get("name"),
            "shader_path": laya_info.get("path"),
        },
    )
    manual_laya_control_schema = _normalize_manual_laya_control_schema(
        project.get("manual_laya_control_schema"),
    )
    effective_laya_control_schema = build_effective_laya_control_schema(
        auto_laya_control_schema,
        manual_laya_control_schema,
        run_overrides=(project.get("algorithm_config") or {}).get("laya_control_group_overrides"),
    )
    laya_control_groups = _schema_to_control_groups(effective_laya_control_schema)

    coverage = _compute_coverage(mapping_rows)
    initial_recommendations = _initial_recommendations(
        unity_material_params=unity_material_params,
        laya_material_params=laya_material_params,
        mapping=mapping_rows,
        laya_param_meta={p["name"]: p for p in laya_info["params"]},
    )

    payload: dict[str, Any] = {
        "project_id": project_id,
        "ran_at": _now_iso(),
        "unity_shader": unity_info,
        "laya_shader": laya_info,
        "laya_material_params": laya_material_params,
        "laya_material_defines": laya_material_defines,
        "unity_material_params": unity_material_params,
        "param_mapping": mapping_rows,
        "stage_plan": stage_plan,
        "effect_graph": effect_graph,
        "unity_feature_summary": module_plan["unity_feature_summary"],
        "laya_module_candidates": module_plan["laya_module_candidates"],
        "module_plan": module_plan["module_plan"],
        "auto_laya_control_schema": auto_laya_control_schema,
        "manual_laya_control_schema": manual_laya_control_schema,
        "effective_laya_control_schema": effective_laya_control_schema,
        "laya_control_groups": laya_control_groups,
        "llm_semantics_context": llm_semantics_context,
        "llm_semantics": llm_semantics,
        "coverage": coverage,
        "initial_recommendations": initial_recommendations,
        "warnings": _collect_warnings(
            unity_info,
            laya_info,
            mapping_rows,
            inputs,
            suppress_mapping_warnings=bool(module_plan["module_plan"]),
        ),
        "mapping_notes": _collect_mapping_notes(mapping_rows),
    }

    paths = project_paths(project_id, config)
    paths.preanalysis_json.parent.mkdir(parents=True, exist_ok=True)
    paths.preanalysis_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rel_pre = _to_rel_posix(paths.preanalysis_json, config.project_root)
    from .project_store import patch_project

    patch_project(project_id, {"preanalysis_path": rel_pre}, config=config)
    return payload


def get_preanalysis(project_id: str, config: LoaderConfig | None = None) -> dict[str, Any] | None:
    config = config or LoaderConfig()
    paths = project_paths(project_id, config)
    preanalysis_path = paths.preanalysis_json if paths.preanalysis_json.exists() else paths.project_dir / "preanalysis.json"
    if not preanalysis_path.exists():
        return None
    try:
        return json.loads(preanalysis_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def save_manual_laya_control_schema(
    project_id: str,
    manual_schema: dict[str, Any],
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Persist human control-schema edits and refresh cached effective schema."""

    config = config or LoaderConfig()
    from .project_store import patch_project

    cleaned = _normalize_manual_laya_control_schema(manual_schema)
    patch_project(
        project_id,
        {
            "manual_laya_control_schema": cleaned,
            "active_laya_control_schema_preset_id": "custom",
        },
        config=config,
    )
    return _refresh_laya_control_schema_payload(project_id, cleaned, config)


def list_laya_control_schema_presets(
    project_id: str,
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Return built-in presets plus project-local custom presets."""

    config = config or LoaderConfig()
    project = get_project(project_id, config)
    presets = _builtin_laya_control_schema_presets()
    for raw in project.get("laya_control_schema_presets", []):
        preset = _normalize_laya_control_schema_preset(raw)
        if preset is not None:
            presets.append(preset)
    return {
        "active_preset_id": str(project.get("active_laya_control_schema_preset_id") or "auto"),
        "presets": presets,
    }


def apply_laya_control_schema_preset(
    project_id: str,
    preset_id: str,
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Apply a preset by replacing the project's manual control schema."""

    config = config or LoaderConfig()
    from .project_store import patch_project

    project = get_project(project_id, config)
    preset_id = str(preset_id or "auto")
    if preset_id == "auto":
        manual_schema = _empty_manual_laya_control_schema(base_auto_hash="auto")
    else:
        preset = _find_laya_control_schema_preset(project, preset_id)
        if preset is None:
            raise ValueError(f"unknown laya control preset: {preset_id}")
        manual_schema = _normalize_manual_laya_control_schema(preset.get("manual_laya_control_schema"))
        manual_schema["base_auto_hash"] = preset_id
    patch_project(
        project_id,
        {
            "manual_laya_control_schema": manual_schema,
            "active_laya_control_schema_preset_id": preset_id,
        },
        config=config,
    )
    return _refresh_laya_control_schema_payload(project_id, manual_schema, config)


def save_laya_control_schema_preset(
    project_id: str,
    name: str,
    description: str = "",
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Save the current manual schema as a project-local preset."""

    config = config or LoaderConfig()
    from .project_store import patch_project

    project = get_project(project_id, config)
    preset_name = str(name or "").strip()
    if not preset_name:
        raise ValueError("preset name is required")
    presets = [
        preset
        for raw in project.get("laya_control_schema_presets", [])
        if (preset := _normalize_laya_control_schema_preset(raw)) is not None and not preset.get("builtin")
    ]
    preset_id = _unique_preset_id(preset_name, presets)
    manual_schema = _normalize_manual_laya_control_schema(project.get("manual_laya_control_schema"))
    manual_schema["base_auto_hash"] = preset_id
    preset = {
        "id": preset_id,
        "name": preset_name,
        "description": str(description or ""),
        "builtin": False,
        "shader_hint": _current_shader_hint(project),
        "manual_laya_control_schema": manual_schema,
    }
    presets.append(preset)
    patch_project(
        project_id,
        {
            "laya_control_schema_presets": presets,
            "manual_laya_control_schema": manual_schema,
            "active_laya_control_schema_preset_id": preset_id,
        },
        config=config,
    )
    return list_laya_control_schema_presets(project_id, config)


def rename_laya_control_schema_preset(
    project_id: str,
    preset_id: str,
    name: str,
    description: str = "",
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    config = config or LoaderConfig()
    from .project_store import patch_project

    project = get_project(project_id, config)
    preset_name = str(name or "").strip()
    if not preset_name:
        raise ValueError("preset name is required")
    presets: list[dict[str, Any]] = []
    updated = False
    for raw in project.get("laya_control_schema_presets", []):
        preset = _normalize_laya_control_schema_preset(raw)
        if preset is None or preset.get("builtin"):
            continue
        if preset.get("id") == preset_id:
            preset["name"] = preset_name
            preset["description"] = str(description or "")
            updated = True
        presets.append(preset)
    if not updated:
        raise ValueError(f"custom preset not found: {preset_id}")
    patch_project(project_id, {"laya_control_schema_presets": presets}, config=config)
    return list_laya_control_schema_presets(project_id, config)


def delete_laya_control_schema_preset(
    project_id: str,
    preset_id: str,
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    config = config or LoaderConfig()
    from .project_store import patch_project

    project = get_project(project_id, config)
    presets: list[dict[str, Any]] = []
    deleted = False
    for raw in project.get("laya_control_schema_presets", []):
        preset = _normalize_laya_control_schema_preset(raw)
        if preset is None or preset.get("builtin"):
            continue
        if preset.get("id") == preset_id:
            deleted = True
            continue
        presets.append(preset)
    if not deleted:
        raise ValueError(f"custom preset not found: {preset_id}")
    patch: dict[str, Any] = {"laya_control_schema_presets": presets}
    if project.get("active_laya_control_schema_preset_id") == preset_id:
        patch["active_laya_control_schema_preset_id"] = "auto"
        patch["manual_laya_control_schema"] = _empty_manual_laya_control_schema(base_auto_hash="auto")
    patch_project(project_id, patch, config=config)
    return list_laya_control_schema_presets(project_id, config)


def _parse_laya(path: str) -> dict[str, Any]:
    from tools.material_fit.laya.shader_parser import parse_laya_shader, shader_info_to_dict

    info = parse_laya_shader(path)
    payload = shader_info_to_dict(info)
    payload["source_excerpt"] = _read_source_excerpt(path)
    return payload


def _parse_unity(path: str) -> dict[str, Any]:
    from tools.material_fit.unity.shader_parser import parse_unity_shaderlab

    info = parse_unity_shaderlab(path)
    return {
        "path": str(info.path),
        "name": info.name,
        "params": [param.__dict__ for param in info.params],
        "defines": [],
        "source_excerpt": _read_source_excerpt(path),
    }


def _read_lmat_params(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        from tools.material_fit.laya import lmat_io

        material = lmat_io.load_lmat(path)
        return lmat_io.extract_params(material)
    except Exception:
        return {}


def _read_lmat_defines(path: str) -> list[str]:
    if not path:
        return []
    try:
        from tools.material_fit.laya import lmat_io

        material = lmat_io.load_lmat(path)
        return [str(item) for item in lmat_io.extract_defines(material)]
    except Exception:
        return []


def _read_unity_params(path: str) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("params"), dict):
        return data["params"]
    if isinstance(data, dict) and isinstance(data.get("properties"), dict):
        return data["properties"]
    return data if isinstance(data, dict) else {}


def _build_effect_graph(
    laya_info: dict[str, Any],
    *,
    material_params: dict[str, Any],
    material_defines: list[str],
    llm_semantics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        from tools.material_fit.optimizer.semantic_graph import (
            build_shader_effect_graph,
            graph_to_dict,
        )
        from tools.material_fit.shared.models import ShaderDefine, ShaderParam

        params = [
            ShaderParam(
                name=p["name"],
                display_name=p.get("display_name", p["name"]),
                param_type=p.get("param_type", ""),
                default=p.get("default"),
                range_min=p.get("range_min"),
                range_max=p.get("range_max"),
                hidden=p.get("hidden"),
            )
            for p in laya_info.get("params", [])
            if isinstance(p, dict) and p.get("name")
        ]
        defines = [
            ShaderDefine(
                name=d["name"],
                define_type=d.get("define_type", "bool"),
                default=d.get("default"),
                position=d.get("position"),
            )
            for d in laya_info.get("defines", [])
            if isinstance(d, dict) and d.get("name")
        ]
        graph = build_shader_effect_graph(
            params,
            shader_defines=defines,
            material_params=material_params,
            material_defines=material_defines,
            llm_semantics=llm_semantics,
        )
        return graph_to_dict(graph)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to build effect graph: {exc}"}


def _build_llm_semantics_context(
    *,
    laya_info: dict[str, Any],
    laya_material_params: dict[str, Any],
    laya_material_defines: list[str],
    unity_info: dict[str, Any] | None,
    unity_material_params: dict[str, Any],
) -> dict[str, Any]:
    try:
        from tools.material_fit.optimizer.llm_semantics import build_llm_semantics_context

        return build_llm_semantics_context(
            laya_shader=laya_info,
            laya_material_params=laya_material_params,
            laya_material_defines=laya_material_defines,
            unity_shader=unity_info,
            unity_material_params=unity_material_params,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to build LLM semantics context: {exc}"}


def _run_llm_semantics(
    project: dict[str, Any],
    *,
    context: dict[str, Any],
    laya_info: dict[str, Any],
    config: LoaderConfig,
    use_llm: bool | None,
) -> dict[str, Any]:
    llm_config = project.get("llm_config") if isinstance(project.get("llm_config"), dict) else {}
    enabled = bool(use_llm) if use_llm is not None else bool(llm_config.get("enabled"))
    if not enabled:
        return {"enabled": False, "status": "skipped", "reason": "LLM semantic inference disabled"}
    from .llm_client import LlmConfigError, load_llm_runtime_config, run_shader_semantics_llm

    try:
        from tools.material_fit.optimizer.llm_semantics import validate_llm_semantics_output

        runtime = load_llm_runtime_config(config.project_root / "tools")
        allowed_params = {
            str(param.get("name"))
            for param in laya_info.get("params", [])
            if isinstance(param, dict) and param.get("name")
        }
        allowed_defines = {
            str(define.get("name"))
            for define in laya_info.get("defines", [])
            if isinstance(define, dict) and define.get("name")
        }
        feature_context = _build_unity_feature_llm_context(context)
        raw = run_shader_semantics_llm(
            feature_context,
            runtime=runtime,
            max_tokens=_env_int("LLM_FEATURE_MAX_TOKENS", 3500),
        )
        validated = validate_llm_semantics_output(
            raw,
            allowed_params=allowed_params,
            allowed_defines=allowed_defines,
        )
        features_count = len(validated.get("unity_feature_summary", [])) + len(validated.get("unity_phenomena", []))
        status = "ok" if features_count else "failed"
        warnings = list(validated.get("warnings", []))
        if not features_count:
            warnings.append("LLM did not return any Unity feature modules")
        return {
            "enabled": True,
            "status": status,
            "provider": "openai-compatible",
            "runtime": runtime.public_dict(),
            "mode": "unity_feature_summary",
            "validated": validated,
            "warnings": warnings,
        }
    except LlmConfigError as exc:
        return {"enabled": True, "status": "not_configured", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "status": "failed", "error": str(exc)}


def _build_module_plan(
    *,
    llm_semantics: dict[str, Any] | None,
    effect_graph: dict[str, Any],
) -> dict[str, Any]:
    features = _normalized_unity_features(llm_semantics)
    candidates = _normalized_laya_candidates(llm_semantics)
    groups = effect_graph.get("groups") if isinstance(effect_graph, dict) else {}
    groups = groups if isinstance(groups, dict) else {}

    plan: list[dict[str, Any]] = []
    for group_name, group in groups.items():
        if not isinstance(group, dict):
            continue
        matched_features = [
            feature
            for feature in features
            if group_name in feature.get("laya_candidate_groups", [])
        ]
        matched_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("group") == group_name
        ]
        suggested = bool(matched_features or matched_candidates or group.get("suggested_by_unity"))
        current_active = bool(group.get("current_active", group.get("active", True)))
        search_params = _unique_strings(
            [
                param
                for candidate in matched_candidates
                for param in candidate.get("params", [])
                if isinstance(param, str)
            ]
        ) or [str(param) for param in group.get("search_params", []) if isinstance(param, str)]
        if not search_params:
            search_params = [str(param) for param in group.get("params", []) if isinstance(param, str)]
        confidence = max(
            [float(feature.get("confidence", 0.0) or 0.0) for feature in matched_features]
            + [float(candidate.get("confidence", 0.0) or 0.0) for candidate in matched_candidates]
            + [float(group.get("search_priority", 0.0) or 0.0)]
            + [0.0]
        )
        action = "skip_low_confidence"
        if suggested and current_active:
            action = "optimize_group"
        elif suggested:
            action = "activate_gate_then_probe"
        elif current_active:
            action = "probe_optional"
        plan.append(
            {
                "group": str(group_name),
                "unity_features": _unique_strings(
                    [str(feature.get("feature", "")) for feature in matched_features]
                    + [str(candidate.get("feature", "")) for candidate in matched_candidates]
                    + [str(item) for item in group.get("unity_features", []) if isinstance(item, str)]
                ),
                "current_active": current_active,
                "suggested_by_unity": suggested,
                "probe_required": bool(suggested and not current_active),
                "search_priority": round(confidence, 3),
                "action": action,
                "params_count": len(group.get("params", [])) if isinstance(group.get("params"), list) else 0,
                "search_params": search_params,
                "gate_params": [str(item) for item in group.get("gate_params", []) if isinstance(item, str)],
                "define_gates": [str(item) for item in group.get("define_gates", []) if isinstance(item, str)],
                "channels": [str(item) for item in group.get("channels", []) if isinstance(item, str)],
                "evidence": _unique_strings(
                    [
                        evidence
                        for feature in matched_features
                        for evidence in feature.get("evidence", [])
                        if isinstance(evidence, str)
                    ]
                    + [str(item) for item in group.get("evidence", []) if isinstance(item, str)]
                )[:10],
                "reason": str(group.get("reason", "")),
            }
        )
    plan.sort(key=lambda item: (not item["suggested_by_unity"], -float(item["search_priority"]), item["group"]))
    return {
        "unity_feature_summary": features,
        "laya_module_candidates": candidates,
        "module_plan": plan,
    }


def _build_laya_control_groups(
    *,
    laya_info: dict[str, Any],
    effect_graph: dict[str, Any],
    material_params: dict[str, Any],
    material_defines: list[str],
    module_plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build a UI-friendly, Inspector-like view of Laya's exposed controls."""

    raw_params = laya_info.get("params") if isinstance(laya_info.get("params"), list) else []
    graph_params = effect_graph.get("params") if isinstance(effect_graph, dict) else {}
    graph_groups = effect_graph.get("groups") if isinstance(effect_graph, dict) else {}
    graph_params = graph_params if isinstance(graph_params, dict) else {}
    graph_groups = graph_groups if isinstance(graph_groups, dict) else {}
    material_define_set = {str(item) for item in material_defines}

    controls_by_group: dict[str, list[dict[str, Any]]] = {}
    for raw in raw_params:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        name = str(raw["name"])
        sem = graph_params.get(name) if isinstance(graph_params.get(name), dict) else {}
        group_name = str(sem.get("group") or "misc")
        gates = [gate for gate in sem.get("gates", []) if isinstance(gate, dict)]
        gate_status = _control_gate_status(gates, material_params, material_define_set)
        control = {
            "name": name,
            "display_name": raw.get("display_name") or name,
            "param_type": str(raw.get("param_type") or ""),
            "current_value": material_params.get(name, raw.get("default")),
            "default": raw.get("default"),
            "range": [raw.get("range_min"), raw.get("range_max")],
            "hidden": raw.get("hidden"),
            "group": group_name,
            "role": str(sem.get("role") or "value"),
            "transform": str(sem.get("transform") or "linear"),
            "searchable": bool(sem.get("searchable", True)),
            "is_gate": any(gate.get("name") == name for gate in gates) or str(sem.get("role") or "") == "gate",
            "gates": gates,
            "gate_status": gate_status,
            "dependencies": [str(item) for item in sem.get("dependencies", []) if isinstance(item, str)],
            "reason": str(sem.get("reason") or ""),
        }
        controls_by_group.setdefault(group_name, []).append(control)

    plan_by_group = {
        str(item.get("group")): item
        for item in module_plan
        if isinstance(item, dict) and item.get("group")
    }
    group_order = [
        str(item.get("group"))
        for item in module_plan
        if isinstance(item, dict) and item.get("group") in controls_by_group
    ]
    for group_name in controls_by_group:
        if group_name not in group_order:
            group_order.append(group_name)

    out: list[dict[str, Any]] = []
    for group_name in group_order:
        controls = controls_by_group.get(group_name, [])
        if not controls:
            continue
        group = graph_groups.get(group_name) if isinstance(graph_groups.get(group_name), dict) else {}
        plan = plan_by_group.get(group_name, {})
        search_params = {
            str(item)
            for item in plan.get("search_params", group.get("search_params", []))
            if isinstance(item, str)
        }
        gate_params = {
            str(item)
            for item in group.get("gate_params", [])
            if isinstance(item, str)
        }
        for control in controls:
            control["is_search_param"] = control["name"] in search_params
            control["is_gate"] = bool(control["is_gate"] or control["name"] in gate_params)
        controls.sort(key=lambda item: (not bool(item["is_gate"]), not bool(item["is_search_param"]), item["name"]))
        out.append(
            {
                "group": group_name,
                "label": _laya_control_group_label(group_name),
                "description": _laya_control_group_description(group_name),
                "current_active": bool(group.get("current_active", group.get("active", True))),
                "suggested_by_unity": bool(group.get("suggested_by_unity", False) or plan.get("suggested_by_unity", False)),
                "probe_required": bool(group.get("probe_required", False) or plan.get("probe_required", False)),
                "search_priority": float(plan.get("search_priority", group.get("search_priority", 0.0)) or 0.0),
                "reason": str(group.get("reason") or plan.get("reason") or ""),
                "channels": [str(item) for item in group.get("channels", []) if isinstance(item, str)],
                "define_gates": [str(item) for item in group.get("define_gates", []) if isinstance(item, str)],
                "gate_params": sorted(gate_params),
                "controls": controls,
                "searchable_count": sum(1 for control in controls if control["searchable"]),
                "gate_count": sum(1 for control in controls if control["is_gate"]),
            }
        )
    return out


def _fish_standard_preset_manual_schema() -> dict[str, Any]:
    template = _fish_standard_template()
    effective_bounds = _fish_standard_effective_bounds()
    groups: dict[str, dict[str, Any]] = {}
    controls: dict[str, dict[str, Any]] = {}
    hidden_controls: list[str] = []
    for order, (group_id, meta) in enumerate(template.items()):
        groups[group_id] = {
            "label": meta["label"],
            "description": meta["description"],
            "enabled": True,
            "locked": True,
            "order": order * 10,
            "search_priority": float(meta.get("priority", 0.0)),
            "channels": list(meta.get("channels", [])),
            "define_gates": list(meta.get("define_gates", [])),
            "gate_params": [
                name
                for name, item in meta["params"].items()
                if isinstance(item, dict) and item.get("gate")
            ],
        }
        for param_name, control_meta in meta["params"].items():
            if control_meta.get("hidden"):
                hidden_controls.append(param_name)
                continue
            controls[param_name] = {
                "group": group_id,
                "role": control_meta.get("role", "value"),
                "searchable": bool(control_meta.get("searchable", True)),
                "is_search_param": bool(control_meta.get("searchable", True)),
                "is_gate": bool(control_meta.get("gate", False)),
                "reason": control_meta.get("reason", "curated FishStandard template"),
                "locked_fields": ["group", "role", "searchable", "is_gate"],
            }
            if param_name in effective_bounds:
                controls[param_name].update(effective_bounds[param_name])
                controls[param_name]["reason"] = (
                    f"{controls[param_name]['reason']}; effective visual search bounds from "
                    "FishStandard expert sample and saturation-risk review"
                )
    return {
        "schema_version": 1,
        "base_auto_hash": "builtin_fish_standard_v2_effective_bounds",
        "groups": groups,
        "controls": controls,
        "deleted_groups": [],
        "hidden_controls": hidden_controls,
    }


def _fish_standard_effective_bounds() -> dict[str, dict[str, Any]]:
    return fish_standard_effective_bounds_for_schema()


def _is_fish_standard_laya_info(laya_info: dict[str, Any]) -> bool:
    shader_name = str(laya_info.get("name") or "").lower()
    shader_path = str(laya_info.get("path") or "").lower().replace("\\", "/")
    return "fishstandard" in shader_name or "fishstandard.shader" in shader_path


def _fish_standard_template() -> dict[str, dict[str, Any]]:
    fixed = {"role": "texture", "searchable": False, "reason": "texture/ST slot; show as dependency, do not optimize numerically"}
    return {
        "base_color": {
            "label": "基础色 / 主体亮度",
            "description": "基础贴图、基础色和全局 gamma。u_Gamma_Power 也会影响 emission 与 Matcap 采样。",
            "priority": 0.90,
            "channels": ["base_color_main_texture"],
            "define_gates": [],
            "params": {
                "u_BaseColor": {"role": "color", "searchable": True},
                "u_BaseMap": fixed,
                "u_BaseMap_ST": {**fixed, "role": "uv_transform"},
                "u_Gamma_Power": {"role": "global_gamma", "searchable": True},
            },
        },
        "diffuse_shadow": {
            "label": "阴影 / 漫反射层次",
            "description": "Toon diffuse ramp、暗部颜色、遮蔽与 GI 强度，会影响整体明暗层次。",
            "priority": 0.80,
            "channels": ["shadow_occlusion", "base_color_main_texture"],
            "define_gates": [],
            "params": {
                "u_DiffuseThreshold": {"role": "shape", "searchable": True},
                "u_DiffuseSmoothness": {"role": "shape", "searchable": True},
                "u_GIIntensity": {"role": "intensity", "searchable": True},
                "u_OcclusionStrength": {"role": "intensity", "searchable": True},
                "u_ShadowColor": {"role": "color", "searchable": True},
            },
        },
        "metallic_smoothness": {
            "label": "Metallic / Smoothness / MAER",
            "description": "MAER 打包图及金属度、光滑度 remap。贴图只展示，数值 remap 可搜索。",
            "priority": 0.70,
            "channels": ["metallic_smoothness_specular"],
            "define_gates": [],
            "params": {
                "u_MAER": {**fixed, "role": "packed_texture"},
                "u_MAER_ST": {**fixed, "role": "uv_transform"},
                "u_Metallic": {"role": "intensity", "searchable": True},
                "u_MetallicRemapMin": {"role": "remap", "searchable": True},
                "u_MetallicRemapMax": {"role": "remap", "searchable": True},
                "u_Smoothness": {"role": "shape", "searchable": True},
                "u_SmoothnessRemapMin": {"role": "remap", "searchable": True},
                "u_SmoothnessRemapMax": {"role": "remap", "searchable": True},
            },
        },
        "main_specular": {
            "label": "主高光 / Specular",
            "description": "主高光颜色、强度、阈值、平滑和 GGX 混合。",
            "priority": 0.75,
            "channels": ["metallic_smoothness_specular"],
            "define_gates": [],
            "params": {
                "u_SpecularColor": {"role": "color", "searchable": True},
                "u_SpecularIntensity": {"role": "intensity", "searchable": True},
                "u_SpecularThreshold": {"role": "shape", "searchable": True},
                "u_SpecularSmooth": {"role": "shape", "searchable": True},
                "u_GGXSpecular": {"role": "blend", "searchable": True},
                "u_MluAlbedoColor": {"role": "albedo_tint_mix", "searchable": True},
                "u_SpecularLightOffset": {"role": "direction_offset", "searchable": True},
                "u_SpecularHighlights": {"role": "gate", "searchable": True, "gate": True},
            },
        },
        "secondary_specular": {
            "label": "第二高光",
            "description": "SPECULARSECOND define 控制的额外高光层。",
            "priority": 0.45,
            "channels": ["metallic_smoothness_specular"],
            "define_gates": ["SPECULARSECOND"],
            "params": {
                "u_SpecularSecondColor": {"role": "color", "searchable": True},
                "u_SpecularSecondIntensity": {"role": "intensity", "searchable": True},
                "u_SpecularSecondThreshold": {"role": "shape", "searchable": True},
                "u_SpecularSecondLightOffset": {"role": "direction_offset", "searchable": True},
            },
        },
        "ibl_reflection": {
            "label": "IBL 环境反射",
            "description": "Cube IBL、旋转、颜色、强度和环境反射开关，受 u_Mask.r 影响。",
            "priority": 0.50,
            "channels": ["environment_reflection_matcap"],
            "define_gates": [],
            "params": {
                "u_IBLMap": {**fixed, "role": "cube_texture"},
                "u_IBLMapColor": {"role": "color", "searchable": True},
                "u_IBLMapIntensity": {"role": "intensity", "searchable": True},
                "u_IBLMapPower": {"role": "shape", "searchable": True},
                "u_IBLMapRotateX": {"role": "angle", "searchable": True},
                "u_IBLMapRotateY": {"role": "angle", "searchable": True},
                "u_IBLMapRotateZ": {"role": "angle", "searchable": True},
                "u_EnvironmentReflections": {"role": "gate", "searchable": True, "gate": True},
            },
        },
        "matcap": {
            "label": "Matcap",
            "description": "主 Matcap 贴图、角度、pow、颜色和强度，受 u_Mask.g 影响。",
            "priority": 0.50,
            "channels": ["environment_reflection_matcap"],
            "define_gates": [],
            "params": {
                "u_MatcapMap": fixed,
                "u_MatcapMap_ST": {**fixed, "role": "uv_transform"},
                "u_MatcapAngle": {"role": "angle", "searchable": True},
                "u_MatcapStrength": {"role": "gate", "searchable": True, "gate": True},
                "u_MatcapPow": {"role": "shape", "searchable": True},
                "u_MatcapColor": {"role": "color", "searchable": True},
            },
        },
        "matcap_add": {
            "label": "Matcap Add",
            "description": "附加 Matcap 加亮层，受 u_Mask.b 影响。",
            "priority": 0.45,
            "channels": ["environment_reflection_matcap"],
            "define_gates": [],
            "params": {
                "u_MatcapAddMap": fixed,
                "u_MatcapAddMap_ST": {**fixed, "role": "uv_transform"},
                "u_MatcapAddAngle": {"role": "angle", "searchable": True},
                "u_MatcapAddStrength": {"role": "gate", "searchable": True, "gate": True},
                "u_MatcapAddPow": {"role": "shape", "searchable": True},
                "u_MatcapAddColor": {"role": "color", "searchable": True},
            },
        },
        "emission": {
            "label": "自发光",
            "description": "EMISSION define 控制的自发光颜色、贴图和强度，另受 MAER.b 影响。",
            "priority": 0.65,
            "channels": ["emission"],
            "define_gates": ["EMISSION"],
            "params": {
                "u_EmissionColor": {"role": "color", "searchable": True},
                "u_EmissionTexture": fixed,
                "u_EmissionTexture_ST": {**fixed, "role": "uv_transform"},
                "u_EmissionScale": {"role": "gate", "searchable": True, "gate": True},
                "u_EmissionPower": {"hidden": True},
            },
        },
        "fresnel": {
            "label": "Fresnel / 边缘光",
            "description": "轮廓边缘光，受强度 gate、u_Mask.a、Fresnel define 变体和顶点色影响。",
            "priority": 0.75,
            "channels": ["fresnel_rim", "center_vs_edge_balance"],
            "define_gates": ["ENABLE_FRESNEL_METALLIC", "ENABLE_DIRECTIONAL_FRESNEL"],
            "params": {
                "u_FresnelColor": {"role": "color", "searchable": True},
                "u_fresnelOffset": {"role": "direction_offset", "searchable": True},
                "u_FresnelThreshold": {"role": "shape", "searchable": True},
                "u_FresnelSmooth": {"role": "shape", "searchable": True},
                "u_FresnelIntensity": {"role": "gate", "searchable": True, "gate": True},
                "u_FresnelUesF0": {"role": "blend", "searchable": True},
                "u_FresnelPow": {"role": "shape", "searchable": True},
                "u_FresnelUseMoldeNormal": {"role": "normal_source", "searchable": True},
            },
        },
        "color_grade": {
            "label": "HSL / Contrast 调色",
            "description": "最终颜色阶段的 HSL 与 Contrast，受 ADJUST_HSV / ENABLE_CONTRAST define 控制。",
            "priority": 0.60,
            "channels": ["color_grading_hsv_contrast"],
            "define_gates": ["ADJUST_HSV", "ENABLE_CONTRAST"],
            "params": {
                "u_AdjustHue": {"role": "angle", "searchable": True},
                "u_AdjustSaturation": {"role": "value", "searchable": True},
                "u_AdjustLightness": {"role": "value", "searchable": True},
                "u_saturationProtection": {"role": "gate", "searchable": True, "gate": True},
                "u_ContrastScale": {"role": "value", "searchable": True},
            },
        },
        "normal": {
            "label": "Normal / 法线",
            "description": "法线贴图和强度，会影响 diffuse、specular、IBL、Matcap、Fresnel。默认不纳入自动数值搜索。",
            "priority": 0.20,
            "channels": [],
            "define_gates": ["TANGENT"],
            "params": {
                "u_BumpMap": fixed,
                "u_BumpMap_ST": {**fixed, "role": "uv_transform"},
                "u_BumpScale": {"role": "intensity", "searchable": False, "reason": "normal strength is high-risk; enable manually if needed"},
            },
        },
        "effect_mask": {
            "label": "Effect Mask / 通道遮罩",
            "description": "u_Mask.r/g/b/a 分别控制 IBL、Matcap、MatcapAdd、Fresnel，默认只作为依赖展示。",
            "priority": 0.10,
            "channels": [],
            "define_gates": [],
            "params": {
                "u_Mask": {**fixed, "role": "effect_mask_texture"},
                "u_Mask_ST": {**fixed, "role": "uv_transform"},
            },
        },
        "lighting_direction": {
            "label": "Lighting Direction / 自定义光向",
            "description": "自定义光照方向，影响 diffuse、高光和 directional Fresnel。默认不自动搜索。",
            "priority": 0.10,
            "channels": [],
            "define_gates": ["CUSTOMLIGHT"],
            "params": {
                "u_SelfLightDir": {"role": "light_direction", "searchable": False, "reason": "global lighting direction; enable manually if needed"},
            },
        },
        "alpha_cutoff": {
            "label": "Alpha / Cutoff",
            "description": "透明度和 AlphaTest cutoff，通常不应参与外观拟合搜索。",
            "priority": 0.05,
            "channels": [],
            "define_gates": ["ALPHATEST"],
            "params": {
                "u_Alpha": {"role": "alpha", "searchable": False},
                "u_Cutoff": {"role": "cutoff", "searchable": False},
            },
        },
    }


def _builtin_laya_control_schema_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": "auto",
            "name": "自动分类",
            "description": "清空人工分类层，使用预分析自动生成的 Laya 控件分组。",
            "builtin": True,
            "shader_hint": "",
            "manual_laya_control_schema": _empty_manual_laya_control_schema(base_auto_hash="auto"),
        },
        {
            "id": "builtin_fish_standard_v1",
            "name": "FishStandard 固定模板",
            "description": "面向当前 FishStandard.shader 的人工整理分类模板。",
            "builtin": True,
            "shader_hint": "FishStandard.shader",
            "manual_laya_control_schema": _fish_standard_preset_manual_schema(),
        },
    ]


def _empty_manual_laya_control_schema(*, base_auto_hash: str = "") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base_auto_hash": base_auto_hash,
        "groups": {},
        "controls": {},
        "deleted_groups": [],
        "hidden_controls": [],
    }


def _normalize_laya_control_schema_preset(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    preset_id = str(value.get("id") or "").strip()
    name = str(value.get("name") or "").strip()
    if not preset_id or not name:
        return None
    return {
        "id": preset_id,
        "name": name,
        "description": str(value.get("description") or ""),
        "builtin": bool(value.get("builtin", False)),
        "shader_hint": str(value.get("shader_hint") or ""),
        "manual_laya_control_schema": _normalize_manual_laya_control_schema(value.get("manual_laya_control_schema")),
    }


def _find_laya_control_schema_preset(project: dict[str, Any], preset_id: str) -> dict[str, Any] | None:
    for preset in _builtin_laya_control_schema_presets():
        if preset["id"] == preset_id:
            return preset
    for raw in project.get("laya_control_schema_presets", []):
        preset = _normalize_laya_control_schema_preset(raw)
        if preset is not None and preset["id"] == preset_id:
            return preset
    return None


def _unique_preset_id(name: str, existing_presets: list[dict[str, Any]]) -> str:
    base = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    base = base or "preset"
    existing = {str(item.get("id")) for item in existing_presets}
    existing.update(item["id"] for item in _builtin_laya_control_schema_presets())
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _current_shader_hint(project: dict[str, Any]) -> str:
    inputs = project.get("inputs") if isinstance(project.get("inputs"), dict) else {}
    return str(inputs.get("laya_shader_path") or "")


def _refresh_laya_control_schema_payload(
    project_id: str,
    manual_schema: dict[str, Any],
    config: LoaderConfig,
) -> dict[str, Any]:
    payload = get_preanalysis(project_id, config)
    if payload is None:
        return {"manual_laya_control_schema": manual_schema}

    auto_schema = payload.get("auto_laya_control_schema")
    if not isinstance(auto_schema, dict):
        auto_schema = _control_groups_to_schema(
            payload.get("laya_control_groups", []),
            source={
                "kind": "auto",
                "generator": "legacy_laya_control_groups",
                "shader_name": (payload.get("laya_shader") or {}).get("name") if isinstance(payload.get("laya_shader"), dict) else None,
                "shader_path": (payload.get("laya_shader") or {}).get("path") if isinstance(payload.get("laya_shader"), dict) else None,
            },
        )
    project = get_project(project_id, config)
    effective = build_effective_laya_control_schema(
        auto_schema,
        manual_schema,
        run_overrides=(project.get("algorithm_config") or {}).get("laya_control_group_overrides"),
    )
    payload["auto_laya_control_schema"] = auto_schema
    payload["manual_laya_control_schema"] = manual_schema
    payload["effective_laya_control_schema"] = effective
    payload["laya_control_groups"] = _schema_to_control_groups(effective)

    paths = project_paths(project_id, config)
    paths.preanalysis_json.parent.mkdir(parents=True, exist_ok=True)
    paths.preanalysis_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _control_groups_to_schema(groups: Any, *, source: dict[str, Any]) -> dict[str, Any]:
    schema_groups: list[dict[str, Any]] = []
    if not isinstance(groups, list):
        groups = []
    for index, raw_group in enumerate(groups):
        if not isinstance(raw_group, dict):
            continue
        group_id = str(raw_group.get("group") or raw_group.get("id") or f"group_{index}")
        controls: list[dict[str, Any]] = []
        for raw_control in raw_group.get("controls", []):
            if not isinstance(raw_control, dict) or not raw_control.get("name"):
                continue
            control = dict(raw_control)
            control["group"] = group_id
            control.setdefault("source", "auto")
            control.setdefault("locked_fields", [])
            controls.append(control)
        schema_groups.append(
            {
                "id": group_id,
                "label": str(raw_group.get("label") or group_id),
                "description": str(raw_group.get("description") or ""),
                "enabled": bool(raw_group.get("enabled", True)),
                "locked": bool(raw_group.get("locked", False)),
                "order": int(raw_group.get("order", index * 10) or index * 10),
                "current_active": bool(raw_group.get("current_active", True)),
                "suggested_by_unity": bool(raw_group.get("suggested_by_unity", False)),
                "probe_required": bool(raw_group.get("probe_required", False)),
                "search_priority": float(raw_group.get("search_priority", 0.0) or 0.0),
                "reason": str(raw_group.get("reason") or ""),
                "channels": [str(item) for item in raw_group.get("channels", []) if isinstance(item, str)],
                "define_gates": [str(item) for item in raw_group.get("define_gates", []) if isinstance(item, str)],
                "gate_params": [str(item) for item in raw_group.get("gate_params", []) if isinstance(item, str)],
                "controls": controls,
                "source": str(raw_group.get("source") or "auto"),
            }
        )
    return {
        "schema_version": 1,
        "source": source,
        "groups": schema_groups,
    }


def _schema_to_control_groups(schema: dict[str, Any]) -> list[dict[str, Any]]:
    groups = schema.get("groups") if isinstance(schema, dict) else []
    out: list[dict[str, Any]] = []
    if not isinstance(groups, list):
        return out
    for raw_group in sorted(
        [group for group in groups if isinstance(group, dict)],
        key=lambda group: (int(group.get("order", 0) or 0), str(group.get("id", ""))),
    ):
        controls = [
            dict(control)
            for control in raw_group.get("controls", [])
            if isinstance(control, dict) and control.get("name") and not control.get("hidden_by_manual")
        ]
        if not controls and not bool(raw_group.get("created_by_user")):
            continue
        controls.sort(key=lambda item: (not bool(item.get("is_gate")), not bool(item.get("is_search_param")), str(item.get("name", ""))))
        out.append(
            {
                "group": str(raw_group.get("id") or ""),
                "label": str(raw_group.get("label") or raw_group.get("id") or ""),
                "description": str(raw_group.get("description") or ""),
                "enabled": bool(raw_group.get("enabled", True)),
                "locked": bool(raw_group.get("locked", False)),
                "order": int(raw_group.get("order", 0) or 0),
                "current_active": bool(raw_group.get("current_active", True)),
                "suggested_by_unity": bool(raw_group.get("suggested_by_unity", False)),
                "probe_required": bool(raw_group.get("probe_required", False)),
                "search_priority": float(raw_group.get("search_priority", 0.0) or 0.0),
                "reason": str(raw_group.get("reason") or ""),
                "channels": [str(item) for item in raw_group.get("channels", []) if isinstance(item, str)],
                "define_gates": [str(item) for item in raw_group.get("define_gates", []) if isinstance(item, str)],
                "gate_params": [str(item) for item in raw_group.get("gate_params", []) if isinstance(item, str)],
                "controls": controls,
                "searchable_count": sum(1 for control in controls if control.get("searchable") is True),
                "gate_count": sum(1 for control in controls if control.get("is_gate") is True),
                "source": str(raw_group.get("source") or "auto"),
                "created_by_user": bool(raw_group.get("created_by_user", False)),
            }
        )
    return out


def _normalize_manual_laya_control_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    groups = value.get("groups") if isinstance(value.get("groups"), dict) else {}
    controls = value.get("controls") if isinstance(value.get("controls"), dict) else {}
    return {
        "schema_version": int(value.get("schema_version", 1) or 1),
        "base_auto_hash": str(value.get("base_auto_hash") or ""),
        "groups": {
            str(group_id): patch
            for group_id, patch in groups.items()
            if isinstance(patch, dict)
        },
        "controls": {
            str(param_name): patch
            for param_name, patch in controls.items()
            if isinstance(patch, dict)
        },
        "deleted_groups": [str(item) for item in value.get("deleted_groups", []) if isinstance(item, str)],
        "hidden_controls": [str(item) for item in value.get("hidden_controls", []) if isinstance(item, str)],
    }


def build_effective_laya_control_schema(
    auto_schema: dict[str, Any],
    manual_schema: dict[str, Any] | None,
    *,
    run_overrides: Any = None,
) -> dict[str, Any]:
    manual_schema = _normalize_manual_laya_control_schema(manual_schema)
    effective = json.loads(json.dumps(auto_schema if isinstance(auto_schema, dict) else {}))
    effective["schema_version"] = int(effective.get("schema_version", 1) or 1)
    effective.setdefault("source", {})
    groups = effective.get("groups") if isinstance(effective.get("groups"), list) else []
    effective["groups"] = groups
    group_by_id = {
        str(group.get("id")): group
        for group in groups
        if isinstance(group, dict) and group.get("id")
    }

    def ensure_group(group_id: str) -> dict[str, Any]:
        group = group_by_id.get(group_id)
        if group is not None:
            return group
        order = (max([int(g.get("order", 0) or 0) for g in groups if isinstance(g, dict)] + [0]) + 10)
        group = {
            "id": group_id,
            "label": group_id,
            "description": "",
            "enabled": True,
            "locked": False,
            "order": order,
            "current_active": True,
            "suggested_by_unity": False,
            "probe_required": False,
            "search_priority": 0.0,
            "reason": "manual group",
            "channels": [],
            "define_gates": [],
            "gate_params": [],
            "controls": [],
            "source": "manual",
        }
        groups.append(group)
        group_by_id[group_id] = group
        return group

    for group_id, patch in manual_schema["groups"].items():
        group = ensure_group(group_id)
        for key in (
            "label",
            "description",
            "enabled",
            "locked",
            "order",
            "created_by_user",
            "current_active",
            "suggested_by_unity",
            "probe_required",
            "search_priority",
            "reason",
            "channels",
            "define_gates",
            "gate_params",
        ):
            if key in patch:
                group[key] = patch[key]
        for key in ("channels", "define_gates", "gate_params"):
            if isinstance(group.get(key), list):
                group[key] = [str(item) for item in group[key] if isinstance(item, str)]
        group["source"] = "manual" if patch else group.get("source", "auto")

    control_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        controls = group.get("controls") if isinstance(group.get("controls"), list) else []
        group["controls"] = controls
        for control in controls:
            if isinstance(control, dict) and control.get("name"):
                control_index[str(control["name"])] = (group, control)

    for param_name, patch in manual_schema["controls"].items():
        target_group_id = str(patch.get("group") or "")
        old = control_index.get(param_name)
        if old is None:
            group = ensure_group(target_group_id or "unassigned")
            control = {
                "name": param_name,
                "display_name": param_name,
                "param_type": "",
                "current_value": None,
                "default": None,
                "range": [None, None],
                "hidden": None,
                "group": group["id"],
                "role": "value",
                "transform": "linear",
                "searchable": False,
                "is_gate": False,
                "is_search_param": False,
                "gates": [],
                "gate_status": {"state": "open", "closed": [], "open": []},
                "dependencies": [],
                "reason": "manual orphan override",
                "source": "manual",
            }
            group["controls"].append(control)
        else:
            old_group, control = old
            if target_group_id and target_group_id != old_group.get("id"):
                old_group["controls"] = [
                    item for item in old_group.get("controls", [])
                    if not (isinstance(item, dict) and item.get("name") == param_name)
                ]
                group = ensure_group(target_group_id)
                group["controls"].append(control)
                control["group"] = target_group_id
        for key in (
            "role",
            "transform",
            "searchable",
            "is_gate",
            "is_search_param",
            "note",
            "reason",
            "range_min",
            "range_max",
        ):
            if key in patch:
                control[key] = patch[key]
        control["source"] = "manual"
        control["locked_fields"] = [str(item) for item in patch.get("locked_fields", []) if isinstance(item, str)]

    deleted_groups = set(manual_schema.get("deleted_groups", []))
    hidden_controls = set(manual_schema.get("hidden_controls", []))
    unassigned = ensure_group("unassigned") if deleted_groups else None
    for group in list(groups):
        group_id = str(group.get("id") or "")
        if group_id in deleted_groups:
            if unassigned is not None and group is not unassigned:
                for control in group.get("controls", []):
                    if isinstance(control, dict):
                        control["group"] = "unassigned"
                        unassigned["controls"].append(control)
                group["controls"] = []
            group["enabled"] = False
            group["hidden"] = True
    for group in groups:
        kept = []
        for control in group.get("controls", []):
            if isinstance(control, dict) and control.get("name") in hidden_controls:
                control["hidden_by_manual"] = True
                control["searchable"] = False
            kept.append(control)
        group["controls"] = kept

    overrides = run_overrides if isinstance(run_overrides, dict) else {}
    for group_id, raw in overrides.items():
        group = ensure_group(str(group_id))
        enabled = raw.get("enabled", True) if isinstance(raw, dict) else raw
        if enabled is False:
            group["enabled"] = False
        elif enabled is True and "enabled" not in manual_schema["groups"].get(str(group_id), {}):
            group["enabled"] = True

    effective["manual_laya_control_schema"] = manual_schema
    effective["groups"] = sorted(groups, key=lambda group: (int(group.get("order", 0) or 0), str(group.get("id", ""))))
    return effective


def _control_gate_status(
    gates: list[dict[str, Any]],
    material_params: dict[str, Any],
    material_defines: set[str],
) -> dict[str, Any]:
    if not gates:
        return {"state": "open", "closed": [], "open": []}
    closed: list[str] = []
    opened: list[str] = []
    for gate in gates:
        kind = str(gate.get("kind") or "")
        name = str(gate.get("name") or "")
        if not name:
            continue
        if kind == "define":
            if name in material_defines:
                opened.append(name)
            else:
                closed.append(name)
        elif kind == "param_nonzero":
            if _as_float(material_params.get(name), 0.0) > 0.0:
                opened.append(name)
            else:
                closed.append(name)
    return {
        "state": "blocked" if closed else "open",
        "closed": closed,
        "open": opened,
    }


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _laya_control_group_label(group: str) -> str:
    return {
        "base_color": "基础色 / 主体亮度",
        "shadow_diffuse": "阴影 / 漫反射层次",
        "specular_smoothness": "高光 / 金属 / 光滑度",
        "reflection_matcap": "环境反射 / Matcap",
        "fresnel": "Fresnel / 边缘光",
        "emission": "自发光",
        "color_grade": "HSV / 对比度调色",
        "misc": "其他控件",
    }.get(group, group)


def _laya_control_group_description(group: str) -> str:
    return {
        "base_color": "影响主体底色、整体明暗和主纹理基调，是大多数材质拟合的第一优先级。",
        "shadow_diffuse": "控制暗部、遮蔽、GI 与 diffuse ramp，适合修正明暗层次。",
        "specular_smoothness": "控制主高光、金属感、粗糙/光滑过渡等材质反射特征。",
        "reflection_matcap": "控制 IBL、环境反射和 Matcap 叠加，常与高光观感耦合。",
        "fresnel": "控制轮廓边缘光和视角相关亮边，通常受强度 gate 控制。",
        "emission": "控制自发光颜色和强度，通常需要 define 或 scale/gate 生效。",
        "color_grade": "控制 Hue、Saturation、Lightness、Contrast 等全局后处理式修正。",
        "misc": "暂未归入明确语义组的参数，后续可由人工或 LLM 进一步整理。",
    }.get(group, "")


def _build_unity_feature_llm_context(context: dict[str, Any]) -> dict[str, Any]:
    unity_shader = context.get("unity_shader") if isinstance(context.get("unity_shader"), dict) else {}
    laya_shader = context.get("laya_shader") if isinstance(context.get("laya_shader"), dict) else {}
    return {
        "task": {
            "goal": "infer_enabled_unity_material_feature_modules",
            "allowed_output": "strict_json_unity_feature_summary",
            "forbidden_actions": ["write_files", "modify_lmat", "run_optimizer"],
            "instructions": [
                "Only analyze what Unity material/shader appears to enable.",
                "Do not output per-Laya-parameter semantics.",
                "Do not copy Unity values as final Laya values.",
                "Return concise JSON with unity_feature_summary only.",
            ],
        },
        "unity_shader": {
            "path": unity_shader.get("path"),
            "name": unity_shader.get("name"),
            "params": unity_shader.get("params", []),
            "defines": unity_shader.get("defines", []),
            "source_excerpt": _trim_middle(str(unity_shader.get("source_excerpt", "")), _env_int("LLM_UNITY_SOURCE_CHARS", 12000)),
        },
        "unity_material": context.get("unity_material") if isinstance(context.get("unity_material"), dict) else {},
        "available_laya_groups": [
            "base_color",
            "shadow_diffuse",
            "specular_smoothness",
            "reflection_matcap",
            "fresnel",
            "emission",
            "color_grade",
            "misc",
        ],
        "laya_shader_summary": {
            "name": laya_shader.get("name"),
            "param_names": [
                str(param.get("name"))
                for param in laya_shader.get("params", [])
                if isinstance(param, dict) and param.get("name")
            ],
            "defines": [
                str(define.get("name"))
                for define in laya_shader.get("defines", [])
                if isinstance(define, dict) and define.get("name")
            ],
        },
        "output_schema": {
            "unity_feature_summary": [
                {
                    "feature": "base_color|normal|occlusion|metallic_smoothness|specular|secondary_specular|matcap_reflection|rim_or_fresnel|emission|color_grade|alpha|other",
                    "enabled": True,
                    "confidence": 0.0,
                    "evidence": ["keyword/texture/value/formula evidence"],
                    "unity_params": ["Unity property names"],
                    "textures": ["texture slots or texture names"],
                    "controls": ["color|intensity|shape|texture|mask|remap|gate"],
                    "laya_candidate_groups": ["one or more available_laya_groups"],
                    "risk": "brief uncertainty or cross-engine mismatch risk",
                }
            ]
        },
    }


def _normalized_unity_features(llm_semantics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(llm_semantics, dict):
        return []
    raw = llm_semantics.get("unity_feature_summary")
    if isinstance(raw, list) and raw:
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            feature = str(item.get("feature") or "other")
            normalized = dict(item)
            groups = [
                str(group)
                for group in normalized.get("laya_candidate_groups", [])
                if isinstance(group, str)
            ]
            normalized["laya_candidate_groups"] = _unique_strings(groups or _candidate_groups_for_feature(feature))
            out.append(normalized)
        return out
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in llm_semantics.get("unity_phenomena", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "other")
        groups = [str(group) for group in item.get("laya_candidate_groups", []) if isinstance(group, str)]
        key = name + "|" + ",".join(groups)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "feature": name,
                "enabled": True,
                "confidence": item.get("confidence", 0.0),
                "evidence": [str(ev) for ev in item.get("unity_evidence", []) if isinstance(ev, str)],
                "unity_params": [],
                "textures": [],
                "controls": [],
                "laya_candidate_groups": groups or _candidate_groups_for_feature(name),
                "risk": item.get("note", ""),
            }
        )
    return out


def _normalized_laya_candidates(llm_semantics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(llm_semantics, dict):
        return []
    raw = llm_semantics.get("laya_module_candidates")
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    return []


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _candidate_groups_for_feature(feature: str) -> list[str]:
    text = feature.lower()
    mapping = [
        (("base", "albedo", "color", "alpha"), ["base_color"]),
        (("normal", "bump"), ["misc"]),
        (("occlusion", "shadow", "diffuse"), ["shadow_diffuse"]),
        (("metallic", "smooth", "rough", "specular", "gloss"), ["specular_smoothness"]),
        (("secondary_specular", "second_specular"), ["specular_smoothness"]),
        (("matcap", "reflection", "reflect", "environment", "ibl"), ["reflection_matcap"]),
        (("rim", "fresnel"), ["fresnel"]),
        (("emission", "emissive"), ["emission"]),
        (("hsv", "color_grade", "contrast", "saturation", "hue"), ["color_grade"]),
    ]
    out: list[str] = []
    for tokens, groups in mapping:
        if any(token in text for token in tokens):
            out.extend(groups)
    return _unique_strings(out or ["misc"])


def _run_llm_semantics_batches(
    context: dict[str, Any],
    *,
    runtime: Any,
    batch_size: int,
    concurrency: int,
) -> dict[str, Any]:
    from .llm_client import run_shader_semantics_llm

    laya_shader = context.get("laya_shader") if isinstance(context.get("laya_shader"), dict) else {}
    params = laya_shader.get("params") if isinstance(laya_shader.get("params"), list) else []
    if not params:
        return run_shader_semantics_llm(context, runtime=runtime, max_tokens=2500)

    merged: dict[str, Any] = {
        "unity_feature_summary": [],
        "laya_module_candidates": [],
        "unity_phenomena": [],
        "param_semantics": [],
        "initial_laya_param_suggestions": [],
        "_batch_warnings": [],
    }
    seen_phenomena: set[str] = set()
    seen_suggestions: set[str] = set()
    chunks = [params[i : i + batch_size] for i in range(0, len(params), batch_size)]
    max_workers = max(1, min(concurrency, len(chunks)))

    def call_batch(index_and_chunk: tuple[int, list[dict[str, Any]]]) -> tuple[int, dict[str, Any] | None, str | None]:
        index, chunk = index_and_chunk
        batch_context = _build_llm_batch_context(context, laya_shader, chunk, index, len(chunks))
        try:
            return index, run_shader_semantics_llm(batch_context, runtime=runtime, max_tokens=3500), None
        except Exception as exc:  # noqa: BLE001
            return index, None, f"LLM batch {index + 1}/{len(chunks)} failed: {exc}"

    results: list[tuple[int, dict[str, Any] | None, str | None]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(call_batch, enumerate(chunks)))

    for index, raw, warning in sorted(results, key=lambda item: item[0]):
        if warning:
            merged["_batch_warnings"].append(warning)
            continue
        if not isinstance(raw, dict):
            merged["_batch_warnings"].append(f"LLM batch {index + 1}/{len(chunks)} returned non-object")
            continue
        for item in raw.get("unity_feature_summary", []):
            if isinstance(item, dict):
                merged["unity_feature_summary"].append(item)
        for item in raw.get("laya_module_candidates", []):
            if isinstance(item, dict):
                merged["laya_module_candidates"].append(item)
        for item in raw.get("param_semantics", []):
            if isinstance(item, dict):
                merged["param_semantics"].append(item)
        for item in raw.get("unity_phenomena", []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("name", "")) + "|" + ",".join(
                str(group) for group in item.get("laya_candidate_groups", []) if isinstance(group, str)
            )
            if key in seen_phenomena:
                continue
            seen_phenomena.add(key)
            merged["unity_phenomena"].append(item)
        for item in raw.get("initial_laya_param_suggestions", []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("laya_param", ""))
            if not key or key in seen_suggestions:
                continue
            seen_suggestions.add(key)
            merged["initial_laya_param_suggestions"].append(item)
    return merged


def _build_llm_batch_context(
    context: dict[str, Any],
    laya_shader: dict[str, Any],
    chunk: list[dict[str, Any]],
    index: int,
    count: int,
) -> dict[str, Any]:
    batch_context = dict(context)
    batch_laya = dict(laya_shader)
    batch_laya["params"] = chunk
    batch_laya["source_excerpt"] = ""
    batch_context["laya_shader"] = batch_laya
    unity_shader = batch_context.get("unity_shader")
    if isinstance(unity_shader, dict):
        batch_context["unity_shader"] = {
            "path": unity_shader.get("path"),
            "name": unity_shader.get("name"),
            "params": unity_shader.get("params", []),
            "defines": unity_shader.get("defines", []),
            "source_excerpt": "",
        }
    batch_names = {str(param.get("name")) for param in chunk if isinstance(param, dict) and param.get("name")}
    laya_material = batch_context.get("laya_material")
    if isinstance(laya_material, dict) and isinstance(laya_material.get("params"), dict):
        laya_material = dict(laya_material)
        laya_material["params"] = {
            name: value
            for name, value in laya_material["params"].items()
            if name in batch_names
        }
        batch_context["laya_material"] = laya_material
    batch_context["task"] = {
        **(context.get("task") if isinstance(context.get("task"), dict) else {}),
        "batch_index": index,
        "batch_count": count,
        "batch_instruction": (
            "Return param_semantics only for laya_shader.params in this batch. "
            "Keep unity_phenomena and initial_laya_param_suggestions concise. "
            "Do not include parameters outside this batch in param_semantics."
        ),
    }
    return batch_context


def _compact_llm_context(context: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(context, ensure_ascii=False))
    for shader_key in ("laya_shader", "unity_shader"):
        shader = out.get(shader_key)
        if isinstance(shader, dict) and isinstance(shader.get("source_excerpt"), str):
            shader["source_excerpt"] = _trim_middle(shader["source_excerpt"], _env_int("LLM_SHADER_SOURCE_CHARS", 6000))
    return out


def _trim_middle(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n\n/* ... source trimmed for LLM request ... */\n\n" + text[-tail:]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        raw = _read_env_value(PROJECT_ROOT / "tools" / ".env", name)
    try:
        value = int(raw or "")
    except ValueError:
        value = default
    return value if value > 0 else default


def _read_env_value(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    prefix = f"{name}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.replace(" ", "").startswith(prefix):
            _, value = stripped.split("=", 1)
            return value.strip().strip('"').strip("'")
    return None


def _build_param_mapping(
    unity_info: dict[str, Any] | None,
    laya_info: dict[str, Any],
    *,
    manual_mapping: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    laya_params = laya_info.get("params", [])
    laya_by_name = {p["name"]: p for p in laya_params}
    if not unity_info:
        return [
            {
                "unity_name": None,
                "unity_type": None,
                "laya_name": p["name"],
                "laya_type": p.get("param_type"),
                "status": "laya_only",
                "score": 0.0,
                "reason": "no Unity shader provided",
            }
            for p in laya_params
        ]

    manual_mapping = manual_mapping or {}
    rows: list[dict[str, Any]] = []
    seen_laya: set[str] = set()
    laya_norm_index = {_normalize(p["name"]): p for p in laya_params}

    for u in unity_info.get("params", []):
        u_name = u["name"]
        u_norm = _normalize(u_name)

        # 1. manual override (user-curated, highest priority)
        manual_target = manual_mapping.get(u_name)
        if manual_target == "":
            seen_laya.add("__skipped__")
            rows.append(_pair(u, None, status="manual_skip", score=1.0, reason="user marked as no mapping"))
            continue
        if manual_target and manual_target in laya_by_name:
            laya = laya_by_name[manual_target]
            seen_laya.add(laya["name"])
            rows.append(_pair(u, laya, status="manual", score=1.0, reason="user-defined mapping"))
            continue

        # 2. curated dictionary
        curated = _curated_pair(u_norm, laya_norm_index)
        if curated is not None and curated["name"] not in seen_laya:
            laya = curated
            seen_laya.add(laya["name"])
            rows.append(_pair(u, laya, status="curated", score=0.95, reason="curated cross-engine dictionary"))
            continue

        # 3. exact normalized
        if u_norm in laya_norm_index and laya_norm_index[u_norm]["name"] not in seen_laya:
            laya = laya_norm_index[u_norm]
            if _types_compatible(u.get("param_type"), laya.get("param_type")):
                seen_laya.add(laya["name"])
                rows.append(_pair(u, laya, status="exact", score=1.0, reason="normalized name match (type compatible)"))
                continue
            # name matches but types disagree — surface clearly, don't auto-pair
            rows.append(_pair(
                u, None, status="unity_only", score=0.0,
                reason=f"name matches Laya `{laya['name']}` but types incompatible ({u.get('param_type')} vs {laya.get('param_type')})",
            ))
            continue

        # 4. type-aware fuzzy
        candidates: list[tuple[dict[str, Any], float]] = []
        for laya in laya_params:
            if laya["name"] in seen_laya:
                continue
            if not _types_compatible(u.get("param_type"), laya.get("param_type")):
                continue
            score = _name_score(u_norm, _normalize(laya["name"]))
            if score >= 0.85:
                candidates.append((laya, score))
        candidates.sort(key=lambda item: item[1], reverse=True)
        if candidates:
            laya, score = candidates[0]
            seen_laya.add(laya["name"])
            rows.append(_pair(u, laya, status="fuzzy", score=score, reason="type-compatible name similarity ≥0.85"))
            continue

        rows.append(_pair(u, None, status="unity_only", score=0.0, reason="no type-compatible Laya counterpart"))

    for laya in laya_params:
        if laya["name"] in seen_laya:
            continue
        rows.append(_pair(None, laya, status="laya_only", score=0.0, reason="not paired with any Unity property"))
    return rows


def _pair(
    unity: dict[str, Any] | None,
    laya: dict[str, Any] | None,
    *,
    status: str,
    score: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "unity_name": unity["name"] if unity else None,
        "unity_type": unity.get("param_type") if unity else None,
        "laya_name": laya["name"] if laya else None,
        "laya_type": laya.get("param_type") if laya else None,
        "status": status,
        "score": round(float(score), 3),
        "reason": reason,
    }


def _types_compatible(unity_type: str | None, laya_type: str | None) -> bool:
    """Return True if a Unity ShaderLab type and a Laya uniformMap type can
    sensibly hold each other's values."""

    if not unity_type or not laya_type:
        return True  # missing info — be permissive, surface to user later
    u = unity_type.lower()
    l = laya_type.lower()

    scalar = {"float", "range", "int"}
    color = {"color"}
    vector = {"vector"}
    tex2d = {"2d"}
    cube = {"cube"}

    def family(t: str) -> str:
        if t in scalar:
            return "scalar"
        if t in color:
            return "color"
        if t in vector:
            return "vector"
        if t in tex2d:
            return "tex2d"
        if t in cube:
            return "cube"
        return t

    fu, fl = family(u), family(l)
    if fu == fl:
        return True
    # Color↔Vector is genuinely interchangeable (vec4 = RGBA = color).
    if {fu, fl} == {"color", "vector"}:
        return True
    return False


def _name_score(a: str, b: str) -> float:
    """Token-aware similarity in [0, 1]. Higher means more similar."""

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        return shorter / longer
    # character-set Jaccard (very lightweight)
    set_a, set_b = set(a), set(b)
    inter = set_a & set_b
    union = set_a | set_b
    return len(inter) / max(len(union), 1)


def _normalize(name: str) -> str:
    name = (name or "").lower()
    if name.startswith("u_"):
        name = name[2:]
    if name.startswith("_"):
        name = name[1:]
    return "".join(ch for ch in name if ch.isalnum())


# ---------------------------------------------------------------------------
# Curated cross-engine dictionary.
#
# Keys are the *normalized* (lowercase, alphanumeric-only, prefix-stripped)
# Unity property names. Values are tuples of acceptable normalized Laya
# uniformMap names, in preference order. The first one that actually exists in
# the parsed Laya shader wins.
#
# Sources: Unity Standard, URP Lit / SimpleLit, common Toon shaders, observed
# Laya Engine FishStandard / Effect / Stylized shaders. Keep entries here
# **only if the semantic meaning is unambiguous** — anything fuzzy should fall
# through to type-aware name match so the user can audit it in the UI.
# ---------------------------------------------------------------------------
_CURATED_DICT: dict[str, tuple[str, ...]] = {
    "color":            ("basecolor", "albedocolor", "maincolor", "tintcolor", "color"),
    "basecolor":        ("basecolor", "albedocolor", "color"),
    "tintcolor":        ("basecolor", "tintcolor", "color"),
    "maintex":          ("albedotexture", "maintexture", "diffusetexture", "basecolortexture"),
    "albedo":           ("albedotexture", "maintexture", "basecolortexture"),
    "albedotex":        ("albedotexture", "basecolortexture"),
    "metallic":         ("metallic",),
    "smoothness":       ("smoothness",),
    "glossiness":       ("smoothness",),
    "roughness":        ("smoothness",),  # smoothness = 1 - roughness, but it's the same channel
    "metallicgloss":    ("metallicglosstexture", "metallictexture"),
    "metallictex":      ("metallictexture",),
    "metallicremapmin": ("metallicremapmin",),
    "metallicremapmax": ("metallicremapmax",),
    "smoothnessremapmin": ("smoothnessremapmin",),
    "smoothnessremapmax": ("smoothnessremapmax",),
    "bumpmap":          ("normaltexture", "bumptexture"),
    "normalmap":        ("normaltexture", "bumptexture"),
    "bumpscale":        ("bumpscale", "normalstrength"),
    "normalscale":      ("bumpscale", "normalstrength"),
    "occlusionmap":     ("occlusiontexture",),
    "occlusionstrength": ("occlusionstrength",),
    "emissioncolor":    ("emissioncolor",),
    "emissionmap":      ("emissiontexture", "emissionmap"),
    "emissionintensity": ("emissionintensity", "emissionpower"),
    "emissionpower":    ("emissionpower", "emissionintensity"),
    "cutoff":           ("cutoff", "alphacutoff"),
    "alphacutoff":      ("cutoff", "alphacutoff"),
    "alpha":            ("alpha",),
    "specularhighlights": ("specularhighlights",),
    "specularcolor":    ("specularcolor",),
    "rimcolor":         ("rimcolor",),  # only paired if such a name exists
    "rimpower":         ("rimpower",),
    "rimintensity":     ("rimintensity",),
    "fresnel":          ("fresnelintensity", "fresnelpower"),
    "fresnelpower":     ("fresnelpower",),
    "fresnelintensity": ("fresnelintensity",),
    "matcap":           ("matcaptexture", "matcap"),
    "matcaptex":        ("matcaptexture",),
    "matcapintensity":  ("matcapintensity",),
}


def _curated_pair(
    u_norm: str,
    laya_norm_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Look up a normalized Unity name in the curated dict and return the
    first Laya param whose normalized name appears in the candidate tuple.
    Returns ``None`` if no curated mapping or the candidates aren't present."""

    candidates = _CURATED_DICT.get(u_norm)
    if not candidates:
        return None
    for cand in candidates:
        laya = laya_norm_index.get(cand)
        if laya:
            return laya
    return None


def _predict_stage_plan(laya_info: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from tools.material_fit.optimizer.adjustment_algorithm import build_adjustment_policies
        from tools.material_fit.shared.models import ShaderParam

        params = [
            ShaderParam(
                name=p["name"],
                display_name=p.get("display_name", p["name"]),
                param_type=p.get("param_type", ""),
                default=p.get("default"),
                range_min=p.get("range_min"),
                range_max=p.get("range_max"),
                hidden=p.get("hidden"),
            )
            for p in laya_info.get("params", [])
        ]
        policies = build_adjustment_policies(params)
        return [
            {
                "name": pol.name,
                "description": pol.description,
                "channels": pol.channels,
                "params": pol.params,
                "max_iterations": pol.max_iterations,
                "target_score": pol.target_score,
            }
            for pol in policies
        ]
    except Exception as exc:  # noqa: BLE001
        return [{"name": "_error", "description": f"failed to build policies: {exc}", "channels": [], "params": [], "max_iterations": 0, "target_score": 0.0}]


def _compute_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    matched_kinds = {"manual", "curated", "exact", "fuzzy"}
    total_unity = sum(1 for r in rows if r["unity_name"])
    matched = sum(1 for r in rows if r["status"] in matched_kinds and r["unity_name"])
    return {
        "unity_total": total_unity,
        "unity_mapped": matched,
        "unity_unmapped": total_unity - matched,
        "laya_total": sum(1 for r in rows if r["laya_name"]),
        "laya_only": sum(1 for r in rows if r["status"] == "laya_only"),
        "ratio": (matched / total_unity) if total_unity else 0.0,
        "by_status": {
            "manual": sum(1 for r in rows if r["status"] == "manual"),
            "curated": sum(1 for r in rows if r["status"] == "curated"),
            "exact": sum(1 for r in rows if r["status"] == "exact"),
            "fuzzy": sum(1 for r in rows if r["status"] == "fuzzy"),
            "manual_skip": sum(1 for r in rows if r["status"] == "manual_skip"),
            "unity_only": sum(1 for r in rows if r["status"] == "unity_only"),
            "laya_only": sum(1 for r in rows if r["status"] == "laya_only"),
        },
    }


def _initial_recommendations(
    *,
    unity_material_params: dict[str, Any],
    laya_material_params: dict[str, Any],
    mapping: list[dict[str, Any]],
    laya_param_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Suggested initial values: only paired rows that we trust enough to apply."""

    trusted = {"manual", "curated", "exact"}
    out: list[dict[str, Any]] = []
    for row in mapping:
        if row["status"] not in trusted:
            continue
        u_name = row["unity_name"]
        l_name = row["laya_name"]
        if not u_name or not l_name:
            continue
        if u_name not in unity_material_params:
            continue
        unity_value = unity_material_params[u_name]
        laya_value = laya_material_params.get(l_name)
        meta = laya_param_meta.get(l_name, {})
        out.append(
            {
                "laya_param": l_name,
                "unity_param": u_name,
                "current_laya_value": laya_value,
                "suggested_value": unity_value,
                "status": row["status"],
                "type": meta.get("param_type"),
                "range": [meta.get("range_min"), meta.get("range_max")],
            }
        )
    return out


def _collect_warnings(
    unity_info: dict[str, Any] | None,
    laya_info: dict[str, Any],
    mapping: list[dict[str, Any]],
    inputs: dict[str, Any],
    *,
    suppress_mapping_warnings: bool = False,
) -> list[str]:
    warnings: list[str] = []
    if not unity_info:
        warnings.append("Unity 着色器未提供，将无法做参数对照映射，仅按 Laya 侧默认 stage plan 调参。")
    if not laya_info.get("params"):
        warnings.append("Laya shader 解析未得到任何参数，请检查 uniformMap 块是否存在。")
    if not suppress_mapping_warnings:
        fuzzy_count = sum(1 for r in mapping if r["status"] == "fuzzy")
        if fuzzy_count:
            warnings.append(
                f"{fuzzy_count} 个参数仅靠名称模糊匹配（已要求类型兼容且相似度≥0.85），仍建议人工复核或在表格里改成 manual。"
            )
        unity_only = sum(1 for r in mapping if r["status"] == "unity_only")
        if unity_only:
            warnings.append(
                f"{unity_only} 个 Unity 属性没有自动找到 Laya 对应——可在表格里手动配对，或添加到 curated 字典。"
            )
    if not (inputs.get("unity_reference_dir_path") or inputs.get("unity_reference_image_path")):
        warnings.append("没有 Unity 参考图或多视角参考目录，自动调参无法进行图像差异分析。")
    if not (inputs.get("laya_project_path") or inputs.get("laya_capture_command_path")):
        warnings.append("没有 Laya 项目目录或 command JSON，后台脚本截图无法触发。")
    if not inputs.get("laya_material_lmat_path"):
        warnings.append("没有 Laya .lmat 写入目标，自动调参无法应用材质修改。")
    return warnings


def _collect_mapping_notes(mapping: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    fuzzy_count = sum(1 for r in mapping if r["status"] == "fuzzy")
    unity_only = sum(1 for r in mapping if r["status"] == "unity_only")
    if fuzzy_count:
        notes.append(f"{fuzzy_count} 个参数仅靠名称模糊匹配，旧参数表仅作为人工参考。")
    if unity_only:
        notes.append(f"{unity_only} 个 Unity 属性未逐项映射；这不影响基于功能模块的搜索空间规划。")
    return notes


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now().isoformat(timespec="seconds")


def _read_source_excerpt(path: str, max_chars: int = 30000) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n\n/* ... source excerpt truncated ... */\n\n" + text[-tail:]

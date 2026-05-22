"""Project model & on-disk store.

A *project* wraps everything needed to drive `tools.material_fit.fit_material`
end-to-end from the UI:

- ``inputs`` — absolute paths to user-provided shader/.lmat/reference files
  (these can live anywhere on the user's machine, not just inside our repo).
- ``algorithm_config`` — ``max_iterations``, ``target_score``, ``apply_lmat``,
  screen-capture region, etc. We map this 1:1 to the existing CLI flags.
- ``preanalysis`` — cached output of the shader parsers + Unity↔Laya param
  mapping; populated by ``preanalysis.run_preanalysis``.
- ``jobs`` — pointer/log of every fit run kicked off from the UI.

Persistence:

```
tools/material_fit/output/<project_id>/
├── project.json          # this module owns it
├── inputs/               # optional copies of small reference assets
├── jobs/<job_id>.json    # job_manager owns these
├── preanalysis.json      # preanalysis module owns
└── runs/<date-settings>/ # one immutable-ish auto-adjust run artifact folder
```

We deliberately *do not* delete any of the existing ``case_loader`` paths;
projects coexist with legacy cases (e.g., ``fish_1580_smoke``) which simply
do not have a ``project.json``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .case_loader import LoaderConfig, _to_rel_posix


PROJECT_FILE = "project.json"
FIT_CONFIG_FILE = "fit_config.json"
PREANALYSIS_FILE = "preanalysis.json"
INPUTS_DIR = "inputs"
CONFIGS_DIR = "configs"
JOBS_DIR = "jobs"
RUNS_DIR = "runs"

_PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
_IMPORT_FILE_SLOTS: dict[str, str] = {
    "laya_shader_path": "laya_shader",
    "unity_shader_path": "unity_shader",
    "unity_material_params_path": "unity_material_params",
}
_UNITY_REFERENCE_DIR = "unity_references"


@dataclass(frozen=True)
class ProjectPaths:
    project_dir: Path
    project_json: Path
    configs_dir: Path
    fit_config_json: Path
    preanalysis_json: Path
    inputs_dir: Path
    jobs_dir: Path
    runs_dir: Path


def project_paths(project_id: str, config: LoaderConfig) -> ProjectPaths:
    project_dir = (config.output_dir / project_id).resolve()
    return ProjectPaths(
        project_dir=project_dir,
        project_json=project_dir / PROJECT_FILE,
        configs_dir=project_dir / CONFIGS_DIR,
        fit_config_json=project_dir / CONFIGS_DIR / FIT_CONFIG_FILE,
        preanalysis_json=project_dir / CONFIGS_DIR / PREANALYSIS_FILE,
        inputs_dir=project_dir / INPUTS_DIR,
        jobs_dir=project_dir / JOBS_DIR,
        runs_dir=project_dir / RUNS_DIR,
    )


def list_projects(config: LoaderConfig | None = None) -> list[dict[str, Any]]:
    config = config or LoaderConfig()
    if not config.output_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(config.output_dir.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_dir():
            continue
        project_file = entry / PROJECT_FILE
        if not project_file.exists():
            continue
        data = _read_json(project_file)
        if not isinstance(data, dict):
            continue
        out.append(_summary(data, entry, config))
    return out


def get_project(project_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    paths = project_paths(project_id, config)
    _ensure_within(paths.project_dir, config.output_dir.resolve())
    if not paths.project_json.exists():
        raise FileNotFoundError(f"project not found: {project_id}")
    data = _read_json(paths.project_json)
    if not isinstance(data, dict):
        raise FileNotFoundError(f"project.json malformed: {project_id}")
    data["_summary"] = _summary(data, paths.project_dir, config)
    return data


def create_project(
    *,
    project_id: str,
    name: str,
    description: str = "",
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    if not _PROJECT_ID_RE.match(project_id or ""):
        raise ValueError("project id must match [a-zA-Z0-9_-]{1,64}")
    config = config or LoaderConfig()
    paths = project_paths(project_id, config)
    _ensure_within(paths.project_dir, config.output_dir.resolve())
    if paths.project_dir.exists():
        raise FileExistsError(f"project directory already exists: {project_id}")
    paths.project_dir.mkdir(parents=True, exist_ok=False)
    paths.inputs_dir.mkdir(parents=True, exist_ok=True)
    paths.configs_dir.mkdir(parents=True, exist_ok=True)
    paths.jobs_dir.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    data: dict[str, Any] = {
        "schema_version": 1,
        "id": project_id,
        "name": name or project_id,
        "description": description or "",
        "created_at": now,
        "updated_at": now,
        "inputs": {
            "unity_shader_path": None,
            "unity_material_params_path": None,
            "unity_reference_image_path": None,
            "unity_reference_dir_path": None,
            "unity_reference_glob": "unity_ref_v*_yaw*_pitch*.png",
            "laya_shader_path": None,
            "laya_material_lmat_path": None,
            "laya_project_path": None,
            "laya_capture_command_path": None,
            "laya_capture_camera_name": "Capture Camera",
            "laya_capture_target_name": "model",
            "laya_capture_region": None,
            "laya_capture_dir": None,
            "laya_capture_state_file": None,
            "laya_capture_prefix": "laya_candidate",
            # E-007 (ExperimentLog.md): Laya editor pauses rendering
            # when its window loses focus. Before each .lmat write
            # and each capture, the pipeline brings this window to
            # the foreground. Set process_pattern='' to disable.
            "laya_window": {
                "process_pattern": "LayaAirIDE",
                "title_pattern": "",
                "settle_ms": 100,
            },
            # E-008 follow-up: anchor the capture region to the Laya
            # window's top-left corner so dragging/resizing the editor
            # between auto-adjust runs doesn't break the screenshot.
            # ``offset_x/y`` and ``width/height`` are populated when the
            # user picks a region — we capture the Laya window's
            # current rect at that moment and store the relative
            # offsets. ``enabled`` defaults to True because there is
            # essentially no downside (we still keep the absolute
            # region as a fallback).
            "laya_capture_anchor": {
                "enabled": False,
                "offset_x": 0,
                "offset_y": 0,
                "width": 0,
                "height": 0,
            },
        },
        "algorithm_config": {
            "max_iterations": 6,
            "target_score": 0.9,
            "perceptual_optional_interval": 50,
            "diff_visual_interval": 50,
            "apply_lmat": True,
            "capture_screen_after_apply": False,
            "use_laya_editor_capture": True,
            "laya_editor_capture": {
                "reload_scene_after_reimport": False,
                "refresh_after_reimport_delay_ms": 800,
                "timeout_s": 90,
                "capture_mode": "rotate_target",
                "render_backend": "draw_scene",
                "capture_debug_mode": "normal",
                "alpha_source": "render_alpha",
                "alpha_from_rgb_threshold": 1.0,
                "mask_alpha_mode": "binary",
                "mask_alpha_threshold": 1.0,
                "render_texture_srgb": True,
                "zero_transparent_rgb": False,
                "align_target_bounds": False,
                "target_base_yaw": 0.0,
                "target_base_pitch": 0.0,
            },
            "rerender_wait_ms": 900,
            "dynamic_rerender_wait": {
                "enabled": True,
                "min_wait_ms": 250,
                "interval_ms": 200,
                "diff_threshold": 0.25,
            },
            "use_capture_contract": False,
            "dry_run": False,
            "fit_score_mode": "research",
            "multiview_scoring": {
                "enabled": True,
                "require_all_views": True,
                "fit_aggregation": "mean",
                "diff_aggregation": "mean",
                "channel_aggregation": "mean_with_worst_severity",
                "primary_view_id": "v000_yaw0_pitch0",
            },
            # fresh_fit starts from a controlled baseline before searching;
            # refine_current keeps the current .lmat as-is and only continues
            # local optimization.
            "auto_adjust_mode": "fresh_fit",
            # Refresh is certified from the project preflight panel. Formal
            # auto-adjust runs must not write an extra probe value before the
            # first iteration because that can disturb the intended initial
            # material state.
            "laya_refresh_check": False,
            "laya_refresh_probe": {
                "mean_diff_change_threshold": 0.5,
                "mean_diff_restore_threshold": 2.5,
            },
            # E-006/E-014: optimizer is pluggable. 'semantic_group'
            # is the current response scheduler; legacy/subspace variants
            # remain available for comparison runs.
            "optimizer": "semantic_group",
            "cma_es": {
                "mode": "warm",
                "warm_start_iters": 12,
                "population_size": None,
                "sigma": None,
                "seed": None,
                # E-010: blend channel-level adjustment_hints into each
                # CMA-ES proposal. 0 disables, 0.30 is the recommended
                # default, > 0.5 is heavy expert-driven exploration.
                "hint_bias_mix_ratio": 0.30,
            },
            # Human-in-the-loop search-space control. Keys are Laya
            # semantic group names from preanalysis.laya_control_groups;
            # values are {"enabled": bool}. Disabled groups are removed
            # from semantic_group / CMA active search spaces when
            # fit_config.json is generated.
            "laya_control_group_overrides": {},
        },
        "manual_param_mapping": {},
        "manual_laya_control_schema": {
            "schema_version": 1,
            "base_auto_hash": "",
            "groups": {},
            "controls": {},
            "deleted_groups": [],
            "hidden_controls": [],
        },
        "active_laya_control_schema_preset_id": "auto",
        "laya_control_schema_presets": [],
        "llm_config": {
            "enabled": False,
            "provider": "openai-compatible",
            "note": "LLM is used for preanalysis shader semantics only; API settings are read from .env.",
        },
        "preanalysis_path": None,
        "active_job_id": None,
        "last_job_id": None,
        "active_run_id": None,
        "last_run_id": None,
    }
    save_project(data, config=config)
    return data


def save_project(data: dict[str, Any], config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    project_id = data.get("id")
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.match(project_id):
        raise ValueError("project.json missing valid 'id'")
    paths = project_paths(project_id, config)
    _ensure_within(paths.project_dir, config.output_dir.resolve())
    if not paths.project_dir.exists():
        raise FileNotFoundError(f"project dir missing: {project_id}")
    data = dict(data)
    data.pop("_summary", None)
    data["updated_at"] = _now_iso()
    paths.project_json.parent.mkdir(parents=True, exist_ok=True)
    paths.project_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    paths.configs_dir.mkdir(parents=True, exist_ok=True)
    (paths.configs_dir / PROJECT_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data


def patch_project(project_id: str, patch: dict[str, Any], config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    current = get_project(project_id, config)
    current.pop("_summary", None)
    merged = _deep_merge(current, patch)
    return save_project(merged, config=config)


def import_project_input_file(
    project_id: str,
    *,
    input_key: str,
    source_path: str,
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Copy a movable experiment input into ``output/<project>/inputs``.

    Runtime-owned paths such as the writable Laya ``.lmat`` and Laya project
    root intentionally do not go through this helper.
    """

    config = config or LoaderConfig()
    if input_key not in _IMPORT_FILE_SLOTS:
        raise ValueError(f"unsupported import input key: {input_key}")
    project = get_project(project_id, config)
    project.pop("_summary", None)
    paths = project_paths(project_id, config)
    _ensure_within(paths.project_dir, config.output_dir.resolve())
    source = Path(source_path).resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"input file not found: {source}")

    target_dir = paths.inputs_dir / _IMPORT_FILE_SLOTS[input_key]
    target = target_dir / source.name
    if source.parent.resolve() != target_dir.resolve():
        _reset_directory(target_dir)
        shutil.copy2(source, target)
    else:
        target_dir.mkdir(parents=True, exist_ok=True)

    inputs = dict(project.get("inputs") or {})
    inputs[input_key] = str(target.resolve())
    project["inputs"] = inputs
    return save_project(project, config=config)


def import_unity_reference_files(
    project_id: str,
    *,
    source_paths: list[str],
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Copy selected Unity multi-view screenshots into the project inputs."""

    config = config or LoaderConfig()
    if not source_paths:
        raise ValueError("source_paths must include at least one file")
    project = get_project(project_id, config)
    project.pop("_summary", None)
    paths = project_paths(project_id, config)
    _ensure_within(paths.project_dir, config.output_dir.resolve())

    sources: list[Path] = []
    for raw in source_paths:
        source = Path(str(raw)).resolve()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Unity reference file not found: {source}")
        if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
            raise ValueError(f"Unity reference must be an image file: {source}")
        sources.append(source)

    target_dir = paths.inputs_dir / _UNITY_REFERENCE_DIR
    used_names: set[str] = set()
    staged: list[tuple[str, bytes | Path]] = []
    for source in sources:
        name = _unique_file_name(source.name, used_names)
        if source.parent.resolve() == target_dir.resolve():
            staged.append((name, source.read_bytes()))
        else:
            staged.append((name, source))
    _reset_directory(target_dir)
    for name, source in staged:
        target = target_dir / name
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            shutil.copy2(source, target)

    inputs = dict(project.get("inputs") or {})
    inputs["unity_reference_dir_path"] = str(target_dir.resolve())
    inputs["unity_reference_glob"] = "*.*"
    project["inputs"] = inputs
    return save_project(project, config=config)


def delete_project(project_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    """Move the project dir into an ``output/.trash/`` sibling so it's recoverable."""

    config = config or LoaderConfig()
    paths = project_paths(project_id, config)
    _ensure_within(paths.project_dir, config.output_dir.resolve())
    if not paths.project_dir.exists():
        raise FileNotFoundError(f"project not found: {project_id}")
    trash_root = config.output_dir / ".trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = trash_root / f"{project_id}_{stamp}_{secrets.token_hex(3)}"
    shutil.move(str(paths.project_dir), str(target))
    return {"id": project_id, "trash_path": _to_rel_posix(target, config.project_root)}


def derive_fit_config(project_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    """Generate a CLI-compatible ``fit_config.json`` payload from project state.

    The returned dict mirrors ``tools/material_fit/fit_config.example.json``
    schema, so it can be written and fed straight into ``fit_material.py``.
    """

    config = config or LoaderConfig()
    project = get_project(project_id, config)
    inputs = project.get("inputs", {})
    algo = project.get("algorithm_config", {})

    def _abs(value: Any) -> str:
        return str(value) if isinstance(value, str) and value else ""

    laya_shader = _abs(inputs.get("laya_shader_path"))
    laya_lmat = _abs(inputs.get("laya_material_lmat_path"))
    if not laya_shader or not laya_lmat:
        raise ValueError(
            "project missing required inputs: laya_shader_path and laya_material_lmat_path",
        )

    paths = project_paths(project_id, config)
    # ``fit_material._resolve_path`` treats relative paths as relative to its
    # own assumed project root (config_path.resolve().parents[2]), which is
    # wrong when the config is nested under output/<project>/. Always emit
    # absolute paths so fit_material uses them verbatim.
    output_dir_abs = str(paths.project_dir.resolve())

    # The maintained capture path is the Laya Editor script path. The old
    # desktop-region screenshot flow remains only as internal legacy code, not
    # as a project-mode default.
    use_laya_editor_capture = True
    image_pairs: list[dict[str, str]] = []
    # Unity references are now standardized as a multi-view directory. The
    # legacy single-reference image pair is intentionally no longer derived.

    optimizer_value = str(algo.get("optimizer", "heuristic")).strip().lower()
    if optimizer_value not in (
        "heuristic",
        "cma_cold",
        "cma_warm",
        "semantic_group",
        "semantic_group_legacy_081",
        "subspace_cma_es",
    ):
        optimizer_value = "heuristic"
    raw_cma_es = algo.get("cma_es") if isinstance(algo.get("cma_es"), dict) else {}
    raw_mix = raw_cma_es.get("hint_bias_mix_ratio", 0.30)
    try:
        mix_ratio_value = float(raw_mix) if raw_mix is not None else 0.30
    except (TypeError, ValueError):
        mix_ratio_value = 0.30
    if mix_ratio_value < 0.0:
        mix_ratio_value = 0.0
    if mix_ratio_value > 1.0:
        mix_ratio_value = 1.0
    cma_es_payload: dict[str, Any] = {
        "mode": str(raw_cma_es.get("mode", "warm")).strip().lower() or "warm",
        "warm_start_iters": int(raw_cma_es.get("warm_start_iters", 12) or 0),
        "population_size": _coerce_optional_int(raw_cma_es.get("population_size")),
        "sigma": _coerce_optional_float(raw_cma_es.get("sigma")),
        "seed": _coerce_optional_int(raw_cma_es.get("seed")),
        # E-010: persisted to fit_config.json so the subprocess
        # picks up the mix ratio even if no CLI override is set.
        "hint_bias_mix_ratio": mix_ratio_value,
    }
    preanalysis_path = paths.preanalysis_json if paths.preanalysis_json.exists() else paths.project_dir / PREANALYSIS_FILE
    preanalysis = _read_json(preanalysis_path) if preanalysis_path.exists() else None

    fit_config: dict[str, Any] = {
        "case_name": project.get("id"),
        "laya_shader_path": laya_shader,
        "laya_material_path": laya_lmat,
        "unity_shader_path": _abs(inputs.get("unity_shader_path")),
        "unity_material_params_path": _abs(inputs.get("unity_material_params_path")),
        "image_pairs": image_pairs,
        "auto_adjust_target_score": float(algo.get("target_score", 0.9)),
        "capture_screen_after_apply": False if use_laya_editor_capture else bool(algo.get("capture_screen_after_apply", False)),
        "rerender_wait_ms": int(algo.get("rerender_wait_ms", 900)),
        "dynamic_rerender_wait": algo.get(
            "dynamic_rerender_wait",
            {
                "enabled": True,
                "min_wait_ms": 250,
                "interval_ms": 200,
                "diff_threshold": 0.25,
            },
        ),
        "screen_capture": {
            "capture_dir": _abs(inputs.get("laya_capture_dir"))
            or str((config.image_root / "vision" / "test_image").resolve()),
            "state_file": _abs(inputs.get("laya_capture_state_file")) or "",
            "prefix": _abs(inputs.get("laya_capture_prefix")) or "laya_candidate",
            "region": _format_region(inputs.get("laya_capture_region")),
            # E-012: cap the rolling ``laya_candidate_NN.png`` pool so
            # auto-adjust runs don't accumulate gigabytes of historical
            # captures. ``0`` keeps everything (legacy behavior); the
            # default 30 retains roughly the last N iterations across
            # all recent runs, which is enough for the UI's iter detail
            # panel and post-mortem diagnostics.
            "max_keep": int(inputs.get("laya_capture_max_keep") or 30),
        },
        "output_dir": output_dir_abs,
        "dry_run": bool(algo.get("dry_run", False)),
        "render_command": [],
        "laya_capture": {},
        "laya_editor_capture": {
            "enabled": use_laya_editor_capture,
            "laya_project": _abs(inputs.get("laya_project_path")),
            "command_path": _abs(inputs.get("laya_capture_command_path")),
            "camera_name": _abs(inputs.get("laya_capture_camera_name")) or "Capture Camera",
            "target_name": _abs(inputs.get("laya_capture_target_name")) or "model",
            "capture_mode": "rotate_target",
            "render_backend": "draw_scene",
            "capture_debug_mode": "normal",
            "alpha_source": "render_alpha",
            "alpha_from_rgb_threshold": 1.0,
            "mask_alpha_mode": "binary",
            "mask_alpha_threshold": 1.0,
            "render_texture_srgb": True,
            "zero_transparent_rgb": False,
            "align_target_bounds": False,
            "reference_dir": _abs(inputs.get("unity_reference_dir_path"))
            or (_discover_unity_reference_dir(config.project_root) if use_laya_editor_capture else ""),
            "reference_glob": str(inputs.get("unity_reference_glob") or "unity_ref_v*_yaw*_pitch*.png"),
            "refresh_assets": [_derive_laya_asset_path(laya_lmat, inputs.get("laya_project_path"))],
            "target_base_yaw": 0.0,
            "target_base_pitch": 0.0,
            **(algo.get("laya_editor_capture") if isinstance(algo.get("laya_editor_capture"), dict) else {}),
        },
        "fit_score_mode": _normalize_fit_score_mode(algo.get("fit_score_mode")),
        "multiview_scoring": _normalize_multiview_scoring(algo.get("multiview_scoring")),
        "auto_adjust_mode": str(algo.get("auto_adjust_mode", "fresh_fit")).lower(),
        "perceptual_optional_interval": _coerce_optional_int(algo.get("perceptual_optional_interval")) or 50,
        "diff_visual_interval": _coerce_optional_int(algo.get("diff_visual_interval")) or 50,
        "optimizer": optimizer_value,
        "cma_es": cma_es_payload,
        "laya_refresh_probe": _normalize_laya_refresh_probe(algo.get("laya_refresh_probe")),
        "laya_window": _normalize_laya_window(inputs.get("laya_window")),
        "laya_capture_anchor": _normalize_capture_anchor(
            inputs.get("laya_capture_anchor"),
            inputs.get("laya_window"),
        ),
    }
    if isinstance(preanalysis, dict):
        if isinstance(preanalysis.get("effect_graph"), dict):
            fit_config["effect_graph"] = _apply_effective_laya_control_schema(
                preanalysis["effect_graph"],
                preanalysis.get("effective_laya_control_schema"),
                algo.get("laya_control_group_overrides"),
            )
            fit_config["semantic_schema_integrity"] = _semantic_schema_integrity(
                preanalysis.get("manual_laya_control_schema"),
                preanalysis.get("effective_laya_control_schema"),
                fit_config.get("effect_graph"),
            )
        if isinstance(preanalysis.get("module_plan"), list):
            fit_config["module_plan"] = _apply_module_plan_overrides(
                preanalysis["module_plan"],
                algo.get("laya_control_group_overrides"),
            )
        if isinstance(preanalysis.get("effective_laya_control_schema"), dict):
            fit_config["effective_laya_control_schema"] = preanalysis["effective_laya_control_schema"]
            fit_config.setdefault("semantic_schema_integrity", _semantic_schema_integrity(
                preanalysis.get("manual_laya_control_schema"),
                preanalysis.get("effective_laya_control_schema"),
                fit_config.get("effect_graph"),
            ))
    return fit_config


def write_fit_config(project_id: str, config: LoaderConfig | None = None) -> Path:
    config = config or LoaderConfig()
    fit_config = derive_fit_config(project_id, config)
    paths = project_paths(project_id, config)
    paths.fit_config_json.parent.mkdir(parents=True, exist_ok=True)
    paths.fit_config_json.write_text(
        json.dumps(fit_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return paths.fit_config_json


def _summary(data: dict[str, Any], project_dir: Path, config: LoaderConfig) -> dict[str, Any]:
    inputs = data.get("inputs") or {}
    required_filled = all(
        bool(inputs.get(key))
        for key in ("laya_shader_path", "laya_material_lmat_path")
    )
    optional_filled = sum(
        1
        for key in (
            "unity_shader_path",
            "unity_material_params_path",
            "laya_capture_region",
        )
        if inputs.get(key)
    )
    last_run_id = data.get("last_run_id")
    auto_dir = project_dir / "auto_adjust"
    if isinstance(last_run_id, str) and last_run_id:
        run_auto_dir = project_dir / RUNS_DIR / last_run_id / "auto_adjust"
        if run_auto_dir.exists():
            auto_dir = run_auto_dir
    auto_iters = 0
    if auto_dir.exists():
        for entry in auto_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("iter_"):
                auto_iters += 1
    return {
        "id": data.get("id"),
        "name": data.get("name") or data.get("id"),
        "description": data.get("description") or "",
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "inputs_required_filled": required_filled,
        "inputs_optional_filled": optional_filled,
        "preanalysis_present": bool(data.get("preanalysis_path"))
        and (config.project_root / data.get("preanalysis_path")).exists()
        if isinstance(data.get("preanalysis_path"), str)
        else False,
        "iterations_count": auto_iters,
        "active_job_id": data.get("active_job_id"),
        "last_job_id": data.get("last_job_id"),
        "active_run_id": data.get("active_run_id"),
        "last_run_id": last_run_id,
        "output_dir": _to_rel_posix(project_dir, config.project_root),
    }


def _semantic_schema_integrity(manual_schema: Any, effective_schema: Any, effect_graph: Any) -> dict[str, Any]:
    warnings: list[str] = []
    expected: dict[str, str] = {}
    if isinstance(effective_schema, dict):
        for group in effective_schema.get("groups", []):
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("id") or "")
            for control in group.get("controls", []):
                if isinstance(control, dict) and control.get("name"):
                    expected[str(control["name"])] = group_id
    actual: dict[str, str] = {}
    if isinstance(effect_graph, dict):
        params = effect_graph.get("params") if isinstance(effect_graph.get("params"), dict) else {}
        for name, payload in params.items():
            if isinstance(payload, dict):
                actual[str(name)] = str(payload.get("group") or "")
    for name, group_id in sorted(expected.items()):
        if actual.get(name) and actual[name] != group_id:
            warnings.append(f"{name}: effective_schema={group_id}, effect_graph={actual[name]}")
    return {
        "schema_hash": _stable_hash(effective_schema),
        "manual_schema_hash": _stable_hash(manual_schema),
        "effective_schema_hash": _stable_hash(effective_schema),
        "effect_graph_hash": _stable_hash(effect_graph),
        "param_group_mismatch_count": len(warnings),
        "warnings": warnings[:50],
    }


def _stable_hash(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        text = json.dumps(str(value), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _derive_laya_asset_path(lmat_path: str, laya_project_path: Any) -> str:
    path = Path(lmat_path)
    if not path.is_absolute():
        return lmat_path

    project_path = Path(str(laya_project_path)) if isinstance(laya_project_path, str) and laya_project_path else None
    if project_path:
        assets_root = project_path / "assets"
        try:
            return path.resolve().relative_to(assets_root.resolve()).as_posix()
        except ValueError:
            pass

    parts = path.parts
    lowered = [part.lower() for part in parts]
    if "assets" in lowered:
        index = lowered.index("assets")
        return Path(*parts[index + 1:]).as_posix()
    return lmat_path


def _derive_laya_target_name(lmat_path: str) -> str:
    """Return the standardized Laya capture target root.

    The maintained Laya test scene contains exactly one model root named
    "model", regardless of the source asset id/path.
    """

    return "model"


def list_laya_scene_nodes(project_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    project = get_project(project_id, config)
    inputs = project.get("inputs") or {}
    return inspect_laya_scene_nodes(inputs)


def inspect_laya_scene_nodes(inputs: dict[str, Any]) -> dict[str, Any]:
    scene_path = _resolve_laya_scene_path(inputs)
    nodes: list[dict[str, Any]] = []
    if scene_path and scene_path.exists():
        try:
            payload = json.loads(scene_path.read_text(encoding="utf-8-sig"))
            _collect_laya_scene_nodes(payload, nodes, parent_path="", parent_active=True)
        except (OSError, json.JSONDecodeError):
            nodes = []
    lmat_target = _derive_laya_target_name(str(inputs.get("laya_material_lmat_path") or ""))
    active_names = {str(node.get("name")) for node in nodes if node.get("active") is True}
    recommended_target = (
        str(inputs.get("laya_capture_target_name") or "")
        or ("model" if "model" in active_names else "")
        or (lmat_target if lmat_target in active_names else "")
        or lmat_target
        or next((str(node.get("name")) for node in nodes if node.get("active") is True and node.get("type") != "Camera"), "")
    )
    recommended_camera = (
        str(inputs.get("laya_capture_camera_name") or "")
        or next((str(node.get("name")) for node in nodes if node.get("type") == "Camera" and node.get("name") == "Capture Camera"), "")
        or next((str(node.get("name")) for node in nodes if node.get("type") == "Camera"), "")
        or "Capture Camera"
    )
    return {
        "scene_path": str(scene_path) if scene_path else "",
        "nodes": nodes,
        "recommended_target_name": recommended_target,
        "recommended_camera_name": recommended_camera,
    }


def _resolve_laya_scene_path(inputs: dict[str, Any]) -> Path | None:
    laya_project = inputs.get("laya_project_path")
    if isinstance(laya_project, str) and laya_project:
        project_path = Path(laya_project)
        candidates = [
            project_path / "assets" / "resources" / "game.ls",
            project_path / "assets" / "game.ls",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        resources = project_path / "assets" / "resources"
        if resources.exists():
            matches = sorted(resources.glob("*.ls"), key=lambda item: item.name)
            if matches:
                return matches[0]
    command_path = inputs.get("laya_capture_command_path")
    if isinstance(command_path, str) and command_path:
        path = Path(command_path)
        assets_root = path.parent if path.parent.name == "assets" else path.parent / "assets"
        candidate = assets_root / "resources" / "game.ls"
        if candidate.exists():
            return candidate
    return None


def _collect_laya_scene_nodes(
    node: Any,
    out: list[dict[str, Any]],
    *,
    parent_path: str,
    parent_active: bool,
) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_laya_scene_nodes(item, out, parent_path=parent_path, parent_active=parent_active)
        return
    if not isinstance(node, dict):
        return
    name = node.get("name")
    node_type = node.get("_$type") or ("Prefab" if node.get("_$prefab") else "")
    local_active = node.get("active")
    active = parent_active and (local_active is not False)
    current_path = f"{parent_path}/{name}" if parent_path and name else (str(name) if name else parent_path)
    if isinstance(name, str) and name:
        out.append(
            {
                "name": name,
                "type": str(node_type or ""),
                "active": active,
                "path": current_path,
                "prefab": str(node.get("_$prefab") or ""),
            }
        )
    children = node.get("_$child")
    if isinstance(children, list):
        for child in children:
            _collect_laya_scene_nodes(child, out, parent_path=current_path, parent_active=active)


def _discover_unity_reference_dir(project_root: Path) -> str:
    unity_root = project_root / "tools" / "material_fit" / "unity"
    if not unity_root.exists():
        return ""
    candidates: list[tuple[float, int, Path]] = []
    for directory in unity_root.rglob("*"):
        if not directory.is_dir():
            continue
        files = [
            item
            for item in directory.glob("unity_ref_v*_yaw*_pitch*.png")
            if item.is_file() and "_mask" not in item.stem
        ]
        if not files:
            continue
        newest = max(item.stat().st_mtime for item in files)
        current_bonus = 1 if "current" in directory.name.lower() else 0
        candidates.append((newest, current_bonus, directory))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1], str(item[2])), reverse=True)
    return str(candidates[0][2].resolve())


def _normalize_capture_anchor(value: Any, laya_window_value: Any) -> dict[str, Any]:
    """Normalize the project's laya_capture_anchor block into a fit_config dict.

    Pulls ``process_pattern`` / ``title_pattern`` from the *separate*
    ``laya_window`` block — they are the same identifiers as for
    focusing, so we don't make the user enter them twice.
    """
    if not isinstance(value, dict):
        value = {}
    window = laya_window_value if isinstance(laya_window_value, dict) else {}
    return {
        "enabled": bool(value.get("enabled", False)),
        "offset_x": int(value.get("offset_x", 0) or 0),
        "offset_y": int(value.get("offset_y", 0) or 0),
        "width": int(value.get("width", 0) or 0),
        "height": int(value.get("height", 0) or 0),
        "process_pattern": str(window.get("process_pattern", "LayaAirIDE")),
        "title_pattern": str(window.get("title_pattern", "")),
    }


def _normalize_laya_window(value: Any) -> dict[str, Any]:
    """Normalize the project's laya_window block into a fit_config-ready dict.

    Always returns a dict with all three keys filled in (so fit_material's
    ``_build_focus_callback`` can read it without further None-checks).
    Empty ``process_pattern`` means "disable focus", which is preserved.
    """
    if not isinstance(value, dict):
        value = {}
    return {
        "process_pattern": str(value.get("process_pattern", "LayaAirIDE")),
        "title_pattern": str(value.get("title_pattern", "")),
        "settle_ms": int(value.get("settle_ms", 250) or 0),
    }


def _normalize_laya_refresh_probe(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        value = {}
    change = _coerce_optional_float(value.get("mean_diff_change_threshold"))
    restore = _coerce_optional_float(value.get("mean_diff_restore_threshold"))
    return {
        "mean_diff_change_threshold": change if change is not None and change >= 0.0 else 0.5,
        "mean_diff_restore_threshold": restore if restore is not None and restore >= 0.0 else 2.5,
    }


def _normalize_fit_score_mode(value: Any) -> str:
    mode = str(value or "research").strip().lower()
    if mode in {"linear", "perceptual", "human_accept", "research"}:
        return mode
    return "research"


def _normalize_multiview_scoring(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    return {
        "enabled": bool(value.get("enabled", True)),
        "require_all_views": bool(value.get("require_all_views", True)),
        "fit_aggregation": str(value.get("fit_aggregation") or "mean"),
        "diff_aggregation": str(value.get("diff_aggregation") or "mean"),
        "channel_aggregation": str(value.get("channel_aggregation") or "mean_with_worst_severity"),
        "primary_view_id": str(value.get("primary_view_id") or "v000_yaw0_pitch0"),
    }


def _apply_effective_laya_control_schema(
    effect_graph: dict[str, Any],
    effective_schema: Any,
    overrides: Any,
) -> dict[str, Any]:
    payload = json.loads(json.dumps(effect_graph))
    groups = payload.get("groups") if isinstance(payload.get("groups"), dict) else {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if isinstance(effective_schema, dict):
        schema_groups = effective_schema.get("groups") if isinstance(effective_schema.get("groups"), list) else []
        for raw_group in schema_groups:
            if not isinstance(raw_group, dict) or not raw_group.get("id"):
                continue
            group_name = str(raw_group["id"])
            graph_group = groups.setdefault(
                group_name,
                {
                    "name": group_name,
                    "params": [],
                    "gate_params": [],
                    "define_gates": [],
                    "channels": [],
                    "active": True,
                    "current_active": True,
                    "suggested_by_unity": False,
                    "probe_required": False,
                    "search_priority": 0.0,
                    "search_params": [],
                    "unity_features": [],
                    "evidence": [],
                    "reason": "",
                },
            )
            graph_group["name"] = group_name
            graph_group["current_active"] = bool(raw_group.get("current_active", True))
            graph_group["active"] = bool(raw_group.get("current_active", True))
            graph_group["suggested_by_unity"] = bool(raw_group.get("suggested_by_unity", False))
            graph_group["probe_required"] = bool(raw_group.get("probe_required", False))
            graph_group["search_priority"] = float(raw_group.get("search_priority", 0.0) or 0.0)
            graph_group["order"] = int(raw_group.get("order", 0) or 0)
            graph_group["source"] = str(raw_group.get("source") or graph_group.get("source") or "auto")
            graph_group["define_gates"] = [str(item) for item in raw_group.get("define_gates", []) if isinstance(item, str)]
            graph_group["gate_params"] = [str(item) for item in raw_group.get("gate_params", []) if isinstance(item, str)]
            controls = [item for item in raw_group.get("controls", []) if isinstance(item, dict) and item.get("name")]
            control_names = [str(item["name"]) for item in controls]
            graph_group["params"] = control_names
            graph_group["search_params"] = [
                str(item["name"])
                for item in controls
                if item.get("searchable") is True
                and _effective_control_is_search_param(item)
                and bool(raw_group.get("enabled", True))
            ]
            if not bool(raw_group.get("enabled", True)):
                graph_group["manual_enabled"] = False
                graph_group["suggested_by_unity"] = False
                graph_group["probe_required"] = False
                graph_group["search_priority"] = 0.0
                graph_group["search_params"] = []
            for control in controls:
                name = str(control["name"])
                param = params.get(name)
                if not isinstance(param, dict):
                    continue
                param["group"] = group_name
                param["role"] = str(control.get("role") or param.get("role") or "value")
                param["transform"] = str(control.get("transform") or param.get("transform") or "linear")
                param["searchable"] = bool(control.get("searchable", True) and raw_group.get("enabled", True))
                param["source"] = str(control.get("source") or param.get("source") or "auto")
                param["confidence"] = _coerce_optional_float(control.get("confidence"))
                param["evidence"] = [str(item) for item in control.get("evidence", []) if isinstance(item, str)]
                param["risk"] = str(control.get("risk") or "")
                param["conflict_status"] = str(control.get("conflict_status") or "none")
                param["auto_group"] = str(control.get("auto_group") or param.get("auto_group") or "")
                param["semantic_sources"] = [str(item) for item in control.get("semantic_sources", []) if isinstance(item, str)]
                range_min = control.get("range_min")
                range_max = control.get("range_max")
                if isinstance(range_min, (int, float)) and isinstance(range_max, (int, float)) and float(range_min) < float(range_max):
                    param["range_min"] = float(range_min)
                    param["range_max"] = float(range_max)
                    param["range_source"] = "effective_visual_search"
                if control.get("reason"):
                    param["reason"] = str(control.get("reason"))

    disabled = _disabled_laya_control_groups(overrides)
    for group_name in disabled:
        group = groups.get(group_name)
        if not isinstance(group, dict):
            continue
        group["manual_enabled"] = False
        group["suggested_by_unity"] = False
        group["probe_required"] = False
        group["search_priority"] = 0.0
        group["search_params"] = []
        group_params = [
            str(item)
            for item in group.get("params", [])
            if isinstance(item, str)
        ] + [
            str(item)
            for item in group.get("gate_params", [])
            if isinstance(item, str)
        ]
        for param_name in group_params:
            param = params.get(param_name)
            if isinstance(param, dict):
                param["searchable"] = False
                param["reason"] = "disabled by human laya control group override"
    payload["manual_laya_control_group_overrides"] = {
        name: {"enabled": False}
        for name in sorted(disabled)
    }
    return payload


def _apply_module_plan_overrides(module_plan: list[Any], overrides: Any) -> list[Any]:
    disabled = _disabled_laya_control_groups(overrides)
    out = json.loads(json.dumps(module_plan))
    for item in out:
        if not isinstance(item, dict) or str(item.get("group")) not in disabled:
            continue
        item["manual_enabled"] = False
        item["suggested_by_unity"] = False
        item["probe_required"] = False
        item["search_priority"] = 0.0
        item["action"] = "disabled_by_human"
        item["search_params"] = []
    return out


def _disabled_laya_control_groups(overrides: Any) -> set[str]:
    if not isinstance(overrides, dict):
        return set()
    disabled: set[str] = set()
    for group_name, raw in overrides.items():
        if isinstance(raw, dict):
            enabled = raw.get("enabled", True)
        else:
            enabled = raw
        if enabled is False:
            disabled.add(str(group_name))
    return disabled


def _effective_control_is_search_param(control: dict[str, Any]) -> bool:
    """Resolve legacy searchable/is_search_param conflicts conservatively.

    Older presets could lock only the ``searchable`` field while leaving a
    stale ``is_search_param=false`` behind. In that case the UI says the
    parameter is searchable, but the optimizer silently drops it from the
    group search space. Treat ``is_search_param=false`` as authoritative only
    when that field itself is locked.
    """

    if control.get("is_search_param", control.get("searchable", True)) is not False:
        return True
    locked = {str(item) for item in control.get("locked_fields", []) if isinstance(item, str)}
    return "is_search_param" not in locked


def _format_region(region: Any) -> str:
    if not isinstance(region, dict):
        return ""
    keys = ("x", "y", "width", "height")
    if not all(k in region for k in keys):
        return ""
    try:
        x, y, w, h = (int(region[k]) for k in keys)
    except (TypeError, ValueError):
        return ""
    return f"{x},{y},{w},{h}"


def _ensure_within(target: Path, root: Path) -> None:
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path {target} outside of {root}") from exc


def _reset_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"input target is not a directory: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _unique_file_name(name: str, used: set[str]) -> str:
    candidate = Path(name).name
    if candidate not in used:
        used.add(candidate)
        return candidate
    stem = Path(candidate).stem
    suffix = Path(candidate).suffix
    index = 2
    while True:
        next_name = f"{stem}_{index}{suffix}"
        if next_name not in used:
            used.add(next_name)
            return next_name
        index += 1


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if (
            isinstance(value, dict)
            and isinstance(out.get(key), dict)
            and key not in {"laya_capture_region", "laya_capture_anchor"}
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out

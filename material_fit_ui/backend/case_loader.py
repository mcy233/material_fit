"""Read-only loaders for ``tools/material_fit/output/<case>/`` artefacts.

The loaders do not depend on FastAPI so they can be reused by future CLIs
or unit tests. They tolerate missing files because partially-completed
runs are common during development.

Case kinds
----------
A case directory is classified into one of:

- ``auto_adjust``: real auto-adjust loop has run; ``auto_adjust/iter_*/decision.json`` exist.
- ``probe``: only dry-run probe candidates exist under ``iterations/iter_*/params.json``.
- ``diff_only``: a one-shot ``analyze_diff`` invocation that wrote ``diff_analysis.json`` and
  ``diff_visual.png`` at the case root.
- ``empty``: nothing visualizable found.

The ``list_iterations``/``get_iteration_detail`` API is unified across kinds by
synthesizing iteration ids:

- ``iter_NNNN`` for real auto_adjust iterations
- ``probe_NNNN`` for probe-only iterations
- ``root_diff`` for diff_only synthesized single entry
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "tools" / "material_fit" / "output"
ALLOWED_IMAGE_ROOT = (PROJECT_ROOT / "tools" / "material_fit").resolve()
_ITER_RE = re.compile(r"^iter_(\d+)$", re.IGNORECASE)
_PROBE_PREFIX = "probe_"
_AUTO_PREFIX = "iter_"
_ROOT_DIFF_ID = "root_diff"


@dataclass(frozen=True)
class LoaderConfig:
    project_root: Path = PROJECT_ROOT
    output_dir: Path = DEFAULT_OUTPUT_DIR
    image_root: Path = ALLOWED_IMAGE_ROOT


def list_cases(config: LoaderConfig | None = None) -> list[dict[str, Any]]:
    """Return every case dir with its kind, one-line summary and last-modified time."""

    config = config or LoaderConfig()
    if not config.output_dir.exists():
        return []
    cases: list[dict[str, Any]] = []
    for entry in sorted(config.output_dir.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        cases.append(_build_case_summary(entry, config))
    return cases


def get_case_overview(case_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    case_dir = _resolve_case_dir(case_id, config)
    artifact_dir = _project_artifact_dir(case_dir) or case_dir
    summary = _build_case_summary(case_dir, config)
    auto_dir = artifact_dir / "auto_adjust"
    auto_result = _read_json(auto_dir / "auto_adjust_result.json") if auto_dir.exists() else None
    return {
        **summary,
        "auto_adjust_result": _strip_iterations(auto_result) if isinstance(auto_result, dict) else None,
        "stage_plan": _read_json(artifact_dir / "stage_plan.json"),
        "adjustment_policies": _read_json(artifact_dir / "adjustment_policies.json"),
        "laya_shader_params": _read_json(artifact_dir / "laya_shader_params.json"),
        "laya_material_params": _read_json(artifact_dir / "laya_material_params.json"),
        "initial_params": _read_json(artifact_dir / "initial_params.json"),
        "unity_shader_params": _read_json(artifact_dir / "unity_shader_params.json"),
        "unity_material_params": _read_json(artifact_dir / "unity_material_params.json"),
        "report_path": _to_image_url_unsafe(artifact_dir / "report.md", config),
        "root_diff_analysis": _read_json(artifact_dir / "diff_analysis.json"),
    }


def get_case_report(case_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    """Return ``report.md`` text plus a ``base_url`` for relative image references."""

    config = config or LoaderConfig()
    case_dir = _resolve_case_dir(case_id, config)
    artifact_dir = _project_artifact_dir(case_dir) or case_dir
    report_path = artifact_dir / "report.md"
    if not report_path.exists():
        raise FileNotFoundError(f"report.md not found under case {case_id}")
    try:
        text = report_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FileNotFoundError(f"failed to read report.md: {exc}") from exc
    return {
        "case_id": case_id,
        "report_path": _to_rel_posix(report_path, config.project_root),
        "case_dir": _to_rel_posix(artifact_dir, config.project_root),
        "image_base": "/api/image?path=",
        "text": text,
    }


def list_iterations(case_id: str, config: LoaderConfig | None = None) -> list[dict[str, Any]]:
    """Unified iteration timeline across auto_adjust / probe / diff_only cases."""

    config = config or LoaderConfig()
    case_dir = _resolve_case_dir(case_id, config)
    case_dir = _project_artifact_dir(case_dir) or case_dir
    auto_iters = _list_auto_adjust_iterations(case_dir, config)
    if auto_iters:
        return auto_iters
    probe_iters = _list_probe_iterations(case_dir, config)
    if probe_iters:
        return probe_iters
    diff_only = _synthesize_root_diff_iteration(case_dir, config)
    if diff_only:
        return [diff_only]
    return []


def get_iteration_detail(case_id: str, iter_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    case_dir = _resolve_case_dir(case_id, config)
    case_dir = _project_artifact_dir(case_dir) or case_dir

    if iter_id == _ROOT_DIFF_ID:
        return _load_root_diff_detail(case_id, case_dir, config)

    if iter_id.startswith(_PROBE_PREFIX):
        return _load_probe_detail(case_id, case_dir, iter_id, config)

    if iter_id.startswith(_AUTO_PREFIX):
        return _load_auto_adjust_detail(case_id, case_dir, iter_id, config)

    raise ValueError(f"unknown iter_id format: {iter_id!r}")


def to_image_url(absolute_or_relative: str | Path, config: LoaderConfig | None = None) -> str | None:
    """Turn an absolute or project-relative path into a frontend-safe URL.

    - Paths inside the repo (``project_root``) become ``/api/image?path=<rel>``.
    - Paths outside the repo (e.g., user-picked screenshots from elsewhere on
      disk) become ``/api/files/preview?path=<urlencoded abs>``. This is fine
      because the backend only ever runs on the user's own machine.

    Returns ``None`` if the path can't be resolved or the file doesn't exist.
    """

    from urllib.parse import quote

    config = config or LoaderConfig()
    if absolute_or_relative is None or absolute_or_relative == "":
        return None
    raw = Path(str(absolute_or_relative))
    candidate = raw if raw.is_absolute() else (config.project_root / raw)
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    try:
        rel = resolved.relative_to(config.project_root).as_posix()
        return f"/api/image?path={quote(rel)}"
    except ValueError:
        return f"/api/files/preview?path={quote(str(resolved))}"


def resolve_image_path(rel_or_abs: str, config: LoaderConfig | None = None) -> Path:
    """Resolve an incoming ``path`` query string for the ``/api/image`` endpoint.

    Accepts repo-relative paths and resolves them under ``project_root``.
    The path must end up inside the repo; absolute paths outside the repo
    must instead use ``resolve_external_preview_path``.
    """

    config = config or LoaderConfig()
    raw = Path(rel_or_abs)
    candidate = raw if raw.is_absolute() else (config.project_root / raw)
    resolved = candidate.resolve()
    _ensure_within(resolved, config.project_root.resolve())
    if not resolved.is_file():
        raise FileNotFoundError(f"image not found: {rel_or_abs}")
    return resolved


def resolve_external_preview_path(absolute_path: str, config: LoaderConfig | None = None) -> Path:
    """Resolve an arbitrary absolute path for ``/api/files/preview``.

    The backend is local-only, so we accept any absolute path that resolves
    to a real file. We deliberately do *not* enforce repo membership here;
    the user's Unity reference image or Laya capture may live anywhere.
    """

    _ = config or LoaderConfig()
    raw = Path(absolute_path)
    if not raw.is_absolute():
        raise ValueError(f"preview path must be absolute, got {absolute_path!r}")
    try:
        resolved = raw.resolve()
    except OSError as exc:
        raise FileNotFoundError(f"failed to resolve {absolute_path!r}: {exc}") from exc
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"file not found: {absolute_path}")
    return resolved


def _classify_case(case_dir: Path) -> str:
    if (case_dir / "project.json").exists():
        return "project"
    if (case_dir / "auto_adjust" / "auto_adjust_result.json").exists():
        return "auto_adjust"
    iter_dir = case_dir / "iterations"
    if iter_dir.is_dir() and any(iter_dir.glob("iter_*/params.json")):
        return "probe"
    if (case_dir / "diff_analysis.json").exists():
        return "diff_only"
    return "empty"


_KIND_LABELS = {
    "project": "project",
    "auto_adjust": "auto-adjust",
    "probe": "probe",
    "diff_only": "diff",
    "empty": "empty",
}


def _build_case_summary(case_dir: Path, config: LoaderConfig) -> dict[str, Any]:
    kind = _classify_case(case_dir)
    auto_dir = case_dir / "auto_adjust"
    auto_result = _read_json(auto_dir / "auto_adjust_result.json") if auto_dir.exists() else None

    iterations_count = 0
    summary_parts: list[str] = []
    extras: dict[str, Any] = {}

    if kind == "project":
        try:
            from . import project_store

            data = project_store._read_json(case_dir / "project.json")  # noqa: SLF001
        except Exception:
            data = None
        if isinstance(data, dict):
            inputs = data.get("inputs") or {}
            artifact_dir = _project_artifact_dir(case_dir)
            auto_dir_for_count = (artifact_dir or case_dir) / "auto_adjust"
            iterations_count = (
                sum(
                    1
                    for entry in auto_dir_for_count.iterdir()
                    if entry.is_dir() and _ITER_RE.match(entry.name)
                )
                if auto_dir_for_count.exists()
                else 0
            )
            active = data.get("active_job_id")
            last_run = data.get("last_run_id")
            last_job = data.get("last_job_id")
            ready = bool(inputs.get("laya_shader_path")) and bool(inputs.get("laya_material_lmat_path"))
            summary_parts.append("ready" if ready else "需要补充输入")
            if iterations_count:
                summary_parts.append(f"{iterations_count} iters")
            if last_run:
                summary_parts.append(f"run {last_run}")
            if active:
                summary_parts.append("running")
            extras = {
                "project_name": data.get("name") or data.get("id"),
                "active_job_id": active,
                "active_run_id": data.get("active_run_id"),
                "last_job_id": last_job,
                "last_run_id": last_run,
                "inputs_ready": ready,
            }
        else:
            summary_parts.append("project")
    elif kind == "auto_adjust" and isinstance(auto_result, dict):
        iters = auto_result.get("iterations") if isinstance(auto_result.get("iterations"), list) else []
        iterations_count = sum(1 for path in auto_dir.iterdir() if _ITER_RE.match(path.name)) if auto_dir.exists() else 0
        best_fit = auto_result.get("best_fit_score")
        target = auto_result.get("target_score")
        status = auto_result.get("status")
        if isinstance(best_fit, (int, float)):
            summary_parts.append(f"best fit {float(best_fit):.4f}")
        if isinstance(target, (int, float)):
            summary_parts.append(f"target {float(target):.3f}")
        summary_parts.append(f"{iterations_count} iters")
        if status:
            summary_parts.append(str(status))
        extras = {
            "best_fit_score": best_fit if isinstance(best_fit, (int, float)) else None,
            "best_score": auto_result.get("best_score") if isinstance(auto_result.get("best_score"), (int, float)) else None,
            "target_score": target if isinstance(target, (int, float)) else None,
            "auto_adjust_status": status,
        }
        del iters  # not used
    elif kind == "probe":
        probe_dir = case_dir / "iterations"
        iterations_count = sum(1 for path in probe_dir.iterdir() if _ITER_RE.match(path.name))
        summary_parts.append(f"{iterations_count} probe candidates")
    elif kind == "diff_only":
        root_diff = _read_json(case_dir / "diff_analysis.json")
        if isinstance(root_diff, dict):
            score = root_diff.get("score")
            if isinstance(score, (int, float)):
                summary_parts.append(f"RGB MAE {float(score):.4f}")
                extras["root_diff_score"] = float(score)
        iterations_count = 1 if (case_dir / "diff_analysis.json").exists() else 0
    else:
        summary_parts.append("empty")

    return {
        "id": case_dir.name,
        "output_dir": _to_rel_posix(case_dir, config.project_root),
        "kind": kind,
        "kind_label": _KIND_LABELS.get(kind, kind),
        "iterations_count": iterations_count,
        "summary": " · ".join(summary_parts),
        "last_modified": _last_modified_iso(case_dir),
        "has_auto_adjust": kind == "auto_adjust",
        "has_report": (case_dir / "report.md").exists(),
        **extras,
    }


def _list_auto_adjust_iterations(case_dir: Path, config: LoaderConfig) -> list[dict[str, Any]]:
    auto_dir = case_dir / "auto_adjust"
    if not auto_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for iter_dir in sorted(auto_dir.iterdir(), key=lambda path: path.name.lower()):
        match = _ITER_RE.match(iter_dir.name)
        if not iter_dir.is_dir() or not match:
            continue
        decision_payload = _read_json(iter_dir / "decision.json")
        if not isinstance(decision_payload, dict):
            continue
        decision = decision_payload.get("decision") if isinstance(decision_payload.get("decision"), dict) else {}
        diff_image_url = _decision_diff_image_url(iter_dir, decision_payload, config)
        items.append(
            {
                "iter_id": iter_dir.name,
                "iteration": decision_payload.get("iteration", int(match.group(1))),
                "kind": "auto_adjust",
                "selected_stage": decision_payload.get("selected_stage"),
                "diff_score_before": decision_payload.get("diff_score_before"),
                "fit_score_before": decision_payload.get("fit_score_before"),
                "target_score": decision_payload.get("target_score"),
                "stop_reason": decision.get("stop_reason"),
                "iteration_gain": decision.get("iteration_gain"),
                "changes_count": len(decision.get("changes", []) if isinstance(decision.get("changes"), list) else []),
                "applied_lmat": decision.get("applied_lmat"),
                "diff_image_url": diff_image_url,
            }
        )
    items.sort(key=lambda item: int(item["iteration"]) if isinstance(item.get("iteration"), int) else 0)
    return items


def _list_probe_iterations(case_dir: Path, config: LoaderConfig) -> list[dict[str, Any]]:
    probe_dir = case_dir / "iterations"
    if not probe_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for iter_dir in sorted(probe_dir.iterdir(), key=lambda path: path.name.lower()):
        match = _ITER_RE.match(iter_dir.name)
        if not iter_dir.is_dir() or not match:
            continue
        params_path = iter_dir / "params.json"
        if not params_path.exists():
            continue
        items.append(
            {
                "iter_id": f"{_PROBE_PREFIX}{int(match.group(1)):04d}",
                "iteration": int(match.group(1)),
                "kind": "probe",
                "selected_stage": "probe",
                "diff_score_before": None,
                "fit_score_before": None,
                "target_score": None,
                "stop_reason": None,
                "iteration_gain": None,
                "changes_count": 0,
                "applied_lmat": None,
                "diff_image_url": None,
            }
        )
    items.sort(key=lambda item: int(item["iteration"]) if isinstance(item.get("iteration"), int) else 0)
    # Mark for downstream that these are probe entries with no diff data
    _ = config
    return items


def _synthesize_root_diff_iteration(case_dir: Path, config: LoaderConfig) -> dict[str, Any] | None:
    root_diff = case_dir / "diff_analysis.json"
    if not root_diff.exists():
        return None
    payload = _read_json(root_diff)
    score = payload.get("score") if isinstance(payload, dict) else None
    diff_image = (case_dir / "diff_visual.png") if (case_dir / "diff_visual.png").exists() else None
    return {
        "iter_id": _ROOT_DIFF_ID,
        "iteration": 0,
        "kind": "diff_only",
        "selected_stage": "root_diff",
        "diff_score_before": score if isinstance(score, (int, float)) else None,
        "fit_score_before": (1.0 - float(score)) if isinstance(score, (int, float)) else None,
        "target_score": None,
        "stop_reason": None,
        "iteration_gain": None,
        "changes_count": 0,
        "applied_lmat": None,
        "diff_image_url": to_image_url(diff_image, config) if diff_image else None,
    }


def _load_auto_adjust_detail(case_id: str, case_dir: Path, iter_id: str, config: LoaderConfig) -> dict[str, Any]:
    iter_dir = (case_dir / "auto_adjust" / iter_id).resolve()
    _ensure_within(iter_dir, case_dir.resolve())
    if not iter_dir.is_dir() or not _ITER_RE.match(iter_dir.name):
        raise FileNotFoundError(f"iteration not found: {iter_id}")
    decision = _read_json(iter_dir / "decision.json")
    diff_analysis = _read_json(iter_dir / "image_analysis" / "diff_analysis.json")
    if not isinstance(diff_analysis, dict):
        diff_analysis = _read_json(iter_dir / "image_analysis" / "pair_00" / "diff_analysis.json")
    candidate_params = _read_json(iter_dir / "candidate" / "params.json")
    candidate_lmat = _first_lmat_text(iter_dir / "candidate")
    images = _collect_iteration_images(diff_analysis, config)
    multiview_images = _collect_multiview_images(iter_dir, decision, config)
    if multiview_images and (not images.get("candidate") or not images.get("diff")):
        first = multiview_images[0]
        images = {
            "reference": images.get("reference") or first.get("reference"),
            "candidate": images.get("candidate") or first.get("candidate"),
            "diff": images.get("diff") or first.get("diff"),
        }
    return {
        "case_id": case_id,
        "iter_id": iter_id,
        "kind": "auto_adjust",
        "decision": decision,
        "diff_analysis": diff_analysis,
        "candidate_params": candidate_params,
        "candidate_lmat_text": candidate_lmat,
        "images": images,
        "multiview_images": multiview_images,
    }


def _load_probe_detail(case_id: str, case_dir: Path, iter_id: str, config: LoaderConfig) -> dict[str, Any]:
    suffix = iter_id[len(_PROBE_PREFIX):]
    if not suffix.isdigit():
        raise ValueError(f"invalid probe iter_id: {iter_id!r}")
    real_iter = f"iter_{int(suffix):04d}"
    iter_dir = (case_dir / "iterations" / real_iter).resolve()
    _ensure_within(iter_dir, case_dir.resolve())
    if not iter_dir.is_dir():
        raise FileNotFoundError(f"probe iteration not found: {iter_id}")
    candidate_params = _read_json(iter_dir / "params.json")
    capture_request = _read_json(iter_dir / "capture_request.json")
    return {
        "case_id": case_id,
        "iter_id": iter_id,
        "kind": "probe",
        "decision": None,
        "diff_analysis": None,
        "candidate_params": candidate_params,
        "candidate_lmat_text": None,
        "capture_request": capture_request,
        "images": {"reference": None, "candidate": None, "diff": None},
        "_note": "probe iterations only contain candidate params; no decision/diff/images",
    }


def _load_root_diff_detail(case_id: str, case_dir: Path, config: LoaderConfig) -> dict[str, Any]:
    diff_path = case_dir / "diff_analysis.json"
    if not diff_path.exists():
        raise FileNotFoundError(f"no root diff_analysis.json under case {case_id}")
    diff_analysis = _read_json(diff_path)
    images = _collect_iteration_images(diff_analysis, config)
    if not images.get("diff"):
        # Fall back to local diff_visual.png next to diff_analysis.json
        local_diff = case_dir / "diff_visual.png"
        if local_diff.exists():
            images["diff"] = to_image_url(local_diff, config)
    return {
        "case_id": case_id,
        "iter_id": _ROOT_DIFF_ID,
        "kind": "diff_only",
        "decision": None,
        "diff_analysis": diff_analysis,
        "candidate_params": None,
        "candidate_lmat_text": None,
        "images": images,
        "_note": "synthesized from case-root diff_analysis.json (one-shot analyze_diff output)",
    }


def _resolve_case_dir(case_id: str, config: LoaderConfig) -> Path:
    if not case_id or "/" in case_id or "\\" in case_id or ".." in case_id:
        raise ValueError(f"invalid case id: {case_id!r}")
    case_dir = (config.output_dir / case_id).resolve()
    _ensure_within(case_dir, config.output_dir.resolve())
    if not case_dir.is_dir():
        raise FileNotFoundError(f"case not found: {case_id}")
    return case_dir


def _project_artifact_dir(case_dir: Path) -> Path | None:
    project_json = case_dir / "project.json"
    if not project_json.exists():
        return None
    project = _read_json(project_json)
    if not isinstance(project, dict):
        return None
    run_id = project.get("active_run_id") or project.get("last_run_id")
    job_id = project.get("active_job_id") or project.get("last_job_id")
    if isinstance(job_id, str) and job_id and isinstance(run_id, str) and run_id:
        run_dir = (case_dir / "jobs" / job_id / "runs" / run_id).resolve()
        try:
            _ensure_within(run_dir, (case_dir / "jobs" / job_id / "runs").resolve())
        except ValueError:
            return None
        if run_dir.is_dir():
            return run_dir
    if isinstance(run_id, str) and run_id:
        run_dir = (case_dir / "runs" / run_id).resolve()
        try:
            _ensure_within(run_dir, (case_dir / "runs").resolve())
        except ValueError:
            return None
        if run_dir.is_dir():
            return run_dir
    return None


def _ensure_within(target: Path, root: Path) -> None:
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path {target} is outside {root}") from exc


def _read_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def _strip_iterations(auto_result: dict[str, Any]) -> dict[str, Any]:
    """Drop the noisy ``iterations`` array; the timeline endpoint owns it."""

    return {key: value for key, value in auto_result.items() if key != "iterations"}


def _decision_diff_image_url(iter_dir: Path, decision_payload: dict[str, Any], config: LoaderConfig) -> str | None:
    diff_image = iter_dir / "image_analysis" / "diff_visual.png"
    if diff_image.exists():
        return to_image_url(diff_image, config)
    multiview = decision_payload.get("multiview_analysis") if isinstance(decision_payload.get("multiview_analysis"), dict) else None
    if multiview:
        views = multiview.get("views") if isinstance(multiview.get("views"), list) else []
        for view in views:
            if isinstance(view, dict) and view.get("diff_image_path"):
                remapped = _remap_moved_run_path(view.get("diff_image_path"), iter_dir)
                return to_image_url(str(remapped or ""), config)
    diff_analysis = decision_payload.get("diff_analysis") if isinstance(decision_payload.get("diff_analysis"), dict) else None
    if diff_analysis:
        return to_image_url(diff_analysis.get("diff_image_path", ""), config)
    return None


def _collect_iteration_images(diff_analysis: Any, config: LoaderConfig) -> dict[str, str | None]:
    if not isinstance(diff_analysis, dict):
        return {"reference": None, "candidate": None, "diff": None}
    return {
        "reference": to_image_url(diff_analysis.get("reference_path", ""), config),
        "candidate": to_image_url(diff_analysis.get("candidate_path", ""), config),
        "diff": to_image_url(diff_analysis.get("diff_image_path", ""), config),
    }


def _collect_multiview_images(iter_dir: Path, decision: Any, config: LoaderConfig) -> list[dict[str, Any]]:
    if not isinstance(decision, dict):
        return []
    pairs = decision.get("input_pairs")
    multiview = decision.get("multiview_analysis") if isinstance(decision.get("multiview_analysis"), dict) else {}
    views = multiview.get("views") if isinstance(multiview.get("views"), list) else []
    view_by_index: dict[int, dict[str, Any]] = {}
    for view in views:
        if not isinstance(view, dict):
            continue
        pair_index = view.get("pair_index")
        if isinstance(pair_index, int):
            view_by_index[pair_index] = view
    if not isinstance(pairs, list):
        pairs = []
    if not pairs and views:
        pairs = [{} for _ in views]
    if not pairs:
        return []
    rows: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            continue
        view_payload = view_by_index.get(index, views[index] if index < len(views) and isinstance(views[index], dict) else {})
        pair_analysis = _read_json(iter_dir / "image_analysis" / f"pair_{index:02d}" / "diff_analysis.json")
        view_id = str(
            view_payload.get("view_id")
            or pair.get("view_id")
            or _extract_view_id(str(pair.get("reference") or view_payload.get("reference") or ""))
            or f"view_{index:03d}"
        )
        reference_path = _remap_moved_run_path(
            (
            pair_analysis.get("reference_path")
            if isinstance(pair_analysis, dict) and pair_analysis.get("reference_path")
            else view_payload.get("reference") or pair.get("reference")
            ),
            iter_dir,
        )
        candidate_path = _remap_moved_run_path(
            (
            pair_analysis.get("candidate_path")
            if isinstance(pair_analysis, dict) and pair_analysis.get("candidate_path")
            else view_payload.get("candidate") or pair.get("candidate")
            ),
            iter_dir,
        )
        diff_path = _remap_moved_run_path(
            (
            pair_analysis.get("diff_image_path")
            if isinstance(pair_analysis, dict) and pair_analysis.get("diff_image_path")
            else view_payload.get("diff_image_path")
            ),
            iter_dir,
        )
        rows.append(
            {
                "pair_index": index,
                "view_id": view_id,
                "reference": to_image_url(str(reference_path or ""), config),
                "candidate": to_image_url(str(candidate_path or ""), config),
                "diff": to_image_url(str(diff_path or ""), config),
                "fit_score": view_payload.get("fit_score") if isinstance(view_payload.get("fit_score"), (int, float)) else (
                    pair_analysis.get("human_accept_score") if isinstance(pair_analysis, dict) else None
                ),
                "diff_score": view_payload.get("diff_score") if isinstance(view_payload.get("diff_score"), (int, float)) else (
                    pair_analysis.get("score") if isinstance(pair_analysis, dict) else None
                ),
                "research_score": view_payload.get("research_score") if isinstance(view_payload.get("research_score"), (int, float)) else (
                    pair_analysis.get("research_metrics", {}).get("score")
                    if isinstance(pair_analysis, dict) and isinstance(pair_analysis.get("research_metrics"), dict)
                    else None
                ),
                "research_loss": view_payload.get("research_loss") if isinstance(view_payload.get("research_loss"), (int, float)) else (
                    pair_analysis.get("research_metrics", {}).get("loss")
                    if isinstance(pair_analysis, dict) and isinstance(pair_analysis.get("research_metrics"), dict)
                    else None
                ),
                "research_valid": view_payload.get("research_valid") if isinstance(view_payload.get("research_valid"), bool) else (
                    pair_analysis.get("research_metrics", {}).get("validity", {}).get("passed")
                    if isinstance(pair_analysis, dict)
                    and isinstance(pair_analysis.get("research_metrics"), dict)
                    and isinstance(pair_analysis.get("research_metrics", {}).get("validity"), dict)
                    else None
                ),
                "analysis_path": str(
                    _remap_moved_run_path(
                        view_payload.get("analysis_path") or (iter_dir / "image_analysis" / f"pair_{index:02d}" / "diff_analysis.json"),
                        iter_dir,
                    )
                ),
            }
        )
    return rows


def _extract_view_id(value: str) -> str | None:
    match = re.search(r"(v\d{3}_yaw-?\d+(?:\.\d+)?_pitch-?\d+(?:\.\d+)?)", value)
    return match.group(1) if match else None


def _remap_moved_run_path(value: Any, iter_dir: Path) -> Any:
    """Map legacy absolute run paths after a run is moved under jobs/<job>/runs."""

    if not value:
        return value
    raw = Path(str(value))
    if raw.exists():
        return value
    parts = list(raw.parts)
    if "auto_adjust" not in parts:
        return value
    try:
        auto_index = parts.index("auto_adjust")
        relative = Path(*parts[auto_index:])
        run_dir = iter_dir.parents[1]
        candidate = run_dir / relative
    except (IndexError, ValueError):
        return value
    return str(candidate) if candidate.exists() else value


def _first_lmat_text(candidate_dir: Path) -> str | None:
    if not candidate_dir.is_dir():
        return None
    for path in sorted(candidate_dir.iterdir()):
        if path.suffix.lower() == ".lmat" and path.is_file():
            try:
                return path.read_text(encoding="utf-8-sig")
            except OSError:
                return None
    return None


def _last_modified_iso(path: Path) -> str | None:
    """Return the most recent mtime under ``path`` as an ISO-8601 string."""

    latest: float = 0.0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                mtime = entry.stat().st_mtime
                if mtime > latest:
                    latest = mtime
    except OSError:
        return None
    if latest <= 0.0:
        try:
            latest = path.stat().st_mtime
        except OSError:
            return None
    return _dt.datetime.fromtimestamp(latest).isoformat(timespec="seconds")


def _to_rel_posix(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _to_image_url_unsafe(path: Path, config: LoaderConfig) -> str | None:
    """Like ``to_image_url`` but without the existence check; useful for non-image files."""

    try:
        resolved = path.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(config.image_root)
    except ValueError:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    try:
        rel = resolved.relative_to(config.project_root).as_posix()
    except ValueError:
        return None
    return f"/api/image?path={rel}"

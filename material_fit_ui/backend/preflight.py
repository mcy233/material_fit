"""UI-side wrapper for ``tools.material_fit.laya.refresh_probe``.

The CLI exposes the same probe via ``--laya-refresh-check``; this
module surfaces it as an HTTP endpoint so the user can verify the
"Laya re-renders on .lmat write" assumption from the UI without
starting an auto-adjust job. That distinction matters for two reasons:

* If the assumption is broken, every fit_score in the auto-adjust loop
  is computed against a stale frame. The user needs to be able to
  validate this in <30s before committing to a multi-minute fit run.
* The probe is destructive-then-restorative. Wrapping it in a job that
  *also* runs the optimizer would conflate two separate failure modes
  (Laya didn't refresh vs. optimizer made a bad proposal). Surfacing
  the probe alone keeps the diagnosis crisp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.material_fit.laya.refresh_probe import (
    ProbeConfig,
    build_probe_options,
    resolve_probe_param,
    run_refresh_probe,
)
from tools.material_fit.laya_capture.editor_bridge import (
    LayaEditorCaptureError,
    trigger_editor_single_view_capture,
)
from .case_loader import LoaderConfig
from .project_store import derive_fit_config, get_project, project_paths


def run_laya_refresh_preflight(
    project_id: str,
    *,
    config: LoaderConfig | None = None,
    probe_param: str | None = "u_BaseColor",
    mean_diff_change_threshold: float | None = None,
    mean_diff_restore_threshold: float | None = None,
) -> dict[str, Any]:
    """Run the magenta-probe preflight against the project's real .lmat
    and persist the verdict at ``output/<project>/preflight/last.json``.

    Raises :class:`ValueError` (→ 400) when the project's inputs are
    incomplete (no laya_material_lmat_path, no capture region, etc.),
    so the UI can prompt the user to fill them in instead of running
    a probe that can't possibly succeed.
    """

    config = config or LoaderConfig()
    project = get_project(project_id, config)
    paths = project_paths(project_id, config)
    inputs = project.get("inputs") or {}
    algo = project.get("algorithm_config") or {}

    laya_material_path = inputs.get("laya_material_lmat_path")
    if not laya_material_path:
        raise ValueError(
            "project inputs must include laya_material_lmat_path before running "
            "the refresh probe"
        )
    laya_material_path = Path(laya_material_path).resolve()
    if not laya_material_path.exists():
        raise FileNotFoundError(f".lmat not found at {laya_material_path}")
    fit_config = derive_fit_config(project_id, config=config)
    laya_shader_params = _read_laya_shader_params(str(inputs.get("laya_shader_path") or ""))
    probe_options = get_laya_probe_options(project_id, config=config)
    requested_probe_param = resolve_probe_param(
        requested=probe_param,
        laya_material_path=laya_material_path,
        laya_shader_params=laya_shader_params,
    )
    editor_capture = fit_config.get("laya_editor_capture")
    if not isinstance(editor_capture, dict):
        editor_capture = {}
        fit_config["laya_editor_capture"] = editor_capture
    # The UI probe must never fall back to desktop-region screenshots.
    # It uses the Laya Editor "selected/specified camera" command path
    # so the captured frame comes directly from Capture Camera.
    editor_capture["enabled"] = True
    # Certify the lightweight material reimport path. Reloading the whole
    # scene can disturb transform state and is intentionally avoided here.
    editor_capture["reload_scene_after_reimport"] = False

    preflight_dir = paths.project_dir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)

    def _capture(step: str) -> Path:
        try:
            result = trigger_editor_single_view_capture(
                config=fit_config,
                project_root=config.project_root,
                output_dir=preflight_dir,
                nonce_prefix=f"preflight-{step}",
                laya_material_path=laya_material_path,
                file_name=f"{step}.png",
            )
        except LayaEditorCaptureError as exc:
            raise RuntimeError(str(exc)) from exc
        screenshots = result.get("screenshots", []) if isinstance(result, dict) else []
        if not screenshots:
            raise RuntimeError(f"Laya editor selected-camera probe produced no screenshot for {step}")
        return Path(str(screenshots[0]))

    rerender_wait_ms = int(algo.get("rerender_wait_ms", 1500))
    probe_cfg = algo.get("laya_refresh_probe") if isinstance(algo.get("laya_refresh_probe"), dict) else {}
    change_threshold = _coerce_threshold(
        mean_diff_change_threshold,
        probe_cfg.get("mean_diff_change_threshold"),
        0.5,
    )
    restore_threshold = _coerce_threshold(
        mean_diff_restore_threshold,
        probe_cfg.get("mean_diff_restore_threshold"),
        2.5,
    )
    probe = run_refresh_probe(
        laya_material_path=laya_material_path,
        capture=_capture,
        config=ProbeConfig(
            probe_param=requested_probe_param,
            rerender_wait_ms=rerender_wait_ms,
            mean_diff_change_threshold=change_threshold,
            mean_diff_restore_threshold=restore_threshold,
        ),
        output_dir=preflight_dir,
        focus=None,
    )
    payload = probe.to_dict()
    payload["capture_method"] = "laya_editor_selected_camera"
    payload["probe_options"] = probe_options
    cert_path = preflight_dir / "refresh_session_cert.json"
    if payload.get("success"):
        cert = _build_refresh_session_cert(
            fit_config=fit_config,
            laya_material_path=laya_material_path,
            probe_payload=payload,
            preflight_dir=preflight_dir,
        )
        cert_path.write_text(_json_dump(cert), encoding="utf-8")
        payload["refresh_session_cert"] = str(cert_path)
    elif cert_path.exists():
        cert_path.unlink()
    # Persist for later inspection.
    last_path = preflight_dir / "last.json"
    last_path.write_text(_json_dump(payload), encoding="utf-8")
    payload["last_path"] = str(last_path)
    return payload


def get_laya_probe_options(
    project_id: str,
    *,
    config: LoaderConfig | None = None,
) -> dict[str, Any]:
    """Return Color-like .lmat params suitable for the Laya refresh probe."""

    config = config or LoaderConfig()
    project = get_project(project_id, config)
    inputs = project.get("inputs") or {}
    laya_material_path = str(inputs.get("laya_material_lmat_path") or "")
    laya_shader_path = str(inputs.get("laya_shader_path") or "")

    shader_params = _read_laya_shader_params(laya_shader_path)
    options = build_probe_options(
        laya_material_path=laya_material_path,
        laya_shader_params=shader_params,
    )
    return {
        **options,
        "laya_shader_path": laya_shader_path,
        "laya_material_lmat_path": laya_material_path,
    }


def get_last_preflight(
    project_id: str,
    *,
    config: LoaderConfig | None = None,
) -> dict[str, Any] | None:
    config = config or LoaderConfig()
    paths = project_paths(project_id, config)
    last_path = paths.project_dir / "preflight" / "last.json"
    if not last_path.exists():
        return None
    try:
        return _json_load(last_path)
    except (OSError, ValueError):
        return None


def _build_refresh_session_cert(
    *,
    fit_config: dict[str, Any],
    laya_material_path: Path,
    probe_payload: dict[str, Any],
    preflight_dir: Path,
) -> dict[str, Any]:
    import datetime as _dt

    editor_capture = fit_config.get("laya_editor_capture") if isinstance(fit_config.get("laya_editor_capture"), dict) else {}
    report_path = preflight_dir / "laya_editor_selected_camera_report.json"
    script_version = ""
    if report_path.exists():
        try:
            report = _json_load(report_path)
            diagnostics = report.get("render_diagnostics") if isinstance(report, dict) else {}
            if isinstance(diagnostics, dict):
                script_version = str(diagnostics.get("script_version") or "")
        except (OSError, ValueError):
            script_version = ""
    refresh_assets = editor_capture.get("refresh_assets") if isinstance(editor_capture.get("refresh_assets"), list) else []
    return {
        "success": True,
        "cert_type": "laya_lmat_reimport_session",
        "verified_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "laya_project": str(editor_capture.get("laya_project") or ""),
        "command_path": str(editor_capture.get("command_path") or ""),
        "lmat_path": str(laya_material_path.resolve()),
        "refresh_assets": [str(item) for item in refresh_assets],
        "reload_scene_after_reimport": False,
        "reimport_only": True,
        "capture_method": probe_payload.get("capture_method"),
        "probe_param": probe_payload.get("probe_param"),
        "probe_value": probe_payload.get("probe_value"),
        "mean_diff_baseline_probe": probe_payload.get("mean_diff_baseline_probe"),
        "mean_diff_baseline_restored": probe_payload.get("mean_diff_baseline_restored"),
        "script_version": script_version,
    }


def _coerce_threshold(*values: Any) -> float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0.0:
            return parsed
    return 0.0


def _json_dump(data: Any) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, indent=2)


def _json_load(path: Path) -> Any:
    import json
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_laya_shader_params(path: str) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        from tools.material_fit.laya.shader_parser import parse_laya_shader, shader_info_to_dict

        return shader_info_to_dict(parse_laya_shader(path)).get("params", [])
    except Exception:
        return []

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


_VIEW_ID_RE = re.compile(r"(v\d{3}_yaw-?\d+(?:\.\d+)?_pitch-?\d+(?:\.\d+)?)")


class LayaEditorCaptureError(RuntimeError):
    """Raised when the Laya editor capture command does not complete."""


def trigger_editor_multiview_capture(
    *,
    config: dict[str, Any],
    project_root: Path,
    iteration_dir: Path,
    iteration: int,
    laya_material_path: Path,
) -> dict[str, Any] | None:
    """Ask the Laya editor extension to reimport changed assets and capture views.

    The Laya side watches ``material_fit_capture_command.json``. Updating the
    nonce is the signal that a new job should run.
    """

    capture_config = config.get("laya_editor_capture")
    if not isinstance(capture_config, dict) or not capture_config.get("enabled"):
        return None

    output_dir = iteration_dir / "laya_multiview"
    return _run_editor_capture_command(
        capture_config=capture_config,
        project_root=project_root,
        output_dir=output_dir,
        nonce=f"auto-adjust-{iteration:04d}-{time.time_ns()}",
        laya_material_path=laya_material_path,
    )


def trigger_editor_single_view_capture(
    *,
    config: dict[str, Any],
    project_root: Path,
    output_dir: Path,
    nonce_prefix: str,
    laya_material_path: Path,
    file_name: str,
    view_id: str = "v000_yaw0_pitch0",
    yaw: float = 0.0,
    pitch: float = 0.0,
) -> dict[str, Any] | None:
    """Run the Laya editor selected-camera capture path for one diagnostic frame."""

    capture_config = config.get("laya_editor_capture")
    if not isinstance(capture_config, dict) or not capture_config.get("enabled"):
        return None

    return _run_editor_capture_command(
        capture_config=capture_config,
        project_root=project_root,
        output_dir=output_dir,
        nonce=f"{nonce_prefix}-{time.time_ns()}",
        laya_material_path=laya_material_path,
        capture_kind="selected_camera",
        output_file_name=file_name,
    )


def _run_editor_capture_command(
    *,
    capture_config: dict[str, Any],
    project_root: Path,
    output_dir: Path,
    nonce: str,
    laya_material_path: Path,
    capture_kind: str = "multiview",
    output_file_name: str | None = None,
    views: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    command_path = _resolve_command_path(capture_config, project_root)
    if not command_path.exists():
        raise LayaEditorCaptureError(f"Laya capture command template not found: {command_path}")

    command = json.loads(command_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    report_name = "laya_editor_selected_camera_report.json" if capture_kind == "selected_camera" else "laya_editor_multiview_report.json"
    report_path = output_dir / report_name
    if report_path.exists():
        report_path.unlink()

    command["enabled"] = True
    command["auto_capture"] = True
    command["nonce"] = nonce
    command["capture_kind"] = capture_kind
    command["output_dir"] = str(output_dir)
    camera_name = str(capture_config.get("camera_name") or "").strip()
    if camera_name:
        command["camera_name"] = camera_name
    target_name = str(capture_config.get("target_name") or "").strip() or "model"
    command["target_name"] = target_name
    command["capture_mode"] = str(capture_config.get("capture_mode") or "rotate_target")
    command["render_backend"] = str(capture_config.get("render_backend") or "draw_scene")
    command["capture_debug_mode"] = str(capture_config.get("capture_debug_mode") or "normal")
    command["alpha_source"] = str(capture_config.get("alpha_source") or "render_alpha")
    command["alpha_from_rgb_threshold"] = float(capture_config.get("alpha_from_rgb_threshold", 1.0) or 1.0)
    command["mask_alpha_mode"] = str(capture_config.get("mask_alpha_mode") or "binary")
    command["mask_alpha_threshold"] = float(capture_config.get("mask_alpha_threshold", 1.0) or 1.0)
    command["render_texture_srgb"] = bool(capture_config.get("render_texture_srgb", True))
    command["zero_transparent_rgb"] = bool(capture_config.get("zero_transparent_rgb", False))
    command["align_target_bounds"] = bool(capture_config.get("align_target_bounds", False))
    command["target_base_yaw"] = float(capture_config.get("target_base_yaw", 0.0) or 0.0)
    command["target_base_pitch"] = float(capture_config.get("target_base_pitch", 0.0) or 0.0)
    if "target_local_z" in capture_config and capture_config.get("target_local_z") is not None:
        command["target_local_z"] = capture_config.get("target_local_z")
    else:
        command.pop("target_local_z", None)
    if output_file_name:
        command["output_file_name"] = output_file_name
    else:
        command.pop("output_file_name", None)
    command["refresh_assets"] = _resolve_refresh_assets(capture_config, command_path, laya_material_path)
    command["reload_scene_after_reimport"] = bool(capture_config.get("reload_scene_after_reimport", False))
    command["refresh_after_reimport_delay_ms"] = int(capture_config.get("refresh_after_reimport_delay_ms", 800))
    if not bool(capture_config.get("allow_camera_overrides", False)):
        command.pop("fov", None)
        command.pop("use_orthographic", None)
        command.pop("orthographic_vertical_size", None)
    if views is not None:
        command["views"] = _resolve_command_views(command, views)
    command.pop("material_patch", None)

    command_path.write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")

    timeout_s = float(capture_config.get("timeout_s", 90))
    poll_interval_s = float(capture_config.get("poll_interval_s", 0.5))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            files = [str(Path(path)) for path in report.get("files", []) if Path(path).exists()]
            if files:
                return {
                    "status": "ok",
                    "command_path": str(command_path),
                    "output_dir": str(output_dir),
                    "report_path": str(report_path),
                    "screenshots": files,
                    "candidate_overrides": _build_candidate_overrides(files),
                    "report": report,
                }
        time.sleep(poll_interval_s)

    raise LayaEditorCaptureError(f"Timed out waiting for Laya editor capture report: {report_path}")


def _resolve_command_views(command: dict[str, Any], views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    template_views = command.get("views")
    first_template = (
        dict(template_views[0])
        if isinstance(template_views, list) and template_views and isinstance(template_views[0], dict)
        else {}
    )
    for view in views:
        view_payload = dict(view)
        use_template = bool(view_payload.pop("__use_template_first_view", False))
        if use_template and first_template:
            merged = dict(first_template)
            if view_payload.get("file_name"):
                merged["file_name"] = view_payload["file_name"]
            merged.setdefault("view_id", view_payload.get("view_id", "v000_yaw0_pitch0"))
            merged.setdefault("yaw", view_payload.get("yaw", 0.0))
            merged.setdefault("pitch", view_payload.get("pitch", 0.0))
            resolved.append(merged)
        else:
            resolved.append(view_payload)
    return resolved


def _resolve_command_path(capture_config: dict[str, Any], project_root: Path) -> Path:
    value = capture_config.get("command_path")
    if value:
        path = Path(str(value))
        return path if path.is_absolute() else project_root / path

    laya_project = capture_config.get("laya_project")
    if laya_project:
        project_path = Path(str(laya_project))
        if not project_path.is_absolute():
            project_path = project_root / project_path
        return project_path / "assets" / "material_fit_capture_command.json"

    raise LayaEditorCaptureError("laya_editor_capture requires command_path or laya_project")


def _resolve_refresh_assets(capture_config: dict[str, Any], command_path: Path, laya_material_path: Path) -> list[str]:
    configured = capture_config.get("refresh_assets")
    if isinstance(configured, list) and configured:
        return [str(item) for item in configured if item]

    project_root = command_path.parent.parent if command_path.parent.name == "assets" else command_path.parent
    assets_root = project_root / "assets"
    try:
        return [laya_material_path.resolve().relative_to(assets_root.resolve()).as_posix()]
    except ValueError:
        return [str(laya_material_path)]


def _derive_target_name(laya_material_path: Path) -> str:
    """Deprecated compatibility shim: Laya scenes now use a fixed target root."""

    return "model"


def _build_candidate_overrides(files: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for file_path in files:
        path = Path(file_path)
        overrides[path.name] = file_path
        overrides[path.stem] = file_path
        match = _VIEW_ID_RE.search(path.stem)
        if match:
            overrides[match.group(1)] = file_path
    return overrides

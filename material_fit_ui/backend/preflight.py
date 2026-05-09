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
    run_refresh_probe,
)
from tools.material_fit.laya.window_focus import (
    FocusTarget,
    focus_laya_window,
)
from tools.material_fit.vision.screen_capture import (
    DEFAULT_PREFIX,
    CaptureAnchor,
    capture_laya_region,
    parse_region,
)

from .case_loader import LoaderConfig
from .project_store import get_project, project_paths


def run_laya_refresh_preflight(
    project_id: str,
    *,
    config: LoaderConfig | None = None,
    probe_param: str = "u_BaseColor",
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

    region_dict = inputs.get("laya_capture_region") or {}
    if not (
        isinstance(region_dict, dict)
        and all(k in region_dict for k in ("x", "y", "width", "height"))
    ):
        raise ValueError(
            "project inputs must include laya_capture_region (x, y, width, height) "
            "before running the refresh probe — otherwise the probe has no viewport "
            "to capture from"
        )
    region = parse_region(
        f"{int(region_dict['x'])},{int(region_dict['y'])},"
        f"{int(region_dict['width'])},{int(region_dict['height'])}"
    )

    capture_dir_value = inputs.get("laya_capture_dir")
    if capture_dir_value:
        capture_dir = Path(capture_dir_value).resolve()
    else:
        capture_dir = (config.image_root / "vision" / "test_image").resolve()
    capture_dir.mkdir(parents=True, exist_ok=True)
    state_file_value = inputs.get("laya_capture_state_file")
    state_file = (
        Path(state_file_value).resolve()
        if state_file_value
        else capture_dir / ".capture_region.json"
    )
    prefix = str(inputs.get("laya_capture_prefix") or DEFAULT_PREFIX)

    preflight_dir = paths.project_dir / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)

    anchor = _build_capture_anchor(inputs)

    def _capture(step: str) -> Path:
        # The probe wants exactly three fixed-name files
        # (``baseline.png``, ``probe.png``, ``restored.png``); writing
        # them through the rolling ``laya_candidate_NN.png`` pool used
        # by the real auto-adjust loop would (a) leak 3 garbage files
        # into ``test_image`` per probe run, and (b) make the UI's
        # cached probe URLs disagree with what's actually on disk.
        # ``output_path`` short-circuits the pool and writes directly
        # to the preflight slot.
        dest = preflight_dir / f"{step}.png"
        result = capture_laya_region(
            region=region,
            reuse_last=False,
            capture_dir=capture_dir,
            state_file=state_file,
            prefix=prefix,
            dry_run=False,
            anchor=anchor,
            output_path=dest,
        )
        return Path(result["output_path"])

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
    focus_callback = _build_focus_callback(inputs)
    probe = run_refresh_probe(
        laya_material_path=laya_material_path,
        capture=_capture,
        config=ProbeConfig(
            probe_param=probe_param,
            rerender_wait_ms=rerender_wait_ms,
            mean_diff_change_threshold=change_threshold,
            mean_diff_restore_threshold=restore_threshold,
        ),
        output_dir=preflight_dir,
        focus=focus_callback,
    )
    payload = probe.to_dict()
    # Persist for later inspection.
    last_path = preflight_dir / "last.json"
    last_path.write_text(_json_dump(payload), encoding="utf-8")
    payload["last_path"] = str(last_path)
    return payload


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


def _build_capture_anchor(inputs: dict[str, Any]) -> CaptureAnchor | None:
    """Construct a :class:`CaptureAnchor` from the project's inputs.

    Returns ``None`` (anchor disabled) unless the user explicitly
    enabled it via ``laya_capture_anchor.enabled = True`` AND we have
    a meaningful width/height (which gets populated when the user
    picks a region with the anchor flow). Returning None makes
    ``capture_laya_region`` fall through to the absolute-region path,
    which is the legacy behavior.
    """
    raw = inputs.get("laya_capture_anchor")
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    width = int(raw.get("width", 0) or 0)
    height = int(raw.get("height", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    window = inputs.get("laya_window") if isinstance(inputs.get("laya_window"), dict) else {}
    return CaptureAnchor(
        enabled=True,
        offset_x=int(raw.get("offset_x", 0) or 0),
        offset_y=int(raw.get("offset_y", 0) or 0),
        width=width,
        height=height,
        process_pattern=str(window.get("process_pattern", "LayaAirIDE")),
        title_pattern=str(window.get("title_pattern", "")),
    )


def _build_focus_callback(inputs: dict[str, Any]):
    """Build a Laya focus callback from the project's ``laya_window`` block.

    Returns ``None`` if the user explicitly disabled focus by setting
    ``process_pattern`` to an empty string. The callback signature
    matches :data:`tools.material_fit.laya.refresh_probe.FocusCallable`.

    Default values: ``process_pattern='LayaAirIDE'`` (covers the
    Windows installer of LayaAirIDE), no title filter (focuses the
    first visible Laya window). Users with multiple Laya projects
    open at once should set ``title_pattern`` to e.g. ``'fish'``
    to disambiguate.
    """
    block = inputs.get("laya_window") or {}
    if not isinstance(block, dict):
        block = {}
    process_pattern = str(block.get("process_pattern", "LayaAirIDE"))
    title_pattern = str(block.get("title_pattern", ""))
    settle_ms = int(block.get("settle_ms", 250))
    if not (process_pattern or title_pattern):
        return None
    target = FocusTarget(process_pattern=process_pattern, title_pattern=title_pattern)

    def _focus(step: str) -> dict[str, Any]:
        result = focus_laya_window(target, settle_ms=settle_ms).to_dict()
        result["step"] = step
        return result

    return _focus


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

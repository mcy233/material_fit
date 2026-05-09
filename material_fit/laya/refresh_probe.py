"""Laya refresh-after-write preflight check.

The auto-adjust loop assumes Laya will re-render whenever we write a
new ``.lmat`` to disk. If that assumption is wrong — e.g. Laya caches
materials and only re-imports on focus, or the file watcher is
disabled, or our ``rerender_wait_ms`` is too short — every captured
screenshot is a stale frame, every fit_score is computed against
yesterday's render, and the optimizer is running blind.

This module runs a 3-step probe:

1. **Baseline capture.** Take a screenshot of the Laya viewport with
   the *current* ``.lmat`` (no modification).
2. **Probe write.** Back up the ``.lmat``, write a probe value
   (default: ``u_BaseColor = magenta``) to it, wait
   ``rerender_wait_ms``, and capture again.
3. **Restore.** Copy the backup back, wait, and capture a third time.

The decision rule:

* The probe image must be **measurably different** from the baseline
  (mean per-pixel L1 distance ≥ ``mean_diff_change_threshold``) →
  proves the Laya renderer noticed the write.
* The restored image must be **back close to the baseline** (mean
  per-pixel L1 distance ≤ ``mean_diff_restore_threshold``) → proves
  the restore actually re-applied the original.
* Both must hold. Either failing means the write/render contract
  is broken and the auto-adjust must abort with a clear reason.

Why mean color difference instead of "magenta pixel ratio"?
-----------------------------------------------------------

The earlier version of this module used a strict "fraction of pixels
matching pure magenta (R,G,B≈(255,0,255))" detector. That works for
unlit / flat materials but fails on textured PBR surfaces: writing
``u_BaseColor=[1,0,1,1]`` modulates the existing texture sample, so a
dark-red base (R=180,G=40,B=70) becomes (R=180,G=0,B=70) — clearly
*different* and visibly more magenta-tinted, but very far from the
strict (255,0,255) cone. Real fish_1580 mecha has ~0.1% magenta
under the strict detector, which trips a 10% threshold.

Mean color diff between baseline and probe is hue-agnostic: it goes
to ~0 when Laya is frozen (literally identical frames) and grows
linearly with the size of the colour shift, no matter the base
material. ``magenta_ratio`` is still computed as a *secondary*
diagnostic — it's useful information for the user — but it no longer
gates the success/fail decision.

Both the write and the restore go through ``lmat_io.write_candidate_lmat``,
which performs a post-write re-read validation; if either step would
have corrupted the ``.lmat`` shape, the probe fails *before* the wait,
and the caller's actual ``.lmat`` is left intact.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import lmat_io


# ---------------------------------------------------------------------
# Public types


@dataclass
class ProbeResult:
    """Outcome of a single :func:`run_refresh_probe` invocation."""

    success: bool
    detected_change: bool          # probe meaningfully differs from baseline
    detected_restore: bool         # restored is close to baseline again
    reason: str                    # human-readable diagnosis

    # Primary signal: mean per-pixel L1 colour distance, in [0, 255].
    # Goes to 0 when frames are identical (Laya frozen) and grows with
    # the size of the visible colour shift. Hue-agnostic.
    mean_diff_baseline_probe: float = 0.0
    mean_diff_baseline_restored: float = 0.0
    mean_diff_probe_restored: float = 0.0

    # Secondary signal: the legacy "magenta pixel fraction" — kept as a
    # supplementary diagnostic. Useful for the user to see *some* pixels
    # really did pick up a magenta cast, even when the probe param's
    # effect is dampened by textures.
    magenta_ratio_baseline: float = 0.0
    magenta_ratio_probe: float = 0.0
    magenta_ratio_restored: float = 0.0

    captures: dict[str, str] = field(default_factory=dict)
    probe_param: str = ""
    probe_value: list[float] = field(default_factory=list)
    rerender_wait_ms: int = 0

    # Thresholds in effect for THIS run, persisted so the UI can
    # surface "value=2.1 threshold=2.5 → just below cutoff" and let
    # the user retry with a looser config.
    mean_diff_change_threshold: float = 0.0
    mean_diff_restore_threshold: float = 0.0
    detection_threshold: float = 0.0  # legacy magenta threshold, kept for the UI
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    focus_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeConfig:
    """Tunable knobs for :func:`run_refresh_probe`."""

    probe_param: str = "u_BaseColor"
    probe_value: tuple[float, float, float, float] = (1.0, 0.0, 1.0, 1.0)

    mean_diff_change_threshold: float = 0.5
    """Minimum mean per-pixel L1 colour distance (in [0, 255]) between
    baseline and probe captures, for the probe to be considered
    'visibly different' from the baseline.

    Tuning: 0 means any non-identical frame counts as a refresh. The
    default 0.5 only filters out tiny capture noise while still accepting
    weak material changes diluted by full-frame background pixels."""

    mean_diff_restore_threshold: float = 2.5
    """Maximum mean per-pixel L1 colour distance (in [0, 255]) between
    baseline and restored captures, for the restore to be considered
    'visibly back to original'.

    Tuning: should be ≥ the noise floor (~1.0) but tight enough to
    flag a still-modified .lmat. Asymmetric vs the change threshold
    on purpose — restoration drift is more dangerous than slow
    detection."""

    detection_threshold: float = 0.10
    """**Legacy** — the old strict-magenta threshold. Kept ONLY so
    callers reading the field still get a sensible default; it no
    longer gates success. Use ``mean_diff_change_threshold`` instead."""

    rerender_wait_ms: int = 1500
    """Sleep duration between writing the .lmat and capturing the
    next frame. Should match the user's ``algorithm_config.rerender_wait_ms``
    so the probe tests the real-pipeline assumption, not a softer one."""

    magenta_r_min: float = 0.6
    magenta_r_max: float = 1.05
    magenta_g_max: float = 0.45
    magenta_b_min: float = 0.6
    magenta_b_max: float = 1.05


CaptureCallable = Callable[[str], Path]
"""A capture function: takes a logical step name (``"baseline"``,
``"probe"``, ``"restored"``) and returns the absolute path to the PNG
the Laya viewport just produced. The probe NEVER drives screen capture
itself — it must be injected so the same call site that runs the real
auto-adjust loop also runs the preflight (otherwise we'd be probing
Laya's behaviour with a different capture path than the one the loop
actually uses, and a passing probe would prove nothing)."""


FocusCallable = Callable[[str], dict[str, Any]]
"""Optional hook called before each .lmat write and each capture.

The argument is a logical step name (``"before_baseline_capture"``,
``"before_probe_write"``, ``"before_probe_capture"``,
``"before_restore_write"``, ``"before_restored_capture"``).
The return dict is recorded into :attr:`ProbeResult.focus_log` for
diagnosis — the typical implementation is :func:`window_focus.focus_laya_window`
which returns ``{"success": bool, "reason": str, "title": str, ...}``.

The hook is called *before* every step that requires Laya to be
visibly responsive (any .lmat write — because Laya only re-renders
when focused — and any capture). If ``None``, no focus is attempted
and the user is responsible for keeping Laya in the foreground."""


# ---------------------------------------------------------------------
# Public API


def run_refresh_probe(
    *,
    laya_material_path: str | Path,
    capture: CaptureCallable,
    config: ProbeConfig | None = None,
    output_dir: str | Path | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    focus: FocusCallable | None = None,
    laya_shader_params: Sequence[Any] | None = None,  # legacy / ignored
) -> ProbeResult:
    """Run the magenta-probe preflight against a real ``.lmat``.

    The function is *transactional* in the sense that it always
    attempts to restore the original ``.lmat`` from a backup, even when
    intermediate steps fail. The backup file is left in place
    (``<lmat>.refresh_probe.bak``) for forensic inspection if anything
    went wrong; remove it manually when satisfied.

    ``laya_shader_params`` is accepted for backward compatibility with
    earlier callers that passed it positionally; the probe does not
    actually need it because it derives the existing param dict from
    the ``.lmat`` itself via :func:`lmat_io.extract_params`. Future
    callers should not pass it.

    Returns a :class:`ProbeResult`. ``success=True`` is the only state
    where the calling code should proceed to a real auto-adjust run.
    """

    config = config or ProbeConfig()
    del laya_shader_params  # accepted for compat, not used
    laya_material_path = Path(laya_material_path).resolve()
    output_dir_path = Path(output_dir).resolve() if output_dir else laya_material_path.parent

    if not laya_material_path.exists():
        return _failure(
            reason=f".lmat not found at {laya_material_path}",
            config=config,
        )

    try:
        original_data = lmat_io.load_lmat(laya_material_path)
        original_params = lmat_io.extract_params(original_data)
    except (OSError, json.JSONDecodeError) as exc:
        return _failure(reason=f"failed to load .lmat: {exc}", config=config, error=str(exc))

    if config.probe_param not in original_params:
        return _failure(
            reason=(
                f"probe param {config.probe_param!r} is not present in the .lmat's "
                f"props ({sorted(original_params.keys())[:6]}...). Set "
                f"ProbeConfig.probe_param to a Color uniform that does exist."
            ),
            config=config,
        )

    notes: list[str] = []
    focus_log: list[dict[str, Any]] = []
    backup_path: Path | None = None
    captures: dict[str, str] = {}

    def _focus(step: str) -> None:
        if focus is None:
            return
        try:
            entry = dict(focus(step))
        except Exception as exc:  # noqa: BLE001
            entry = {"success": False, "reason": f"focus hook raised: {exc}", "step": step}
        entry.setdefault("step", step)
        focus_log.append(entry)

    try:
        # Step 0: back up the .lmat byte-for-byte before *any* write.
        backup_path = lmat_io.backup_lmat(laya_material_path, suffix=".refresh_probe.bak")
        notes.append(f"backup written to {backup_path}")

        # Step 1: baseline capture (no modification).
        _focus("before_baseline_capture")
        baseline_path = _capture_step(capture, "baseline")
        captures["baseline"] = str(baseline_path)
        baseline_magenta = magenta_ratio(baseline_path, config)

        # Step 2: write probe color → wait → capture.
        probe_params = dict(original_params)
        probe_params[config.probe_param] = list(config.probe_value)
        # Focus BEFORE the write so Laya is the active renderer when its
        # file watcher fires (Laya throttles when in background).
        _focus("before_probe_write")
        try:
            lmat_io.write_candidate_lmat(
                laya_material_path,
                laya_material_path,
                probe_params,
                allow_missing_keys=True,
            )
        except lmat_io.LmatWriteError as exc:
            # The candidate write performed a self-validating round trip
            # and rolled back; the user's .lmat is intact, but the probe
            # cannot proceed.
            return _failure(
                reason=(
                    f"refused to write probe value because lmat_io rejected "
                    f"the resulting payload (this is the same guard that "
                    f"protects you in the real auto-adjust loop): {exc}"
                ),
                config=config,
                captures=captures,
                notes=notes,
                error=str(exc),
                focus_log=focus_log,
            )

        sleep_fn(max(config.rerender_wait_ms, 0) / 1000.0)
        _focus("before_probe_capture")
        probe_capture_path = _capture_step(capture, "probe")
        captures["probe"] = str(probe_capture_path)
        probe_magenta = magenta_ratio(probe_capture_path, config)

        # Step 3: restore byte-for-byte from backup → wait → capture.
        _focus("before_restore_write")
        shutil.copyfile(backup_path, laya_material_path)
        sleep_fn(max(config.rerender_wait_ms, 0) / 1000.0)
        _focus("before_restored_capture")
        restored_capture_path = _capture_step(capture, "restored")
        captures["restored"] = str(restored_capture_path)
        restored_magenta = magenta_ratio(restored_capture_path, config)

        # Compute the primary mean-color-diff signals.
        diff_bp = mean_color_diff(baseline_path, probe_capture_path)
        diff_br = mean_color_diff(baseline_path, restored_capture_path)
        diff_pr = mean_color_diff(probe_capture_path, restored_capture_path)

    except Exception as exc:  # noqa: BLE001
        # Best-effort restore.
        if backup_path is not None and backup_path.exists():
            try:
                shutil.copyfile(backup_path, laya_material_path)
                notes.append("restored .lmat from backup after exception")
            except OSError as restore_exc:
                notes.append(f"FAILED to restore .lmat: {restore_exc}")
        return _failure(
            reason=f"probe pipeline raised: {type(exc).__name__}: {exc}",
            config=config,
            captures=captures,
            notes=notes,
            error=str(exc),
            focus_log=focus_log,
        )

    # Decision logic — primary signal is mean per-pixel color difference.
    # Hue-agnostic, robust to textured/PBR materials where strict
    # "fraction of pure-magenta pixels" stays near zero even when Laya
    # clearly refreshed.
    detected_change = diff_bp > config.mean_diff_change_threshold
    detected_restore = diff_br <= config.mean_diff_restore_threshold
    success = detected_change and detected_restore

    # Frozen-Laya detection: all three captures are essentially identical
    # (within the capture noise floor). Keep this independent from the
    # user-configurable pass threshold; otherwise a weak-but-real probe
    # signal gets mislabeled as "not refreshing".
    noise_floor = 0.5
    frozen = diff_bp < noise_floor and diff_pr < noise_floor and diff_br < noise_floor

    if success:
        reason = (
            f"Laya did refresh: mean color diff baseline→probe = {diff_bp:.2f} "
            f"(> {config.mean_diff_change_threshold:.2f}), and restored is back "
            f"close to baseline (diff = {diff_br:.2f}, ≤ {config.mean_diff_restore_threshold:.2f}). "
            f"Magenta-ratio side-signal: {baseline_magenta:.2%} → {probe_magenta:.2%} "
            f"→ {restored_magenta:.2%}."
        )
    elif frozen:
        reason = (
            f"Laya is NOT refreshing on .lmat write. All three captures look "
            f"essentially identical (mean diff baseline→probe = {diff_bp:.2f}, "
            f"probe→restored = {diff_pr:.2f}, baseline→restored = {diff_br:.2f}; "
            f"all below the {noise_floor:.2f} noise floor). The optimizer would "
            f"run blind. Common causes: Laya editor not focused (E-007 — check "
            f"the focus log below), rerender_wait_ms too short, or .lmat path "
            f"doesn't match the material Laya is actually rendering."
        )
    elif not detected_change:
        reason = (
            f"Probe write did not produce enough color difference "
            f"(mean diff baseline→probe = {diff_bp:.2f}, required > "
            f"{config.mean_diff_change_threshold:.2f}). Set "
            f"mean_diff_change_threshold to 0 if any non-zero difference "
            f"should count as a refresh. baseline→restored diff was {diff_br:.2f}."
        )
    else:
        reason = (
            f"Probe was visible (mean diff baseline→probe = {diff_bp:.2f}) "
            f"but restored is still different from baseline "
            f"(mean diff baseline→restored = {diff_br:.2f}, expected "
            f"≤ {config.mean_diff_restore_threshold:.2f}). The .lmat is now "
            f"in an unknown state — check the backup at {backup_path}."
        )

    return ProbeResult(
        success=success,
        detected_change=detected_change,
        detected_restore=detected_restore,
        reason=reason,
        mean_diff_baseline_probe=diff_bp,
        mean_diff_baseline_restored=diff_br,
        mean_diff_probe_restored=diff_pr,
        magenta_ratio_baseline=baseline_magenta,
        magenta_ratio_probe=probe_magenta,
        magenta_ratio_restored=restored_magenta,
        captures=captures,
        probe_param=config.probe_param,
        probe_value=list(config.probe_value),
        rerender_wait_ms=config.rerender_wait_ms,
        mean_diff_change_threshold=config.mean_diff_change_threshold,
        mean_diff_restore_threshold=config.mean_diff_restore_threshold,
        detection_threshold=config.detection_threshold,
        notes=notes,
        focus_log=focus_log,
    )


# ---------------------------------------------------------------------
# Image analysis


def mean_color_diff(path_a: str | Path, path_b: str | Path) -> float:
    """Mean per-pixel L1 color distance between two PNGs, in [0, 255].

    Used as the primary "did Laya refresh?" signal because it is
    hue-agnostic: it goes to 0 iff the two captured frames are
    pixel-identical (Laya frozen) and grows linearly with the size of
    the visible color shift, no matter what the base material looks
    like.

    Returns 0.0 for unreadable / missing / mismatched-size pairs —
    the caller decides how to interpret that (in the probe, a 0.0 on
    baseline-vs-probe is treated as "Laya did not refresh", which is
    the conservative interpretation).
    """
    try:
        from PIL import Image
    except ImportError:
        return 0.0

    try:
        with Image.open(path_a) as img_a:
            a = img_a.convert("RGB")
            ax, ay = a.size
            a_bytes = a.tobytes()
        with Image.open(path_b) as img_b:
            b = img_b.convert("RGB")
            bx, by = b.size
            b_bytes = b.tobytes()
    except (OSError, ValueError, FileNotFoundError):
        return 0.0

    if (ax, ay) != (bx, by):
        # Capture sizes drifted between calls (e.g. window resize during
        # the probe). We could try to scale-and-compare, but in practice
        # the user wants to know about this; bail with 0.0 so the probe
        # reports "no change detected" and the user re-picks the region.
        return 0.0
    if not a_bytes or not b_bytes:
        return 0.0

    # Use numpy if available — much faster on large captures (1080p
    # is ~6M pixels, the python loop would take seconds).
    try:
        import numpy as np
        arr_a = np.frombuffer(a_bytes, dtype=np.uint8)
        arr_b = np.frombuffer(b_bytes, dtype=np.uint8)
        # Cast to int16 to allow signed subtraction without underflow.
        return float(np.mean(np.abs(arr_a.astype(np.int16) - arr_b.astype(np.int16))))
    except ImportError:
        total = 0
        for byte_a, byte_b in zip(a_bytes, b_bytes):
            total += abs(byte_a - byte_b)
        return total / max(len(a_bytes), 1)


def magenta_ratio(image_path: str | Path, config: ProbeConfig | None = None) -> float:
    """Return the fraction of pixels in ``image_path`` that look magenta.

    Magenta is defined as ``R ∈ [0.6, 1.05] ∧ G ≤ 0.45 ∧ B ∈ [0.6, 1.05]``
    (config is overridable). Uses PIL because the rest of the pipeline
    already depends on it. Returns 0.0 for unreadable images rather
    than raising — the caller decides how to interpret a 0.0.
    """
    config = config or ProbeConfig()
    try:
        from PIL import Image
    except ImportError:
        return 0.0

    try:
        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
            pixels = list(rgb.getdata())
    except (OSError, ValueError):
        return 0.0

    if not pixels:
        return 0.0

    r_lo = config.magenta_r_min * 255
    r_hi = config.magenta_r_max * 255
    g_hi = config.magenta_g_max * 255
    b_lo = config.magenta_b_min * 255
    b_hi = config.magenta_b_max * 255

    matched = 0
    for r, g, b in pixels:
        if r_lo <= r <= r_hi and g <= g_hi and b_lo <= b <= b_hi:
            matched += 1
    return matched / len(pixels)


# ---------------------------------------------------------------------
# helpers


def _capture_step(capture: CaptureCallable, step: str) -> Path:
    path = Path(capture(step)).resolve()
    if not path.exists():
        raise FileNotFoundError(f"capture step {step!r} did not produce a file at {path}")
    return path


def _failure(
    *,
    reason: str,
    config: ProbeConfig,
    captures: dict[str, str] | None = None,
    notes: Sequence[str] = (),
    error: str | None = None,
    focus_log: Sequence[dict[str, Any]] = (),
) -> ProbeResult:
    return ProbeResult(
        success=False,
        detected_change=False,
        detected_restore=False,
        reason=reason,
        mean_diff_baseline_probe=0.0,
        mean_diff_baseline_restored=0.0,
        mean_diff_probe_restored=0.0,
        magenta_ratio_baseline=0.0,
        magenta_ratio_probe=0.0,
        magenta_ratio_restored=0.0,
        captures=dict(captures or {}),
        probe_param=config.probe_param,
        probe_value=list(config.probe_value),
        rerender_wait_ms=config.rerender_wait_ms,
        mean_diff_change_threshold=config.mean_diff_change_threshold,
        mean_diff_restore_threshold=config.mean_diff_restore_threshold,
        detection_threshold=config.detection_threshold,
        notes=list(notes),
        error=error,
        focus_log=list(focus_log),
    )


__all__ = [
    "CaptureCallable",
    "FocusCallable",
    "ProbeConfig",
    "ProbeResult",
    "magenta_ratio",
    "mean_color_diff",
    "run_refresh_probe",
]

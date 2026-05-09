"""Perceptual scoring helpers for cross-engine material fitting.

This module collects three independent improvements over the legacy
"global RGB MAE" score that are introduced in experiment **E-009**:

1. :func:`auto_background_mask`
   Detects the dominant editor / sky background in both reference and
   candidate images by clustering corner pixels, and returns a mask
   that excludes those pixels from any per-pixel comparison.
   This is the single biggest correctness fix: in our fish_1580
   case, ~70% of every Laya screenshot is the editor's blue-grey
   background plate, and Unity's reference uses a different
   off-white plate. Comparing those plates pixel-wise generated
   tens of percent of pure noise that dominated the global MAE.

2. :func:`channel_weighted_mae`
   Instead of treating every pixel equally, we weight per-region
   ``rgb_mae`` figures (already produced by
   :mod:`tools.material_fit.vision.diff_analysis`) by a configurable
   prior that reflects how strongly each material channel drives
   human perception. Defaults are chosen so that small but
   eye-catching regions (emission, specular highlights, fresnel rim)
   are not drowned out by the much larger mid-tone region.

3. :func:`ssim_score`
   Computes a structural-similarity index using
   :func:`skimage.metrics.structural_similarity`. SSIM is locally
   windowed and therefore tolerant to ~1-pixel positional jitter,
   which is the most common confound in screen-captured Laya
   frames (the model sometimes renders 1 px shifted when the
   editor regains focus).

The three signals are combined into a single
:class:`PerceptualScoreResult` so the optimizer / UI can consume
them with one call site. They are deliberately decoupled so the
implementation in :mod:`diff_analysis` can be modernised without
breaking callers.

This module pairs with the dedicated paper-grade write-up at
``tools/material_fit/docs/Metric_Validation.md``. Any change to the
default weights, thresholds, or formulas should be reflected there
as well so the experimental record stays in sync.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# Third-party deps are all optional at import time so the rest of the
# pipeline still loads on machines that haven't installed Pillow /
# numpy / scikit-image.

try:  # pragma: no cover - exercised by environment, not unit tests
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:  # pragma: no cover
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


__all__ = [
    "AutoMaskConfig",
    "AutoMaskResult",
    "ChannelWeightConfig",
    "DEFAULT_CHANNEL_WEIGHTS",
    "PerceptualScoreResult",
    "auto_background_mask",
    "channel_weighted_mae",
    "combine_fit_score",
    "perceptual_score_from_analysis",
    "ssim_score",
]


# ---------------------------------------------------------------------
# Auto background mask
# ---------------------------------------------------------------------


@dataclass
class AutoMaskConfig:
    """Configuration for :func:`auto_background_mask`.

    Both ``reference`` and ``candidate`` are sampled at the four
    corners with ``corner_size`` x ``corner_size`` patches; the
    median of those samples is treated as that image's "background
    plate" colour. Any pixel within ``threshold`` (max channel L∞
    distance, in 0-255 ints) of either plate is considered
    background and excluded.

    The combined-image rule is conservative: a pixel is foreground
    only when **both** images consider it foreground. This is correct
    for our use case because if either side is "obviously
    background", the comparison there carries no material signal.
    """

    corner_size: int = 12
    threshold: int = 16
    min_foreground_ratio: float = 0.05  # if foreground < this, fall back to no-mask
    enabled: bool = True


@dataclass
class AutoMaskResult:
    """Outcome of :func:`auto_background_mask`."""

    status: str  # "ok", "skipped", "low_signal", "unavailable"
    mask: Any = None  # np.ndarray of shape (H, W) float32 in [0, 1], or None
    reference_bg_color: tuple[int, int, int] | None = None
    candidate_bg_color: tuple[int, int, int] | None = None
    reference_bg_ratio: float = 0.0
    candidate_bg_ratio: float = 0.0
    foreground_ratio: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reference_bg_color": list(self.reference_bg_color) if self.reference_bg_color else None,
            "candidate_bg_color": list(self.candidate_bg_color) if self.candidate_bg_color else None,
            "reference_bg_ratio": self.reference_bg_ratio,
            "candidate_bg_ratio": self.candidate_bg_ratio,
            "foreground_ratio": self.foreground_ratio,
            "notes": list(self.notes),
        }


def _detect_bg_color(arr: Any, corner_size: int) -> tuple[tuple[int, int, int], float, int]:
    """Return ``((r, g, b), bg_pixel_ratio, threshold_used)`` for a single image.

    The background is estimated as the median colour over the four
    corner patches. The ratio is the fraction of pixels within
    ``threshold`` of that median (using max-channel L∞ distance).
    The threshold is fixed by the caller; we still report it so the
    caller can attach it to diagnostics.
    """

    h, w = arr.shape[:2]
    cs = max(1, min(corner_size, min(h, w) // 4))
    corners = [
        arr[0:cs, 0:cs],
        arr[0:cs, w - cs : w],
        arr[h - cs : h, 0:cs],
        arr[h - cs : h, w - cs : w],
    ]
    stacked = np.concatenate([c.reshape(-1, 3) for c in corners], axis=0)
    median = np.median(stacked, axis=0)
    bg_color = (int(median[0]), int(median[1]), int(median[2]))
    return bg_color, 0.0, 0  # ratio filled in later by caller


def auto_background_mask(
    reference: Any,
    candidate: Any,
    config: AutoMaskConfig | None = None,
) -> AutoMaskResult:
    """Build a foreground mask by detecting each image's background plate.

    ``reference`` and ``candidate`` may be ``PIL.Image`` instances or
    numpy arrays of shape ``(H, W, 3)``. They must already be the
    same size; callers are responsible for resizing.

    Returns:
        :class:`AutoMaskResult`. When numpy / PIL is missing the
        result has ``status="unavailable"`` and ``mask=None`` so the
        caller can fall back to weight=1 everywhere.
    """

    cfg = config or AutoMaskConfig()
    if not cfg.enabled:
        return AutoMaskResult(status="skipped", notes=["auto_mask disabled by config"])
    if np is None:
        return AutoMaskResult(status="unavailable", notes=["numpy not installed"])

    ref_arr = _to_rgb_array(reference)
    cand_arr = _to_rgb_array(candidate)
    if ref_arr is None or cand_arr is None:
        return AutoMaskResult(status="unavailable", notes=["could not convert images to RGB array"])
    if ref_arr.shape != cand_arr.shape:
        return AutoMaskResult(
            status="unavailable",
            notes=[f"shape mismatch: ref={ref_arr.shape}, cand={cand_arr.shape}"],
        )

    h, w = ref_arr.shape[:2]
    ref_bg, _, _ = _detect_bg_color(ref_arr, cfg.corner_size)
    cand_bg, _, _ = _detect_bg_color(cand_arr, cfg.corner_size)

    ref_diff = np.abs(ref_arr.astype(np.int32) - np.array(ref_bg, dtype=np.int32)).max(axis=2)
    cand_diff = np.abs(cand_arr.astype(np.int32) - np.array(cand_bg, dtype=np.int32)).max(axis=2)

    ref_bg_mask = ref_diff <= cfg.threshold
    cand_bg_mask = cand_diff <= cfg.threshold

    ref_bg_ratio = float(ref_bg_mask.sum()) / float(h * w)
    cand_bg_ratio = float(cand_bg_mask.sum()) / float(h * w)

    foreground = ~(ref_bg_mask | cand_bg_mask)
    foreground_ratio = float(foreground.sum()) / float(h * w)

    notes: list[str] = []
    if foreground_ratio < cfg.min_foreground_ratio:
        notes.append(
            f"foreground ratio {foreground_ratio:.3f} below "
            f"min_foreground_ratio {cfg.min_foreground_ratio:.3f}; falling back to no mask"
        )
        return AutoMaskResult(
            status="low_signal",
            mask=None,
            reference_bg_color=ref_bg,
            candidate_bg_color=cand_bg,
            reference_bg_ratio=ref_bg_ratio,
            candidate_bg_ratio=cand_bg_ratio,
            foreground_ratio=foreground_ratio,
            notes=notes,
        )

    mask = foreground.astype(np.float32)
    return AutoMaskResult(
        status="ok",
        mask=mask,
        reference_bg_color=ref_bg,
        candidate_bg_color=cand_bg,
        reference_bg_ratio=ref_bg_ratio,
        candidate_bg_ratio=cand_bg_ratio,
        foreground_ratio=foreground_ratio,
        notes=notes,
    )


def _to_rgb_array(image: Any) -> Any:
    if np is None:
        return None
    if hasattr(image, "convert") and hasattr(image, "size"):
        # PIL.Image
        rgb = image.convert("RGB")
        return np.array(rgb, dtype=np.uint8)
    if hasattr(image, "shape"):
        arr = image
        if arr.ndim == 3 and arr.shape[2] >= 3:
            return arr[:, :, :3].astype(np.uint8)
    return None


# ---------------------------------------------------------------------
# Channel-weighted MAE
# ---------------------------------------------------------------------


# These weights are calibrated for "stylized PBR character + props"
# (which fish_1580 belongs to). The intuition: emission and specular
# regions are tiny in pixel count but enormous in perceptual weight,
# so they get >2x the weight per pixel they occupy.
#
# The weights MUST sum to 1.0 (asserted in :class:`ChannelWeightConfig`).
DEFAULT_CHANNEL_WEIGHTS: dict[str, float] = {
    "base_color_main_texture": 0.30,
    "metallic_smoothness_specular": 0.18,
    "emission": 0.12,
    "fresnel_rim": 0.12,
    "shadow_occlusion": 0.10,
    "color_grading_hsv_contrast": 0.18,
}


@dataclass
class ChannelWeightConfig:
    """Configuration for channel-weighted MAE."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CHANNEL_WEIGHTS))

    def __post_init__(self) -> None:
        total = sum(self.weights.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"channel weights must sum to 1.0; got {total:.6f}")
        for name, value in self.weights.items():
            if value < 0.0:
                raise ValueError(f"channel weight for {name} must be non-negative; got {value}")


def channel_weighted_mae(
    material_channels: dict[str, Any],
    config: ChannelWeightConfig | None = None,
) -> dict[str, Any]:
    """Compute a weighted average of per-channel ``rgb_mae`` values.

    ``material_channels`` is the dict produced by
    :mod:`diff_analysis._build_material_channel_diagnostics`. Channels
    that are missing or have ``valid=False`` are skipped, and their
    weights are renormalised across the remaining ones. This makes
    the metric robust to images that genuinely have no emission
    region etc.

    Returns a dict with keys: ``weighted_mae``, ``weights_used``,
    ``contributions`` (per-channel weighted mae × weight),
    ``coverage`` (fraction of nominal weight that ended up valid).
    """

    cfg = config or ChannelWeightConfig()
    raw_weights = cfg.weights

    contributions: dict[str, float] = {}
    weights_used: dict[str, float] = {}
    valid_total_weight = 0.0
    for name, weight in raw_weights.items():
        ch = material_channels.get(name)
        if not isinstance(ch, dict) or not ch.get("valid"):
            continue
        valid_total_weight += weight
        weights_used[name] = weight

    if valid_total_weight <= 0.0:
        return {
            "weighted_mae": math.inf,
            "weights_used": {},
            "contributions": {},
            "coverage": 0.0,
        }

    renorm = 1.0 / valid_total_weight
    weighted_mae = 0.0
    for name, weight in weights_used.items():
        rgb_mae = float(material_channels[name].get("rgb_mae", 0.0))
        scaled_weight = weight * renorm
        weights_used[name] = scaled_weight
        contribution = rgb_mae * scaled_weight
        contributions[name] = contribution
        weighted_mae += contribution

    return {
        "weighted_mae": weighted_mae,
        "weights_used": weights_used,
        "contributions": contributions,
        "coverage": valid_total_weight,  # before renorm; tells us how much of nominal we used
    }


# ---------------------------------------------------------------------
# SSIM
# ---------------------------------------------------------------------


def ssim_score(
    reference: Any,
    candidate: Any,
    *,
    mask: Any = None,
    win_size: int | None = None,
) -> dict[str, Any]:
    """Compute structural-similarity index for two same-size images.

    When ``mask`` is provided (an ``H x W`` float array in [0, 1]),
    the SSIM map is averaged with that weighting; otherwise the
    standard mean over the full image is used.

    Returns a dict with keys: ``status`` (``"ok"`` / ``"unavailable"``),
    ``ssim`` (in [-1, 1] but typically [0, 1]), ``win_size``, ``notes``.
    Falls back to ``status="unavailable"`` if numpy / scikit-image is
    not installed; never raises.
    """

    if np is None:
        return {"status": "unavailable", "ssim": None, "notes": ["numpy not installed"]}
    try:
        from skimage.metrics import structural_similarity
    except ImportError:
        return {"status": "unavailable", "ssim": None, "notes": ["scikit-image not installed"]}

    ref_arr = _to_rgb_array(reference)
    cand_arr = _to_rgb_array(candidate)
    if ref_arr is None or cand_arr is None:
        return {"status": "unavailable", "ssim": None, "notes": ["could not convert images"]}
    if ref_arr.shape != cand_arr.shape:
        return {"status": "unavailable", "ssim": None, "notes": [f"shape mismatch {ref_arr.shape} vs {cand_arr.shape}"]}

    h, w = ref_arr.shape[:2]
    # SSIM requires odd win_size <= min(H, W).
    win = win_size if win_size and win_size % 2 == 1 else 7
    win = min(win, h, w)
    if win % 2 == 0:
        win = max(3, win - 1)

    try:
        full_score, full_map = structural_similarity(
            ref_arr,
            cand_arr,
            channel_axis=2,
            data_range=255,
            win_size=win,
            full=True,
        )
    except ValueError as exc:  # window too large, image too small
        return {"status": "unavailable", "ssim": None, "notes": [f"ssim failed: {exc}"]}

    if mask is None:
        return {"status": "ok", "ssim": float(full_score), "win_size": win, "notes": []}

    mask_arr = mask if hasattr(mask, "shape") else np.asarray(mask)
    if mask_arr.shape[:2] != (h, w):
        return {
            "status": "ok",
            "ssim": float(full_score),
            "win_size": win,
            "notes": [f"mask shape {mask_arr.shape[:2]} != image {(h, w)}; using unmasked SSIM"],
        }

    if mask_arr.ndim == 3:
        mask_arr = mask_arr[..., 0]
    mask_arr = mask_arr.astype(np.float32)

    # full_map is per-channel; collapse to per-pixel mean.
    if full_map.ndim == 3:
        per_pixel = full_map.mean(axis=2)
    else:
        per_pixel = full_map

    weight_sum = float(mask_arr.sum())
    if weight_sum <= 0.0:
        return {
            "status": "ok",
            "ssim": float(full_score),
            "win_size": win,
            "notes": ["mask is all-zero; using unmasked SSIM"],
        }
    masked = float((per_pixel * mask_arr).sum() / weight_sum)
    return {
        "status": "ok",
        "ssim": masked,
        "ssim_unmasked": float(full_score),
        "win_size": win,
        "notes": [],
    }


# ---------------------------------------------------------------------
# Combined fit_score
# ---------------------------------------------------------------------


@dataclass
class PerceptualScoreResult:
    """Bundle of every signal :func:`combine_fit_score` produces."""

    fit_score: float  # combined, in [0, 1] (clamped)
    fit_components: dict[str, float]
    weighted_mae: float
    legacy_mae: float
    ssim: float | None
    auto_mask: AutoMaskResult | None
    channel_weights: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fit_score": self.fit_score,
            "fit_components": dict(self.fit_components),
            "weighted_mae": self.weighted_mae,
            "legacy_mae": self.legacy_mae,
            "ssim": self.ssim,
            "auto_mask": self.auto_mask.as_dict() if self.auto_mask else None,
            "channel_weights": dict(self.channel_weights),
        }


_MAE_BRANCH_DECAY = 4.0
"""Decay rate ``k`` of the new ``exp(-k * weighted_mae)`` mapping.

Calibrated against the synthetic E-009 / E-011 sweeps so that:

* ``MAE = 0.00`` → ``mae_branch = 1.00`` (perfect match)
* ``MAE = 0.05`` → ``mae_branch ≈ 0.82`` (~20 / 255 channel error, target hit)
* ``MAE = 0.10`` → ``mae_branch ≈ 0.67``
* ``MAE = 0.20`` → ``mae_branch ≈ 0.45``
* ``MAE = 0.33`` → ``mae_branch ≈ 0.27`` (the fish_1580 baseline that
  the old ``1 - sqrt(4·MAE)`` mapping clipped to 0 — now visible).

Crucially the curve is **strictly monotonic** for any finite MAE,
so the optimizer always sees a non-zero gradient when it improves
even slightly, which is exactly what the legacy mapping killed
once MAE crossed 0.25."""


def combine_fit_score(
    *,
    weighted_mae: float,
    ssim: float | None,
    weights: tuple[float, float] = (0.7, 0.3),
) -> tuple[float, dict[str, float]]:
    """Combine weighted-MAE and SSIM into a single ``fit_score`` in [0, 1].

    Phase-summary 2026-05-08 P0 fix
    -------------------------------

    The legacy MAE branch was ``max(0, 1 - sqrt(4·weighted_mae))``,
    which clips to ``0`` for ``weighted_mae > 0.25``. The fish_1580
    run (``best_mae=0.21``, ``weighted_mae≈0.33``) lived entirely in
    that saturated region, so the optimizer saw a flat zero on the
    MAE side for **all** 42 iterations and was effectively driven by
    SSIM noise alone (delta ≈ 1e-5).

    The new mapping is ``exp(-k · weighted_mae)`` with
    ``k = _MAE_BRANCH_DECAY``. This is strictly monotonic in MAE,
    so any genuine improvement — even from 0.34 to 0.30 — produces a
    measurable, sign-correct change in ``fit_score``. The SSIM
    branch keeps its ``max(0, ssim)`` mapping; weights stay (0.7, 0.3).

    The companion field ``mae_branch_saturated`` is **always False**
    under the new mapping and is exposed only so downstream
    diagnostics can confirm the upgrade is in effect.

    Returns ``(fit_score, components)`` where ``components`` keeps
    the un-combined branch scores plus the saturation flag for
    diagnostics.
    """

    if not math.isfinite(weighted_mae):
        return 0.0, {
            "mae_branch": 0.0,
            "ssim_branch": 0.0,
            "mae_branch_saturated": False,
            "mae_mapping": "exp_decay",
            "mae_decay_k": _MAE_BRANCH_DECAY,
        }

    mae_clamped = max(0.0, float(weighted_mae))
    mae_branch = math.exp(-_MAE_BRANCH_DECAY * mae_clamped)
    mae_branch = min(1.0, max(0.0, mae_branch))
    # Legacy mapping value preserved for forensic comparison only —
    # this is what the pre-P0 build would have produced and is what
    # the fish_1580 saturation diagnosis uses to confirm the bug.
    legacy_mae_branch = max(0.0, 1.0 - math.sqrt(mae_clamped * 4.0))
    legacy_saturated = mae_clamped > 0.25

    if ssim is None or not math.isfinite(float(ssim)):
        # Degrade gracefully to MAE-only.
        return mae_branch, {
            "mae_branch": mae_branch,
            "ssim_branch": float("nan"),
            "mae_branch_saturated": False,
            "legacy_mae_branch": legacy_mae_branch,
            "legacy_mae_branch_saturated": legacy_saturated,
            "mae_mapping": "exp_decay",
            "mae_decay_k": _MAE_BRANCH_DECAY,
        }

    ssim_branch = max(0.0, min(1.0, float(ssim)))
    w_mae, w_ssim = weights
    total = w_mae + w_ssim
    if total <= 0.0:
        total = 1.0
    fit = (mae_branch * w_mae + ssim_branch * w_ssim) / total
    fit = min(1.0, max(0.0, fit))
    return fit, {
        "mae_branch": mae_branch,
        "ssim_branch": ssim_branch,
        "mae_branch_saturated": False,
        "legacy_mae_branch": legacy_mae_branch,
        "legacy_mae_branch_saturated": legacy_saturated,
        "mae_mapping": "exp_decay",
        "mae_decay_k": _MAE_BRANCH_DECAY,
    }


def perceptual_score_from_analysis(
    analysis: dict[str, Any],
    *,
    reference_path: str | Path | None = None,
    candidate_path: str | Path | None = None,
    auto_mask_config: AutoMaskConfig | None = None,
    channel_weights_config: ChannelWeightConfig | None = None,
    branch_weights: tuple[float, float] = (0.7, 0.3),
) -> PerceptualScoreResult:
    """Compute the full perceptual score from a :func:`diff_analysis` result.

    This is the *high-level* entry point used by both
    :mod:`fit_material` and the report generator. It does not
    mutate the input analysis dict, but it does load the underlying
    images (via the paths in the analysis or the explicit overrides)
    to compute SSIM and the auto mask.
    """

    weight_cfg = channel_weights_config or ChannelWeightConfig()
    channels = analysis.get("material_channels", {}) or {}
    weighted = channel_weighted_mae(channels, weight_cfg)
    weighted_mae = float(weighted["weighted_mae"])
    legacy_mae = float(analysis.get("score", math.inf))

    ref_path = reference_path or analysis.get("reference_path")
    cand_path = candidate_path or analysis.get("candidate_path")

    auto_mask: AutoMaskResult | None = None
    ssim_value: float | None = None

    if Image is not None and ref_path and cand_path:
        try:
            ref_img = Image.open(str(ref_path)).convert("RGB")
            cand_img = Image.open(str(cand_path)).convert("RGB")
            if ref_img.size != cand_img.size:
                cand_img = cand_img.resize(ref_img.size)
            auto_mask = auto_background_mask(ref_img, cand_img, auto_mask_config)
            ssim_payload = ssim_score(
                ref_img,
                cand_img,
                mask=auto_mask.mask if auto_mask and auto_mask.status == "ok" else None,
            )
            if ssim_payload.get("status") == "ok":
                ssim_value = float(ssim_payload["ssim"])
        except (FileNotFoundError, OSError):
            auto_mask = AutoMaskResult(status="unavailable", notes=["image load failed"])
            ssim_value = None

    fit, components = combine_fit_score(
        weighted_mae=weighted_mae,
        ssim=ssim_value,
        weights=branch_weights,
    )

    return PerceptualScoreResult(
        fit_score=fit,
        fit_components=components,
        weighted_mae=weighted_mae,
        legacy_mae=legacy_mae,
        ssim=ssim_value,
        auto_mask=auto_mask,
        channel_weights=dict(weight_cfg.weights),
    )


# ---------------------------------------------------------------------
# Helpers exposed for tests
# ---------------------------------------------------------------------


def _renormalise_weights(weights: dict[str, float], available: Iterable[str]) -> dict[str, float]:
    """Internal helper kept public for unit testing."""

    available_set = set(available)
    total = sum(value for name, value in weights.items() if name in available_set)
    if total <= 0.0:
        return {}
    return {name: value / total for name, value in weights.items() if name in available_set}

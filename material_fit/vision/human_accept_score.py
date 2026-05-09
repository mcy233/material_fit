from __future__ import annotations

import math
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]


def build_human_accept_score(
    *,
    global_metrics: dict[str, Any],
    channels: dict[str, Any],
    weighted_mae: float,
    strict_fit_score: float,
    ssim: float | None,
    alignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a tolerant material-match score for human-acceptable similarity."""

    rgb_mae = _finite_float(global_metrics.get("rgb_mae"), 1.0)
    luma_mae = _finite_float(global_metrics.get("luma_mae"), rgb_mae)
    sat_mae = _finite_float(global_metrics.get("saturation_mae"), rgb_mae)

    color_distribution_score = _exp_score(
        rgb_mae * 0.55 + luma_mae * 0.30 + sat_mae * 0.15,
        decay=1.50,
    )
    material_channel_score = _exp_score(weighted_mae, decay=1.30)

    ssim_value = _finite_float(ssim, float("nan"))
    if math.isfinite(ssim_value):
        relaxed_structure_score = 0.60 + 0.40 * max(0.0, min(1.0, ssim_value))
    else:
        relaxed_structure_score = color_distribution_score

    feature_error = _material_feature_error(channels, fallback=rgb_mae)
    material_feature_score = _exp_score(feature_error, decay=1.45)
    strict_pixel_score = max(0.0, min(1.0, _finite_float(strict_fit_score, 0.0)))

    alignment_score = _finite_float((alignment or {}).get("alignment_score"), 1.0)
    weights = {
        "foreground_color_distribution": 0.32,
        "material_channel_statistics": 0.24,
        "relaxed_structure": 0.14,
        "material_feature_statistics": 0.15,
        "foreground_bbox_alignment": 0.05,
        "strict_pixel_guardrail": 0.10,
    }
    components = {
        "foreground_color_distribution": color_distribution_score,
        "material_channel_statistics": material_channel_score,
        "relaxed_structure": relaxed_structure_score,
        "material_feature_statistics": material_feature_score,
        "foreground_bbox_alignment": alignment_score,
        "strict_pixel_guardrail": strict_pixel_score,
    }
    score = sum(components[name] * weights[name] for name in weights)
    score = max(0.0, min(1.0, score))
    return {
        "score": score,
        "metric": "human_accept_material_score_v2" if alignment else "human_accept_material_score_v1",
        "components": components,
        "weights": weights,
        "inputs": {
            "rgb_mae": rgb_mae,
            "luma_mae": luma_mae,
            "saturation_mae": sat_mae,
            "weighted_mae": weighted_mae,
            "ssim": ssim,
            "material_feature_error": feature_error,
            "strict_fit_score": strict_fit_score,
        },
        "alignment": alignment,
        "notes": [
            "primary score for optimization when fit_score_mode='human_accept'",
            "keeps strict pixel score as a low-weight guardrail instead of the main target",
        ],
    }


def build_foreground_alignment(
    reference: Any,
    candidate: Any,
    *,
    reference_bg_color: tuple[int, int, int] | None,
    candidate_bg_color: tuple[int, int, int] | None,
    threshold: int,
) -> dict[str, Any]:
    """Estimate coarse foreground bbox alignment and contour ignore-band size."""

    if np is None or reference_bg_color is None or candidate_bg_color is None:
        return {"status": "unavailable", "reason": "numpy or background colors unavailable"}

    ref_arr = np.asarray(reference.convert("RGB") if hasattr(reference, "convert") else reference)
    cand_arr = np.asarray(candidate.convert("RGB") if hasattr(candidate, "convert") else candidate)
    if ref_arr.shape[:2] != cand_arr.shape[:2]:
        return {"status": "unavailable", "reason": f"shape mismatch {ref_arr.shape[:2]} vs {cand_arr.shape[:2]}"}

    ref_mask = _foreground_from_bg(ref_arr, reference_bg_color, threshold)
    cand_mask = _foreground_from_bg(cand_arr, candidate_bg_color, threshold)
    ref_bbox = _bbox(ref_mask)
    cand_bbox = _bbox(cand_mask)
    if ref_bbox is None or cand_bbox is None:
        return {"status": "low_signal", "reason": "empty foreground bbox"}

    h, w = ref_arr.shape[:2]
    ref_cx, ref_cy, ref_bw, ref_bh = _bbox_features(ref_bbox)
    cand_cx, cand_cy, cand_bw, cand_bh = _bbox_features(cand_bbox)
    center_delta = math.hypot((cand_cx - ref_cx) / max(w, 1), (cand_cy - ref_cy) / max(h, 1))
    scale_delta = abs(cand_bw / max(ref_bw, 1.0) - 1.0) + abs(cand_bh / max(ref_bh, 1.0) - 1.0)
    alignment_error = center_delta * 2.0 + scale_delta * 0.5
    alignment_score = _exp_score(alignment_error, decay=2.5)
    union = ref_mask | cand_mask
    boundary = _boundary_band(union)
    foreground_pixels = max(int(union.sum()), 1)
    return {
        "status": "ok",
        "reference_bbox": list(ref_bbox),
        "candidate_bbox": list(cand_bbox),
        "center_delta_norm": center_delta,
        "scale_delta": scale_delta,
        "alignment_score": alignment_score,
        "ignore_band_pixels": int(boundary.sum()),
        "ignore_band_ratio_of_foreground": float(boundary.sum()) / float(foreground_pixels),
        "notes": ["bbox coarse alignment and contour ignore-band diagnostics; no image resampling dependency"],
    }


def _foreground_from_bg(arr: Any, bg_color: tuple[int, int, int], threshold: int) -> Any:
    bg = np.array(bg_color, dtype=np.int32)
    diff = np.abs(arr.astype(np.int32) - bg).max(axis=2)
    return diff > int(threshold)


def _bbox(mask: Any) -> tuple[int, int, int, int] | None:
    ys, xs = np.where(mask)
    if len(xs) == 0 or len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _bbox_features(bbox: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    bw = max(float(x1 - x0 + 1), 1.0)
    bh = max(float(y1 - y0 + 1), 1.0)
    return x0 + bw * 0.5, y0 + bh * 0.5, bw, bh


def _boundary_band(mask: Any) -> Any:
    padded = np.pad(mask.astype(bool), 1, mode="constant", constant_values=False)
    center = padded[1:-1, 1:-1]
    eroded = (
        center
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    return center & ~eroded


def _material_feature_error(channels: dict[str, Any], *, fallback: float) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    feature_weights = {
        "base_color_main_texture": 0.35,
        "metallic_smoothness_specular": 0.20,
        "emission": 0.18,
        "fresnel_rim": 0.17,
        "color_grading_hsv_contrast": 0.10,
    }
    for name, weight in feature_weights.items():
        payload = channels.get(name)
        if not isinstance(payload, dict) or not payload.get("valid"):
            continue
        channel_error = _finite_float(payload.get("rgb_mae"), fallback)
        weighted_sum += channel_error * weight
        total_weight += weight
    if total_weight <= 0.0:
        return max(0.0, fallback)
    return max(0.0, weighted_sum / total_weight)


def _exp_score(error: float, *, decay: float) -> float:
    error = max(0.0, _finite_float(error, 1.0))
    return max(0.0, min(1.0, math.exp(-decay * error)))


def _finite_float(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        value = float(value)
        if math.isfinite(value):
            return value
    return fallback

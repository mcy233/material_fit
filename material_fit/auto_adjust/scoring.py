from __future__ import annotations

import math
from typing import Any


def diff_score_to_fit_score(diff_score: float, *, mode: str = "linear") -> float:
    """Convert lower-is-better RGB MAE into a higher-is-better automation score."""

    if not math.isfinite(diff_score):
        return -math.inf
    mae = max(0.0, float(diff_score))
    if mode == "perceptual":
        return max(0.0, min(1.0, 1.0 - math.sqrt(mae * 4.0)))
    return max(0.0, min(1.0, 1.0 - mae))


def resolve_fit_score(
    analysis: dict[str, Any],
    diff_score: float,
    *,
    mode: str = "human_accept",
) -> float:
    """Pick the headline score for one auto-adjust iteration."""

    if isinstance(analysis, dict):
        if mode == "human_accept":
            raw = analysis.get("human_accept_score")
            if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
                return max(0.0, min(1.0, float(raw)))
        if mode in ("human_accept", "perceptual"):
            raw = analysis.get("perceptual_fit_score")
            if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
                return max(0.0, min(1.0, float(raw)))
    return diff_score_to_fit_score(diff_score, mode=mode)


def extract_perceptual_signals(analysis: dict[str, Any]) -> dict[str, Any]:
    """Pull compact strict/human scoring diagnostics from an analysis dict."""

    if not isinstance(analysis, dict):
        return {}
    perc = analysis.get("perceptual")
    auto_mask = analysis.get("auto_mask")
    if not isinstance(perc, dict):
        return {}
    out: dict[str, Any] = {
        "weighted_mae": perc.get("weighted_mae"),
        "ssim": perc.get("ssim"),
        "ssim_status": perc.get("ssim_status"),
        "fit_score": perc.get("fit_score"),
        "fit_components": perc.get("fit_components"),
        "branch_weights": perc.get("branch_weights"),
        "weights_used": perc.get("weights_used"),
        "coverage": perc.get("coverage"),
        "diagnostics": perc.get("diagnostics"),
    }
    human_accept = analysis.get("human_accept")
    if isinstance(human_accept, dict):
        out["human_accept"] = {
            "score": human_accept.get("score"),
            "components": human_accept.get("components"),
            "weights": human_accept.get("weights"),
            "inputs": human_accept.get("inputs"),
        }
    if isinstance(auto_mask, dict):
        out["auto_mask"] = {
            "status": auto_mask.get("status"),
            "foreground_ratio": auto_mask.get("foreground_ratio"),
            "reference_bg_ratio": auto_mask.get("reference_bg_ratio"),
            "candidate_bg_ratio": auto_mask.get("candidate_bg_ratio"),
            "reference_bg_color": auto_mask.get("reference_bg_color"),
            "candidate_bg_color": auto_mask.get("candidate_bg_color"),
        }
    return out

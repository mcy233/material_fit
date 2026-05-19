from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .background_normalize import (
    BackgroundNormalizeConfig,
    NormalizeResult,
    normalize_pair,
)
from .image_score import load_rgba_pair
from .perceptual_score import (
    AutoMaskConfig,
    AutoMaskResult,
    ChannelWeightConfig,
    auto_background_mask,
    channel_weighted_mae,
    combine_fit_score,
    ssim_score,
)
from .research_metrics import aggregate_research_metrics, build_research_metrics
from .human_accept_score import build_foreground_alignment, build_human_accept_score


@dataclass
class ImageDiffConfig:
    """Configuration for one reference/candidate image comparison.

    See :mod:`tools.material_fit.vision.perceptual_score` for the
    rationale behind the ``auto_mask_*`` and ``channel_weights_*``
    fields. They drive the experiment **E-009** scientific score
    upgrade (alpha-mask + channel-weighted MAE + SSIM); the legacy
    "global RGB MAE" pipeline is preserved on the :attr:`score`
    field for backward compatibility.
    """

    reference_path: str | Path
    candidate_path: str | Path
    mask_path: str | Path | None = None
    output_dir: str | Path | None = None
    generate_diff_image: bool = True
    diff_gain: float = 4.0
    dark_threshold: float = 0.08
    highlight_threshold: float = 0.72
    emission_threshold: float = 0.88
    auto_mask_enabled: bool = True
    auto_mask_threshold: int = 16
    auto_mask_corner_size: int = 12
    channel_weights: dict[str, float] | None = None
    fit_branch_weights: tuple[float, float] = (0.7, 0.3)
    # Experiment E-011 (2026-05-07): when both engines render onto a
    # *pure* but different background (Unity grey vs Laya sky), this
    # preprocessing step substitutes each image's detected bg with a
    # unified ``bg_normalize_target``. Eliminates pure-bg colour
    # leakage and partially corrects the silhouette anti-alias band.
    # Disable for ablation, or when one engine is rendered onto a
    # *non-uniform* bg (e.g. a sky gradient or a real environment) —
    # in that case the corner detector returns a single colour but
    # would over-substitute interior pixels that happen to share it.
    bg_normalize_enabled: bool = True
    bg_normalize_target: tuple[int, int, int] = (128, 128, 128)
    bg_normalize_soft_low: float = 2.0
    bg_normalize_soft_high: float = 4.0
    bg_normalize_corner_size: int = 12


def analyze_image_diff(config: ImageDiffConfig) -> dict[str, Any]:
    """Analyze visual differences between Unity reference and Laya candidate.

    The output is deliberately numeric and grouped by material concerns so the
    optimizer can decide whether to adjust base color, smoothness, fresnel,
    emission, color grading, etc. It is still a heuristic analysis; it does not
    replace shader-specific render passes, but it gives the next tuning stage a
    much richer signal than a single MAE value.
    """

    try:
        reference, candidate, mask = load_rgba_pair(config.reference_path, config.candidate_path, config.mask_path)
        research_reference = reference.copy()
        research_candidate = candidate.copy()
        from PIL import Image
        import numpy as np
    except ImportError:
        return {"status": "pending", "score": math.inf, "reason": "Pillow is not installed"}

    width, height = reference.size

    # Experiment E-009: when no explicit mask was supplied, derive a
    # foreground mask by clustering corner pixels of both images.
    # This excludes the editor / sky background from per-pixel
    # statistics. It is conservative: only pixels that BOTH images
    # consider foreground are kept. See the dedicated write-up at
    # docs/Metric_Validation.md for the calibration data.
    #
    # E-011 (2026-05-07) IMPORTANT ORDERING: ``auto_background_mask``
    # MUST run on the *original* (un-normalised) images, otherwise
    # bg substitution mutates the colours auto_mask uses to detect
    # "what is bg", and edge-pixel classification flips
    # asymmetrically between the two images. We therefore (1) build
    # the mask first, (2) reuse the bg colours it already detected
    # to drive E-011's substitution, (3) feed the *substituted*
    # images into the per-pixel weighted MAE and SSIM with the
    # original mask preserved.
    mask_pixels = mask.load() if mask else None
    auto_mask_payload: AutoMaskResult | None = None
    auto_mask_array = None
    if config.auto_mask_enabled and mask_pixels is None:
        auto_mask_cfg = AutoMaskConfig(
            corner_size=config.auto_mask_corner_size,
            threshold=config.auto_mask_threshold,
            enabled=True,
        )
        auto_mask_payload = auto_background_mask(reference, candidate, auto_mask_cfg)
        if auto_mask_payload.status == "ok" and auto_mask_payload.mask is not None:
            auto_mask_array = auto_mask_payload.mask

    human_alignment_payload: dict[str, Any] | None = None
    if auto_mask_payload is not None:
        human_alignment_payload = build_foreground_alignment(
            reference,
            candidate,
            reference_bg_color=auto_mask_payload.reference_bg_color,
            candidate_bg_color=auto_mask_payload.candidate_bg_color,
            threshold=config.auto_mask_threshold,
        )

    # Experiment E-011: bg normalisation runs *after* mask detection
    # but *before* the per-pixel weighted MAE / SSIM, so:
    #
    #   - the auto-mask is computed against each engine's true bg
    #     (engine_bg_unity, engine_bg_laya), not against a shared
    #     synthetic target (which would shift the mask boundary
    #     asymmetrically — see Q2 in verify_e011.py for the bug
    #     this ordering fixes);
    #   - SSIM still sees a *consistent* surrounding bg colour in
    #     both images, so its 7×7 window doesn't get confused at
    #     mask boundaries.
    #
    # We pass the bg colours auto_mask already found in via
    # ``source_bg_override`` so we never re-detect — single source
    # of truth.
    bg_normalize_payload: dict[str, Any] | None = None
    if (
        config.bg_normalize_enabled
        and mask_pixels is None
        and auto_mask_payload is not None
        and auto_mask_payload.reference_bg_color is not None
        and auto_mask_payload.candidate_bg_color is not None
    ):
        bg_cfg = BackgroundNormalizeConfig(
            enabled=True,
            target_bg=tuple(int(v) for v in config.bg_normalize_target),
            soft_low=float(config.bg_normalize_soft_low),
            soft_high=float(config.bg_normalize_soft_high),
            corner_size=int(config.bg_normalize_corner_size),
        )
        ref_arr = np.asarray(reference, dtype=np.uint8)
        cand_arr = np.asarray(candidate, dtype=np.uint8)
        from .background_normalize import normalize_background as _normalize
        ref_norm = _normalize(
            ref_arr, bg_cfg, source_bg_override=auto_mask_payload.reference_bg_color
        )
        cand_norm = _normalize(
            cand_arr, bg_cfg, source_bg_override=auto_mask_payload.candidate_bg_color
        )
        reference = Image.fromarray(ref_norm.image, mode="RGBA")
        candidate = Image.fromarray(cand_norm.image, mode="RGBA")
        bg_normalize_payload = {
            "enabled": True,
            "reference": ref_norm.as_dict(),
            "candidate": cand_norm.as_dict(),
        }
    elif not config.bg_normalize_enabled:
        bg_normalize_payload = {"enabled": False, "reason": "disabled in config"}
    elif mask_pixels is not None:
        bg_normalize_payload = {
            "enabled": False,
            "reason": "explicit mask supplied — skipped to honour caller's mask",
        }
    else:
        bg_normalize_payload = {
            "enabled": False,
            "reason": (
                "auto_mask did not produce reference/candidate bg colours "
                "(low signal or unavailable) — substitution skipped to avoid "
                "running an independent corner detector"
            ),
        }

    ref_pixels = reference.load()
    cand_pixels = candidate.load()

    stats = _Accumulator()
    dark_stats = _Accumulator()
    mid_stats = _Accumulator()
    highlight_stats = _Accumulator()
    emission_stats = _Accumulator()
    edge_stats = _Accumulator()
    center_stats = _Accumulator()

    diff_image = Image.new("RGBA", (width, height), (0, 0, 0, 255)) if config.generate_diff_image else None
    diff_pixels = diff_image.load() if diff_image else None

    center_x = (width - 1) * 0.5
    center_y = (height - 1) * 0.5
    max_radius = max(math.hypot(center_x, center_y), 1.0)

    for y in range(height):
        for x in range(width):
            if mask_pixels is not None:
                weight = mask_pixels[x, y] / 255.0
            elif auto_mask_array is not None:
                weight = float(auto_mask_array[y, x])
            else:
                weight = 1.0
            if weight <= 0.0:
                continue

            ref_rgb = tuple(ref_pixels[x, y][i] / 255.0 for i in range(3))
            cand_rgb = tuple(cand_pixels[x, y][i] / 255.0 for i in range(3))
            sample = _make_sample(ref_rgb, cand_rgb, weight)
            stats.add(sample)

            ref_luma = sample["ref_luma"]
            if ref_luma <= config.dark_threshold:
                dark_stats.add(sample)
            elif ref_luma >= config.highlight_threshold:
                highlight_stats.add(sample)
            else:
                mid_stats.add(sample)
            if ref_luma >= config.emission_threshold:
                emission_stats.add(sample)

            radius = math.hypot(x - center_x, y - center_y) / max_radius
            if radius >= 0.72:
                edge_stats.add(sample)
            elif radius <= 0.45:
                center_stats.add(sample)

            if diff_pixels:
                diff_pixels[x, y] = tuple(
                    int(max(0.0, min(1.0, abs(ref_rgb[i] - cand_rgb[i]) * config.diff_gain)) * 255) for i in range(3)
                ) + (255,)

    output_dir = Path(config.output_dir) if config.output_dir else Path(config.candidate_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    diff_image_path = None
    if diff_image:
        diff_image_path = output_dir / "diff_visual.png"
        diff_image.save(diff_image_path)

    global_metrics = stats.to_metrics()
    sections = {
        "dark_shadow_occlusion": dark_stats.to_metrics(),
        "base_mid_tone": mid_stats.to_metrics(),
        "highlight_specular_reflection": highlight_stats.to_metrics(),
        "very_bright_emission": emission_stats.to_metrics(),
        "edge_fresnel_rim": edge_stats.to_metrics(),
        "center_body": center_stats.to_metrics(),
    }
    channels = _build_material_channel_diagnostics(global_metrics, sections)
    suggestions = _build_adjustment_hints(channels)

    # Experiment E-009: compute the channel-weighted MAE + SSIM and
    # surface a single ``perceptual_fit_score`` next to the legacy
    # ``score`` (RGB MAE). The legacy field stays the primary key
    # consumed by the optimizer until the migration in fit_material
    # is complete; new code should prefer ``perceptual_fit_score``
    # and ``perceptual.weighted_mae``.
    weight_cfg = ChannelWeightConfig(weights=dict(config.channel_weights)) if config.channel_weights else ChannelWeightConfig()
    weighted = channel_weighted_mae(channels, weight_cfg)
    ssim_payload = ssim_score(
        reference,
        candidate,
        mask=auto_mask_array,
    )
    ssim_value = ssim_payload.get("ssim") if ssim_payload.get("status") == "ok" else None
    fit_value, fit_components = combine_fit_score(
        weighted_mae=float(weighted["weighted_mae"]),
        ssim=ssim_value,
        weights=config.fit_branch_weights,
    )
    human_accept = build_human_accept_score(
        global_metrics=global_metrics,
        channels=channels,
        weighted_mae=float(weighted["weighted_mae"]),
        strict_fit_score=float(fit_value),
        ssim=ssim_value,
        alignment=human_alignment_payload,
    )
    # P0 diagnostics (phase summary 2026-05-08): the fish_1580 post
    # mortem couldn't tell at a glance whether a flat fit_score came
    # from saturated metrics, vanishing foreground, or unfixed bg
    # mismatch. We now surface a tiny `diagnostics` block so the
    # next "nothing is moving" report can be triaged in seconds:
    #
    # * mae_branch_saturated — true if the legacy ``1 - sqrt(4·MAE)``
    #   curve would clip to 0 here (i.e. weighted_mae > 0.25). Under
    #   the new exp_decay mapping the live mae_branch is *not* zero,
    #   but this flag preserves the visibility of the old failure mode.
    # * foreground_ratio — fraction of pixels that survived the
    #   intersection-of-foregrounds auto mask. < 0.05 means the mask
    #   is wrong; < 0.20 typically means scene framing differs.
    # * bg_color_delta — L2 distance between the reference and
    #   candidate background colours BEFORE bg_normalize ran. After
    #   the E-011 substitution this should not affect the score, but
    #   a huge value here is a hint the bg detector or the rendering
    #   bg setup might be off.
    diag_foreground_ratio: float | None = None
    diag_bg_delta: float | None = None
    diag_legacy_saturated = bool(fit_components.get("legacy_mae_branch_saturated", False))
    if auto_mask_payload is not None:
        if auto_mask_payload.foreground_ratio is not None:
            diag_foreground_ratio = float(auto_mask_payload.foreground_ratio)
        ref_bg = auto_mask_payload.reference_bg_color
        cand_bg = auto_mask_payload.candidate_bg_color
        if ref_bg is not None and cand_bg is not None:
            diag_bg_delta = float(
                math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(ref_bg, cand_bg)))
            )
    diagnostics = {
        "mae_branch_saturated": diag_legacy_saturated,
        "foreground_ratio": diag_foreground_ratio,
        "bg_color_delta": diag_bg_delta,
        "bg_normalize_applied": bool(
            isinstance(bg_normalize_payload, dict) and bg_normalize_payload.get("enabled")
        ),
    }
    perceptual_block = {
        "weighted_mae": weighted["weighted_mae"],
        "weights_used": weighted["weights_used"],
        "contributions": weighted["contributions"],
        "coverage": weighted["coverage"],
        "ssim": ssim_value,
        "ssim_status": ssim_payload.get("status"),
        "ssim_notes": ssim_payload.get("notes", []),
        "fit_score": fit_value,
        "fit_components": fit_components,
        "branch_weights": list(config.fit_branch_weights),
        "diagnostics": diagnostics,
    }
    research_metrics = build_research_metrics(
        research_reference,
        research_candidate,
        explicit_mask=mask,
        fallback_mask=auto_mask_array,
    )

    result: dict[str, Any] = {
        "status": "ok",
        "metric": "material_oriented_image_diff_v1",
        "score": global_metrics["rgb_mae"],
        "perceptual_fit_score": fit_value,
        "image_size": [width, height],
        "reference_path": str(config.reference_path),
        "candidate_path": str(config.candidate_path),
        "mask_path": str(config.mask_path) if config.mask_path else "",
        "diff_image_path": str(diff_image_path) if diff_image_path else "",
        "auto_mask": auto_mask_payload.as_dict() if auto_mask_payload else None,
        "bg_normalize": bg_normalize_payload,
        "global": global_metrics,
        "regions": sections,
        "material_channels": channels,
        "adjustment_hints": suggestions,
        "perceptual": perceptual_block,
        "research_metrics": research_metrics,
        "human_accept_score": human_accept["score"],
        "human_accept": human_accept,
    }

    report_path = output_dir / "diff_analysis.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["report_path"] = str(report_path)
    return result


def analyze_image_pairs(pairs: Iterable[dict[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    """Analyze multiple camera angles and aggregate their material diagnostics."""

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs):
        pair_dir = output_root / f"pair_{index:02d}"
        result = analyze_image_diff(
            ImageDiffConfig(
                reference_path=pair["reference"],
                candidate_path=pair["candidate"],
                mask_path=pair.get("mask"),
                output_dir=pair_dir,
            )
        )
        results.append(result)

    ok_results = [item for item in results if item.get("status") == "ok"]
    aggregate = {
        "status": "ok" if ok_results else "pending",
        "pairs": results,
        "score": _mean(item.get("score", math.inf) for item in ok_results),
        "material_channels": _aggregate_channels(ok_results),
    }
    (output_root / "multi_view_diff_analysis.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return aggregate


def analyze_multiview_pairs(
    pairs: Iterable[dict[str, Any]],
    output_dir: str | Path,
    *,
    fit_score_mode: str = "human_accept",
    aggregation_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze multi-view render pairs and build a strategy-compatible aggregate.

    The returned ``strategy_analysis`` intentionally keeps the same top-level
    shape as :func:`analyze_image_diff`, so existing optimizers can consume it
    without knowing whether the signal came from one view or many.
    """

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    aggregation = aggregation_config if isinstance(aggregation_config, dict) else {}
    pair_items = list(pairs)
    analyses: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    for index, pair in enumerate(pair_items):
        pair_dir = output_root / f"pair_{index:02d}"
        result = analyze_image_diff(
            ImageDiffConfig(
                reference_path=pair["reference"],
                candidate_path=pair["candidate"],
                mask_path=pair.get("mask"),
                output_dir=pair_dir,
            )
        )
        diff_score = _finite_float(result.get("score"), math.inf)
        fit_score = _resolve_view_fit_score(result, diff_score, fit_score_mode)
        view_id = str(pair.get("view_id") or pair.get("id") or f"view_{index:03d}")
        view_payload = {
            "pair_index": index,
            "view_id": view_id,
            "reference": str(pair.get("reference", "")),
            "candidate": str(pair.get("candidate", "")),
            "mask": str(pair.get("mask", "")) if pair.get("mask") else "",
            "analysis_dir": str(pair_dir),
            "analysis_path": str(result.get("report_path") or pair_dir / "diff_analysis.json"),
            "diff_image_path": str(result.get("diff_image_path") or ""),
            "diff_score": diff_score,
            "fit_score": fit_score,
            "perceptual_fit_score": _optional_float(result.get("perceptual_fit_score")),
            "human_accept_score": _optional_float(result.get("human_accept_score")),
            "research_score": _optional_float((result.get("research_metrics") or {}).get("score") if isinstance(result.get("research_metrics"), dict) else None),
            "research_loss": _optional_float((result.get("research_metrics") or {}).get("loss") if isinstance(result.get("research_metrics"), dict) else None),
            "research_valid": (
                (result.get("research_metrics") or {}).get("validity", {}).get("passed")
                if isinstance(result.get("research_metrics"), dict)
                and isinstance(result.get("research_metrics", {}).get("validity"), dict)
                else None
            ),
            "research_metrics": result.get("research_metrics") if isinstance(result.get("research_metrics"), dict) else None,
            "status": str(result.get("status") or ""),
        }
        analyses.append(result)
        views.append(view_payload)

    ok_analyses = [item for item in analyses if item.get("status") == "ok"]
    ok_views = [
        view
        for view in views
        if math.isfinite(_finite_float(view.get("diff_score"), math.inf))
        or math.isfinite(_finite_float(view.get("fit_score"), -math.inf))
    ]
    diff_scores = [_finite_float(view.get("diff_score"), math.inf) for view in ok_views]
    fit_scores = [_finite_float(view.get("fit_score"), -math.inf) for view in ok_views]
    diff_mean = _mean(diff_scores)
    fit_mean = _mean(fit_scores)
    fit_min = _min_finite(fit_scores, default=-math.inf)
    fit_max = _max_finite(fit_scores, default=-math.inf)
    fit_p10 = _percentile_finite(fit_scores, 10.0, default=-math.inf)
    loss_values = [1.0 - score for score in fit_scores if math.isfinite(score)]
    p90_loss = _percentile_finite(loss_values, 90.0, default=math.inf)
    worst_view = _worst_fit_view(ok_views)

    base_analysis = dict(ok_analyses[0]) if ok_analyses else {
        "status": "pending",
        "metric": "material_oriented_multiview_diff_v1",
        "score": math.inf,
    }
    aggregate_channels = _aggregate_strategy_channels(ok_analyses)
    aggregate_perceptual = _aggregate_perceptual(ok_analyses)
    aggregate_human = _aggregate_human_accept(ok_analyses)
    aggregate_research = aggregate_research_metrics(
        [
            item.get("research_metrics")
            for item in ok_analyses
            if isinstance(item.get("research_metrics"), dict)
        ]
    )
    strategy_analysis = dict(base_analysis)
    strategy_analysis["metric"] = "material_oriented_multiview_diff_v1"
    strategy_analysis["score"] = diff_mean
    if math.isfinite(fit_mean):
        strategy_analysis["perceptual_fit_score"] = fit_mean
    if aggregate_human:
        strategy_analysis["human_accept"] = aggregate_human
        if isinstance(aggregate_human.get("score"), (int, float)):
            strategy_analysis["human_accept_score"] = float(aggregate_human["score"])
    strategy_analysis["material_channels"] = aggregate_channels
    strategy_analysis["adjustment_hints"] = _build_adjustment_hints(aggregate_channels)
    if aggregate_perceptual:
        strategy_analysis["perceptual"] = aggregate_perceptual
    if aggregate_research:
        strategy_analysis["research_metrics"] = aggregate_research
    strategy_analysis["global"] = _aggregate_metric_dicts([item.get("global") for item in ok_analyses])
    strategy_analysis["regions"] = _aggregate_regions(ok_analyses)

    summary = {
        "mean_diff_score": diff_mean,
        "mean_fit_score": fit_mean,
        "min_fit_score": fit_min,
        "max_fit_score": fit_max,
        "p10_fit_score": fit_p10,
        "p90_loss": p90_loss,
        "worst_view_id": worst_view.get("view_id") if worst_view else "",
        "worst_fit_score": worst_view.get("fit_score") if worst_view else None,
        "research_score": aggregate_research.get("score") if isinstance(aggregate_research, dict) else None,
        "research_loss": aggregate_research.get("loss") if isinstance(aggregate_research, dict) else None,
        "research_valid_view_count": aggregate_research.get("valid_view_count") if isinstance(aggregate_research, dict) else None,
        "research_invalid_view_count": aggregate_research.get("invalid_view_count") if isinstance(aggregate_research, dict) else None,
    }
    multiview = {
        "version": 1,
        "status": "ok" if ok_analyses else "pending",
        "aggregation": {
            "fit": str(aggregation.get("fit_aggregation") or "mean"),
            "diff": str(aggregation.get("diff_aggregation") or "mean"),
            "channels": str(aggregation.get("channel_aggregation") or "mean_with_worst_severity"),
        },
        "pair_count": len(pair_items),
        "ok_count": len(ok_analyses),
        "diff_scores": diff_scores,
        "fit_scores": fit_scores,
        "views": views,
        "summary": summary,
    }
    strategy_analysis["multiview"] = multiview
    report = {
        "status": multiview["status"],
        "pairs": analyses,
        "views": views,
        "summary": summary,
        "multiview_analysis": multiview,
        "strategy_analysis": strategy_analysis,
    }
    (output_root / "multi_view_diff_analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


class _Accumulator:
    def __init__(self) -> None:
        self.weight = 0.0
        self.rgb_abs = [0.0, 0.0, 0.0]
        self.rgb_signed = [0.0, 0.0, 0.0]
        self.luma_abs = 0.0
        self.luma_signed = 0.0
        self.saturation_abs = 0.0
        self.saturation_signed = 0.0
        self.contrast_abs = 0.0
        self.contrast_signed = 0.0

    def add(self, sample: dict[str, float]) -> None:
        weight = sample["weight"]
        self.weight += weight
        for index, channel in enumerate(("r", "g", "b")):
            signed = sample[f"signed_{channel}"]
            self.rgb_signed[index] += signed * weight
            self.rgb_abs[index] += abs(signed) * weight
        self.luma_signed += sample["signed_luma"] * weight
        self.luma_abs += abs(sample["signed_luma"]) * weight
        self.saturation_signed += sample["signed_saturation"] * weight
        self.saturation_abs += abs(sample["signed_saturation"]) * weight
        self.contrast_signed += sample["signed_contrast"] * weight
        self.contrast_abs += abs(sample["signed_contrast"]) * weight

    def to_metrics(self) -> dict[str, Any]:
        if self.weight <= 0.0:
            return {"pixels": 0, "rgb_mae": 0.0, "valid": False}
        inv = 1.0 / self.weight
        rgb_abs = [value * inv for value in self.rgb_abs]
        rgb_signed = [value * inv for value in self.rgb_signed]
        return {
            "valid": True,
            "pixels": round(self.weight, 3),
            "rgb_mae": sum(rgb_abs) / 3.0,
            "rgb_abs": rgb_abs,
            "rgb_signed_candidate_minus_reference": rgb_signed,
            "luma_mae": self.luma_abs * inv,
            "luma_signed_candidate_minus_reference": self.luma_signed * inv,
            "saturation_mae": self.saturation_abs * inv,
            "saturation_signed_candidate_minus_reference": self.saturation_signed * inv,
            "contrast_mae": self.contrast_abs * inv,
            "contrast_signed_candidate_minus_reference": self.contrast_signed * inv,
        }


def _make_sample(ref_rgb: tuple[float, float, float], cand_rgb: tuple[float, float, float], weight: float) -> dict[str, float]:
    ref_luma = _luma(ref_rgb)
    cand_luma = _luma(cand_rgb)
    ref_sat = _saturation(ref_rgb)
    cand_sat = _saturation(cand_rgb)
    return {
        "weight": weight,
        "ref_luma": ref_luma,
        "cand_luma": cand_luma,
        "signed_r": cand_rgb[0] - ref_rgb[0],
        "signed_g": cand_rgb[1] - ref_rgb[1],
        "signed_b": cand_rgb[2] - ref_rgb[2],
        "signed_luma": cand_luma - ref_luma,
        "signed_saturation": cand_sat - ref_sat,
        "signed_contrast": abs(cand_luma - 0.5) - abs(ref_luma - 0.5),
    }


def _luma(rgb: tuple[float, float, float]) -> float:
    return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722


def _saturation(rgb: tuple[float, float, float]) -> float:
    return max(rgb) - min(rgb)


def _build_material_channel_diagnostics(global_metrics: dict[str, Any], regions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    mid = regions["base_mid_tone"]
    highlight = regions["highlight_specular_reflection"]
    emission = regions["very_bright_emission"]
    edge = regions["edge_fresnel_rim"]
    dark = regions["dark_shadow_occlusion"]
    center = regions["center_body"]

    return {
        "base_color_main_texture": _channel("中间调/主体色差", mid, ["u_BaseColor", "u_BaseMap", "u_Gamma_Power"]),
        "metallic_smoothness_specular": _channel("高亮区亮度与色差", highlight, ["u_Metallic", "u_Smoothness", "u_MetallicRemap*", "u_SmoothnessRemap*"]),
        "environment_reflection_matcap": _channel("高亮区与边缘共同差异", _merge_metric(highlight, edge), ["u_IBLMap", "u_IBLExposure", "u_MatcapMap", "u_MatcapStrength"]),
        "fresnel_rim": _channel("边缘区差异", edge, ["u_FresnelColor", "u_FresnelIntensity", "u_FresnelPow", "u_fresnelOffset"]),
        "emission": _channel("极亮区差异", emission, ["u_EmissionColor", "u_EmissionScale", "u_EmissionTexture"]),
        "shadow_occlusion": _channel("暗部差异", dark, ["u_MAER.g", "occlusion", "ambient"]),
        "color_grading_hsv_contrast": _channel("全局饱和度/对比度差异", global_metrics, ["u_AdjustHue", "u_AdjustSaturation", "u_AdjustLightness", "u_ContrastScale"]),
        "center_vs_edge_balance": {
            "valid": center.get("valid", False) and edge.get("valid", False),
            "center_luma_signed": center.get("luma_signed_candidate_minus_reference", 0.0),
            "edge_luma_signed": edge.get("luma_signed_candidate_minus_reference", 0.0),
            "edge_minus_center_luma_bias": edge.get("luma_signed_candidate_minus_reference", 0.0) - center.get("luma_signed_candidate_minus_reference", 0.0),
            "related_params": ["u_FresnelIntensity", "u_MatcapStrength", "u_EmissionScale"],
        },
    }


def _channel(name: str, metrics: dict[str, Any], related_params: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "valid": metrics.get("valid", False),
        "severity": _severity(metrics.get("rgb_mae", 0.0)),
        "rgb_mae": metrics.get("rgb_mae", 0.0),
        "luma_bias_candidate_minus_reference": metrics.get("luma_signed_candidate_minus_reference", 0.0),
        "saturation_bias_candidate_minus_reference": metrics.get("saturation_signed_candidate_minus_reference", 0.0),
        "contrast_bias_candidate_minus_reference": metrics.get("contrast_signed_candidate_minus_reference", 0.0),
        "rgb_bias_candidate_minus_reference": metrics.get("rgb_signed_candidate_minus_reference", [0.0, 0.0, 0.0]),
        "related_params": related_params,
    }


def _merge_metric(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    if not a.get("valid"):
        return b
    if not b.get("valid"):
        return a
    aw = float(a.get("pixels", 0.0))
    bw = float(b.get("pixels", 0.0))
    total = max(aw + bw, 1.0)
    merged: dict[str, Any] = {"valid": True, "pixels": total}
    for key in ("rgb_mae", "luma_signed_candidate_minus_reference", "saturation_signed_candidate_minus_reference", "contrast_signed_candidate_minus_reference"):
        merged[key] = (float(a.get(key, 0.0)) * aw + float(b.get(key, 0.0)) * bw) / total
    merged["rgb_signed_candidate_minus_reference"] = [
        (a.get("rgb_signed_candidate_minus_reference", [0, 0, 0])[i] * aw + b.get("rgb_signed_candidate_minus_reference", [0, 0, 0])[i] * bw) / total
        for i in range(3)
    ]
    return merged


def _severity(value: float) -> str:
    if value >= 0.16:
        return "high"
    if value >= 0.07:
        return "medium"
    if value >= 0.025:
        return "low"
    return "none"


def _build_adjustment_hints(channels: dict[str, Any]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for key, channel in channels.items():
        if not isinstance(channel, dict) or not channel.get("valid") or channel.get("severity") == "none":
            continue
        luma_bias = float(channel.get("luma_bias_candidate_minus_reference", 0.0))
        sat_bias = float(channel.get("saturation_bias_candidate_minus_reference", 0.0))
        direction = "decrease" if luma_bias > 0.015 else "increase" if luma_bias < -0.015 else "inspect"
        hints.append(
            {
                "channel": key,
                "severity": channel.get("severity"),
                "direction": direction,
                "reason": _hint_reason(key, luma_bias, sat_bias),
                "related_params": channel.get("related_params", []),
            }
        )
    return hints


def _hint_reason(channel: str, luma_bias: float, sat_bias: float) -> str:
    brightness = "偏亮" if luma_bias > 0.015 else "偏暗" if luma_bias < -0.015 else "亮度接近"
    saturation = "饱和度偏高" if sat_bias > 0.015 else "饱和度偏低" if sat_bias < -0.015 else "饱和度接近"
    return f"{channel} 区域候选图相对参考图{brightness}，{saturation}"


def _aggregate_channels(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {}
    keys = sorted({key for result in results for key in result.get("material_channels", {}).keys()})
    aggregate: dict[str, Any] = {}
    for key in keys:
        values = [result["material_channels"].get(key, {}) for result in results]
        valid_values = [value for value in values if isinstance(value, dict) and value.get("valid")]
        aggregate[key] = {
            "valid": bool(valid_values),
            "avg_rgb_mae": _mean(value.get("rgb_mae", 0.0) for value in valid_values),
            "avg_luma_bias_candidate_minus_reference": _mean(value.get("luma_bias_candidate_minus_reference", 0.0) for value in valid_values),
            "worst_severity": _worst_severity(value.get("severity", "none") for value in valid_values),
        }
    return aggregate


def _aggregate_strategy_channels(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {}
    keys = sorted({key for result in results for key in result.get("material_channels", {}).keys()})
    aggregate: dict[str, Any] = {}
    for key in keys:
        values = [
            result.get("material_channels", {}).get(key, {})
            for result in results
            if isinstance(result.get("material_channels"), dict)
        ]
        valid_values = [value for value in values if isinstance(value, dict) and value.get("valid")]
        if not valid_values:
            aggregate[key] = {"valid": False, "severity": "none", "rgb_mae": 0.0}
            continue
        related_params: list[str] = []
        for value in valid_values:
            for param in value.get("related_params", []) if isinstance(value.get("related_params"), list) else []:
                if isinstance(param, str) and param not in related_params:
                    related_params.append(param)
        rgb_biases = [value.get("rgb_bias_candidate_minus_reference") for value in valid_values]
        aggregate[key] = {
            "name": valid_values[0].get("name") or key,
            "valid": True,
            "severity": _worst_severity(value.get("severity", "none") for value in valid_values),
            "rgb_mae": _mean(value.get("rgb_mae", 0.0) for value in valid_values),
            "avg_rgb_mae": _mean(value.get("rgb_mae", 0.0) for value in valid_values),
            "max_rgb_mae": _max_finite((value.get("rgb_mae", 0.0) for value in valid_values), default=0.0),
            "luma_bias_candidate_minus_reference": _mean(
                value.get("luma_bias_candidate_minus_reference", 0.0) for value in valid_values
            ),
            "saturation_bias_candidate_minus_reference": _mean(
                value.get("saturation_bias_candidate_minus_reference", 0.0) for value in valid_values
            ),
            "contrast_bias_candidate_minus_reference": _mean(
                value.get("contrast_bias_candidate_minus_reference", 0.0) for value in valid_values
            ),
            "rgb_bias_candidate_minus_reference": _mean_vector(rgb_biases, 3),
            "related_params": related_params,
            "view_count": len(valid_values),
        }
        for extra_key in ("center_luma_signed", "edge_luma_signed", "edge_minus_center_luma_bias"):
            if any(extra_key in value for value in valid_values):
                aggregate[key][extra_key] = _mean(value.get(extra_key, 0.0) for value in valid_values)
    return aggregate


def _aggregate_perceptual(results: list[dict[str, Any]]) -> dict[str, Any]:
    perceptuals = [item.get("perceptual") for item in results if isinstance(item.get("perceptual"), dict)]
    if not perceptuals:
        return {}
    base = dict(perceptuals[0])
    for key in ("weighted_mae", "ssim", "fit_score"):
        base[key] = _mean(item.get(key, math.inf) for item in perceptuals)
    base["diagnostics"] = _aggregate_diagnostics([item.get("diagnostics") for item in perceptuals])
    base["view_count"] = len(perceptuals)
    return base


def _aggregate_human_accept(results: list[dict[str, Any]]) -> dict[str, Any]:
    items = [item.get("human_accept") for item in results if isinstance(item.get("human_accept"), dict)]
    if not items:
        scores = [item.get("human_accept_score") for item in results]
        mean_score = _mean(score for score in scores if isinstance(score, (int, float)))
        return {"score": mean_score, "view_count": len(scores)} if math.isfinite(mean_score) else {}
    base = dict(items[0])
    base["score"] = _mean(item.get("score", math.inf) for item in items)
    base["view_count"] = len(items)
    return base


def _aggregate_diagnostics(items: Iterable[Any]) -> dict[str, Any]:
    dicts = [item for item in items if isinstance(item, dict)]
    if not dicts:
        return {}
    keys = sorted({key for item in dicts for key in item.keys()})
    out: dict[str, Any] = {}
    for key in keys:
        values = [item.get(key) for item in dicts]
        if all(isinstance(value, bool) for value in values if value is not None):
            out[key] = any(bool(value) for value in values)
        else:
            numeric = [
                float(value)
                for value in values
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
            ]
            if numeric:
                out[key] = sum(numeric) / len(numeric)
            else:
                out[key] = next((value for value in values if value is not None), None)
    return out


def _aggregate_regions(results: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted({
        key
        for result in results
        if isinstance(result.get("regions"), dict)
        for key in result["regions"].keys()
    })
    return {
        key: _aggregate_metric_dicts(
            result.get("regions", {}).get(key)
            for result in results
            if isinstance(result.get("regions"), dict)
        )
        for key in keys
    }


def _aggregate_metric_dicts(items: Iterable[Any]) -> dict[str, Any]:
    dicts = [item for item in items if isinstance(item, dict) and item.get("valid")]
    if not dicts:
        return {"valid": False, "pixels": 0}
    out: dict[str, Any] = {"valid": True}
    numeric_keys = sorted({
        key
        for item in dicts
        for key, value in item.items()
        if isinstance(value, (int, float)) and key != "valid"
    })
    for key in numeric_keys:
        out[key] = _mean(item.get(key, math.inf) for item in dicts)
    vector_keys = sorted({
        key
        for item in dicts
        for key, value in item.items()
        if isinstance(value, list) and value and all(isinstance(v, (int, float)) for v in value)
    })
    for key in vector_keys:
        max_len = max(len(item.get(key, [])) for item in dicts)
        out[key] = _mean_vector((item.get(key) for item in dicts), max_len)
    return out


def _resolve_view_fit_score(analysis: dict[str, Any], diff_score: float, mode: str) -> float:
    if mode == "human_accept":
        value = analysis.get("human_accept_score")
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return max(0.0, min(1.0, float(value)))
    if mode in ("human_accept", "perceptual"):
        value = analysis.get("perceptual_fit_score")
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return max(0.0, min(1.0, float(value)))
    if not math.isfinite(diff_score):
        return -math.inf
    mae = max(0.0, float(diff_score))
    if mode == "perceptual":
        return max(0.0, min(1.0, 1.0 - math.sqrt(mae * 4.0)))
    return max(0.0, min(1.0, 1.0 - mae))


def _finite_float(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _optional_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _mean_vector(values: Iterable[Any], length: int) -> list[float]:
    vectors = [
        [float(item) for item in value[:length]]
        for value in values
        if isinstance(value, list) and len(value) >= length and all(isinstance(item, (int, float)) for item in value[:length])
    ]
    if not vectors:
        return [0.0] * length
    return [sum(vector[index] for vector in vectors) / len(vectors) for index in range(length)]


def _min_finite(values: Iterable[float], *, default: float) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return min(finite) if finite else default


def _max_finite(values: Iterable[float], *, default: float) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return max(finite) if finite else default


def _percentile_finite(values: Iterable[float], percentile: float, *, default: float) -> float:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return default
    if len(finite) == 1:
        return finite[0]
    position = (len(finite) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return finite[int(position)]
    ratio = position - lower
    return finite[lower] * (1.0 - ratio) + finite[upper] * ratio


def _worst_fit_view(views: list[dict[str, Any]]) -> dict[str, Any] | None:
    finite = [view for view in views if math.isfinite(_finite_float(view.get("fit_score"), -math.inf))]
    if not finite:
        return None
    return min(finite, key=lambda view: _finite_float(view.get("fit_score"), math.inf))


def _mean(values: Iterable[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return sum(finite) / len(finite) if finite else math.inf


def _worst_severity(values: Iterable[str]) -> str:
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    worst = "none"
    for value in values:
        if order.get(value, 0) > order[worst]:
            worst = value
    return worst


def config_to_dict(config: ImageDiffConfig) -> dict[str, Any]:
    return asdict(config)
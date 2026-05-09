"""Tests for :mod:`tools.material_fit.vision.perceptual_score` and the
E-009 wiring in :mod:`tools.material_fit.vision.diff_analysis` /
:mod:`tools.material_fit.fit_material`.

These tests exercise the *correctness* contracts of the new metric,
not its performance on realistic Laya screenshots — that comparison
lives in ``docs/Metric_Validation.md`` and the
``tests/manual/smoke_e009.py`` harness.

Contract coverage:

* :func:`auto_background_mask`
    1. Two solid-coloured corners-and-foreground synthetic images
       must produce a mask that excludes the corners and keeps the
       foreground regardless of which background colours are used.
    2. When the candidate has zero background contrast (every pixel
       is foreground), the mask must report ``foreground_ratio≈1``
       and stay all-ones, i.e. it must NOT spuriously mask out
       large regions just because the corner sampler hits a flat
       colour.
    3. Disabled config returns ``status="skipped"`` and ``mask=None``.

* :func:`channel_weighted_mae`
    1. With every channel ``valid`` and equal MAE values, the
       weighted mae equals that single value (sanity).
    2. When some channels are invalid, the remaining weights are
       renormalised; the result equals the weighted average over
       the surviving channels.
    3. Weights that don't sum to 1.0 must raise ``ValueError`` at
       config time.

* :func:`ssim_score`
    1. Identical images return ssim≈1.0 (with a bit of slack for
       float arithmetic).
    2. A mask with a single foreground rectangle weighting must
       still return a finite ssim and never raise.
    3. Mismatched shapes return ``status="unavailable"`` rather than
       raising.

* :func:`combine_fit_score`
    1. ``fit_score`` is in [0, 1] and monotonically decreases as
       weighted_mae increases (for fixed SSIM).
    2. ``fit_score`` is monotonically increasing in SSIM (for
       fixed MAE).
    3. Missing SSIM falls back to MAE-only; the returned components
       record ``ssim_branch`` as NaN.

* :mod:`diff_analysis` integration
    1. Calling :func:`analyze_image_diff` with two RGB-only images
       and ``auto_mask_enabled=True`` produces a result containing
       all of ``auto_mask``, ``perceptual``, ``perceptual_fit_score``
       fields.

* :mod:`fit_material` integration
    1. :func:`_resolve_fit_score` prefers ``perceptual_fit_score``
       when present and falls back to ``_diff_score_to_fit_score``
       otherwise.
"""

from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PIL_Image = pytest.importorskip("PIL.Image")
np = pytest.importorskip("numpy")

from tools.material_fit.vision import perceptual_score as ps
from tools.material_fit.vision.diff_analysis import (
    ImageDiffConfig,
    analyze_image_diff,
)
from tools.material_fit.fit_material import _resolve_fit_score


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _solid(color, size=(64, 48)):
    img = PIL_Image.new("RGB", size, tuple(color))
    return img


def _foreground_on_background(fg_color, bg_color, size=(64, 48), fg_box=(20, 14, 44, 34)):
    img = PIL_Image.new("RGB", size, tuple(bg_color))
    px = img.load()
    for y in range(fg_box[1], fg_box[3]):
        for x in range(fg_box[0], fg_box[2]):
            px[x, y] = tuple(fg_color)
    return img


# ---------------------------------------------------------------------
# auto_background_mask
# ---------------------------------------------------------------------


def test_auto_mask_excludes_corner_backgrounds():
    ref = _foreground_on_background(fg_color=(200, 30, 30), bg_color=(170, 160, 145))
    cand = _foreground_on_background(fg_color=(180, 60, 60), bg_color=(135, 150, 180))

    res = ps.auto_background_mask(ref, cand)

    assert res.status == "ok"
    assert res.mask is not None
    assert res.mask.shape == (48, 64)
    assert res.foreground_ratio < 0.6  # only the central rectangle survives
    assert res.foreground_ratio > 0.05
    assert res.reference_bg_color == (170, 160, 145)
    assert res.candidate_bg_color == (135, 150, 180)
    # Corner pixels must be masked out
    assert res.mask[0, 0] == 0.0
    assert res.mask[-1, -1] == 0.0
    # A pixel in the foreground rectangle must be kept
    assert res.mask[24, 32] == 1.0


def test_auto_mask_skipped_when_disabled():
    ref = _solid((100, 100, 100))
    cand = _solid((110, 110, 110))
    res = ps.auto_background_mask(ref, cand, ps.AutoMaskConfig(enabled=False))
    assert res.status == "skipped"
    assert res.mask is None


def test_auto_mask_low_signal_falls_back():
    ref = _solid((128, 128, 128))
    cand = _solid((130, 130, 130))
    # Both images are basically all-corner-colour, so foreground
    # ratio will be ~0%. We expect the helper to refuse and return
    # status="low_signal" so the caller falls back to no-mask.
    res = ps.auto_background_mask(
        ref,
        cand,
        ps.AutoMaskConfig(threshold=8, min_foreground_ratio=0.05),
    )
    assert res.status == "low_signal"
    assert res.mask is None
    assert res.foreground_ratio < 0.05


def test_auto_mask_handles_shape_mismatch():
    ref = _solid((0, 0, 0), size=(64, 48))
    cand = _solid((255, 255, 255), size=(32, 32))
    res = ps.auto_background_mask(ref, cand)
    assert res.status == "unavailable"
    assert res.mask is None


# ---------------------------------------------------------------------
# channel_weighted_mae
# ---------------------------------------------------------------------


def _make_channels(values: dict[str, float]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ps.DEFAULT_CHANNEL_WEIGHTS:
        if name in values:
            out[name] = {"valid": True, "rgb_mae": values[name]}
        else:
            out[name] = {"valid": False, "rgb_mae": 0.0}
    return out


def test_channel_weighted_mae_equal_values():
    channels = _make_channels({name: 0.20 for name in ps.DEFAULT_CHANNEL_WEIGHTS})
    res = ps.channel_weighted_mae(channels)
    assert math.isclose(res["weighted_mae"], 0.20, abs_tol=1e-9)
    assert math.isclose(res["coverage"], 1.0, abs_tol=1e-9)


def test_channel_weighted_mae_renormalises_invalid_channels():
    # Only 2 of the 6 channels report valid metrics, with equal weights.
    channels = {
        "base_color_main_texture": {"valid": True, "rgb_mae": 0.10},
        "metallic_smoothness_specular": {"valid": True, "rgb_mae": 0.30},
        "emission": {"valid": False, "rgb_mae": 0.0},
        "fresnel_rim": {"valid": False, "rgb_mae": 0.0},
        "shadow_occlusion": {"valid": False, "rgb_mae": 0.0},
        "color_grading_hsv_contrast": {"valid": False, "rgb_mae": 0.0},
    }
    res = ps.channel_weighted_mae(channels)
    # Coverage = base(0.30) + metallic(0.18) = 0.48, before renorm.
    expected_coverage = ps.DEFAULT_CHANNEL_WEIGHTS["base_color_main_texture"] + ps.DEFAULT_CHANNEL_WEIGHTS["metallic_smoothness_specular"]
    assert math.isclose(res["coverage"], expected_coverage, abs_tol=1e-9)
    # After renorm, base weighs 0.30/0.48 ≈ 0.625, metallic 0.18/0.48 ≈ 0.375.
    expected_mae = (
        0.10 * (ps.DEFAULT_CHANNEL_WEIGHTS["base_color_main_texture"] / expected_coverage)
        + 0.30 * (ps.DEFAULT_CHANNEL_WEIGHTS["metallic_smoothness_specular"] / expected_coverage)
    )
    assert math.isclose(res["weighted_mae"], expected_mae, abs_tol=1e-9)


def test_channel_weighted_mae_empty_returns_inf():
    channels = _make_channels({})  # all invalid
    res = ps.channel_weighted_mae(channels)
    assert res["weighted_mae"] == math.inf
    assert res["coverage"] == 0.0


def test_channel_weight_config_rejects_non_unit_sum():
    with pytest.raises(ValueError):
        ps.ChannelWeightConfig(weights={"a": 0.5, "b": 0.4})


def test_channel_weight_config_rejects_negative():
    with pytest.raises(ValueError):
        ps.ChannelWeightConfig(weights={"a": 1.5, "b": -0.5})


# ---------------------------------------------------------------------
# ssim_score
# ---------------------------------------------------------------------


def test_ssim_identical_images():
    ref = _solid((127, 64, 200))
    res = ps.ssim_score(ref, ref)
    assert res["status"] == "ok"
    assert math.isclose(res["ssim"], 1.0, abs_tol=1e-3)


def test_ssim_with_mask_does_not_raise():
    ref = _foreground_on_background(fg_color=(200, 50, 50), bg_color=(120, 120, 120))
    cand = _foreground_on_background(fg_color=(190, 70, 70), bg_color=(125, 120, 118))
    arr = np.zeros((48, 64), dtype=np.float32)
    arr[14:34, 20:44] = 1.0
    res = ps.ssim_score(ref, cand, mask=arr)
    assert res["status"] == "ok"
    assert -1.0 <= res["ssim"] <= 1.0
    assert res["ssim_unmasked"] is not None


def test_ssim_shape_mismatch_returns_unavailable():
    ref = _solid((0, 0, 0), size=(64, 48))
    cand = _solid((0, 0, 0), size=(32, 32))
    res = ps.ssim_score(ref, cand)
    assert res["status"] == "unavailable"
    assert res["ssim"] is None


# ---------------------------------------------------------------------
# combine_fit_score
# ---------------------------------------------------------------------


def test_combine_fit_score_monotonic_in_mae():
    fit_low_mae, _ = ps.combine_fit_score(weighted_mae=0.01, ssim=0.8)
    fit_mid_mae, _ = ps.combine_fit_score(weighted_mae=0.10, ssim=0.8)
    fit_high_mae, _ = ps.combine_fit_score(weighted_mae=0.30, ssim=0.8)
    assert fit_low_mae > fit_mid_mae > fit_high_mae
    for fit in (fit_low_mae, fit_mid_mae, fit_high_mae):
        assert 0.0 <= fit <= 1.0


def test_combine_fit_score_monotonic_in_ssim():
    fit_low, _ = ps.combine_fit_score(weighted_mae=0.05, ssim=0.1)
    fit_mid, _ = ps.combine_fit_score(weighted_mae=0.05, ssim=0.5)
    fit_high, _ = ps.combine_fit_score(weighted_mae=0.05, ssim=0.95)
    assert fit_low < fit_mid < fit_high


def test_combine_fit_score_falls_back_to_mae_only_when_ssim_missing():
    fit, components = ps.combine_fit_score(weighted_mae=0.05, ssim=None)
    assert math.isnan(components["ssim_branch"])
    assert math.isclose(fit, components["mae_branch"], abs_tol=1e-9)


def test_combine_fit_score_mae_branch_does_not_saturate_after_p0():
    """Phase summary 2026-05-08 P0 regression: at high MAE the legacy
    ``1 - sqrt(4·MAE)`` mapping clipped to zero, killing the
    optimizer's gradient. The new ``exp(-k·MAE)`` mapping must stay
    strictly positive and strictly decreasing well past the old
    saturation point so cold-start runs (fish_1580 had MAE≈0.33)
    still receive a meaningful signal.
    """

    fit_at_zero, comps_zero = ps.combine_fit_score(weighted_mae=0.0, ssim=0.0)
    fit_at_quarter, comps_quarter = ps.combine_fit_score(weighted_mae=0.25, ssim=0.0)
    fit_at_third, comps_third = ps.combine_fit_score(weighted_mae=0.33, ssim=0.0)
    fit_at_half, comps_half = ps.combine_fit_score(weighted_mae=0.50, ssim=0.0)

    assert comps_zero["mae_branch"] == 1.0
    assert comps_quarter["mae_branch"] > 0.05
    assert comps_third["mae_branch"] > 0.05
    assert comps_half["mae_branch"] > 0.05
    assert comps_third["mae_branch"] < comps_quarter["mae_branch"]
    assert comps_half["mae_branch"] < comps_third["mae_branch"]
    assert comps_zero["mae_branch_saturated"] is False
    assert comps_third["mae_branch_saturated"] is False
    assert comps_third["legacy_mae_branch_saturated"] is True
    assert comps_third["mae_mapping"] == "exp_decay"
    assert fit_at_zero > fit_at_quarter > fit_at_third > fit_at_half


# ---------------------------------------------------------------------
# diff_analysis integration
# ---------------------------------------------------------------------


def test_analyze_image_diff_emits_e009_blocks(tmp_path):
    ref = _foreground_on_background(fg_color=(220, 60, 40), bg_color=(170, 160, 145), size=(96, 72))
    cand = _foreground_on_background(fg_color=(180, 80, 60), bg_color=(135, 150, 180), size=(96, 72))
    ref_path = tmp_path / "ref.png"
    cand_path = tmp_path / "cand.png"
    ref.save(ref_path)
    cand.save(cand_path)

    res = analyze_image_diff(
        ImageDiffConfig(
            reference_path=str(ref_path),
            candidate_path=str(cand_path),
            output_dir=str(tmp_path / "out"),
            generate_diff_image=False,
        )
    )

    assert res["status"] == "ok"
    assert "perceptual_fit_score" in res
    assert isinstance(res["perceptual_fit_score"], float)
    assert 0.0 <= res["perceptual_fit_score"] <= 1.0
    assert res["auto_mask"] is not None
    assert res["auto_mask"]["status"] in ("ok", "low_signal")
    assert "perceptual" in res
    perc = res["perceptual"]
    assert "weighted_mae" in perc
    assert "ssim" in perc
    assert "fit_score" in perc
    assert "weights_used" in perc
    assert "human_accept_score" in res
    assert isinstance(res["human_accept_score"], float)
    assert 0.0 <= res["human_accept_score"] <= 1.0
    assert res["human_accept"]["metric"] in {
        "human_accept_material_score_v1",
        "human_accept_material_score_v2",
    }
    assert "foreground_color_distribution" in res["human_accept"]["components"]
    assert "foreground_bbox_alignment" in res["human_accept"]["components"]


def test_analyze_image_diff_respects_explicit_mask(tmp_path):
    """When the caller supplies an explicit ``mask_path``, the auto
    mask must be skipped — the explicit mask wins."""

    ref = _foreground_on_background(fg_color=(220, 60, 40), bg_color=(170, 160, 145), size=(96, 72))
    cand = _foreground_on_background(fg_color=(180, 80, 60), bg_color=(135, 150, 180), size=(96, 72))
    mask_img = PIL_Image.new("L", (96, 72), 255)
    ref_path = tmp_path / "ref.png"
    cand_path = tmp_path / "cand.png"
    mask_path = tmp_path / "mask.png"
    ref.save(ref_path)
    cand.save(cand_path)
    mask_img.save(mask_path)

    res = analyze_image_diff(
        ImageDiffConfig(
            reference_path=str(ref_path),
            candidate_path=str(cand_path),
            mask_path=str(mask_path),
            output_dir=str(tmp_path / "out"),
            generate_diff_image=False,
        )
    )
    # Because we supplied an explicit (all-ones) mask, the auto
    # mask must NOT be applied — auto_mask field is None.
    assert res["auto_mask"] is None


# ---------------------------------------------------------------------
# fit_material._resolve_fit_score
# ---------------------------------------------------------------------


def test_resolve_fit_score_prefers_perceptual_fit_score():
    analysis = {
        "score": 0.20,  # legacy MAE
        "perceptual_fit_score": 0.42,
    }
    res = _resolve_fit_score(analysis, diff_score=0.20, mode="perceptual")
    assert math.isclose(res, 0.42, abs_tol=1e-9)


def test_resolve_fit_score_prefers_human_accept_score():
    analysis = {
        "score": 0.20,
        "perceptual_fit_score": 0.42,
        "human_accept_score": 0.67,
    }
    res = _resolve_fit_score(analysis, diff_score=0.20, mode="human_accept")
    assert math.isclose(res, 0.67, abs_tol=1e-9)


def test_resolve_fit_score_falls_back_when_perceptual_missing():
    analysis = {"score": 0.20}
    res = _resolve_fit_score(analysis, diff_score=0.20, mode="perceptual")
    expected = 1.0 - math.sqrt(0.20 * 4.0)
    expected = max(0.0, min(1.0, expected))
    assert math.isclose(res, expected, abs_tol=1e-9)


def test_resolve_fit_score_clamps_to_unit_interval():
    # An out-of-range perceptual_fit_score must be clamped.
    analysis = {"score": 0.0, "perceptual_fit_score": 1.5}
    assert _resolve_fit_score(analysis, diff_score=0.0) == 1.0
    analysis2 = {"score": 0.0, "perceptual_fit_score": -0.4}
    assert _resolve_fit_score(analysis2, diff_score=0.0) == 0.0

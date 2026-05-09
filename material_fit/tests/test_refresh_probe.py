"""Tests for :mod:`tools.material_fit.laya.refresh_probe`.

We mock the Laya viewport with a programmable ``capture`` callable
that returns a synthetic PNG of our choice for each call. This lets
us exercise four real-world failure modes without an actual Laya
editor:

1. **Happy path** — capture goes baseline-grey → probe-magenta →
   restored-grey. The probe should report ``success=True``.
2. **Laya is frozen** — capture returns the SAME baseline frame for
   all three steps. The probe must report ``detected_change=False``
   and surface a clear "Laya is NOT refreshing" reason.
3. **Restore failed** — probe captures magenta, but restored capture
   is also magenta (i.e., Laya did refresh on the probe write but
   somehow not on the restore, OR the .lmat backup didn't work). The
   probe must flag this as a non-success state and tell the user
   their .lmat is in an unknown state.
4. **.lmat write rejected** — the probe param is not present on the
   .lmat. The probe must fail BEFORE any capture is taken, leaving
   the .lmat untouched.

We also verify the magenta ratio detector by feeding it solid-colour
PNGs (pure magenta = 1.0, pure grey = 0.0, half-magenta-half-grey ≈
0.5).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PIL_Image = pytest.importorskip("PIL.Image")  # noqa: N816 — pytest's importorskip uses the module name

from tools.material_fit.laya import lmat_io  # noqa: E402
from tools.material_fit.laya import refresh_probe  # noqa: E402
from tools.material_fit.laya.refresh_probe import (  # noqa: E402
    ProbeConfig,
    magenta_ratio,
    run_refresh_probe,
)
from tools.material_fit.shared.models import ShaderParam  # noqa: E402


# ---------------------------------------------------------------------
# fixtures


def _shader_params() -> list[ShaderParam]:
    return [
        ShaderParam("u_BaseColor", "Color", default=[1, 1, 1, 1]),
        ShaderParam("u_Gamma_Power", "Range", default=1.0, range_min=0.05, range_max=10.0),
    ]


def _write_minimal_lmat(path: Path) -> None:
    """Create an .lmat with just enough structure for lmat_io.write_candidate_lmat."""
    payload = {
        "type": "Material",
        "version": "LAYAMATERIAL:04",
        "props": {
            "_$shader": "fish/FishStandard",
            "type": "Laya.Material",
            "u_BaseColor": [0.8, 0.6, 0.4, 1.0],
            "u_Gamma_Power": 2.2,
            "textures": [],
            "defines": [],
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_solid_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> Path:
    img = PIL_Image.new("RGB", size, color)
    img.save(path, "PNG")
    return path


def _write_sparse_probe_png(
    path: Path,
    *,
    base: tuple[int, int, int] = (128, 128, 128),
    probe: tuple[int, int, int] = (255, 0, 255),
    changed_pixels: int = 10,
    size: tuple[int, int] = (32, 32),
) -> Path:
    img = PIL_Image.new("RGB", size, base)
    width, height = size
    for index in range(min(changed_pixels, width * height)):
        img.putpixel((index % width, index // width), probe)
    img.save(path, "PNG")
    return path


# ---------------------------------------------------------------------
# magenta_ratio


def test_magenta_ratio_solid_magenta_is_one(tmp_path: Path):
    p = _write_solid_png(tmp_path / "m.png", (255, 0, 255))
    assert magenta_ratio(p) == pytest.approx(1.0)


def test_magenta_ratio_solid_grey_is_zero(tmp_path: Path):
    p = _write_solid_png(tmp_path / "g.png", (128, 128, 128))
    assert magenta_ratio(p) == pytest.approx(0.0)


def test_magenta_ratio_half_and_half(tmp_path: Path):
    img = PIL_Image.new("RGB", (32, 32), (128, 128, 128))
    # Paint the right half magenta.
    for x in range(16, 32):
        for y in range(32):
            img.putpixel((x, y), (255, 0, 255))
    p = tmp_path / "half.png"
    img.save(p, "PNG")
    ratio = magenta_ratio(p)
    assert 0.45 < ratio < 0.55


def test_magenta_ratio_amber_fish_color_is_zero(tmp_path: Path):
    """The fish_1580 reference is amber (~ R 220, G 160, B 60). Must not
    register as magenta — otherwise the probe's signal would have no
    headroom to grow above the baseline."""
    p = _write_solid_png(tmp_path / "amber.png", (220, 160, 60))
    assert magenta_ratio(p) == pytest.approx(0.0)


def test_magenta_ratio_unreadable_returns_zero(tmp_path: Path):
    bogus = tmp_path / "missing.png"
    assert magenta_ratio(bogus) == 0.0


# ---------------------------------------------------------------------
# run_refresh_probe — happy path


def test_run_refresh_probe_happy_path(tmp_path: Path):
    """Capture returns: grey → magenta → grey. Probe must succeed and
    leave the .lmat in its original state."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    original_bytes = lmat_path.read_bytes()

    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    grey = _write_solid_png(capture_dir / "grey.png", (128, 128, 128))
    magenta = _write_solid_png(capture_dir / "magenta.png", (255, 0, 255))

    sequence = {"baseline": grey, "probe": magenta, "restored": grey}

    def capture(step: str) -> Path:
        return sequence[step]

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=capture,
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is True
    assert result.detected_change is True
    assert result.detected_restore is True
    # Primary signal: large baseline→probe diff, near-zero baseline→restored.
    assert result.mean_diff_baseline_probe > 50.0  # grey vs magenta is huge
    assert result.mean_diff_baseline_restored == pytest.approx(0.0)
    # Magenta-ratio side-signal still informative.
    assert result.magenta_ratio_baseline == pytest.approx(0.0)
    assert result.magenta_ratio_probe == pytest.approx(1.0)
    assert result.magenta_ratio_restored == pytest.approx(0.0)
    # .lmat has been restored byte-for-byte.
    assert lmat_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------
# run_refresh_probe — Laya frozen (the bug we're hunting for)


def test_run_refresh_probe_detects_frozen_laya(tmp_path: Path):
    """Capture always returns the same grey frame regardless of the
    .lmat content. This is what we'd see if Laya doesn't watch the
    .lmat file or our rerender_wait_ms is way too short. The probe
    must NOT mark this as success."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    original_bytes = lmat_path.read_bytes()

    grey = _write_solid_png(tmp_path / "grey.png", (128, 128, 128))

    def frozen_capture(_step: str) -> Path:
        return grey

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=frozen_capture,
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is False
    assert result.detected_change is False
    # Reason text must contain something users can act on.
    reason_l = result.reason.lower()
    assert "not refreshing" in reason_l or "did not visibly change" in reason_l
    # Mean color diff must be 0 since all three frames are identical.
    assert result.mean_diff_baseline_probe == pytest.approx(0.0)
    assert result.mean_diff_baseline_restored == pytest.approx(0.0)
    assert result.mean_diff_probe_restored == pytest.approx(0.0)
    # Backup should still exist on disk and original .lmat should be unchanged.
    assert lmat_path.read_bytes() == original_bytes


def test_run_refresh_probe_reports_weak_signal_below_threshold_without_frozen_label(tmp_path: Path):
    """A small foreground change can be real but diluted by full-frame
    averaging. It should fail the configured threshold without claiming
    the three captures are identical."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)

    baseline = _write_solid_png(tmp_path / "baseline.png", (128, 128, 128))
    probe = _write_sparse_probe_png(tmp_path / "probe.png")
    sequence = {"baseline": baseline, "probe": probe, "restored": baseline}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        capture=lambda step: sequence[step],
        config=ProbeConfig(rerender_wait_ms=0, mean_diff_change_threshold=1.5),
        sleep_fn=lambda _: None,
    )

    assert result.success is False
    assert result.detected_change is False
    assert result.detected_restore is True
    assert 1.0 < result.mean_diff_baseline_probe < 1.5
    reason_l = result.reason.lower()
    assert "not produce enough color difference" in reason_l
    assert "not refreshing" not in reason_l


def test_run_refresh_probe_default_threshold_accepts_diluted_visible_change(tmp_path: Path):
    """The default threshold is intentionally low: a small but real
    foreground change should be enough to prove Laya refreshed."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)

    baseline = _write_solid_png(tmp_path / "baseline.png", (128, 128, 128))
    probe = _write_sparse_probe_png(tmp_path / "probe.png")
    sequence = {"baseline": baseline, "probe": probe, "restored": baseline}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        capture=lambda step: sequence[step],
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is True, result.reason
    assert result.detected_change is True
    assert result.detected_restore is True
    assert 1.0 < result.mean_diff_baseline_probe < 1.5


def test_run_refresh_probe_zero_threshold_accepts_any_nonzero_difference(tmp_path: Path):
    """When the user sets the threshold to 0, any non-identical probe
    frame is enough to count as refreshed."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)

    baseline = _write_solid_png(tmp_path / "baseline.png", (128, 128, 128))
    probe = _write_sparse_probe_png(tmp_path / "probe.png", changed_pixels=1)
    sequence = {"baseline": baseline, "probe": probe, "restored": baseline}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        capture=lambda step: sequence[step],
        config=ProbeConfig(rerender_wait_ms=0, mean_diff_change_threshold=0.0),
        sleep_fn=lambda _: None,
    )

    assert result.success is True, result.reason
    assert result.detected_change is True
    assert result.mean_diff_baseline_probe > 0.0


# ---------------------------------------------------------------------
# run_refresh_probe — restore failed


def test_run_refresh_probe_detects_failed_restore(tmp_path: Path):
    """Capture sequence: grey → magenta → magenta. The probe write
    visibly registered, but the restore did not. Must flag as
    non-success and the reason must mention .lmat is in an unknown
    state."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)

    grey = _write_solid_png(tmp_path / "grey.png", (128, 128, 128))
    magenta = _write_solid_png(tmp_path / "magenta.png", (255, 0, 255))

    sequence = {"baseline": grey, "probe": magenta, "restored": magenta}

    def capture(step: str) -> Path:
        return sequence[step]

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=capture,
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is False
    assert result.detected_change is True
    assert result.detected_restore is False
    assert "unknown state" in result.reason.lower() or "did not undo" in result.reason.lower()


# ---------------------------------------------------------------------
# run_refresh_probe — preflight check before any capture


def test_run_refresh_probe_aborts_when_probe_param_missing(tmp_path: Path):
    """If the .lmat doesn't even contain the probe param, the probe
    must fail BEFORE writing anything or capturing. The .lmat must be
    untouched, and the capture callable must not have been called."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    original_bytes = lmat_path.read_bytes()

    captured_steps: list[str] = []

    def capture(step: str) -> Path:
        captured_steps.append(step)
        raise AssertionError(f"capture should not be called; step={step!r}")

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=capture,
        config=ProbeConfig(probe_param="u_BogusColor", rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is False
    assert "u_BogusColor" in result.reason
    assert captured_steps == []
    assert lmat_path.read_bytes() == original_bytes


def test_run_refresh_probe_aborts_when_lmat_missing(tmp_path: Path):
    captured_steps: list[str] = []

    def capture(step: str) -> Path:
        captured_steps.append(step)
        raise AssertionError("should not be called")

    result = run_refresh_probe(
        laya_material_path=tmp_path / "does_not_exist.lmat",
        laya_shader_params=_shader_params(),
        capture=capture,
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )
    assert result.success is False
    assert "not found" in result.reason
    assert captured_steps == []


# ---------------------------------------------------------------------
# run_refresh_probe — recovery on capture exception


def test_run_refresh_probe_restores_on_capture_exception(tmp_path: Path):
    """If the capture callable raises mid-flight (e.g., user closed
    Laya), the probe must still attempt to restore the .lmat from
    backup before reporting failure."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    original_bytes = lmat_path.read_bytes()

    grey = _write_solid_png(tmp_path / "grey.png", (128, 128, 128))
    call_count = {"n": 0}

    def flaky_capture(step: str) -> Path:
        call_count["n"] += 1
        if step == "probe":
            raise RuntimeError("Laya viewport disappeared")
        return grey

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=flaky_capture,
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is False
    assert "Laya viewport disappeared" in (result.error or "")
    # The most important property: the user's .lmat is intact even
    # though the probe crashed mid-pipeline.
    assert lmat_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------
# run_refresh_probe — round-trip integration with lmat_io


def test_run_refresh_probe_does_not_corrupt_lmat_on_success(tmp_path: Path):
    """The .lmat must round-trip through extract_params / write_candidate_lmat
    cleanly, and the structural diff between the original and the
    end-of-probe state must be empty."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    original_data = lmat_io.load_lmat(lmat_path)

    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    grey = _write_solid_png(capture_dir / "grey.png", (128, 128, 128))
    magenta = _write_solid_png(capture_dir / "magenta.png", (255, 0, 255))
    seq = {"baseline": grey, "probe": magenta, "restored": grey}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=lambda step: seq[step],
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    assert result.success is True
    final_data = lmat_io.load_lmat(lmat_path)
    diff = lmat_io.diff_shapes(original_data, final_data)
    assert diff == [], f"Expected zero structural diff after probe, got {diff!r}"


# ---------------------------------------------------------------------
# run_refresh_probe — focus_callback hook (E-007)


def _build_grey_magenta_grey_capture(tmp_path: Path):
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    grey = _write_solid_png(capture_dir / "grey.png", (128, 128, 128))
    magenta = _write_solid_png(capture_dir / "magenta.png", (255, 0, 255))
    seq = {"baseline": grey, "probe": magenta, "restored": grey}

    def capture(step: str) -> Path:
        return seq[step]

    return capture


def test_focus_callback_invoked_at_all_five_phases(tmp_path: Path):
    """Every phase that depends on Laya being responsive — three captures
    plus two .lmat writes — must trigger the focus hook. Anything less
    leaves a window of opportunity for Laya to slip into the background
    and start serving stale frames."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)

    focus_calls: list[str] = []

    def focus(step: str) -> dict:
        focus_calls.append(step)
        return {"success": True, "reason": "ok", "title": "fish"}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=_build_grey_magenta_grey_capture(tmp_path),
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
        focus=focus,
    )

    assert result.success is True
    # Order matters: write phases must come BEFORE their corresponding
    # capture, otherwise we'd be focusing AFTER Laya had already failed
    # to react to the file change.
    assert focus_calls == [
        "before_baseline_capture",
        "before_probe_write",
        "before_probe_capture",
        "before_restore_write",
        "before_restored_capture",
    ]
    # focus_log on the result lets the UI surface failures per-step.
    assert len(result.focus_log) == 5
    assert all(entry.get("success") is True for entry in result.focus_log)
    assert {entry["step"] for entry in result.focus_log} == set(focus_calls)


def test_focus_callback_failure_does_not_abort_probe(tmp_path: Path):
    """A focus call returning success=False (e.g., Laya window not found)
    must not crash the probe — it just gets recorded in focus_log so
    the user can diagnose. The probe continues so the user can still
    see whether Laya happened to be focused already."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)

    def focus(step: str) -> dict:
        return {"success": False, "reason": "no laya window found"}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=_build_grey_magenta_grey_capture(tmp_path),
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
        focus=focus,
    )

    # Probe still completed (capture was happy regardless of focus).
    assert result.success is True
    assert len(result.focus_log) == 5
    assert all(entry.get("success") is False for entry in result.focus_log)


def test_focus_callback_exception_isolated_to_focus_log(tmp_path: Path):
    """A focus hook that raises must not propagate into the probe —
    we catch and record the failure so a buggy focus implementation
    can't break the more important .lmat-write/restore safety guarantees."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    original_bytes = lmat_path.read_bytes()

    def angry_focus(step: str) -> dict:
        raise RuntimeError(f"win32 boom on {step}")

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=_build_grey_magenta_grey_capture(tmp_path),
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
        focus=angry_focus,
    )

    assert result.success is True  # capture sequence is still valid
    assert len(result.focus_log) == 5
    for entry in result.focus_log:
        assert entry["success"] is False
        assert "win32 boom" in entry["reason"]
    # Crucially the .lmat must still round-trip cleanly.
    assert lmat_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------
# mean_color_diff — primary detection signal


def test_mean_color_diff_identical_images_is_zero(tmp_path: Path):
    """When Laya is frozen, both captures are byte-identical → diff=0.
    This is the foundational signal of the new detector."""
    from tools.material_fit.laya.refresh_probe import mean_color_diff
    p = _write_solid_png(tmp_path / "a.png", (180, 40, 70))
    assert mean_color_diff(p, p) == pytest.approx(0.0)


def test_mean_color_diff_grey_vs_magenta_is_large(tmp_path: Path):
    """Saturation extremes — grey vs pure magenta is the biggest
    possible sane diff and must register way above any threshold."""
    from tools.material_fit.laya.refresh_probe import mean_color_diff
    grey = _write_solid_png(tmp_path / "g.png", (128, 128, 128))
    mag = _write_solid_png(tmp_path / "m.png", (255, 0, 255))
    diff = mean_color_diff(grey, mag)
    # Per-channel: |255-128| + |0-128| + |255-128| = 127+128+127=382 over 3 = 127.3
    assert 120.0 < diff < 130.0


def test_mean_color_diff_dark_red_to_magenta_modulation(tmp_path: Path):
    """User's real case: a dark-red textured surface gets multiplied by
    [1, 0, 1] base color → R kept, G zeroed, B kept. The resulting
    image is NOT pure magenta (so the legacy magenta_ratio detector
    misses it) but the mean diff is comfortably above the default
    threshold of 1.5. This is the bug we're fixing in this commit."""
    from tools.material_fit.laya.refresh_probe import mean_color_diff
    base = _write_solid_png(tmp_path / "base.png", (180, 40, 70))
    # After u_BaseColor=[1,0,1,1] modulates a dark-red texture:
    probed = _write_solid_png(tmp_path / "probed.png", (180, 0, 70))
    diff = mean_color_diff(base, probed)
    # Only G channel changed by 40 → mean = 40/3 ≈ 13.3
    assert 10.0 < diff < 20.0
    # Crucially: way above the 1.5 default change threshold.
    assert diff > 1.5


def test_mean_color_diff_subpixel_jpeg_noise_under_threshold(tmp_path: Path):
    """If two captures differ by <1 per channel (rounding/JPEG-style
    noise), the diff stays under the default 1.5 threshold so we
    don't false-positive on identical renders."""
    from tools.material_fit.laya.refresh_probe import mean_color_diff
    a = _write_solid_png(tmp_path / "a.png", (180, 40, 70))
    b = _write_solid_png(tmp_path / "b.png", (181, 40, 71))
    diff = mean_color_diff(a, b)
    # Per-channel: 1 + 0 + 1 = 2 over 3 ≈ 0.67
    assert diff < 1.5


def test_mean_color_diff_handles_missing_file(tmp_path: Path):
    """Robust to missing files — returns 0 instead of raising."""
    from tools.material_fit.laya.refresh_probe import mean_color_diff
    real = _write_solid_png(tmp_path / "a.png", (128, 128, 128))
    assert mean_color_diff(real, tmp_path / "missing.png") == 0.0


def test_mean_color_diff_handles_size_mismatch(tmp_path: Path):
    """If two captures have different sizes (window resize during
    probe), return 0 to surface 'no change detected' to the user
    rather than fudging with rescaling."""
    from PIL import Image
    from tools.material_fit.laya.refresh_probe import mean_color_diff
    a_path = tmp_path / "a.png"
    b_path = tmp_path / "b.png"
    Image.new("RGB", (32, 32), (128, 128, 128)).save(a_path)
    Image.new("RGB", (64, 64), (255, 0, 255)).save(b_path)
    assert mean_color_diff(a_path, b_path) == 0.0


# ---------------------------------------------------------------------
# run_refresh_probe — textured-PBR realistic case


def test_run_refresh_probe_succeeds_on_dark_red_pbr_modulation(tmp_path: Path):
    """The bug we shipped this commit to fix: probe must SUCCEED for
    a textured red mecha that gets a u_BaseColor=[1,0,1,1] write.
    The resulting frame is NOT pure magenta — the legacy magenta_ratio
    threshold (10% of pixels) would FAIL — but the mean color diff
    is large enough to register."""

    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    # Same color the user actually saw in the screenshot (dark red mecha).
    base_color = (180, 40, 70)
    # u_BaseColor=[1,0,1,1] zeros the G channel → still dark, not pure magenta.
    probed_color = (180, 0, 70)
    base = _write_solid_png(capture_dir / "base.png", base_color)
    probed = _write_solid_png(capture_dir / "probed.png", probed_color)
    seq = {"baseline": base, "probe": probed, "restored": base}

    result = run_refresh_probe(
        laya_material_path=lmat_path,
        capture=lambda step: seq[step],
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )

    # The headline assertion: the probe SUCCEEDS even though magenta
    # ratio is essentially zero (the colour is dark, not pure magenta).
    assert result.success is True, f"reason={result.reason}"
    assert result.detected_change is True
    assert result.detected_restore is True
    # Magenta side-signal stays small — proving the new detector is
    # what carried the success, not the legacy magenta logic.
    assert result.magenta_ratio_probe < 0.05
    # Mean diff is the actual carrier of the signal.
    assert result.mean_diff_baseline_probe > 1.5


def test_focus_callback_none_means_no_focus_log(tmp_path: Path):
    """Backward-compat: callers that don't pass focus get an empty
    focus_log and otherwise unchanged behavior."""
    lmat_path = tmp_path / "m.lmat"
    _write_minimal_lmat(lmat_path)
    result = run_refresh_probe(
        laya_material_path=lmat_path,
        laya_shader_params=_shader_params(),
        capture=_build_grey_magenta_grey_capture(tmp_path),
        config=ProbeConfig(rerender_wait_ms=0),
        sleep_fn=lambda _: None,
    )
    assert result.success is True
    assert result.focus_log == []

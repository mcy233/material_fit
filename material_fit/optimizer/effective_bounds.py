from __future__ import annotations

"""Effective visual search bounds shared by optimizers and UI presets.

These ranges are not Laya engine hard limits. They are conservative visual
search intervals derived from FishStandard expert tuning and saturation-risk
review, so optimizers can explore beyond shader fallback ranges without
wandering into obviously extreme values.
"""

FISH_STANDARD_EFFECTIVE_BOUNDS: dict[str, tuple[float, float]] = {
    "u_gamma_power": (0.35, 3.0),
    "u_giintensity": (0.0, 4.0),
    "u_occlusionstrength": (0.0, 2.0),
    "u_diffusethreshold": (0.0, 2.0),
    "u_diffusesmoothness": (0.0, 3.0),
    "u_metallic": (0.0, 2.0),
    "u_smoothness": (0.0, 2.0),
    "u_metallicremapmin": (-1.0, 2.0),
    "u_metallicremapmax": (-1.0, 2.0),
    "u_smoothnessremapmin": (-1.0, 2.0),
    "u_smoothnessremapmax": (-1.0, 2.0),
    "u_specularintensity": (0.0, 12.0),
    "u_specularthreshold": (0.0, 12.0),
    "u_specularsmooth": (0.0, 6.0),
    "u_ggxspecular": (0.0, 2.0),
    "u_mlualbedocolor": (0.0, 2.0),
    "u_specularsecondintensity": (0.0, 8.0),
    "u_specularsecondthreshold": (0.0, 80.0),
    "u_iblmapintensity": (0.0, 10.0),
    "u_iblmappower": (0.1, 10.0),
    "u_iblmaprotatex": (-360.0, 360.0),
    "u_iblmaprotatey": (-360.0, 360.0),
    "u_iblmaprotatez": (-360.0, 360.0),
    "u_matcapangle": (-360.0, 360.0),
    "u_matcapstrength": (0.0, 20.0),
    "u_matcappow": (0.1, 10.0),
    "u_matcapaddangle": (-360.0, 360.0),
    "u_matcapaddstrength": (0.0, 20.0),
    "u_matcapaddpow": (0.1, 10.0),
    "u_emissionscale": (0.0, 12.0),
    "u_fresnelthreshold": (0.0, 4.0),
    "u_fresnelsmooth": (0.0, 4.0),
    "u_fresnelintensity": (0.0, 20.0),
    "u_fresneluesf0": (0.0, 2.0),
    "u_fresnelpow": (0.1, 10.0),
    "u_fresnelusemoldenormal": (0.0, 1.0),
    "u_adjusthue": (-180.0, 180.0),
    "u_adjustsaturation": (-1.0, 2.0),
    "u_adjustlightness": (-1.0, 1.0),
    "u_saturationprotection": (0.0, 1.0),
    "u_contrastscale": (-1.0, 2.0),
}


def effective_bounds_for_param(name: str) -> tuple[float, float] | None:
    return FISH_STANDARD_EFFECTIVE_BOUNDS.get(name.lower())


def effective_bounds_schema() -> dict[str, dict[str, float]]:
    return {
        name: {"range_min": bounds[0], "range_max": bounds[1]}
        for name, bounds in FISH_STANDARD_EFFECTIVE_BOUNDS.items()
    }

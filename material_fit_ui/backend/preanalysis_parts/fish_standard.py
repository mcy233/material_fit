from __future__ import annotations

from tools.material_fit.optimizer.effective_bounds import FISH_STANDARD_EFFECTIVE_BOUNDS


def fish_standard_effective_bounds_for_schema() -> dict[str, dict[str, float | str]]:
    """Return UI-schema metadata for FishStandard effective search ranges."""

    out: dict[str, dict[str, float | str]] = {}
    for lower_name, (range_min, range_max) in FISH_STANDARD_EFFECTIVE_BOUNDS.items():
        name = _canonical_fish_standard_name(lower_name)
        payload: dict[str, float | str] = {"range_min": range_min, "range_max": range_max}
        if any(token in lower_name for token in ("power", "pow")):
            payload["transform"] = "log"
        out[name] = payload
    return out


def _canonical_fish_standard_name(lower_name: str) -> str:
    known = {
        "u_gamma_power": "u_Gamma_Power",
        "u_giintensity": "u_GIIntensity",
        "u_occlusionstrength": "u_OcclusionStrength",
        "u_diffusethreshold": "u_DiffuseThreshold",
        "u_diffusesmoothness": "u_DiffuseSmoothness",
        "u_metallic": "u_Metallic",
        "u_smoothness": "u_Smoothness",
        "u_metallicremapmin": "u_MetallicRemapMin",
        "u_metallicremapmax": "u_MetallicRemapMax",
        "u_smoothnessremapmin": "u_SmoothnessRemapMin",
        "u_smoothnessremapmax": "u_SmoothnessRemapMax",
        "u_specularintensity": "u_SpecularIntensity",
        "u_specularthreshold": "u_SpecularThreshold",
        "u_specularsmooth": "u_SpecularSmooth",
        "u_ggxspecular": "u_GGXSpecular",
        "u_mlualbedocolor": "u_MluAlbedoColor",
        "u_specularsecondintensity": "u_SpecularSecondIntensity",
        "u_specularsecondthreshold": "u_SpecularSecondThreshold",
        "u_iblmapintensity": "u_IBLMapIntensity",
        "u_iblmappower": "u_IBLMapPower",
        "u_iblmaprotatex": "u_IBLMapRotateX",
        "u_iblmaprotatey": "u_IBLMapRotateY",
        "u_iblmaprotatez": "u_IBLMapRotateZ",
        "u_matcapangle": "u_MatcapAngle",
        "u_matcapstrength": "u_MatcapStrength",
        "u_matcappow": "u_MatcapPow",
        "u_matcapaddangle": "u_MatcapAddAngle",
        "u_matcapaddstrength": "u_MatcapAddStrength",
        "u_matcapaddpow": "u_MatcapAddPow",
        "u_emissionscale": "u_EmissionScale",
        "u_fresnelthreshold": "u_FresnelThreshold",
        "u_fresnelsmooth": "u_FresnelSmooth",
        "u_fresnelintensity": "u_FresnelIntensity",
        "u_fresneluesf0": "u_FresnelUesF0",
        "u_fresnelpow": "u_FresnelPow",
        "u_fresnelusemoldenormal": "u_FresnelUseMoldeNormal",
        "u_adjusthue": "u_AdjustHue",
        "u_adjustsaturation": "u_AdjustSaturation",
        "u_adjustlightness": "u_AdjustLightness",
        "u_saturationprotection": "u_saturationProtection",
        "u_contrastscale": "u_ContrastScale",
    }
    return known.get(lower_name, lower_name)

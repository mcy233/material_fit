from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.material_fit.optimizer.cma_es_optimizer import ParameterEncoder  # noqa: E402
from tools.material_fit.optimizer.group_probe import (  # noqa: E402
    evaluate_group_probe,
    generate_group_probe_candidates,
)
from tools.material_fit.optimizer.llm_semantics import (  # noqa: E402
    build_llm_semantics_context,
    validate_llm_semantics_output,
)
from tools.material_fit.optimizer.semantic_graph import (  # noqa: E402
    build_shader_effect_graph,
)
from tools.material_fit.optimizer.strategy import (  # noqa: E402
    SemanticGroupStrategy,
    StrategyContext,
)
from tools.material_fit.optimizer.adjustment_algorithm import AdjustmentState  # noqa: E402
from tools.material_fit.shared.models import ShaderDefine, ShaderParam  # noqa: E402


def _shader_params() -> list[ShaderParam]:
    return [
        ShaderParam("u_BaseColor", "Color", default=[1, 1, 1, 1]),
        ShaderParam("u_Gamma_Power", "Range", default=1.0, range_min=0.05, range_max=10.0),
        ShaderParam("u_FresnelIntensity", "Float", default=0.0, range_min=0.0, range_max=8.0),
        ShaderParam("u_FresnelColor", "Color", default=[1, 0, 0, 1]),
        ShaderParam("u_FresnelSmooth", "Range", default=0.5, range_min=0.0, range_max=1.0),
        ShaderParam("u_EmissionColor", "Color", default=[0, 0, 0, 1]),
        ShaderParam("u_EmissionScale", "Float", default=0.0, range_min=0.0, range_max=8.0),
        ShaderParam("u_AdjustHue", "Float", default=0.0, range_min=0.0, range_max=360.0),
        ShaderParam("u_BaseMap", "Texture2D", default="white"),
    ]


def _params() -> dict[str, object]:
    return {
        "u_BaseColor": [0.3, 0.2, 0.1, 1.0],
        "u_Gamma_Power": 2.2,
        "u_FresnelIntensity": 0.0,
        "u_FresnelColor": [1.0, 0.0, 0.0, 1.0],
        "u_FresnelSmooth": 0.5,
        "u_EmissionColor": [0.0, 0.0, 0.0, 1.0],
        "u_EmissionScale": 1.0,
        "u_AdjustHue": 15.0,
        "u_BaseMap": "white",
    }


def test_semantic_graph_marks_groups_gates_and_transforms():
    graph = build_shader_effect_graph(
        _shader_params(),
        shader_defines=[ShaderDefine("EMISSION"), ShaderDefine("ADJUST_HSV")],
        material_params=_params(),
        material_defines=["EMISSION"],
    )

    assert graph.params["u_Gamma_Power"].transform == "log"
    assert graph.params["u_AdjustHue"].transform == "circular"
    assert graph.params["u_BaseMap"].searchable is False
    assert graph.params["u_FresnelColor"].gates[0].name == "u_FresnelIntensity"
    assert graph.groups["fresnel"].active is False
    assert graph.groups["emission"].active is True
    assert graph.groups["color_grade"].active is False


def test_parameter_encoder_uses_semantic_log_transform_when_graph_is_provided():
    graph = build_shader_effect_graph(_shader_params(), material_params=_params())
    encoder = ParameterEncoder(_params(), _shader_params(), semantics=graph)

    gamma_axis = next(axis for axis in encoder.axes if axis.param_name == "u_Gamma_Power")
    assert gamma_axis.transform == "log"
    fresnel_axis = next(axis for axis in encoder.axes if axis.param_name == "u_FresnelIntensity")
    assert fresnel_axis.transform == "log"
    assert math.expm1(fresnel_axis.high) == pytest.approx(20.0)
    encoded = encoder.encode(_params())
    decoded = encoder.decode(encoded)
    assert decoded["u_Gamma_Power"] == pytest.approx(2.2)


def test_group_probe_candidates_activate_inactive_gated_groups_and_report_results():
    graph = build_shader_effect_graph(
        _shader_params(),
        shader_defines=[ShaderDefine("EMISSION")],
        material_params=_params(),
        material_defines=["EMISSION"],
    )
    candidates = generate_group_probe_candidates(_params(), graph)
    groups = {candidate.group for candidate in candidates}

    assert "emission" in groups
    fresnel_probe = next(candidate for candidate in candidates if candidate.group == "fresnel")
    assert fresnel_probe.changed_params == ["u_FresnelIntensity"]
    result = evaluate_group_probe(group="emission", mean_diff=1.2, threshold=0.5)
    assert result.active is True


def test_llm_semantics_validator_only_allows_known_params_and_defines():
    context = build_llm_semantics_context(
        laya_shader={"params": [{"name": "u_FresnelColor"}], "defines": [{"name": "FRESNEL"}]},
        laya_material_params={"u_FresnelColor": [1, 0, 0, 1]},
        laya_material_defines=[],
    )
    assert context["task"]["allowed_output"] == "strict_json_semantic_prior"

    validated = validate_llm_semantics_output(
        {
            "param_semantics": [
                {
                    "name": "u_FresnelColor",
                    "group": "fresnel",
                    "role": "color",
                    "transform": "color_rgb",
                    "gates": [{"kind": "define", "name": "FRESNEL"}],
                },
                {"name": "u_NotReal", "group": "misc"},
            ],
            "unity_feature_summary": [
                {
                    "feature": "rim_or_fresnel",
                    "enabled": True,
                    "confidence": 0.88,
                    "evidence": ["_RIMLIGHT_ON keyword"],
                    "unity_params": ["_RimColor"],
                    "controls": ["color", "intensity"],
                    "laya_candidate_groups": ["fresnel"],
                }
            ],
            "laya_module_candidates": [
                {
                    "feature": "rim_or_fresnel",
                    "group": "fresnel",
                    "confidence": 0.8,
                    "params": ["u_FresnelColor", "u_NotReal"],
                    "define_gates": ["FRESNEL", "NOT_A_DEFINE"],
                    "param_gates": ["u_FresnelColor", "u_NotReal"],
                }
            ],
            "unity_phenomena": [
                {
                    "name": "rim_or_fresnel",
                    "confidence": 0.9,
                    "unity_evidence": ["Unity shader contains fresnel-like term"],
                    "laya_candidate_groups": ["fresnel", "not_a_group"],
                }
            ],
            "initial_laya_param_suggestions": [
                {
                    "laya_param": "u_FresnelColor",
                    "suggested_value": [1, 0.2, 0.2, 1],
                    "confidence": 0.6,
                    "source_unity_params": ["_RimColor"],
                },
                {"laya_param": "u_NotReal", "suggested_value": 1.0},
            ],
        },
        allowed_params={"u_FresnelColor"},
        allowed_defines={"FRESNEL"},
    )

    assert len(validated["param_semantics"]) == 1
    assert validated["unity_feature_summary"][0]["feature"] == "rim_or_fresnel"
    assert validated["laya_module_candidates"][0]["params"] == ["u_FresnelColor"]
    assert validated["laya_module_candidates"][0]["define_gates"] == ["FRESNEL"]
    assert validated["laya_module_candidates"][0]["param_gates"] == ["u_FresnelColor"]
    assert len(validated["unity_phenomena"]) == 1
    assert validated["unity_phenomena"][0]["laya_candidate_groups"] == ["fresnel", "misc"]
    assert len(validated["initial_laya_param_suggestions"]) == 1
    assert validated["warnings"] == [
        "ignored unknown param 'u_NotReal'",
        "ignored initial suggestion for unknown param 'u_NotReal'",
    ]


def test_semantic_graph_marks_unity_suggested_inactive_group_for_probe():
    graph = build_shader_effect_graph(
        _shader_params(),
        material_params=_params(),
        llm_semantics={
            "unity_feature_summary": [
                {
                    "feature": "rim_or_fresnel",
                    "confidence": 0.9,
                    "evidence": ["Unity rim keyword is enabled"],
                    "laya_candidate_groups": ["fresnel"],
                }
            ],
            "laya_module_candidates": [
                {
                    "feature": "rim_or_fresnel",
                    "group": "fresnel",
                    "confidence": 0.9,
                    "params": ["u_FresnelIntensity", "u_FresnelColor"],
                }
            ],
        },
    )

    group = graph.groups["fresnel"]
    assert group.current_active is False
    assert group.suggested_by_unity is True
    assert group.probe_required is True
    assert group.search_priority == pytest.approx(0.9)
    assert "u_FresnelIntensity" in graph.active_search_params()


def test_semantic_group_strategy_walks_ui_panel_order():
    """The optimizer should follow the human-curated UI panel order.

    When the run console assigns ``order=10`` to ``fresnel`` and a
    larger order to ``base_color``, ``SemanticGroupStrategy`` must
    pick ``fresnel`` first regardless of which channel currently has
    the worst residual.
    """

    params = dict(_params())
    params["u_FresnelIntensity"] = 1.0
    graph = build_shader_effect_graph(_shader_params(), material_params=params)
    # Simulate the run console preset assigning fresnel as the first
    # panel and demoting base_color to the back.
    fresnel = graph.groups["fresnel"]
    base = graph.groups["base_color"]
    object.__setattr__(fresnel, "order", 10)
    object.__setattr__(base, "order", 100)

    strategy = SemanticGroupStrategy(
        initial_params=params,
        shader_params=_shader_params(),
        graph=graph,
    )
    ctx = StrategyContext(
        iteration=0,
        current_params=params,
        analysis={
            "material_channels": {
                "fresnel_rim": {"rgb_mae": 0.9},
                "base_color_main_texture": {"rgb_mae": 0.1},
            }
        },
        diff_score=0.5,
        fit_score=0.5,
        state=AdjustmentState(best_params=params),
    )

    next_params, decision = strategy.propose(ctx)

    assert decision["optimizer"] == "semantic_group"
    assert decision["semantic_group"]["name"] == "fresnel"
    assert next_params != params
    assert decision["changes"]


def test_semantic_group_fresh_fit_runs_isolation_before_base_color():
    params = dict(_params())
    params["u_EmissionScale"] = 1.0
    graph = build_shader_effect_graph(_shader_params(), material_params=params)

    strategy = SemanticGroupStrategy(
        initial_params=params,
        shader_params=_shader_params(),
        graph=graph,
        auto_adjust_mode="fresh_fit",
    )
    next_params, decision = strategy.propose(
        StrategyContext(
            iteration=0,
            current_params=params,
            analysis={},
            diff_score=0.5,
            fit_score=0.5,
            state=AdjustmentState(best_params=params),
        )
    )

    assert decision["semantic_action"] == "isolate_base_color"
    assert next_params["u_EmissionScale"] == 0.0


def test_semantic_group_refine_current_skips_isolation():
    params = dict(_params())
    params["u_EmissionScale"] = 1.0
    graph = build_shader_effect_graph(_shader_params(), material_params=params)

    strategy = SemanticGroupStrategy(
        initial_params=params,
        shader_params=_shader_params(),
        graph=graph,
        auto_adjust_mode="refine_current",
    )
    next_params, decision = strategy.propose(
        StrategyContext(
            iteration=0,
            current_params=params,
            analysis={},
            diff_score=0.5,
            fit_score=0.5,
            state=AdjustmentState(best_params=params),
        )
    )

    assert decision.get("semantic_action") != "isolate_base_color"
    assert next_params["u_EmissionScale"] != 0.0

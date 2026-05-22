"""Legacy semantic group optimizer that reproduced the 0.8-era run."""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..shared.models import ShaderParam
from .effective_bounds import effective_bounds_for_param
from .semantic_graph import ShaderEffectGraph
from .strategy import CmaesStrategy, OptimizerStrategy, StrategyContext


class LegacySemanticGroupStrategy(OptimizerStrategy):
    """Low-dimensional group search driven by :class:`ShaderEffectGraph`."""

    name = "semantic_group_legacy_081"

    def __init__(
        self,
        *,
        initial_params: dict[str, Any],
        shader_params: Sequence[ShaderParam],
        graph: ShaderEffectGraph,
        auto_adjust_mode: str = "fresh_fit",
    ) -> None:
        from .cma_es_optimizer import ParameterEncoder

        self._graph = graph
        self._shader_params = list(shader_params)
        # Honor the human-curated order from the run console preset
        # first; fall back to suggested_by_unity / search_priority for
        # groups that don't carry an explicit order. This way the UI
        # panel order is what the optimizer walks through.
        groups_with_order = [
            (int(getattr(group, "order", 0) or 0), idx, group)
            for idx, group in enumerate(graph.groups.values())
        ]
        groups_with_order.sort(
            key=lambda item: (
                self._group_order_key(item[2]),
                not item[2].suggested_by_unity,
                -float(item[2].search_priority or 0.0),
                item[1],
            )
        )
        self._group_order: list[str] = []
        for _, _, group in groups_with_order:
            candidate_params = self._candidate_group_params(group)
            if any(graph.params.get(p) and graph.params[p].searchable for p in candidate_params):
                self._group_order.append(group.name)
        if not self._group_order:
            self._group_order = [group.name for group in graph.groups.values()]
        self._encoder_cls = ParameterEncoder
        self._initial_params = dict(initial_params)
        # Phase-summary 2026-05-08 follow-up: the first post-P0 run
        # (job 22:05:24) showed that ±18% single-axis perturbation
        # only nudges the perceptual fit_score by ~±1e-4 ~ ±7e-4,
        # which never cleared the old 0.5%-of-fit threshold. Bumping
        # the cold-start step to 0.25 produces visibly larger pixel
        # changes (typical ΔMAE ~5e-3 → Δfit ~2e-3) so the algorithm
        # can actually accept candidates instead of rolling back 30
        # iterations in a row.
        self._step_schedule = [0.25, 0.14, 0.075, 0.040]
        # Same root cause: the relative threshold of 0.5% × fit was
        # too strict for the actual signal magnitude. We tighten the
        # absolute floor (5e-5 ≈ noise of two consecutive identical
        # screenshots) and lower the relative floor to 0.1% so a
        # genuine pixel-level improvement is not classified as noise.
        self._min_improvement_abs = 5.0e-5
        self._min_improvement_rel = 0.001  # 0.1% of base fit
        self._probe_score_delta_abs = 2.5e-5
        self._probe_score_delta_rel = 0.0005
        # When a group only exposes one or two searchable axes, allow
        # very few rejected probes before declaring it exhausted —
        # otherwise the very first FishStandard run wastes 7+ iterations
        # bouncing on u_BaseColor before fresnel ever gets a turn.
        self._max_group_no_improve = 8
        self._max_group_no_improve_small = 3
        self._max_group_cycles = 3
        self._group_cycle = 0
        self._group_state: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, Any] | None = None
        self._auto_adjust_mode = (auto_adjust_mode or "fresh_fit").strip().lower()
        self._isolation_done = False

    def wants_global_no_improve_check(self) -> bool:
        # This strategy owns accept/reject and per-group exhaustion. The
        # legacy global abort is too aggressive for deliberate probes,
        # where a visible-but-worse candidate is still useful evidence.
        return False

    def propose(self, ctx: StrategyContext) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._group_order:
            return ctx.current_params, {
                "optimizer": self.name,
                "stop_reason": "no_semantic_groups",
                "stage": None,
            }
        previous_eval = self._consume_pending(ctx)
        base_params = previous_eval.get("next_base_params")
        if not isinstance(base_params, dict):
            base_params = dict(ctx.current_params)

        isolation = self._isolation_candidate(base_params)
        if isolation is not None:
            proposed, changed = isolation
            self._pending = {
                "group": "__isolate_base__",
                "kind": "isolation",
                "base_params": dict(base_params),
                "base_fit_score": float(ctx.fit_score),
                "changed_params": changed,
                "force_accept": True,
            }
            self._isolation_done = True
            return proposed, {
                "optimizer": self.name,
                "stage": {
                    "name": "isolate_base",
                    "description": (
                        "actively suppress specular/reflection/matcap/"
                        "emission/fresnel before tuning base color"
                    ),
                },
                "semantic_action": "isolate_base_color",
                "changes": CmaesStrategy._diff_params(base_params, proposed),
                "stop_reason": "continue",
                "previous_candidate": previous_eval or None,
                "isolation_forced_accept": True,
            }

        group_name = self._select_group(ctx.analysis, ctx.iteration, preferred=previous_eval.get("group"))
        if not group_name:
            return base_params, {
                "optimizer": self.name,
                "stop_reason": "all_semantic_groups_exhausted",
                "stage": None,
                "previous_candidate": previous_eval or None,
            }

        proposed, decision = self._propose_for_group(
            group_name=group_name,
            base_params=base_params,
            base_fit_score=ctx.fit_score,
            analysis=ctx.analysis,
            iteration=ctx.iteration,
        )
        decision["previous_candidate"] = previous_eval or None
        return proposed, decision

    def _isolation_candidate(self, base_params: dict[str, Any]) -> tuple[dict[str, Any], list[str]] | None:
        """Actively suppress detail layers before the first base pass.

        Human artists usually do not tune body/base colour while strong
        Fresnel, emission, matcap, IBL and specular lobes are still
        dominating the viewport. Merely *skipping* those groups is not
        enough: their current material defaults still contaminate the
        screenshot. This candidate writes a temporary isolation preset
        into the material so the following base_color candidates are
        evaluated against a cleaner diffuse-like view.

        The candidate is force-accepted by ``_consume_pending``. It is
        an analysis setup step, not a claim that the full final score is
        immediately better.
        """

        if self._isolation_done or self._auto_adjust_mode == "refine_current":
            return None
        if not self._group_order or self._group_order[0] != "base_color":
            self._isolation_done = True
            return None
        suppress_values: dict[str, Any] = self._semantic_isolation_values(base_params)
        suppress_values.update({
            "u_FresnelIntensity": 0.0,
            "u_FresnelPow": 0.0,
            "u_EmissionScale": 0.0,
            "u_EmissionPower": 0.0,
            "u_MatcapStrength": 0.0,
            "u_MatcapAddStrength": 0.0,
            "u_IBLMapIntensity": 0.0,
            "u_EnvironmentReflections": 0.0,
            "u_SpecularIntensity": 0.0,
            "u_SpecularSecondIntensity": 0.0,
            "u_GGXSpecular": 0.0,
        })
        color_values: dict[str, Any] = {
            "u_EmissionColor": [0.0, 0.0, 0.0, 0.0],
            "u_FresnelColor": [0.0, 0.0, 0.0, 0.0],
            "u_MatcapColor": [1.0, 1.0, 1.0, 1.0],
            "u_MatcapAddColor": [1.0, 1.0, 1.0, 1.0],
            "u_IBLMapColor": [1.0, 1.0, 1.0, 1.0],
        }
        candidate = dict(base_params)
        changed: list[str] = []
        for name, value in {**suppress_values, **color_values}.items():
            if name not in candidate:
                continue
            before = candidate.get(name)
            new_value = list(value) if isinstance(value, list) else value
            if before != new_value:
                candidate[name] = new_value
                changed.append(name)
        if not changed:
            self._isolation_done = True
            return None
        return candidate, changed

    def _semantic_isolation_values(self, base_params: dict[str, Any]) -> dict[str, Any]:
        """Choose suppress targets from the effect graph before falling back to names."""

        targets: dict[str, Any] = {}
        suppress_tokens = (
            "fresnel",
            "rim",
            "emission",
            "emissive",
            "matcap",
            "specular",
            "reflection",
            "environment",
            "ibl",
            "outline",
        )
        for group in self._graph.groups.values():
            group_text = " ".join(
                [
                    str(group.name),
                    str(group.reason),
                    " ".join(str(item) for item in group.channels),
                    " ".join(str(item) for item in group.unity_features),
                ]
            ).lower()
            if group.name == "base_color" or not any(token in group_text for token in suppress_tokens):
                continue
            for name in self._candidate_group_params(group):
                if name not in base_params or name in targets:
                    continue
                targets[name] = self._neutral_suppressed_value(name, base_params.get(name))
        return targets

    def _neutral_suppressed_value(self, name: str, value: Any) -> Any:
        sem = self._graph.params.get(name)
        text = " ".join(
            [
                name,
                str(getattr(sem, "group", "")),
                str(getattr(sem, "role", "")),
                str(getattr(sem, "reason", "")),
            ]
        ).lower()
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return 0.0
        if isinstance(value, list):
            if any(token in text for token in ("emission", "emissive", "fresnel", "rim", "outline")):
                return [0.0 for _ in value]
            if any(token in text for token in ("matcap", "ibl", "environment", "reflection")):
                return [1.0 for _ in value]
            return [0.0 for _ in value]
        return value

    def stop_reason(self) -> str | None:
        if (
            self._group_cycle >= self._max_group_cycles
            and self._group_order
            and all(self._group_status(name) in {"exhausted", "inactive_or_invisible"} for name in self._group_order)
        ):
            return "semantic_groups_exhausted"
        return None

    def _consume_pending(self, ctx: StrategyContext) -> dict[str, Any]:
        if self._pending is None:
            return {}
        pending = self._pending
        self._pending = None
        group_name = str(pending.get("group") or "")
        state = self._state_for_group(group_name)
        base_fit = float(pending.get("base_fit_score", ctx.fit_score))
        delta = float(ctx.fit_score) - base_fit
        # Both thresholds are now ``max(abs, rel * base_fit)`` so they
        # auto-scale: when fit_score is tiny (cold-start) almost any
        # measurable gain counts; when fit_score is high we demand a
        # proportionally bigger improvement to keep moving.
        min_improvement = max(
            self._min_improvement_abs,
            self._min_improvement_rel * abs(base_fit),
        )
        probe_threshold = max(
            self._probe_score_delta_abs,
            self._probe_score_delta_rel * abs(base_fit),
        )
        accepted = delta >= min_improvement
        if pending.get("force_accept"):
            accepted = True
        visibly_changed = abs(delta) >= probe_threshold
        if pending.get("kind") == "probe" and visibly_changed:
            state["phase"] = "optimize"
            state["probe_passed"] = True
        if accepted:
            state["status"] = "active"
            state["no_improve"] = 0
            state["best_fit_score"] = max(float(state.get("best_fit_score", -math.inf)), float(ctx.fit_score))
            state["best_params"] = dict(ctx.current_params)
            state["axis_rejected_dirs"] = {}
            next_base = dict(ctx.current_params)
            outcome = "accepted_for_isolation" if pending.get("force_accept") else "accepted"
        else:
            state["no_improve"] = int(state.get("no_improve", 0)) + 1
            next_base = dict(pending.get("base_params") or ctx.current_params)
            outcome = "rejected_rollback_to_base"
            limit = self._effective_no_improve_limit(group_name)
            if pending.get("kind") == "probe" and not visibly_changed and state["no_improve"] >= 2:
                state["status"] = "inactive_or_invisible"
            elif state["no_improve"] >= limit:
                state["status"] = "exhausted"
        if pending.get("kind") == "pattern" and not accepted:
            self._advance_after_reject(state, pending)
        elif pending.get("kind") == "pattern" and accepted:
            if pending.get("combo"):
                state["combo_cursor"] = int(pending.get("combo_index", state.get("combo_cursor", 0))) + 1
            else:
                state["axis_cursor"] = int(pending.get("axis_index", 0)) + 1
            state["direction"] = 1.0
        return {
            "group": group_name,
            "kind": pending.get("kind"),
            "outcome": outcome,
            "accepted": accepted,
            "fit_score": ctx.fit_score,
            "base_fit_score": base_fit,
            "delta": delta,
            "min_improvement": min_improvement,
            "probe_threshold": probe_threshold,
            "visible_probe_delta": visibly_changed,
            "changed_params": pending.get("changed_params", []),
            "next_base_params": next_base,
            "group_state": self._json_group_state(state),
        }

    def _propose_for_group(
        self,
        *,
        group_name: str,
        base_params: dict[str, Any],
        base_fit_score: float,
        analysis: dict[str, Any],
        iteration: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        group = self._graph.groups[group_name]
        state = self._state_for_group(group_name)
        if state["phase"] == "probe":
            probe_params, probe_changes = self._probe_candidate(base_params, group)
            if probe_changes:
                self._pending = {
                    "group": group_name,
                    "kind": "probe",
                    "base_params": dict(base_params),
                    "base_fit_score": float(base_fit_score),
                    "changed_params": probe_changes,
                }
                return probe_params, self._decision(
                    group=group,
                    state=state,
                    action="probe_group",
                    changes=CmaesStrategy._diff_params(base_params, probe_params),
                    stop_reason="continue",
                    extra={"probe_changed_params": probe_changes},
                )
            state["phase"] = "optimize"

        proposed, pattern_payload = self._pattern_candidate(
            base_params=base_params,
            group=group,
            state=state,
            analysis=analysis,
            iteration=iteration,
        )
        changes = CmaesStrategy._diff_params(base_params, proposed)
        if changes:
            self._pending = {
                "group": group_name,
                "kind": "pattern",
                "base_params": dict(base_params),
                "base_fit_score": float(base_fit_score),
                "changed_params": [str(change.get("param")) for change in changes if isinstance(change, dict)],
                **pattern_payload,
            }
        else:
            state["status"] = "exhausted"
        return proposed, self._decision(
            group=group,
            state=state,
            action="pattern_search",
            changes=changes,
            stop_reason="continue" if changes else "no_effective_change",
            extra={"axis": pattern_payload} if pattern_payload else {},
        )

    def _pattern_candidate(
        self,
        *,
        base_params: dict[str, Any],
        group: Any,
        state: dict[str, Any],
        analysis: dict[str, Any],
        iteration: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        whitelist = self._searchable_params_for_group(group, base_params)
        if not whitelist:
            state["status"] = "exhausted"
            return dict(base_params), {}
        combo_candidate = self._base_color_combo_candidate(
            base_params=base_params,
            group=group,
            state=state,
            analysis=analysis,
            iteration=iteration,
        )
        if combo_candidate is not None:
            return combo_candidate
        encoder = self._encoder_cls(
            base_params,
            self._shader_params,
            param_whitelist=whitelist,
            semantics=self._graph,
        )
        if encoder.dim == 0:
            state["status"] = "exhausted"
            return dict(base_params), {}

        vec = encoder.encode(base_params)
        axis_index = int(state.get("axis_cursor", 0)) % encoder.dim
        axis = encoder.axes[axis_index]
        hinted = self._hint_direction(analysis, axis.param_name)
        direction = hinted or float(state.get("direction", 1.0) or 1.0)
        step_index = min(int(state.get("step_index", 0)), len(self._step_schedule) - 1)
        step_ratio = self._step_schedule[step_index]
        width = max(float(axis.high) - float(axis.low), 1e-9)
        vec[axis_index] = max(
            encoder.lower_bounds[axis_index],
            min(encoder.upper_bounds[axis_index], vec[axis_index] + direction * step_ratio * width),
        )
        proposed = encoder.decode(vec)
        state["last_axis"] = axis.param_name
        state["last_direction"] = direction
        return proposed, {
            "axis_index": axis_index,
            "param": axis.param_name,
            "sub_index": axis.sub_index,
            "transform": axis.transform,
            "direction": direction,
            "hint_direction": hinted,
            "step_ratio": step_ratio,
            "step_index": step_index,
            "iteration": iteration,
        }

    def _base_color_combo_candidate(
        self,
        *,
        base_params: dict[str, Any],
        group: Any,
        state: dict[str, Any],
        analysis: dict[str, Any],
        iteration: int,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if group.name != "base_color":
            return None
        base_name = next((name for name in group.search_params if name in base_params and "basecolor" in name.lower()), "")
        gamma_name = next((name for name in group.search_params if name in base_params and "gamma" in name.lower()), "")
        if not base_name or not isinstance(base_params.get(base_name), list):
            return None

        channel = {}
        channels = analysis.get("material_channels") if isinstance(analysis, dict) else None
        if isinstance(channels, dict) and isinstance(channels.get("base_color_main_texture"), dict):
            channel = channels["base_color_main_texture"]
        rgb_bias = channel.get("rgb_bias_candidate_minus_reference") if isinstance(channel, dict) else None
        if not isinstance(rgb_bias, list) or len(rgb_bias) < 3:
            rgb_bias = [0.0, 0.0, 0.0]
        rgb_bias = [float(v) if isinstance(v, (int, float)) and math.isfinite(float(v)) else 0.0 for v in rgb_bias[:3]]
        luma_bias = channel.get("luma_bias_candidate_minus_reference") if isinstance(channel, dict) else 0.0
        luma_bias = float(luma_bias) if isinstance(luma_bias, (int, float)) and math.isfinite(float(luma_bias)) else 0.0

        combos = [
            ("inverse_rgb_bias", 0.55, 1.0, "bias"),
            ("strong_inverse_rgb_bias", 0.90, 1.0, "bias"),
            ("darken_desaturate", 0.0, 0.78, "desaturate"),
            ("cool_shadow", 0.0, 1.0, "scale:0.65,0.75,0.95"),
            ("purple_shadow", 0.0, 1.0, "scale:0.65,0.55,0.90"),
            ("reduce_red_lift_blue", 0.0, 1.0, "offset:-0.20,-0.10,+0.10"),
        ]
        combo_index = int(state.get("combo_cursor", 0))
        if combo_index >= len(combos):
            return None

        combo_name, bias_gain, value_scale, mode = combos[combo_index]
        current = list(base_params.get(base_name) or [])
        if len(current) < 3:
            return None
        rgb = [self._clamp01(float(current[i])) for i in range(3)]
        if mode == "bias":
            rgb = [self._clamp01(rgb[i] - bias_gain * rgb_bias[i]) for i in range(3)]
        elif mode == "desaturate":
            mean = sum(rgb) / 3.0
            rgb = [self._clamp01((mean + (rgb[i] - mean) * 0.65) * value_scale) for i in range(3)]
        elif mode.startswith("scale:"):
            scales = [float(item) for item in mode.split(":", 1)[1].split(",")]
            rgb = [self._clamp01(rgb[i] * scales[i]) for i in range(3)]
        elif mode.startswith("offset:"):
            offsets = [float(item) for item in mode.split(":", 1)[1].split(",")]
            rgb = [self._clamp01(rgb[i] + offsets[i]) for i in range(3)]

        proposed = dict(base_params)
        new_color = list(current)
        for i in range(3):
            new_color[i] = rgb[i]
        proposed[base_name] = new_color
        changed = [base_name]

        if gamma_name and isinstance(base_params.get(gamma_name), (int, float)):
            gamma = float(base_params[gamma_name])
            if luma_bias > 0.02:
                gamma *= 0.65
            elif luma_bias < -0.02:
                gamma *= 1.25
            if combo_name in {"darken_desaturate", "cool_shadow", "purple_shadow"}:
                gamma *= 0.80
            gamma = max(0.05, min(10.0, gamma))
            if abs(gamma - float(base_params[gamma_name])) > 1e-8:
                proposed[gamma_name] = gamma
                changed.append(gamma_name)

        if not CmaesStrategy._diff_params(base_params, proposed):
            state["combo_cursor"] = combo_index + 1
            return self._base_color_combo_candidate(
                base_params=base_params,
                group=group,
                state=state,
                analysis=analysis,
                iteration=iteration,
            )

        state["last_axis"] = f"combo:{combo_name}"
        state["last_direction"] = 0.0
        return proposed, {
            "combo": True,
            "combo_index": combo_index,
            "combo_name": combo_name,
            "param": base_name,
            "changed_params": changed,
            "rgb_bias_candidate_minus_reference": rgb_bias,
            "luma_bias_candidate_minus_reference": luma_bias,
            "step_ratio": self._step_schedule[min(int(state.get("step_index", 0)), len(self._step_schedule) - 1)],
            "iteration": iteration,
        }

    def _probe_candidate(self, base_params: dict[str, Any], group: Any) -> tuple[dict[str, Any], list[str]]:
        candidate = dict(base_params)
        probe_order = list(group.gate_params) + list(group.search_params) + list(group.params)
        seen: set[str] = set()
        changed: list[str] = []
        for name in probe_order:
            if name in seen or name not in candidate:
                continue
            seen.add(name)
            sem = self._graph.params.get(name)
            if sem is None or not sem.searchable:
                continue
            before = candidate.get(name)
            candidate[name] = self._probe_value(name, before, sem)
            if candidate.get(name) != before:
                changed.append(name)
                break
        return candidate, changed

    def _probe_value(self, name: str, value: Any, sem: Any) -> Any:
        if isinstance(value, bool):
            return True
        if isinstance(value, (int, float)):
            low, high = self._bounds_for_value(name, float(value), sem)
            if float(value) <= low + 1e-8:
                return max(min(low + (high - low) * 0.35, high), low)
            step = max((high - low) * 0.18, 1e-4)
            return max(low, min(high, float(value) + step))
        if isinstance(value, list) and value and all(isinstance(item, (int, float)) for item in value):
            out = list(value)
            limit = min(3, len(out))
            for idx in range(limit):
                out[idx] = max(0.0, min(1.0, float(out[idx]) + 0.18))
            return out
        return value

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _bounds_for_value(self, name: str, value: float, sem: Any) -> tuple[float, float]:
        lower = name.lower()
        if (bounds := effective_bounds_for_param(name)) is not None:
            return bounds
        low = sem.range_min if getattr(sem, "range_min", None) is not None else None
        high = sem.range_max if getattr(sem, "range_max", None) is not None else None
        if low is not None and high is not None and float(low) < float(high):
            return float(low), float(high)
        if any(token in lower for token in ("intensity", "strength", "scale")):
            return 0.0, 8.0
        if any(token in lower for token in ("threshold", "smooth", "metallic", "occlusion")):
            return 0.0, 1.0
        if "pow" in lower or "power" in lower:
            return 0.0, 10.0
        if "gamma" in lower:
            return 0.05, 10.0
        return min(value - 1.0, 0.0), max(value + 1.0, 1.0)

    def _searchable_params_for_group(self, group: Any, params: dict[str, Any]) -> list[str]:
        names = self._candidate_group_params(group)
        return [
            name
            for name in names
            if name in params
            and self._graph.params.get(name) is not None
            and self._graph.params[name].searchable
        ]

    def _state_for_group(self, group_name: str) -> dict[str, Any]:
        state = self._group_state.get(group_name)
        if state is not None:
            return state
        group = self._graph.groups.get(group_name)
        state = {
            "phase": "probe" if group is not None and (group.probe_required or not group.current_active) else "optimize",
            "status": "pending",
            "step_index": 0,
            "axis_cursor": 0,
            "direction": 1.0,
            "no_improve": 0,
            "probe_passed": False,
            "best_fit_score": -math.inf,
            "best_params": dict(self._initial_params),
        }
        self._group_state[group_name] = state
        return state

    def _group_status(self, group_name: str) -> str:
        return str(self._state_for_group(group_name).get("status", "pending"))

    def _effective_no_improve_limit(self, group_name: str) -> int:
        group = self._graph.groups.get(group_name)
        if group is None:
            return self._max_group_no_improve
        searchable = [
            name
            for name in self._candidate_group_params(group)
            if self._graph.params.get(name) is not None and self._graph.params[name].searchable
        ]
        if len(searchable) <= 2:
            return self._max_group_no_improve_small
        return self._max_group_no_improve

    def _advance_after_reject(self, state: dict[str, Any], pending: dict[str, Any]) -> None:
        if pending.get("combo"):
            state["combo_cursor"] = int(pending.get("combo_index", state.get("combo_cursor", 0))) + 1
            if int(state.get("no_improve", 0)) > 0 and int(state["no_improve"]) % 4 == 0:
                state["step_index"] = min(int(state.get("step_index", 0)) + 1, len(self._step_schedule) - 1)
            return
        # Track per-axis +/- attempts so once both directions have been
        # rejected for the same axis we advance the cursor immediately.
        # Without this the strategy spent 7+ iterations re-trying a
        # single u_BaseColor axis in the first FishStandard run.
        axis_index = int(pending.get("axis_index", state.get("axis_cursor", 0)))
        direction = float(pending.get("direction", state.get("direction", 1.0)) or 1.0)
        rejected = state.setdefault("axis_rejected_dirs", {})
        bucket = rejected.setdefault(axis_index, [])
        sign = "+" if direction >= 0.0 else "-"
        if sign not in bucket:
            bucket.append(sign)
        both_dirs_tried = "+" in bucket and "-" in bucket
        if both_dirs_tried:
            state["axis_cursor"] = axis_index + 1
            state["direction"] = 1.0
            rejected.pop(axis_index, None)
        elif direction > 0.0:
            state["direction"] = -1.0
        else:
            state["direction"] = 1.0
            state["axis_cursor"] = axis_index + 1
            rejected.pop(axis_index, None)
        if int(state.get("no_improve", 0)) > 0 and int(state["no_improve"]) % 4 == 0:
            state["step_index"] = min(int(state.get("step_index", 0)) + 1, len(self._step_schedule) - 1)

    def _decision(
        self,
        *,
        group: Any,
        state: dict[str, Any],
        action: str,
        changes: list[dict[str, Any]],
        stop_reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "optimizer": self.name,
            "stage": {"name": group.name, "description": f"semantic group {action}: {group.reason}"},
            "semantic_group": group.to_dict(),
            "semantic_action": action,
            "group_state": self._json_group_state(state),
            "changes": changes,
            "stop_reason": stop_reason,
        }
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _json_group_state(state: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in state.items()
            if key != "best_params"
        }

    def _candidate_group_params(self, group: Any) -> list[str]:
        if group.search_params:
            return list(group.search_params)
        if not group.current_active and group.gate_params:
            return list(group.gate_params)
        return list(group.params)

    @staticmethod
    def _group_order_key(group: Any) -> int:
        order = int(getattr(group, "order", 0) or 0)
        if order > 0:
            return order
        if getattr(group, "name", "") == "base_color":
            return 0
        return 10_000

    def _select_group(self, analysis: dict[str, Any], iteration: int, preferred: Any = None) -> str:
        # Stick with the preferred group as long as it's still working.
        # This keeps a successful pattern_search going inside the group
        # instead of jumping around channels every iteration.
        preferred_name = str(preferred or "")
        if (
            preferred_name in self._group_order
            and self._group_status(preferred_name) not in {"exhausted", "inactive_or_invisible"}
        ):
            return preferred_name

        # Walk through the human-curated UI panel order. Whichever group
        # comes first and is still workable wins — this is what makes
        # the run console panel order meaningful for the optimizer.
        for name in self._group_order:
            if self._group_status(name) not in {"exhausted", "inactive_or_invisible"}:
                return name
        if self._restart_exhausted_groups():
            for name in self._group_order:
                if self._group_status(name) not in {"exhausted", "inactive_or_invisible"}:
                    return name
        return ""

    def _restart_exhausted_groups(self) -> bool:
        if self._group_cycle >= self._max_group_cycles:
            return False
        restarted = False
        self._group_cycle += 1
        for name in self._group_order:
            state = self._state_for_group(name)
            if state.get("status") != "exhausted":
                continue
            state["status"] = "pending"
            state["phase"] = "optimize"
            state["no_improve"] = 0
            state["axis_cursor"] = 0
            state["combo_cursor"] = 0
            state["direction"] = 1.0
            state["axis_rejected_dirs"] = {}
            state["step_index"] = min(int(state.get("step_index", 0)) + 1, len(self._step_schedule) - 1)
            restarted = True
        return restarted

    @staticmethod
    def _hint_direction(analysis: dict[str, Any], param_name: str) -> float:
        hints = analysis.get("adjustment_hints") if isinstance(analysis, dict) else None
        if not isinstance(hints, list):
            return 0.0
        total = 0.0
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            related = hint.get("related_params")
            if not isinstance(related, list):
                continue
            if not any(CmaesStrategy._param_match(param_name.lower(), str(item).lower()) for item in related):
                continue
            direction = str(hint.get("direction", "")).lower()
            if direction == "increase":
                total += 1.0
            elif direction == "decrease":
                total -= 1.0
        if total > 0.0:
            return 1.0
        if total < 0.0:
            return -1.0
        return 0.0




__all__ = ["LegacySemanticGroupStrategy"]

"""Response-driven semantic optimizer facade."""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..shared.models import ShaderParam
from .acceptance_policy import AcceptancePolicy
from .branch_guard import BranchDriftGuard
from .candidate_builder import CandidateBuilder, diff_params
from .experiment_planner import ExperimentPlanner
from .response_map import ResponseMap, context_key_from_metrics
from .search_evidence import TopKArchive, metric_vector_from_analysis
from .semantic_graph import ShaderEffectGraph


class ResponseDrivenSemanticStrategy:
    """Thin compatibility wrapper around the response-driven scheduler."""

    name = "semantic_group"

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
        self._initial_params = dict(initial_params)
        self._auto_adjust_mode = (auto_adjust_mode or "fresh_fit").strip().lower()
        self._step_schedule = [0.25, 0.14, 0.075, 0.040]
        self._pending: dict[str, Any] | None = None
        self._response_map = ResponseMap()
        self._topk = TopKArchive(capacity=16)
        self._acceptance_policy = AcceptancePolicy()
        self._branch_guard = BranchDriftGuard()
        self._builder = CandidateBuilder(
            graph=graph,
            shader_params=shader_params,
            encoder_cls=ParameterEncoder,
            step_schedule=self._step_schedule,
        )
        self._planner = ExperimentPlanner(
            graph=graph,
            builder=self._builder,
            response_map=self._response_map,
            archive=self._topk,
            param_candidate_pool_size=10,
        )
        self._min_improvement_abs = 5.0e-5
        self._min_improvement_rel = 0.001

    def wants_global_no_improve_check(self) -> bool:
        return False

    def stop_reason(self) -> str | None:
        return None

    def research_summary(self) -> dict[str, Any]:
        bottleneck = self._latest_bottleneck()
        ranking = self._param_ranking({}, {"research_metrics": {"components": bottleneck}}, limit=None)
        snapshot = self._planner.snapshot(trial=None, bottleneck=bottleneck, ranking=ranking).to_dict()
        return {
            "phase": snapshot["planner_phase"],
            "planner": snapshot,
            "topk": self._topk.summary(limit=8),
            "param_priority": ranking,
            "param_candidate_pool_size": self._planner.param_candidate_pool_size,
            "response_map": self._response_map.summary(bottleneck, limit=8),
            "branch_guard": self._branch_guard.summary(),
            "acceptance_policy": self._acceptance_policy.summary(),
        }

    def propose(self, ctx: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        previous_eval = self._consume_pending(ctx)
        if not previous_eval:
            self._record_topk(ctx, trial_kind=None)
        base_params = previous_eval.get("next_base_params")
        if not isinstance(base_params, dict):
            base_params = dict(ctx.current_params)
        bottleneck = self._metric_bottleneck(ctx.analysis)
        ranking = self._param_ranking(base_params, ctx.analysis, limit=None)
        trial = self._planner.next_trial(
            base_params=base_params,
            analysis=ctx.analysis,
            fit_score=float(ctx.fit_score),
            iteration=int(ctx.iteration),
            ranking=ranking,
            bottleneck=bottleneck,
        )
        if trial is None:
            return base_params, {
                "optimizer": self.name,
                "stage": None,
                "semantic_action": "no_effective_trial",
                "scheduler": self._scheduler_state("none", ctx.analysis, base_params=base_params, trial=None),
                "changes": [],
                "stop_reason": "no_effective_trial",
                "previous_candidate": previous_eval or None,
            }
        base_metrics = metric_vector_from_analysis(ctx.analysis, ctx.fit_score)
        self._pending = trial.to_pending(
            base_params=base_params,
            base_fit_score=float(ctx.fit_score),
            base_metrics=base_metrics,
        )
        changes = diff_params(base_params, trial.proposed_params)
        return trial.proposed_params, {
            "optimizer": self.name,
            "stage": {"name": trial.stage_name, "description": trial.reason},
            "semantic_action": trial.kind,
            "scheduler": self._scheduler_state(trial.kind, ctx.analysis, base_params=base_params, trial=trial),
            "changes": changes,
            "trial": {
                "kind": trial.kind,
                "reason": trial.reason,
                "changed_params": trial.changed_params,
                **trial.payload,
            },
            "stop_reason": "continue",
            "previous_candidate": previous_eval or None,
        }

    def _consume_pending(self, ctx: Any) -> dict[str, Any]:
        if self._pending is None:
            return {}
        pending = self._pending
        self._pending = None
        base_fit = float(pending.get("base_fit_score", ctx.fit_score))
        delta = float(ctx.fit_score) - base_fit
        candidate_metrics = metric_vector_from_analysis(ctx.analysis, ctx.fit_score)
        base_metrics = pending.get("base_metrics")
        if not hasattr(base_metrics, "components"):
            base_metrics = metric_vector_from_analysis({}, base_fit)
        min_improvement = max(self._min_improvement_abs, self._min_improvement_rel * abs(base_fit))
        acceptance = self._acceptance_policy.evaluate(
            base=base_metrics,
            candidate=candidate_metrics,
            fit_delta=delta,
            min_improvement=min_improvement,
            phase=str(pending.get("kind") or "response_guided"),
        )
        changed_params = [str(item) for item in pending.get("changed_params", []) if item]
        branch_exploratory_accept = (
            not acceptance.accepted
            and acceptance.reason == "insufficient_gain"
            and self._branch_guard.allows_exploration(fit_score=float(ctx.fit_score))
            and self._response_map.supports_exploratory_accept(
                changed_params=changed_params,
                fit_delta=delta,
                component_gain=acceptance.component_gain,
                bottleneck=self._metric_bottleneck(ctx.analysis),
            )
        )
        accepted = acceptance.accepted or branch_exploratory_accept
        response_observation = self._response_map.observe_trial(
            changed_params=changed_params,
            before_params=dict(pending.get("base_params") or {}),
            after_params=dict(ctx.current_params),
            before=base_metrics,
            after=candidate_metrics,
            fit_delta=delta,
            accepted=accepted,
            candidate_kind=str(pending.get("kind") or ""),
            context_key=context_key_from_metrics(base_metrics),
        )
        run_best_params = getattr(ctx.state, "best_fit_params", {}) or getattr(ctx.state, "best_params", {})
        run_best_score = float(getattr(ctx.state, "best_fit_score", -math.inf))
        if isinstance(run_best_params, dict) and run_best_params:
            self._branch_guard.update_checkpoint(params=run_best_params, fit_score=run_best_score)
        if accepted:
            drift = self._branch_guard.observe(
                iteration=int(ctx.iteration),
                params=ctx.current_params,
                fit_score=float(ctx.fit_score),
                metrics=candidate_metrics,
            )
            if drift.should_rollback and self._branch_guard.checkpoint_params:
                next_base = self._branch_guard.checkpoint_params
                outcome = "accepted_but_drift_rollback_to_checkpoint"
            else:
                next_base = dict(ctx.current_params)
                if branch_exploratory_accept:
                    outcome = "exploratory_accept_checkpoint_branch"
                elif float(ctx.fit_score) < run_best_score - 1.0e-9:
                    outcome = "accepted_checkpoint_branch"
                else:
                    outcome = "accepted"
        else:
            base_drift = self._branch_guard.evaluate_without_record(fit_score=base_fit)
            if base_drift.should_rollback and self._branch_guard.checkpoint_params:
                next_base = self._branch_guard.checkpoint_params
                outcome = "rejected_drift_rollback_to_checkpoint"
            else:
                next_base = dict(pending.get("base_params") or ctx.current_params)
                outcome = "rejected_keep_branch_base"
        self._record_topk(ctx, trial_kind=str(pending.get("kind") or ""))
        return {
            "kind": pending.get("kind"),
            "outcome": outcome,
            "accepted": accepted,
            "fit_score": ctx.fit_score,
            "base_fit_score": base_fit,
            "delta": delta,
            "min_improvement": min_improvement,
            "changed_params": changed_params,
            "next_base_params": next_base,
            "acceptance": acceptance.to_dict(),
            "branch_exploratory_accept": branch_exploratory_accept,
            "branch_guard": self._branch_guard.summary(),
            "response_observation": response_observation,
            "response_map": self._response_map.summary(self._metric_bottleneck(ctx.analysis), limit=8),
            "topk": self._topk.summary(limit=5),
        }

    def _scheduler_state(
        self,
        selected_kind: str,
        analysis: dict[str, Any] | None = None,
        *,
        base_params: dict[str, Any] | None = None,
        trial: Any | None = None,
    ) -> dict[str, Any]:
        params = base_params or {}
        bottleneck = self._metric_bottleneck(analysis or {})
        ranking = self._param_ranking(params, analysis or {}, limit=None)
        snapshot = self._planner.snapshot(trial=trial, bottleneck=bottleneck, ranking=ranking).to_dict()
        gated_rows = self._gated_param_rows(params)
        activation_rows = self._activation_param_rows(params, analysis or {})
        return {
            **snapshot,
            "phase": snapshot["planner_phase"],
            "raw_phase": snapshot["planner_phase"],
            "selected_group": selected_kind,
            "selected_trial_kind": selected_kind,
            "group_order": [],
            "group_status": {},
            "force_breakthrough": False,
            "plateau": {"planner_phase": snapshot["planner_phase"]},
            "trust_region": {"active": False, "retired": True},
            "branch_guard": self._branch_guard.summary(),
            "response_map": self._response_map.summary(bottleneck, limit=8),
            "param_selection_rule": (
                "Unified response-driven scheduler: choose calibration, single, pair, "
                "subspace, or archive restart from ResponseMap evidence, bottleneck, and budgets."
            ),
            "calibration": snapshot["budget_state"]["calibration"],
            "response_pair": snapshot["budget_state"]["pair"],
            "search_param_count": len(ranking),
            "all_searchable_param_count": self._searchable_param_count(params),
            "gated_param_count": len(gated_rows),
            "activation_candidate_count": len(activation_rows),
            "param_candidate_pool_size": self._planner.param_candidate_pool_size,
            "gated_params": gated_rows,
            "activation_candidates": activation_rows,
            # Backward-compatible aliases for existing UI panels.
            "param_agenda": snapshot["param_candidate_pool"],
        }

    def _param_ranking(self, base_params: dict[str, Any], analysis: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
        bottleneck = self._metric_bottleneck(analysis)
        source_names = self._graph.active_search_params_for(base_params) if base_params else self._graph.active_search_params()
        rows: list[dict[str, Any]] = []
        for name in source_names:
            if base_params and name not in base_params:
                continue
            sem = self._graph.params.get(name)
            relevance = self._semantic_relevance_for_param(name, bottleneck)
            response = self._response_map.summary_for(name, bottleneck, semantic_relevance=relevance)
            legacy_priority = relevance + 0.025 / math.sqrt(float(response.get("attempts", 0) or 0) + 1.0)
            no_effect = float(response.get("no_effect_rate", 0.0) or 0.0)
            confidence = float(response.get("confidence", 0.0) or 0.0)
            priority = (
                0.45 * legacy_priority
                + 0.55 * float(response.get("response_priority", 0.0) or 0.0)
                + 0.025 / math.sqrt(float(response.get("attempts", 0) or 0) + 1.0)
            )
            if no_effect > 0.70 and confidence > 0.25:
                priority -= 0.045
            rows.append(
                {
                    "param": name,
                    "group": str(getattr(sem, "group", "") or ""),
                    "group_status": "semantic_auxiliary",
                    "role": str(getattr(sem, "role", "") or ""),
                    "transform": str(getattr(sem, "transform", "") or ""),
                    "semantic_relevance": relevance,
                    "legacy_priority": legacy_priority,
                    "priority": priority,
                    "response_priority": response.get("response_priority"),
                    "response_attempts": response.get("attempts", 0),
                    "response_confidence": response.get("confidence", 0.0),
                    "response_no_effect_rate": response.get("no_effect_rate", 0.0),
                    "response_recommended_direction": response.get("recommended_direction"),
                    "response_context_sensitivity": response.get("context_sensitivity"),
                    "interaction_suspicion": response.get("interaction_suspicion"),
                    "response_metrics": response.get("metrics", {}),
                }
            )
        rows.sort(key=lambda item: float(item.get("priority", 0.0) or 0.0), reverse=True)
        return rows if limit is None else rows[: max(0, int(limit))]

    def _param_agenda(self, base_params: dict[str, Any], analysis: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
        return self._param_ranking(base_params, analysis, limit=limit)

    def _metric_bottleneck(self, analysis: dict[str, Any]) -> dict[str, float]:
        metrics = analysis.get("research_metrics") if isinstance(analysis, dict) else None
        components = metrics.get("components") if isinstance(metrics, dict) else None
        if not isinstance(components, dict):
            return {}
        rows = [
            (str(key), float(value))
            for key, value in components.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
        ]
        rows.sort(key=lambda item: item[1], reverse=True)
        return {key: value for key, value in rows[:4] if value > 0.0}

    def _semantic_relevance_for_param(self, param_name: str, bottleneck: dict[str, float]) -> float:
        sem = self._graph.params.get(param_name)
        text = " ".join(
            [
                param_name,
                str(getattr(sem, "group", "")),
                str(getattr(sem, "role", "")),
                str(getattr(sem, "transform", "")),
            ]
        ).lower()
        keys = set(bottleneck)
        score = 0.0
        if keys & {"color_mean", "color_p95"} and any(token in text for token in ("color", "saturation", "hue", "gamma", "texpower", "reflect", "shadow")):
            score += 0.055
        if keys & {"luminance_mae", "luminance_bias"} and any(token in text for token in ("gamma", "power", "intensity", "shadow", "ao", "contrast", "reflect")):
            score += 0.045
        if keys & {"highlight"} and any(token in text for token in ("specular", "smooth", "threshold", "shadow", "rim", "fresnel", "reflect")):
            score += 0.055
        if keys & {"structure_ssim_l", "detail_texture"} and any(token in text for token in ("normal", "smooth", "threshold", "specular", "shadow", "power")):
            score += 0.045
        return score

    def _record_topk(self, ctx: Any, trial_kind: str | None) -> None:
        self._topk.add(
            params=ctx.current_params,
            fit_score=float(ctx.fit_score),
            metrics=metric_vector_from_analysis(ctx.analysis, ctx.fit_score),
            group=trial_kind,
            iteration=int(ctx.iteration),
        )

    def _latest_bottleneck(self) -> dict[str, float]:
        if not self._topk.items:
            return {}
        components = self._topk.items[0].get("components")
        if not isinstance(components, dict):
            return {}
        rows = [
            (str(key), float(value))
            for key, value in components.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        rows.sort(key=lambda item: item[1], reverse=True)
        return {key: value for key, value in rows[:4] if value > 0.0}

    def _searchable_param_count(self, base_params: dict[str, Any]) -> int:
        return len([
            name for name, sem in self._graph.params.items()
            if sem.searchable and (not base_params or name in base_params)
        ])

    def _gated_param_rows(self, base_params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            item for item in self._graph.gated_search_params_for(base_params)
            if not base_params or str(item.get("param") or "") in base_params
        ]

    def _activation_param_rows(self, base_params: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, Any]]:
        bottleneck = self._metric_bottleneck(analysis)
        rows: list[dict[str, Any]] = []
        for item in self._graph.activation_params_for(base_params):
            name = str(item.get("param") or "")
            if base_params and name not in base_params:
                continue
            rows.append({
                "param": name,
                **item,
                "semantic_relevance": self._semantic_relevance_for_param(name, bottleneck),
            })
        rows.sort(key=lambda item: float(item.get("semantic_relevance", 0.0)), reverse=True)
        return rows


__all__ = ["ResponseDrivenSemanticStrategy"]

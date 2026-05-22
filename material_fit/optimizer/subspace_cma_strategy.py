"""Expensive subspace CMA-ES strategy for effect-first comparison runs."""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..shared.models import ShaderParam
from .candidate_builder import diff_params
from .cma_es_optimizer import CmaesConfig, CmaesOptimizer, ParameterEncoder
from .semantic_graph import ShaderEffectGraph


class SubspaceCmaEsStrategy:
    """Run CMA-ES inside a compact semantic/metric subspace."""

    name = "subspace_cma_es"

    def __init__(
        self,
        *,
        initial_params: dict[str, Any],
        shader_params: Sequence[ShaderParam],
        graph: ShaderEffectGraph,
        population_size: int | None = None,
        sigma: float | None = None,
        seed: int | None = None,
        max_axes: int = 10,
    ) -> None:
        self._initial_params = dict(initial_params)
        self._shader_params = list(shader_params)
        self._graph = graph
        self._max_axes = max(2, int(max_axes))
        self._config = CmaesConfig(
            population_size=population_size,
            sigma=float(sigma) if sigma is not None else 0.22,
            seed=seed,
        )
        self._optimizer: CmaesOptimizer | None = None
        self._encoder: ParameterEncoder | None = None
        self._subspace_params: list[str] = []
        self._pending_params: dict[str, Any] | None = None
        self._last_loss: float | None = None
        self._last_fit_score: float | None = None
        self._restart_count = 0
        self._archive: list[tuple[float, dict[str, Any]]] = []

    def wants_global_no_improve_check(self) -> bool:
        return False

    def stop_reason(self) -> str | None:
        if self._optimizer is not None and self._optimizer.should_stop():
            return "subspace_cma_should_stop"
        return None

    def research_summary(self) -> dict[str, Any]:
        best_params, best_loss = self._optimizer.best if self._optimizer is not None else (None, math.inf)
        return {
            "phase": "subspace_cma",
            "subspace_params": list(self._subspace_params),
            "trainable_dim": self._encoder.dim if self._encoder is not None else 0,
            "population_size": self._optimizer.population_size if self._optimizer is not None else None,
            "evaluations": self._optimizer.evaluations if self._optimizer is not None else 0,
            "generation": self._generation(),
            "population_index": self._population_index(),
            "sigma": self._config.sigma,
            "restart_count": self._restart_count,
            "archive_size": len(self._archive),
            "best_loss": best_loss if math.isfinite(float(best_loss)) else None,
            "best_fit_score_in_subspace": (1.0 - best_loss) if math.isfinite(float(best_loss)) else None,
            "has_best_params": best_params is not None,
        }

    def propose(self, ctx: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._optimizer is None:
            self._initialize(ctx.current_params, ctx.analysis)
        if self._optimizer is None:
            return ctx.current_params, {
                "optimizer": self.name,
                "stage": None,
                "stop_reason": "no_subspace_axes",
                "changes": [],
            }
        if self._pending_params is not None:
            loss = self._fit_score_to_loss(ctx.fit_score, ctx.diff_score)
            self._optimizer.tell(loss)
            self._record_archive(self._pending_params, loss)
            self._last_loss = loss
            self._last_fit_score = float(ctx.fit_score)
        if self._optimizer.should_stop():
            self._restart_count += 1
            self._initialize(self._archive_center(ctx.current_params), ctx.analysis, force=True)
            if self._optimizer is None:
                return ctx.current_params, {
                    "optimizer": self.name,
                    "stage": None,
                    "stop_reason": "subspace_cma_restart_failed",
                    "changes": [],
                }

        proposed = self._optimizer.ask()
        self._pending_params = proposed
        changes = diff_params(ctx.current_params, proposed)
        return proposed, {
            "optimizer": self.name,
            "stage": {"name": "subspace_cma_es", "description": "CMA-ES inside response/semantic active subspace"},
            "semantic_action": "subspace_cma_candidate",
            "changes": changes,
            "stop_reason": self.stop_reason() or ("continue" if changes else "no_effective_change"),
            "subspace_cma_es": {
                "subspace_params": list(self._subspace_params),
                "trainable_dim": self._encoder.dim if self._encoder is not None else 0,
                "population_size": self._optimizer.population_size,
                "evaluations": self._optimizer.evaluations,
                "generation": self._generation(),
                "population_index": self._population_index(),
                "sigma": self._config.sigma,
                "restart_count": self._restart_count,
                "archive_size": len(self._archive),
                "last_loss_fed": self._last_loss,
                "last_fit_score": self._last_fit_score,
                "best": self._best_summary(),
            },
        }

    def _initialize(self, current_params: dict[str, Any], analysis: dict[str, Any], *, force: bool = False) -> None:
        if self._optimizer is not None and not force:
            return
        self._optimizer = None
        self._encoder = None
        self._subspace_params = []
        self._pending_params = None
        candidates = [
            name for name in self._graph.active_search_params_for(current_params)
            if name in current_params
        ]
        candidates.sort(key=lambda name: self._semantic_relevance(name, self._metric_bottleneck(analysis)), reverse=True)
        if not candidates:
            return
        selected: list[str] = []
        best_encoder: ParameterEncoder | None = None
        for name in candidates:
            selected.append(name)
            encoder = ParameterEncoder(
                current_params,
                self._shader_params,
                param_whitelist=selected,
                semantics=self._graph,
            )
            if encoder.dim > self._max_axes:
                selected.pop()
                break
            if encoder.dim > 0:
                best_encoder = encoder
            if encoder.dim >= self._max_axes:
                break
        if best_encoder is None:
            return
        self._subspace_params = list(selected)
        self._encoder = best_encoder
        self._optimizer = CmaesOptimizer(
            best_encoder,
            config=self._config,
            initial_mean=current_params,
        )

    def _generation(self) -> int:
        if self._optimizer is None:
            return 0
        return int(self._optimizer.evaluations // max(self._optimizer.population_size, 1))

    def _population_index(self) -> int:
        if self._optimizer is None:
            return 0
        return int(self._optimizer.evaluations % max(self._optimizer.population_size, 1))

    def _best_summary(self) -> dict[str, Any]:
        if self._optimizer is None:
            return {"loss": None, "fit_score": None, "has_params": False}
        best_params, best_loss = self._optimizer.best
        if not math.isfinite(float(best_loss)):
            return {"loss": None, "fit_score": None, "has_params": best_params is not None}
        return {
            "loss": float(best_loss),
            "fit_score": 1.0 - float(best_loss),
            "has_params": best_params is not None,
        }

    def _record_archive(self, params: dict[str, Any] | None, loss: float) -> None:
        if params is None or not math.isfinite(float(loss)):
            return
        self._archive.append((float(loss), dict(params)))
        self._archive.sort(key=lambda item: item[0])
        del self._archive[8:]

    def _archive_center(self, fallback: dict[str, Any]) -> dict[str, Any]:
        if self._archive:
            return dict(self._archive[0][1])
        return dict(fallback)

    @staticmethod
    def _fit_score_to_loss(fit_score: float, diff_score: float) -> float:
        if math.isfinite(float(fit_score)):
            return max(0.0, 1.0 - float(fit_score))
        if math.isfinite(float(diff_score)):
            return max(0.0, float(diff_score))
        return 0.5

    @staticmethod
    def _metric_bottleneck(analysis: dict[str, Any]) -> dict[str, float]:
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

    def _semantic_relevance(self, param_name: str, bottleneck: dict[str, float]) -> float:
        sem = self._graph.params.get(param_name)
        text = " ".join([
            param_name,
            str(getattr(sem, "group", "")),
            str(getattr(sem, "role", "")),
            str(getattr(sem, "transform", "")),
        ]).lower()
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


__all__ = ["SubspaceCmaEsStrategy"]

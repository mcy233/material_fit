"""Pluggable optimization strategies for ``fit_material._run_auto_adjustment``.

This module isolates *which* algorithm proposes the next parameter set
from *how* the rest of the pipeline (image diff, ``.lmat`` writer,
screenshot capture) drives a single iteration. Without this split,
adding CMA-ES would mean sprinkling ``if optimizer == "..."`` across
the auto-adjust loop, which makes both branches harder to reason about
and makes future optimizers (BO, NSGA-II, ...) require touching
``fit_material.py`` again.

The contract is:

* The pipeline analyses the *current* candidate render and computes
  ``fit_score`` / ``diff_score``.
* It then calls :meth:`OptimizerStrategy.propose` with that signal and
  expects ``(next_params, decision_dict)`` back.
* The strategy is responsible for (a) advancing its own internal state
  (heuristic stage tracking, CMA-ES population/generation) and (b)
  emitting a JSON-friendly ``decision`` dict that records *why* the
  proposed change was made — this is what the UI shows and what
  research-time inspection relies on.

This file imports from both ``adjustment_algorithm`` (heuristic) and
``cma_es_optimizer`` (CMA-ES). The CMA-ES dependency is *lazy*
because ``cmaes`` is not in ``requirements.txt`` for production users
who only want the heuristic path — the strategy raises a clear
:class:`OptimizerUnavailableError` when CMA-ES is requested without
the library installed.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from ..shared.models import ShaderParam
from .adjustment_algorithm import (
    STUCK_NO_IMPROVE_LIMIT,
    AdjustmentState,
    AdjustmentStagePolicy,
    choose_stage,
    propose_next_params,
    update_stage_progress,
)
from .response_driven_strategy import ResponseDrivenSemanticStrategy
from .semantic_graph import ShaderEffectGraph, graph_from_dict


# ---------------------------------------------------------------------
# Strategy interface


class OptimizerUnavailableError(RuntimeError):
    """Raised when the requested optimizer's dependencies aren't installed."""


@dataclass
class StrategyContext:
    """Per-iteration context handed to the strategy.

    All fields are read-only from the strategy's perspective — the
    pipeline owns the global ``AdjustmentState`` and only mutates it
    based on the strategy's returned decision.
    """

    iteration: int
    current_params: dict[str, Any]
    analysis: dict[str, Any]
    diff_score: float
    fit_score: float
    state: AdjustmentState


class OptimizerStrategy(ABC):
    """Abstract base for parameter-proposing strategies."""

    name: str = "<unset>"

    @abstractmethod
    def propose(self, ctx: StrategyContext) -> tuple[dict[str, Any], dict[str, Any]]:
        """Propose the next parameter set.

        Returns ``(next_params, decision)`` where:

        * ``next_params`` is the dict to be written into the candidate
          ``.lmat`` (or applied directly when ``--apply-lmat``).
        * ``decision`` is a JSON-serializable dict recording the
          rationale (which stage / which gen / what changed). The
          pipeline serializes it into ``decision.json`` verbatim under
          a ``decision`` key plus an ``optimizer`` field that this
          base class fills in.
        """

    def stop_reason(self) -> str | None:
        """Optional: return a strategy-emitted termination reason.

        The default implementation returns ``None`` (no opinion). The
        pipeline will still honour ``target_score`` and the global
        no-improve abort. CMA-ES uses this to surface
        ``cmaes.CMA.should_stop()`` once the population has converged.
        """
        return None

    def wants_global_no_improve_check(self) -> bool:
        """Return True if the pipeline's
        :func:`adjustment_algorithm.should_abort_global` rule should be
        applied to this strategy.

        ``HeuristicStrategy`` returns True (default) — its
        determinism means 4 consecutive non-improving moves really
        does mean it is stuck. ``CmaesStrategy`` returns False —
        CMA-ES is a stochastic sampler whose individual proposals
        are *expected* to be worse than the best-so-far, especially
        in the early generations of a 49-dim run. Letting
        ``GLOBAL_NO_IMPROVE_LIMIT=4`` abort it after 5 iterations
        crippled E-007's actual run (see [`Metric_Validation.md` § 5](../docs/Metric_Validation.md)
        for the diagnosis). E-010 routes around this by giving each
        strategy its own decision.
        """
        return True

    def research_summary(self) -> dict[str, Any]:
        """Optional optimizer-specific research diagnostics."""

        return {}


# ---------------------------------------------------------------------
# Heuristic strategy (existing stage-aware path)


class HeuristicStrategy(OptimizerStrategy):
    """Wraps :func:`adjustment_algorithm.propose_next_params` 1:1.

    This is the production strategy that has been driving the auto-adjust
    loop since E-002 ([`ExperimentLog.md`](../docs/ExperimentLog.md))
    fixed the stage-progression bug. It uses ``analysis.material_channels``
    feedback to pick a stage and propose channel-bias corrections
    inside that stage.
    """

    name = "heuristic"

    def __init__(
        self,
        policies: Sequence[AdjustmentStagePolicy],
        shader_params: Sequence[ShaderParam],
        unity_material_params: dict[str, Any] | None,
    ) -> None:
        self._policies = list(policies)
        self._shader_params = list(shader_params)
        self._unity_material_params = unity_material_params or {}

    def propose(self, ctx: StrategyContext) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._policies:
            return ctx.current_params, {
                "stop_reason": "no_policies",
                "optimizer": self.name,
                "stage": None,
            }
        policy, stage_transition = choose_stage(self._policies, ctx.analysis, ctx.state)
        if policy is None:
            return ctx.current_params, {
                "stop_reason": "no_policies",
                "optimizer": self.name,
                "stage": None,
                "stage_transition": stage_transition,
            }
        next_params, decision = propose_next_params(
            ctx.current_params,
            self._shader_params,
            ctx.analysis,
            policy,
            iteration=ctx.iteration,
            unity_material_params=self._unity_material_params,
        )
        decision["optimizer"] = self.name
        decision["stage_transition"] = stage_transition
        progress = update_stage_progress(ctx.state, policy, ctx.analysis)
        decision["stage_progress"] = progress
        if decision.get("stop_reason") == "no_effective_change":
            # Force stuck-detection so the next call advances stage even
            # when the channel score didn't move (E-002 contract).
            ctx.state.stage_no_improve = max(
                ctx.state.stage_no_improve, STUCK_NO_IMPROVE_LIMIT
            )
        return next_params, decision


# ---------------------------------------------------------------------
# CMA-ES strategy (cold or warm-started)


@dataclass(frozen=True)
class CmaesStrategyConfig:
    """User-tunable knobs surfaced to the UI / fit_config.json.

    ``mode``:
      * ``"cold"``  — vanilla CMA-ES seeded at the project's initial
        ``.lmat`` parameters. No prior history used.
      * ``"warm"``  — Warm-Started CMA-ES (Nomura et al., AAAI 2021).
        The pipeline supplies up to ``warm_start_iters`` (params,
        fit_score) pairs from previous heuristic iterations as the
        prior. Falls back to ``cold`` automatically when the project
        has no prior iterations.

    ``hint_bias_mix_ratio`` (E-010): blend the channel-level
    ``adjustment_hints`` produced by :mod:`vision.diff_analysis`
    into each CMA-ES proposal. ``0.0`` disables the bias and gives
    the legacy behaviour. ``0.30`` is the recommended starting
    point for stylised PBR materials. Values > ~0.5 will dominate
    the CMA-ES exploration and effectively turn the algorithm into
    coordinate descent driven by the hints — useful as a fast
    sanity check, less useful for final convergence.

    The remaining fields map directly to ``CmaesConfig`` and are
    optional; ``None`` means "use library default".
    """

    mode: str = "warm"
    warm_start_iters: int = 12
    population_size: int | None = None
    sigma: float | None = None
    seed: int | None = None
    hint_bias_mix_ratio: float = 0.30


class CmaesStrategy(OptimizerStrategy):
    """Black-box CMA-ES optimizer over the project's parameter dict.

    Per iteration:

    1. If we have a *previous* iteration's fitness, ``tell()`` it back
       so CMA-ES updates its distribution.
    2. ``ask()`` for a new candidate.
    3. Return that candidate as ``next_params`` plus a ``decision``
       dict recording the population/generation index, the warm-start
       state, and which axes changed since ``ctx.current_params``.
    """

    name = "cma_es"

    def __init__(
        self,
        *,
        initial_params: dict[str, Any],
        shader_params: Sequence[ShaderParam],
        config: CmaesStrategyConfig,
        warm_start_history: Sequence[tuple[dict[str, Any], float]] = (),
        semantic_graph: ShaderEffectGraph | None = None,
        param_whitelist: Sequence[str] | None = None,
    ) -> None:
        try:
            from .cma_es_optimizer import (  # noqa: WPS433 — lazy import
                CmaesConfig,
                CmaesOptimizer,
                ParameterEncoder,
            )
        except ImportError as exc:
            raise OptimizerUnavailableError(
                "CMA-ES optimizer requires the `cmaes` package. "
                "Install with: pip install cmaes"
            ) from exc

        self._config = config
        cma_config_kwargs: dict[str, Any] = {}
        if config.population_size is not None:
            cma_config_kwargs["population_size"] = int(config.population_size)
        if config.sigma is not None:
            cma_config_kwargs["sigma"] = float(config.sigma)
        if config.seed is not None:
            cma_config_kwargs["seed"] = int(config.seed)

        self._encoder = ParameterEncoder(
            initial_params,
            list(shader_params),
            param_whitelist=param_whitelist,
            semantics=semantic_graph,
        )
        if self._encoder.dim == 0:
            raise OptimizerUnavailableError(
                "CMA-ES has no trainable axes for this material — every "
                "parameter is either a texture binding, a tiling vector, "
                "or blacklisted. Switch to the heuristic optimizer."
            )

        warm_samples: list[tuple[dict[str, Any], float]] = []
        if config.mode == "warm" and warm_start_history:
            warm_samples = list(warm_start_history[: max(int(config.warm_start_iters), 0)])
        # WS-CMA-ES requires ≥2 samples to estimate covariance; fall
        # back to cold gracefully when we don't have enough.
        if len(warm_samples) < 2:
            warm_samples = []

        self._opt = CmaesOptimizer(
            self._encoder,
            config=CmaesConfig(**cma_config_kwargs),
            warm_start_samples=warm_samples or None,
            initial_mean=initial_params,
        )
        self._warm_started = self._opt.warm_started
        self._history_size_used = len(warm_samples)

        # CMA-ES runs in ask/tell pairs. We hold the *currently asked*
        # parameter set + the fitness from the *previous* completed
        # iteration so we can chain them on the next propose() call.
        self._pending_params: dict[str, Any] | None = None
        self._last_observed_fitness: float | None = None
        # Sample a first proposal eagerly so the caller doesn't have to
        # special-case "iter 0 has no previous fitness".
        self._asked_first = False

    @property
    def warm_started(self) -> bool:
        return self._warm_started

    @property
    def population_size(self) -> int:
        return self._opt.population_size

    @property
    def trainable_dim(self) -> int:
        return self._encoder.dim

    def stop_reason(self) -> str | None:
        if self._opt.should_stop():
            return "cmaes_should_stop"
        return None

    def wants_global_no_improve_check(self) -> bool:
        # E-010: CMA-ES is stochastic. A 4-consecutive-not-better
        # window is not evidence the run is stuck; it is the
        # *expected* behaviour for the first few generations.
        return False

    def propose(self, ctx: StrategyContext) -> tuple[dict[str, Any], dict[str, Any]]:
        import numpy as np  # local import — already a hard dep, but keep CMA-ES branch lazy

        # 1. Tell back the previous iteration's fitness, if we have a
        #    pending ask waiting for a response. CMA-ES is minimization,
        #    so loss = 1 - fit_score (clipped) translates "higher score
        #    is better" into the right direction.
        if self._pending_params is not None:
            loss = self._fit_score_to_loss(ctx.fit_score, ctx.diff_score)
            self._opt.tell(loss)
            self._last_observed_fitness = loss

        # 2. Build the optional E-010 hint-bias callback.
        hint_payload = self._build_hint_bias_payload(ctx.analysis)
        bias_callback = hint_payload["callback"]

        # 3. Ask for the next candidate (with bias if enabled).
        proposed = self._opt.ask(bias_callback=bias_callback)
        self._pending_params = proposed
        self._asked_first = True

        # 4. Compute changes (for transparency in decision.json).
        changes = self._diff_params(ctx.current_params, proposed)

        decision: dict[str, Any] = {
            "optimizer": self.name,
            "mode": self._config.mode,
            "stage": {"name": f"cma_{self._config.mode}", "description": "Black-box CMA-ES proposal"},
            "iteration_gain": None,
            "score": ctx.diff_score,
            "changes": changes,
            "stop_reason": self.stop_reason() or ("continue" if changes else "no_effective_change"),
            "cma_es": {
                "warm_started": self._warm_started,
                "warm_start_iters_used": self._history_size_used,
                "population_size": self.population_size,
                "trainable_dim": self.trainable_dim,
                "evaluations": self._opt.evaluations,
                "best_fitness": self._opt.best[1] if self._opt.evaluations > 0 else None,
                "last_loss_fed": self._last_observed_fitness,
                "hint_bias": {
                    "mix_ratio": self._config.hint_bias_mix_ratio,
                    "applied": hint_payload["applied"],
                    "n_axes_biased": hint_payload["n_axes_biased"],
                    "max_abs_delta": hint_payload["max_abs_delta"],
                    "channels_used": hint_payload["channels_used"],
                },
            },
        }
        return proposed, decision

    # -----------------------------------------------------------------
    # E-010 hint-bias machinery

    _SEVERITY_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.25}

    def _build_hint_bias_payload(self, analysis: dict[str, Any]) -> dict[str, Any]:
        """Compute (and stash diagnostics about) the per-axis hint bias.

        Returns a dict with:

        * ``callback`` — the function to pass to ``CmaesOptimizer.ask()``
          (or ``None`` when no bias is applicable).
        * ``applied`` — True when ``callback`` is non-None.
        * ``n_axes_biased`` — count of axes with non-zero delta.
        * ``max_abs_delta`` — worst-case delta magnitude (in original
          coordinate units), useful when debugging a runaway bias.
        * ``channels_used`` — list of channel names that contributed.

        The callback is built so it always clamps to the encoder's
        bounds — feeding CMA-ES out-of-range vectors will distort
        its covariance estimate and is never desired.
        """

        import numpy as np

        mix_ratio = float(self._config.hint_bias_mix_ratio)
        if mix_ratio <= 0.0:
            return {
                "callback": None,
                "applied": False,
                "n_axes_biased": 0,
                "max_abs_delta": 0.0,
                "channels_used": [],
            }

        hints = analysis.get("adjustment_hints") if isinstance(analysis, dict) else None
        if not isinstance(hints, list) or not hints:
            return {
                "callback": None,
                "applied": False,
                "n_axes_biased": 0,
                "max_abs_delta": 0.0,
                "channels_used": [],
            }

        bias_vec, channels_used = self._compute_hint_vector(hints, mix_ratio)
        n_axes_biased = int(np.count_nonzero(bias_vec))
        max_abs = float(np.max(np.abs(bias_vec))) if bias_vec.size else 0.0
        if n_axes_biased == 0:
            return {
                "callback": None,
                "applied": False,
                "n_axes_biased": 0,
                "max_abs_delta": 0.0,
                "channels_used": [],
            }

        lower = self._encoder.lower_bounds
        upper = self._encoder.upper_bounds

        def _bias_callback(vec_orig: "np.ndarray") -> "np.ndarray":
            biased = vec_orig + bias_vec
            return np.clip(biased, lower, upper)

        return {
            "callback": _bias_callback,
            "applied": True,
            "n_axes_biased": n_axes_biased,
            "max_abs_delta": max_abs,
            "channels_used": channels_used,
        }

    def _compute_hint_vector(
        self,
        hints: list[dict[str, Any]],
        mix_ratio: float,
    ) -> tuple["np.ndarray", list[str]]:
        """Translate channel-level hints into a per-axis delta vector.

        Algorithm (E-010, see ``ExperimentLog.md`` E-010 entry):

        1. For each axis, look up its parameter name and find every
           hint whose ``related_params`` contains that name (with
           wildcard ``*`` support — e.g. ``u_MetallicRemap*`` matches
           ``u_MetallicRemapMin/Max``).
        2. Each contributing hint signs its severity weight by its
           ``direction`` (+1 = increase, -1 = decrease, 0 = inspect).
        3. The total signed weight is multiplied by the axis's range
           and 5% of one mix step:

               delta_axis = signed_weight * 0.05 * (high - low) * mix_ratio

           5% is the same scale as our heuristic's ``iteration_gain``
           default, chosen so the bias is always smaller than CMA-ES's
           own per-step exploration sigma (~0.3 of normalised range)
           and therefore cannot dominate the search.

        Returns ``(delta_vector, contributing_channels)``.
        """

        import numpy as np

        axes = self._encoder.axes
        deltas = np.zeros(len(axes), dtype=np.float64)
        channels_used: set[str] = set()

        # Pre-build a list of (severity_weight, signed_direction,
        # related_params_lower) for fast iteration.
        compiled: list[tuple[float, float, list[str], str]] = []
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            severity = self._SEVERITY_WEIGHT.get(str(hint.get("severity") or "").lower(), 0.0)
            if severity <= 0.0:
                continue
            direction = str(hint.get("direction") or "").lower()
            if direction == "increase":
                sign = +1.0
            elif direction == "decrease":
                sign = -1.0
            else:
                continue
            params = hint.get("related_params") or []
            if not isinstance(params, list):
                continue
            params_lower = [str(p).lower() for p in params if isinstance(p, str)]
            channel_name = str(hint.get("channel") or "")
            compiled.append((severity, sign, params_lower, channel_name))

        if not compiled:
            return deltas, []

        for i, axis in enumerate(axes):
            axis_name = axis.param_name.lower()
            axis_range = float(axis.high - axis.low)
            if axis_range <= 0:
                continue
            signed_weight = 0.0
            local_channels: list[str] = []
            for severity, sign, params_lower, channel in compiled:
                if any(self._param_match(axis_name, p) for p in params_lower):
                    signed_weight += severity * sign
                    local_channels.append(channel)
            if signed_weight == 0.0:
                continue
            deltas[i] = signed_weight * 0.05 * axis_range * mix_ratio
            channels_used.update(local_channels)

        return deltas, sorted(channels_used)

    @staticmethod
    def _param_match(axis_name: str, hint_pattern: str) -> bool:
        """Match a hint's ``related_params`` entry against an encoder axis name.

        Supports trailing ``*`` wildcards (e.g. ``u_MetallicRemap*``)
        and falls back to exact equality otherwise.
        """

        if not hint_pattern:
            return False
        if hint_pattern.endswith("*"):
            return axis_name.startswith(hint_pattern[:-1])
        return axis_name == hint_pattern

    # -----------------------------------------------------------------
    # helpers

    @staticmethod
    def _fit_score_to_loss(fit_score: float, diff_score: float) -> float:
        """Map the higher-is-better fit_score (or RGB MAE fallback) into
        a CMA-ES minimization loss in [0, ~1]."""
        if math.isfinite(fit_score):
            return max(0.0, 1.0 - float(fit_score))
        if math.isfinite(diff_score):
            return max(0.0, float(diff_score))
        # No usable signal — feed a neutral 0.5 so CMA-ES doesn't
        # collapse to the worst sample of the generation.
        return 0.5

    @staticmethod
    def _diff_params(
        old: dict[str, Any],
        new: dict[str, Any],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, value in new.items():
            old_value = old.get(name)
            if isinstance(value, list) and isinstance(old_value, list) and len(value) == len(old_value):
                if any(
                    not _isclose(_to_number(a), _to_number(b))
                    for a, b in zip(old_value, value)
                ):
                    out.append({
                        "param": name,
                        "old": old_value,
                        "new": value,
                        "reason": "CMA-ES sample",
                    })
            elif isinstance(value, (int, float)) and isinstance(old_value, (int, float)):
                if not _isclose(float(value), float(old_value)):
                    out.append({
                        "param": name,
                        "old": old_value,
                        "new": value,
                        "reason": "CMA-ES sample",
                    })
        return out


SemanticGroupStrategy = ResponseDrivenSemanticStrategy



# ---------------------------------------------------------------------
# Strategy factory


def build_strategy(
    *,
    optimizer: str,
    initial_params: dict[str, Any],
    shader_params: Sequence[ShaderParam],
    policies: Sequence[AdjustmentStagePolicy],
    unity_material_params: dict[str, Any] | None,
    cma_es_config: CmaesStrategyConfig | None = None,
    warm_start_history: Sequence[tuple[dict[str, Any], float]] = (),
    semantic_graph: ShaderEffectGraph | dict[str, Any] | None = None,
    auto_adjust_mode: str = "fresh_fit",
) -> OptimizerStrategy:
    """Construct the requested strategy.

    ``optimizer`` is one of:

    * ``"heuristic"`` — current production path (E-002 stage-aware).
    * ``"cma_cold"`` — vanilla CMA-ES.
    * ``"cma_warm"`` — Warm-Started CMA-ES (E-006).
    * ``"semantic_group"`` — response-driven semantic scheduler.
    * ``"semantic_group_legacy_081"`` — preserved pattern-search baseline.
    * ``"subspace_cma_es"`` — expensive CMA-ES inside a semantic subspace.

    Unknown optimizer names raise :class:`ValueError` rather than
    silently falling back to the heuristic — silent fallbacks here
    would confuse research-time experiment comparisons.
    """
    optimizer = (optimizer or "heuristic").strip().lower()
    graph = semantic_graph if isinstance(semantic_graph, ShaderEffectGraph) else graph_from_dict(semantic_graph)
    if optimizer == "heuristic":
        return HeuristicStrategy(policies, shader_params, unity_material_params)
    if optimizer == "semantic_group":
        if graph is None:
            raise ValueError("semantic_group optimizer requires a semantic effect graph")
        return SemanticGroupStrategy(
            initial_params=initial_params,
            shader_params=shader_params,
            graph=graph,
            auto_adjust_mode=auto_adjust_mode,
        )
    if optimizer == "semantic_group_legacy_081":
        if graph is None:
            raise ValueError("semantic_group_legacy_081 optimizer requires a semantic effect graph")
        from .legacy_semantic_strategy import LegacySemanticGroupStrategy

        return LegacySemanticGroupStrategy(
            initial_params=initial_params,
            shader_params=shader_params,
            graph=graph,
            auto_adjust_mode=auto_adjust_mode,
        )
    if optimizer == "subspace_cma_es":
        if graph is None:
            raise ValueError("subspace_cma_es optimizer requires a semantic effect graph")
        from .subspace_cma_strategy import SubspaceCmaEsStrategy

        config = cma_es_config or CmaesStrategyConfig()
        return SubspaceCmaEsStrategy(
            initial_params=initial_params,
            shader_params=shader_params,
            graph=graph,
            population_size=config.population_size,
            sigma=config.sigma,
            seed=config.seed,
        )
    if optimizer in ("cma_cold", "cma_warm"):
        config = cma_es_config or CmaesStrategyConfig()
        config = CmaesStrategyConfig(
            mode="cold" if optimizer == "cma_cold" else "warm",
            warm_start_iters=config.warm_start_iters,
            population_size=config.population_size,
            sigma=config.sigma,
            seed=config.seed,
            hint_bias_mix_ratio=config.hint_bias_mix_ratio,
        )
        return CmaesStrategy(
            initial_params=initial_params,
            shader_params=shader_params,
            config=config,
            warm_start_history=warm_start_history if optimizer == "cma_warm" else (),
            semantic_graph=graph,
            param_whitelist=(graph.active_search_params() if graph else None),
        )
    raise ValueError(
        f"unknown optimizer: {optimizer!r} "
        "(expected 'heuristic', 'cma_cold', 'cma_warm', 'semantic_group', "
        "'semantic_group_legacy_081', or 'subspace_cma_es')"
    )


def cmaes_strategy_config_from_dict(data: dict[str, Any] | None) -> CmaesStrategyConfig:
    """Lenient dict→config helper for fit_config.json / project.json."""
    if not isinstance(data, dict):
        return CmaesStrategyConfig()
    mode = data.get("mode")
    raw_mix = data.get("hint_bias_mix_ratio", 0.30)
    try:
        mix_ratio = float(raw_mix)
    except (TypeError, ValueError):
        mix_ratio = 0.30
    if not math.isfinite(mix_ratio) or mix_ratio < 0.0:
        mix_ratio = 0.0
    if mix_ratio > 1.0:
        mix_ratio = 1.0
    return CmaesStrategyConfig(
        mode=str(mode).strip().lower() if isinstance(mode, str) and mode else "warm",
        warm_start_iters=int(data.get("warm_start_iters", 12)),
        population_size=_optional_int(data.get("population_size")),
        sigma=_optional_float(data.get("sigma")),
        seed=_optional_int(data.get("seed")),
        hint_bias_mix_ratio=mix_ratio,
    )


def cmaes_strategy_config_to_dict(config: CmaesStrategyConfig) -> dict[str, Any]:
    return asdict(config)


# ---------------------------------------------------------------------
# tiny utilities


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _to_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _isclose(a: float, b: float, *, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


__all__ = [
    "CmaesStrategy",
    "CmaesStrategyConfig",
    "HeuristicStrategy",
    "SemanticGroupStrategy",
    "OptimizerStrategy",
    "OptimizerUnavailableError",
    "StrategyContext",
    "build_strategy",
    "cmaes_strategy_config_from_dict",
    "cmaes_strategy_config_to_dict",
]

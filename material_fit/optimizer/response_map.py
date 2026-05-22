"""Online parameter-to-metric response model.

This module is intentionally lightweight: it is not a surrogate renderer and
does not try to learn a full high-dimensional objective.  It records which
parameters changed, which metric losses improved/worsened, and whether the
trial contained enough signal to be useful.  The optimizer can then prefer
parameters with stable evidence and avoid accepting zero-gain exploratory
steps as if they were meaningful valley crossings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .search_evidence import MetricVector


@dataclass
class MetricResponseStats:
    """EMA response of one parameter to one metric loss."""

    gain_ema: float = 0.0
    abs_gain_ema: float = 0.0
    positive_count: int = 0
    negative_count: int = 0

    def observe(self, value: float, alpha: float) -> None:
        value = float(value)
        self.gain_ema = (1.0 - alpha) * self.gain_ema + alpha * value
        self.abs_gain_ema = (1.0 - alpha) * self.abs_gain_ema + alpha * abs(value)
        if value > 0.0:
            self.positive_count += 1
        elif value < 0.0:
            self.negative_count += 1

    @property
    def consistency(self) -> float:
        total = self.positive_count + self.negative_count
        if total <= 0:
            return 0.0
        return abs(self.positive_count - self.negative_count) / total

    def to_dict(self) -> dict[str, Any]:
        return {
            "gain_ema": self.gain_ema,
            "abs_gain_ema": self.abs_gain_ema,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "consistency": self.consistency,
        }


@dataclass
class ParamResponseStats:
    """Evidence accumulated for one shader parameter."""

    attempts: int = 0
    significant_trials: int = 0
    no_effect_trials: int = 0
    accepted_trials: int = 0
    fit_gain_ema: float = 0.0
    risk_ema: float = 0.0
    metrics: dict[str, MetricResponseStats] = field(default_factory=dict)
    context_scores: dict[str, float] = field(default_factory=dict)
    direction_scores: dict[str, float] = field(default_factory=dict)

    def observe(
        self,
        *,
        fit_delta: float,
        component_gains: dict[str, float],
        risk: float,
        accepted: bool,
        significant: bool,
        context_key: str,
        direction: float,
        alpha: float,
        weight: float,
    ) -> None:
        self.attempts += 1
        if significant:
            self.significant_trials += 1
        else:
            self.no_effect_trials += 1
        if accepted:
            self.accepted_trials += 1
        weighted_fit = float(fit_delta) * float(weight)
        self.fit_gain_ema = (1.0 - alpha) * self.fit_gain_ema + alpha * weighted_fit
        self.risk_ema = (1.0 - alpha) * self.risk_ema + alpha * max(float(risk), 0.0) * float(weight)
        for key, value in component_gains.items():
            self.metrics.setdefault(key, MetricResponseStats()).observe(float(value) * float(weight), alpha)
        self.context_scores[context_key] = (
            (1.0 - alpha) * self.context_scores.get(context_key, 0.0)
            + alpha * weighted_fit
        )
        direction_key = "positive" if direction >= 0.0 else "negative"
        self.direction_scores[direction_key] = (
            (1.0 - alpha) * self.direction_scores.get(direction_key, 0.0)
            + alpha * weighted_fit
        )

    @property
    def no_effect_rate(self) -> float:
        return self.no_effect_trials / max(self.attempts, 1)

    @property
    def confidence(self) -> float:
        # Confidence grows with significant evidence but is penalized when
        # most trials are below the measurable signal floor.
        evidence = self.significant_trials / max(self.attempts, 1)
        sample = min(1.0, math.log1p(self.attempts) / math.log(8.0))
        return max(0.0, min(1.0, evidence * sample))

    @property
    def context_sensitivity(self) -> float:
        if len(self.context_scores) <= 1:
            return 0.0
        values = list(self.context_scores.values())
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return math.sqrt(max(variance, 0.0))

    @property
    def recommended_direction(self) -> str:
        pos = self.direction_scores.get("positive", 0.0)
        neg = self.direction_scores.get("negative", 0.0)
        if abs(pos - neg) < 1.0e-9:
            return "unknown"
        return "positive" if pos > neg else "negative"

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "significant_trials": self.significant_trials,
            "no_effect_trials": self.no_effect_trials,
            "no_effect_rate": self.no_effect_rate,
            "accepted_trials": self.accepted_trials,
            "fit_gain_ema": self.fit_gain_ema,
            "risk_ema": self.risk_ema,
            "confidence": self.confidence,
            "context_sensitivity": self.context_sensitivity,
            "recommended_direction": self.recommended_direction,
            "metrics": {key: value.to_dict() for key, value in self.metrics.items()},
        }


class ResponseMap:
    """Evidence map: parameter -> metric component response."""

    def __init__(
        self,
        *,
        alpha: float = 0.30,
        min_score_signal: float = 5.0e-4,
        min_component_signal: float = 3.0e-3,
    ) -> None:
        self.alpha = max(0.01, min(1.0, float(alpha)))
        self.min_score_signal = max(0.0, float(min_score_signal))
        self.min_component_signal = max(0.0, float(min_component_signal))
        self._params: dict[str, ParamResponseStats] = {}
        self._pair_trials: dict[tuple[str, str], int] = {}
        self._pair_gain_ema: dict[tuple[str, str], float] = {}
        self.trial_count = 0
        self.significant_trial_count = 0

    def observe_trial(
        self,
        *,
        changed_params: Iterable[str],
        before_params: dict[str, Any],
        after_params: dict[str, Any],
        before: MetricVector,
        after: MetricVector,
        fit_delta: float,
        accepted: bool,
        candidate_kind: str = "",
        context_key: str = "",
    ) -> dict[str, Any]:
        del candidate_kind  # Reserved for weighting policy extensions.
        names = sorted({str(item) for item in changed_params if item})
        if not names:
            return {"significant": False, "changed_params": []}
        self.trial_count += 1
        component_gains = {
            key: before.components.get(key, 0.0) - after.components.get(key, 0.0)
            for key in set(before.components) | set(after.components)
        }
        component_signal = max((abs(value) for value in component_gains.values()), default=0.0)
        significant = abs(float(fit_delta)) >= self.min_score_signal or component_signal >= self.min_component_signal
        if significant:
            self.significant_trial_count += 1
        risk = self._risk(before, after)
        weight = 1.0 / max(len(names), 1)
        for name in names:
            direction = _param_direction(before_params.get(name), after_params.get(name))
            self._params.setdefault(name, ParamResponseStats()).observe(
                fit_delta=float(fit_delta),
                component_gains=component_gains,
                risk=risk,
                accepted=accepted,
                significant=significant,
                context_key=context_key,
                direction=direction,
                alpha=self.alpha,
                weight=weight,
            )
        if len(names) >= 2:
            for pair in _pairs(names):
                self._pair_trials[pair] = self._pair_trials.get(pair, 0) + 1
                old = self._pair_gain_ema.get(pair, 0.0)
                self._pair_gain_ema[pair] = (1.0 - self.alpha) * old + self.alpha * float(fit_delta)
        return {
            "significant": significant,
            "changed_params": names,
            "component_signal": component_signal,
            "component_gains": component_gains,
        }

    def priority_for(
        self,
        param: str,
        bottleneck: dict[str, float],
        *,
        semantic_relevance: float = 0.0,
    ) -> float:
        stats = self._params.get(param)
        if stats is None:
            return max(float(semantic_relevance), 0.0) + 0.040
        component_score = 0.0
        consistency_score = 0.0
        for key, need in bottleneck.items():
            metric = stats.metrics.get(key)
            if metric is None:
                continue
            component_score += max(metric.gain_ema, 0.0) * max(float(need), 0.0)
            consistency_score += metric.consistency * max(float(need), 0.0) * 0.01
        exploration = 0.030 / math.sqrt(stats.attempts + 1.0)
        no_effect_penalty = 0.050 * stats.no_effect_rate if stats.attempts >= 3 else 0.0
        risk_penalty = max(stats.risk_ema, 0.0)
        return (
            max(float(semantic_relevance), 0.0)
            + max(stats.fit_gain_ema, 0.0)
            + component_score
            + consistency_score
            + exploration
            - no_effect_penalty
            - risk_penalty
        )

    def summary_for(self, param: str, bottleneck: dict[str, float], *, semantic_relevance: float = 0.0) -> dict[str, Any]:
        stats = self._params.get(param)
        base = stats.to_dict() if stats is not None else {
            "attempts": 0,
            "significant_trials": 0,
            "no_effect_trials": 0,
            "no_effect_rate": 0.0,
            "accepted_trials": 0,
            "fit_gain_ema": 0.0,
            "risk_ema": 0.0,
            "confidence": 0.0,
            "context_sensitivity": 0.0,
            "recommended_direction": "unknown",
            "metrics": {},
        }
        base["response_priority"] = self.priority_for(param, bottleneck, semantic_relevance=semantic_relevance)
        base["interaction_suspicion"] = self.interaction_suspicion(param)
        return base

    def interaction_suspicion(self, param: str) -> float:
        values = [
            max(self._pair_gain_ema.get(pair, 0.0), 0.0)
            for pair in self._pair_trials
            if param in pair
        ]
        if not values:
            return 0.0
        return max(values)

    def supports_exploratory_accept(
        self,
        *,
        changed_params: Iterable[str],
        fit_delta: float,
        component_gain: float,
        bottleneck: dict[str, float],
    ) -> bool:
        names = [str(item) for item in changed_params if item]
        if not names:
            return False
        if abs(float(fit_delta)) >= self.min_score_signal:
            return True
        if float(component_gain) >= self.min_component_signal:
            return True
        # If there is no measured signal, only continue when at least one
        # changed parameter has strong prior evidence for the active bottleneck.
        for name in names:
            stats = self._params.get(name)
            if stats is None or stats.confidence < 0.35 or stats.no_effect_rate > 0.60:
                continue
            if self.priority_for(name, bottleneck) > 0.035:
                return True
        return False

    def summary(self, bottleneck: dict[str, float], limit: int = 12) -> dict[str, Any]:
        rows = [
            {"param": name, **self.summary_for(name, bottleneck)}
            for name in self._params
        ]
        rows.sort(key=lambda item: float(item.get("response_priority", 0.0)), reverse=True)
        return {
            "trial_count": self.trial_count,
            "significant_trial_count": self.significant_trial_count,
            "top_params": rows[: max(0, int(limit))],
            "top_pairs": self._top_pairs(limit=limit),
        }

    def _top_pairs(self, limit: int) -> list[dict[str, Any]]:
        pairs = [
            {
                "params": list(pair),
                "trials": self._pair_trials.get(pair, 0),
                "fit_gain_ema": self._pair_gain_ema.get(pair, 0.0),
            }
            for pair in self._pair_trials
        ]
        pairs.sort(key=lambda item: float(item.get("fit_gain_ema", 0.0)), reverse=True)
        return pairs[: max(0, int(limit))]

    @staticmethod
    def _risk(before: MetricVector, after: MetricVector) -> float:
        if before.worst_fit_score is None or after.worst_fit_score is None:
            return 0.0
        return max(float(before.worst_fit_score) - float(after.worst_fit_score), 0.0)


def context_key_from_metrics(metrics: MetricVector) -> str:
    if not metrics.components:
        return "unknown"
    primary = max(metrics.components.items(), key=lambda item: float(item[1]))[0]
    score = metrics.fit_score
    if score is None or not math.isfinite(float(score)):
        band = "score_unknown"
    elif score < 0.45:
        band = "score_low"
    elif score < 0.70:
        band = "score_mid"
    else:
        band = "score_high"
    return f"{band}:{primary}"


def _param_direction(before: Any, after: Any) -> float:
    b = _numeric_projection(before)
    a = _numeric_projection(after)
    if a is None or b is None:
        return 1.0
    delta = a - b
    if abs(delta) < 1.0e-12:
        return 1.0
    return 1.0 if delta > 0.0 else -1.0


def _numeric_projection(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, list):
        nums = [float(item) for item in value if isinstance(item, (int, float)) and not isinstance(item, bool)]
        if not nums:
            return None
        return sum(nums) / len(nums)
    return None


def _pairs(names: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for left_index, left in enumerate(names):
        for right in names[left_index + 1:]:
            out.append((left, right))
    return out


__all__ = [
    "MetricResponseStats",
    "ParamResponseStats",
    "ResponseMap",
    "context_key_from_metrics",
]

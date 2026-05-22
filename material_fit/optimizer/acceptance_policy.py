"""Acceptance policy for evidence-driven semantic search."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .search_evidence import MetricVector


@dataclass(frozen=True)
class AcceptanceDecision:
    accepted: bool
    provisional: bool
    reason: str
    fit_delta: float
    min_improvement: float
    component_gain: float
    worst_view_delta: float | None = None
    risk_penalty: float = 0.0
    improved_components: dict[str, float] = field(default_factory=dict)
    worsened_components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "provisional": self.provisional,
            "reason": self.reason,
            "fit_delta": self.fit_delta,
            "min_improvement": self.min_improvement,
            "component_gain": self.component_gain,
            "worst_view_delta": self.worst_view_delta,
            "risk_penalty": self.risk_penalty,
            "improved_components": dict(self.improved_components),
            "worsened_components": dict(self.worsened_components),
        }


class AcceptancePolicy:
    """Blend global score, component bottlenecks, and worst-view risk."""

    def __init__(
        self,
        *,
        provisional_fit_drop: float = 0.006,
        component_gain_threshold: float = 0.030,
        worst_view_drop_soft_limit: float = 0.050,
        bottleneck_worsen_soft_limit: float = 0.045,
    ) -> None:
        self.provisional_fit_drop = max(0.0, float(provisional_fit_drop))
        self.component_gain_threshold = max(0.0, float(component_gain_threshold))
        self.worst_view_drop_soft_limit = max(0.0, float(worst_view_drop_soft_limit))
        self.bottleneck_worsen_soft_limit = max(0.0, float(bottleneck_worsen_soft_limit))
        self.provisional_accept_count = 0
        self.rejected_by_worst_view_count = 0
        self.rejected_by_bottleneck_count = 0

    def evaluate(
        self,
        *,
        base: MetricVector,
        candidate: MetricVector,
        fit_delta: float,
        min_improvement: float,
        phase: str,
        force_accept: bool = False,
    ) -> AcceptanceDecision:
        if force_accept:
            return AcceptanceDecision(
                accepted=True,
                provisional=False,
                reason="force_accept",
                fit_delta=fit_delta,
                min_improvement=min_improvement,
                component_gain=0.0,
            )
        bottleneck_worsening = self._primary_bottleneck_worsening(base, candidate)
        if (
            bottleneck_worsening
            and max(bottleneck_worsening.values()) > self.bottleneck_worsen_soft_limit
            and fit_delta < min_improvement * 3.0
        ):
            self.rejected_by_bottleneck_count += 1
            component_gain, improved = self._component_gain(base, candidate)
            return AcceptanceDecision(
                accepted=False,
                provisional=False,
                reason="primary_bottleneck_worsened",
                fit_delta=fit_delta,
                min_improvement=min_improvement,
                component_gain=component_gain,
                worst_view_delta=self._worst_view_delta(base, candidate),
                improved_components=improved,
                worsened_components=bottleneck_worsening,
            )

        if fit_delta >= min_improvement:
            component_gain, improved = self._component_gain(base, candidate)
            return AcceptanceDecision(
                accepted=True,
                provisional=False,
                reason="fit_score_improved",
                fit_delta=fit_delta,
                min_improvement=min_improvement,
                component_gain=component_gain,
                worst_view_delta=self._worst_view_delta(base, candidate),
                improved_components=improved,
                worsened_components=bottleneck_worsening,
            )

        component_gain, improved = self._component_gain(base, candidate)
        worst_delta = self._worst_view_delta(base, candidate)
        risk_penalty = self._risk_penalty(worst_delta)
        net_component_gain = max(component_gain - risk_penalty, 0.0)
        exploratory_component_phase = phase in {
            "breakthrough",
            "pair_probe",
            "subspace_batch",
            "archive_restart",
        }
        within_drop = fit_delta >= -self.provisional_fit_drop
        if exploratory_component_phase and within_drop and net_component_gain >= self.component_gain_threshold:
            self.provisional_accept_count += 1
            return AcceptanceDecision(
                accepted=True,
                provisional=True,
                reason="component_bottleneck_improved",
                fit_delta=fit_delta,
                min_improvement=min_improvement,
                component_gain=component_gain,
                worst_view_delta=worst_delta,
                risk_penalty=risk_penalty,
                improved_components=improved,
                worsened_components=bottleneck_worsening,
            )
        if risk_penalty > 0.0:
            self.rejected_by_worst_view_count += 1
        return AcceptanceDecision(
            accepted=False,
            provisional=False,
            reason="insufficient_gain",
            fit_delta=fit_delta,
            min_improvement=min_improvement,
            component_gain=component_gain,
            worst_view_delta=worst_delta,
            risk_penalty=risk_penalty,
            improved_components=improved,
        )

    def summary(self) -> dict[str, int]:
        return {
            "provisional_accept_count": self.provisional_accept_count,
            "rejected_by_worst_view_count": self.rejected_by_worst_view_count,
            "rejected_by_bottleneck_count": self.rejected_by_bottleneck_count,
        }

    @staticmethod
    def _component_gain(base: MetricVector, candidate: MetricVector) -> tuple[float, dict[str, float]]:
        improvements: dict[str, float] = {}
        for key in set(base.components) | set(candidate.components):
            before = float(base.components.get(key, 0.0))
            after = float(candidate.components.get(key, 0.0))
            gain = before - after
            if gain > 0.0:
                improvements[key] = gain
        return sum(improvements.values()), improvements

    def _risk_penalty(self, worst_delta: float | None) -> float:
        if worst_delta is None or not math.isfinite(worst_delta):
            return 0.0
        if worst_delta >= -self.worst_view_drop_soft_limit:
            return 0.0
        return abs(worst_delta) - self.worst_view_drop_soft_limit

    @staticmethod
    def _worst_view_delta(base: MetricVector, candidate: MetricVector) -> float | None:
        if base.worst_fit_score is None or candidate.worst_fit_score is None:
            return None
        return float(candidate.worst_fit_score) - float(base.worst_fit_score)

    @staticmethod
    def _primary_bottleneck_worsening(base: MetricVector, candidate: MetricVector) -> dict[str, float]:
        if not base.components:
            return {}
        primary_key = max(base.components.items(), key=lambda item: float(item[1]))[0]
        before = float(base.components.get(primary_key, 0.0))
        after = float(candidate.components.get(primary_key, before))
        worsening = after - before
        if worsening <= 0.0:
            return {}
        return {primary_key: worsening}


__all__ = ["AcceptanceDecision", "AcceptancePolicy"]

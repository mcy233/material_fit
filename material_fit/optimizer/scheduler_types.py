from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TrialIntent:
    """One optimizer experiment selected by the scheduler."""

    kind: str
    proposed_params: dict[str, Any]
    changed_params: list[str]
    reason: str
    stage_name: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_pending(
        self,
        *,
        base_params: dict[str, Any],
        base_fit_score: float,
        base_metrics: Any,
    ) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "base_params": dict(base_params),
            "base_fit_score": float(base_fit_score),
            "base_metrics": base_metrics,
            "changed_params": list(self.changed_params),
            "stage_name": self.stage_name,
            **dict(self.payload),
        }


@dataclass(frozen=True)
class TrialResult:
    """Result from the previously rendered trial."""

    kind: str
    accepted: bool
    outcome: str
    fit_score: float
    base_fit_score: float
    delta: float
    changed_params: list[str]
    next_base_params: dict[str, Any]
    response_observation: dict[str, Any]
    acceptance: dict[str, Any]


@dataclass(frozen=True)
class SchedulerSnapshot:
    """JSON-friendly scheduler state for UI/debugging."""

    planner_phase: str
    trial_kind: str
    trial_reason: str
    component_bottleneck: dict[str, float]
    param_ranking: list[dict[str, Any]]
    param_candidate_pool: list[dict[str, Any]]
    budget_state: dict[str, Any]
    evidence_status: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "planner_phase": self.planner_phase,
            "trial_kind": self.trial_kind,
            "trial_reason": self.trial_reason,
            "component_bottleneck": dict(self.component_bottleneck),
            "param_ranking": list(self.param_ranking),
            "param_candidate_pool": list(self.param_candidate_pool),
            "budget_state": dict(self.budget_state),
            "evidence_status": dict(self.evidence_status),
        }


__all__ = ["SchedulerSnapshot", "TrialIntent", "TrialResult"]

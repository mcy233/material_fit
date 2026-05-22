"""Unified response-driven experiment scheduler."""

from __future__ import annotations

import math
from typing import Any, Sequence

from .candidate_builder import CandidateBuilder, diff_params
from .response_map import ResponseMap
from .scheduler_types import SchedulerSnapshot, TrialIntent
from .search_evidence import TopKArchive
from .semantic_graph import ShaderEffectGraph


class ExperimentPlanner:
    """Select the next optimizer trial from a single policy surface."""

    def __init__(
        self,
        *,
        graph: ShaderEffectGraph,
        builder: CandidateBuilder,
        response_map: ResponseMap,
        archive: TopKArchive,
        param_candidate_pool_size: int = 10,
    ) -> None:
        self.graph = graph
        self.builder = builder
        self.response_map = response_map
        self.archive = archive
        self.param_candidate_pool_size = max(3, int(param_candidate_pool_size))
        self.calibration_min_attempts = 2
        self.calibration_budget = min(72, max(16, len(self.graph.params) * 2))
        self.pair_budget = 32
        self.subspace_budget = 24
        self.archive_restart_budget = 8
        self.single_param_soft_cap = 24
        self.single_param_hard_cap = 48
        self.no_effect_cooldown_attempts = 20
        self.no_effect_cooldown_rate = 0.55
        self.recent_window = 40
        self.recent_param_share_limit = 0.25
        self.calibration_started = 0
        self.pair_started = 0
        self.subspace_started = 0
        self.archive_restart_started = 0
        self._last_snapshot: SchedulerSnapshot | None = None
        self._recent_best: list[tuple[int, float]] = []
        self._recent_params: list[str] = []
        self._subspace_cursor = 0

    @property
    def last_snapshot(self) -> SchedulerSnapshot | None:
        return self._last_snapshot

    def next_trial(
        self,
        *,
        base_params: dict[str, Any],
        analysis: dict[str, Any],
        fit_score: float,
        iteration: int,
        ranking: list[dict[str, Any]],
        bottleneck: dict[str, float],
    ) -> TrialIntent | None:
        self._remember_fit(iteration, fit_score)
        if self._plateau_like():
            trial = (
                self._archive_restart_trial(base_params, fit_score, bottleneck)
                or self._subspace_trial(base_params, iteration, ranking)
                or self._pair_trial(base_params, iteration, ranking)
                or self._single_param_trial(base_params, ranking)
            )
        else:
            trial = (
                self._calibration_trial(base_params, analysis, ranking)
                or self._pair_trial(base_params, iteration, ranking)
                or self._single_param_trial(base_params, ranking)
                or self._archive_restart_trial(base_params, fit_score, bottleneck)
            )
        if trial is None and ranking:
            trial = self._fallback_probe(base_params, ranking)
        self._remember_trial(trial)
        self._last_snapshot = self.snapshot(
            trial=trial,
            bottleneck=bottleneck,
            ranking=ranking,
        )
        return trial

    def snapshot(
        self,
        *,
        trial: TrialIntent | None,
        bottleneck: dict[str, float],
        ranking: list[dict[str, Any]],
    ) -> SchedulerSnapshot:
        pool = ranking[: self.param_candidate_pool_size]
        return SchedulerSnapshot(
            planner_phase=self._planner_phase(),
            trial_kind=trial.kind if trial else "none",
            trial_reason=trial.reason if trial else "no effective candidate",
            component_bottleneck=bottleneck,
            param_ranking=ranking,
            param_candidate_pool=pool,
            budget_state=self.budget_state(),
            evidence_status=self.evidence_status(ranking),
        )

    def budget_state(self) -> dict[str, Any]:
        return {
            "calibration": {"started": self.calibration_started, "budget": self.calibration_budget},
            "pair": {"started": self.pair_started, "budget": self.pair_budget},
            "subspace": {"started": self.subspace_started, "budget": self.subspace_budget},
            "archive_restart": {"started": self.archive_restart_started, "budget": self.archive_restart_budget},
            "recent_param_counts": self._recent_param_counts(),
            "recent_window": self.recent_window,
            "recent_param_share_limit": self.recent_param_share_limit,
        }

    def evidence_status(self, ranking: Sequence[dict[str, Any]]) -> dict[str, Any]:
        attempted = [row for row in ranking if int(row.get("response_attempts", 0) or 0) > 0]
        confident = [row for row in ranking if float(row.get("response_confidence", 0.0) or 0.0) >= 0.35]
        no_effect = [row for row in ranking if float(row.get("response_no_effect_rate", 0.0) or 0.0) >= 0.60]
        return {
            "response_trials": self.response_map.trial_count,
            "significant_trials": self.response_map.significant_trial_count,
            "attempted_param_count": len(attempted),
            "confident_param_count": len(confident),
            "high_no_effect_param_count": len(no_effect),
        }

    def _calibration_trial(
        self,
        base_params: dict[str, Any],
        analysis: dict[str, Any],
        ranking: list[dict[str, Any]],
    ) -> TrialIntent | None:
        del analysis
        if self.calibration_started >= self.calibration_budget:
            return None
        rows = [
            row for row in ranking[: self.param_candidate_pool_size]
            if int(row.get("response_attempts", 0) or 0) < self.calibration_min_attempts
        ]
        rows.sort(
            key=lambda row: (
                -float(row.get("priority", 0.0) or 0.0),
                int(row.get("response_attempts", 0) or 0),
            )
        )
        for row in rows:
            name = str(row.get("param") or "")
            if name not in base_params:
                continue
            attempts = int(row.get("response_attempts", 0) or 0)
            direction = 1.0 if attempts % 2 == 0 else -1.0
            result = self.builder.nudge_param_candidate(
                base_params=base_params,
                param_name=name,
                step_scale=1.15,
                group_cycle=0,
                direction_override=direction,
            )
            if result is None:
                continue
            proposed, payload = result
            self.calibration_started += 1
            payload.update({"row": row, "calibration_attempt": attempts + 1})
            return TrialIntent(
                kind="calibration_probe",
                proposed_params=proposed,
                changed_params=list(payload.get("changed_params", [])),
                reason=f"calibrate {name} direction {'+' if direction > 0 else '-'} for response evidence",
                stage_name=str(row.get("group") or "calibration"),
                payload=payload,
            )
        return None

    def _single_param_trial(self, base_params: dict[str, Any], ranking: list[dict[str, Any]]) -> TrialIntent | None:
        for row in ranking[: self.param_candidate_pool_size]:
            name = str(row.get("param") or "")
            if name not in base_params:
                continue
            attempts = int(row.get("response_attempts", 0) or 0)
            confidence = float(row.get("response_confidence", 0.0) or 0.0)
            no_effect = float(row.get("response_no_effect_rate", 0.0) or 0.0)
            if attempts < self.calibration_min_attempts or self._single_param_cooled_down(name, attempts, confidence, no_effect):
                continue
            direction = _direction_from_row(row)
            result = self.builder.nudge_param_candidate(
                base_params=base_params,
                param_name=name,
                step_scale=0.85 if confidence >= 0.35 else 0.65,
                group_cycle=min(attempts // 4, 3),
                direction_override=direction,
            )
            if result is None:
                continue
            proposed, payload = result
            payload.update({"row": row})
            return TrialIntent(
                kind="single_param",
                proposed_params=proposed,
                changed_params=list(payload.get("changed_params", [])),
                reason=f"continue stable response parameter {name}",
                stage_name=str(row.get("group") or "single_param"),
                payload=payload,
            )
        return None

    def _single_param_cooled_down(self, name: str, attempts: int, confidence: float, no_effect: float) -> bool:
        if attempts >= self.single_param_hard_cap:
            return True
        if attempts >= self.no_effect_cooldown_attempts and no_effect >= self.no_effect_cooldown_rate:
            return True
        if attempts >= self.single_param_soft_cap and (confidence < 0.45 or no_effect >= 0.35):
            return True
        counts = self._recent_param_counts()
        recent_total = max(len(self._recent_params), 1)
        if counts.get(name, 0) / recent_total > self.recent_param_share_limit:
            return True
        return False

    def _pair_trial(
        self,
        base_params: dict[str, Any],
        iteration: int,
        ranking: list[dict[str, Any]],
    ) -> TrialIntent | None:
        if self.pair_started >= self.pair_budget or self.response_map.trial_count < 6:
            return None
        if int(iteration) % 3 != 0:
            return None
        eligible = [
            row for row in ranking[: max(8, self.param_candidate_pool_size)]
            if str(row.get("param") or "") in base_params
            and int(row.get("response_attempts", 0) or 0) >= self.calibration_min_attempts
            and (
                float(row.get("interaction_suspicion", 0.0) or 0.0) > 0.0
                or float(row.get("response_no_effect_rate", 0.0) or 0.0) >= 0.45
                or float(row.get("response_context_sensitivity", 0.0) or 0.0) > 0.0
            )
        ]
        pair = self._diverse_pair(eligible)
        if len(pair) < 2:
            return None
        directions = [_direction_from_row(row) for row in pair]
        result = self.builder.build_subspace_candidate(
            base_params=base_params,
            param_rows=pair,
            directions=directions,
            group_cycle=1,
            step_scale=0.50,
        )
        if result is None:
            return None
        proposed, payload = result
        self.pair_started += 1
        payload.update({"pair_params": [row.get("param") for row in pair], "rows": pair})
        return TrialIntent(
            kind="pair_probe",
            proposed_params=proposed,
            changed_params=list(payload.get("changed_params", [])),
            reason="test coupled parameters after weak or context-sensitive single responses",
            stage_name="pair_probe",
            payload=payload,
        )

    def _subspace_trial(
        self,
        base_params: dict[str, Any],
        iteration: int,
        ranking: list[dict[str, Any]],
    ) -> TrialIntent | None:
        if self.subspace_started >= self.subspace_budget or not self._plateau_like():
            return None
        rows = [row for row in ranking[:8] if str(row.get("param") or "") in base_params]
        if len(rows) < 3:
            return None
        directions = self._subspace_directions(len(rows), iteration)
        result = self.builder.build_subspace_candidate(
            base_params=base_params,
            param_rows=rows,
            directions=directions,
            group_cycle=2,
            step_scale=0.32,
        )
        if result is None:
            return None
        proposed, payload = result
        self.subspace_started += 1
        payload.update({"subspace_params": [row.get("param") for row in rows], "rows": rows})
        return TrialIntent(
            kind="subspace_batch",
            proposed_params=proposed,
            changed_params=list(payload.get("changed_params", [])),
            reason="plateau-like history: evaluate sparse low-dimensional joint move",
            stage_name="subspace_batch",
            payload=payload,
        )

    def _archive_restart_trial(
        self,
        base_params: dict[str, Any],
        fit_score: float,
        bottleneck: dict[str, float],
    ) -> TrialIntent | None:
        if self.archive_restart_started >= self.archive_restart_budget or not self._plateau_like(long=True):
            return None
        restart = self.archive.select_restart(
            bottleneck=bottleneck,
            current_params=base_params,
            min_fit_score=max(float(fit_score) - 0.18, 0.0),
        )
        if not restart or not isinstance(restart.get("params"), dict):
            return None
        proposed = dict(restart["params"])
        changes = diff_params(base_params, proposed)
        if not changes:
            return None
        self.archive_restart_started += 1
        return TrialIntent(
            kind="archive_restart",
            proposed_params=proposed,
            changed_params=[str(change.get("param")) for change in changes],
            reason="long plateau: restart from archived checkpoint branch",
            stage_name="archive_restart",
            payload={"archive_restart": {key: value for key, value in restart.items() if key != "params"}},
        )

    def _fallback_probe(self, base_params: dict[str, Any], ranking: list[dict[str, Any]]) -> TrialIntent | None:
        for row in ranking:
            name = str(row.get("param") or "")
            result = self.builder.probe_param_candidate(base_params=base_params, param_name=name)
            if result is None:
                continue
            proposed, payload = result
            payload.update({"row": row})
            return TrialIntent(
                kind="fallback_probe",
                proposed_params=proposed,
                changed_params=list(payload.get("changed_params", [])),
                reason=f"fallback visible probe for {name}",
                stage_name=str(row.get("group") or "fallback_probe"),
                payload=payload,
            )
        return None

    @staticmethod
    def _diverse_pair(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        groups: set[str] = set()
        for row in rows:
            group = str(row.get("group") or "")
            if len(selected) == 1 and group in groups and len({str(item.get("group") or "") for item in rows}) > 1:
                continue
            selected.append(row)
            groups.add(group)
            if len(selected) == 2:
                break
        return selected

    def _remember_fit(self, iteration: int, fit_score: float) -> None:
        score = float(fit_score)
        if not math.isfinite(score):
            return
        best = score if not self._recent_best else max(score, self._recent_best[-1][1])
        self._recent_best.append((int(iteration), best))
        if len(self._recent_best) > 30:
            del self._recent_best[0]

    def _remember_trial(self, trial: TrialIntent | None) -> None:
        if trial is None:
            return
        for name in trial.changed_params:
            self._recent_params.append(str(name))
        if len(self._recent_params) > self.recent_window:
            del self._recent_params[: len(self._recent_params) - self.recent_window]

    def _recent_param_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in self._recent_params[-self.recent_window:]:
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))

    def _plateau_like(self, *, long: bool = False) -> bool:
        window = 24 if long else 12
        if len(self._recent_best) < window:
            return False
        oldest = self._recent_best[-window][1]
        latest = self._recent_best[-1][1]
        return latest - oldest < (0.004 if long else 0.002)

    def _subspace_directions(self, dim: int, iteration: int) -> list[float]:
        self._subspace_cursor += 1
        seed = int(iteration) + self._subspace_cursor * 7
        out: list[float] = []
        for axis in range(dim):
            code = (seed * (axis + 3) + axis * axis) % 5
            out.append(0.0 if code == 0 else (1.0 if code in {1, 3} else -1.0))
        if sum(1 for value in out if value != 0.0) < 2 and len(out) >= 2:
            out[0] = 1.0
            out[1] = -1.0
        return out

    def _planner_phase(self) -> str:
        if self._plateau_like():
            return "subspace"
        if self.calibration_started < min(self.calibration_budget, self.param_candidate_pool_size * self.calibration_min_attempts):
            return "calibration"
        return "response_guided"


def _direction_from_row(row: dict[str, Any]) -> float:
    direction = str(row.get("response_recommended_direction") or "")
    confidence = float(row.get("response_confidence", 0.0) or 0.0)
    if confidence >= 0.35:
        if direction == "positive":
            return 1.0
        if direction == "negative":
            return -1.0
    attempts = int(row.get("response_attempts", 0) or 0)
    return -1.0 if attempts % 2 else 1.0


__all__ = ["ExperimentPlanner"]

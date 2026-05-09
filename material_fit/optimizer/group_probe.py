from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .semantic_graph import ShaderEffectGraph


@dataclass(frozen=True)
class GroupProbeCandidate:
    group: str
    params: dict[str, Any]
    changed_params: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GroupProbeResult:
    group: str
    active: bool
    mean_diff: float
    threshold: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_group_probe_candidates(
    base_params: dict[str, Any],
    graph: ShaderEffectGraph,
    *,
    step_ratio: float = 0.12,
) -> list[GroupProbeCandidate]:
    """Create one small perturbation per active effect group.

    The caller can render each candidate and feed the measured image diff
    into :func:`evaluate_group_probe`. Groups whose perturbation produces
    no measurable change should be removed from the later optimizer budget.
    """

    out: list[GroupProbeCandidate] = []
    for group in graph.groups.values():
        candidate = dict(base_params)
        changed: list[str] = []
        probe_order = list(group.gate_params) + list(group.params) if not group.active else list(group.params)
        for name in probe_order:
            sem = graph.params.get(name)
            if sem is None or not sem.searchable:
                continue
            if _bump_param(candidate, name, sem.range_min, sem.range_max, step_ratio):
                changed.append(name)
            # Keep probes cheap and interpretable: one high-leverage knob per group.
            if changed:
                break
        if changed:
            out.append(
                GroupProbeCandidate(
                    group=group.name,
                    params=candidate,
                    changed_params=changed,
                    reason=f"probe {group.name} via {changed[0]}",
                )
            )
    return out


def evaluate_group_probe(
    *,
    group: str,
    mean_diff: float,
    threshold: float = 0.5,
) -> GroupProbeResult:
    active = float(mean_diff) > float(threshold)
    return GroupProbeResult(
        group=group,
        active=active,
        mean_diff=float(mean_diff),
        threshold=float(threshold),
        reason=(
            f"group {group} produced measurable change"
            if active
            else f"group {group} did not clear probe threshold"
        ),
    )


def _bump_param(
    params: dict[str, Any],
    name: str,
    range_min: float | None,
    range_max: float | None,
    step_ratio: float,
) -> bool:
    value = params.get(name)
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        low, high = _bounds(value, range_min, range_max)
        step = max((high - low) * step_ratio, 1e-4)
        params[name] = _clamp(float(value) + step, low, high)
        return params[name] != value
    if isinstance(value, list) and value and all(isinstance(item, (int, float)) for item in value):
        out = list(value)
        limit = min(3, len(out))
        changed = False
        for idx in range(limit):
            low, high = (0.0, 1.0)
            bumped = _clamp(float(out[idx]) + step_ratio, low, high)
            if bumped != out[idx]:
                out[idx] = bumped
                changed = True
        if changed:
            params[name] = out
        return changed
    return False


def _bounds(value: float, range_min: float | None, range_max: float | None) -> tuple[float, float]:
    low = float(range_min) if range_min is not None else min(float(value) - 1.0, 0.0)
    high = float(range_max) if range_max is not None else max(float(value) + 1.0, 1.0)
    if low >= high:
        return low - 1.0, high + 1.0
    return low, high


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

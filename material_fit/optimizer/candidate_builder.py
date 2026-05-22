"""Candidate construction utilities for response-driven scheduling."""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..shared.models import ShaderParam
from .effective_bounds import effective_bounds_for_param
from .semantic_graph import ShaderEffectGraph


class CandidateBuilder:
    """Build concrete material candidates without owning scheduling policy."""

    def __init__(
        self,
        *,
        graph: ShaderEffectGraph,
        shader_params: Sequence[ShaderParam],
        encoder_cls: Any,
        step_schedule: Sequence[float],
    ) -> None:
        self._graph = graph
        self._shader_params = list(shader_params)
        self._encoder_cls = encoder_cls
        self._step_schedule = list(step_schedule)

    def candidate_group_params(self, group: Any) -> list[str]:
        if group.search_params:
            return list(group.search_params)
        if not group.current_active and group.gate_params:
            return list(group.gate_params)
        return list(group.params)

    def nudge_param_candidate(
        self,
        *,
        base_params: dict[str, Any],
        param_name: str,
        step_scale: float,
        group_cycle: int,
        axis_offset: int = 0,
        direction_override: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if param_name not in base_params:
            return None
        sem = self._graph.params.get(param_name)
        if sem is None or not sem.searchable:
            return None
        encoder = self._encoder_cls(
            base_params,
            self._shader_params,
            param_whitelist=[param_name],
            semantics=self._graph,
        )
        if encoder.dim == 0:
            return None
        vec = encoder.encode(base_params)
        axis_index = max(0, min(int(axis_offset), encoder.dim - 1))
        axis = encoder.axes[axis_index]
        direction = direction_override if direction_override is not None else 1.0
        step_ratio = self._step_schedule[min(group_cycle, len(self._step_schedule) - 1)] * step_scale
        width = max(float(axis.high) - float(axis.low), 1e-9)
        vec[axis_index] = max(
            encoder.lower_bounds[axis_index],
            min(encoder.upper_bounds[axis_index], vec[axis_index] + direction * step_ratio * width),
        )
        proposed = encoder.decode(vec)
        changes = diff_params(base_params, proposed)
        if not changes:
            return None
        return proposed, {
            "param": param_name,
            "axis_index": axis_index,
            "axis_param": axis.param_name,
            "sub_index": axis.sub_index,
            "transform": axis.transform,
            "direction": direction,
            "step_ratio": step_ratio,
            "changed_params": [str(change.get("param")) for change in changes if isinstance(change, dict)],
        }

    def probe_param_candidate(
        self,
        *,
        base_params: dict[str, Any],
        param_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if param_name not in base_params:
            return None
        sem = self._graph.params.get(param_name)
        if sem is None or not sem.searchable:
            return None
        candidate = dict(base_params)
        before = candidate.get(param_name)
        candidate[param_name] = self._probe_value(param_name, before, sem)
        changes = diff_params(base_params, candidate)
        if not changes:
            return None
        return candidate, {
            "param": param_name,
            "changed_params": [str(change.get("param")) for change in changes if isinstance(change, dict)],
            "probe": True,
        }

    def build_subspace_candidate(
        self,
        *,
        base_params: dict[str, Any],
        param_rows: Sequence[dict[str, Any]],
        directions: Sequence[float],
        group_cycle: int,
        step_scale: float,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        candidate = dict(base_params)
        axes: list[dict[str, Any]] = []
        changed: list[str] = []
        for row, direction in zip(param_rows, directions):
            name = str(row.get("param") or "")
            if not name or name not in candidate or abs(float(direction)) < 1.0e-12:
                continue
            result = self.nudge_param_candidate(
                base_params=candidate,
                param_name=name,
                step_scale=step_scale,
                group_cycle=group_cycle,
                direction_override=float(direction),
            )
            if result is None:
                continue
            candidate, payload = result
            axes.append(payload)
            changed.extend(str(item) for item in payload.get("changed_params", []) if item)
        unique_changed = sorted(set(changed))
        if len(unique_changed) < 2 or candidate == base_params:
            return None
        return candidate, {"changed_params": unique_changed, "axes": axes}

    def _probe_value(self, name: str, value: Any, sem: Any) -> Any:
        if isinstance(value, bool):
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            low, high = self._bounds_for_value(name, float(value), sem)
            if float(value) <= low + 1e-8:
                return max(min(low + (high - low) * 0.35, high), low)
            step = max((high - low) * 0.18, 1e-4)
            return max(low, min(high, float(value) + step))
        if isinstance(value, list) and value and all(isinstance(item, (int, float)) for item in value):
            out = list(value)
            for idx in range(min(3, len(out))):
                out[idx] = max(0.0, min(1.0, float(out[idx]) + 0.18))
            return out
        return value

    @staticmethod
    def _bounds_for_value(name: str, value: float, sem: Any) -> tuple[float, float]:
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


def diff_params(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if _same_value(old, new):
            continue
        out.append({"param": key, "before": old, "after": new})
    return out


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1.0e-9, abs_tol=1.0e-9)
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


__all__ = ["CandidateBuilder", "diff_params"]

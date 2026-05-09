from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


def load_warm_start_history(
    auto_dir: Path,
    *,
    limit: int,
) -> list[tuple[dict[str, Any], float]]:
    """Scan ``auto_adjust/iter_*/`` for completed ``(params, fit_score)`` pairs."""

    if limit <= 0 or not auto_dir.is_dir():
        return []
    out: list[tuple[int, dict[str, Any], float]] = []
    for entry in auto_dir.iterdir():
        if not entry.is_dir() or not entry.name.startswith("iter_"):
            continue
        try:
            idx = int(entry.name[len("iter_"):])
        except ValueError:
            continue
        params_path = entry / "candidate" / "params.json"
        decision_path = entry / "decision.json"
        if not params_path.exists() or not decision_path.exists():
            continue
        try:
            params = json.loads(params_path.read_text(encoding="utf-8-sig"))
            decision = json.loads(decision_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(params, dict) or not isinstance(decision, dict):
            continue
        fit_score = decision.get("fit_score_before")
        if not isinstance(fit_score, (int, float)) or not math.isfinite(float(fit_score)):
            continue
        out.append((idx, params, float(fit_score)))
    out.sort(key=lambda item: item[0])
    return [(params, fit_score) for _, params, fit_score in out[-limit:]]

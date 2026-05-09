from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..case_loader import LoaderConfig


def config() -> LoaderConfig:
    return LoaderConfig()


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"expected numeric threshold, got {value!r}") from exc
    if parsed < 0.0:
        raise HTTPException(status_code=400, detail="thresholds must be >= 0")
    return parsed

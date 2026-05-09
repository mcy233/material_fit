from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import preflight
from .common import config, optional_float

router = APIRouter()


@router.post("/api/projects/{project_id}/preflight/laya_refresh")
def api_run_laya_refresh_preflight(
    project_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    probe_param = str(payload.get("probe_param") or "u_BaseColor")
    try:
        return preflight.run_laya_refresh_preflight(
            project_id,
            config=config(),
            probe_param=probe_param,
            mean_diff_change_threshold=optional_float(payload.get("mean_diff_change_threshold")),
            mean_diff_restore_threshold=optional_float(payload.get("mean_diff_restore_threshold")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"preflight failed: {exc}") from exc


@router.get("/api/projects/{project_id}/preflight/laya_refresh")
def api_get_last_laya_refresh_preflight(project_id: str) -> dict[str, Any]:
    result = preflight.get_last_preflight(project_id, config=config())
    if result is None:
        raise HTTPException(status_code=404, detail="no preflight has been run for this project yet")
    return result

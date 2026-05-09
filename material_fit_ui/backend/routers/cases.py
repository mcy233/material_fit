from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import case_loader
from .common import config

router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, Any]:
    cfg = config()
    return {
        "status": "ok",
        "project_root": str(cfg.project_root),
        "output_dir": str(cfg.output_dir),
        "output_dir_exists": cfg.output_dir.exists(),
    }


@router.get("/api/cases")
def api_list_cases() -> list[dict[str, Any]]:
    return case_loader.list_cases(config())


@router.get("/api/cases/{case_id}/overview")
def api_case_overview(case_id: str) -> dict[str, Any]:
    try:
        return case_loader.get_case_overview(case_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/cases/{case_id}/iterations")
def api_case_iterations(case_id: str) -> list[dict[str, Any]]:
    try:
        return case_loader.list_iterations(case_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/cases/{case_id}/report")
def api_case_report(case_id: str) -> dict[str, Any]:
    try:
        return case_loader.get_case_report(case_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/cases/{case_id}/iterations/{iter_id}")
def api_iteration_detail(case_id: str, iter_id: str) -> dict[str, Any]:
    try:
        return case_loader.get_iteration_detail(case_id, iter_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/image")
def api_image(path: str = Query(..., min_length=1, max_length=1024)) -> FileResponse:
    try:
        resolved = case_loader.resolve_image_path(path, config())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(resolved)

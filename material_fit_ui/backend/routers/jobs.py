from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import job_manager
from .common import config

router = APIRouter()


@router.post("/api/projects/{project_id}/jobs")
def api_start_job(project_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        return job_manager.start_job(project_id, config=config(), overrides=payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/jobs")
def api_list_jobs(project_id: str) -> list[dict[str, Any]]:
    try:
        return job_manager.list_jobs(project_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/jobs/{job_id}")
def api_get_job(job_id: str) -> dict[str, Any]:
    try:
        return job_manager.get_job(job_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return job_manager.cancel_job(job_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/jobs/{job_id}/log")
def api_job_log(job_id: str, tail_kb: int = 64) -> dict[str, Any]:
    try:
        text = job_manager.get_job_log(job_id, config(), tail_kb=max(1, min(tail_kb, 1024)))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"job_id": job_id, "tail_kb": tail_kb, "text": text}

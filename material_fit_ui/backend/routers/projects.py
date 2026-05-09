from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import project_store
from .common import config

router = APIRouter()


@router.get("/api/projects")
def api_list_projects() -> list[dict[str, Any]]:
    return project_store.list_projects(config())


@router.post("/api/projects")
def api_create_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return project_store.create_project(
            project_id=str(payload.get("id", "")),
            name=str(payload.get("name", "") or payload.get("id", "")),
            description=str(payload.get("description", "") or ""),
            config=config(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}")
def api_get_project(project_id: str) -> dict[str, Any]:
    try:
        return project_store.get_project(project_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/api/projects/{project_id}")
def api_patch_project(project_id: str, patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return project_store.patch_project(project_id, patch, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str) -> dict[str, Any]:
    try:
        return project_store.delete_project(project_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

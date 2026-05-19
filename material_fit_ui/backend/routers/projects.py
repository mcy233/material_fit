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


@router.get("/api/projects/{project_id}/laya_scene_nodes")
def api_list_laya_scene_nodes(project_id: str) -> dict[str, Any]:
    try:
        return project_store.list_laya_scene_nodes(project_id, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/laya_scene_nodes/inspect")
def api_inspect_laya_scene_nodes(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else payload
    return project_store.inspect_laya_scene_nodes(inputs)


@router.patch("/api/projects/{project_id}")
def api_patch_project(project_id: str, patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return project_store.patch_project(project_id, patch, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/inputs/import_file")
def api_import_project_input_file(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return project_store.import_project_input_file(
            project_id,
            input_key=str(payload.get("input_key") or ""),
            source_path=str(payload.get("source_path") or ""),
            config=config(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/inputs/import_unity_references")
def api_import_unity_reference_files(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    raw_paths = payload.get("source_paths")
    source_paths = [str(item) for item in raw_paths] if isinstance(raw_paths, list) else []
    try:
        return project_store.import_unity_reference_files(
            project_id,
            source_paths=source_paths,
            config=config(),
        )
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

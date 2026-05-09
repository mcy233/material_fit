from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import preanalysis, project_store
from .common import config

router = APIRouter()


@router.post("/api/projects/{project_id}/preanalyze")
def api_preanalyze(
    project_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    try:
        use_llm = payload.get("use_llm")
        return preanalysis.run_preanalysis(
            project_id,
            config(),
            use_llm=bool(use_llm) if use_llm is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"preanalysis failed: {exc}") from exc


@router.get("/api/projects/{project_id}/preanalysis")
def api_get_preanalysis(project_id: str) -> dict[str, Any]:
    result = preanalysis.get_preanalysis(project_id, config())
    if result is None:
        raise HTTPException(status_code=404, detail="preanalysis not yet run for this project")
    return result


@router.put("/api/projects/{project_id}/laya_control_schema")
def api_set_laya_control_schema(
    project_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    manual_schema = payload.get("manual_laya_control_schema")
    if not isinstance(manual_schema, dict):
        raise HTTPException(status_code=400, detail="manual_laya_control_schema must be an object")
    try:
        return preanalysis.save_manual_laya_control_schema(project_id, manual_schema, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/laya_control_schema_presets")
def api_list_laya_control_schema_presets(project_id: str) -> dict[str, Any]:
    try:
        return preanalysis.list_laya_control_schema_presets(project_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/laya_control_schema_presets/apply")
def api_apply_laya_control_schema_preset(
    project_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    preset_id = str(payload.get("preset_id") or "auto")
    try:
        return preanalysis.apply_laya_control_schema_preset(project_id, preset_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/laya_control_schema_presets")
def api_save_laya_control_schema_preset(
    project_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        return preanalysis.save_laya_control_schema_preset(
            project_id,
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            config=config(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/projects/{project_id}/laya_control_schema_presets/{preset_id}")
def api_rename_laya_control_schema_preset(
    project_id: str,
    preset_id: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        return preanalysis.rename_laya_control_schema_preset(
            project_id,
            preset_id,
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            config=config(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/projects/{project_id}/laya_control_schema_presets/{preset_id}")
def api_delete_laya_control_schema_preset(project_id: str, preset_id: str) -> dict[str, Any]:
    try:
        return preanalysis.delete_laya_control_schema_preset(project_id, preset_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/projects/{project_id}/manual_mapping")
def api_set_manual_mapping(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    mapping = payload.get("manual_param_mapping")
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400, detail="manual_param_mapping must be an object")
    cleaned: dict[str, str] = {}
    for k, v in mapping.items():
        if not isinstance(k, str) or v is None:
            continue
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail=f"value for {k!r} must be string or null")
        cleaned[k] = v
    try:
        project_store.patch_project(project_id, {"manual_param_mapping": cleaned}, config())
        return preanalysis.run_preanalysis(project_id, config())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"manual mapping save failed: {exc}") from exc

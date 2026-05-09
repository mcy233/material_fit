from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from .. import case_loader, file_dialog, region_picker
from .common import config

router = APIRouter()


@router.get("/api/files/preview")
def api_files_preview(path: str = Query(..., min_length=1, max_length=2048)) -> FileResponse:
    try:
        resolved = case_loader.resolve_external_preview_path(path, config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(resolved)


@router.post("/api/files/pick")
def api_pick_file(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    payload = payload or {}
    return file_dialog.pick(
        mode=str(payload.get("mode", "open")),
        title=payload.get("title"),
        initial_dir=payload.get("initial_dir"),
        initial_file=payload.get("initial_file"),
        filetypes=payload.get("filetypes"),
    )


@router.post("/api/files/pick_region")
def api_pick_region(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    laya_window = payload.get("laya_window") if isinstance(payload, dict) else None
    return region_picker.pick_region(
        laya_window=laya_window if isinstance(laya_window, dict) else None,
    )


@router.get("/api/files/info")
def api_file_info(path: str = Query(..., min_length=1, max_length=2048)) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False}
    try:
        stat = p.stat()
    except OSError as exc:
        return {"path": str(p), "exists": True, "error": str(exc)}
    return {
        "path": str(p),
        "exists": True,
        "is_file": p.is_file(),
        "is_dir": p.is_dir(),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "name": p.name,
        "suffix": p.suffix,
    }

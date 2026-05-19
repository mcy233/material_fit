from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from .. import case_loader, file_dialog
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


@router.get("/api/files/list")
def api_file_list(
    path: str = Query(..., min_length=1, max_length=2048),
    pattern: str = Query("*", min_length=1, max_length=256),
    limit: int = Query(64, ge=1, le=512),
) -> dict[str, Any]:
    directory = Path(path)
    if not directory.exists():
        return {"path": str(directory), "exists": False, "files": []}
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail=f"path is not a directory: {directory}")
    try:
        files = [
            item
            for item in directory.glob(pattern)
            if item.is_file()
        ][:limit]
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "path": str(directory),
        "exists": True,
        "files": [
            {
                "path": str(item),
                "exists": True,
                "is_file": True,
                "is_dir": False,
                "name": item.name,
                "suffix": item.suffix,
                "size": item.stat().st_size,
                "mtime": item.stat().st_mtime,
            }
            for item in sorted(files, key=lambda p: p.name)
        ],
    }


@router.get("/api/files/unity_references")
def api_unity_references(
    path: str = Query("", max_length=2048),
    pattern: str = Query("unity_ref_v*_yaw*_pitch*.png", min_length=1, max_length=256),
    limit: int = Query(32, ge=1, le=512),
) -> dict[str, Any]:
    cfg = config()
    directory = Path(path) if path else _discover_latest_unity_reference_dir(cfg.project_root, pattern)
    if directory is None or not directory.exists():
        return {"path": str(directory or ""), "exists": False, "files": []}
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail=f"path is not a directory: {directory}")
    files = _reference_files(directory, pattern)[:limit]
    return {
        "path": str(directory),
        "exists": True,
        "files": [_file_payload(item) for item in files],
    }


def _discover_latest_unity_reference_dir(project_root: Path, pattern: str) -> Path | None:
    unity_root = project_root / "tools" / "material_fit" / "unity"
    if not unity_root.exists():
        return None
    candidates: list[tuple[float, int, Path]] = []
    for directory in unity_root.rglob("*"):
        if not directory.is_dir():
            continue
        files = _reference_files(directory, pattern)
        if not files:
            continue
        newest = max(item.stat().st_mtime for item in files)
        current_bonus = 1 if "current" in directory.name.lower() else 0
        candidates.append((newest, current_bonus, directory))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], str(item[2])), reverse=True)
    return candidates[0][2]


def _reference_files(directory: Path, pattern: str) -> list[Path]:
    return sorted(
        (
            item
            for item in directory.glob(pattern)
            if item.is_file() and "_mask" not in item.stem
        ),
        key=lambda item: item.name,
    )


def _file_payload(item: Path) -> dict[str, Any]:
    stat = item.stat()
    return {
        "path": str(item),
        "exists": True,
        "is_file": True,
        "is_dir": False,
        "name": item.name,
        "suffix": item.suffix,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }

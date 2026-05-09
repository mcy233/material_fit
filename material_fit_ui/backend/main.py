"""FastAPI entrypoint for the Material Fit UI backend."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routers import cases, files, jobs, preanalysis, preflight, projects

app = FastAPI(title="Material Fit UI", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

for router in (
    cases.router,
    projects.router,
    files.router,
    preanalysis.router,
    preflight.router,
    jobs.router,
):
    app.include_router(router)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):  # type: ignore[no-untyped-def]
    return JSONResponse(status_code=500, content={"detail": f"internal error: {exc}"})

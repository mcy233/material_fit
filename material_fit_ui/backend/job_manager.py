"""Subprocess-based job runner for ``fit_material`` invocations.

Each job:

- writes ``fit_config.json`` from the project's current state;
- spawns ``python -m tools.material_fit.fit_material --config <path> ...``;
- captures stdout/stderr to ``jobs/<job_id>.log``;
- maintains ``jobs/<job_id>.json`` with status, observed iterations, etc.;
- a watcher thread tails ``auto_adjust/iter_*/decision.json`` so the UI can
  poll the project's iteration list and immediately see the latest decision
  without waiting for the whole run to finish.

This deliberately does NOT use SSE/websockets yet — the frontend polls the
job state and the existing ``/iterations`` endpoint, which is good enough at
human-scale iteration cadence and avoids long-lived connections.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .case_loader import LoaderConfig
from .project_store import (
    derive_fit_config,
    get_project,
    patch_project,
    project_paths,
    write_fit_config,
)


_JOB_REGISTRY: dict[str, "Job"] = {}
_REGISTRY_LOCK = threading.Lock()


@dataclass
class Job:
    job_id: str
    project_id: str
    pid: int | None = None
    status: str = "queued"
    started_at: str | None = None
    ended_at: str | None = None
    return_code: int | None = None
    error: str | None = None
    args: list[str] = field(default_factory=list)
    iterations_observed: int = 0
    last_iter_id: str | None = None
    last_decision_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "pid": self.pid,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "return_code": self.return_code,
            "error": self.error,
            "args": list(self.args),
            "iterations_observed": self.iterations_observed,
            "last_iter_id": self.last_iter_id,
            "last_decision_summary": self.last_decision_summary,
        }


def start_job(
    project_id: str,
    *,
    config: LoaderConfig | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a new auto-adjust job for the given project."""

    config = config or LoaderConfig()
    project = get_project(project_id, config)
    paths = project_paths(project_id, config)

    if project.get("active_job_id"):
        existing = _load_job_dict(project["active_job_id"], paths.jobs_dir)
        if existing and existing.get("status") == "running":
            raise RuntimeError(f"project {project_id} already has running job {existing['job_id']}")

    _clear_previous_iteration_outputs(paths.project_dir)
    fit_config_path = write_fit_config(project_id, config)
    fit_config = derive_fit_config(project_id, config)

    algo = project.get("algorithm_config", {})
    # E-010: per-optimizer iteration budget. Heuristic was designed
    # around 6 iterations (one per stage); CMA-ES needs many more
    # generations to converge on a 49-dim space (literature: 100+
    # evals). Default to 30 for CMA-ES, 6 for heuristic, but always
    # honour an explicit ``algo['max_iterations']`` override.
    optimizer_value_for_default = str(algo.get("optimizer", "heuristic")).strip().lower()
    if optimizer_value_for_default in ("cma_cold", "cma_warm"):
        default_iterations = 30
    elif optimizer_value_for_default == "semantic_group":
        default_iterations = 12
    else:
        default_iterations = 6
    args = [
        sys.executable,
        "-m",
        "tools.material_fit.fit_material",
        "--config",
        str(fit_config_path),
        "--auto-adjust",
        "--iterations",
        str(int(algo.get("max_iterations", default_iterations))),
        "--target-score",
        str(float(algo.get("target_score", 0.5))),
    ]
    if algo.get("apply_lmat", True):
        args.append("--apply-lmat")
        args.append("--write-candidate-lmat")
    if algo.get("capture_screen_after_apply", True):
        args.append("--capture-screen-after-apply")
    if algo.get("dry_run", False):
        args.append("--dry-run")
    if algo.get("use_capture_contract", False):
        args.append("--capture")
    fit_score_mode = str(algo.get("fit_score_mode", "linear")).lower()
    if fit_score_mode in ("linear", "perceptual"):
        args.extend(["--fit-score-mode", fit_score_mode])
    region = fit_config.get("screen_capture", {}).get("region")
    if region:
        args.extend(["--screen-capture-region", region])
    # E-012: rolling capture pool size. Only emit the flag when the
    # project explicitly set max_keep to a non-default value, so the
    # CLI's own default (30) takes over otherwise.
    max_keep = fit_config.get("screen_capture", {}).get("max_keep")
    if max_keep not in (None, ""):
        try:
            args.extend(["--screen-capture-max-keep", str(int(max_keep))])
        except (TypeError, ValueError):
            pass

    # E-006 (ExperimentLog.md): pluggable optimizer. The default
    # 'heuristic' value matches fit_material.py's own default, so old
    # projects without these fields still work unchanged.
    optimizer_value = str(algo.get("optimizer", "heuristic")).strip().lower()
    if optimizer_value not in ("heuristic", "cma_cold", "cma_warm", "semantic_group"):
        optimizer_value = "heuristic"
    args.extend(["--optimizer", optimizer_value])

    cma_es = algo.get("cma_es") if isinstance(algo.get("cma_es"), dict) else {}
    if cma_es.get("warm_start_iters") is not None:
        args.extend(["--cma-warm-start-iters", str(int(cma_es["warm_start_iters"]))])
    if cma_es.get("population_size") not in (None, ""):
        args.extend(["--cma-population-size", str(int(cma_es["population_size"]))])
    if cma_es.get("sigma") not in (None, ""):
        args.extend(["--cma-sigma", str(float(cma_es["sigma"]))])
    if cma_es.get("seed") not in (None, ""):
        args.extend(["--cma-seed", str(int(cma_es["seed"]))])
    # E-010: hint-bias mix ratio. Only emit the CLI flag when the
    # project actually persisted a value, so subprocess defaults
    # remain consistent with `CmaesStrategyConfig.hint_bias_mix_ratio`.
    if cma_es.get("hint_bias_mix_ratio") not in (None, ""):
        args.extend(["--cma-hint-bias-mix-ratio", str(float(cma_es["hint_bias_mix_ratio"]))])

    # E-007: also turn on the magenta-probe preflight by default, and
    # carry through the project's laya_window block so the focus
    # callback is identical between the UI's standalone preflight and
    # the auto-adjust subprocess. Set algorithm_config.laya_refresh_check
    # to false to suppress the preflight (the focus block is always passed
    # via fit_config.json regardless).
    if algo.get("laya_refresh_check", True) and algo.get("apply_lmat", True):
        args.append("--laya-refresh-check")
    laya_window = fit_config.get("laya_window") or {}
    if isinstance(laya_window, dict):
        process_pat = str(laya_window.get("process_pattern", ""))
        title_pat = str(laya_window.get("title_pattern", ""))
        args.extend(["--laya-window-process", process_pat])
        args.extend(["--laya-window-title", title_pat])

    if isinstance(overrides, dict):
        # allow callers to append extra CLI args; safe-typed
        for arg in overrides.get("extra_args", []):
            if isinstance(arg, str):
                args.append(arg)

    paths.jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = _new_job_id()
    log_path = paths.jobs_dir / f"{job_id}.log"
    state_path = paths.jobs_dir / f"{job_id}.json"

    job = Job(
        job_id=job_id,
        project_id=project_id,
        status="running",
        started_at=_now_iso(),
        args=args,
    )

    log_handle = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=str(config.project_root),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    job.pid = proc.pid
    _save_job(job, state_path)
    with _REGISTRY_LOCK:
        _JOB_REGISTRY[job_id] = job
    patch_project(
        project_id,
        {"active_job_id": job_id, "last_job_id": job_id},
        config=config,
    )

    watcher = threading.Thread(
        target=_watch_job,
        args=(job, proc, log_handle, paths.project_dir, state_path, config),
        name=f"fit-job-{job_id}",
        daemon=True,
    )
    watcher.start()
    return job.to_dict()


def _clear_previous_iteration_outputs(project_dir: Path) -> None:
    """Reset per-run iteration artifacts before launching a new job.

    Job history remains under ``jobs/``; only the files that drive the
    timeline/detail UI are removed so every new auto-adjust run starts
    from ``iter_0000`` visually and analytically.
    """

    auto_dir = project_dir / "auto_adjust"
    if auto_dir.exists():
        for entry in auto_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("iter_"):
                shutil.rmtree(entry, ignore_errors=True)
        for name in ("state.json", "auto_adjust_result.json"):
            target = auto_dir / name
            if target.exists():
                target.unlink()
    iter_dir = project_dir / "iterations"
    if iter_dir.exists():
        for entry in iter_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("iter_"):
                shutil.rmtree(entry, ignore_errors=True)


def list_jobs(project_id: str, config: LoaderConfig | None = None) -> list[dict[str, Any]]:
    config = config or LoaderConfig()
    paths = project_paths(project_id, config)
    if not paths.jobs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(paths.jobs_dir.iterdir(), key=lambda path: path.name.lower(), reverse=True):
        if entry.suffix.lower() != ".json":
            continue
        data = _load_job_dict(entry.stem, paths.jobs_dir)
        if data:
            out.append(data)
    return out


def get_job(job_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    with _REGISTRY_LOCK:
        registered = _JOB_REGISTRY.get(job_id)
    if registered:
        return registered.to_dict()
    # Fall back to disk lookup across all projects
    for project_dir in config.output_dir.iterdir() if config.output_dir.exists() else []:
        if not project_dir.is_dir():
            continue
        candidate = project_dir / "jobs" / f"{job_id}.json"
        if candidate.exists():
            data = _load_job_dict(job_id, project_dir / "jobs")
            if data:
                return data
    raise FileNotFoundError(f"job not found: {job_id}")


def cancel_job(job_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    with _REGISTRY_LOCK:
        job = _JOB_REGISTRY.get(job_id)
    if not job or not job.pid:
        raise FileNotFoundError(f"job {job_id} not running in this server instance")
    if job.status not in {"running", "queued"}:
        return job.to_dict()
    try:
        if os.name == "nt":
            os.kill(job.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(job.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError, OSError) as exc:
        job.error = f"cancel failed: {exc}"
    job.status = "cancelling"
    _save_job(job, _state_path_for(job, config))
    return job.to_dict()


def get_job_log(job_id: str, config: LoaderConfig | None = None, *, tail_kb: int = 64) -> str:
    config = config or LoaderConfig()
    for project_dir in config.output_dir.iterdir() if config.output_dir.exists() else []:
        if not project_dir.is_dir():
            continue
        log_path = project_dir / "jobs" / f"{job_id}.log"
        if log_path.exists():
            try:
                size = log_path.stat().st_size
                start = max(0, size - tail_kb * 1024)
                with log_path.open("rb") as fh:
                    fh.seek(start)
                    raw = fh.read()
                return raw.decode("utf-8", errors="replace")
            except OSError:
                return ""
    raise FileNotFoundError(f"job log not found: {job_id}")


def _watch_job(
    job: Job,
    proc: subprocess.Popen,
    log_handle: Any,
    project_dir: Path,
    state_path: Path,
    config: LoaderConfig,
) -> None:
    auto_dir = project_dir / "auto_adjust"
    seen: set[str] = set()
    poll_interval = 1.0
    try:
        while True:
            new_iter = _scan_iterations(auto_dir, seen)
            if new_iter is not None:
                job.iterations_observed = len(seen)
                job.last_iter_id, job.last_decision_summary = new_iter
                _save_job(job, state_path)
            ret = proc.poll()
            if ret is not None:
                # final scan to catch any iterations written just before exit
                last = _scan_iterations(auto_dir, seen)
                if last is not None:
                    job.iterations_observed = len(seen)
                    job.last_iter_id, job.last_decision_summary = last
                job.return_code = ret
                if job.status == "cancelling":
                    job.status = "cancelled"
                elif ret == 0:
                    job.status = "completed"
                else:
                    job.status = "failed"
                    job.error = job.error or f"process exited with code {ret}"
                job.ended_at = _now_iso()
                break
            time.sleep(poll_interval)
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"watcher crashed: {exc}"
        job.ended_at = _now_iso()
    finally:
        try:
            log_handle.close()
        except Exception:
            pass
        _save_job(job, state_path)
        try:
            patch_project(
                job.project_id,
                {"active_job_id": None, "last_job_id": job.job_id},
                config=config,
            )
        except Exception:  # noqa: BLE001
            pass


def _scan_iterations(auto_dir: Path, seen: set[str]) -> tuple[str, dict[str, Any]] | None:
    if not auto_dir.is_dir():
        return None
    new_one: tuple[str, dict[str, Any]] | None = None
    for entry in sorted(auto_dir.iterdir(), key=lambda path: path.name.lower()):
        if not entry.is_dir() or not entry.name.startswith("iter_"):
            continue
        decision = entry / "decision.json"
        if not decision.exists() or entry.name in seen:
            continue
        seen.add(entry.name)
        summary = _summarize_decision(decision)
        if summary is not None:
            new_one = (entry.name, summary)
    return new_one


def _summarize_decision(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    perceptual = data.get("perceptual_signals") if isinstance(data.get("perceptual_signals"), dict) else {}
    human = perceptual.get("human_accept") if isinstance(perceptual.get("human_accept"), dict) else {}
    return {
        "iteration": data.get("iteration"),
        "selected_stage": data.get("selected_stage"),
        "fit_score_before": data.get("fit_score_before"),
        "diff_score_before": data.get("diff_score_before"),
        "human_accept_score": human.get("score"),
        "perceptual_fit_score": perceptual.get("fit_score"),
        "weighted_mae": perceptual.get("weighted_mae"),
        "stop_reason": inner.get("stop_reason"),
        "changes_count": len(inner.get("changes") or []) if isinstance(inner.get("changes"), list) else 0,
        "optimizer": inner.get("optimizer"),
        "cma_es": inner.get("cma_es"),
    }


def _save_job(job: Job, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _state_path_for(job: Job, config: LoaderConfig) -> Path:
    paths = project_paths(job.project_id, config)
    return paths.jobs_dir / f"{job.job_id}.json"


def _load_job_dict(job_id: str, jobs_dir: Path) -> dict[str, Any] | None:
    path = jobs_dir / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _new_job_id() -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"job_{stamp}_{secrets.token_hex(3)}"


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now().isoformat(timespec="seconds")

"""Subprocess-based job runner for ``fit_material`` invocations.

Each job:

- creates ``jobs/<job_id>/runs/<date-settings>/`` and writes that run's ``fit_config.json``;
- spawns ``python -m tools.material_fit.fit_material --config <path> ...``;
- captures stdout/stderr to ``jobs/<job_id>/job.log``;
- maintains ``jobs/<job_id>/job.json`` with status, observed iterations, etc.;
- a watcher thread tails ``jobs/<job_id>/runs/<run_id>/auto_adjust/iter_*/decision.json`` so the UI can
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

from . import case_loader
from .case_loader import LoaderConfig
from .project_store import (
    derive_fit_config,
    get_project,
    patch_project,
    project_paths,
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
    run_id: str | None = None
    run_dir: str | None = None
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
            "run_id": self.run_id,
            "run_dir": self.run_dir,
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

    fit_config = derive_fit_config(project_id, config)
    algo = project.get("algorithm_config", {})
    editor_capture_enabled = bool(
        isinstance(fit_config.get("laya_editor_capture"), dict)
        and fit_config["laya_editor_capture"].get("enabled")
    )
    # E-010: per-optimizer iteration budget. Heuristic was designed
    # around 6 iterations (one per stage); CMA-ES needs many more
    # generations to converge on a 49-dim space (literature: 100+
    # evals). Default to 30 for CMA-ES, 6 for heuristic, but always
    # honour an explicit ``algo['max_iterations']`` override.
    optimizer_value_for_default = str(algo.get("optimizer", "heuristic")).strip().lower()
    if optimizer_value_for_default in ("cma_cold", "cma_warm", "subspace_cma_es"):
        default_iterations = 30
    elif optimizer_value_for_default in ("semantic_group", "semantic_group_legacy_081"):
        default_iterations = 12
    else:
        default_iterations = 6
    job_id = _new_job_id()
    run_id = _new_run_id(algo)
    job_dir = paths.jobs_dir / job_id
    run_dir = job_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    fit_config = _fit_config_for_run(fit_config, run_dir)
    fit_config["project_preflight_dir"] = str((paths.project_dir / "preflight").resolve())
    refresh_cert = _valid_refresh_session_cert(paths, fit_config)
    if refresh_cert:
        fit_config["laya_refresh_session_cert"] = refresh_cert
    fit_config_path = run_dir / "fit_config.json"
    fit_config_path.write_text(
        json.dumps(fit_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    job_dir.joinpath("job_config.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "project_id": project_id,
                "run_id": run_id,
                "run_dir": str(run_dir),
                "fit_config_path": str(fit_config_path),
                "algorithm_config": algo,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

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
    if (not editor_capture_enabled) and algo.get("capture_screen_after_apply", False):
        args.append("--capture-screen-after-apply")
    if algo.get("dry_run", False):
        args.append("--dry-run")
    if (not editor_capture_enabled) and algo.get("use_capture_contract", False):
        args.append("--capture")
    fit_score_mode = str(algo.get("fit_score_mode", "research")).lower()
    if fit_score_mode in ("linear", "perceptual", "human_accept", "research"):
        args.extend(["--fit-score-mode", fit_score_mode])
    region = fit_config.get("screen_capture", {}).get("region")
    if (not editor_capture_enabled) and region:
        args.extend(["--screen-capture-region", region])
    # E-012: rolling capture pool size. Only emit the flag when the
    # project explicitly set max_keep to a non-default value, so the
    # CLI's own default (30) takes over otherwise.
    max_keep = fit_config.get("screen_capture", {}).get("max_keep")
    if (not editor_capture_enabled) and max_keep not in (None, ""):
        try:
            args.extend(["--screen-capture-max-keep", str(int(max_keep))])
        except (TypeError, ValueError):
            pass

    # E-006 (ExperimentLog.md): pluggable optimizer. The default
    # 'heuristic' value matches fit_material.py's own default, so old
    # projects without these fields still work unchanged.
    optimizer_value = str(algo.get("optimizer", "heuristic")).strip().lower()
    if optimizer_value not in (
        "heuristic",
        "cma_cold",
        "cma_warm",
        "semantic_group",
        "semantic_group_legacy_081",
        "subspace_cma_es",
    ):
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

    # The Laya refresh probe belongs to the project preflight flow, not the
    # formal auto-adjust loop. Running it here writes a temporary probe value
    # into the .lmat before the first iteration and can contaminate the user's
    # intended initial state. Keep the CLI flag available for manual debugging,
    # but do not append it from UI jobs.
    laya_window = fit_config.get("laya_window") or {}
    if (not editor_capture_enabled) and isinstance(laya_window, dict):
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
    log_path = job_dir / "job.log"
    state_path = job_dir / "job.json"

    job = Job(
        job_id=job_id,
        project_id=project_id,
        status="running",
        started_at=_now_iso(),
        args=args,
        run_id=run_id,
        run_dir=str(run_dir),
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
        {
            "active_job_id": job_id,
            "last_job_id": job_id,
            "active_run_id": run_id,
            "last_run_id": run_id,
        },
        config=config,
    )

    watcher = threading.Thread(
        target=_watch_job,
        args=(job, proc, log_handle, run_dir, state_path, config),
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
        if entry.is_dir():
            data = _load_job_dict(entry.name, paths.jobs_dir)
        elif entry.suffix.lower() == ".json":
            data = _load_job_dict(entry.stem, paths.jobs_dir)
        else:
            continue
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
    data = _find_job_dict(job_id, config)
    if data:
        return data
    raise FileNotFoundError(f"job not found: {job_id}")


def _find_job_dict(job_id: str, config: LoaderConfig) -> dict[str, Any] | None:
    for project_dir in config.output_dir.iterdir() if config.output_dir.exists() else []:
        if not project_dir.is_dir():
            continue
        candidate = project_dir / "jobs" / job_id / "job.json"
        legacy_candidate = project_dir / "jobs" / f"{job_id}.json"
        if candidate.exists() or legacy_candidate.exists():
            data = _load_job_dict(job_id, project_dir / "jobs")
            if data:
                return data
    return None


def _job_from_dict(data: dict[str, Any]) -> Job:
    return Job(
        job_id=str(data.get("job_id") or ""),
        project_id=str(data.get("project_id") or ""),
        pid=int(data["pid"]) if data.get("pid") not in (None, "") else None,
        status=str(data.get("status") or "queued"),
        started_at=str(data.get("started_at")) if data.get("started_at") else None,
        ended_at=str(data.get("ended_at")) if data.get("ended_at") else None,
        return_code=int(data["return_code"]) if data.get("return_code") not in (None, "") else None,
        error=str(data.get("error")) if data.get("error") else None,
        args=[str(item) for item in data.get("args", [])] if isinstance(data.get("args"), list) else [],
        run_id=str(data.get("run_id")) if data.get("run_id") else None,
        run_dir=str(data.get("run_dir")) if data.get("run_dir") else None,
        iterations_observed=int(data.get("iterations_observed") or 0),
        last_iter_id=str(data.get("last_iter_id")) if data.get("last_iter_id") else None,
        last_decision_summary=data.get("last_decision_summary") if isinstance(data.get("last_decision_summary"), dict) else None,
    )


def _terminate_process_tree(pid: int) -> str | None:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode not in (0, 128):
                return (result.stderr or f"taskkill exited with {result.returncode}").strip()
            return None
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return None
        return None
    except (PermissionError, ProcessLookupError, OSError) as exc:
        return str(exc)


def _clear_active_job_if_current(job: Job, config: LoaderConfig) -> None:
    try:
        project = get_project(job.project_id, config)
        patch: dict[str, Any] = {
            "last_job_id": job.job_id,
            "last_run_id": job.run_id,
        }
        if project.get("active_job_id") == job.job_id:
            patch["active_job_id"] = None
        if project.get("active_run_id") == job.run_id:
            patch["active_run_id"] = None
        patch_project(job.project_id, patch, config=config)
    except Exception:  # noqa: BLE001
        pass


def cancel_job(job_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    with _REGISTRY_LOCK:
        job = _JOB_REGISTRY.get(job_id)
    if job is None:
        data = _find_job_dict(job_id, config)
        if not data:
            raise FileNotFoundError(f"job not found: {job_id}")
        job = _job_from_dict(data)
    if not job.pid:
        raise FileNotFoundError(f"job {job_id} has no recorded pid")
    if job.status not in {"running", "queued", "cancelling"}:
        _clear_active_job_if_current(job, config)
        return job.to_dict()
    job.status = "cancelling"
    _save_job(job, _state_path_for(job, config))
    kill_error = _terminate_process_tree(job.pid)
    if kill_error:
        job.error = f"cancel failed: {kill_error}"
    else:
        # When the watcher is alive it will overwrite this with the real
        # return code. If the backend was restarted and only job.json remains,
        # this disk update prevents the UI from being stuck in "running".
        job.status = "cancelled"
        job.ended_at = job.ended_at or _now_iso()
    _save_job(job, _state_path_for(job, config))
    _save_job_result(job, _state_path_for(job, config).parent)
    _clear_active_job_if_current(job, config)
    return job.to_dict()


def get_job_log(job_id: str, config: LoaderConfig | None = None, *, tail_kb: int = 64) -> str:
    config = config or LoaderConfig()
    for project_dir in config.output_dir.iterdir() if config.output_dir.exists() else []:
        if not project_dir.is_dir():
            continue
        log_path = project_dir / "jobs" / job_id / "job.log"
        if not log_path.exists():
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


def list_job_iterations(job_id: str, config: LoaderConfig | None = None) -> list[dict[str, Any]]:
    config = config or LoaderConfig()
    job = get_job(job_id, config)
    run_dir = _job_run_dir(job, config)
    if run_dir is None:
        return []
    return case_loader._list_auto_adjust_iterations(run_dir, config)  # noqa: SLF001


def get_job_iteration_detail(job_id: str, iter_id: str, config: LoaderConfig | None = None) -> dict[str, Any]:
    config = config or LoaderConfig()
    job = get_job(job_id, config)
    run_dir = _job_run_dir(job, config)
    if run_dir is None:
        raise FileNotFoundError(f"job run artifacts not found: {job_id}")
    if not iter_id.startswith("iter_"):
        raise ValueError(f"unknown iter_id format: {iter_id!r}")
    return case_loader._load_auto_adjust_detail(job.get("project_id") or job_id, run_dir, iter_id, config)  # noqa: SLF001


def _job_run_dir(job: dict[str, Any], config: LoaderConfig) -> Path | None:
    raw = job.get("run_dir")
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        path = config.project_root / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.exists() and resolved.is_dir() else None


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
                if job.status in {"cancelling", "cancelled"}:
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
        _save_job_result(job, state_path.parent)
        try:
            patch_project(
                job.project_id,
                {
                    "active_job_id": None,
                    "last_job_id": job.job_id,
                    "active_run_id": None,
                    "last_run_id": job.run_id,
                },
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
    research = perceptual.get("research_metrics") if isinstance(perceptual.get("research_metrics"), dict) else {}
    return {
        "iteration": data.get("iteration"),
        "selected_stage": data.get("selected_stage"),
        "fit_score_before": data.get("fit_score_before"),
        "diff_score_before": data.get("diff_score_before"),
        "research_score": research.get("score"),
        "research_loss": research.get("loss"),
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


def _save_job_result(job: Job, job_dir: Path) -> None:
    payload = {
        "job_id": job.job_id,
        "project_id": job.project_id,
        "status": job.status,
        "return_code": job.return_code,
        "error": job.error,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
        "run_id": job.run_id,
        "run_dir": job.run_dir,
        "iterations_observed": job.iterations_observed,
        "last_iter_id": job.last_iter_id,
        "last_decision_summary": job.last_decision_summary,
    }
    try:
        (job_dir / "job_result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _state_path_for(job: Job, config: LoaderConfig) -> Path:
    paths = project_paths(job.project_id, config)
    return paths.jobs_dir / job.job_id / "job.json"


def _load_job_dict(job_id: str, jobs_dir: Path) -> dict[str, Any] | None:
    path = jobs_dir / job_id / "job.json"
    if not path.exists():
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


def _new_run_id(algo: dict[str, Any]) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    optimizer = _slug(str(algo.get("optimizer", "optimizer")))
    score_mode = _slug(str(algo.get("fit_score_mode", "score")))
    adjust_mode = _slug(str(algo.get("auto_adjust_mode", "mode")))
    return f"{stamp}-{optimizer}-{score_mode}-{adjust_mode}-{secrets.token_hex(2)}"


def _slug(value: str) -> str:
    safe = []
    for ch in value.strip().lower():
        if ch.isalnum():
            safe.append(ch)
        elif ch in {"_", "-"}:
            safe.append("-")
    slug = "".join(safe).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "default"


def _fit_config_for_run(fit_config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    payload = dict(fit_config)
    payload["output_dir"] = str(run_dir.resolve())
    payload["external_backup_dir"] = str((run_dir / "external_backups").resolve())
    screen_capture = dict(payload.get("screen_capture") or {})
    capture_dir = run_dir / "captures"
    screen_capture["capture_dir"] = str(capture_dir.resolve())
    screen_capture["state_file"] = str((capture_dir / ".capture_region.json").resolve())
    # Each run has its own capture directory, so keep every capture from
    # that run instead of pruning a shared rolling pool.
    screen_capture["max_keep"] = 0
    payload["screen_capture"] = screen_capture
    return payload


def _valid_refresh_session_cert(paths: Any, fit_config: dict[str, Any]) -> dict[str, Any] | None:
    cert_path = paths.project_dir / "preflight" / "refresh_session_cert.json"
    if not cert_path.exists():
        return None
    try:
        cert = json.loads(cert_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cert, dict) or not cert.get("success"):
        return None
    editor_capture = fit_config.get("laya_editor_capture") if isinstance(fit_config.get("laya_editor_capture"), dict) else {}
    lmat_path = str(Path(str(fit_config.get("laya_material_path") or "")).resolve())
    if str(cert.get("lmat_path") or "") != lmat_path:
        return None
    for key in ("laya_project", "command_path"):
        expected = str(editor_capture.get(key) or "")
        if expected and str(cert.get(key) or "") != expected:
            return None
    refresh_assets = editor_capture.get("refresh_assets") if isinstance(editor_capture.get("refresh_assets"), list) else []
    if [str(item) for item in refresh_assets] != [str(item) for item in cert.get("refresh_assets", [])]:
        return None
    if cert.get("reload_scene_after_reimport") is not False or cert.get("reimport_only") is not True:
        return None
    return {**cert, "path": str(cert_path.resolve())}


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now().isoformat(timespec="seconds")

"""Native file picker exposed via subprocess.

The FastAPI handler calls :func:`pick`, which spawns
``file_dialog_helper.py`` as a short-lived subprocess running ``tkinter``.
The user's selection (an absolute path or empty string for cancellation)
comes back via stdout JSON. No tkinter ever runs inside the uvicorn process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


HELPER_PATH = Path(__file__).with_name("file_dialog_helper.py")


def pick(
    *,
    mode: str = "open",
    title: str | None = None,
    initial_dir: str | None = None,
    initial_file: str | None = None,
    filetypes: list[list[str]] | None = None,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    """Run the native dialog helper and return ``{"path": "<abs path>"}``.

    Empty ``path`` means the user cancelled. The function itself does not
    raise on cancellation; callers decide how to interpret it. ``open_many``
    additionally returns ``{"paths": [...]}``.
    """

    payload = {
        "mode": mode,
        "title": title,
        "initial_dir": initial_dir,
        "initial_file": initial_file,
        "filetypes": filetypes,
    }
    args = [sys.executable, str(HELPER_PATH)]
    try:
        proc = subprocess.run(
            args,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=os.getcwd(),
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"path": "", "error": "file dialog timed out"}
    except FileNotFoundError as exc:
        return {"path": "", "error": f"helper not found: {exc}"}

    if proc.returncode != 0:
        return {"path": "", "error": (proc.stderr or "").strip() or f"helper exit {proc.returncode}"}

    try:
        out = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"path": "", "error": "helper returned non-JSON"}
    if not isinstance(out, dict):
        return {"path": "", "error": "helper returned non-object"}
    return out

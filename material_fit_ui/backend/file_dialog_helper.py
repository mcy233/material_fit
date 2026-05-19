"""Run a single tkinter file dialog in this process and print JSON to stdout.

Spawned as a short-lived subprocess by ``file_dialog.pick`` so that:
- the dialog never blocks the FastAPI event loop;
- we never have to start/stop a Tk root inside the long-running uvicorn process,
  which on Windows can corrupt the GUI thread state when called from a worker.

stdin payload (JSON, single line)::

    {
      "mode":          "open" | "open_many" | "save" | "directory",
      "title":         str,
      "initial_dir":   str | null,
      "initial_file":  str | null,
      "filetypes":     [["PNG image", "*.png"], ...] | null
    }

stdout payload (JSON, single line)::

    {"path": "<absolute path or empty string if cancelled>", "paths": ["<absolute path>", ...]}
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    raw = sys.stdin.read() or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        sys.stdout.write(json.dumps({"path": "", "error": "invalid stdin JSON"}))
        return 2

    try:
        from tkinter import Tk, filedialog
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"path": "", "error": f"tkinter unavailable: {exc}"}))
        return 3

    root = Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        root.update_idletasks()
        try:
            root.lift()
        except Exception:
            pass
        try:
            root.focus_force()
        except Exception:
            pass

        title = args.get("title") or "Select file"
        initial_dir = args.get("initial_dir") or os.getcwd()
        initial_file = args.get("initial_file") or ""
        raw_filetypes = args.get("filetypes")
        filetypes: list[tuple[str, str]] | None = None
        if isinstance(raw_filetypes, list):
            valid: list[tuple[str, str]] = []
            for entry in raw_filetypes:
                if isinstance(entry, list) and len(entry) >= 2:
                    valid.append((str(entry[0]), str(entry[1])))
            filetypes = valid or None

        mode = (args.get("mode") or "open").lower()
        if mode == "save":
            path = filedialog.asksaveasfilename(
                title=title,
                initialdir=initial_dir,
                initialfile=initial_file,
                filetypes=filetypes or [("All files", "*.*")],
            )
            paths: tuple[str, ...] | list[str] = ()
        elif mode == "directory":
            path = filedialog.askdirectory(title=title, initialdir=initial_dir)
            paths = ()
        elif mode == "open_many":
            paths = filedialog.askopenfilenames(
                title=title,
                initialdir=initial_dir,
                initialfile=initial_file,
                filetypes=filetypes or [("All files", "*.*")],
            )
            path = paths[0] if paths else ""
        else:
            path = filedialog.askopenfilename(
                title=title,
                initialdir=initial_dir,
                initialfile=initial_file,
                filetypes=filetypes or [("All files", "*.*")],
            )
            paths = ()
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    sys.stdout.write(json.dumps({"path": path or "", "paths": list(paths or [])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

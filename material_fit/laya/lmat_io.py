"""Read/write helpers for Laya ``.lmat`` material files.

This module is intentionally paranoid. ``.lmat`` is a JSON document that Laya
deserialises into a strongly typed material; the file *looks* free-form, but in
practice every field has an expected shape (scalar vs. vec4, bool vs. int,
texture-binding lives only inside ``props.textures``). One wrong-shaped value
and the whole material fails to load — which is exactly the failure mode that
prompted this hardening pass.

Therefore:

* ``apply_params`` refuses to write keys that don't already exist in the
  source material, and refuses to change the *shape* of an existing value
  (vec4 stays vec4, scalar stays scalar, bool stays bool, etc.). Anything
  weird raises :class:`LmatWriteError` instead of being silently saved.
* ``save_candidate_lmat`` round-trips through disk after writing and re-runs
  the same shape check, so a bug in the JSON encoder cannot corrupt the file
  under our nose.
* Everything that previously did ``props[k] = v`` now goes through the
  validating path; the old permissive ``apply_params`` is kept under
  ``apply_params_unchecked`` for unit-test convenience but is **not** what
  ``fit_material`` uses.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any


class LmatWriteError(ValueError):
    """Raised when a candidate .lmat would alter the file in unsafe ways.

    "Unsafe" here means: introducing a new top-level key, removing an existing
    one, or changing the *shape* (list length, list-vs-scalar, bool-vs-number,
    string-vs-anything) of an existing value. Numeric tweaks within the
    existing shape are allowed.
    """


def load_lmat(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_lmat(data: dict[str, Any], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    # Pin newline style explicitly: on Windows ``write_text`` would translate
    # ``\n`` to ``\r\n`` and produce a file that diffs noisily against the
    # original LF-terminated .lmat. Laya parses both, but we want byte-stable
    # output for VCS / round-trip checks.
    with open(output_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)


def backup_lmat(path: str | Path, suffix: str = ".bak", target_dir: str | Path | None = None) -> Path:
    source = Path(path)
    if target_dir is None:
        target = source.with_name(source.name + suffix)
    else:
        destination_dir = Path(target_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / f"{source.name}{suffix}"
    shutil.copy2(source, target)
    return target


def get_props(data: dict[str, Any]) -> dict[str, Any]:
    props = data.get("props")
    if not isinstance(props, dict):
        raise ValueError("Invalid .lmat: missing props object")
    return props


# Keys that look like material params but are actually engine bindings or
# header metadata (shader binding name, render queue, render mode). They must
# never be touched by the optimiser.
_RESERVED_TOP_LEVEL_KEYS = frozenset({"type", "renderQueue", "materialRenderMode"})


def extract_params(data: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of ``props`` that look like adjustable uniforms.

    We exclude:

    * ``textures`` and ``defines`` (structural, not scalar uniforms)
    * ``type``, ``renderQueue``, ``materialRenderMode`` (engine bindings)
    * ``s_*`` (engine render-state, not uniforms)

    The intent is that anything returned here is safe for an optimiser to
    propose new values for.
    """
    props = get_props(data)
    excluded = {"textures", "defines"} | _RESERVED_TOP_LEVEL_KEYS
    return {
        key: value
        for key, value in props.items()
        if key not in excluded and not key.startswith("s_")
    }


def extract_textures(data: dict[str, Any]) -> list[dict[str, Any]]:
    textures = get_props(data).get("textures", [])
    return textures if isinstance(textures, list) else []


def extract_defines(data: dict[str, Any]) -> list[str]:
    defines = get_props(data).get("defines", [])
    return defines if isinstance(defines, list) else []


def _shape_of(value: Any) -> str:
    """Compact, comparable description of a value's shape."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        # Track length so vec3 vs vec4 is caught.
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return "dict"
    if value is None:
        return "null"
    return type(value).__name__


def _shape_compatible(old: Any, new: Any) -> bool:
    """Return True if ``new`` can replace ``old`` without changing shape."""
    return _shape_of(old) == _shape_of(new)


def apply_params_unchecked(data: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Permissive overwrite — for tests only. Use :func:`apply_params`."""
    result = copy.deepcopy(data)
    props = get_props(result)
    for key, value in params.items():
        props[key] = value
    return result


def apply_params(
    data: dict[str, Any],
    params: dict[str, Any],
    *,
    allow_missing_keys: bool = False,
) -> dict[str, Any]:
    """Return a new ``.lmat`` dict with ``params`` applied to ``props``.

    Hard rules (raise :class:`LmatWriteError` on violation):

    * The key being written must already exist in ``props``. Adding new
      top-level uniforms is the bug that breaks textures (sampler defaults
      like ``"white"`` end up as top-level strings). If ``allow_missing_keys``
      is set we drop unknown keys silently rather than adding them.
    * The new value must have the same *shape* as the existing one (same
      list length, same primitive kind). Vec4 cannot become a scalar, a
      number cannot become a list, a bool cannot become a number.
    * Reserved keys (``type``, ``renderQueue``, ``materialRenderMode``,
      ``textures``, ``defines``, ``s_*``) cannot be overwritten via this
      function.

    Anything in ``params`` that fails these checks is reported in the
    raised error so the caller can fix the mapping.
    """
    result = copy.deepcopy(data)
    props = get_props(result)

    bad_new_keys: list[str] = []
    bad_shape: list[str] = []
    bad_reserved: list[str] = []

    for key, value in params.items():
        if (
            key in _RESERVED_TOP_LEVEL_KEYS
            or key in {"textures", "defines"}
            or key.startswith("s_")
        ):
            bad_reserved.append(key)
            continue
        if key not in props:
            if allow_missing_keys:
                continue
            bad_new_keys.append(f"{key}={value!r}")
            continue
        if not _shape_compatible(props[key], value):
            bad_shape.append(
                f"{key}: {_shape_of(props[key])}({props[key]!r}) -> {_shape_of(value)}({value!r})"
            )
            continue
        props[key] = value

    if bad_reserved or bad_new_keys or bad_shape:
        msg_parts = ["Refusing to write candidate .lmat — corruption guard tripped:"]
        if bad_reserved:
            msg_parts.append(
                "  * tried to overwrite reserved keys: " + ", ".join(sorted(bad_reserved))
            )
        if bad_new_keys:
            msg_parts.append(
                "  * tried to introduce keys not present in source .lmat:\n      "
                + "\n      ".join(bad_new_keys)
            )
        if bad_shape:
            msg_parts.append(
                "  * tried to change value shape on existing keys:\n      "
                + "\n      ".join(bad_shape)
            )
        raise LmatWriteError("\n".join(msg_parts))

    return result


def diff_shapes(original: dict[str, Any], rewritten: dict[str, Any]) -> list[str]:
    """Compare two .lmat dicts and return shape-changing differences.

    Pure value differences (e.g. ``u_Metallic: 0 -> 0.5``) are *not* reported;
    we only flag adds, removes and shape changes — i.e. things that would
    structurally break Laya parsing.
    """
    diffs: list[str] = []
    a = original.get("props", {}) if isinstance(original, dict) else {}
    b = rewritten.get("props", {}) if isinstance(rewritten, dict) else {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        diffs.append(f"props itself changed type: {type(a).__name__} -> {type(b).__name__}")
        return diffs
    for key in a.keys() - b.keys():
        diffs.append(f"REMOVED props.{key}")
    for key in b.keys() - a.keys():
        diffs.append(f"ADDED props.{key}={b[key]!r}")
    for key in a.keys() & b.keys():
        if not _shape_compatible(a[key], b[key]):
            diffs.append(
                f"SHAPE props.{key}: {_shape_of(a[key])}({a[key]!r}) -> "
                f"{_shape_of(b[key])}({b[key]!r})"
            )
    return diffs


def write_candidate_lmat(
    source_path: str | Path,
    output_path: str | Path,
    params: dict[str, Any],
    *,
    allow_missing_keys: bool = False,
) -> None:
    """Apply ``params`` to ``source_path`` and save to ``output_path``.

    Performs a full guard cycle:

    1. Validate ``params`` shape against the source via :func:`apply_params`.
    2. Save to disk.
    3. Re-read from disk and recompute :func:`diff_shapes`. If anything
       structural changed (add/remove key, shape mismatch) we delete the
       broken output and raise — this defends against JSON encoder bugs and
       race conditions overwriting the file with garbage.
    """
    source_data = load_lmat(source_path)
    rewritten = apply_params(source_data, params, allow_missing_keys=allow_missing_keys)
    output = Path(output_path)
    save_lmat(rewritten, output)
    try:
        reloaded = load_lmat(output)
    except (json.JSONDecodeError, OSError) as exc:
        try:
            output.unlink()
        except OSError:
            pass
        raise LmatWriteError(
            f"Saved candidate .lmat at {output} could not be re-parsed: {exc}"
        ) from exc
    diffs = diff_shapes(source_data, reloaded)
    if diffs:
        try:
            output.unlink()
        except OSError:
            pass
        raise LmatWriteError(
            "Saved candidate .lmat changed structure compared to source:\n  "
            + "\n  ".join(diffs)
        )

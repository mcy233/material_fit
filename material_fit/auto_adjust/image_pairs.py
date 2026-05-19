from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..vision.screen_capture import DEFAULT_CAPTURE_DIR, DEFAULT_PREFIX, find_latest_candidate


class ImagePairCollectionError(RuntimeError):
    """Raised when the configured multi-view pair contract is incomplete."""


def collect_image_pairs(
    config: dict[str, Any],
    project_root: Path,
    output_dir: Path,
    *,
    candidate_override: str | dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Collect reference/candidate image pairs for one auto-adjust iteration."""

    require_all_views = _require_all_views(config)
    pairs = config.get("image_pairs")
    if pairs:
        collected_pairs: list[dict[str, str]] = []
        missing: list[dict[str, str]] = []
        for pair in pairs:
            collected: dict[str, str] = {}
            view_id = _normalize_view_id(str(pair.get("view_id") or pair.get("id") or ""))
            if view_id:
                collected["view_id"] = view_id
            for key, value in pair.items():
                if key not in {"reference", "candidate", "mask"} or not value:
                    continue
                if key == "candidate" and candidate_override:
                    override = _resolve_candidate_override(pair, candidate_override)
                    if override:
                        collected[key] = override
                        continue
                    if isinstance(candidate_override, str):
                        collected[key] = candidate_override
                        continue
                    # No matching view in a multiview override; skip this pair.
                    missing.append(
                        {
                            "view_id": view_id or _normalize_view_id(str(pair.get("reference") or "")) or str(pair.get("reference") or ""),
                            "reference": str(pair.get("reference") or ""),
                            "reason": "missing_candidate_override",
                        }
                    )
                    continue
                if key == "candidate" and str(value).lower() == "latest":
                    latest = find_latest_candidate(
                        pair.get("candidate_dir", DEFAULT_CAPTURE_DIR),
                        pair.get("candidate_prefix", DEFAULT_PREFIX),
                    )
                    if latest:
                        collected[key] = str(latest)
                    else:
                        missing.append(
                            {
                                "view_id": view_id or _normalize_view_id(str(pair.get("reference") or "")) or str(pair.get("reference") or ""),
                                "reference": str(pair.get("reference") or ""),
                                "reason": "missing_latest_candidate",
                            }
                        )
                    continue
                collected[key] = str(_resolve_path(project_root, value))
            if "view_id" not in collected and collected.get("reference"):
                collected["view_id"] = _normalize_view_id(Path(collected["reference"]).stem) or Path(collected["reference"]).stem
            if "reference" in collected and "candidate" in collected:
                collected_pairs.append(collected)
            elif require_all_views and collected.get("reference"):
                missing.append(
                    {
                        "view_id": collected.get("view_id", ""),
                        "reference": collected.get("reference", ""),
                        "reason": "incomplete_pair",
                    }
                )
        _raise_if_missing_views(missing, collected_pairs, require_all_views)
        return collected_pairs

    editor_capture = config.get("laya_editor_capture")
    if isinstance(editor_capture, dict):
        reference_dir = editor_capture.get("reference_dir")
        if reference_dir:
            ref_dir = _resolve_path(project_root, reference_dir)
            pattern = str(editor_capture.get("reference_glob", "unity_ref_v*_yaw*_pitch*.png"))
            collected_pairs = []
            missing = []
            for reference in sorted(ref_dir.glob(pattern)):
                view_id = _extract_view_id(reference.stem)
                pair = {
                    "reference": str(reference),
                    "candidate": "latest",
                    "view_id": view_id or reference.stem,
                }
                if candidate_override:
                    override = _resolve_candidate_override(pair, candidate_override)
                    if override:
                        pair["candidate"] = override
                    else:
                        missing.append(
                            {
                                "view_id": str(pair["view_id"]),
                                "reference": str(reference),
                                "reason": "missing_candidate_override",
                            }
                        )
                if pair["candidate"] != "latest":
                    collected_pairs.append(pair)
            _raise_if_missing_views(missing, collected_pairs, require_all_views)
            if collected_pairs:
                return collected_pairs

    references = config.get("reference_images", [])
    candidates = config.get("candidate_images", [])
    masks = config.get("mask_images", [])
    collected: list[dict[str, str]] = []
    for index, reference in enumerate(references):
        if index < len(candidates):
            candidate = candidates[index]
            latest = find_latest_candidate(DEFAULT_CAPTURE_DIR, DEFAULT_PREFIX)
            override = _resolve_candidate_override({"reference": reference}, candidate_override)
            pair = {
                "reference": str(_resolve_path(project_root, reference)),
                "candidate": str(override or (
                    latest
                    if str(candidate).lower() == "latest" and latest
                    else _resolve_path(project_root, candidate)
                )),
                "view_id": _normalize_view_id(Path(str(reference)).stem) or f"view_{index:03d}",
            }
            if index < len(masks) and masks[index]:
                pair["mask"] = str(_resolve_path(project_root, masks[index]))
            collected.append(pair)
    if not collected:
        auto_reference = output_dir / "unity_reference.png"
        auto_candidate = output_dir / "laya_capture.png"
        if auto_reference.exists() and auto_candidate.exists():
            collected.append({"reference": str(auto_reference), "candidate": str(auto_candidate)})
    return collected


def _resolve_path(project_root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _resolve_candidate_override(pair: dict[str, Any], override: str | dict[str, str] | None) -> str | None:
    if not override:
        return None
    if isinstance(override, str):
        return override

    keys: list[str] = []
    for key in ("view_id", "id"):
        value = pair.get(key)
        if value:
            keys.append(str(value))
            normalized = _normalize_view_id(str(value))
            if normalized:
                keys.append(normalized)

    reference = pair.get("reference")
    if reference:
        ref_path = Path(str(reference))
        keys.extend([ref_path.name, ref_path.stem])
        normalized = _normalize_view_id(ref_path.stem)
        if normalized:
            keys.append(normalized)

    for key in keys:
        value = override.get(key)
        if value:
            return value
    return None


def _extract_view_id(stem: str) -> str | None:
    match = re.search(r"(v\d{3}_yaw-?\d+(?:\.\d+)?_pitch-?\d+(?:\.\d+)?)", stem)
    return match.group(1) if match else None


def _normalize_view_id(value: str) -> str | None:
    if not value:
        return None
    text = Path(value).stem
    match = re.search(r"(v\d{3}_yaw-?\d+(?:\.\d+)?_pitch-?\d+(?:\.\d+)?)", text)
    if match:
        return match.group(1)
    simple = re.search(r"\b(v\d{3})\b", text)
    if simple:
        return simple.group(1)
    return text if text.startswith("view_") else None


def _require_all_views(config: dict[str, Any]) -> bool:
    scoring = config.get("multiview_scoring")
    if isinstance(scoring, dict) and "require_all_views" in scoring:
        return bool(scoring.get("require_all_views"))
    editor_capture = config.get("laya_editor_capture")
    return bool(isinstance(editor_capture, dict) and editor_capture.get("reference_dir"))


def _raise_if_missing_views(
    missing: list[dict[str, str]],
    collected_pairs: list[dict[str, str]],
    require_all_views: bool,
) -> None:
    if not require_all_views or not missing:
        return
    missing_text = ", ".join(
        f"{item.get('view_id') or item.get('reference') or '?'}:{item.get('reason') or 'missing'}"
        for item in missing
    )
    raise ImagePairCollectionError(
        "Incomplete multi-view image pairs: "
        f"matched={len(collected_pairs)}, missing={len(missing)} ({missing_text})"
    )

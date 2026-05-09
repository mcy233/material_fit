from __future__ import annotations

from pathlib import Path
from typing import Any

from ..vision.screen_capture import DEFAULT_CAPTURE_DIR, DEFAULT_PREFIX, find_latest_candidate


def collect_image_pairs(
    config: dict[str, Any],
    project_root: Path,
    output_dir: Path,
    *,
    candidate_override: str | None = None,
) -> list[dict[str, str]]:
    """Collect reference/candidate image pairs for one auto-adjust iteration."""

    pairs = config.get("image_pairs")
    if pairs:
        collected_pairs: list[dict[str, str]] = []
        for pair in pairs:
            collected: dict[str, str] = {}
            for key, value in pair.items():
                if key not in {"reference", "candidate", "mask"} or not value:
                    continue
                if key == "candidate" and candidate_override:
                    collected[key] = candidate_override
                    continue
                if key == "candidate" and str(value).lower() == "latest":
                    latest = find_latest_candidate(
                        pair.get("candidate_dir", DEFAULT_CAPTURE_DIR),
                        pair.get("candidate_prefix", DEFAULT_PREFIX),
                    )
                    if latest:
                        collected[key] = str(latest)
                    continue
                collected[key] = str(_resolve_path(project_root, value))
            if "reference" in collected and "candidate" in collected:
                collected_pairs.append(collected)
        return collected_pairs

    references = config.get("reference_images", [])
    candidates = config.get("candidate_images", [])
    masks = config.get("mask_images", [])
    collected: list[dict[str, str]] = []
    for index, reference in enumerate(references):
        if index < len(candidates):
            candidate = candidates[index]
            latest = find_latest_candidate(DEFAULT_CAPTURE_DIR, DEFAULT_PREFIX)
            pair = {
                "reference": str(_resolve_path(project_root, reference)),
                "candidate": str(candidate_override or (
                    latest
                    if str(candidate).lower() == "latest" and latest
                    else _resolve_path(project_root, candidate)
                )),
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

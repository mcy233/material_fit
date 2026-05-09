from __future__ import annotations

"""Auto-adjust loop boundary.

The full loop still lives in ``fit_material.py`` while it is being untangled
from render/capture helpers. This module provides the intended import boundary
for new code and keeps lightweight helpers near the auto-adjust package.
"""

from .history import load_warm_start_history
from .image_pairs import collect_image_pairs
from .scoring import diff_score_to_fit_score, extract_perceptual_signals, resolve_fit_score

__all__ = [
    "collect_image_pairs",
    "diff_score_to_fit_score",
    "extract_perceptual_signals",
    "load_warm_start_history",
    "resolve_fit_score",
]

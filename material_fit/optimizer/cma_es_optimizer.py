"""CMA-ES optimizer for cross-engine material fitting.

This module replaces the per-stage heuristic in
:mod:`tools.material_fit.optimizer.adjustment_algorithm` with a black-box
optimization wrapper around the `cmaes <https://github.com/CyberAgentAILab/cmaes>`_
library, including support for *Warm Starting CMA-ES* (Nomura et al., AAAI
2021) — the technique we identified in
``tools/material_fit/docs/RelatedWork_Survey.md`` §7 as the cheapest way
to fold the existing heuristic results into a global optimizer.

Why this matters:

* The heuristic schedule has clear failure modes: it can only adjust one
  stage per iteration, has no backtracking, and assumes parameter
  decoupling that often fails in FishStandard-style shaders.
* CMA-ES handles couplings via its covariance matrix and is known to be
  competitive on the 10-100 dim non-convex non-smooth landscapes that
  inverse-rendering produces.
* Warm-starting CMA-ES from the heuristic's intermediate samples lets us
  recycle 5-15 evaluations of "free" prior information into a tighter
  initial sampling distribution, instead of throwing them away.

The class :class:`CmaesOptimizer` exposes a ``ask()`` / ``tell()`` loop in
the *parameter dict* representation that the rest of the pipeline already
speaks. Encoding/decoding to the flat ``np.ndarray`` CMA-ES requires is
handled by :class:`ParameterEncoder`, which:

* automatically skips texture-binding params, ``*_ST`` tiling vectors and
  other non-numeric uniforms (the same set of corruption-causing keys
  that ``parameter_search.build_initial_params`` already filters);
* uses ``ShaderParam.range_min/range_max`` for bounds when available,
  with name-based fallbacks identical to ``adjustment_algorithm._clamp_number``
  so we do not introduce a *second* bounds policy;
* preserves alpha (``[3]``) of color params untouched (we never optimize
  alpha here) and clamps RGB into ``[0, 1]``.

The module deliberately does *not* call into ``lmat_io`` or run any
real renderer — it produces *param dicts*, and it is the caller's job to
feed those into ``write_candidate_lmat`` + the Laya screenshot pipeline
to obtain a fitness. This separation is what makes it possible to also
unit-test the optimizer on synthetic objectives (see
``experiments/cma_es_warm_start_benchmark.py``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from ..shared.models import ShaderParam
from .effective_bounds import effective_bounds_for_param
from .semantic_graph import ParamSemantics, ShaderEffectGraph


JsonValue = float | int | bool | list[float | int | bool]
ParamDict = dict[str, JsonValue]
# E-010 bias hook: receives the just-sampled candidate in
# original-coordinate space and returns a possibly-modified vector
# of the same shape. Used by :class:`CmaesStrategy` to inject the
# ``adjustment_hints`` channel-level priors as a soft drift on the
# CMA-ES samples without bypassing the optimizer's learning loop.
from typing import Callable as _Callable
BiasCallback = _Callable[[np.ndarray], np.ndarray]


# Texture-binding uniforms and tiling vectors must never participate in
# black-box search: they are not numeric scalars in the .lmat domain, or
# they have semantics (UV scale/offset) that should never be touched by
# an appearance-fitting optimizer. Both sets duplicate the
# ``parameter_search`` policy so the encoder cannot accidentally accept
# something that the strict ``lmat_io.apply_params`` writer would reject
# at the file level.
_TEXTURE_PARAM_TYPES = frozenset({
    "texture2d", "texture", "texturecube",
    "sampler2d", "sampler", "samplercube",
    "rendertexture",
})

# *_ST is Unity's per-texture (scale_u, scale_v, offset_u, offset_v) and
# always matches the texture's physical layout, never the appearance.
_NAME_BLACKLIST = {
    "u_alpha",      # transparency, controlled by mask, not appearance
    "u_cutoff",     # alpha cutoff threshold
    "u_fresneluesf0",
    "u_fresneluesemoldenormal",
    "u_fresneluesnormal",
    "u_specularhighlights",
    "u_environmentreflections",
}
_NAME_SUFFIX_BLACKLIST = ("_st",)

def _is_texture_type(param: ShaderParam) -> bool:
    return str(param.param_type).strip().lower() in _TEXTURE_PARAM_TYPES


def _is_blacklisted_name(name: str) -> bool:
    lower = name.lower()
    if lower in _NAME_BLACKLIST:
        return True
    return any(lower.endswith(suffix) for suffix in _NAME_SUFFIX_BLACKLIST)


def _param_semantics_map(
    semantics: ShaderEffectGraph | dict[str, ParamSemantics] | None,
) -> dict[str, ParamSemantics]:
    if semantics is None:
        return {}
    if isinstance(semantics, ShaderEffectGraph):
        return dict(semantics.params)
    return dict(semantics)


def _axis_transform(
    semantic: ParamSemantics | None,
    name: str,
    param: ShaderParam | None,
) -> str:
    if semantic is not None and semantic.transform:
        return semantic.transform
    if param is not None and str(param.param_type).strip().lower() == "color":
        return "color_rgb"
    return "linear"


# Default bounds inferred from param-name keywords. Mirrors
# ``adjustment_algorithm._clamp_number`` so we don't run two clamp
# policies that disagree.
def _default_bounds(name: str, value: float) -> tuple[float, float]:
    lower = name.lower()
    if (bounds := effective_bounds_for_param(name)) is not None:
        return bounds
    low = -math.inf
    high = math.inf
    if any(token in lower for token in ("intensity", "strength", "scale")):
        low = max(low, 0.0)
        high = min(high, 8.0)
    if any(token in lower for token in ("threshold", "smooth", "metallic", "occlusion")):
        low = max(low, 0.0)
        high = min(high, 1.0)
    if "pow" in lower or "power" in lower:
        low = max(low, 0.0)
        high = min(high, 10.0)
    if "gamma" in lower:
        low = max(low, 0.05)
        high = min(high, 10.0)
    if low == -math.inf:
        # Fall back to a wide but finite interval centred on the current
        # value so CMA-ES has a chance of exploring without producing
        # NaNs.
        low = min(value - 4.0, -2.0)
    if high == math.inf:
        high = max(value + 4.0, 2.0)
    if low >= high:
        # Degenerate: open it up symmetrically.
        mid = 0.5 * (low + high)
        low = mid - 1.0
        high = mid + 1.0
    return low, high


@dataclass(frozen=True)
class _AxisSpec:
    """One scalar axis of the encoded vector, with bounds.

    A single ``ShaderParam`` produces 1 axis (scalar) or N axes (vector);
    each axis carries the (param_name, sub_index) pair so we can decode
    a flat ``np.ndarray`` back to a param dict losslessly.
    """

    param_name: str
    sub_index: int            # -1 = scalar; 0..3 = list index
    low: float
    high: float
    initial: float
    is_color: bool = False    # alpha (sub_index=3) of color is *not* an
                              # axis; this flag is for documentation
    transform: str = "linear"


class ParameterEncoder:
    """Bidirectional mapping between (param dict) and (flat np.ndarray).

    Construction selects the *trainable* subset of params from
    ``initial_params``: numeric scalars and numeric lists of length 3 or 4
    whose ``ShaderParam`` is not a texture binding and whose name is not
    blacklisted. For length-4 colors we expose only the 3 RGB channels;
    alpha is preserved verbatim from ``initial_params`` on every decode.
    """

    def __init__(
        self,
        initial_params: ParamDict,
        shader_params: Sequence[ShaderParam],
        *,
        param_whitelist: Iterable[str] | None = None,
        semantics: ShaderEffectGraph | dict[str, ParamSemantics] | None = None,
    ) -> None:
        self._param_info = {p.name: p for p in shader_params}
        self._semantics = _param_semantics_map(semantics)
        self._fixed: ParamDict = {}
        self._axes: list[_AxisSpec] = []
        # alpha-of-color values, indexed by param name (we round-trip them
        # outside the optimizer).
        self._alpha: dict[str, float] = {}
        whitelist = set(param_whitelist) if param_whitelist is not None else None

        for name, value in initial_params.items():
            if whitelist is not None and name not in whitelist:
                self._fixed[name] = value
                continue
            param = self._param_info.get(name)
            semantic = self._semantics.get(name)
            if semantic is not None and not semantic.searchable:
                self._fixed[name] = value
                continue
            if param is not None and _is_texture_type(param):
                self._fixed[name] = value
                continue
            if _is_blacklisted_name(name):
                self._fixed[name] = value
                continue
            if isinstance(value, bool):
                # Bool flags are not searched in black-box space; if a
                # caller really wants to flip them they should drive the
                # search separately (this keeps the search space
                # continuous, which is required by CMA-ES).
                self._fixed[name] = value
                continue
            if isinstance(value, (int, float)):
                low, high = self._scalar_bounds(name, float(value), param, semantic)
                transform = _axis_transform(semantic, name, param)
                low_t, high_t, initial_t, transform = self._transform_bounds(
                    low, high, float(value), transform
                )
                self._axes.append(
                    _AxisSpec(
                        param_name=name,
                        sub_index=-1,
                        low=low_t,
                        high=high_t,
                        initial=initial_t,
                        transform=transform,
                    )
                )
                continue
            if isinstance(value, list) and len(value) in (3, 4) and all(isinstance(x, (int, float)) for x in value):
                # Treat length-4 vector as RGB(A) iff name suggests a
                # color; otherwise treat all components as free axes.
                is_color = self._looks_like_color(name, param, len(value))
                effective_len = 3 if is_color and len(value) == 4 else len(value)
                if is_color and len(value) == 4:
                    self._alpha[name] = float(value[3])
                for sub_idx in range(effective_len):
                    sub_value = float(value[sub_idx])
                    low, high = self._sub_bounds(name, sub_idx, sub_value, param, is_color, semantic)
                    transform = "linear" if is_color else _axis_transform(semantic, name, param)
                    low_t, high_t, initial_t, transform = self._transform_bounds(
                        low, high, sub_value, transform
                    )
                    self._axes.append(
                        _AxisSpec(
                            param_name=name,
                            sub_index=sub_idx,
                            low=low_t,
                            high=high_t,
                            initial=initial_t,
                            is_color=is_color,
                            transform=transform,
                        )
                    )
                continue
            # Anything else (string, None, length-2 vec like u_*_ST that
            # somehow slipped past blacklist, etc.) is fixed.
            self._fixed[name] = value

    # ------------------------------------------------------------------
    # Public properties

    @property
    def dim(self) -> int:
        return len(self._axes)

    @property
    def lower_bounds(self) -> np.ndarray:
        return np.array([a.low for a in self._axes], dtype=np.float64)

    @property
    def upper_bounds(self) -> np.ndarray:
        return np.array([a.high for a in self._axes], dtype=np.float64)

    @property
    def initial_vector(self) -> np.ndarray:
        return np.array([a.initial for a in self._axes], dtype=np.float64)

    @property
    def axes(self) -> tuple[_AxisSpec, ...]:
        return tuple(self._axes)

    @property
    def fixed_params(self) -> ParamDict:
        return dict(self._fixed)

    # ------------------------------------------------------------------
    # encode / decode

    def encode(self, params: ParamDict) -> np.ndarray:
        """Convert a full param dict into a flat ``np.ndarray`` of length :attr:`dim`."""
        out = np.empty(len(self._axes), dtype=np.float64)
        for i, axis in enumerate(self._axes):
            value = params.get(axis.param_name)
            if axis.sub_index < 0:
                if not isinstance(value, (int, float)):
                    out[i] = axis.initial
                else:
                    out[i] = self._encode_axis_value(float(value), axis)
            else:
                if not isinstance(value, list) or axis.sub_index >= len(value):
                    out[i] = axis.initial
                else:
                    sub = value[axis.sub_index]
                    if not isinstance(sub, (int, float)):
                        out[i] = axis.initial
                    else:
                        out[i] = self._encode_axis_value(float(sub), axis)
        return out

    def decode(self, vector: np.ndarray) -> ParamDict:
        """Convert a flat vector back into a full param dict.

        The result starts from :attr:`fixed_params` (texture bindings,
        STs, blacklisted toggles) and then layers the trainable axes on
        top. Alpha of color params is restored from the alpha cache so
        the dict is round-trippable into ``write_candidate_lmat``.
        """
        if vector.shape != (len(self._axes),):
            raise ValueError(f"vector shape {vector.shape} does not match dim {len(self._axes)}")
        out: ParamDict = dict(self._fixed)
        # Materialize list-typed params lazily as we encounter sub-axes.
        list_buffers: dict[str, list[float]] = {}
        list_is_color: dict[str, bool] = {}
        for i, axis in enumerate(self._axes):
            value = self._decode_axis_value(float(vector[i]), axis)
            if axis.sub_index < 0:
                out[axis.param_name] = value
            else:
                buf = list_buffers.setdefault(axis.param_name, [])
                while len(buf) <= axis.sub_index:
                    buf.append(0.0)
                buf[axis.sub_index] = value
                list_is_color[axis.param_name] = axis.is_color
        for name, buf in list_buffers.items():
            if list_is_color.get(name) and name in self._alpha:
                # ensure RGBA length 4 with cached alpha
                while len(buf) < 3:
                    buf.append(0.0)
                buf = buf[:3] + [self._alpha[name]]
            out[name] = buf
        return out

    # ------------------------------------------------------------------
    # bounds helpers

    @staticmethod
    def _clamp(x: float, low: float, high: float) -> float:
        if not math.isfinite(x):
            return 0.5 * (low + high)
        return max(low, min(high, x))

    def _transform_bounds(
        self,
        low: float,
        high: float,
        initial: float,
        transform: str,
    ) -> tuple[float, float, float, str]:
        initial = self._clamp(initial, low, high)
        if transform == "log" and low > -1.0 and high > low:
            low_t = math.log1p(low)
            high_t = math.log1p(high)
            return low_t, high_t, self._clamp(math.log1p(initial), low_t, high_t), "log"
        return low, high, initial, "linear" if transform == "log" else transform

    def _encode_axis_value(self, value: float, axis: _AxisSpec) -> float:
        if axis.transform == "log":
            value = max(value, -0.999999)
            return self._clamp(math.log1p(value), axis.low, axis.high)
        return self._clamp(value, axis.low, axis.high)

    def _decode_axis_value(self, value: float, axis: _AxisSpec) -> float:
        value = self._clamp(value, axis.low, axis.high)
        if axis.transform == "log":
            return math.expm1(value)
        if axis.transform == "circular":
            width = axis.high - axis.low
            if width > 0:
                return axis.low + ((value - axis.low) % width)
        return value

    def _scalar_bounds(
        self,
        name: str,
        value: float,
        param: ShaderParam | None,
        semantic: ParamSemantics | None = None,
    ) -> tuple[float, float]:
        if (bounds := effective_bounds_for_param(name)) is not None:
            return bounds
        if semantic is not None:
            low = float(semantic.range_min) if semantic.range_min is not None else -math.inf
            high = float(semantic.range_max) if semantic.range_max is not None else math.inf
            if math.isfinite(low) and math.isfinite(high) and low < high:
                return low, high
        if param is not None:
            low = float(param.range_min) if param.range_min is not None else -math.inf
            high = float(param.range_max) if param.range_max is not None else math.inf
            if math.isfinite(low) and math.isfinite(high) and low < high:
                return low, high
            # If only one side is set, blend with name-based defaults.
            default_low, default_high = _default_bounds(name, value)
            if not math.isfinite(low):
                low = default_low
            if not math.isfinite(high):
                high = default_high
            if low < high:
                return low, high
        return _default_bounds(name, value)

    def _sub_bounds(
        self,
        name: str,
        sub_idx: int,
        value: float,
        param: ShaderParam | None,
        is_color: bool,
        semantic: ParamSemantics | None = None,
    ) -> tuple[float, float]:
        if is_color:
            return 0.0, 1.0
        # Vector but not color: fall back to scalar bounds policy.
        return self._scalar_bounds(f"{name}[{sub_idx}]", value, param, semantic)

    @staticmethod
    def _looks_like_color(name: str, param: ShaderParam | None, length: int) -> bool:
        if param is not None and str(param.param_type).strip().lower() == "color":
            return True
        if length == 4 and re.search(r"color|tint|albedo|emission", name, re.IGNORECASE):
            return True
        return False


# ----------------------------------------------------------------------
# CMA-ES wrapper


@dataclass
class CmaesConfig:
    """Hyper-parameters for the CMA-ES driver.

    Defaults mirror Hansen's recommendations for bounded problems with
    cheap evaluations *relative to* the population size. The optimizer
    runs CMA-ES in *normalized* [0, 1] coordinates internally so the
    same ``sigma`` works for axes that span 1 unit (colors) or 10 units
    (gamma). Decoding restores the original bounds. This avoids the
    heterogeneous-scale failure mode where one shared sigma is too
    small for some axes and too large for others.
    """
    population_size: int | None = None  # None lets the library default (4 + 3*log(dim))
    sigma: float = 0.30                 # in *normalized* [0, 1] coordinates
    seed: int | None = None
    bounds_handling: str = "clip"       # "clip" or "raise" — only "clip" is implemented today
    warm_start_gamma: float = 0.1       # Nomura et al. recommend 0.1
    warm_start_alpha: float = 0.1       # Nomura et al. recommend 0.1


class CmaesOptimizer:
    """ask()/tell() driver around :class:`cmaes.CMA` with WS support."""

    def __init__(
        self,
        encoder: ParameterEncoder,
        config: CmaesConfig | None = None,
        *,
        warm_start_samples: list[tuple[ParamDict, float]] | None = None,
        initial_mean: np.ndarray | ParamDict | None = None,
    ) -> None:
        try:
            from cmaes import CMA, get_warm_start_mgd
        except ImportError as exc:  # pragma: no cover - dependency error
            raise RuntimeError(
                "cmaes library is required. Install with: pip install cmaes"
            ) from exc

        self._encoder = encoder
        self._config = config or CmaesConfig()
        self._step = 0
        self._evaluated = 0
        self._best_vector: np.ndarray | None = None  # in *original* coordinates
        self._best_fitness = math.inf
        self._history: list[tuple[np.ndarray, float]] = []

        dim = encoder.dim
        if dim == 0:
            raise ValueError("ParameterEncoder produced an empty search space")

        # Internal CMA-ES runs in normalized [0, 1] coordinates so a
        # single sigma works across axes whose original widths differ
        # by orders of magnitude (color: 1.0, gamma: ~10, intensity: 8).
        self._lo = encoder.lower_bounds
        self._hi = encoder.upper_bounds
        self._width = self._hi - self._lo
        norm_bounds = np.stack(
            [np.zeros(dim, dtype=np.float64), np.ones(dim, dtype=np.float64)],
            axis=1,
        )
        sigma = max(float(self._config.sigma), 1e-3)

        if initial_mean is None:
            mean_orig = encoder.initial_vector
        elif isinstance(initial_mean, dict):
            mean_orig = encoder.encode(initial_mean)
        else:
            mean_orig = np.asarray(initial_mean, dtype=np.float64)
            if mean_orig.shape != (dim,):
                raise ValueError(f"initial_mean shape {mean_orig.shape} != ({dim},)")

        if warm_start_samples:
            source = self._build_warm_start_source(warm_start_samples)
            # cmaes.get_warm_start_mgd requires at least ``ceil(1/gamma)``
            # source solutions (it picks the top-(gamma*N) elite). Our
            # heuristic warm-start often has only 6-12 samples while the
            # library default gamma=0.1 needs ≥10. Auto-relax gamma so a
            # warm-start of 3 samples still works (picks top-1 as elite),
            # without ever using a gamma > 1.0.
            gamma = max(self._config.warm_start_gamma, 1.0 / max(len(source), 1))
            gamma = min(gamma, 1.0)
            mean, ws_sigma, cov = get_warm_start_mgd(
                source,
                gamma=gamma,
                alpha=self._config.warm_start_alpha,
            )
            self._cma = CMA(
                mean=np.asarray(mean, dtype=np.float64),
                sigma=float(ws_sigma),
                cov=np.asarray(cov, dtype=np.float64),
                bounds=norm_bounds,
                seed=self._config.seed,
                population_size=self._config.population_size,
            )
            self._warm_started = True
        else:
            self._cma = CMA(
                mean=self._to_norm(mean_orig),
                sigma=sigma,
                bounds=norm_bounds,
                seed=self._config.seed,
                population_size=self._config.population_size,
            )
            self._warm_started = False

        self._pending: list[np.ndarray] = []         # raw normalized vectors
        self._pending_results: list[tuple[np.ndarray, float]] = []

    def _to_norm(self, x: np.ndarray) -> np.ndarray:
        """Map original-coordinate vector into the [0, 1] CMA-ES space."""
        norm = (x - self._lo) / np.where(self._width > 0, self._width, 1.0)
        return np.clip(norm, 0.0, 1.0)

    def _from_norm(self, x_norm: np.ndarray) -> np.ndarray:
        """Map [0, 1] back into original coordinates (used for decoding)."""
        return self._lo + np.clip(x_norm, 0.0, 1.0) * self._width

    # ------------------------------------------------------------------
    # public properties

    @property
    def dim(self) -> int:
        return self._encoder.dim

    @property
    def population_size(self) -> int:
        return self._cma.population_size

    @property
    def best(self) -> tuple[ParamDict | None, float]:
        if self._best_vector is None:
            return None, math.inf
        return self._encoder.decode(self._best_vector), self._best_fitness

    @property
    def warm_started(self) -> bool:
        return self._warm_started

    @property
    def evaluations(self) -> int:
        return self._evaluated

    def should_stop(self) -> bool:
        return bool(self._cma.should_stop())

    # ------------------------------------------------------------------
    # ask / tell

    def ask(self, bias_callback: "BiasCallback | None" = None) -> ParamDict:
        """Sample one candidate parameter dict from the CMA-ES distribution.

        ``bias_callback`` is an optional hook (E-010) that receives the
        sampled candidate in **original-coordinate space** and must
        return a biased vector of the same shape. The biased vector is
        re-normalised before being stashed in ``_pending`` so that
        :meth:`tell` will hand CMA-ES the *biased* (point, fitness)
        pair — which is the mathematically correct way to inject
        soft expert priors: CMA-ES learns the relationship between
        what we actually evaluated and the loss, not what its
        unbiased sampling distribution suggested.

        The callback may also return the input unchanged (no-op) or
        clip into bounds; this method does NOT enforce bounds itself
        because the caller is in a better position to know which
        direction to clip toward.
        """

        vec_norm = self._cma.ask()
        vec_orig = self._from_norm(vec_norm)
        if bias_callback is not None:
            vec_orig = bias_callback(vec_orig)
            vec_orig = np.asarray(vec_orig, dtype=np.float64)
            vec_norm = self._to_norm(vec_orig)
        self._pending.append(vec_norm)
        return self._encoder.decode(vec_orig)

    def tell(self, fitness: float) -> None:
        """Report the fitness for the *most recently asked* candidate.

        Once a full population worth of samples has been told, the
        underlying CMA-ES is updated. Lower fitness is better
        (minimization).
        """
        if not self._pending:
            raise RuntimeError("tell() called without a matching ask()")
        vec_norm = self._pending.pop(0)
        fitness = float(fitness)
        self._pending_results.append((vec_norm, fitness))
        self._evaluated += 1
        vec_orig = self._from_norm(vec_norm)
        self._history.append((vec_orig.copy(), fitness))
        if fitness < self._best_fitness:
            self._best_fitness = fitness
            self._best_vector = vec_orig.copy()
        if len(self._pending_results) >= self._cma.population_size:
            self._cma.tell(self._pending_results)
            self._pending_results = []
            self._step += 1

    def history(self) -> list[tuple[ParamDict, float]]:
        return [(self._encoder.decode(v), f) for v, f in self._history]

    # ------------------------------------------------------------------
    # warm-start helpers

    def _build_warm_start_source(
        self,
        samples: list[tuple[ParamDict, float]],
    ) -> list[tuple[np.ndarray, float]]:
        if len(samples) < 2:
            raise ValueError(
                "warm_start_samples needs at least 2 (param, fitness) pairs to "
                "estimate a covariance"
            )
        # Normalize encoded vectors into [0, 1] before letting the
        # warm-start MGD estimator see them — covariance estimation in
        # raw coords would be dominated by the widest axes (gamma, etc.).
        return [(self._to_norm(self._encoder.encode(p)), float(f)) for p, f in samples]


# ----------------------------------------------------------------------
# convenience: heuristic-history → warm-started CMA-ES


def cmaes_from_heuristic_history(
    initial_params: ParamDict,
    shader_params: Sequence[ShaderParam],
    history: Sequence[tuple[ParamDict, float]],
    *,
    config: CmaesConfig | None = None,
    param_whitelist: Iterable[str] | None = None,
) -> CmaesOptimizer:
    """Build a WS-CMA-ES seeded from the heuristic's iteration history.

    ``history`` is the sequence of ``(params, fitness)`` pairs the
    heuristic has produced so far (e.g. the contents of
    ``output/<case>/auto_adjust/iter_*/``). Pairs whose param dict has
    a different set of trainable keys than ``initial_params`` are
    dropped — we cannot warm-start through a different search space.
    """
    encoder = ParameterEncoder(
        initial_params,
        shader_params,
        param_whitelist=param_whitelist,
    )
    if not history:
        return CmaesOptimizer(encoder, config=config)
    return CmaesOptimizer(encoder, config=config, warm_start_samples=list(history))


__all__ = [
    "CmaesConfig",
    "CmaesOptimizer",
    "ParameterEncoder",
    "cmaes_from_heuristic_history",
]

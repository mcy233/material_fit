from __future__ import annotations

"""Strategy interface facade.

The concrete classes still live in ``strategy.py`` for compatibility during
the current refactor, but new code should import the interface from here.
"""

from .strategy import OptimizerStrategy, OptimizerUnavailableError, StrategyContext

__all__ = ["OptimizerStrategy", "OptimizerUnavailableError", "StrategyContext"]

"""Pikselzone Memory V1 local foundation.

This package deliberately separates event memory from operational truth:
Kanban remains task truth, Git remains code truth, and generated knowledge is
derived context only.
"""

from .core import MemoryConfig, MemoryError

__all__ = ["MemoryConfig", "MemoryError"]

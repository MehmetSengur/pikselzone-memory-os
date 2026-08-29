"""Pikselzone Memory V1 local foundation.

This package deliberately separates event memory from operational truth:
Kanban remains task truth, Git remains code truth, and generated knowledge is
derived context only.
"""

from .companion import CompanionManager, LastSessionData, RuleItem, ThreadItem
from .core import MemoryConfig, MemoryError
from .rule_learner import RuleLearner

__all__ = [
    "CompanionManager",
    "LastSessionData",
    "MemoryConfig",
    "MemoryError",
    "RuleItem",
    "RuleLearner",
    "ThreadItem",
]

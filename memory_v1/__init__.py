"""Pikselzone Memory V1 local foundation.

This package deliberately separates event memory from operational truth:
Kanban remains task truth, Git remains code truth, and generated knowledge is
derived context only.
"""

from .companion import CompanionManager, LastSessionData, RuleItem, ThreadItem
from .core import MemoryConfig, MemoryError
from .doctor import run_doctor, run_self_healing
from .graph_engine import ConceptData, KnowledgeGraphEngine
from .rule_learner import RuleLearner
from .self_evolution import EvolutionProposal, EvolutionResult, SelfEvolutionEngine
from .skill_engine import SkillEngine, SkillSpec, WorkflowObservation

__all__ = [
    "CompanionManager",
    "ConceptData",
    "EvolutionProposal",
    "EvolutionResult",
    "KnowledgeGraphEngine",
    "LastSessionData",
    "MemoryConfig",
    "MemoryError",
    "RuleItem",
    "RuleLearner",
    "SelfEvolutionEngine",
    "SkillEngine",
    "SkillSpec",
    "ThreadItem",
    "WorkflowObservation",
    "run_doctor",
    "run_self_healing",
]

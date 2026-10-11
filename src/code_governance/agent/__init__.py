"""External Coding Agent orchestration for ChangeGuard."""

from .base import Agent, AgentContext, CodingAgent, CodingAgentResult
from .codex_agent import CodexAgent
from .contract_agent import ContractAgent, ContractGenerationError
from .harness import HarnessRunResult
from .orchestrator import Orchestrator
from .planner import Plan, PlannerAgent
from .repair import RepairAgent, RepairPlan

__all__ = (
    "HarnessRunResult",
    "Agent",
    "AgentContext",
    "CodingAgent",
    "CodingAgentResult",
    "CodexAgent",
    "ContractAgent",
    "ContractGenerationError",
    "Orchestrator",
    "Plan",
    "PlannerAgent",
    "RepairAgent",
    "RepairPlan",
)

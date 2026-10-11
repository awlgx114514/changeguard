"""Backward-compatible import location for the canonical Harness entry point."""

from .harness import HarnessOrchestrator


Orchestrator = HarnessOrchestrator

__all__ = ("HarnessOrchestrator", "Orchestrator")

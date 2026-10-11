from dataclasses import dataclass
from typing import Tuple

from code_governance.report.report import ChangeReport

from .base import Agent, AgentContext


@dataclass(frozen=True)
class RepairPlan:
    steps: Tuple[str, ...]
    findings: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "steps": list(self.steps),
            "findings": list(self.findings),
        }


class RepairAgent(Agent):
    """Turns deterministic Validator findings into bounded repair instructions."""

    def plan(self, report: ChangeReport) -> RepairPlan:
        steps = []
        findings = []
        for finding in report.findings:
            findings.append(f"{finding.code}: {finding.message}")
            if finding.code == "forbidden_file" and finding.path:
                steps.append(f"Revert changes to forbidden file: {finding.path}")
            elif finding.code == "unexpected_file" and finding.path:
                steps.append(f"Remove or move changes outside Contract scope: {finding.path}")
            elif finding.code == "diff_size_exceeded":
                steps.append("Reduce changed files or lines to the Contract diff limit.")
            elif finding.code == "verification_failed":
                steps.append("Fix the failing verification command before retrying.")
            else:
                steps.append(f"Resolve validator finding: {finding.message}")
        return RepairPlan(tuple(steps), tuple(findings))

    def run(self, context: AgentContext) -> RepairPlan:
        return self.plan(context.report)

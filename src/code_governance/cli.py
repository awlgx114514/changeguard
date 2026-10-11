import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from code_governance.contract.loader import ContractLoadError, load_contract
from code_governance.contract.validator import ContractValidator
from code_governance.agent.orchestrator import Orchestrator
from code_governance.evaluation.dataset import load_dataset
from code_governance.evaluation.report import render_json as render_evaluation_json
from code_governance.evaluation.runner import BenchmarkRunner
from code_governance.git.context import GitContextError
from code_governance.report.report import build_report, render_json, render_report
from code_governance.validator.core import ValidatorEngine
from code_governance.validator.assertions import AssertionEvaluatorRegistry
from code_governance.validator.evidence import (
    ValidationEvidence,
    ValidationEvidenceError,
    collect_workspace_snapshot,
)
from code_governance.verification.runner import execute_verification_claims
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.tools import ConsoleApprovalProvider, ShellTool, ToolPolicy, ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="changeguard")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate repository changes against a contract")
    validate.add_argument("contract", type=Path, help="path to Change Contract YAML")
    validate.add_argument("--repo", type=Path, default=Path("."), help="repository path (default: .)")
    validate.add_argument("--format", choices=("text", "json"), default="text")
    validate.add_argument(
        "--approval",
        choices=("interactive", "deny"),
        default="interactive" if sys.stdin.isatty() else "deny",
        help="approval behavior for governed verification commands",
    )
    run = commands.add_parser("run", help="run an external coding agent under ChangeGuard")
    run.add_argument("task_file", type=Path, help="path to a task Markdown file")
    run.add_argument("--repo", type=Path, default=Path("."), help="repository path (default: .)")
    run.add_argument(
        "--approval",
        choices=("interactive", "deny"),
        default="interactive" if sys.stdin.isatty() else "deny",
        help="approval behavior for governed write and command operations",
    )
    benchmark = commands.add_parser("benchmark", help="run an evaluation benchmark")
    benchmark.add_argument("--dataset", type=Path, required=True)
    benchmark.add_argument(
        "--mode",
        choices=("baseline", "hardened"),
        required=True,
    )
    benchmark.add_argument("--output", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        try:
            contract = load_contract(args.contract)
            admission = ContractValidator().validate(contract)
            if not admission.is_valid:
                for finding in admission.findings:
                    if finding.severity == "error":
                        print(f"Error: {finding.code}: {finding.message}", file=sys.stderr)
                return 2
            before = collect_workspace_snapshot(args.repo)
            approval_provider = ConsoleApprovalProvider() if args.approval == "interactive" else None
            registry = ToolRegistry(policy=ToolPolicy(), approval_provider=approval_provider)
            registry.register(ShellTool())
            verification = execute_verification_claims(
                tuple(contract.verification.claims),
                CanonicalActionExecutor(registry),
                args.repo,
            )
            after = collect_workspace_snapshot(args.repo)
            evidence = ValidationEvidence.from_snapshots(before, after, verification)
            assertion_results = tuple(
                AssertionEvaluatorRegistry().evaluate(assertion, evidence)
                for assertion in contract.verification.assertions
            )
            result = ValidatorEngine().validate(
                contract,
                evidence,
                assertion_results=assertion_results,
            )
            report = build_report(evidence, result, contract=contract)
        except (ContractLoadError, GitContextError, ValidationEvidenceError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 2

        print(render_json(report) if args.format == "json" else render_report(report))
        return {"PASS": 0, "WARNING": 1, "FAIL": 2}[result.status]

    if args.command == "run":
        approval_provider = ConsoleApprovalProvider() if args.approval == "interactive" else None
        result = Orchestrator(approval_provider=approval_provider).run(
            args.task_file,
            args.repo,
            progress=print,
        )
        if result.error is not None:
            print(f"Error: {result.error}", file=sys.stderr)
            return 2
        print(render_report(result.report))
        return {"PASS": 0, "WARNING": 1, "FAIL": 2}[result.validation_result]

    if args.command == "benchmark":
        try:
            dataset = load_dataset(args.dataset)
            report = BenchmarkRunner(dataset).run(args.mode)
            content = render_evaluation_json(report)
            if args.output is not None:
                args.output.write_text(content, encoding="utf-8")
            else:
                print(content)
        except Exception as error:
            print(f"Error: {error}", file=sys.stderr)
            return 2
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

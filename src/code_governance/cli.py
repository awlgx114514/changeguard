import argparse
import importlib
import sys
from pathlib import Path
from typing import Optional, Sequence


_LEGACY_EXPORTS = {
    "ContractLoadError": ("code_governance.contract.loader", "ContractLoadError"),
    "load_contract": ("code_governance.contract.loader", "load_contract"),
    "ContractValidator": ("code_governance.contract.validator", "ContractValidator"),
    "Orchestrator": ("code_governance.agent.orchestrator", "Orchestrator"),
    "load_dataset": ("code_governance.evaluation.dataset", "load_dataset"),
    "render_evaluation_json": ("code_governance.evaluation.report", "render_json"),
    "BenchmarkRunner": ("code_governance.evaluation.runner", "BenchmarkRunner"),
    "GitContextError": ("code_governance.git.context", "GitContextError"),
    "build_report": ("code_governance.report.report", "build_report"),
    "render_json": ("code_governance.report.report", "render_json"),
    "render_report": ("code_governance.report.report", "render_report"),
    "ValidatorEngine": ("code_governance.validator.core", "ValidatorEngine"),
    "AssertionEvaluatorRegistry": ("code_governance.validator.assertions", "AssertionEvaluatorRegistry"),
    "ValidationEvidence": ("code_governance.validator.evidence", "ValidationEvidence"),
    "ValidationEvidenceError": ("code_governance.validator.evidence", "ValidationEvidenceError"),
    "collect_workspace_snapshot": ("code_governance.validator.evidence", "collect_workspace_snapshot"),
    "execute_verification_claims": ("code_governance.verification.runner", "execute_verification_claims"),
    "CanonicalActionExecutor": ("code_governance.runtime.action_executor", "CanonicalActionExecutor"),
    "ConsoleApprovalProvider": ("code_governance.tools", "ConsoleApprovalProvider"),
    "ShellTool": ("code_governance.tools", "ShellTool"),
    "ToolPolicy": ("code_governance.tools", "ToolPolicy"),
    "ToolRegistry": ("code_governance.tools", "ToolRegistry"),
}


def __getattr__(name: str):
    if name not in _LEGACY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _LEGACY_EXPORTS[name]
    value = getattr(importlib.import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def _legacy_export(name: str):
    if name in globals():
        return globals()[name]
    return __getattr__(name)


def _audit_capture(run_id: str, repository: Path, sources: Sequence[Path]):
    from code_governance.audit.integrity import verify_audit_sources
    from code_governance.audit.snapshot import build_audit_snapshot
    from code_governance.audit.sources import capture_run_sources

    raw = capture_run_sources(run_id, repository, sources)
    verified = verify_audit_sources(raw)
    return raw, verified, build_audit_snapshot(raw, verified)


def _audit_source_summary(raw):
    return {
        "capture_id": raw.capture_id,
        "selected_sources": [source.to_dict() for source in raw.selected_sources],
        "discovery_scope": raw.discovery_scope.to_dict(),
        "read_boundaries": list(raw.read_boundaries),
    }


def _audit_diagnostics(verified):
    return [
        {"code": finding.reason_code, **finding.to_dict()}
        for finding in verified.findings
    ]


def _audit_status_exit(status: str) -> int:
    return {"COMPLETE": 0, "PARTIAL": 1, "INVALID": 2, "UNAVAILABLE": 3}[status]


def _emit_audit_result(command: str, status: str, sources, payload, diagnostics, output_format: str, snapshot=None, boundary=None) -> int:
    from code_governance.audit.presentation import render_human, render_json

    envelope = {
        "audit_schema_version": "1.0",
        "command": command,
        "audit_status": status,
        "sources": sources,
        "payload": payload,
        "diagnostics": diagnostics,
    }
    if output_format == "json":
        print(render_json(envelope))
    else:
        human_payload = payload
        if snapshot is not None:
            human_payload = {"snapshot": snapshot.to_dict(), **(payload if isinstance(payload, dict) else {})}
        print(render_human(command, status, human_payload, boundary))
    return _audit_status_exit(status)


def _audit_selection_error(command: str, status: str, code: str, message: str, output_format: str) -> int:
    payload = {
        "audit_schema_version": "1.0",
        "capture": {"requested_selector": None, "discovery_scope": {"completeness": "DISCOVERY_UNKNOWN"}},
        "identity": {"run_id": None},
        "execution": [],
        "evidence": [],
        "decision": [],
        "persistence": {},
        "integrity_findings": [],
        "diagnostics": [],
        "audit_status": status,
        "representation_gaps": [],
        "coverage": {},
    }
    return _emit_audit_result(
        command,
        status,
        [],
        payload,
        [{"code": code, "message": message, "status": status}],
        output_format,
    )


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
    for command in ("audit", "replay"):
        audit = commands.add_parser(command, help=f"inspect stored run history with {command}")
        audit.add_argument("run_id")
        audit.add_argument("--repo", type=Path, default=Path("."))
        audit.add_argument(
            "--source",
            type=Path,
            action="append",
            default=[],
            help="path to a sealed historical source; repeat to include more sources",
        )
        audit.add_argument("--format", choices=("human", "json"), default="human")
    diff = commands.add_parser("diff", help="compare stored run histories")
    diff.add_argument("run_a")
    diff.add_argument("run_b")
    diff.add_argument("--repo", type=Path, default=Path("."))
    diff.add_argument(
        "--source",
        type=Path,
        action="append",
        default=[],
        help="path to a sealed historical source; repeat to include more sources",
    )
    diff.add_argument("--format", choices=("human", "json"), default="human")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        ContractLoadError = _legacy_export("ContractLoadError")
        load_contract = _legacy_export("load_contract")
        ContractValidator = _legacy_export("ContractValidator")
        GitContextError = _legacy_export("GitContextError")
        build_report = _legacy_export("build_report")
        render_json = _legacy_export("render_json")
        render_report = _legacy_export("render_report")
        CanonicalActionExecutor = _legacy_export("CanonicalActionExecutor")
        ConsoleApprovalProvider = _legacy_export("ConsoleApprovalProvider")
        ShellTool = _legacy_export("ShellTool")
        ToolPolicy = _legacy_export("ToolPolicy")
        ToolRegistry = _legacy_export("ToolRegistry")
        AssertionEvaluatorRegistry = _legacy_export("AssertionEvaluatorRegistry")
        ValidatorEngine = _legacy_export("ValidatorEngine")
        ValidationEvidence = _legacy_export("ValidationEvidence")
        ValidationEvidenceError = _legacy_export("ValidationEvidenceError")
        collect_workspace_snapshot = _legacy_export("collect_workspace_snapshot")
        execute_verification_claims = _legacy_export("execute_verification_claims")

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
        Orchestrator = _legacy_export("Orchestrator")
        render_report = _legacy_export("render_report")
        ConsoleApprovalProvider = _legacy_export("ConsoleApprovalProvider")

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
        load_dataset = _legacy_export("load_dataset")
        render_evaluation_json = _legacy_export("render_evaluation_json")
        BenchmarkRunner = _legacy_export("BenchmarkRunner")

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

    if args.command in {"audit", "replay"}:
        from code_governance.audit.analysis import build_replay, first_failing_boundary

        try:
            raw, verified, snapshot = _audit_capture(args.run_id, args.repo, args.source)
        except ValueError as error:
            return _audit_selection_error(args.command, "INVALID", "INVALID_SELECTION", str(error), args.format)
        except OSError as error:
            return _audit_selection_error(args.command, "UNAVAILABLE", "SOURCE_UNAVAILABLE", str(error), args.format)

        snapshot_view = snapshot.to_dict()
        boundary = first_failing_boundary(snapshot)
        if args.command == "audit":
            payload = snapshot_view
        else:
            payload = build_replay(snapshot)
            payload["first_failing_boundary"] = boundary
        return _emit_audit_result(
            args.command,
            snapshot.audit_status,
            _audit_source_summary(raw),
            payload,
            _audit_diagnostics(verified),
            args.format,
            snapshot,
            boundary,
        )

    if args.command == "diff":
        from code_governance.audit.analysis import build_semantic_diff

        try:
            left_raw, left_verified, left_snapshot = _audit_capture(args.run_a, args.repo, args.source)
            right_raw, right_verified, right_snapshot = _audit_capture(args.run_b, args.repo, args.source)
        except ValueError as error:
            return _audit_selection_error("diff", "INVALID", "INVALID_SELECTION", str(error), args.format)
        except OSError as error:
            return _audit_selection_error("diff", "UNAVAILABLE", "SOURCE_UNAVAILABLE", str(error), args.format)
        payload = build_semantic_diff(left_snapshot, right_snapshot)
        status_values = {left_snapshot.audit_status, right_snapshot.audit_status}
        if "INVALID" in status_values:
            status = "INVALID"
        elif "UNAVAILABLE" in status_values:
            status = "UNAVAILABLE"
        elif payload["comparability"] != "FULLY_COMPARABLE" or status_values != {"COMPLETE"}:
            status = "PARTIAL"
        else:
            status = "COMPLETE"
        diagnostics = _audit_diagnostics(left_verified) + _audit_diagnostics(right_verified) + payload["diagnostics"]
        sources = [_audit_source_summary(left_raw), _audit_source_summary(right_raw)]
        return _emit_audit_result("diff", status, sources, payload, diagnostics, args.format)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

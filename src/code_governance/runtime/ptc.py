"""Fail-closed syntax validation for the experimental read-only PTC mode."""

import ast
import json
import sys

from .bounded_execution import BoundedToolExecutor
from .execution import ExecutionResult, FailureRecord, evidence_qualifiers
from .action import Action, OperationKind


_SDK_CALLS = frozenset({"search_files", "git_status", "git_diff"})
_PURE_CALLS = frozenset({"len", "range"})
_ALLOWED_NODES = (
    ast.Module, ast.Assign, ast.If, ast.For, ast.Expr, ast.Name, ast.Constant,
    ast.List, ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.Load, ast.Store,
    ast.Compare, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Call, ast.Attribute,
    ast.keyword, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is,
    ast.IsNot, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Add, ast.Sub, ast.USub,
)


class PTCProgramRejected(ValueError):
    """Raised before a worker is started for unsupported or unsafe source."""


class PTCCodeRuntime:
    """Run a validated PTC program in a Runtime-owned bounded worker."""

    def __init__(self, *, timeout_seconds: float = 20.0, executor=None, tool_registry=None, worker_command=None):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._executor = executor or BoundedToolExecutor(max_output_bytes=32 * 1024)
        self._tool_registry = tool_registry
        self._worker_command = tuple(worker_command or (sys.executable, "-m", "code_governance.runtime.ptc_worker"))

    def execute(self, action, workspace) -> ExecutionResult:
        try:
            validate_program(action.operation.fields.get("program"))
        except PTCProgramRejected as error:
            return self._failure(action, "PTC_PROGRAM_REJECTED", error)
        subcalls = []
        child_omissions = []
        execution = self._executor.run_ptc_worker(
            self._worker_command, workspace,
            timeout_seconds=self._timeout_seconds, action_id=action.id,
            initial_request={"program": action.operation.fields["program"]},
            request_handler=lambda request: self._tool_request(
                request, workspace, action.id, subcalls, child_omissions,
            ),
        )
        if execution.status == "TIMEOUT":
            return self._failure(
                action, "PTC_CODE_TIMEOUT", TimeoutError("PTC worker timed out"),
                metadata={"effect_uncertain": False, "output_partial": True},
            )
        if execution.status != "SUCCESS" or execution.returncode != 0:
            return self._failure(action, "PTC_CODE_RUNTIME_FAILURE", RuntimeError(execution.stderr[:512] or execution.error or "PTC worker failed"))
        try:
            value = json.loads(execution.stdout)["result"]
            value, truncated = _bounded_value(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return self._failure(action, "PTC_CODE_RUNTIME_FAILURE", error)
        failures = {}
        for child in subcalls:
            if child.failure is not None:
                category = f"PTC_SUBCALL_{child.failure.category}"
                failures[category] = failures.get(category, 0) + 1
        rendered = json.dumps(value, ensure_ascii=False)
        truncated = truncated or any(child_omissions) or len(rendered) > 4096
        metadata = {"curated_result": value, "truncated": truncated, "subcall_count": len(subcalls), "failed_subcalls": failures}
        if failures:
            failure = FailureRecord(
                stage="TOOL_EXECUTION",
                category="PTC_SUBCALL_FAILURE",
                action_id=action.id,
                detail="one or more PTC child actions failed",
                error_type="PTCChildFailure",
            )
            return ExecutionResult(
                action_id=action.id,
                status="FAILURE",
                effect_type="READ",
                result_fingerprint=_fingerprint(value),
                summary="\n".join((*evidence_qualifiers(metadata), failure.detail)),
                metadata=metadata,
                failure=failure,
            )
        return ExecutionResult(action_id=action.id, status="SUCCESS", effect_type="READ",
            state_changed=any(child.state_changed for child in subcalls),
            information_gain=any(child.success and child.information_gain for child in subcalls),
            result_fingerprint=_fingerprint(value),
            summary="\n".join((*evidence_qualifiers(metadata), f"PTC curated result: {rendered[:4096]}")),
            metadata=metadata)

    def _tool_request(self, request, workspace, parent_action_id, subcalls, child_omissions=None):
        if self._tool_registry is None:
            return {"ok": False, "data": {"failure_category": "PTC_CODE_RUNTIME_FAILURE"}}
        name, args = request.get("name"), request.get("args")
        if name == "search_files" and isinstance(args, list) and len(args) == 1 and isinstance(args[0], str):
            child = Action.create_operation(OperationKind.FILE_FIND, name_query=args[0])
        elif name == "git_status" and args == []:
            child = Action.create_operation(OperationKind.GIT_STATUS)
        elif name == "git_diff" and args == []:
            child = Action.create_operation(OperationKind.GIT_DIFF)
        else:
            return {"ok": False, "data": {"failure_category": "PTC_PROGRAM_REJECTED"}}
        result = self._tool_registry.execute(child, workspace, parent_action_id=parent_action_id)
        subcalls.append(result)
        if child_omissions is not None:
            child_omissions.append(_child_result_omitted(result))
        trace = getattr(self._tool_registry, "_trace_manager", None)
        if trace is not None:
            trace.emit("ptc_child_result", {
                "parent_action_id": parent_action_id,
                "action_id": result.action_id,
                "result": result.to_dict(),
            })
        if not result.success:
            return {"ok": False, "data": {"failure_category": result.failure.category if result.failure else "PTC_SUBCALL_TOOL_FAILURE"}}
        if name == "search_files":
            paths = list(result.metadata.get("paths", ()))
            if child_omissions is not None and len(paths) > 100:
                child_omissions[-1] = True
            return {"ok": True, "data": paths[:100]}
        return {"ok": True, "data": {"stdout": result.metadata.get("stdout_preview", ""), "stderr": result.metadata.get("stderr_preview", "")}}

    @staticmethod
    def _failure(action, category, error, metadata=None):
        failure = FailureRecord(stage="TOOL_EXECUTION", category=category, action_id=action.id, detail=str(error)[:512], error_type=type(error).__name__)
        facts = {"failure_category": category, **dict(metadata or {})}
        summary = "\n".join((*evidence_qualifiers(facts), failure.detail or category))
        return ExecutionResult(action_id=action.id, status="TIMEOUT" if category == "PTC_CODE_TIMEOUT" else "FAILURE", summary=summary, metadata=facts, failure=failure)


def _bounded_value(value, depth=0):
    if depth > 6: return "<truncated-depth>", True
    if isinstance(value, str): return (value[:4096], len(value) > 4096)
    if isinstance(value, (list, tuple)):
        items = [_bounded_value(item, depth + 1) for item in value[:100]]
        return [item for item, _ in items], len(value) > 100 or any(flag for _, flag in items)
    if isinstance(value, dict):
        items = list(value.items())[:100]
        bounded = {}
        truncated = len(value) > 100
        for key, item in items:
            key_text = str(key)
            bounded_key = key_text[:256]
            bounded_value, item_truncated = _bounded_value(item, depth + 1)
            bounded[bounded_key] = bounded_value
            truncated = truncated or len(key_text) > 256 or item_truncated
        return bounded, truncated
    if value is None or isinstance(value, (bool, int, float)): return value, False
    raise ValueError("PTC result is not serializable")


def _child_result_omitted(result) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if metadata.get("truncated") is True:
        return True
    return any(
        metadata.get(key) is True
        for key in ("stdout_truncated", "stderr_truncated", "content_truncated")
    )


def _fingerprint(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def validate_program(source: str, *, max_bytes: int = 16_384, max_nodes: int = 1_000) -> ast.Module:
    if not isinstance(source, str) or not source.strip():
        raise PTCProgramRejected("program must be non-empty")
    if len(source.encode("utf-8")) > max_bytes:
        raise PTCProgramRejected("program exceeds the source limit")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as error:
        raise PTCProgramRejected("program syntax is unsupported") from error
    if len(list(ast.walk(tree))) > max_nodes:
        raise PTCProgramRejected("program exceeds the syntax limit")
    _Validator(tree).validate()
    return tree


class _Validator(ast.NodeVisitor):
    def __init__(self, tree: ast.Module):
        self._parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self._tree = tree

    def validate(self) -> None:
        result_positions = [index for index, statement in enumerate(self._tree.body) if self._is_result(statement)]
        if result_positions != [len(self._tree.body) - 1]:
            raise PTCProgramRejected("program requires one final result assignment")
        self.visit(self._tree)

    def generic_visit(self, node):
        if not isinstance(node, _ALLOWED_NODES):
            raise PTCProgramRejected("program uses unsupported syntax")
        super().generic_visit(node)

    def visit_Assign(self, node):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            raise PTCProgramRejected("assignment target is unsupported")
        if node.targets[0].id.startswith("__"):
            raise PTCProgramRejected("dunder names are forbidden")
        self.visit(node.targets[0])
        self.visit(node.value)

    def visit_For(self, node):
        if not isinstance(node.target, ast.Name) or not isinstance(node.iter, ast.Call):
            raise PTCProgramRejected("for loop is unsupported")
        if not isinstance(node.iter.func, ast.Name) or node.iter.func.id != "range":
            raise PTCProgramRejected("for loop must use range")
        if not 1 <= len(node.iter.args) <= 2 or node.iter.keywords or not all(
            isinstance(value, ast.Constant) and isinstance(value.value, int) and 0 <= value.value <= 100
            for value in node.iter.args
        ):
            raise PTCProgramRejected("range must be statically bounded")
        if node.orelse:
            raise PTCProgramRejected("for else is unsupported")
        self.generic_visit(node)

    def visit_Call(self, node):
        direct_name = node.func.id if isinstance(node.func, ast.Name) else ""
        sdk_name = node.func.attr if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "tools" else ""
        if direct_name not in _PURE_CALLS and sdk_name not in _SDK_CALLS:
            raise PTCProgramRejected("call target is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        parent = self._parents.get(node)
        if not (isinstance(node.value, ast.Name) and node.value.id == "tools" and node.attr in _SDK_CALLS and isinstance(parent, ast.Call) and parent.func is node):
            raise PTCProgramRejected("attribute access is not allowed")

    def visit_Name(self, node):
        if "__" in node.id:
            raise PTCProgramRejected("dunder names are forbidden")

    @staticmethod
    def _is_result(statement) -> bool:
        return isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "result"

from pathlib import Path
import hashlib
import json
import sys
from typing import Mapping

from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.runtime.process_lifecycle import ProcessLifecycleError
from .base import Tool


class FilesystemTool(Tool):
    """Workspace filesystem operations, each entered through an explicit V2 route."""

    name = "filesystem"
    description = "Read, find, search, replace, write, or create workspace files."
    input_schema = {"operation": "file.read | file.find | file.search | file.replace | file.write | file.create"}

    def __init__(self, executor=None, search_worker_command=None, max_results: int = 1000):
        self._executor = executor or BoundedToolExecutor()
        self._search_worker_command = tuple(search_worker_command or (sys.executable, "-m", "code_governance.tools.search_worker"))
        self._max_results = max_results

    def execute(self, payload: Mapping[str, object], workspace: Path) -> dict:
        operation = str(payload.get("operation") or "")
        if operation == "file.read":
            return self._read(payload, workspace)
        if operation == "file.find":
            return self._find(payload, workspace)
        if operation == "file.search":
            return self._search(payload, workspace)
        if operation == "file.replace":
            return self._replace(payload, workspace)
        if operation == "file.write":
            return self._write(payload, workspace)
        if operation == "file.create":
            return self._create(payload, workspace)
        raise ValueError(f"unsupported filesystem operation: {operation}")

    def _read(self, payload, workspace):
        path = self._path(str(payload.get("path", "")), workspace)
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        cursor = payload.get("cursor")
        if cursor:
            try:
                start = int(str(cursor).removeprefix("line:"))
            except ValueError as error:
                raise ValueError("file.read cursor is invalid") from error
        else:
            start = int(payload.get("start_line", 1))
        count = int(payload.get("line_count", 200))
        visible = "".join(lines[start - 1 : start - 1 + count])
        next_start = start + count
        return {
            "path": path.relative_to(Path(workspace).resolve()).as_posix(),
            "content": visible,
            "file_hash": self._hash(content),
            "start_line": start,
            "line_count": count,
            "next_cursor": None if next_start > len(lines) else f"line:{next_start}",
            "eof": next_start > len(lines),
        }

    def _find(self, payload, workspace):
        query = str(payload.get("name_query") or payload.get("query") or "").lower()
        if not query:
            raise ValueError("file.find requires name_query")
        request = json.dumps({"workspace": str(Path(workspace).resolve()), "query": query, "max_results": self._max_results})
        execution = self._executor.run_subprocess(
            self._search_worker_command,
            workspace,
            timeout_seconds=float(payload.get("_runtime_timeout_seconds", 30.0)),
            action_id=str(payload.get("_runtime_action_id", "")),
            trace=payload.get("_runtime_trace"),
            input_text=request,
            omission_callback=payload.get("_runtime_capture_omission_callback"),
        )
        if execution.status == "TIMEOUT":
            raise TimeoutError("filesystem find timed out after the bounded deadline")
        if execution.status == "FAILURE":
            raise ProcessLifecycleError(execution.error or "filesystem find worker lifecycle failed")
        try:
            paths = json.loads(execution.stdout)["paths"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("filesystem find worker returned invalid paths") from error
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError("filesystem find worker returned invalid paths")
        directory = payload.get("directory")
        if directory:
            prefix = str(directory).rstrip("/") + "/"
            paths = [path for path in paths if path.startswith(prefix)]
        return {"paths": sorted(paths[: self._max_results])}

    def _search(self, payload, workspace):
        query = str(payload.get("query", ""))
        if not query:
            raise ValueError("file.search requires query")
        root = self._path_or_root(payload.get("path"), workspace)
        files = [root] if root.is_file() else list(root.rglob("*"))
        matches = []
        for path in files:
            if not path.is_file():
                continue
            try:
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if query in line:
                        matches.append({"path": path.relative_to(Path(workspace).resolve()).as_posix(), "line": line_number, "text": line[:512]})
                        if len(matches) >= self._max_results:
                            return {"matches": matches}
            except UnicodeDecodeError:
                continue
        return {"matches": matches}

    def _replace(self, payload, workspace):
        path = self._path(str(payload.get("path", "")), workspace)
        current = path.read_text(encoding="utf-8")
        expected_hash = str(payload.get("observed_file_hash", ""))
        if self._hash(current) != expected_hash:
            raise ValueError("file.replace observed_file_hash is stale")
        expected = payload.get("expected_text")
        replacement = payload.get("replacement")
        if not isinstance(expected, str) or not isinstance(replacement, str) or current.count(expected) != 1:
            raise ValueError("file.replace expected_text must match exactly once")
        updated = current.replace(expected, replacement, 1)
        path.write_text(updated, encoding="utf-8")
        return self._write_result(path, workspace, current, updated)

    def _write(self, payload, workspace):
        path = self._path(str(payload.get("path", "")), workspace)
        current = path.read_text(encoding="utf-8")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("file.write content must be text")
        if self._hash(current) != str(payload.get("observed_file_hash", "")):
            raise ValueError("file.write observed_file_hash is stale")
        path.write_text(content, encoding="utf-8")
        return self._write_result(path, workspace, current, content)

    def _create(self, payload, workspace):
        path = self._new_path(str(payload.get("path", "")), workspace)
        if path.exists():
            raise ValueError("file.create refuses to overwrite an existing file")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("file.create content must be text")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self._write_result(path, workspace, None, content)

    def _write_result(self, path, workspace, previous, content):
        return {"path": path.relative_to(Path(workspace).resolve()).as_posix(), "written": len(content), "state_changed": previous != content, "content_fingerprint": self._hash(content), "file_hash": self._hash(content)}

    @staticmethod
    def _hash(content):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _path_or_root(self, value, workspace):
        if value is None or value == "":
            return Path(workspace).resolve()
        return self._path(str(value), workspace)

    def _path(self, value: str, workspace: Path) -> Path:
        root = Path(workspace).resolve()
        candidate = (root / value).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise ValueError("path must name an existing workspace file")
        return candidate

    def _new_path(self, value: str, workspace: Path) -> Path:
        root = Path(workspace).resolve()
        candidate = (root / value).resolve()
        if root not in candidate.parents or not value:
            raise ValueError("path must be a workspace-relative file path")
        return candidate

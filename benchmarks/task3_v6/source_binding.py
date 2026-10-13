from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path


_GENERATED_DIRECTORIES = frozenset(
    {".git", ".pytest_cache", "__pycache__", "build", "dist"}
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_tree_identity(project_root: Path) -> tuple[str, int]:
    root = project_root.resolve(strict=True)
    digest = hashlib.sha256()
    file_count = 0

    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _GENERATED_DIRECTORIES and not name.endswith(".egg-info")
        )
        for filename in sorted(filenames):
            path = Path(current, filename)
            if path.suffix in {".pyc", ".pyo"} or filename.endswith(".egg-info"):
                continue

            relative_path = path.relative_to(root).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative_path).to_bytes(8, "big"))
            digest.update(relative_path)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
            file_count += 1

    return digest.hexdigest(), file_count


def current_import_evidence(
    expected_source_root: Path,
    expected_backend_sha256: str,
    *,
    run_id: str,
    fixture_identity: str,
    asset_identity: str,
    environment_identity: str,
    case_id: str,
) -> dict[str, object]:
    import anyio
    import anyio._backends._asyncio as asyncio_backend

    source_root = expected_source_root.resolve(strict=True)
    expected_anyio_path = (source_root / "anyio" / "__init__.py").resolve()
    expected_backend_path = (
        source_root / "anyio" / "_backends" / "_asyncio.py"
    ).resolve()
    actual_anyio_path = Path(anyio.__file__).resolve(strict=True)
    actual_backend_path = Path(asyncio_backend.__file__).resolve(strict=True)
    actual_backend_sha256 = file_sha256(actual_backend_path)
    binding_errors = []

    if actual_anyio_path != expected_anyio_path:
        binding_errors.append("AnyIO package path differs from the expected source root")
    if actual_backend_path != expected_backend_path:
        binding_errors.append("asyncio backend path differs from the expected source root")
    if actual_backend_sha256 != expected_backend_sha256:
        binding_errors.append("asyncio backend SHA256 differs from the expected source")

    return {
        "run_id": run_id,
        "case_id": case_id,
        "fixture_identity": fixture_identity,
        "asset_identity": asset_identity,
        "environment_identity": environment_identity,
        "python_executable": str(Path(sys.executable).resolve()),
        "cwd": str(Path.cwd().resolve()),
        "expected_source_root": str(source_root),
        "expected_anyio_path": str(expected_anyio_path),
        "actual_anyio_path": str(actual_anyio_path),
        "expected_backend_path": str(expected_backend_path),
        "actual_backend_path": str(actual_backend_path),
        "actual_backend_sha256": actual_backend_sha256,
        "expected_backend_sha256": expected_backend_sha256,
        "sys_path": [str(Path(entry or Path.cwd()).resolve()) for entry in sys.path],
        "binding_errors": binding_errors,
        "binding_status": "PASS" if not binding_errors else "SOURCE_BINDING_INVALID",
    }


def pytest_configure(config: object) -> None:
    project_root = Path(os.environ["TASK3_W2_SOURCE_ROOT"]).resolve(strict=True)
    import_root = Path(os.environ["TASK3_W2_IMPORT_ROOT"]).resolve(strict=True)
    expected_tree_hash = os.environ["TASK3_W2_EXPECTED_TREE_SHA256"]
    expected_file_count = int(os.environ["TASK3_W2_EXPECTED_FILE_COUNT"])
    expected_backend_sha256 = os.environ["TASK3_W2_EXPECTED_BACKEND_SHA256"]
    evidence_path = Path(os.environ["TASK3_W2_IMPORT_EVIDENCE_PATH"])
    run_id = os.environ["TASK3_W2_RUN_ID"]
    case_id = os.environ["TASK3_W2_CASE_ID"]
    fixture_identity = os.environ["TASK3_W2_FIXTURE_IDENTITY"]
    asset_identity = os.environ["TASK3_W2_ASSET_IDENTITY"]
    environment_identity = os.environ["TASK3_W2_ENVIRONMENT_IDENTITY"]

    actual_tree_hash, actual_file_count = source_tree_identity(project_root)
    evidence = current_import_evidence(
        import_root,
        expected_backend_sha256,
        run_id=run_id,
        fixture_identity=fixture_identity,
        asset_identity=asset_identity,
        environment_identity=environment_identity,
        case_id=case_id,
    )
    evidence["source_tree_sha256"] = actual_tree_hash
    evidence["expected_source_tree_sha256"] = expected_tree_hash
    evidence["source_file_count"] = actual_file_count
    evidence["expected_source_file_count"] = expected_file_count
    if actual_tree_hash != expected_tree_hash:
        evidence["binding_errors"].append(
            "source tree SHA256 differs from the registered W2 source"
        )
    if actual_file_count != expected_file_count:
        evidence["binding_errors"].append(
            "source tree file count differs from the registered W2 source"
        )
    if evidence["binding_errors"]:
        evidence["binding_status"] = "SOURCE_BINDING_INVALID"

    evidence["windows_version"] = platform.version()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if evidence["binding_status"] != "PASS":
        raise RuntimeError(
            "SOURCE_BINDING_INVALID: actual AnyIO import does not match registered W2 source"
        )

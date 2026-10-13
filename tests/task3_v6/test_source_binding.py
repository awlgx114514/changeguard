from __future__ import annotations

import hashlib
from pathlib import Path

import anyio
import anyio._backends._asyncio as asyncio_backend
import pytest

from benchmarks.task3_v6.source_binding import (
    current_import_evidence,
    source_tree_identity,
)


def test_source_tree_identity_ignores_generated_files(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    package_file = source_root / "anyio" / "__init__.py"
    package_file.parent.mkdir(parents=True)
    package_file.write_text("value = 1\n", encoding="utf-8")

    first_identity, first_count = source_tree_identity(source_root)

    (source_root / "anyio" / "__pycache__").mkdir()
    (source_root / "anyio" / "__pycache__" / "generated.pyc").write_bytes(b"cache")
    (source_root / ".pytest_cache").mkdir()
    (source_root / ".pytest_cache" / "generated").write_text("cache", encoding="utf-8")
    second_identity, second_count = source_tree_identity(source_root)

    assert first_identity == second_identity
    assert first_count == second_count == 1

    package_file.write_text("value = 2\n", encoding="utf-8")
    third_identity, third_count = source_tree_identity(source_root)
    assert third_identity != first_identity
    assert third_count == 1


def test_import_evidence_records_loaded_anyio_source() -> None:
    expected_root = Path(anyio.__file__).resolve().parents[1]
    backend_path = Path(asyncio_backend.__file__).resolve()
    expected_backend_hash = hashlib.sha256(backend_path.read_bytes()).hexdigest()

    evidence = current_import_evidence(
        expected_root,
        expected_backend_hash,
        run_id="source-binding-test",
        fixture_identity="fixture-test",
        asset_identity="asset-test",
        environment_identity="environment-test",
        case_id="import-evidence",
    )

    assert evidence["binding_status"] == "PASS"
    assert evidence["actual_anyio_path"] == str(Path(anyio.__file__).resolve())
    assert evidence["actual_backend_path"] == str(backend_path)
    assert evidence["actual_backend_sha256"] == expected_backend_hash
    assert evidence["expected_backend_sha256"] == expected_backend_hash
    assert evidence["run_id"] == "source-binding-test"
    assert evidence["fixture_identity"] == "fixture-test"
    assert evidence["asset_identity"] == "asset-test"
    assert evidence["environment_identity"] == "environment-test"
    assert evidence["case_id"] == "import-evidence"
    assert any(str(expected_root) == entry for entry in evidence["sys_path"])


def test_import_evidence_rejects_different_source_root() -> None:
    wrong_root = Path(__file__).resolve().parent
    backend_path = Path(asyncio_backend.__file__).resolve()
    expected_backend_hash = hashlib.sha256(backend_path.read_bytes()).hexdigest()

    evidence = current_import_evidence(
        wrong_root,
        expected_backend_hash,
        run_id="source-binding-test",
        fixture_identity="fixture-test",
        asset_identity="asset-test",
        environment_identity="environment-test",
        case_id="wrong-root",
    )

    assert evidence["binding_status"] == "SOURCE_BINDING_INVALID"
    assert evidence["actual_backend_sha256"] == expected_backend_hash

import json
import ast
from pathlib import Path

from benchmarks.task3_v6.profile_catalog import ProfileClass, load_profile_catalog
from benchmarks.task3_v6.source_binding import file_sha256, source_tree_identity


def test_known_fix_asset_is_bound_to_the_frozen_anyio_revision():
    repository = Path(__file__).resolve().parents[2]
    catalog = load_profile_catalog()
    profile = next(item for item in catalog.profiles if item.profile_id == "KNOWN_FIX")

    assert profile.profile_class is ProfileClass.CORRECT
    assert profile.registration_status == "REGISTERED"
    asset_root = repository / profile.asset_registration["path"]
    manifest_path = repository / profile.asset_registration["manifest_reference"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    tree_hash, file_count = source_tree_identity(asset_root)

    assert manifest["source_identity"] == (
        "https://github.com/agronholm/anyio@2571e5805b9986cdcbc1e155c9898188cd13d611"
    )
    assert manifest["base_revision"] == "2571e5805b9986cdcbc1e155c9898188cd13d611"
    assert manifest["base_git_tree_sha1"] == "25a969076bc3f1afdd8072b71b559127a439204f"
    assert manifest["benchmark_implementation_commit"] == (
        "b92c883a567e4b1cd7af5c8b02d9a13363713a3b"
    )
    assert manifest["profile_catalog_version"] == catalog.version
    assert manifest["profile_catalog_sha256"] == catalog.sha256
    assert manifest["asset_identity"] == "sha256:e23bc1598c4092a0ea6bdf11a511e7e596cc5afe7e4407b792e39a93e9485917"
    assert tree_hash == manifest["asset_identity"].removeprefix("sha256:")
    assert file_count == manifest["file_count"] == 115
    assert ".git" not in {path.name for path in asset_root.iterdir()}
    assert file_sha256(asset_root / "src/anyio/_backends/_asyncio.py") == (
        "2fb55aefe0c38f7f7b77d16737d1210eed4d938c30a07327be8f6616d39b8051"
    )


def test_known_fix_resource_probe_records_actual_backend_binding():
    repository = Path(__file__).resolve().parents[2]
    probe_path = (
        repository
        / "benchmarks/task3_v6/acceptance/test_known_fix_resource_probe.py"
    )
    module = ast.parse(probe_path.read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in module.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from_modules = {
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom)
    }
    loaded_attributes = {
        node.attr
        for node in ast.walk(module)
        if isinstance(node, ast.Attribute)
    }

    assert "anyio._backends._asyncio" in imported_modules
    assert "benchmarks.task3_v6.campaign" in imported_from_modules
    assert "benchmarks.task3_v6.measurement" in imported_from_modules
    assert "__file__" in loaded_attributes
    assert "actual_backend_sha256" in ast.unparse(module)
    assert "asset_manifest_sha256" in ast.unparse(module)
    assert "record_observation" in ast.unparse(module)
    assert "terminalize" in ast.unparse(module)


def test_registered_correct_alternatives_have_source_acceptance_manifests():
    repository = Path(__file__).resolve().parents[2]
    catalog = load_profile_catalog()
    correct_alternatives = {
        "CA_DIFFERENT_TRANSPORT_HELPER",
        "CA_LEGAL_CLEANUP_ORDERING",
        "CA_LATE_CACHE_UPDATE",
        "CA_ASYNC_CLOSED_STREAM_PROBE",
        "CA_BUFFERING_SPOOLING",
    }

    for profile in catalog.profiles:
        if profile.profile_id not in correct_alternatives:
            continue

        asset_root = repository / profile.asset_registration["path"]
        manifest_path = repository / profile.asset_registration["manifest_reference"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        tree_hash, file_count = source_tree_identity(asset_root)
        assert manifest["asset_id"] == profile.profile_id
        assert manifest["base_revision"] == (
            "2571e5805b9986cdcbc1e155c9898188cd13d611"
        )
        assert manifest["final_tree_hash"] == tree_hash
        assert manifest["asset_identity"] == f"sha256:{tree_hash}"
        assert manifest["fixture_identity"]
        assert manifest["file_count"] == file_count == 115
        assert manifest["source_acceptance_status"] == "PASS"
        assert manifest["benchmark_implementation_commit"] == (
            "b92c883a567e4b1cd7af5c8b02d9a13363713a3b"
        )
        assert manifest["profile_catalog_version"] == catalog.version
        assert manifest["profile_catalog_sha256"] == catalog.sha256


def test_legal_cleanup_alternative_reorders_independent_output_closures():
    repository = Path(__file__).resolve().parents[2]
    source = repository / (
        "benchmarks/task3_v6/assets/CA_LEGAL_CLEANUP_ORDERING/"
        "src/anyio/_backends/_asyncio.py"
    )
    module = ast.parse(source.read_text(encoding="utf-8"))
    process_class = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "Process"
    )
    aclose = next(
        node
        for node in process_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "aclose"
    )
    close_lines = {
        node.func.value.attr: node.lineno
        for node in ast.walk(aclose)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "aclose"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
    }

    assert close_lines["_stderr"] < close_lines["_stdout"]


def test_transport_helper_alternative_executes_direct_pipe_transport_cleanup():
    repository = Path(__file__).resolve().parents[2]
    source = repository / (
        "benchmarks/task3_v6/assets/CA_DIFFERENT_TRANSPORT_HELPER/"
        "src/anyio/_backends/_asyncio.py"
    )
    module = ast.parse(source.read_text(encoding="utf-8"))
    process_class = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "Process"
    )
    methods = {
        node.name: node
        for node in process_class.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    aclose = methods["aclose"]
    helper = methods["_close_pipe_transports"]
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_close_pipe_transports"
        for node in ast.walk(aclose)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_pipe_transport"
        for node in ast.walk(helper)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "close"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pipe"
        for node in ast.walk(helper)
    )


def test_late_cache_alternative_defers_only_the_exit_notification():
    repository = Path(__file__).resolve().parents[2]
    source = repository / (
        "benchmarks/task3_v6/assets/CA_LATE_CACHE_UPDATE/"
        "src/anyio/_backends/_asyncio.py"
    )
    module = ast.parse(source.read_text(encoding="utf-8"))
    protocol = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "_ProcessStreamProtocol"
    )
    process_exited = next(
        node
        for node in protocol.body
        if isinstance(node, ast.FunctionDef) and node.name == "process_exited"
    )
    calls = [
        node
        for node in ast.walk(process_exited)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert any(
        call.func.attr == "call_soon"
        and call.args
        and isinstance(call.args[0], ast.Attribute)
        and call.args[0].attr == "set"
        for call in calls
    )
    schedule_line = min(
        call.lineno for call in calls if call.func.attr == "call_soon"
    )
    super_line = next(
        node.lineno
        for node in ast.walk(process_exited)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "process_exited"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    )
    assert super_line < schedule_line
    process = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "Process"
    )
    returncode_property = next(
        node
        for node in process.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "returncode"
        and any(isinstance(item, ast.Name) and item.id == "property" for item in node.decorator_list)
    )
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "returncode"
        for node in ast.walk(returncode_property)
    )


def test_async_closed_stream_alternative_finishes_async_close_before_probe():
    repository = Path(__file__).resolve().parents[2]
    source = repository / (
        "benchmarks/task3_v6/assets/CA_ASYNC_CLOSED_STREAM_PROBE/"
        "src/anyio/_backends/_asyncio.py"
    )
    module = ast.parse(source.read_text(encoding="utf-8"))
    wrapper = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "StreamReaderWrapper"
    )
    aclose = next(
        node
        for node in wrapper.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "aclose"
    )
    checkpoint_line = next(
        node.lineno
        for node in ast.walk(aclose)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "checkpoint"
    )
    exception_line = next(
        node.lineno
        for node in ast.walk(aclose)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set_exception"
    )

    assert checkpoint_line < exception_line


def test_buffering_alternative_spools_pending_pipe_data_before_close():
    repository = Path(__file__).resolve().parents[2]
    source = repository / (
        "benchmarks/task3_v6/assets/CA_BUFFERING_SPOOLING/"
        "src/anyio/_backends/_asyncio.py"
    )
    module = ast.parse(source.read_text(encoding="utf-8"))
    process = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "Process"
    )
    methods = {
        node.name: node
        for node in process.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    aclose = methods["aclose"]
    spool_pending = methods["_spool_pending_output"]
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_spool_pending_output"
        for node in ast.walk(aclose)
    ) == 2
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "SpooledTemporaryFile"
        for node in ast.walk(spool_pending)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
        for node in ast.walk(spool_pending)
    )


def test_correct_alternatives_change_only_the_registered_backend_file():
    repository = Path(__file__).resolve().parents[2]
    base = repository / "benchmarks/task3_v6/assets/KNOWN_FIX"
    profile_ids = (
        "CA_DIFFERENT_TRANSPORT_HELPER",
        "CA_LEGAL_CLEANUP_ORDERING",
        "CA_LATE_CACHE_UPDATE",
        "CA_ASYNC_CLOSED_STREAM_PROBE",
        "CA_BUFFERING_SPOOLING",
    )
    generated_directories = {
        ".git",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
    }

    def source_files(root):
        files = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue

            relative = path.relative_to(root)
            if (
                path.suffix in {".pyc", ".pyo"}
                or any(part in generated_directories for part in relative.parts)
                or any(part.endswith(".egg-info") for part in relative.parts)
            ):
                continue
            files[relative.as_posix()] = file_sha256(path)
        return files

    base_files = source_files(base)

    for profile_id in profile_ids:
        source_root = repository / "benchmarks/task3_v6/assets" / profile_id
        profile_files = source_files(source_root)
        changed_paths = sorted(
            path
            for path in set(base_files) | set(profile_files)
            if base_files.get(path) != profile_files.get(path)
        )

        assert changed_paths == ["src/anyio/_backends/_asyncio.py"]


def test_w1_manifest_binds_its_single_cleanup_mutation():
    repository = Path(__file__).resolve().parents[2]
    catalog = load_profile_catalog()
    profile = next(item for item in catalog.profiles if item.profile_id == "W1")

    assert profile.profile_class is ProfileClass.REQUIRED_WRONG
    assert profile.asset_registration["manifest_reference"] is not None
    manifest_path = repository / profile.asset_registration["manifest_reference"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset_root = repository / profile.asset_registration["path"]
    tree_hash, file_count = source_tree_identity(asset_root)

    assert manifest["asset_id"] == "W1"
    assert manifest["final_tree_hash"] == tree_hash
    assert manifest["asset_identity"] == f"sha256:{tree_hash}"
    assert manifest["fixture_identity"] == f"sha256:{tree_hash}"
    assert file_count == manifest["file_count"] == 115
    assert manifest["benchmark_implementation_commit"] == (
        "b92c883a567e4b1cd7af5c8b02d9a13363713a3b"
    )
    assert manifest["benchmark_implementation_tree"] == (
        "a0bc6855a8bb97e97d1dc459d98ca1d3b6a70553"
    )
    assert manifest["profile_catalog_version"] == catalog.version
    assert manifest["profile_catalog_sha256"] == catalog.sha256
    assert manifest["source_acceptance"]["changed_source_paths"] == [
        "src/anyio/_backends/_asyncio.py"
    ]
    assert manifest["registered_lifecycle_defect"] == {
        "defect_id": "cleanup-resource-release",
        "interaction_type": "public_close",
        "required_fact": "DIRECT_CHILD_LIVE_CONFIRMED",
    }


def test_w2_manifest_binds_catalog_and_source_bound_public_suite():
    repository = Path(__file__).resolve().parents[2]
    catalog = load_profile_catalog()
    profile = next(item for item in catalog.profiles if item.profile_id == "W2")

    assert profile.profile_class is ProfileClass.REQUIRED_WRONG
    manifest_path = repository / profile.asset_registration["manifest_reference"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset_root = repository / profile.asset_registration["path"]
    tree_hash, file_count = source_tree_identity(asset_root)

    assert manifest["asset_id"] == "W2"
    assert manifest["final_tree_hash"] == tree_hash
    assert manifest["asset_identity"] == f"sha256:{tree_hash}"
    assert manifest["fixture_identity"] == f"sha256:{tree_hash}"
    assert file_count == manifest["file_count"] == 115
    assert manifest["benchmark_implementation_commit"] == (
        "b92c883a567e4b1cd7af5c8b02d9a13363713a3b"
    )
    assert manifest["benchmark_implementation_tree"] == (
        "a0bc6855a8bb97e97d1dc459d98ca1d3b6a70553"
    )
    assert manifest["profile_catalog_version"] == catalog.version
    assert manifest["profile_catalog_sha256"] == catalog.sha256
    assert manifest["source_acceptance"]["public_subprocess_suite"] == (
        "54 passed, 34 skipped, 0 failed"
    )
    assert manifest["registered_lifecycle_defect"] == {
        "defect_id": "cleanup-resource-release",
        "interaction_type": "public_close",
        "required_fact": "DIRECT_CHILD_LIVE_CONFIRMED",
    }


def test_w3_manifest_binds_only_the_pipe_release_omission():
    repository = Path(__file__).resolve().parents[2]
    catalog = load_profile_catalog()
    profile = next(item for item in catalog.profiles if item.profile_id == "W3")
    base = repository / "benchmarks/task3_v6/assets/KNOWN_FIX"
    asset_root = repository / profile.asset_registration["path"]
    manifest_path = repository / profile.asset_registration["manifest_reference"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    generated_directories = {".git", ".pytest_cache", "__pycache__", "build", "dist"}

    def source_files(root):
        return {
            path.relative_to(root).as_posix(): file_sha256(path)
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix not in {".pyc", ".pyo"}
            and not any(
                part in generated_directories
                or part.endswith(".egg-info")
                for part in path.relative_to(root).parts
            )
        }

    base_files = source_files(base)
    variant_files = source_files(asset_root)
    changed_paths = sorted(
        path
        for path in set(base_files) | set(variant_files)
        if base_files.get(path) != variant_files.get(path)
    )
    tree_hash, file_count = source_tree_identity(asset_root)

    assert profile.profile_class is ProfileClass.REQUIRED_WRONG
    assert changed_paths == ["src/anyio/_backends/_asyncio.py"]
    assert manifest["final_tree_hash"] == tree_hash
    assert manifest["asset_identity"] == f"sha256:{tree_hash}"
    assert manifest["fixture_identity"] == f"sha256:{tree_hash}"
    assert manifest["file_count"] == file_count == 115
    assert manifest["profile_catalog_sha256"] == catalog.sha256
    assert manifest["source_acceptance"]["public_subprocess_suite"] == {
        "result": "TARGET_SEMANTIC_DEFECT_ONLY",
        "passed": 52,
        "skipped": 34,
        "failed": 2,
        "expected_failures": [
            "test_close_with_stdout_blocked_subprocess[asyncio]",
            "test_close_with_stdout_blocked_subprocess[asyncio+eager]",
        ],
        "unexpected_failures": [],
    }

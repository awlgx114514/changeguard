import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import anyio
import anyio._backends._asyncio as asyncio_backend
import pytest

from benchmarks.task3_v6.campaign import (
    CampaignIdentity,
    CampaignProfileType,
    CampaignStore,
)
from benchmarks.task3_v6.measurement import (
    CompletionIdentity,
    CompletionKind,
    MeasurementProfile,
    ObservationAuthority,
    SharedCompletionRecord,
    TimedInteraction,
)
from benchmarks.task3_v6.profile_catalog import ProfileClass, load_profile_catalog
from benchmarks.task3_v6.source_binding import (
    current_import_evidence,
    file_sha256,
    source_tree_identity,
)


_RUN_COUNT = int(os.environ.get("TASK3_PROFILE_RUN_COUNT", "1"))
if not 1 <= _RUN_COUNT <= 8:
    raise RuntimeError("TASK3_PROFILE_RUN_COUNT must be between 1 and 8")


def _qpc() -> tuple[int, int]:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryPerformanceCounter.argtypes = (ctypes.POINTER(ctypes.c_longlong),)
    kernel32.QueryPerformanceCounter.restype = ctypes.c_int
    kernel32.QueryPerformanceFrequency.argtypes = (ctypes.POINTER(ctypes.c_longlong),)
    kernel32.QueryPerformanceFrequency.restype = ctypes.c_int
    ticks = ctypes.c_longlong()
    frequency = ctypes.c_longlong()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(ticks)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel32.QueryPerformanceFrequency(ctypes.byref(frequency)):
        raise ctypes.WinError(ctypes.get_last_error())
    return ticks.value, frequency.value


def _environment_details(request) -> dict[str, object]:
    plugin_names = []
    unnamed_plugin_count = 0
    for name, _plugin in request.config.pluginmanager.list_name_plugin():
        if name and not name.isdigit():
            plugin_names.append(name)
        else:
            unnamed_plugin_count += 1

    return {
        "os": platform.platform(),
        "windows_version": platform.version(),
        "machine": platform.machine(),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "dependencies": sorted(
            (distribution.metadata["Name"], distribution.version)
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        ),
        "pytest_plugins": sorted(set(plugin_names)),
        "unnamed_plugin_count": unnamed_plugin_count,
        "path": os.environ.get("PATH", ""),
        "cwd": str(Path.cwd().resolve()),
    }


def _benchmark_binding(repository: Path, manifest: dict[str, object]) -> dict[str, str]:
    actual_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    actual_tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{actual_commit}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    catalog = load_profile_catalog()
    if (
        actual_commit != manifest["benchmark_implementation_commit"]
        or actual_tree != manifest["benchmark_implementation_tree"]
        or catalog.version != manifest["profile_catalog_version"]
        or catalog.sha256 != manifest["profile_catalog_sha256"]
    ):
        raise RuntimeError("benchmark or profile-catalog binding is invalid")
    return {
        "commit": actual_commit,
        "tree": actual_tree,
        "catalog_version": catalog.version,
        "catalog_sha256": catalog.sha256,
        "catalog_file_sha256": file_sha256(
            repository / "benchmarks/task3_v6/task3_profile_catalog_v1.json"
        ),
    }


async def _run_buffering_spooling_probe(
    *,
    backend_path: Path,
    expected_backend_sha256: str,
) -> dict[str, object]:
    payload = bytes(range(256)) * 16
    child_code = f"import os; os.write(1, bytes.fromhex({payload.hex()!r}))"
    process = await anyio.open_process(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdout is None or process._stdout is None:
        raise RuntimeError("finite-output probe did not create an inherited stdout pipe")

    try:
        with anyio.fail_after(5):
            await process.wait()
        if process.returncode != 0:
            raise RuntimeError("finite-output child exited unsuccessfully")

        pending_buffer = process._stdout._stream._buffer
        with anyio.fail_after(5):
            while len(pending_buffer) != len(payload):
                await anyio.sleep(0.01)

        stream = process.stdout
        public_prefix = await stream.receive(1024)
        pending_bytes = bytes(pending_buffer)
        if public_prefix + pending_bytes != payload:
            raise RuntimeError("finite-output pipe bytes differ from the child payload")

        pipe_transport = process._transport.get_pipe_transport(1)
        if pipe_transport is None:
            raise RuntimeError("finite-output probe lost its original stdout transport")
        backend_path_before = str(Path(asyncio_backend.__file__).resolve(strict=True))
        backend_sha256_before = file_sha256(Path(backend_path_before))
        if (
            backend_path_before != str(backend_path)
            or backend_sha256_before != expected_backend_sha256
        ):
            raise RuntimeError("finite-output probe source identity is invalid before close")

        spool_writes = []
        spool_returns = []

        def capture_spool_activity(frame, event, argument):
            if (
                event == "call"
                and frame.f_globals.get("__name__") == "tempfile"
                and frame.f_code.co_name == "write"
            ):
                spool = frame.f_locals["self"]
                data = frame.f_locals.get("s")
                if isinstance(data, (bytes, bytearray, memoryview)):
                    spool_writes.append((spool, bytes(data)))
            elif (
                event == "return"
                and frame.f_code.co_name == "_spool_pending_output"
                and Path(frame.f_code.co_filename).resolve() == backend_path
            ):
                spool = frame.f_locals["spool"]
                spool_returns.append(
                    {
                        "spool": spool,
                        "returned_bytes": argument,
                        "closed": spool.closed,
                    }
                )

        previous_profile = sys.getprofile()
        sys.setprofile(capture_spool_activity)
        try:
            await process.aclose()
        finally:
            sys.setprofile(previous_profile)

        if len(spool_returns) != 1:
            raise RuntimeError("finite-output close did not perform one spool operation")
        spool_return = spool_returns[0]
        matching_writes = [
            data for spool, data in spool_writes if spool is spool_return["spool"]
        ]
        if len(matching_writes) != 1:
            raise RuntimeError("finite-output spool did not perform one identifiable write")

        try:
            with anyio.fail_after(2):
                await stream.receive()
        except anyio.ClosedResourceError:
            public_stream_after_close = "CLOSED_RESOURCE_ERROR"
        else:
            raise RuntimeError("same public stdout stream remained readable after close")

        backend_path_after = str(Path(asyncio_backend.__file__).resolve(strict=True))
        backend_sha256_after = file_sha256(Path(backend_path_after))
        pipe_transport_after = process._transport.get_pipe_transport(1)
        probe = {
            "status": "PASS",
            "expected_payload_hex": payload.hex(),
            "public_prefix_hex": public_prefix.hex(),
            "pre_close_buffer_hex": pending_bytes.hex(),
            "spool_write_hex": matching_writes[0].hex(),
            "spool_returned_bytes": spool_return["returned_bytes"],
            "spool_closed": spool_return["closed"],
            "direct_child_returncode": process.returncode,
            "public_stream_after_close": public_stream_after_close,
            "stream_identity_before": id(stream),
            "stream_identity_after": id(process.stdout),
            "pipe_transport_identity_before": id(pipe_transport),
            "pipe_transport_identity_after": (
                id(pipe_transport_after) if pipe_transport_after is not None else None
            ),
            "pipe_transport_closed_after": pipe_transport.is_closing(),
            "backend_path_before": backend_path_before,
            "backend_path_after": backend_path_after,
            "backend_sha256_before": backend_sha256_before,
            "backend_sha256_after": backend_sha256_after,
        }
        return probe
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.fail_after(5):
                await process.wait()
        if not process._transport.is_closing():
            process._transport.close()


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("prefreeze_run", range(_RUN_COUNT))
@pytest.mark.anyio
async def test_known_fix_closes_a_backpressured_child_before_returning(
    anyio_backend, prefreeze_run, request
):
    if sys.platform != "win32":
        raise RuntimeError("this native lifecycle probe requires Windows")

    source_root = Path(os.environ["TASK3_PROFILE_SOURCE_ROOT"]).resolve(strict=True)
    manifest_path = Path(os.environ["TASK3_PROFILE_MANIFEST"]).resolve(strict=True)
    evidence_root = Path(os.environ["TASK3_PROFILE_EVIDENCE_ROOT"]).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repository = Path(__file__).resolve().parents[3]
    benchmark_binding = _benchmark_binding(repository, manifest)
    catalog = load_profile_catalog()
    profile = next(
        item for item in catalog.profiles if item.profile_id == manifest["asset_id"]
    )
    if profile.profile_class is ProfileClass.EXCLUDED_LIMITATION:
        raise RuntimeError("excluded profiles cannot enter prefreeze acceptance")
    registered_defect = manifest.get("registered_lifecycle_defect")
    if (
        profile.profile_class is ProfileClass.REQUIRED_WRONG
        and not isinstance(registered_defect, dict)
    ):
        raise RuntimeError("required wrong profile lacks a registered lifecycle defect")
    if profile.profile_class is ProfileClass.CORRECT and registered_defect is not None:
        raise RuntimeError("correct profile cannot register a lifecycle defect")
    expected_asset_root = repository / profile.asset_registration["path"]
    if source_root != (expected_asset_root / "src").resolve(strict=True):
        raise RuntimeError("asset source root differs from its canonical catalog path")
    if manifest_path != (
        repository / profile.asset_registration["manifest_reference"]
    ).resolve(strict=True):
        raise RuntimeError("asset manifest differs from its canonical catalog reference")

    asset_root = source_root.parent
    actual_tree_hash, actual_file_count = source_tree_identity(asset_root)
    expected_backend_sha256 = manifest["file_sha256"][
        "src/anyio/_backends/_asyncio.py"
    ]
    environment_details = _environment_details(request)
    environment_identity = "sha256:" + hashlib.sha256(
        json.dumps(
            environment_details,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    run_id = f"{manifest['asset_id']}-{uuid.uuid4().hex}"
    campaign_id = f"task3-prefreeze-{uuid.uuid4().hex}"
    source_import_evidence = current_import_evidence(
        source_root,
        expected_backend_sha256,
        run_id=run_id,
        fixture_identity=manifest["fixture_identity"],
        asset_identity=manifest["asset_identity"],
        environment_identity=environment_identity,
        case_id="backpressured-close",
    )
    if (
        source_import_evidence["binding_status"] != "PASS"
        or actual_tree_hash != manifest["final_tree_hash"]
        or actual_file_count != manifest["file_count"]
        or file_sha256(Path(asyncio_backend.__file__).resolve(strict=True))
        != expected_backend_sha256
    ):
        raise RuntimeError("source binding does not match the registered asset")
    buffering_spooling_probe = None
    if profile.profile_id == "CA_BUFFERING_SPOOLING":
        buffering_spooling_probe = await _run_buffering_spooling_probe(
            backend_path=Path(asyncio_backend.__file__).resolve(strict=True),
            expected_backend_sha256=expected_backend_sha256,
        )

    helper_paths = (
        repository / "benchmarks/task3_v6/profile_catalog.py",
        repository / "benchmarks/task3_v6/source_binding.py",
        repository / "benchmarks/task3_v6/campaign.py",
        repository / "benchmarks/task3_v6/measurement.py",
        Path(__file__).resolve(),
    )
    helper_identity = {
        path.relative_to(repository).as_posix(): file_sha256(path)
        for path in helper_paths
    }
    database_path = evidence_root / manifest["asset_id"] / run_id / "campaign.sqlite3"
    observation_path = evidence_root / manifest["asset_id"] / run_id / "observation.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists() or observation_path.exists():
        raise RuntimeError("prefreeze evidence path already exists")

    store = CampaignStore(database_path)
    campaign_identity = CampaignIdentity(
        campaign_id=campaign_id,
        semantic_spec_identity="task3-semantics-v1",
        benchmark_contract_identity="task3-benchmark-v6",
        scoring_contract_identity="task3-scoring-v6",
        calibration_campaign_identity="task3-calibration-v6",
        deadline_formula_identity="task3-deadline-formula-v1",
        accepted_asset_identities=(manifest["asset_identity"],),
        environment_profile_identity=environment_identity,
        formal_profile_identity=(
            "task3-calibration-v6:correct-prefreeze"
            if profile.profile_class is ProfileClass.CORRECT
            else "task3-calibration-v6:wrong-prefreeze"
        ),
        profile_type=(
            CampaignProfileType.CORRECT
            if profile.profile_class is ProfileClass.CORRECT
            else CampaignProfileType.WRONG
        ),
        profile_catalog_version=catalog.version,
        profile_catalog_hash=catalog.sha256,
        profile_id=profile.profile_id,
    )
    store.register(campaign_identity)
    if registered_defect is not None:
        store.register_asset_lifecycle_defect(
            campaign_id,
            asset_identity=manifest["asset_identity"],
            defect_id=registered_defect["defect_id"],
            interaction_type=registered_defect["interaction_type"],
            required_fact=registered_defect["required_fact"],
        )
    _, qpc_frequency = _qpc()
    measurement_profile = MeasurementProfile(
        campaign_identity.formal_profile_identity,
        "QueryPerformanceCounter",
        qpc_frequency,
        12_000_000_000,
        campaign_identity.deadline_formula_identity,
    )
    store.register_measurement_profile(campaign_id, measurement_profile)
    claim = store.claim_slot(
        campaign_id=campaign_id,
        slot_id=prefreeze_run,
        run_id=run_id,
        asset_identity=manifest["asset_identity"],
        environment_identity=environment_identity,
        claim_metadata={"supervisor": "task3-v6-catalog-prefreeze"},
    )
    store.start_execution(
        claim,
        fixture_identity=manifest["fixture_identity"],
        launch_plan_identity=(
            "catalog-correct-backpressured-close-v1"
            if profile.profile_class is ProfileClass.CORRECT
            else "catalog-wrong-backpressured-close-v1"
        ),
        launch_identity=uuid.uuid4().hex,
        execution_owner_identity="supervisor-task3-v6",
    )
    interaction_id = f"backpressured-close-{prefreeze_run}"
    completion_identity = CompletionIdentity.create(
        campaign_id=campaign_id,
        slot_id=prefreeze_run,
        run_id=run_id,
        case_id="subprocess-close",
        interaction_id=interaction_id,
        interaction_type="public_close",
        interaction_generation=1,
        measurement_observation_owner_identity="supervisor-task3-v6",
        terminal_decision_identity=f"decision-{run_id}",
    )
    authority = ObservationAuthority(observation_path)
    completion = SharedCompletionRecord.create(
        completion_identity,
        measurement_profile,
        authority,
    )
    child_code = "import os; chunk=b'x'*65536; exec('while True: os.write(1, chunk)')"
    process = await anyio.open_process(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    store.bind_direct_child(claim, completion_identity, process)
    close_error = None
    try:
        with anyio.fail_after(5):
            while not process._stdout._stream._paused:
                await anyio.sleep(0.01)

        start_ticks, frequency = _qpc()
        completion.publisher_begin()
        completion_kind = CompletionKind.RETURNED
        exception_type = None
        result_identity = f"aclose-returned:{interaction_id}"
        try:
            await process.aclose()
        except BaseException as error:
            close_error = error
            completion_kind = CompletionKind.REGISTERED_EXCEPTION
            exception_type = type(error).__qualname__
            result_identity = f"aclose-raised:{interaction_id}"
        finally:
            end_ticks, _ = _qpc()
            completion.publish(
                TimedInteraction(
                    "completed",
                    result_identity,
                    completion_kind,
                    start_ticks,
                    end_ticks,
                    frequency,
                    measurement_profile.clock_identity,
                    exception_type,
                )
            )
            completion.terminal_snapshot()

        run_evidence = store.record_observation(
            claim,
            completion_identity,
            authority,
            process=process,
        )
        public_stream_probe = {"status": "STREAM_UNAVAILABLE"}
        if process.stdout is not None:
            try:
                with anyio.fail_after(2):
                    returned_bytes = await process.stdout.receive()
            except anyio.ClosedResourceError:
                public_stream_probe = {"status": "CLOSED_RESOURCE_ERROR"}
            except anyio.EndOfStream:
                public_stream_probe = {"status": "END_OF_STREAM"}
            else:
                public_stream_probe = {
                    "status": "BUFFERED_DATA_RETURNED",
                    "byte_count": len(returned_bytes),
                }
        decision = store.terminalize(claim, f"terminal-{run_id}")
        lifecycle = store.get_lifecycle_evidence(run_evidence.lifecycle_evidence_ids[0])
        direct_child_component = next(
            item
            for item in decision.component_results
            if item["component_id"].endswith("direct_child_lifecycle")
        )
        record = {
            "artifact_schema": "task3-profile-prefreeze-run-v1",
            "benchmark_implementation_commit": benchmark_binding["commit"],
            "benchmark_implementation_tree": benchmark_binding["tree"],
            "profile_catalog_version": benchmark_binding["catalog_version"],
            "profile_catalog_sha256": benchmark_binding["catalog_sha256"],
            "profile_catalog_file_sha256": benchmark_binding["catalog_file_sha256"],
            "profile_id": profile.profile_id,
            "profile_class": profile.profile_class.value,
            "campaign_id": campaign_id,
            "slot_id": prefreeze_run,
            "run_id": run_id,
            "asset_manifest_path": manifest_path.relative_to(repository).as_posix(),
            "asset_manifest_sha256": file_sha256(manifest_path),
            "source_identity": manifest.get("source_identity"),
            "base_revision": manifest.get("base_revision"),
            "mutation_delta": manifest.get("mutation_delta"),
            "registered_lifecycle_defect": registered_defect,
            "asset_identity": manifest["asset_identity"],
            "fixture_identity": manifest["fixture_identity"],
            "environment_identity": environment_identity,
            "environment_details": environment_details,
            "source_import_evidence": source_import_evidence,
            "asset_tree_sha256": actual_tree_hash,
            "asset_file_count": actual_file_count,
            "actual_backend_sha256": file_sha256(
                Path(asyncio_backend.__file__).resolve(strict=True)
            ),
            "grader_sha256": file_sha256(Path(__file__).resolve()),
            "helper_identity": helper_identity,
            "measurement_profile": asdict(measurement_profile),
            "completion_evidence": asdict(run_evidence),
            "lifecycle_evidence": asdict(lifecycle),
            "direct_child_component": direct_child_component,
            "public_stream_probe": public_stream_probe,
            "buffering_spooling_probe": buffering_spooling_probe,
            "component_results": list(decision.component_results),
            "attribution_result": decision.attribution_result,
            "fail_attribution": decision.fail_attribution,
            "terminal_decision": asdict(decision),
            "task_verdict": decision.verdict.value,
            "terminal_decision_identity": decision.terminal_decision_identity,
            "evidence_ids": list(decision.evidence_ids),
            "raw_wait_result": lifecycle.os_query_raw_result,
            "process_returncode": process.returncode,
            "close_error": None if close_error is None else repr(close_error),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        artifact_path = database_path.parent / f"{run_id}.json"
        with artifact_path.open("x", encoding="utf-8", newline="\n") as artifact:
            json.dump(record, artifact, indent=2, sort_keys=True)
            artifact.write("\n")
            artifact.flush()
            os.fsync(artifact.fileno())
        print(
            "PROFILE_RUN_EVIDENCE="
            + json.dumps(
                {
                    "profile_id": profile.profile_id,
                    "run_id": run_id,
                    "artifact_path": str(artifact_path),
                    "raw_wait_result": lifecycle.os_query_raw_result,
                    "component_verdict": direct_child_component["verdict"],
                    "task_verdict": decision.verdict.value,
                    "source_binding": source_import_evidence["binding_status"],
                    "benchmark_commit": benchmark_binding["commit"],
                    "catalog_sha256": benchmark_binding["catalog_sha256"],
                },
                sort_keys=True,
            )
        )
        if profile.profile_class is ProfileClass.CORRECT:
            assert close_error is None
            assert lifecycle.os_query_raw_result == 0
            assert direct_child_component["verdict"] == "PASS"
            assert decision.verdict.value == "PASS"
        else:
            assert lifecycle.os_query_raw_result == 258
            assert direct_child_component["verdict"] == "FAIL"
            assert decision.verdict.value == "FAIL"
            assert decision.attribution_result["valid"] is True
            assert decision.fail_attribution == "confirmed_rejection"
        if profile.profile_id == "CA_ASYNC_CLOSED_STREAM_PROBE":
            assert public_stream_probe["status"] == "CLOSED_RESOURCE_ERROR"
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.fail_after(5):
                await process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    await stream.aclose()
            process._transport.close()
        completion.close()

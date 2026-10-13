from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


_repository = Path(__file__).resolve().parents[3]
_asset_source = _repository / "benchmarks" / "task3_v6" / "assets" / "W2" / "wrong-variant" / "src"
sys.path.insert(0, str(_repository))
sys.path.insert(0, str(_asset_source))

import anyio
import anyio._backends._asyncio as asyncio_backend

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
from benchmarks.task3_v6.source_binding import (
    current_import_evidence,
    file_sha256,
    source_tree_identity,
)
from benchmarks.task3_v6.profile_catalog import load_profile_catalog


class _FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))


def _qpc() -> tuple[int, int]:
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


def _asset_manifest() -> dict[str, object]:
    manifest_path = Path(__file__).with_name("w2-asset-manifest-fidelity-v2.json")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _asset_identity() -> str:
    manifest = _asset_manifest()
    actual_tree_hash, actual_file_count = source_tree_identity(_asset_source.parent)
    expected_backend_sha256 = manifest["file_sha256"][
        "src/anyio/_backends/_asyncio.py"
    ]
    actual_backend_path = Path(asyncio_backend.__file__).resolve(strict=True)
    expected_backend_path = (
        _asset_source / "anyio" / "_backends" / "_asyncio.py"
    ).resolve(strict=True)
    actual_anyio_path = Path(anyio.__file__).resolve(strict=True)
    expected_anyio_path = (_asset_source / "anyio" / "__init__.py").resolve(strict=True)
    if actual_tree_hash != manifest["final_tree_hash"]:
        raise ValueError("W2 source tree does not match its registered manifest")
    if actual_file_count != manifest["file_count"]:
        raise ValueError("W2 source file count does not match its registered manifest")
    if actual_backend_path != expected_backend_path:
        raise ValueError("the asyncio backend was imported from outside the W2 source tree")
    if actual_anyio_path != expected_anyio_path:
        raise ValueError("AnyIO was imported from outside the W2 source tree")
    if file_sha256(actual_backend_path) != expected_backend_sha256:
        raise ValueError("the imported asyncio backend differs from the registered W2 source")
    if manifest["fixture_identity"] != manifest["asset_identity"]:
        raise ValueError("W2 fixture and asset identities do not match")

    return str(manifest["asset_identity"])


def _registered_defect(asset_identity: str) -> dict[str, str]:
    path = Path(__file__).with_name("w2_registered_lifecycle_defect.json")
    defect = json.loads(path.read_text(encoding="utf-8"))
    if defect["asset_identity"] != asset_identity:
        raise ValueError("registered lifecycle defect does not bind to the reconstructed asset")
    return defect


def _environment_identity() -> str:
    details = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "windows_build": platform.version(),
    }
    encoded = json.dumps(details, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _run_one(
    database_path: Path,
    slot_id: int,
    asset_identity: str,
    defect: dict[str, str],
) -> dict[str, object]:
    run_id = f"w2-v6-{slot_id}-{uuid.uuid4().hex}"
    campaign_id = f"w2-v6-{uuid.uuid4().hex}"
    environment_identity = _environment_identity()
    manifest = _asset_manifest()
    profile_catalog = load_profile_catalog()
    expected_backend_sha256 = manifest["file_sha256"][
        "src/anyio/_backends/_asyncio.py"
    ]
    source_tree_sha256, source_file_count = source_tree_identity(_asset_source.parent)
    source_import_evidence = current_import_evidence(
        _asset_source,
        expected_backend_sha256,
        run_id=run_id,
        fixture_identity=asset_identity,
        asset_identity=asset_identity,
        environment_identity=environment_identity,
        case_id=f"public-close-{slot_id}",
    )
    source_import_evidence["source_tree_sha256"] = source_tree_sha256
    source_import_evidence["expected_source_tree_sha256"] = manifest["final_tree_hash"]
    source_import_evidence["source_file_count"] = source_file_count
    source_import_evidence["expected_source_file_count"] = manifest["file_count"]
    if (
        source_import_evidence["binding_status"] != "PASS"
        or source_tree_sha256 != manifest["final_tree_hash"]
        or source_file_count != manifest["file_count"]
    ):
        raise ValueError("W2 execution source binding is invalid")
    store = CampaignStore(database_path)
    campaign_identity = CampaignIdentity(
        campaign_id=campaign_id,
        semantic_spec_identity="task3-semantics-v1",
        benchmark_contract_identity="task3-benchmark-v6",
        scoring_contract_identity="task3-scoring-v6",
        calibration_campaign_identity="task3-calibration-v6",
        deadline_formula_identity="task3-deadline-formula-v1",
        accepted_asset_identities=(asset_identity,),
        environment_profile_identity=environment_identity,
        formal_profile_identity="task3-calibration-v6:wrong-profile",
        profile_type=CampaignProfileType.WRONG,
        profile_catalog_version=profile_catalog.version,
        profile_catalog_hash=profile_catalog.sha256,
        profile_id="W2",
    )
    store.register(campaign_identity)
    profile = MeasurementProfile(
        campaign_identity.formal_profile_identity,
        "QueryPerformanceCounter",
        _qpc()[1],
        12_000_000_000,
        campaign_identity.deadline_formula_identity,
    )
    store.register_measurement_profile(campaign_id, profile)
    store.register_asset_lifecycle_defect(
        campaign_id,
        asset_identity=asset_identity,
        defect_id=defect["defect_id"],
        interaction_type=defect["interaction_type"],
        required_fact=defect["required_lifecycle_fact"],
    )
    claim = store.claim_slot(
        campaign_id=campaign_id,
        slot_id=slot_id,
        run_id=run_id,
        asset_identity=asset_identity,
        environment_identity=environment_identity,
        claim_metadata={"supervisor": "task3-v6-w2-acceptance"},
    )
    store.start_execution(
        claim,
        fixture_identity=asset_identity,
        launch_plan_identity="w2-public-close-probe-v1",
        launch_identity=uuid.uuid4().hex,
        execution_owner_identity="supervisor-task3-v6",
    )
    interaction_id = f"public-close-{slot_id}"
    completion_identity = CompletionIdentity.create(
        campaign_id=campaign_id,
        slot_id=slot_id,
        run_id=run_id,
        case_id="subprocess-close",
        interaction_id=interaction_id,
        interaction_type="public_close",
        interaction_generation=1,
        measurement_observation_owner_identity="supervisor-task3-v6",
        terminal_decision_identity=f"decision-{run_id}",
    )
    authority = ObservationAuthority(database_path.with_name(f"{run_id}-observation.sqlite3"))
    completion = SharedCompletionRecord.create(completion_identity, profile, authority)
    child_code = "\n".join(
        (
            "import os",
            "chunk = b'x' * 65536",
            "while True:",
            "    os.write(1, chunk)",
        )
    )
    process = await anyio.open_process(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    store.bind_direct_child(claim, completion_identity, process)
    with anyio.fail_after(5):
        while not any(
            stream is not None and stream._stream._paused
            for stream in (process._stdout, process._stderr)
        ):
            await anyio.sleep(0.01)
    start_ticks, frequency = _qpc()
    completion.publisher_begin()
    completion_kind = CompletionKind.RETURNED
    exception_type = None
    result_identity = f"aclose-returned:{interaction_id}"
    try:
        await process.aclose()
    except BaseException as error:
        completion_kind = CompletionKind.REGISTERED_EXCEPTION
        exception_type = type(error).__qualname__
        result_identity = f"aclose-raised:{interaction_id}"
        raise
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
                profile.clock_identity,
                exception_type,
            )
        )
        completion.terminal_snapshot()
    evidence = store.record_observation(
        claim,
        completion_identity,
        authority,
        process=process,
    )
    decision = store.terminalize(claim, f"terminal-{run_id}")
    lifecycle_id = evidence.lifecycle_evidence_ids[0]
    lifecycle = store.get_lifecycle_evidence(lifecycle_id)
    child_returncode_after_observation = process.returncode
    if child_returncode_after_observation is None:
        process.kill()
        with anyio.move_on_after(5):
            await process.wait()
    completion.close()
    return {
        "run_id": run_id,
        "profile_id": campaign_identity.profile_id,
        "profile_catalog_version": campaign_identity.profile_catalog_version,
        "profile_catalog_hash": campaign_identity.profile_catalog_hash,
        "asset_identity": asset_identity,
        "fixture_identity": asset_identity,
        "source_import_evidence": source_import_evidence,
        "environment_identity": environment_identity,
        "lifecycle_evidence_id": lifecycle_id,
        "raw_wait_result": lifecycle.os_query_raw_result,
        "lifecycle_fact": lifecycle.lifecycle_fact.value,
        "direct_child_leak_verdict": next(
            item["verdict"]
            for item in decision.component_results
            if item["component_id"].endswith("direct_child_lifecycle")
        ),
        "task_verdict": decision.verdict.value,
        "rejection_attribution_valid": decision.attribution_result.get("valid", False),
        "confirmed_rejection": decision.fail_attribution == "confirmed_rejection",
        "component_results": list(decision.component_results),
        "attribution_result": decision.attribution_result,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_path": str(database_path),
    }


async def _run_many(output_dir: Path, count: int) -> list[dict[str, object]]:
    asset_identity = _asset_identity()
    defect = _registered_defect(asset_identity)
    results = []
    for index in range(count):
        database_path = output_dir / f"run-{index + 1:02d}-{uuid.uuid4().hex}.sqlite3"
        database_path.parent.mkdir(parents=True, exist_ok=True)
        results.append(await _run_one(database_path, index, asset_identity, defect))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.count < 1 or args.count > 8:
        parser.error("count must be between 1 and 8")
    results = anyio.run(_run_many, args.output.resolve(), args.count, backend="asyncio")
    args.output.mkdir(parents=True, exist_ok=True)
    profile_catalog = load_profile_catalog()
    summary = {
        "run_count": len(results),
        "profile_id": "W2",
        "profile_catalog_version": profile_catalog.version,
        "profile_catalog_hash": profile_catalog.sha256,
        "source_identity": _asset_manifest()["source_identity"],
        "source_tree_sha256": _asset_manifest()["final_tree_hash"],
        "fixture_identity": _asset_manifest()["fixture_identity"],
        "asset_identity": _asset_manifest()["asset_identity"],
        "source_binding_status": "PASS",
        "results": results,
        "confirmed_rejection_count": sum(result["confirmed_rejection"] is True for result in results),
        "pass_count": sum(result["task_verdict"] == "PASS" for result in results),
        "invalid_count": sum(result["task_verdict"] == "INVALID" for result in results),
        "not_proven_count": sum(result["task_verdict"] == "NOT_PROVEN" for result in results),
    }
    output_file = args.output / "summary.json"
    output_file.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if all(result["confirmed_rejection"] is True for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

import asyncio
import json
import os
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anyio
import anyio._backends._asyncio as asyncio_backend
import pytest

from benchmarks.task3_v6.measurement import StableChildProcessHandle
from benchmarks.task3_v6.source_binding import file_sha256, source_tree_identity


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.anyio
async def test_w3_pending_close_has_source_bound_live_child_evidence(anyio_backend):
    if sys.platform != "win32":
        raise RuntimeError("W3 native lifecycle diagnostic requires Windows")

    expectation = os.environ["TASK3_W3_EXPECTATION"]
    if expectation not in {"REFERENCE_FIXED", "W3"}:
        raise RuntimeError("unknown W3 diagnostic expectation")
    source_root = Path(os.environ["TASK3_PROFILE_SOURCE_ROOT"]).resolve(strict=True)
    expected_tree_sha256 = os.environ["TASK3_W3_EXPECTED_TREE_SHA256"]
    expected_backend_sha256 = os.environ["TASK3_W3_EXPECTED_BACKEND_SHA256"]
    expected_backend_path = str(
        (source_root / "anyio" / "_backends" / "_asyncio.py").resolve(strict=True)
    )
    actual_backend_path = str(Path(asyncio_backend.__file__).resolve(strict=True))
    actual_backend_sha256 = file_sha256(Path(actual_backend_path))
    actual_tree_sha256, source_file_count = source_tree_identity(source_root.parent)
    if (
        actual_backend_path != expected_backend_path
        or actual_backend_sha256 != expected_backend_sha256
        or actual_tree_sha256 != expected_tree_sha256
    ):
        raise RuntimeError("W3 diagnostic imported source differs from its registered tree")

    run_id = uuid.uuid4().hex
    campaign_id = f"task3-w3-diagnostic-{run_id}"
    child_chunk_bytes = 65536
    child_output_target_bytes = 33554432
    child_code = (
        f"import os; chunk=b'x'*{child_chunk_bytes}; "
        f"remaining={child_output_target_bytes}; "
        "exec('while remaining:\\n written=os.write(1, chunk[:remaining])\\n remaining-=written')"
    )
    process = await anyio.open_process(
        [sys.executable, "-c", child_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None or process._stdout is None:
        raise RuntimeError("W3 diagnostic did not create both redirected output pipes")

    stable_child = StableChildProcessHandle(process)
    close_task = None
    artifact_path = Path(os.environ["TASK3_W3_DIAGNOSTIC_OUTPUT"]).resolve()
    try:
        with anyio.fail_after(5):
            while not process._stdout._stream._paused:
                await anyio.lowlevel.checkpoint()

        stdout_pipe_before = process._transport.get_pipe_transport(1)
        if stdout_pipe_before is None:
            raise RuntimeError("W3 diagnostic lost its original inherited stdout pipe")
        if process.returncode is not None:
            raise RuntimeError("finite output child exited before reaching backpressure")

        close_task = asyncio.create_task(process.aclose())
        with anyio.fail_after(5):
            while not isinstance(
                process._stdout._stream._exception,
                anyio.ClosedResourceError,
            ) or not isinstance(
                process._stderr._stream._exception,
                anyio.ClosedResourceError,
            ):
                await anyio.lowlevel.checkpoint()
        if expectation == "REFERENCE_FIXED":
            with anyio.fail_after(5):
                await close_task
        else:
            await anyio.lowlevel.checkpoint()

        wrapper_close_status = "CLOSED_RESOURCE_ERROR"
        try:
            await process.stderr.receive()
        except anyio.ClosedResourceError:
            pass
        else:
            wrapper_close_status = "PUBLIC_READ_REMAINED_OPEN"

        stdout_pipe_after = process._transport.get_pipe_transport(1)
        pipe_transport_closed = stdout_pipe_before.is_closing()
        operation_pending = not close_task.done()
        direct_child_wait_result = int(
            stable_child._kernel32.WaitForSingleObject(stable_child.handle, 0)
        )
        backend_path_after = str(Path(asyncio_backend.__file__).resolve(strict=True))
        backend_sha256_after = file_sha256(Path(backend_path_after))
        record = {
            "artifact_schema": "task3-w3-pending-close-diagnostic-v1",
            "scoring_eligible": False,
            "expectation": expectation,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "child_program": child_code,
            "child_chunk_bytes": child_chunk_bytes,
            "child_output_target_bytes": child_output_target_bytes,
            "benchmark_implementation_commit": "b92c883a567e4b1cd7af5c8b02d9a13363713a3b",
            "benchmark_implementation_tree": "a0bc6855a8bb97e97d1dc459d98ca1d3b6a70553",
            "asset_identity": f"sha256:{actual_tree_sha256}",
            "source_file_count": source_file_count,
            "python_executable": str(Path(sys.executable).resolve()),
            "python_version": sys.version,
            "os": platform.platform(),
            "source_import_path_before": actual_backend_path,
            "source_import_sha256_before": actual_backend_sha256,
            "source_import_path_after": backend_path_after,
            "source_import_sha256_after": backend_sha256_after,
            "diagnostic_helper_path": str(Path(__file__).resolve()),
            "diagnostic_helper_sha256": file_sha256(Path(__file__).resolve()),
            "stdout_pipe_transport_identity_before": id(stdout_pipe_before),
            "stdout_pipe_transport_identity_after": (
                id(stdout_pipe_after) if stdout_pipe_after is not None else None
            ),
            "stdout_pipe_transport_closed_at_observation": pipe_transport_closed,
            "reader_backpressure_paused": process._stdout._stream._paused,
            "reader_buffered_bytes": len(process._stdout._stream._buffer),
            "public_wrapper_close_status": wrapper_close_status,
            "aclose_pending_at_observation": operation_pending,
            "aclose_task_done_at_observation": close_task.done(),
            "direct_child_pid": stable_child.pid,
            "direct_child_creation_identity": stable_child.creation_identity,
            "stable_handle_binding_identity": stable_child.stable_handle_binding_identity,
            "wait_for_single_object_raw_result": direct_child_wait_result,
            "process_returncode_at_observation": process.returncode,
            "observation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        with artifact_path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(record, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        print("W3_DIAGNOSTIC_EVIDENCE=" + json.dumps(record, sort_keys=True))

        if expectation == "REFERENCE_FIXED":
            if direct_child_wait_result != 0:
                raise AssertionError("reference-fixed child was not terminal after close")
            if not pipe_transport_closed or operation_pending:
                raise AssertionError("reference-fixed close did not release the pipe and return")
        else:
            if direct_child_wait_result != 258:
                raise AssertionError("W3 did not preserve a live direct child at observation")
            if not operation_pending or pipe_transport_closed:
                raise AssertionError("W3 close was not pending with the original pipe open")
            if wrapper_close_status != "CLOSED_RESOURCE_ERROR":
                raise AssertionError("W3 public wrapper did not close before the live-child query")
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.fail_after(5):
                await process.wait()
        if close_task is not None and not close_task.done():
            with anyio.fail_after(5):
                await close_task
        if not process._transport.is_closing():
            process._transport.close()
        stable_child.close()

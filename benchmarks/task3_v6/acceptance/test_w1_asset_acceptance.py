from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import anyio
import pytest


def test_acceptance_imports_the_reconstructed_anyio_source():
    asset_source = (
        Path(__file__).resolve().parents[1] / "assets" / "W1" / "reference-fixed" / "src"
    ).resolve()
    imported_source = Path(anyio.__file__).resolve()
    assert asset_source in imported_source.parents


class _FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))


def _stable_process_handle(pid: int) -> tuple[int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    )
    kernel32.GetProcessTimes.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x00100000 | 0x00001000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    created = _FileTime()
    exited = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(handle)
        raise error
    creation_identity = (created.high << 32) | created.low
    return handle, creation_identity


@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_w1_aclose_leaves_backpressured_direct_child_live(tmp_path, anyio_backend):
    marker = tmp_path / "writer-started.txt"
    child_code = (
        "import os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "chunk=b'x'*65536; "
        "exec('while True: os.write(1, chunk)')"
    )
    process = await anyio.open_process(
        [sys.executable, "-c", child_code, str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    handle = None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    try:
        with anyio.fail_after(5):
            while not marker.is_file():
                await anyio.sleep(0.01)
        handle, creation_identity = _stable_process_handle(process.pid)
        await process.aclose()
        raw_wait_result = kernel32.WaitForSingleObject(handle, 0)
        assert process.returncode is None
        assert raw_wait_result == 258
        assert creation_identity > 0
    finally:
        if process.returncode is None:
            process.kill()
            with anyio.move_on_after(5):
                await process.wait()
        if handle:
            kernel32.CloseHandle(handle)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                await stream.aclose()

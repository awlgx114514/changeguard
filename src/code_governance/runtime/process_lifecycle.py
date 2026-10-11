"""Runtime-owned subprocess lifecycle primitives.

This module deliberately owns only one process tree at a time.  It is used
by bounded read-only tools and is also the source of the Windows Job Object
implementation used by the Codex adapter.
"""

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import subprocess
from threading import Thread
from time import monotonic
from typing import Callable, Optional, Sequence


_ROOT_TERMINATION_GRACE_SECONDS = 0.25
_PIPE_CLEANUP_SECONDS = 2.0
_FINAL_WAIT_SECONDS = 1.0
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1


class ProcessLifecycleError(RuntimeError):
    """The Runtime could not prove that an owned process became quiescent."""


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    quiescent: bool
    stdout_omitted: bool = False
    stderr_omitted: bool = False


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("this_period_total_user_time", ctypes.c_longlong),
        ("this_period_total_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


class _NoopProcessTree:
    def terminate(self) -> None:
        return None

    def quiescent(self) -> bool:
        return True

    def close(self) -> None:
        return None


class _WindowsProcessTree:
    """Owns a Windows Job Object so descendants cannot outlive the owner."""

    def __init__(self, process):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.QueryInformationJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = _JobObjectExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                self._handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(self._handle, process._handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            kernel32.CloseHandle(self._handle)
            self._handle = None
            raise

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def quiescent(self) -> bool:
        if not self._handle:
            return True
        info = _JobObjectBasicAccountingInformation()
        returned = ctypes.c_uint32()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active_processes == 0

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _create_process_tree(process):
    if os.name != "nt":
        return _NoopProcessTree()
    return _WindowsProcessTree(process)


class _BoundedCollector:
    def __init__(self, stream, max_bytes: int):
        self._stream = stream
        self._max_bytes = max(1, int(max_bytes))
        self._chunks = []
        self._size = 0
        self.omitted = False
        self.error = None

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    return
                remaining = self._max_bytes - self._size
                if remaining > 0:
                    self._chunks.append(chunk[:remaining])
                    self._size += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    self.omitted = True
        except Exception as error:
            self.error = error

    def value(self) -> bytes:
        return b"".join(self._chunks)


class OwnedProcess:
    """Run one subprocess while owning termination and final quiescence."""

    @classmethod
    def run(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        input_text: Optional[str] = None,
        max_output_bytes: int = 256 * 1024,
        event_callback: Optional[Callable[[str], None]] = None,
    ) -> ProcessOutcome:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process_tree = _create_process_tree(process)
        stdout_collector = _BoundedCollector(process.stdout, max_output_bytes)
        stderr_collector = _BoundedCollector(process.stderr, max_output_bytes)
        stdout_thread = Thread(target=stdout_collector.run, name="changeguard-stdout")
        stderr_thread = Thread(target=stderr_collector.run, name="changeguard-stderr")
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            if input_text is not None:
                try:
                    process.stdin.write(input_text.encode("utf-8"))
                except (BrokenPipeError, OSError):
                    pass
            process.stdin.close()
            deadline = monotonic() + timeout_seconds
            while process.poll() is None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    timed_out = True
                    if event_callback is not None:
                        event_callback("timeout_requested")
                        event_callback("termination_started")
                    _terminate_owned(process, process_tree)
                    break
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if event_callback is not None:
                        event_callback("timeout_requested")
                        event_callback("termination_started")
                    _terminate_owned(process, process_tree)
                    break
            if process.poll() is None:
                raise ProcessLifecycleError("owned process did not stop")
            if timed_out:
                _wait_for_quiescence(process_tree, timeout=_FINAL_WAIT_SECONDS)
            else:
                _wait_for_quiescence(process_tree, timeout=0.0)
            if event_callback is not None:
                event_callback("quiescent")
            stdout_thread.join(timeout=_PIPE_CLEANUP_SECONDS)
            stderr_thread.join(timeout=_PIPE_CLEANUP_SECONDS)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                raise ProcessLifecycleError("process output pipes did not quiesce")
            if stdout_collector.error or stderr_collector.error:
                raise ProcessLifecycleError("process output collection failed")
            return ProcessOutcome(
                returncode=process.returncode,
                stdout=_decode_stream(stdout_collector.value()),
                stderr=_decode_stream(stderr_collector.value()),
                timed_out=timed_out,
                quiescent=True,
                stdout_omitted=stdout_collector.omitted,
                stderr_omitted=stderr_collector.omitted,
            )
        finally:
            process_tree.close()


def _terminate_owned(process, process_tree) -> None:
    if process.poll() is None:
        if os.name == "nt":
            _terminate_windows_tree(process.pid)
        try:
            process.terminate()
            process.wait(timeout=_ROOT_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    process_tree.terminate()
    try:
        process.wait(timeout=_PIPE_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        process_tree.terminate()
        try:
            process.wait(timeout=_FINAL_WAIT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise ProcessLifecycleError("owned process termination timed out") from error


def _terminate_windows_tree(pid: int) -> None:
    """Use the native Windows process-tree terminator to close escaped descendants."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=_PIPE_CLEANUP_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcessLifecycleError("Windows process-tree termination failed") from error


def _wait_for_quiescence(process_tree, timeout: float) -> None:
    deadline = monotonic() + max(0.0, timeout)
    while True:
        if process_tree.quiescent():
            return
        if monotonic() >= deadline:
            raise ProcessLifecycleError("owned process descendants did not quiesce")


def _decode_stream(value: bytes) -> str:
    """Match subprocess.run(text=True) newline normalization."""
    return value.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

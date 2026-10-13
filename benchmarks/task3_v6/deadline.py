from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass


QUANTUM_NS = 1_000_000
TIMEOUT_PARAMETER_UNIT_MS = 1
MAX_FINITE_TIMEOUT_MS = 0xFFFFFFFE
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF


def _deadline_ns(maximum_observed_ns: int) -> int:
    if maximum_observed_ns < 0:
        raise ValueError("maximum_observed_ns must be non-negative")
    scaled = 2 * max(maximum_observed_ns, QUANTUM_NS)
    return ((scaled + QUANTUM_NS - 1) // QUANTUM_NS) * QUANTUM_NS


def probe_deadline_ns(p_max_ns: int) -> int:
    return _deadline_ns(p_max_ns)


def eof_deadline_ns(e_max_ns: int) -> int:
    return _deadline_ns(e_max_ns)


def wait_timeout_ms(budget_ns: int) -> int:
    if type(budget_ns) is not int or budget_ns < QUANTUM_NS or budget_ns % QUANTUM_NS:
        raise ValueError("budget_ns must be a positive whole-millisecond deadline")
    timeout_ms = budget_ns // QUANTUM_NS
    if timeout_ms > MAX_FINITE_TIMEOUT_MS:
        raise ValueError("deadline exceeds the finite Win32 wait range")
    return timeout_ms


@dataclass(frozen=True)
class DeadlineWaitResult:
    requested_timeout_ns: int
    converted_timeout_ms: int
    start_ticks: int
    end_ticks: int
    frequency: int
    wait_result: int
    logical_deadline: int
    actual_elapsed_ns: int
    outcome: str
    last_error: int | None


def classify_wait_result(
    wait_result: int,
    *,
    start_ticks: int,
    end_ticks: int,
    frequency: int,
    requested_timeout_ns: int,
) -> str:
    if end_ticks < start_ticks or frequency <= 0 or requested_timeout_ns <= 0:
        raise ValueError("invalid deadline timing values")
    if wait_result == WAIT_FAILED:
        return "WAIT_FAILED"

    elapsed_ticks = end_ticks - start_ticks
    before_deadline = (
        elapsed_ticks * 1_000_000_000
        < requested_timeout_ns * frequency
    )
    if wait_result == WAIT_OBJECT_0:
        return (
            "COMPLETED_BEFORE_DEADLINE"
            if before_deadline
            else "COMPLETED_AFTER_DEADLINE"
        )
    if wait_result == WAIT_TIMEOUT:
        return "TIMEOUT_BEFORE_DEADLINE" if before_deadline else "TIMED_OUT"
    raise ValueError(f"unexpected WaitForSingleObject result: {wait_result}")


def wait_for_single_object(handle: int | ctypes.c_void_p, timeout_ns: int) -> DeadlineWaitResult:
    if os.name != "nt":
        raise OSError("Task #3 v6 deadline wait requires Windows")
    timeout_ms = wait_timeout_ms(timeout_ns)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryPerformanceFrequency.argtypes = (
        ctypes.POINTER(ctypes.c_longlong),
    )
    kernel32.QueryPerformanceFrequency.restype = ctypes.c_int
    kernel32.QueryPerformanceCounter.argtypes = (
        ctypes.POINTER(ctypes.c_longlong),
    )
    kernel32.QueryPerformanceCounter.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32

    frequency_value = ctypes.c_longlong()
    if not kernel32.QueryPerformanceFrequency(ctypes.byref(frequency_value)):
        raise ctypes.WinError(ctypes.get_last_error())
    frequency = frequency_value.value
    if frequency <= 0:
        raise OSError("QueryPerformanceFrequency returned a non-positive value")

    start_value = ctypes.c_longlong()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(start_value)):
        raise ctypes.WinError(ctypes.get_last_error())
    start_ticks = start_value.value
    native_handle = handle if isinstance(handle, ctypes.c_void_p) else ctypes.c_void_p(handle)
    wait_result = int(kernel32.WaitForSingleObject(native_handle, timeout_ms))
    wait_error = ctypes.get_last_error() if wait_result == WAIT_FAILED else None
    end_value = ctypes.c_longlong()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(end_value)):
        raise ctypes.WinError(ctypes.get_last_error())
    end_ticks = end_value.value
    logical_duration_ticks = (
        timeout_ns * frequency + 1_000_000_000 - 1
    ) // 1_000_000_000
    logical_deadline = start_ticks + logical_duration_ticks
    actual_elapsed_ns = (end_ticks - start_ticks) * 1_000_000_000 // frequency
    outcome = classify_wait_result(
        wait_result,
        start_ticks=start_ticks,
        end_ticks=end_ticks,
        frequency=frequency,
        requested_timeout_ns=timeout_ns,
    )
    return DeadlineWaitResult(
        requested_timeout_ns=timeout_ns,
        converted_timeout_ms=timeout_ms,
        start_ticks=start_ticks,
        end_ticks=end_ticks,
        frequency=frequency,
        wait_result=wait_result,
        logical_deadline=logical_deadline,
        actual_elapsed_ns=actual_elapsed_ns,
        outcome=outcome,
        last_error=wait_error,
    )

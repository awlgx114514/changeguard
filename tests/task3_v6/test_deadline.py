from __future__ import annotations

import ctypes
import os

import pytest

from benchmarks.task3_v6.deadline import (
    QUANTUM_NS,
    eof_deadline_ns,
    probe_deadline_ns,
    wait_timeout_ms,
    wait_for_single_object,
)


@pytest.mark.parametrize(
    ("observed", "expected"),
    (
        (0, 2_000_000),
        (1_000_000, 2_000_000),
        (1_000_001, 3_000_000),
        (1_500_000, 3_000_000),
    ),
)
def test_frozen_deadline_formula_uses_integer_ceiling(observed, expected):
    assert probe_deadline_ns(observed) == expected
    assert eof_deadline_ns(observed) == expected
    assert expected % QUANTUM_NS == 0


def test_win32_timeout_uses_milliseconds_and_rejects_infinite_sentinel():
    assert wait_timeout_ms(2_000_000) == 2
    assert wait_timeout_ms(0xFFFFFFFE * QUANTUM_NS) == 0xFFFFFFFE
    with pytest.raises(ValueError, match="whole-millisecond"):
        wait_timeout_ms(1_000_001)
    with pytest.raises(ValueError, match="finite Win32 wait range"):
        wait_timeout_ms(0xFFFFFFFF * QUANTUM_NS)


def test_deadline_formula_rejects_negative_measurements():
    with pytest.raises(ValueError, match="non-negative"):
        probe_deadline_ns(-1)


@pytest.mark.skipif(os.name != "nt", reason="Win32 deadline wrapper")
def test_native_deadline_wait_records_signal_before_deadline():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_wchar_p,
    )
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
    kernel32.SetEvent.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateEventW(None, True, True, None)
    assert handle
    try:
        result = wait_for_single_object(handle, 10_000_000)
        assert result.outcome == "COMPLETED_BEFORE_DEADLINE"
        assert result.wait_result == 0
        assert result.requested_timeout_ns == 10_000_000
        assert result.converted_timeout_ms == 10
        assert result.end_ticks >= result.start_ticks
        assert result.actual_elapsed_ns >= 0
    finally:
        assert kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Win32 deadline wrapper")
def test_native_deadline_wait_records_timeout_without_margin():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_wchar_p,
    )
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateEventW(None, True, False, None)
    assert handle
    try:
        result = wait_for_single_object(handle, 1_000_000)
        assert result.wait_result == 258
        assert result.outcome in {
            "TIMEOUT_BEFORE_DEADLINE",
            "TIMED_OUT",
        }
        assert result.converted_timeout_ms == 1
        assert result.logical_deadline >= result.start_ticks
        assert result.actual_elapsed_ns >= 0
    finally:
        assert kernel32.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Win32 deadline wrapper")
def test_native_deadline_wait_preserves_wait_failed():
    result = wait_for_single_object(ctypes.c_void_p(0x1234), 1_000_000)
    assert result.wait_result == 0xFFFFFFFF
    assert result.outcome == "WAIT_FAILED"
    assert result.last_error != 0


def test_deadline_wait_classifies_completion_after_logical_deadline():
    from benchmarks.task3_v6.deadline import classify_wait_result

    assert classify_wait_result(
        0,
        start_ticks=100,
        end_ticks=111,
        frequency=1_000_000_000,
        requested_timeout_ns=10,
    ) == "COMPLETED_AFTER_DEADLINE"


def test_deadline_wait_classifies_timeout_returned_before_logical_deadline():
    from benchmarks.task3_v6.deadline import classify_wait_result

    assert classify_wait_result(
        258,
        start_ticks=100,
        end_ticks=105,
        frequency=1_000_000_000,
        requested_timeout_ns=10,
    ) == "TIMEOUT_BEFORE_DEADLINE"

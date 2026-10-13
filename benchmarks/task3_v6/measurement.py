from __future__ import annotations

import ctypes
import hashlib
import json
import mmap
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from contextlib import closing
from typing import Callable, TypeVar


EMPTY = 0
STAGING = 1
COMMITTED = 2
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
_PAYLOAD_OFFSET = 16
_PAYLOAD_CAPACITY = 65520
_T = TypeVar("_T")


class MeasurementInfrastructureError(RuntimeError):
    pass


class CompletionKind(str, Enum):
    RETURNED = "returned"
    REGISTERED_EXCEPTION = "registered_exception"


class LifecycleFact(str, Enum):
    DIRECT_CHILD_LIVE_CONFIRMED = "DIRECT_CHILD_LIVE_CONFIRMED"
    DIRECT_CHILD_TERMINAL_CONFIRMED = "DIRECT_CHILD_TERMINAL_CONFIRMED"
    NO_LEAK_OBSERVED_AT_EXTERNAL_CHECK = "NO_LEAK_OBSERVED_AT_EXTERNAL_CHECK"
    OS_QUERY_INVALID = "OS_QUERY_INVALID"


@dataclass(frozen=True)
class LifecycleEvidence:
    evidence_id: str
    campaign_id: str
    slot_id: int
    run_id: str
    case_id: str
    interaction_id: str
    interaction_generation: int
    asset_identity: str
    observation_authority: str
    observation_source: str
    child_process_identity: str
    child_pid: int
    child_creation_identity: int
    stable_handle_binding_identity: str
    child_binding_status: str
    query_order_status: str
    transaction_identity: str
    transaction_completion_identity: str
    os_query_operation: str
    os_query_raw_result: int | None
    os_query_error: int | None
    observation_sequence: int
    observation_timestamp_utc: str
    lifecycle_fact: LifecycleFact


class StableChildProcessHandle:
    def __init__(self, process):
        if os.name != "nt":
            raise MeasurementInfrastructureError("Task #3 v6 lifecycle observation requires Windows")
        pid = process.pid
        if type(pid) is not int or pid <= 0:
            raise MeasurementInfrastructureError("direct child process has no valid PID")
        self._kernel32 = _win32()
        self._kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._kernel32.GetProcessTimes.restype = ctypes.c_int
        self._kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self.handle = self._kernel32.OpenProcess(0x00100000 | 0x00001000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        created = _FileTime()
        exited = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        if not self._kernel32.GetProcessTimes(
            self.handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
        self.pid = pid
        self.creation_identity = (created.high << 32) | created.low
        self.child_process_identity = hashlib.sha256(
            f"{pid}:{self.creation_identity}".encode("ascii")
        ).hexdigest()
        self.stable_handle_binding_identity = uuid.uuid4().hex
        self._queried = False
        self._closed = False
        self._cleanup_started = False
        self._observation_lock = threading.Lock()

    def mark_cleanup_started(self) -> None:
        with self._observation_lock:
            self._cleanup_started = True

    def observe_first_wait_zero(
        self,
        *,
        campaign_id: str,
        slot_id: int,
        run_id: str,
        case_id: str,
        interaction_id: str,
        interaction_generation: int,
        asset_identity: str,
        observation_authority: str,
        transaction_identity: str,
        transaction_completion_identity: str,
        process,
    ) -> LifecycleEvidence:
        with self._observation_lock:
            if self._closed or self._queried:
                raise MeasurementInfrastructureError(
                    "stable child handle observation is already consumed"
                )
            self._queried = True
            cleanup_started = self._cleanup_started
        binding_status = "VALID" if process.pid == self.pid else "INVALID"
        if cleanup_started:
            raw_result = None
            query_error = None
            query_order_status = "INVALID_CLEANUP_PRECEDED_QUERY"
            fact = LifecycleFact.OS_QUERY_INVALID
        else:
            raw_result = int(self._kernel32.WaitForSingleObject(self.handle, 0))
            query_error = ctypes.get_last_error() if raw_result == WAIT_FAILED else None
            query_order_status = "VALID_FIRST_EXTERNAL_CHECK"
        if cleanup_started:
            pass
        elif binding_status != "VALID" or raw_result == WAIT_FAILED:
            fact = LifecycleFact.OS_QUERY_INVALID
        elif raw_result == WAIT_TIMEOUT:
            fact = LifecycleFact.DIRECT_CHILD_LIVE_CONFIRMED
        elif raw_result == WAIT_OBJECT_0:
            fact = LifecycleFact.NO_LEAK_OBSERVED_AT_EXTERNAL_CHECK
        else:
            fact = LifecycleFact.OS_QUERY_INVALID
        evidence_data = {
            "campaign_id": campaign_id,
            "slot_id": slot_id,
            "run_id": run_id,
            "case_id": case_id,
            "interaction_id": interaction_id,
            "interaction_generation": interaction_generation,
            "asset_identity": asset_identity,
            "observation_authority": observation_authority,
            "observation_source": "SUPERVISOR_TRUSTED_OS",
            "child_process_identity": self.child_process_identity,
            "child_pid": self.pid,
            "child_creation_identity": self.creation_identity,
            "stable_handle_binding_identity": self.stable_handle_binding_identity,
            "child_binding_status": binding_status,
            "query_order_status": query_order_status,
            "transaction_identity": transaction_identity,
            "transaction_completion_identity": transaction_completion_identity,
            "os_query_operation": (
                "NOT_PERFORMED"
                if cleanup_started
                else "WaitForSingleObject(handle, 0)"
            ),
            "os_query_raw_result": raw_result,
            "os_query_error": query_error,
            "observation_sequence": 1,
            "observation_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "lifecycle_fact": fact.value,
        }
        evidence_id = hashlib.sha256(
            json.dumps(
                evidence_data,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return LifecycleEvidence(
            evidence_id=evidence_id,
            **{**evidence_data, "lifecycle_fact": fact},
        )

    def close(self) -> None:
        if not self._closed:
            self._kernel32.CloseHandle(self.handle)
            self._closed = True


class _FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))


@dataclass(frozen=True)
class MeasurementProfile:
    profile_identity: str
    clock_identity: str
    counter_frequency: int
    budget_ns: int
    formula_version: str

    def __post_init__(self) -> None:
        if not self.profile_identity or not self.clock_identity or not self.formula_version:
            raise ValueError("measurement profile identity fields are required")
        if type(self.counter_frequency) is not int or self.counter_frequency <= 0:
            raise ValueError("measurement profile counter frequency must be positive")
        if type(self.budget_ns) is not int or self.budget_ns <= 0:
            raise ValueError("measurement profile budget must be positive")


class ObservationAuthority:
    def __init__(self, database_path: str | Path):
        self._uri = str(database_path) == ":memory:"
        self.database_path = (
            f"file:task3_observation_{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self._uri
            else str(database_path)
        )
        if not self._uri:
            Path(self.database_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._memory_anchor = (
            sqlite3.connect(self.database_path, uri=True)
            if self._uri
            else None
        )
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS observations ("
                "identity TEXT PRIMARY KEY, owner_identity TEXT NOT NULL, "
                "status TEXT NOT NULL, snapshot TEXT, snapshot_hash TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, uri=self._uri, isolation_level=None)

    @staticmethod
    def _key(identity: "CompletionIdentity") -> str:
        return json.dumps(
            {
                "campaign_id": identity.campaign_id,
                "slot_id": identity.slot_id,
                "run_id": identity.run_id,
                "interaction_id": identity.interaction_id,
                "interaction_generation": identity.interaction_generation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def begin(self, identity: "CompletionIdentity") -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO observations(identity, owner_identity, status, snapshot, snapshot_hash) "
                "VALUES (?, ?, 'observation_started', NULL, NULL)",
                (self._key(identity), json.dumps(asdict(identity), sort_keys=True)),
            )
            owner = connection.execute(
                "SELECT owner_identity FROM observations WHERE identity=?",
                (self._key(identity),),
            ).fetchone()
            if owner[0] != json.dumps(asdict(identity), sort_keys=True):
                connection.rollback()
                raise MeasurementInfrastructureError(
                    "logical interaction is already bound to another completion record"
                )
            connection.commit()
            return cursor.rowcount == 1

    def finish(self, identity: "CompletionIdentity", snapshot: "TerminalSnapshot") -> None:
        payload = json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE observations SET status='finished', snapshot=?, snapshot_hash=? "
                "WHERE identity=? AND status='observation_started' AND snapshot IS NULL",
                (payload, payload_hash, self._key(identity)),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise MeasurementInfrastructureError("observation result is already immutable")
            connection.commit()

    def read(self, identity: "CompletionIdentity") -> tuple[str, "TerminalSnapshot | None"] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT owner_identity, status, snapshot, snapshot_hash "
                "FROM observations WHERE identity=?",
                (self._key(identity),),
            ).fetchone()
        if row is None:
            return None
        if row[0] != json.dumps(asdict(identity), sort_keys=True):
            raise MeasurementInfrastructureError(
                "logical interaction is already bound to another completion record"
            )
        if row[2] is None:
            return row[1], None
        if hashlib.sha256(row[2].encode("utf-8")).hexdigest() != row[3]:
            raise MeasurementInfrastructureError("persisted terminal observation integrity check failed")
        value = json.loads(row[2])
        payload = value["payload"]
        if payload is not None:
            payload = CompletionPayload(**payload)
        return row[1], TerminalSnapshot(**{**value, "payload": payload})

    def exists(self, identity: "CompletionIdentity") -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM observations WHERE identity=?",
                (self._key(identity),),
            ).fetchone()
        return row is not None


@dataclass(frozen=True)
class CompletionIdentity:
    allocation_identity: str
    campaign_id: str
    slot_id: int
    run_id: str
    case_id: str
    interaction_id: str
    interaction_type: str
    interaction_generation: int
    record_storage_identity: str
    record_commit_identity: str
    state_word_identity: str
    measurement_observation_owner_identity: str
    terminal_decision_identity: str
    notification_object_identity: str

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        slot_id: int = 0,
        run_id: str,
        case_id: str,
        interaction_id: str,
        interaction_type: str,
        interaction_generation: int,
        measurement_observation_owner_identity: str,
        terminal_decision_identity: str,
    ) -> "CompletionIdentity":
        if type(slot_id) is not int or slot_id < 0:
            raise ValueError("slot_id must be non-negative")
        if type(interaction_generation) is not int or interaction_generation < 1:
            raise ValueError("interaction_generation must be positive")
        required = (
            campaign_id,
            run_id,
            case_id,
            interaction_id,
            interaction_type,
            measurement_observation_owner_identity,
            terminal_decision_identity,
        )
        if any(not value for value in required):
            raise ValueError("completion identity fields must be non-empty")
        return cls(
            allocation_identity=uuid.uuid4().hex,
            campaign_id=campaign_id,
            slot_id=slot_id,
            run_id=run_id,
            case_id=case_id,
            interaction_id=interaction_id,
            interaction_type=interaction_type,
            interaction_generation=interaction_generation,
            record_storage_identity=uuid.uuid4().hex,
            record_commit_identity=uuid.uuid4().hex,
            state_word_identity=uuid.uuid4().hex,
            measurement_observation_owner_identity=measurement_observation_owner_identity,
            terminal_decision_identity=terminal_decision_identity,
            notification_object_identity=uuid.uuid4().hex,
        )


@dataclass(frozen=True)
class TimedInteraction:
    result: object
    result_identity: str
    completion_kind: CompletionKind
    start_ticks: int
    end_ticks: int
    counter_frequency: int
    clock_identity: str
    exception_type: str | None = None


@dataclass(frozen=True)
class CompletionPayload:
    allocation_identity: str
    campaign_id: str
    slot_id: int
    run_id: str
    case_id: str
    interaction_id: str
    interaction_type: str
    interaction_generation: int
    record_storage_identity: str
    record_commit_identity: str
    state_word_identity: str
    measurement_observation_owner_identity: str
    terminal_decision_identity: str
    notification_object_identity: str
    measurement_profile_identity: str
    deadline_formula_version: str
    completion_kind: str
    result_identity: str
    start_ticks: int
    end_ticks: int
    counter_frequency: int
    clock_identity: str
    exception_type: str | None

    @classmethod
    def from_identity(
        cls,
        identity: CompletionIdentity,
        timing: TimedInteraction,
        profile: MeasurementProfile,
    ) -> "CompletionPayload":
        return cls(
            **asdict(identity),
            measurement_profile_identity=profile.profile_identity,
            deadline_formula_version=profile.formula_version,
            completion_kind=timing.completion_kind.value,
            result_identity=timing.result_identity,
            start_ticks=timing.start_ticks,
            end_ticks=timing.end_ticks,
            counter_frequency=timing.counter_frequency,
            clock_identity=timing.clock_identity,
            exception_type=timing.exception_type,
        )


@dataclass(frozen=True)
class TerminalSnapshot:
    state: str
    payload: CompletionPayload | None
    infrastructure_failure: bool
    semantic_timeout: str
    reason: str
    observation_count: int
    budget_ns: int | None
    elapsed_ticks: int | None


def _win32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise MeasurementInfrastructureError("Task #3 v6 measurement requires Windows")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _counter_frequency(kernel32: ctypes.WinDLL) -> int:
    frequency = ctypes.c_longlong()
    if not kernel32.QueryPerformanceFrequency(ctypes.byref(frequency)):
        raise ctypes.WinError(ctypes.get_last_error())
    if frequency.value <= 0:
        raise MeasurementInfrastructureError("QueryPerformanceFrequency returned a non-positive value")
    return frequency.value


def _counter_value(kernel32: ctypes.WinDLL) -> int:
    counter = ctypes.c_longlong()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(counter)):
        raise ctypes.WinError(ctypes.get_last_error())
    return counter.value


class InteractionTimingAdapter:
    def __init__(
        self,
        profile: MeasurementProfile,
        registered_exceptions: tuple[type[BaseException], ...] = (),
    ):
        self._profile = profile
        self._registered_exceptions = registered_exceptions
        self._kernel32 = _win32()
        self._kernel32.QueryPerformanceFrequency.argtypes = (
            ctypes.POINTER(ctypes.c_longlong),
        )
        self._kernel32.QueryPerformanceFrequency.restype = ctypes.c_int
        self._kernel32.QueryPerformanceCounter.argtypes = (
            ctypes.POINTER(ctypes.c_longlong),
        )
        self._kernel32.QueryPerformanceCounter.restype = ctypes.c_int

    def measure(self, interaction: Callable[[], _T]) -> TimedInteraction:
        result_identity = uuid.uuid4().hex
        frequency = _counter_frequency(self._kernel32)
        start_ticks = _counter_value(self._kernel32)
        try:
            result = interaction()
        except self._registered_exceptions as exc:
            end_ticks = _counter_value(self._kernel32)
            return TimedInteraction(
                result=None,
                result_identity=result_identity,
                completion_kind=CompletionKind.REGISTERED_EXCEPTION,
                start_ticks=start_ticks,
                end_ticks=end_ticks,
                counter_frequency=frequency,
                clock_identity=self._profile.clock_identity,
                exception_type=type(exc).__name__,
            )
        except BaseException:
            _counter_value(self._kernel32)
            raise
        end_ticks = _counter_value(self._kernel32)
        return TimedInteraction(
            result=result,
            result_identity=result_identity,
            completion_kind=CompletionKind.RETURNED,
            start_ticks=start_ticks,
            end_ticks=end_ticks,
            counter_frequency=frequency,
            clock_identity=self._profile.clock_identity,
        )


class SharedCompletionRecord:
    def __init__(
        self,
        identity: CompletionIdentity,
        native_library: ctypes.CDLL,
        profile: MeasurementProfile,
        observation_authority: ObservationAuthority,
    ):
        self.identity = identity
        self._profile = profile
        self._observation_authority = observation_authority
        self._native = native_library
        self._mapping_name = f"Task3-{identity.record_storage_identity}"
        self._mapping = mmap.mmap(
            -1,
            _PAYLOAD_OFFSET + _PAYLOAD_CAPACITY,
            tagname=self._mapping_name,
            access=mmap.ACCESS_WRITE,
        )
        self._publisher_mapping = mmap.mmap(
            -1,
            _PAYLOAD_OFFSET + _PAYLOAD_CAPACITY,
            tagname=self._mapping_name,
            access=mmap.ACCESS_WRITE,
        )
        self._kernel32 = _win32()
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.CreateEventW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        )
        self._kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
        self._kernel32.SetEvent.restype = ctypes.c_int
        self._kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._event = self._kernel32.CreateEventW(None, False, False, None)
        if not self._event:
            self._mapping.close()
            raise ctypes.WinError(ctypes.get_last_error())
        self._condition = threading.Condition()
        self._snapshot_active = 0
        self._snapshot_taken = False
        self._closed = False
        self._publisher_closed = False
        self._published = False
        self._staged_payload: CompletionPayload | None = None
        self._terminal_snapshot: TerminalSnapshot | None = None
        base_address = ctypes.addressof(ctypes.c_char.from_buffer(self._mapping))
        self._state_address = base_address
        publisher_base_address = ctypes.addressof(
            ctypes.c_char.from_buffer(self._publisher_mapping)
        )
        self._publisher_state_address = publisher_base_address
        if self._state_address % 8 != 0:
            self._kernel32.CloseHandle(self._event)
            self._event = None
            self._publisher_mapping.close()
            self._mapping.close()
            self._closed = True
            raise MeasurementInfrastructureError("state word is not 64-bit aligned")
        self.state_word_alignment = 8
        self.publisher_state_word_identity = identity.state_word_identity
        self.supervisor_state_word_identity = identity.state_word_identity

    @property
    def profile(self) -> MeasurementProfile:
        return self._profile

    @classmethod
    def create(
        cls,
        identity: CompletionIdentity,
        profile: MeasurementProfile,
        observation_authority: ObservationAuthority,
    ) -> "SharedCompletionRecord":
        if os.name != "nt":
            raise MeasurementInfrastructureError("Task #3 v6 measurement requires Windows")
        root = Path(__file__).resolve().parents[2]
        dll_path = root / ".tmp" / "task3_v6" / "task3_atomic.dll"
        if not dll_path.is_file():
            raise MeasurementInfrastructureError(
                f"native atomic helper is missing: {dll_path}"
            )
        return cls(identity, ctypes.CDLL(str(dll_path)), profile, observation_authority)

    def _compare_exchange(
        self,
        exchange: int,
        comparand: int,
        mapping: mmap.mmap | None = None,
    ) -> int:
        state = ctypes.c_longlong.from_buffer(mapping or self._mapping, 0)
        function = self._native.task3_interlocked_compare_exchange64
        function.argtypes = (
            ctypes.POINTER(ctypes.c_longlong),
            ctypes.c_longlong,
            ctypes.c_longlong,
        )
        function.restype = ctypes.c_longlong
        previous = function(ctypes.byref(state), exchange, comparand)
        del state
        return previous

    def publisher_begin(self) -> bool:
        if self._closed or self._publisher_closed or self._published:
            raise MeasurementInfrastructureError("completion allocation is not writable")
        return self._compare_exchange(STAGING, EMPTY, self._publisher_mapping) == EMPTY

    @property
    def state_word_address(self) -> int:
        return self._state_address

    @property
    def publisher_state_word_address(self) -> int:
        return self._publisher_state_address

    def stage(self, timing: TimedInteraction) -> CompletionPayload:
        if self._closed or self._publisher_closed or self._published:
            raise MeasurementInfrastructureError("completion record cannot be overwritten")
        if self._staged_payload is not None:
            raise MeasurementInfrastructureError("completion payload is already staged")
        if self._compare_exchange(STAGING, STAGING, self._publisher_mapping) != STAGING:
            raise MeasurementInfrastructureError("publisher does not own STAGING state")
        payload = CompletionPayload.from_identity(self.identity, timing, self._profile)
        if (
            type(payload.start_ticks) is not int
            or type(payload.end_ticks) is not int
            or type(payload.counter_frequency) is not int
            or payload.start_ticks < 0
            or payload.end_ticks < payload.start_ticks
            or payload.counter_frequency <= 0
            or not payload.clock_identity
            or payload.measurement_profile_identity != self._profile.profile_identity
            or payload.deadline_formula_version != self._profile.formula_version
            or not payload.result_identity
            or not payload.clock_identity
            or payload.completion_kind not in {kind.value for kind in CompletionKind}
            or (
                payload.completion_kind == CompletionKind.RETURNED.value
                and payload.exception_type is not None
            )
            or (
                payload.completion_kind == CompletionKind.REGISTERED_EXCEPTION.value
                and not payload.exception_type
            )
        ):
            raise MeasurementInfrastructureError("completion timing payload is invalid")
        encoded = json.dumps(asdict(payload), sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > _PAYLOAD_CAPACITY:
            raise MeasurementInfrastructureError("completion payload exceeds fixed record capacity")
        self._publisher_mapping.seek(8)
        self._publisher_mapping.write(len(encoded).to_bytes(8, "little"))
        self._publisher_mapping.seek(_PAYLOAD_OFFSET)
        self._publisher_mapping.write(encoded)
        self._publisher_mapping.flush()
        self._staged_payload = payload
        return payload

    def commit(self) -> CompletionPayload:
        if self._closed or self._publisher_closed or self._published:
            raise MeasurementInfrastructureError("completion record cannot be overwritten")
        if self._staged_payload is None:
            raise MeasurementInfrastructureError("completion payload must be staged before commit")
        if self._compare_exchange(STAGING, STAGING, self._publisher_mapping) != STAGING:
            raise MeasurementInfrastructureError("publisher does not own STAGING state")
        if self._compare_exchange(COMMITTED, STAGING, self._publisher_mapping) != STAGING:
            raise MeasurementInfrastructureError("STAGING to COMMITTED transition failed")
        self._published = True
        if not self._kernel32.SetEvent(self._event):
            raise ctypes.WinError(ctypes.get_last_error())
        return self._staged_payload

    def publish(self, timing: TimedInteraction) -> CompletionPayload:
        self.stage(timing)
        return self.commit()

    def publisher_exit(self) -> None:
        if self._publisher_closed:
            return
        self._publisher_closed = True
        self._publisher_mapping.close()

    def wait_for_notification(self, timeout_ms: int = INFINITE) -> bool:
        if self._closed:
            raise MeasurementInfrastructureError("notification handle is closed")
        result = self._kernel32.WaitForSingleObject(self._event, timeout_ms)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    @property
    def notification_is_initially_nonsignaled(self) -> bool:
        return self._kernel32.WaitForSingleObject(self._event, 0) == WAIT_TIMEOUT

    def terminal_snapshot(self) -> TerminalSnapshot:
        with self._condition:
            if self._closed:
                raise MeasurementInfrastructureError("supervisor mapping is closed")
            if self._snapshot_taken:
                raise MeasurementInfrastructureError("terminal state may be observed only once")
            existing = self._observation_authority.read(self.identity)
            if existing is not None:
                status, snapshot = existing
                if status == "finished" and snapshot is not None:
                    self._terminal_snapshot = snapshot
                    self._snapshot_taken = True
                    return snapshot
                raise MeasurementInfrastructureError(
                    "terminal observation has already started and has no committed result"
                )
            if not self._observation_authority.begin(self.identity):
                existing = self._observation_authority.read(self.identity)
                if existing is None or existing[0] != "finished" or existing[1] is None:
                    raise MeasurementInfrastructureError(
                        "terminal observation authority is already owned"
                    )
                self._terminal_snapshot = existing[1]
                self._snapshot_taken = True
                return existing[1]
            self._snapshot_active += 1
            self._snapshot_taken = True
        try:
            observed = self._compare_exchange(EMPTY, EMPTY)
            if observed == EMPTY:
                snapshot = TerminalSnapshot(
                    "INFRASTRUCTURE_FAILURE", None, True, "UNPROVEN", "NO_COMMITTED_RECORD", 1,
                    None, None,
                )
            elif observed == STAGING:
                snapshot = TerminalSnapshot(
                    "STAGING", None, True, "UNPROVEN", "publisher_staging", 1,
                    None, None,
                )
            elif observed == COMMITTED:
                length = int.from_bytes(self._mapping[8:16], "little")
                if length <= 0 or length > _PAYLOAD_CAPACITY:
                    snapshot = TerminalSnapshot(
                        "INVALID_RECORD", None, True, "UNPROVEN", "invalid_payload_length", 1,
                        None, None,
                    )
                else:
                    raw = self._mapping[_PAYLOAD_OFFSET : _PAYLOAD_OFFSET + length]
                    try:
                        data = json.loads(raw)
                        payload = CompletionPayload(**data)
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                        snapshot = TerminalSnapshot(
                            "INVALID_RECORD",
                            None,
                            True,
                            "UNPROVEN",
                            f"invalid_payload:{type(exc).__name__}",
                            1,
                            None,
                            None,
                        )
                    else:
                        expected = asdict(self.identity)
                        actual = {
                            key: getattr(payload, key)
                            for key in expected
                        }
                        timing_valid = (
                            type(payload.start_ticks) is int
                            and type(payload.end_ticks) is int
                            and type(payload.counter_frequency) is int
                            and payload.start_ticks >= 0
                            and payload.end_ticks >= payload.start_ticks
                            and payload.counter_frequency == self.profile.counter_frequency
                            and payload.clock_identity == self.profile.clock_identity
                            and payload.measurement_profile_identity == self.profile.profile_identity
                            and payload.deadline_formula_version == self.profile.formula_version
                            and bool(payload.result_identity)
                            and bool(payload.clock_identity)
                            and payload.completion_kind
                            in {kind.value for kind in CompletionKind}
                            and (
                                payload.completion_kind == CompletionKind.RETURNED.value
                                and payload.exception_type is None
                                or payload.completion_kind
                                == CompletionKind.REGISTERED_EXCEPTION.value
                                and bool(payload.exception_type)
                            )
                        )
                        if actual != expected or not timing_valid:
                            snapshot = TerminalSnapshot(
                                "INVALID_RECORD", None, True, "UNPROVEN", "identity_or_timing_mismatch", 1,
                                None, None,
                            )
                        else:
                            elapsed_ticks = payload.end_ticks - payload.start_ticks
                            on_time = (
                                elapsed_ticks * 1_000_000_000
                                <= self.profile.budget_ns * self.profile.counter_frequency
                            )
                            semantic_timeout = "NOT_CONFIRMED" if on_time else "CONFIRMED"
                            snapshot = TerminalSnapshot(
                                "COMMITTED", payload, False, semantic_timeout,
                                "committed_timing_compared_to_frozen_integer_budget", 1,
                                self.profile.budget_ns, elapsed_ticks,
                            )
            else:
                snapshot = TerminalSnapshot(
                    "INVALID_RECORD", None, True, "UNPROVEN", "unknown_state_word", 1,
                    None, None,
                )
            with self._condition:
                self._terminal_snapshot = snapshot
            self._observation_authority.finish(self.identity, snapshot)
            return snapshot
        finally:
            with self._condition:
                self._snapshot_active -= 1
                self._condition.notify_all()

    @property
    def terminal_snapshot_count(self) -> int:
        return int(self._snapshot_taken)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            if not self._snapshot_taken and not self._observation_authority.exists(self.identity):
                raise MeasurementInfrastructureError(
                    "supervisor mapping cannot close before snapshot ownership transfer"
                )
            while self._snapshot_active:
                self._condition.wait()
            self._closed = True
        if self._event:
            self._kernel32.CloseHandle(self._event)
            self._event = None
        self.publisher_exit()
        self._mapping.close()

    def __enter__(self) -> "SharedCompletionRecord":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

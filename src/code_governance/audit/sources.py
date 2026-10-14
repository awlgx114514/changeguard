import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import apsw

from .models import AuditDiscoveryScope, AuditRecord, RawAuditSources, RawSource, SourceGap


TASK3_TABLES = (
    "campaigns",
    "slots",
    "terminal_decisions",
    "run_evidence",
    "direct_child_bindings",
    "lifecycle_evidence",
    "registered_asset_defects",
    "measurement_profiles",
    "prepared_terminal_decisions",
    "diagnostics",
)

_TASK3_REQUIRED_COLUMNS = {
    "campaigns": {"campaign_id", "identity_json", "identity_hash", "state"},
    "slots": {"campaign_id", "slot_id", "state", "run_id", "terminal_decision_identity", "verdict"},
    "terminal_decisions": {"terminal_decision_identity", "campaign_id", "run_id", "slot_id", "payload_json", "payload_hash"},
    "run_evidence": {"evidence_id", "campaign_id", "run_id", "slot_id", "interaction_id", "interaction_generation", "payload_json", "payload_hash"},
    "direct_child_bindings": {"campaign_id", "slot_id", "run_id", "case_id", "interaction_id", "interaction_generation", "transaction_identity", "payload_json", "payload_hash"},
    "lifecycle_evidence": {"evidence_id", "campaign_id", "slot_id", "run_id", "interaction_id", "interaction_generation", "payload_json", "payload_hash"},
    "registered_asset_defects": {"campaign_id", "asset_identity", "defect_id", "payload_json", "payload_hash"},
    "measurement_profiles": {"campaign_id", "payload_json", "payload_hash"},
    "prepared_terminal_decisions": {"terminal_decision_identity", "campaign_id", "run_id", "slot_id", "payload_json", "payload_hash", "phase", "receipt_identity"},
    "diagnostics": {"diagnostic_id", "terminal_decision_identity", "event_type", "event_payload"},
}

_TASK3_QUERIES = {
    "campaigns": "SELECT * FROM campaigns WHERE campaign_id=?",
    "slots": "SELECT * FROM slots WHERE run_id=?",
    "terminal_decisions": "SELECT * FROM terminal_decisions WHERE run_id=?",
    "run_evidence": "SELECT * FROM run_evidence WHERE run_id=?",
    "direct_child_bindings": "SELECT * FROM direct_child_bindings WHERE run_id=?",
    "lifecycle_evidence": "SELECT * FROM lifecycle_evidence WHERE run_id=?",
    "registered_asset_defects": "SELECT * FROM registered_asset_defects WHERE campaign_id=?",
    "measurement_profiles": "SELECT * FROM measurement_profiles WHERE campaign_id=?",
    "prepared_terminal_decisions": "SELECT * FROM prepared_terminal_decisions WHERE run_id=?",
    "diagnostics": (
        "SELECT diagnostics.*, terminal_decisions.run_id, terminal_decisions.campaign_id, terminal_decisions.slot_id "
        "FROM diagnostics "
        "JOIN terminal_decisions USING (terminal_decision_identity) "
        "WHERE terminal_decisions.run_id=?"
    ),
}

_TASK3_REFERENCE_QUERIES = {
    "run_evidence": "SELECT * FROM run_evidence WHERE evidence_id=?",
    "lifecycle_evidence": "SELECT * FROM lifecycle_evidence WHERE evidence_id=?",
}


class DuplicateJSONKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_json(contents: bytes) -> Any:
    return json.loads(
        contents.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )


def _is_valid_run_id(run_id: str) -> bool:
    return (
        bool(run_id)
        and run_id not in {".", ".."}
        and Path(run_id).name == run_id
        and not Path(run_id).drive
        and not run_id.endswith((" ", "."))
        and not any(character in run_id for character in '<>:"|?*')
        and not any(ord(character) < 32 for character in run_id)
    )


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _file_snapshot(path: Path, allowed_root: Path) -> tuple[bytes, dict[str, Any]]:
    root = allowed_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise PermissionError(f"source path is outside its allowed root: {path}")
    with resolved.open("rb") as stream:
        before = os.fstat(stream.fileno())
        contents = stream.read()
        after = os.fstat(stream.fileno())
    boundary = {
        "locator": str(resolved),
        "size_bytes": len(contents),
        "mtime_ns_before": before.st_mtime_ns,
        "mtime_ns_after": after.st_mtime_ns,
        "file_id_before": getattr(before, "st_ino", None),
        "file_id_after": getattr(after, "st_ino", None),
        "stable_during_capture": (
            before.st_size == after.st_size == len(contents)
            and before.st_mtime_ns == after.st_mtime_ns
            and getattr(before, "st_ino", None) == getattr(after, "st_ino", None)
        ),
    }
    if not boundary["stable_during_capture"]:
        raise OSError(f"source changed during capture: {resolved}")
    return contents, boundary


def _record_ref(source_id: str, kind: str, locator: str, native_id: Optional[str]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "locator_kind": "DATABASE_ROW" if kind.startswith("TASK3_") else "JSON_POINTER",
        "native_locator": locator,
        "record_kind": kind,
        "native_id": native_id,
        "identity_class": "NATIVE" if native_id is not None else "AUDIT_LOCAL",
    }


def _memory_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("records", "items", "decisions", "failures", "patterns"):
        items = value.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return [value]


def _capture_json_source(
    path: Path,
    allowed_root: Path,
    run_id: str,
    capture_id: str,
    source_index: int,
    explicit: bool,
) -> tuple[RawSource, list[AuditRecord], list[SourceGap], dict[str, Any]]:
    locator = str(path)
    source_id = f"source-{source_index}"
    gaps: list[SourceGap] = []
    records: list[AuditRecord] = []
    try:
        contents, boundary = _file_snapshot(path, allowed_root)
    except PermissionError as error:
        gap = SourceGap(
            "SOURCE_UNAVAILABLE",
            locator,
            {"run_id": run_id},
            {"reason": str(error), "scope": "EXPLICIT_SOURCE" if explicit else "RUN_DIRECTORY"},
        )
        return (
            RawSource(source_id, "UNKNOWN", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("SOURCE_UNAVAILABLE",)),
            records,
            [gap],
            {"locator": locator, "status": "SOURCE_UNAVAILABLE"},
        )
    except OSError as error:
        gap = SourceGap(
            "SOURCE_UNAVAILABLE",
            locator,
            {"run_id": run_id},
            {"reason": str(error), "scope": "EXPLICIT_SOURCE" if explicit else "RUN_DIRECTORY"},
        )
        return (
            RawSource(source_id, "UNKNOWN", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("SOURCE_UNAVAILABLE",)),
            records,
            [gap],
            {"locator": locator, "status": "SOURCE_UNAVAILABLE"},
        )

    digest = _sha256(contents)
    try:
        value = _decode_json(contents)
    except (UnicodeDecodeError, ValueError) as error:
        gap = SourceGap("SOURCE_CORRUPT", locator, {"run_id": run_id}, {"reason": str(error)})
        source = RawSource(
            source_id,
            "JSON",
            str(Path(locator).resolve()),
            "SOURCE_CORRUPT",
            digest,
            None,
            {},
            (),
            boundary,
            ("JSON_DECODING_FAILED",),
            captured_content=contents,
        )
        return source, records, [gap], boundary

    source_kind = "JSON_EXPORT"
    schema_version = None
    source_availability = "AVAILABLE"
    native_identity: dict[str, Any] = {}
    limitations: list[str] = []
    has_target_record = False

    if isinstance(value, dict) and "events" in value:
        schema_version = str(value.get("schema_version")) if value.get("schema_version") is not None else None
        source_kind = "TRACE_V2" if schema_version == "v2" else "TRACE_UNSUPPORTED"
        native_identity = {"run_id": value.get("run_id")}
        if value.get("run_id") != run_id:
            gaps.append(
                SourceGap(
                    "IDENTITY_CONFLICT",
                    locator,
                    {"run_id": run_id},
                    {"observed_run_id": value.get("run_id"), "source_kind": source_kind},
                )
            )
        elif schema_version != "v2":
            source_availability = "SOURCE_UNSUPPORTED"
            has_target_record = True
            limitations.append("TRACE_SCHEMA_UNSUPPORTED")
            gaps.append(
                SourceGap(
                    "SCHEMA_UNSUPPORTED",
                    locator,
                    {"run_id": run_id},
                    {"observed_schema_version": schema_version, "supported_schema_versions": ["v2"]},
                )
            )
        elif not isinstance(value.get("events"), list):
            gaps.append(SourceGap("SOURCE_CORRUPT", locator, {"run_id": run_id}, {"reason": "Trace v2 events must be a list"}))
        else:
            has_target_record = True
            previous_sequence: Optional[int] = None
            for index, event in enumerate(value["events"]):
                if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not isinstance(event.get("data"), dict):
                    gaps.append(
                        SourceGap(
                            "SOURCE_CORRUPT",
                            locator,
                            {"run_id": run_id},
                            {"event_index": index, "reason": "Trace v2 event has an invalid shape"},
                        )
                    )
                    continue
                sequence = event.get("sequence")
                event_type = event["type"]
                kind = _trace_record_kind(event_type)
                valid_sequence = isinstance(sequence, int) and not isinstance(sequence, bool)
                if not valid_sequence:
                    gaps.append(
                        SourceGap(
                            "SOURCE_CORRUPT",
                            locator,
                            {"run_id": run_id},
                            {"event_index": index, "reason": "Trace v2 sequence must be an integer"},
                        )
                    )
                elif previous_sequence is not None and sequence <= previous_sequence:
                    gaps.append(
                        SourceGap(
                            "IDENTITY_CONFLICT",
                            locator,
                            {"run_id": run_id},
                            {"event_index": index, "sequence": sequence, "previous_sequence": previous_sequence},
                        )
                    )
                if valid_sequence:
                    previous_sequence = sequence
                native_id = str(sequence) if valid_sequence else None
                data = {
                    "type": event_type,
                    "data": event["data"],
                    "sequence": sequence,
                    "timestamp": event.get("timestamp"),
                }
                records.append(
                    AuditRecord(
                        _record_ref(source_id, kind, f"/events/{index}", native_id),
                        kind,
                        source_kind,
                        schema_version,
                        {"run_id": run_id},
                        native_id,
                        data,
                        "trace-producer",
                    )
                )
    elif isinstance(value, dict) and isinstance(value.get("results"), list) and "run_count" in value:
        source_kind = "TASK3_SUMMARY"
        native_identity = {"profile_id": value.get("profile_id"), "run_count": value.get("run_count")}
        matched = [item for item in value["results"] if isinstance(item, dict) and item.get("run_id") == run_id]
        if len(matched) > 1:
            gaps.append(
                SourceGap(
                    "IDENTITY_CONFLICT",
                    locator,
                    {"run_id": run_id},
                    {"matching_result_count": len(matched), "source_kind": source_kind},
                )
            )
        for index, item in enumerate(value["results"]):
            if not isinstance(item, dict):
                gaps.append(SourceGap("SOURCE_CORRUPT", locator, {"run_id": run_id}, {"result_index": index, "reason": "summary result must be an object"}))
                continue
            if item.get("run_id") != run_id:
                continue
            has_target_record = True
            records.append(
                AuditRecord(
                    _record_ref(source_id, "UNBOUND_SUPPLEMENT", f"/results/{index}", run_id),
                    "UNBOUND_SUPPLEMENT",
                    source_kind,
                    None,
                    {"run_id": run_id},
                    run_id,
                    item,
                    None,
                )
            )
            limitations.append("SUMMARY_HAS_NO_RUN_SPECIFIC_SOURCE_BINDING")
            binding = item.get("source_import_evidence")
            if isinstance(binding, dict):
                binding_run_id = binding.get("run_id")
                binding_scope = {
                    "run_id": binding_run_id,
                    "case_id": binding.get("case_id"),
                    "fixture_identity": binding.get("fixture_identity"),
                    "asset_identity": binding.get("asset_identity"),
                    "environment_identity": binding.get("environment_identity"),
                }
                records.append(
                    AuditRecord(
                        _record_ref(source_id, "SOURCE_BINDING", f"/results/{index}/source_import_evidence", str(binding_run_id or run_id)),
                        "SOURCE_BINDING",
                        source_kind,
                        None,
                        {key: value for key, value in binding_scope.items() if value is not None},
                        str(binding_run_id or run_id),
                        binding,
                        None,
                    )
                )
                limitations.pop()
    else:
        source_kind = "MEMORY_JSON" if Path(locator).parent.name == "memory" else "JSON_EXPORT"
        for index, item in enumerate(_memory_items(value)):
            if item.get("run_id") != run_id:
                continue
            has_target_record = True
            kind = _memory_record_kind(Path(locator).stem)
            native_id = next(
                (str(item[key]) for key in ("decision_id", "failure_id", "pattern_id", "id") if item.get(key) is not None),
                None,
            )
            records.append(
                AuditRecord(
                    _record_ref(source_id, kind, f"/{index}", native_id),
                    kind,
                    source_kind,
                    None,
                    {"run_id": run_id},
                    native_id,
                    item,
                    "memory-producer",
                )
            )
        if not has_target_record:
            limitations.append("UNBOUND_JSON_EXPORT")

    record_refs = tuple(record.record_ref for record in records)
    source = RawSource(
        source_id,
        source_kind,
        str(Path(locator).resolve()),
        source_availability,
        digest,
        schema_version,
        native_identity,
        record_refs,
        boundary,
        tuple(limitations),
        captured_content=contents,
    )
    return source, records, gaps, boundary


def _trace_record_kind(event_type: str) -> str:
    if event_type in {"action_decided", "decision_made", "tool_policy_decided", "approval_resolved"}:
        return "ACTION"
    if event_type in {"action_executed", "tool_execution_completed", "tool_finished", "ptc_child_result"}:
        return "EXECUTION_RESULT"
    if event_type == "observation_received":
        return "OBSERVATION"
    if event_type.startswith("validation_") or event_type in {"contract_validated", "progress_evaluated"}:
        return "VALIDATION"
    if event_type.startswith("repair_"):
        return "REPAIR"
    if event_type in {"tool_policy_evaluated", "approval_requested", "hard_guard_evaluated"}:
        return "POLICY_EVALUATION"
    if event_type == "task_finished":
        return "REPORT_PROJECTION"
    if event_type in {
        "task_started",
        "agent_loop_started",
        "agent_loop_finished",
        "context_created",
        "context_contract_attached",
        "memory_retrieved",
        "plan_created",
        "decision_context_prepared",
        "state_updated",
        "history_compressed",
    }:
        return "LIFECYCLE_MARKER"
    if event_type in {"tool_called", "tool_execution_started", "tool_timeout_requested", "tool_termination_started", "tool_quiescent"}:
        return "EXECUTION_MARKER"
    return "UNSUPPORTED_RECORD"


def _memory_record_kind(name: str) -> str:
    if name == "decisions":
        return "MEMORY_DECISION"
    if name == "failures":
        return "MEMORY_FAILURE"
    return "MEMORY_PATTERN"


def _sqlite_rows(connection: apsw.Connection, sql: str, parameter: str) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute(sql, (parameter,))
    rows: list[dict[str, Any]] = []
    columns: Optional[list[str]] = None
    for row in cursor:
        if columns is None:
            columns = [column[0] for column in cursor.get_description()]
        rows.append(dict(zip(columns, row)))
    return rows


def _sqlite_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        return _decode_json(value.encode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _task3_record_id(table: str, row: dict[str, Any]) -> Optional[str]:
    if table == "slots" and row.get("slot_id") is not None:
        return f"{row.get('campaign_id')}:{row['slot_id']}"
    if table == "direct_child_bindings" and row.get("transaction_identity") is not None:
        return str(row["transaction_identity"])
    if table == "registered_asset_defects":
        return ":".join(str(row.get(key, "")) for key in ("campaign_id", "asset_identity", "defect_id"))
    if table == "diagnostics" and row.get("diagnostic_id") is not None:
        return str(row["diagnostic_id"])
    for key in (
        "terminal_decision_identity",
        "evidence_id",
        "campaign_id",
        "diagnostic_id",
    ):
        if row.get(key) is not None:
            return str(row[key])
    return None


def _decision_evidence_ids(records: list[AuditRecord]) -> set[str]:
    references: set[str] = set()
    for record in records:
        if record.record_kind != "TASK3_TERMINAL_DECISIONS":
            continue
        payload = record.data.get("payload")
        if not isinstance(payload, dict):
            continue
        references.update(value for value in payload.get("evidence_ids", []) if isinstance(value, str))
        attribution = payload.get("attribution_result")
        if isinstance(attribution, dict):
            references.update(value for value in attribution.get("evidence_ids", []) if isinstance(value, str))
        components = payload.get("component_results", [])
        if isinstance(components, list):
            for component in components:
                if isinstance(component, dict):
                    references.update(value for value in component.get("evidence_ids", []) if isinstance(value, str))
    return references


def _measurement_lifecycle_ids(records: list[AuditRecord]) -> set[str]:
    references: set[str] = set()
    for record in records:
        if record.record_kind != "TASK3_RUN_EVIDENCE":
            continue
        payload = record.data.get("payload")
        if isinstance(payload, dict):
            references.update(
                value for value in payload.get("lifecycle_evidence_ids", [])
                if isinstance(value, str)
            )
    return references


def _capture_task3_database(
    path: Path,
    allowed_root: Path,
    run_id: str,
    source_index: int,
) -> tuple[RawSource, list[AuditRecord], list[SourceGap], dict[str, Any]]:
    locator = str(path)
    source_id = f"source-{source_index}"
    gaps: list[SourceGap] = []
    records: list[AuditRecord] = []
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = allowed_root.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root):
            raise PermissionError(f"source path is outside its allowed root: {path}")
    except PermissionError as error:
        gap = SourceGap("SOURCE_UNAVAILABLE", locator, {"run_id": run_id}, {"reason": str(error)})
        return RawSource(source_id, "TASK3_SQLITE", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("SOURCE_UNAVAILABLE",)), records, [gap], {"locator": locator, "status": "SOURCE_UNAVAILABLE"}
    except OSError as error:
        gap = SourceGap("SOURCE_UNAVAILABLE", locator, {"run_id": run_id}, {"reason": str(error)})
        return RawSource(source_id, "TASK3_SQLITE", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("SOURCE_UNAVAILABLE",)), records, [gap], {"locator": locator, "status": "SOURCE_UNAVAILABLE"}

    sidecars = tuple(resolved_path.with_name(resolved_path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))
    existing_sidecars = [str(sidecar) for sidecar in sidecars if sidecar.exists()]
    if existing_sidecars:
        gap = SourceGap(
            "READ_ONLY_PRECONDITION_FAILED",
            locator,
            {"run_id": run_id},
            {"existing_sidecars": existing_sidecars},
        )
        return (
            RawSource(source_id, "TASK3_SQLITE", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("READ_ONLY_PRECONDITION_FAILED",)),
            records,
            [gap],
            {"locator": locator, "status": "READ_ONLY_PRECONDITION_FAILED", "sidecars": existing_sidecars},
        )

    try:
        contents, boundary = _file_snapshot(resolved_path, allowed_root)
    except PermissionError as error:
        gap = SourceGap("SOURCE_UNAVAILABLE", locator, {"run_id": run_id}, {"reason": str(error)})
        return RawSource(source_id, "TASK3_SQLITE", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("SOURCE_UNAVAILABLE",)), records, [gap], {"locator": locator, "status": "SOURCE_UNAVAILABLE"}
    except OSError as error:
        gap = SourceGap("SOURCE_UNAVAILABLE", locator, {"run_id": run_id}, {"reason": str(error)})
        return RawSource(source_id, "TASK3_SQLITE", locator, "SOURCE_UNAVAILABLE", None, None, {}, (), {}, ("SOURCE_UNAVAILABLE",)), records, [gap], {"locator": locator, "status": "SOURCE_UNAVAILABLE"}

    digest = _sha256(contents)
    database_uri = Path(boundary["locator"]).as_uri() + "?mode=ro&immutable=1"
    connection = apsw.Connection(
        database_uri,
        flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI,
    )
    integrity_rows = list(connection.execute("PRAGMA integrity_check"))
    if integrity_rows != [("ok",)]:
        connection.close()
        gap = SourceGap("SOURCE_CORRUPT", locator, {"run_id": run_id}, {"integrity_check": integrity_rows})
        return (
            RawSource(
                source_id,
                "TASK3_SQLITE",
                str(path.resolve()),
                "SOURCE_CORRUPT",
                digest,
                None,
                {},
                (),
                boundary,
                ("SQLITE_INTEGRITY_CHECK_FAILED",),
                captured_content=contents,
            ),
            records,
            [gap],
            boundary,
        )

    foreign_key_rows = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_key_rows:
        gaps.append(
            SourceGap(
                "IDENTITY_CONFLICT",
                locator,
                {"run_id": run_id},
                {"foreign_key_violations": foreign_key_rows},
            )
        )

    table_rows = list(connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
    available_tables = {row[0] for row in table_rows}
    missing_tables = sorted(set(TASK3_TABLES) - available_tables)
    missing_columns: dict[str, list[str]] = {}
    for table in TASK3_TABLES:
        if table not in available_tables:
            continue
        column_rows = list(connection.execute(f'PRAGMA table_info("{table}")'))
        available_columns = {row[1] for row in column_rows}
        absent = sorted(_TASK3_REQUIRED_COLUMNS[table] - available_columns)
        if absent:
            missing_columns[table] = absent
    unsupported_tables = set(missing_tables) | set(missing_columns)
    if missing_tables or missing_columns:
        gaps.append(
            SourceGap(
                "SCHEMA_UNSUPPORTED",
                locator,
                {"run_id": run_id},
                {
                    "missing_tables": missing_tables,
                    "missing_columns": missing_columns,
                    "schema_scope": "TASK3_SQLITE",
                },
            )
        )

    slots = (
        _sqlite_rows(connection, _TASK3_QUERIES["slots"], run_id)
        if "slots" in available_tables and "slots" not in unsupported_tables
        else []
    )
    campaign_ids = sorted({str(row["campaign_id"]) for row in slots if row.get("campaign_id") is not None})
    explicit_references_followed: list[dict[str, Any]] = []
    for table in TASK3_TABLES:
        if table not in available_tables or table in unsupported_tables:
            continue
        if table == "slots":
            table_records = slots
        elif table == "campaigns":
            table_records = [row for campaign_id in campaign_ids for row in _sqlite_rows(connection, _TASK3_QUERIES[table], campaign_id)]
        elif table in {"registered_asset_defects", "measurement_profiles"}:
            table_records = [row for campaign_id in campaign_ids for row in _sqlite_rows(connection, _TASK3_QUERIES[table], campaign_id)]
        else:
            table_records = _sqlite_rows(connection, _TASK3_QUERIES[table], run_id)
        if table in _TASK3_REFERENCE_QUERIES:
            reference_ids = _decision_evidence_ids(records)
            if table == "lifecycle_evidence":
                reference_ids.update(_measurement_lifecycle_ids(records))
            selected_row_ids = {
                (row.get("evidence_id"), row.get("run_id"))
                for row in table_records
            }
            for reference_id in sorted(reference_ids):
                referenced_rows = _sqlite_rows(
                    connection,
                    _TASK3_REFERENCE_QUERIES[table],
                    reference_id,
                )
                unseen_rows = [
                    row for row in referenced_rows
                    if (row.get("evidence_id"), row.get("run_id")) not in selected_row_ids
                ]
                table_records.extend(unseen_rows)
                selected_row_ids.update(
                    (row.get("evidence_id"), row.get("run_id"))
                    for row in unseen_rows
                )
                explicit_references_followed.append(
                    {
                        "native_id": reference_id,
                        "target_table": table,
                        "status": "CAPTURED" if referenced_rows else "NOT_FOUND",
                        "captured_scopes": [
                            {
                                key: row.get(key)
                                for key in ("run_id", "campaign_id", "slot_id", "interaction_id", "interaction_generation")
                            }
                            for row in referenced_rows
                        ],
                    }
                )
        for row in table_records:
            row_id = _task3_record_id(table, row)
            payload = _sqlite_payload(row.get("payload_json"))
            if row.get("payload_json") is not None and payload is None:
                gaps.append(
                    SourceGap(
                        "SOURCE_CORRUPT",
                        locator,
                        {"run_id": run_id, "table": table, "record_id": row_id},
                        {"field": "payload_json", "reason": "persisted JSON payload cannot be decoded"},
                    )
                )
            data = {"row": row}
            if payload is not None:
                data["payload"] = payload
            identity_json = _sqlite_payload(row.get("identity_json"))
            if identity_json is not None:
                data["identity"] = identity_json
            elif row.get("identity_json") is not None:
                gaps.append(
                    SourceGap(
                        "SOURCE_CORRUPT",
                        locator,
                        {"run_id": run_id, "table": table, "record_id": row_id},
                        {"field": "identity_json", "reason": "persisted JSON identity cannot be decoded"},
                    )
                )
            event_payload = _sqlite_payload(row.get("event_payload"))
            if event_payload is not None:
                data["event_payload"] = event_payload
            elif row.get("event_payload") is not None:
                gaps.append(
                    SourceGap(
                        "SOURCE_CORRUPT",
                        locator,
                        {"run_id": run_id, "table": table, "record_id": row_id},
                        {"field": "event_payload", "reason": "persisted JSON event cannot be decoded"},
                    )
                )
            scope = {
                key: row[key]
                for key in (
                    "campaign_id",
                    "slot_id",
                    "run_id",
                    "case_id",
                    "interaction_id",
                    "interaction_generation",
                )
                if key in row
            }
            if isinstance(payload, dict):
                for key in (
                    "campaign_id",
                    "slot_id",
                    "run_id",
                    "case_id",
                    "interaction_id",
                    "interaction_generation",
                ):
                    if key not in scope and payload.get(key) is not None:
                        scope[key] = payload[key]
            record_kind = "TASK3_" + table.upper()
            record_ref = _record_ref(source_id, record_kind, f"{table}:{row_id or 'unknown'}", row_id)
            records.append(
                AuditRecord(
                    record_ref,
                    record_kind,
                    "TASK3_SQLITE",
                    None,
                    scope,
                    row_id,
                    data,
                    payload.get("observation_authority") if isinstance(payload, dict) else None,
                )
            )
    connection.close()

    current_contents, current_boundary = _file_snapshot(resolved_path, allowed_root)
    current_sidecars = [str(sidecar) for sidecar in sidecars if sidecar.exists()]
    if _sha256(current_contents) != digest or current_sidecars:
        gap = SourceGap(
            "SOURCE_CHANGED",
            locator,
            {"run_id": run_id},
            {
                "initial_digest": digest,
                "current_digest": _sha256(current_contents),
                "current_sidecars": current_sidecars,
            },
        )
        source = RawSource(
            source_id,
            "TASK3_SQLITE",
            str(Path(locator).resolve()),
            "SOURCE_UNAVAILABLE",
            digest,
            None,
            {"run_id": run_id, "campaign_ids": campaign_ids},
            tuple(record.record_ref for record in records),
            {**boundary, "after_read": current_boundary, "snapshot_method": "READ_ONLY_IMMUTABLE_URI"},
            ("SOURCE_CHANGED",),
            captured_content=contents,
        )
        return source, records, [*gaps, gap], source.read_boundary

    source = RawSource(
        source_id,
        "TASK3_SQLITE",
        str(path.resolve()),
        "AVAILABLE",
        digest,
        None,
        {"run_id": run_id, "campaign_ids": campaign_ids},
        tuple(record.record_ref for record in records),
        {
            **boundary,
            "read_consistency": "SINGLE_SOURCE_SNAPSHOT",
            "snapshot_method": "READ_ONLY_IMMUTABLE_URI",
            "immutable_open": True,
            "input_precondition": "CALLER_SELECTED_SEALED_HISTORICAL_SOURCE",
            "after_read": current_boundary,
            "sqlite_integrity_check": "PASS",
            "explicit_references_followed": explicit_references_followed,
        },
        tuple(["SCHEMA_UNSUPPORTED"] if unsupported_tables else []),
        captured_content=contents,
    )
    return source, records, gaps, source.read_boundary


def _capture_source(
    path: Path,
    allowed_root: Path,
    run_id: str,
    capture_id: str,
    source_index: int,
    explicit: bool,
) -> tuple[RawSource, list[AuditRecord], list[SourceGap], dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        try:
            return _capture_task3_database(path, allowed_root, run_id, source_index)
        except apsw.Error as error:
            source_id = f"source-{source_index}"
            gap = SourceGap(
                "SOURCE_CORRUPT",
                str(path),
                {"run_id": run_id},
                {"reason": str(error), "format": "TASK3_SQLITE"},
            )
            try:
                contents, boundary = _file_snapshot(path, allowed_root)
            except OSError:
                contents, boundary = None, {"locator": str(path), "status": "SOURCE_UNAVAILABLE"}
            source = RawSource(
                source_id,
                "TASK3_SQLITE",
                str(path),
                "SOURCE_CORRUPT",
                _sha256(contents) if contents is not None else None,
                None,
                {},
                (),
                boundary,
                ("SQLITE_READ_FAILED",),
                captured_content=contents,
            )
            return source, [], [gap], boundary
    return _capture_json_source(path, allowed_root, run_id, capture_id, source_index, explicit)


def capture_run_sources(
    run_id: str,
    repository: Path,
    explicit_sources: Iterable[Path] = (),
) -> RawAuditSources:
    if not _is_valid_run_id(run_id):
        raise ValueError("run_id must be a non-empty path-safe native identifier")
    repository = Path(repository).resolve(strict=True)
    if not repository.is_dir():
        raise NotADirectoryError(f"repository path is not a directory: {repository}")
    capture_id = uuid.uuid4().hex
    requested: list[tuple[Path, Path, bool]] = []
    probed_locations: list[dict[str, Any]] = []
    run_directory = repository / ".agent" / "runs" / run_id
    trace_path = run_directory / "trace.json"
    if trace_path.exists():
        requested.append((trace_path, repository, False))
    else:
        probed_locations.append({"locator": str(trace_path), "availability": "NOT_FOUND"})
    memory_directory = repository / ".agent" / "memory"
    for name in ("decisions.json", "failures.json", "successful_patterns.json"):
        memory_path = memory_directory / name
        if memory_path.exists():
            requested.append((memory_path, repository, False))
        else:
            probed_locations.append({"locator": str(memory_path), "availability": "NOT_FOUND"})
    for source in explicit_sources:
        source_path = Path(source)
        requested.append((source_path, source_path.parent, True))

    selected: list[RawSource] = []
    records: list[AuditRecord] = []
    gaps: list[SourceGap] = []
    boundaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, allowed_root, explicit in requested:
        key = str(path.resolve()) if path.exists() else str(path.absolute())
        if key in seen:
            continue
        seen.add(key)
        source, captured_records, source_gaps, boundary = _capture_source(
            path,
            allowed_root,
            run_id,
            capture_id,
            len(selected) + 1,
            explicit,
        )
        selected.append(source)
        records.extend(captured_records)
        gaps.extend(source_gaps)
        boundaries.append(boundary)

    target_matches = [
        record
        for record in records
        if record.native_scope.get("run_id") == run_id
    ]
    trace_header_matches = any(
        source.source_kind in {"TRACE_V2", "TRACE_UNSUPPORTED"}
        and source.native_identity.get("run_id") == run_id
        for source in selected
    )
    target_present = bool(target_matches) or trace_header_matches
    if not target_present and not any(gap.code == "IDENTITY_CONFLICT" for gap in gaps):
        gaps.append(SourceGap("RUN_NOT_FOUND", None, {"run_id": run_id}, {"selection": "exact run identity"}))

    included_classes = tuple(sorted({source.source_kind for source in selected}))
    followed_references = tuple(
        {
            "source_id": source.source_id,
            **reference,
        }
        for source, boundary in zip(selected, boundaries)
        for reference in boundary.get("explicit_references_followed", [])
    )
    unavailable = tuple(gap.to_dict() for gap in gaps if gap.code in {"SOURCE_UNAVAILABLE", "READ_ONLY_PRECONDITION_FAILED"})
    limitations = ["INDEPENDENT_EXPORT_UNIVERSE_NOT_ENUMERATED"]
    if any(source.source_kind == "TASK3_SQLITE" for source in selected):
        limitations.append("TASK3_DATABASE_AND_OTHER_RUN_SOURCES_ARE_NOT_BOUND_TO_ONE_COMMIT")
    discovery = AuditDiscoveryScope(
        {"run_id": run_id, "namespace": str(repository)},
        {"run_id": run_id, "scope": "RUN"},
        capture_id,
        included_classes,
        tuple(
            probed_locations
            + [
                {"locator": source.locator, "source_id": source.source_id, "availability": source.availability}
                for source in selected
            ]
        ),
        followed_references,
        (),
        unavailable,
        tuple(
            ["EXACT_RUN_DIRECTORY", "FIXED_MEMORY_COLLECTIONS"]
            + (["EXPLICIT_STORED_REFERENCES"] if followed_references else [])
            + ["EXPLICIT_SOURCE" for item in requested if item[2]]
        ),
        "DISCOVERY_UNKNOWN",
        (),
        tuple(limitations),
    )
    if any(gap.code in {"SOURCE_CORRUPT", "IDENTITY_CONFLICT"} for gap in gaps):
        capture_status = "INVALID"
    elif not target_present:
        capture_status = "UNAVAILABLE"
    else:
        capture_status = "PARTIAL"
    return RawAuditSources(
        capture_id,
        run_id,
        tuple(selected),
        tuple(records),
        tuple(gaps),
        tuple(boundaries),
        discovery,
        capture_status,
    )

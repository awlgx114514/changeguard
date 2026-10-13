from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


SLOT_COUNT = 32


class CampaignStateError(RuntimeError):
    pass


class SlotState(str, Enum):
    UNCLAIMED = "UNCLAIMED"
    CLAIMED = "CLAIMED"
    TERMINAL = "TERMINAL"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_PROVEN = "NOT_PROVEN"
    INVALID = "INVALID"


class CampaignProfileType(str, Enum):
    CORRECT = "CORRECT"
    WRONG = "WRONG"


@dataclass(frozen=True)
class CampaignIdentity:
    campaign_id: str
    semantic_spec_identity: str
    benchmark_contract_identity: str
    scoring_contract_identity: str
    calibration_campaign_identity: str
    deadline_formula_identity: str
    accepted_asset_identities: tuple[str, ...]
    environment_profile_identity: str
    formal_profile_identity: str
    profile_type: CampaignProfileType = CampaignProfileType.CORRECT
    profile_catalog_version: str | None = None
    profile_catalog_hash: str | None = None
    profile_id: str | None = None

    def __post_init__(self) -> None:
        if not self.campaign_id:
            raise ValueError("campaign_id is required")
        scalar_fields = (
            self.semantic_spec_identity,
            self.benchmark_contract_identity,
            self.scoring_contract_identity,
            self.calibration_campaign_identity,
            self.deadline_formula_identity,
            self.environment_profile_identity,
            self.formal_profile_identity,
        )
        if any(not value for value in scalar_fields):
            raise ValueError("sealed campaign identity fields are required")
        if not self.accepted_asset_identities or any(
            not value for value in self.accepted_asset_identities
        ):
            raise ValueError("accepted asset identities are required")
        if len(self.accepted_asset_identities) != 1:
            raise ValueError("each sealed campaign must bind exactly one asset identity")
        if len(set(self.accepted_asset_identities)) != len(self.accepted_asset_identities):
            raise ValueError("accepted asset identities must be unique")
        if not isinstance(self.profile_type, CampaignProfileType):
            object.__setattr__(self, "profile_type", CampaignProfileType(self.profile_type))
        catalog_binding = (
            self.profile_catalog_version,
            self.profile_catalog_hash,
            self.profile_id,
        )
        if any(value is not None for value in catalog_binding) and any(
            not value for value in catalog_binding
        ):
            raise ValueError("profile catalog binding fields must be complete")
        if self.profile_catalog_hash is not None and (
            len(self.profile_catalog_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.profile_catalog_hash)
        ):
            raise ValueError("profile_catalog_hash must be a lowercase SHA256 digest")


@dataclass(frozen=True)
class SlotClaim:
    campaign_id: str
    slot_id: int
    run_id: str
    asset_identity: str
    environment_identity: str
    claim_metadata: dict[str, Any]
    claim_timestamp_utc: str


@dataclass(frozen=True)
class SlotSnapshot:
    campaign_id: str
    slot_id: int
    state: SlotState
    run_id: str | None
    asset_identity: str | None
    environment_identity: str | None
    claim_metadata: dict[str, Any] | None
    claim_timestamp_utc: str | None
    start_record: dict[str, Any] | None
    terminal_decision_identity: str | None
    verdict: Verdict | None


@dataclass(frozen=True)
class ExecutionStartRecord:
    slot_id: int
    run_id: str
    fixture_identity: str
    asset_identity: str
    environment_identity: str
    launch_plan_identity: str
    launch_identity: str
    execution_owner_identity: str
    committed: bool
    launch_permitted: bool


@dataclass(frozen=True)
class TerminalDecision:
    terminal_decision_identity: str
    campaign_id: str
    run_id: str
    slot_id: int
    case_id: str
    interaction_id: str
    interaction_generation: int
    allocation_identity: str
    snapshot_identity: str
    record_commit_identity: str
    verdict: Verdict
    decision_reason: str
    evidence_ids: tuple[str, ...]
    fail_attribution: str | None = None
    component_results: tuple[dict[str, Any], ...] = ()
    attribution_result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        required = (
            self.terminal_decision_identity,
            self.campaign_id,
            self.run_id,
            self.case_id,
            self.interaction_id,
            self.allocation_identity,
            self.snapshot_identity,
            self.record_commit_identity,
            self.decision_reason,
        )
        if any(not value for value in required):
            raise ValueError("terminal decision identity and reason fields are required")
        if self.interaction_generation < 1:
            raise ValueError("interaction_generation must be positive")
        if self.verdict is Verdict.FAIL and self.fail_attribution not in {
            "confirmed_rejection",
            "unattributed_fail",
        }:
            raise ValueError("FAIL decisions require frozen fail attribution")
        if self.verdict is not Verdict.FAIL and self.fail_attribution is not None:
            raise ValueError("fail attribution is valid only for FAIL decisions")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence identifiers must be unique")


@dataclass(frozen=True)
class CampaignCounts:
    preregistered: int
    attempted: int
    terminal: int
    pending: int
    unclaimed: int
    verdicts: dict[str, int]
    confirmed_rejections: int
    unattributed_failures: int
    admission: str


@dataclass(frozen=True)
class RunEvidence:
    evidence_id: str
    campaign_id: str
    slot_id: int
    run_id: str
    asset_identity: str
    environment_identity: str
    case_id: str
    interaction_id: str
    interaction_type: str
    interaction_generation: int
    allocation_identity: str
    snapshot_identity: str
    record_commit_identity: str
    observation_state: str
    semantic_timeout: str
    completion_kind: str | None
    measurement_profile_identity: str
    deadline_formula_version: str
    clock_identity: str
    counter_frequency: int
    budget_ns: int
    lifecycle_evidence_ids: tuple[str, ...]
    lifecycle_required: bool

    @classmethod
    def from_snapshot(
        cls,
        *,
        slot_id: int,
        asset_identity: str,
        environment_identity: str,
        identity: Any,
        snapshot: Any,
        profile: Any,
        lifecycle_evidence_ids: tuple[str, ...] = (),
        lifecycle_required: bool = False,
    ) -> "RunEvidence":
        if identity.slot_id != slot_id:
            raise ValueError("completion identity slot differs from evidence slot")
        if snapshot.budget_ns is not None and snapshot.budget_ns != profile.budget_ns:
            raise ValueError("terminal observation budget differs from supervisor profile")
        snapshot_data = asdict(snapshot)
        snapshot_identity = _digest(_json(snapshot_data))
        payload = snapshot.payload
        if payload is not None and (
            payload.campaign_id != identity.campaign_id
            or payload.slot_id != identity.slot_id
            or payload.run_id != identity.run_id
            or payload.case_id != identity.case_id
            or payload.interaction_id != identity.interaction_id
            or payload.interaction_generation != identity.interaction_generation
            or payload.allocation_identity != identity.allocation_identity
            or payload.record_commit_identity != identity.record_commit_identity
            or payload.measurement_profile_identity != profile.profile_identity
            or payload.deadline_formula_version != profile.formula_version
            or payload.clock_identity != profile.clock_identity
            or payload.counter_frequency != profile.counter_frequency
        ):
            raise ValueError("terminal observation does not match its identity-bound profile")
        evidence_data = {
            "campaign_id": identity.campaign_id,
            "slot_id": slot_id,
            "run_id": identity.run_id,
            "asset_identity": asset_identity,
            "environment_identity": environment_identity,
            "case_id": identity.case_id,
            "interaction_id": identity.interaction_id,
            "interaction_type": identity.interaction_type,
            "interaction_generation": identity.interaction_generation,
            "allocation_identity": identity.allocation_identity,
            "snapshot_identity": snapshot_identity,
            "record_commit_identity": identity.record_commit_identity,
            "observation_state": snapshot.state,
            "semantic_timeout": snapshot.semantic_timeout,
            "completion_kind": payload.completion_kind if payload else None,
            "measurement_profile_identity": profile.profile_identity,
            "deadline_formula_version": profile.formula_version,
            "clock_identity": profile.clock_identity,
            "counter_frequency": profile.counter_frequency,
            "budget_ns": profile.budget_ns,
            "lifecycle_evidence_ids": lifecycle_evidence_ids,
            "lifecycle_required": lifecycle_required,
        }
        evidence_id = _digest(_json(evidence_data))
        return cls(evidence_id=evidence_id, **evidence_data)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_dict(identity: CampaignIdentity) -> dict[str, Any]:
    values = asdict(identity)
    if all(
        values[field] is None
        for field in (
            "profile_catalog_version",
            "profile_catalog_hash",
            "profile_id",
        )
    ):
        values.pop("profile_catalog_version")
        values.pop("profile_catalog_hash")
        values.pop("profile_id")
    return values


def _identity_json(identity: CampaignIdentity) -> str:
    return _json(_identity_dict(identity))


def _experiment_identity_hash(identity: CampaignIdentity) -> str:
    values = _identity_dict(identity)
    values.pop("campaign_id")
    return _digest(_json(values))


def _decision_dict(decision: TerminalDecision) -> dict[str, Any]:
    data = asdict(decision)
    data["verdict"] = decision.verdict.value
    return data


class CampaignStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._direct_child_handles: dict[tuple[str, int, str, str, int], Any] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _require_claim_binding(
        self, connection: sqlite3.Connection, claim: SlotClaim
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT state, run_id, asset_identity, environment_identity, claim_metadata, "
            "claim_timestamp_utc FROM slots WHERE campaign_id=? AND slot_id=?",
            (claim.campaign_id, claim.slot_id),
        ).fetchone()
        if (
            not row
            or row["state"] != SlotState.CLAIMED.value
            or row["run_id"] != claim.run_id
            or row["asset_identity"] != claim.asset_identity
            or row["environment_identity"] != claim.environment_identity
            or row["claim_metadata"] != _json(claim.claim_metadata)
            or row["claim_timestamp_utc"] != claim.claim_timestamp_utc
        ):
            raise CampaignStateError("slot claim differs from the committed run binding")
        return row

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    identity_json TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('OPEN', 'STOPPED', 'CLOSED')),
                    stop_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS slots (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                    slot_id INTEGER NOT NULL CHECK (slot_id >= 0 AND slot_id < 32),
                    state TEXT NOT NULL CHECK (state IN ('UNCLAIMED', 'CLAIMED', 'TERMINAL')),
                    run_id TEXT,
                    asset_identity TEXT,
                    environment_identity TEXT,
                    claim_metadata TEXT,
                    claim_timestamp_utc TEXT,
                    start_record TEXT,
                    terminal_decision_identity TEXT,
                    verdict TEXT,
                    PRIMARY KEY (campaign_id, slot_id),
                    UNIQUE (campaign_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS terminal_decisions (
                    terminal_decision_identity TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE (campaign_id, run_id),
                    FOREIGN KEY (campaign_id, slot_id)
                        REFERENCES slots(campaign_id, slot_id)
                );
                CREATE TABLE IF NOT EXISTS run_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    interaction_id TEXT NOT NULL,
                    interaction_generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE (campaign_id, slot_id, interaction_id, interaction_generation),
                    FOREIGN KEY (campaign_id, slot_id)
                        REFERENCES slots(campaign_id, slot_id)
                );
                CREATE TABLE IF NOT EXISTS direct_child_bindings (
                    campaign_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    interaction_id TEXT NOT NULL,
                    interaction_generation INTEGER NOT NULL,
                    transaction_identity TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, slot_id, run_id, interaction_id, interaction_generation),
                    FOREIGN KEY (campaign_id, slot_id)
                        REFERENCES slots(campaign_id, slot_id)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    interaction_id TEXT NOT NULL,
                    interaction_generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    UNIQUE (campaign_id, slot_id, run_id, interaction_id, interaction_generation),
                    FOREIGN KEY (campaign_id, slot_id)
                        REFERENCES slots(campaign_id, slot_id)
                );
                CREATE TABLE IF NOT EXISTS registered_asset_defects (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
                    asset_identity TEXT NOT NULL,
                    defect_id TEXT NOT NULL,
                    interaction_type TEXT NOT NULL,
                    required_fact TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, asset_identity, defect_id)
                );
                CREATE TABLE IF NOT EXISTS measurement_profiles (
                    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(campaign_id),
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prepared_terminal_decisions (
                    terminal_decision_identity TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('PREPARED', 'RECEIPT', 'COMMITTED')),
                    receipt_identity TEXT,
                    UNIQUE (campaign_id, run_id),
                    FOREIGN KEY (campaign_id, slot_id)
                        REFERENCES slots(campaign_id, slot_id)
                );
                CREATE TABLE IF NOT EXISTS diagnostics (
                    diagnostic_id TEXT PRIMARY KEY,
                    terminal_decision_identity TEXT NOT NULL
                        REFERENCES terminal_decisions(terminal_decision_identity),
                    event_type TEXT NOT NULL,
                    event_payload TEXT NOT NULL
                );
                """
            )

    def register(self, identity: CampaignIdentity) -> None:
        identity_json = _identity_json(identity)
        identity_hash = _experiment_identity_hash(identity)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT identity_json FROM campaigns WHERE campaign_id = ?",
                (identity.campaign_id,),
            ).fetchone()
            if current:
                if current["identity_json"] != identity_json:
                    raise CampaignStateError("campaign identity cannot be changed")
                connection.commit()
                return
            prior = connection.execute(
                "SELECT campaign_id FROM campaigns WHERE identity_hash = ?",
                (identity_hash,),
            ).fetchone()
            if prior:
                raise CampaignStateError("campaign identity churn cannot create a replacement campaign")
            connection.execute(
                "INSERT INTO campaigns VALUES (?, ?, ?, 'OPEN', NULL)",
                (identity.campaign_id, identity_json, identity_hash),
            )
            connection.executemany(
                "INSERT INTO slots (campaign_id, slot_id, state) VALUES (?, ?, 'UNCLAIMED')",
                ((identity.campaign_id, slot_id) for slot_id in range(SLOT_COUNT)),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_slot(
        self,
        *,
        campaign_id: str,
        slot_id: int,
        run_id: str,
        asset_identity: str,
        environment_identity: str,
        claim_metadata: dict[str, Any],
    ) -> SlotClaim:
        if not run_id or not asset_identity or not environment_identity:
            raise ValueError("claim identities are required")
        claim_timestamp_utc = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT identity_json, state FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise CampaignStateError("campaign is not registered")
            if campaign["state"] != "OPEN":
                raise CampaignStateError("campaign does not accept new slot claims")
            identity = json.loads(campaign["identity_json"])
            profile_row = connection.execute(
                "SELECT payload_json, payload_hash FROM measurement_profiles WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if (
                profile_row is None
                or _digest(profile_row["payload_json"]) != profile_row["payload_hash"]
            ):
                raise CampaignStateError(
                    "supervisor measurement profile must be registered before slot claims"
                )
            profile = json.loads(profile_row["payload_json"])
            if (
                profile["profile_identity"] != identity["formal_profile_identity"]
                or profile["formula_version"] != identity["deadline_formula_identity"]
            ):
                raise CampaignStateError(
                    "supervisor measurement profile differs from the sealed campaign"
                )
            if asset_identity not in identity["accepted_asset_identities"]:
                raise CampaignStateError("claim asset identity is outside the sealed campaign")
            if environment_identity != identity["environment_profile_identity"]:
                raise CampaignStateError("claim environment identity differs from the sealed profile")
            cursor = connection.execute(
                """UPDATE slots SET state='CLAIMED', run_id=?, asset_identity=?,
                   environment_identity=?, claim_metadata=?, claim_timestamp_utc=?
                   WHERE campaign_id=? AND slot_id=? AND state='UNCLAIMED'""",
                (
                    run_id,
                    asset_identity,
                    environment_identity,
                    _json(claim_metadata),
                    claim_timestamp_utc,
                    campaign_id,
                    slot_id,
                ),
            )
            if cursor.rowcount != 1:
                raise CampaignStateError("slot is already claimed or terminal")
            connection.commit()
            return SlotClaim(
                campaign_id,
                slot_id,
                run_id,
                asset_identity,
                environment_identity,
                claim_metadata,
                claim_timestamp_utc,
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_execution(
        self,
        claim: SlotClaim,
        *,
        fixture_identity: str,
        launch_plan_identity: str,
        launch_identity: str,
        execution_owner_identity: str,
    ) -> ExecutionStartRecord:
        values = (
            fixture_identity,
            launch_plan_identity,
            launch_identity,
            execution_owner_identity,
        )
        if any(not value for value in values):
            raise ValueError("candidate execution start identities are required")
        record = {
            "slot_id": claim.slot_id,
            "run_id": claim.run_id,
            "fixture_identity": fixture_identity,
            "asset_identity": claim.asset_identity,
            "environment_identity": claim.environment_identity,
            "launch_plan_identity": launch_plan_identity,
            "launch_identity": launch_identity,
            "execution_owner_identity": execution_owner_identity,
        }
        encoded = _json(record)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_claim_binding(connection, claim)
            start_record = connection.execute(
                "SELECT start_record FROM slots WHERE campaign_id=? AND slot_id=?",
                (claim.campaign_id, claim.slot_id),
            ).fetchone()["start_record"]
            if start_record is not None:
                if start_record != encoded:
                    raise CampaignStateError("execution start record is immutable")
                connection.commit()
                return ExecutionStartRecord(**record, committed=True, launch_permitted=False)
            connection.execute(
                "UPDATE slots SET start_record=? WHERE campaign_id=? AND slot_id=? AND start_record IS NULL",
                (encoded, claim.campaign_id, claim.slot_id),
            )
            connection.commit()
            return ExecutionStartRecord(**record, committed=True, launch_permitted=True)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def register_asset_lifecycle_defect(
        self,
        campaign_id: str,
        *,
        asset_identity: str,
        defect_id: str,
        interaction_type: str,
        required_fact: Any,
    ) -> None:
        from benchmarks.task3_v6.measurement import LifecycleFact

        if not asset_identity or not defect_id or not interaction_type:
            raise CampaignStateError("registered lifecycle defect identity is required")
        fact = LifecycleFact(required_fact)
        record = {
            "campaign_id": campaign_id,
            "asset_identity": asset_identity,
            "defect_id": defect_id,
            "interaction_type": interaction_type,
            "required_fact": fact.value,
        }
        encoded = _json(record)
        payload_hash = _digest(encoded)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            campaign_row = connection.execute(
                "SELECT identity_json FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if campaign_row is None:
                raise CampaignStateError("campaign is not registered")
            identity = json.loads(campaign_row["identity_json"])
            if asset_identity not in identity["accepted_asset_identities"]:
                raise CampaignStateError("lifecycle defect asset is not accepted by the campaign")
            claimed = connection.execute(
                "SELECT 1 FROM slots WHERE campaign_id=? AND state!='UNCLAIMED' LIMIT 1",
                (campaign_id,),
            ).fetchone()
            if claimed:
                raise CampaignStateError("lifecycle defects must be registered before slot claims")
            previous = connection.execute(
                "SELECT payload_json, payload_hash FROM registered_asset_defects "
                "WHERE campaign_id=? AND asset_identity=? AND defect_id=?",
                (campaign_id, asset_identity, defect_id),
            ).fetchone()
            if previous:
                if previous["payload_json"] != encoded or previous["payload_hash"] != payload_hash:
                    raise CampaignStateError("registered lifecycle defect is immutable")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO registered_asset_defects VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    campaign_id,
                    asset_identity,
                    defect_id,
                    interaction_type,
                    fact.value,
                    encoded,
                    payload_hash,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def bind_direct_child(self, claim: SlotClaim, identity: Any, process: Any) -> None:
        from benchmarks.task3_v6.measurement import StableChildProcessHandle

        if (
            identity.campaign_id != claim.campaign_id
            or identity.slot_id != claim.slot_id
            or identity.run_id != claim.run_id
        ):
            raise CampaignStateError("direct child binding differs from the claimed run")
        handle = StableChildProcessHandle(process)
        record = {
            "campaign_id": claim.campaign_id,
            "slot_id": claim.slot_id,
            "run_id": claim.run_id,
            "case_id": identity.case_id,
            "interaction_id": identity.interaction_id,
            "interaction_generation": identity.interaction_generation,
            "transaction_identity": identity.record_commit_identity,
            "child_process_identity": handle.child_process_identity,
            "child_pid": handle.pid,
            "child_creation_identity": handle.creation_identity,
            "stable_handle_binding_identity": handle.stable_handle_binding_identity,
            "observation_authority": identity.measurement_observation_owner_identity,
            "observation_source": "SUPERVISOR_TRUSTED_OS",
        }
        encoded = _json(record)
        payload_hash = _digest(encoded)
        key = (
            claim.campaign_id,
            claim.slot_id,
            claim.run_id,
            identity.interaction_id,
            identity.interaction_generation,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_claim_binding(connection, claim)
            start = connection.execute(
                "SELECT start_record FROM slots WHERE campaign_id=? AND slot_id=?",
                (claim.campaign_id, claim.slot_id),
            ).fetchone()
            if start is None or start["start_record"] is None:
                raise CampaignStateError("direct child binding requires a committed execution start")
            previous = connection.execute(
                "SELECT payload_json, payload_hash FROM direct_child_bindings "
                "WHERE campaign_id=? AND slot_id=? AND run_id=? AND interaction_id=? "
                "AND interaction_generation=?",
                key,
            ).fetchone()
            if previous:
                if previous["payload_json"] != encoded or previous["payload_hash"] != payload_hash:
                    raise CampaignStateError("direct child binding is immutable")
                connection.commit()
                handle.close()
                return
            connection.execute(
                "INSERT INTO direct_child_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim.campaign_id,
                    claim.slot_id,
                    claim.run_id,
                    identity.case_id,
                    identity.interaction_id,
                    identity.interaction_generation,
                    identity.record_commit_identity,
                    encoded,
                    payload_hash,
                ),
            )
            connection.commit()
            self._direct_child_handles[key] = handle
        except BaseException:
            connection.rollback()
            handle.close()
            raise
        finally:
            connection.close()

    def record_observation(
        self,
        claim: SlotClaim,
        identity: Any,
        observation_authority: Any,
        *,
        process: Any | None = None,
    ) -> RunEvidence:
        if (
            identity.campaign_id != claim.campaign_id
            or identity.slot_id != claim.slot_id
            or identity.run_id != claim.run_id
        ):
            raise CampaignStateError("observation identity does not bind to the claimed run")
        observed = observation_authority.read(identity)
        if observed is None or observed[0] != "finished" or observed[1] is None:
            raise CampaignStateError("terminal observation is not durably available")
        key = (
            claim.campaign_id,
            claim.slot_id,
            claim.run_id,
            identity.interaction_id,
            identity.interaction_generation,
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json, payload_hash FROM measurement_profiles WHERE campaign_id=?",
                (claim.campaign_id,),
            ).fetchone()
            binding_row = connection.execute(
                "SELECT payload_json, payload_hash FROM direct_child_bindings "
                "WHERE campaign_id=? AND slot_id=? AND run_id=? AND interaction_id=? "
                "AND interaction_generation=?",
                key,
            ).fetchone()
        if row is None or _digest(row["payload_json"]) != row["payload_hash"]:
            raise CampaignStateError("supervisor measurement profile is missing or invalid")
        from benchmarks.task3_v6.measurement import MeasurementProfile

        profile = MeasurementProfile(**json.loads(row["payload_json"]))
        lifecycle = None
        handle = self._direct_child_handles.get(key)
        if binding_row is not None:
            if _digest(binding_row["payload_json"]) != binding_row["payload_hash"]:
                raise CampaignStateError("registered direct child binding integrity check failed")
            binding = json.loads(binding_row["payload_json"])
            if (
                binding["transaction_identity"] != identity.record_commit_identity
                or binding["observation_authority"]
                != identity.measurement_observation_owner_identity
                or binding["observation_source"] != "SUPERVISOR_TRUSTED_OS"
            ):
                raise CampaignStateError("direct child binding differs from the completion transaction")
            snapshot = observed[1]
            if (
                handle is not None
                and process is not None
                and snapshot.state == "COMMITTED"
                and snapshot.payload is not None
                and snapshot.payload.completion_kind == "returned"
            ):
                from benchmarks.task3_v6.measurement import StableChildProcessHandle

                if not isinstance(handle, StableChildProcessHandle):
                    raise CampaignStateError("direct child handle is not supervisor-owned")
                snapshot_identity = _digest(_json(asdict(snapshot)))
                lifecycle = handle.observe_first_wait_zero(
                    campaign_id=claim.campaign_id,
                    slot_id=claim.slot_id,
                    run_id=claim.run_id,
                    case_id=identity.case_id,
                    interaction_id=identity.interaction_id,
                    interaction_generation=identity.interaction_generation,
                    asset_identity=claim.asset_identity,
                    observation_authority=identity.measurement_observation_owner_identity,
                    transaction_identity=identity.record_commit_identity,
                    transaction_completion_identity=snapshot_identity,
                    process=process,
                )
        evidence = RunEvidence.from_snapshot(
            slot_id=claim.slot_id,
            asset_identity=claim.asset_identity,
            environment_identity=claim.environment_identity,
            identity=identity,
            snapshot=observed[1],
            profile=profile,
            lifecycle_evidence_ids=(lifecycle.evidence_id,) if lifecycle else (),
            lifecycle_required=binding_row is not None,
        )
        self._record_run_evidence(claim, evidence, lifecycle)
        if handle is not None:
            handle.close()
            self._direct_child_handles.pop(key, None)
        return evidence

    def mark_direct_child_cleanup(
        self, claim: SlotClaim, identity: Any
    ) -> None:
        key = (
            claim.campaign_id,
            claim.slot_id,
            claim.run_id,
            identity.interaction_id,
            identity.interaction_generation,
        )
        handle = self._direct_child_handles.get(key)
        if handle is None:
            raise CampaignStateError("supervisor stable child handle is unavailable")
        handle.mark_cleanup_started()

    def _record_run_evidence(
        self, claim: SlotClaim, evidence: RunEvidence, lifecycle: Any | None = None
    ) -> None:
        if (
            evidence.campaign_id != claim.campaign_id
            or evidence.slot_id != claim.slot_id
            or evidence.run_id != claim.run_id
            or evidence.asset_identity != claim.asset_identity
            or evidence.environment_identity != claim.environment_identity
        ):
            raise CampaignStateError("run evidence does not bind to the claimed run")
        if (
            not evidence.evidence_id
            or not evidence.case_id
            or not evidence.interaction_id
            or not evidence.interaction_type
            or not evidence.allocation_identity
            or not evidence.snapshot_identity
            or not evidence.record_commit_identity
            or type(evidence.interaction_generation) is not int
            or evidence.interaction_generation < 1
            or evidence.observation_state
            not in {"COMMITTED", "STAGING", "INVALID_RECORD", "INFRASTRUCTURE_FAILURE"}
            or evidence.semantic_timeout
            not in {"CONFIRMED", "NOT_CONFIRMED", "UNPROVEN"}
            or type(evidence.lifecycle_required) is not bool
            or len(set(evidence.lifecycle_evidence_ids)) != len(evidence.lifecycle_evidence_ids)
            or type(evidence.counter_frequency) is not int
            or evidence.counter_frequency <= 0
            or type(evidence.budget_ns) is not int
            or evidence.budget_ns <= 0
            or not evidence.measurement_profile_identity
            or not evidence.deadline_formula_version
            or not evidence.clock_identity
            or (
                evidence.observation_state == "COMMITTED"
                and evidence.completion_kind not in {"returned", "registered_exception"}
            )
            or (
                evidence.observation_state != "COMMITTED"
                and evidence.semantic_timeout != "UNPROVEN"
            )
        ):
            raise CampaignStateError("run evidence does not contain a valid observation")
        encoded = _json(asdict(evidence))
        payload_hash = _digest(encoded)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_claim_binding(connection, claim)
            profile_row = connection.execute(
                "SELECT payload_json, payload_hash FROM measurement_profiles WHERE campaign_id=?",
                (claim.campaign_id,),
            ).fetchone()
            if profile_row is None or _digest(profile_row["payload_json"]) != profile_row["payload_hash"]:
                raise CampaignStateError("supervisor measurement profile is missing or invalid")
            profile = json.loads(profile_row["payload_json"])
            campaign_row = connection.execute(
                "SELECT identity_json FROM campaigns WHERE campaign_id=?",
                (claim.campaign_id,),
            ).fetchone()
            campaign_identity = json.loads(campaign_row["identity_json"])
            if (
                profile["profile_identity"] != campaign_identity["formal_profile_identity"]
                or profile["formula_version"] != campaign_identity["deadline_formula_identity"]
                or evidence.measurement_profile_identity != profile["profile_identity"]
                or evidence.deadline_formula_version != profile["formula_version"]
                or evidence.clock_identity != profile["clock_identity"]
                or evidence.counter_frequency != profile["counter_frequency"]
                or evidence.budget_ns != profile["budget_ns"]
            ):
                raise CampaignStateError("run evidence differs from the registered measurement profile")
            row = connection.execute(
                "SELECT start_record FROM slots WHERE campaign_id=? AND slot_id=?",
                (claim.campaign_id, claim.slot_id),
            ).fetchone()
            if row["start_record"] is None:
                raise CampaignStateError("run evidence requires a committed execution start")
            start = json.loads(row["start_record"])
            if (
                start["run_id"] != evidence.run_id
                or start["slot_id"] != evidence.slot_id
                or start["asset_identity"] != evidence.asset_identity
                or start["environment_identity"] != evidence.environment_identity
            ):
                raise CampaignStateError("run evidence differs from the committed execution start")
            binding_row = connection.execute(
                "SELECT payload_json, payload_hash FROM direct_child_bindings "
                "WHERE campaign_id=? AND slot_id=? AND run_id=? AND interaction_id=? "
                "AND interaction_generation=?",
                (
                    claim.campaign_id,
                    claim.slot_id,
                    claim.run_id,
                    evidence.interaction_id,
                    evidence.interaction_generation,
                ),
            ).fetchone()
            if evidence.lifecycle_required != (binding_row is not None):
                raise CampaignStateError("run evidence lifecycle requirement differs from supervisor binding")
            if lifecycle is None and evidence.lifecycle_evidence_ids:
                raise CampaignStateError("run evidence references an unavailable lifecycle artifact")
            if lifecycle is not None:
                if not evidence.lifecycle_required or evidence.lifecycle_evidence_ids != (
                    lifecycle.evidence_id,
                ):
                    raise CampaignStateError("lifecycle artifact is not referenced by current run evidence")
                lifecycle_record = asdict(lifecycle)
                lifecycle_record["lifecycle_fact"] = lifecycle.lifecycle_fact.value
                lifecycle_identity = dict(lifecycle_record)
                lifecycle_identity.pop("evidence_id")
                if (
                    lifecycle.campaign_id != claim.campaign_id
                    or lifecycle.slot_id != claim.slot_id
                    or lifecycle.run_id != claim.run_id
                    or lifecycle.case_id != evidence.case_id
                    or lifecycle.interaction_id != evidence.interaction_id
                    or lifecycle.interaction_generation != evidence.interaction_generation
                    or lifecycle.asset_identity != claim.asset_identity
                    or lifecycle.transaction_identity != evidence.record_commit_identity
                    or lifecycle.transaction_completion_identity != evidence.snapshot_identity
                    or lifecycle.observation_source != "SUPERVISOR_TRUSTED_OS"
                    or lifecycle.observation_sequence != 1
                    or lifecycle.os_query_operation
                    not in {"WaitForSingleObject(handle, 0)", "NOT_PERFORMED"}
                    or lifecycle.child_binding_status not in {"VALID", "INVALID"}
                    or lifecycle.query_order_status
                    not in {"VALID_FIRST_EXTERNAL_CHECK", "INVALID_CLEANUP_PRECEDED_QUERY"}
                    or _digest(_json(lifecycle_identity)) != lifecycle.evidence_id
                ):
                    raise CampaignStateError("lifecycle artifact does not bind to current completion evidence")
                binding = json.loads(binding_row["payload_json"])
                if (
                    _digest(binding_row["payload_json"]) != binding_row["payload_hash"]
                    or lifecycle.child_process_identity != binding["child_process_identity"]
                    or lifecycle.child_pid != binding["child_pid"]
                    or lifecycle.child_creation_identity != binding["child_creation_identity"]
                    or lifecycle.stable_handle_binding_identity
                    != binding["stable_handle_binding_identity"]
                    or lifecycle.observation_authority != binding["observation_authority"]
                ):
                    raise CampaignStateError("lifecycle evidence differs from the registered child handle")
                lifecycle_payload = _json(lifecycle_record)
                lifecycle_hash = _digest(lifecycle_payload)
                previous_lifecycle = connection.execute(
                    "SELECT payload_json, payload_hash FROM lifecycle_evidence WHERE evidence_id=?",
                    (lifecycle.evidence_id,),
                ).fetchone()
                if previous_lifecycle and (
                    previous_lifecycle["payload_json"] != lifecycle_payload
                    or previous_lifecycle["payload_hash"] != lifecycle_hash
                ):
                    raise CampaignStateError("lifecycle evidence identity is bound to different content")
                if not previous_lifecycle:
                    connection.execute(
                        "INSERT INTO lifecycle_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            lifecycle.evidence_id,
                            claim.campaign_id,
                            claim.slot_id,
                            claim.run_id,
                            lifecycle.interaction_id,
                            lifecycle.interaction_generation,
                            lifecycle_payload,
                            lifecycle_hash,
                        ),
                    )
            previous = connection.execute(
                "SELECT payload_json, payload_hash FROM run_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if previous:
                if previous["payload_json"] != encoded or previous["payload_hash"] != payload_hash:
                    raise CampaignStateError("evidence identity is bound to different content")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO run_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.evidence_id,
                    claim.campaign_id,
                    claim.run_id,
                    claim.slot_id,
                    evidence.interaction_id,
                    evidence.interaction_generation,
                    encoded,
                    payload_hash,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_lifecycle_evidence(self, evidence_id: str) -> Any:
        from benchmarks.task3_v6.measurement import LifecycleEvidence, LifecycleFact

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json, payload_hash FROM lifecycle_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        if row is None or _digest(row["payload_json"]) != row["payload_hash"]:
            raise CampaignStateError("persisted lifecycle evidence is missing or invalid")
        payload = json.loads(row["payload_json"])
        if payload["evidence_id"] != evidence_id:
            raise CampaignStateError("persisted lifecycle evidence identifier differs")
        evidence_identity = dict(payload)
        evidence_identity.pop("evidence_id")
        if _digest(_json(evidence_identity)) != evidence_id:
            raise CampaignStateError("persisted lifecycle evidence content identity differs")
        payload["lifecycle_fact"] = LifecycleFact(payload["lifecycle_fact"])
        return LifecycleEvidence(**payload)

    def register_measurement_profile(self, campaign_id: str, profile: Any) -> None:
        profile_data = asdict(profile)
        if (
            not profile_data.get("profile_identity")
            or not profile_data.get("clock_identity")
            or not profile_data.get("formula_version")
            or type(profile_data.get("counter_frequency")) is not int
            or profile_data["counter_frequency"] <= 0
            or type(profile_data.get("budget_ns")) is not int
            or profile_data["budget_ns"] <= 0
        ):
            raise CampaignStateError("supervisor measurement profile is invalid")
        encoded = _json(profile_data)
        digest = _digest(encoded)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT identity_json FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if row is None:
                raise CampaignStateError("campaign is not registered")
            campaign_identity = json.loads(row["identity_json"])
            if (
                campaign_identity["formal_profile_identity"] != profile_data["profile_identity"]
                or campaign_identity["deadline_formula_identity"] != profile_data["formula_version"]
            ):
                raise CampaignStateError("measurement profile differs from the sealed campaign")
            previous = connection.execute(
                "SELECT payload_json, payload_hash FROM measurement_profiles WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if previous:
                if previous["payload_json"] != encoded or previous["payload_hash"] != digest:
                    raise CampaignStateError("supervisor measurement profile is immutable")
            else:
                claimed = connection.execute(
                    "SELECT COUNT(*) FROM slots WHERE campaign_id=? AND state!='UNCLAIMED'",
                    (campaign_id,),
                ).fetchone()[0]
                if claimed:
                    raise CampaignStateError(
                        "measurement profile must be registered before slot claims"
                    )
                connection.execute(
                    "INSERT INTO measurement_profiles VALUES (?, ?, ?)",
                    (campaign_id, encoded, digest),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_measurement_profile(self, campaign_id: str) -> Any:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json, payload_hash FROM measurement_profiles WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
        if row is None or _digest(row["payload_json"]) != row["payload_hash"]:
            raise CampaignStateError("supervisor measurement profile is missing or invalid")
        from benchmarks.task3_v6.measurement import MeasurementProfile

        return MeasurementProfile(**json.loads(row["payload_json"]))

    def terminalize(
        self, claim: SlotClaim, terminal_decision_identity: str
    ) -> TerminalDecision:
        if (
            not isinstance(terminal_decision_identity, str)
            or terminal_decision_identity in {value.value for value in Verdict}
            or terminal_decision_identity == "confirmed_rejection"
        ):
            raise CampaignStateError("terminal verdicts must be produced from stored run evidence")
        prepared = self.prepare_terminal_decision(claim, terminal_decision_identity)
        self.record_terminal_receipt(terminal_decision_identity)
        self.commit_terminal_decision(terminal_decision_identity)
        decision = self.get_terminal_decision(terminal_decision_identity)
        if decision is None:
            raise CampaignStateError("committed terminal decision could not be read")
        if decision != prepared:
            raise CampaignStateError("terminal decision changed during persistence")
        return decision

    def prepare_terminal_decision(
        self, claim: SlotClaim, terminal_decision_identity: str
    ) -> TerminalDecision:
        if (
            not isinstance(terminal_decision_identity, str)
            or not terminal_decision_identity
            or terminal_decision_identity in {value.value for value in Verdict}
            or terminal_decision_identity == "confirmed_rejection"
        ):
            raise CampaignStateError("terminal decision identity is required")
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT payload_json FROM prepared_terminal_decisions "
                "WHERE terminal_decision_identity=?",
                (terminal_decision_identity,),
            ).fetchone()
        if existing:
            existing_data = json.loads(existing["payload_json"])
            if (
                existing_data["campaign_id"] != claim.campaign_id
                or existing_data["run_id"] != claim.run_id
                or existing_data["slot_id"] != claim.slot_id
            ):
                raise CampaignStateError("prepared decision belongs to a different claimed run")
            existing_data["verdict"] = Verdict(existing_data["verdict"])
            existing_data["evidence_ids"] = tuple(existing_data["evidence_ids"])
            existing_data["component_results"] = tuple(existing_data["component_results"])
            for component in existing_data["component_results"]:
                component["evidence_ids"] = tuple(component["evidence_ids"])
            if existing_data["attribution_result"] is not None:
                existing_data["attribution_result"]["evidence_ids"] = tuple(
                    existing_data["attribution_result"]["evidence_ids"]
                )
                if "conditions" in existing_data["attribution_result"]:
                    existing_data["attribution_result"]["conditions"] = tuple(
                        existing_data["attribution_result"]["conditions"]
                    )
            return TerminalDecision(**existing_data)
        decision = Task3ScoringConsumer(self).score(claim, terminal_decision_identity)
        encoded = _json(_decision_dict(decision))
        payload_hash = _digest(encoded)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT payload_json, payload_hash FROM prepared_terminal_decisions "
                "WHERE terminal_decision_identity=?",
                (decision.terminal_decision_identity,),
            ).fetchone()
            if previous:
                if previous["payload_json"] != encoded or previous["payload_hash"] != payload_hash:
                    raise CampaignStateError("terminal decision identity is bound to different content")
                connection.commit()
                return decision
            self._require_claim_binding(connection, claim)
            connection.execute(
                """INSERT INTO prepared_terminal_decisions
                   (terminal_decision_identity, campaign_id, run_id, slot_id,
                    payload_json, payload_hash, phase, receipt_identity)
                   VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', NULL)""",
                (
                    decision.terminal_decision_identity,
                    claim.campaign_id,
                    claim.run_id,
                    claim.slot_id,
                    encoded,
                    payload_hash,
                ),
            )
            connection.commit()
            return decision
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _score_run_evidence(
        self, claim: SlotClaim, terminal_decision_identity: str
    ) -> TerminalDecision:
        with closing(self._connect()) as connection:
            self._require_claim_binding(connection, claim)
            start = connection.execute(
                "SELECT start_record FROM slots WHERE campaign_id=? AND slot_id=?",
                (claim.campaign_id, claim.slot_id),
            ).fetchone()
            rows = connection.execute(
                "SELECT payload_json, payload_hash FROM run_evidence "
                "WHERE campaign_id=? AND slot_id=? AND run_id=? ORDER BY interaction_generation",
                (claim.campaign_id, claim.slot_id, claim.run_id),
            ).fetchall()
            campaign = connection.execute(
                "SELECT identity_json FROM campaigns WHERE campaign_id=?",
                (claim.campaign_id,),
            ).fetchone()
            profile_row = connection.execute(
                "SELECT payload_json, payload_hash FROM measurement_profiles WHERE campaign_id=?",
                (claim.campaign_id,),
            ).fetchone()
        if not start or start["start_record"] is None:
            raise CampaignStateError("scoring requires a committed execution start")
        if not rows:
            raise CampaignStateError("scoring requires current run evidence")
        if profile_row is None or _digest(profile_row["payload_json"]) != profile_row["payload_hash"]:
            raise CampaignStateError("supervisor measurement profile is missing or invalid")
        profile = json.loads(profile_row["payload_json"])
        campaign_identity = json.loads(campaign["identity_json"])
        if (
            profile["profile_identity"] != campaign_identity["formal_profile_identity"]
            or profile["formula_version"] != campaign_identity["deadline_formula_identity"]
        ):
            raise CampaignStateError("registered measurement profile differs from the sealed campaign")
        evidence = []
        for row in rows:
            if _digest(row["payload_json"]) != row["payload_hash"]:
                raise CampaignStateError("stored run evidence integrity check failed")
            item = json.loads(row["payload_json"])
            if (
                item["campaign_id"] != claim.campaign_id
                or item["slot_id"] != claim.slot_id
                or item["run_id"] != claim.run_id
                or item["asset_identity"] != claim.asset_identity
                or item["environment_identity"] != claim.environment_identity
                or item["measurement_profile_identity"] != profile["profile_identity"]
                or item["deadline_formula_version"] != profile["formula_version"]
                or item["clock_identity"] != profile["clock_identity"]
                or item["counter_frequency"] != profile["counter_frequency"]
                or item["budget_ns"] != profile["budget_ns"]
            ):
                raise CampaignStateError(
                    "stored evidence identity differs from the current run or registered profile"
                )
            evidence.append(item)
        by_interaction: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            by_interaction.setdefault(item["interaction_id"], []).append(item)
        selected = [
            max(items, key=lambda item: item["interaction_generation"])
            for items in by_interaction.values()
        ]
        selected.sort(key=lambda item: (item["case_id"], item["interaction_id"]))
        component_results = []
        confirmed_failure_components = []
        lifecycle_attributions = []
        for item in selected:
            evidence_ref = (item["evidence_id"],)
            if item["observation_state"] == "COMMITTED":
                if item["completion_kind"] != "returned":
                    completion_verdict = Verdict.FAIL
                    confirmed_failure_components.append(False)
                else:
                    completion_verdict = Verdict.PASS
                if item["semantic_timeout"] == "NOT_CONFIRMED":
                    deadline_verdict = Verdict.PASS
                elif item["semantic_timeout"] == "CONFIRMED":
                    deadline_verdict = Verdict.FAIL
                    confirmed_failure_components.append(True)
                else:
                    deadline_verdict = Verdict.NOT_PROVEN
                    confirmed_failure_components.append(False)
            elif item["observation_state"] == "INVALID_RECORD":
                completion_verdict = Verdict.INVALID
                deadline_verdict = Verdict.INVALID
            else:
                completion_verdict = Verdict.NOT_PROVEN
                deadline_verdict = Verdict.NOT_PROVEN
            component_results.extend(
                (
                    {
                        "component_id": f"{item['case_id']}:{item['interaction_id']}:completion_record",
                        "verdict": completion_verdict.value,
                        "evidence_ids": evidence_ref,
                    },
                    {
                        "component_id": f"{item['case_id']}:{item['interaction_id']}:deadline",
                        "verdict": deadline_verdict.value,
                        "evidence_ids": evidence_ref,
                    },
                )
            )
            with closing(self._connect()) as connection:
                registered_binding = connection.execute(
                    "SELECT 1 FROM direct_child_bindings WHERE campaign_id=? AND slot_id=? "
                    "AND run_id=? AND interaction_id=? AND interaction_generation=?",
                    (
                        item["campaign_id"],
                        item["slot_id"],
                        item["run_id"],
                        item["interaction_id"],
                        item["interaction_generation"],
                    ),
                ).fetchone()
            if item["lifecycle_required"] or item["lifecycle_evidence_ids"] or registered_binding:
                lifecycle_verdict = Verdict.NOT_PROVEN
                lifecycle_refs: tuple[str, ...] = ()
                lifecycle_attribution = {
                    "asset_identity_matches": False,
                    "registered_defect_applies": False,
                    "observed_fact_matches": False,
                    "same_run": False,
                    "prerequisites_complete": False,
                    "valid": False,
                }
                ids = tuple(item["lifecycle_evidence_ids"])
                if item["lifecycle_required"] != (registered_binding is not None):
                    lifecycle_verdict = Verdict.INVALID
                if registered_binding is None:
                    lifecycle_verdict = Verdict.INVALID
                if len(ids) > 1:
                    lifecycle_verdict = Verdict.INVALID
                elif ids:
                    lifecycle_refs = ids
                    with closing(self._connect()) as connection:
                        lifecycle_row = connection.execute(
                            "SELECT payload_json, payload_hash FROM lifecycle_evidence "
                            "WHERE evidence_id=?",
                            (ids[0],),
                        ).fetchone()
                        binding_row = connection.execute(
                            "SELECT payload_json, payload_hash FROM direct_child_bindings "
                            "WHERE campaign_id=? AND slot_id=? AND run_id=? AND interaction_id=? "
                            "AND interaction_generation=?",
                            (
                                item["campaign_id"],
                                item["slot_id"],
                                item["run_id"],
                                item["interaction_id"],
                                item["interaction_generation"],
                            ),
                        ).fetchone()
                        defect_rows = connection.execute(
                            "SELECT payload_json, payload_hash FROM registered_asset_defects "
                            "WHERE campaign_id=? AND asset_identity=? AND interaction_type=?",
                            (claim.campaign_id, item["asset_identity"], item["interaction_type"]),
                        ).fetchall()
                    if (
                        lifecycle_row is None
                        or binding_row is None
                        or _digest(lifecycle_row["payload_json"]) != lifecycle_row["payload_hash"]
                        or _digest(binding_row["payload_json"]) != binding_row["payload_hash"]
                    ):
                        lifecycle_verdict = Verdict.INVALID
                    else:
                        lifecycle_data = json.loads(lifecycle_row["payload_json"])
                        lifecycle_identity = dict(lifecycle_data)
                        lifecycle_identity.pop("evidence_id", None)
                        binding_data = json.loads(binding_row["payload_json"])
                        same_run = (
                            lifecycle_data["campaign_id"] == item["campaign_id"]
                            and lifecycle_data["slot_id"] == item["slot_id"]
                            and lifecycle_data["run_id"] == item["run_id"]
                            and lifecycle_data["case_id"] == item["case_id"]
                            and lifecycle_data["interaction_id"] == item["interaction_id"]
                            and lifecycle_data["interaction_generation"]
                            == item["interaction_generation"]
                            and lifecycle_data["asset_identity"] == item["asset_identity"]
                        )
                        binding_valid = (
                            binding_data["campaign_id"] == item["campaign_id"]
                            and binding_data["slot_id"] == item["slot_id"]
                            and binding_data["run_id"] == item["run_id"]
                            and binding_data["case_id"] == item["case_id"]
                            and binding_data["interaction_id"] == item["interaction_id"]
                            and binding_data["interaction_generation"]
                            == item["interaction_generation"]
                            and binding_data["transaction_identity"]
                            == item["record_commit_identity"]
                            and binding_data["observation_source"] == "SUPERVISOR_TRUSTED_OS"
                            and lifecycle_data["child_process_identity"]
                            == binding_data["child_process_identity"]
                            and lifecycle_data["child_pid"] == binding_data["child_pid"]
                            and lifecycle_data["child_creation_identity"]
                            == binding_data["child_creation_identity"]
                            and lifecycle_data["stable_handle_binding_identity"]
                            == binding_data["stable_handle_binding_identity"]
                            and lifecycle_data["transaction_identity"]
                            == item["record_commit_identity"]
                            and lifecycle_data["transaction_completion_identity"]
                            == item["snapshot_identity"]
                            and lifecycle_data["observation_authority"]
                            == binding_data["observation_authority"]
                            and lifecycle_data["child_binding_status"] == "VALID"
                        )
                        prerequisites_complete = (
                            item["observation_state"] == "COMMITTED"
                            and item["completion_kind"] == "returned"
                            and lifecycle_data["observation_source"] == "SUPERVISOR_TRUSTED_OS"
                            and lifecycle_data["observation_sequence"] == 1
                            and lifecycle_data["os_query_operation"]
                            == "WaitForSingleObject(handle, 0)"
                            and lifecycle_data["query_order_status"]
                            == "VALID_FIRST_EXTERNAL_CHECK"
                            and lifecycle_data["os_query_raw_result"] in {0, 258, 0xFFFFFFFF}
                        )
                        registered_defects = []
                        for defect_row in defect_rows:
                            if _digest(defect_row["payload_json"]) != defect_row["payload_hash"]:
                                continue
                            defect = json.loads(defect_row["payload_json"])
                            if defect["campaign_id"] == claim.campaign_id and defect["defect_id"]:
                                registered_defects.append(defect)
                        applicable_defects = [
                            defect
                            for defect in registered_defects
                            if defect["asset_identity"] == item["asset_identity"]
                            and defect["interaction_type"] == item["interaction_type"]
                            and defect["required_fact"]
                            == lifecycle_data["lifecycle_fact"]
                        ]
                        defect_applies = bool(applicable_defects)
                        fact_matches = (
                            lifecycle_data["lifecycle_fact"]
                            == "DIRECT_CHILD_LIVE_CONFIRMED"
                            and lifecycle_data["os_query_raw_result"] == 258
                        ) or (
                            lifecycle_data["lifecycle_fact"]
                            == "NO_LEAK_OBSERVED_AT_EXTERNAL_CHECK"
                            and lifecycle_data["os_query_raw_result"] == 0
                        )
                        lifecycle_attribution = {
                            "asset_identity_matches": lifecycle_data["asset_identity"]
                            == claim.asset_identity,
                            "registered_defect_applies": defect_applies,
                            "observed_fact_matches": fact_matches,
                            "same_run": same_run,
                            "prerequisites_complete": prerequisites_complete and binding_valid,
                        }
                        lifecycle_attribution["valid"] = all(
                            lifecycle_attribution.values()
                        )
                        if (
                            lifecycle_data["evidence_id"] != ids[0]
                            or
                            _digest(_json(lifecycle_identity)) != ids[0]
                            or
                            not same_run
                            or not binding_valid
                            or not prerequisites_complete
                            or not fact_matches
                        ):
                            lifecycle_verdict = Verdict.INVALID
                        elif lifecycle_data["os_query_raw_result"] == 0xFFFFFFFF:
                            lifecycle_verdict = Verdict.INVALID
                        elif lifecycle_data["lifecycle_fact"] == "DIRECT_CHILD_LIVE_CONFIRMED":
                            lifecycle_verdict = Verdict.FAIL
                            confirmed_failure_components.append(
                                lifecycle_attribution["valid"] and defect_applies
                            )
                        elif lifecycle_data["lifecycle_fact"] == "NO_LEAK_OBSERVED_AT_EXTERNAL_CHECK":
                            lifecycle_verdict = Verdict.PASS
                        else:
                            lifecycle_verdict = Verdict.INVALID
                        lifecycle_attributions.append(
                            {
                                "evidence_id": ids[0],
                                **lifecycle_attribution,
                            }
                        )
                component_results.append(
                    {
                        "component_id": f"{item['case_id']}:{item['interaction_id']}:direct_child_lifecycle",
                        "verdict": lifecycle_verdict.value,
                        "evidence_ids": lifecycle_refs,
                    }
                )
        component_verdicts = {item["verdict"] for item in component_results}
        if Verdict.INVALID.value in component_verdicts:
            verdict = Verdict.INVALID
            attribution = None
        elif Verdict.NOT_PROVEN.value in component_verdicts:
            verdict = Verdict.NOT_PROVEN
            attribution = None
        elif Verdict.FAIL.value in component_verdicts:
            verdict = Verdict.FAIL
            attribution = (
                "confirmed_rejection"
                if confirmed_failure_components and all(confirmed_failure_components)
                else "unattributed_fail"
            )
        else:
            verdict = Verdict.PASS
            attribution = None
        latest = max(evidence, key=lambda item: item["interaction_generation"])
        attribution_result = {
            "category": attribution,
            "evidence_ids": tuple(
                item["evidence_id"]
                for item in lifecycle_attributions
                if item["valid"]
            ) or ((latest["evidence_id"],) if attribution else ()),
        }
        if lifecycle_attributions:
            attribution_result["valid"] = bool(
                attribution == "confirmed_rejection"
                and confirmed_failure_components
                and all(confirmed_failure_components)
            )
            attribution_result["conditions"] = tuple(lifecycle_attributions)
        return TerminalDecision(
            terminal_decision_identity=terminal_decision_identity,
            campaign_id=claim.campaign_id,
            run_id=claim.run_id,
            slot_id=claim.slot_id,
            case_id=latest["case_id"],
            interaction_id=latest["interaction_id"],
            interaction_generation=latest["interaction_generation"],
            allocation_identity=latest["allocation_identity"],
            snapshot_identity=latest["snapshot_identity"],
            record_commit_identity=latest["record_commit_identity"],
            verdict=verdict,
            decision_reason="derived from identity-bound terminal measurement evidence",
            evidence_ids=tuple(
                evidence_id
                for item in evidence
                for evidence_id in (item["evidence_id"], *item["lifecycle_evidence_ids"])
            ),
            fail_attribution=attribution,
            component_results=tuple(component_results),
            attribution_result=attribution_result,
        )


    def record_terminal_receipt(self, decision_identity: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_hash, phase, receipt_identity FROM prepared_terminal_decisions "
                "WHERE terminal_decision_identity=?",
                (decision_identity,),
            ).fetchone()
            if not row:
                raise CampaignStateError("terminal decision has no prepared record")
            receipt_identity = _digest(f"{decision_identity}:{row['payload_hash']}")
            if row["phase"] == "PREPARED":
                connection.execute(
                    "UPDATE prepared_terminal_decisions SET phase='RECEIPT', receipt_identity=? "
                    "WHERE terminal_decision_identity=? AND phase='PREPARED'",
                    (receipt_identity, decision_identity),
                )
            elif row["receipt_identity"] != receipt_identity:
                raise CampaignStateError("terminal receipt identity conflicts with prepared content")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit_terminal_decision(self, decision_identity: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            prepared = connection.execute(
                "SELECT * FROM prepared_terminal_decisions WHERE terminal_decision_identity=?",
                (decision_identity,),
            ).fetchone()
            if not prepared:
                raise CampaignStateError("terminal decision has no prepared record")
            if prepared["phase"] == "COMMITTED":
                committed = connection.execute(
                    "SELECT payload_json FROM terminal_decisions WHERE terminal_decision_identity=?",
                    (decision_identity,),
                ).fetchone()
                if not committed or committed["payload_json"] != prepared["payload_json"]:
                    raise CampaignStateError("committed terminal decision is inconsistent")
                connection.commit()
                return
            if prepared["phase"] != "RECEIPT" or not prepared["receipt_identity"]:
                raise CampaignStateError("terminal decision requires a durable receipt")
            slot = connection.execute(
                "SELECT state, run_id, terminal_decision_identity FROM slots "
                "WHERE campaign_id=? AND slot_id=?",
                (prepared["campaign_id"], prepared["slot_id"]),
            ).fetchone()
            if (
                not slot
                or slot["state"] != "CLAIMED"
                or slot["run_id"] != prepared["run_id"]
            ):
                raise CampaignStateError("terminal decision requires the same claimed run")
            connection.execute(
                """INSERT INTO terminal_decisions
                   (terminal_decision_identity, campaign_id, run_id, slot_id, payload_json, payload_hash)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    prepared["terminal_decision_identity"],
                    prepared["campaign_id"],
                    prepared["run_id"],
                    prepared["slot_id"],
                    prepared["payload_json"],
                    prepared["payload_hash"],
                ),
            )
            decision = json.loads(prepared["payload_json"])
            cursor = connection.execute(
                """UPDATE slots SET state='TERMINAL', terminal_decision_identity=?, verdict=?
                   WHERE campaign_id=? AND slot_id=? AND state='CLAIMED' AND run_id=?""",
                (
                    decision_identity,
                    decision["verdict"],
                    prepared["campaign_id"],
                    prepared["slot_id"],
                    prepared["run_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise CampaignStateError("slot terminal commit lost its claimed-state condition")
            connection.execute(
                "UPDATE prepared_terminal_decisions SET phase='COMMITTED' "
                "WHERE terminal_decision_identity=? AND phase='RECEIPT'",
                (decision_identity,),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def terminal_persistence_phase(self, decision_identity: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT phase FROM prepared_terminal_decisions WHERE terminal_decision_identity=?",
                (decision_identity,),
            ).fetchone()
        return row["phase"] if row else None

    def append_diagnostic(
        self,
        *,
        terminal_decision_identity: str,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> str:
        if not event_type:
            raise ValueError("diagnostic event_type is required")
        diagnostic_id = uuid.uuid4().hex
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO diagnostics VALUES (?, ?, ?, ?)",
                (diagnostic_id, terminal_decision_identity, event_type, _json(event_payload)),
            )
        return diagnostic_id

    def stop_new_claims(self, campaign_id: str, reason: str) -> None:
        if not reason:
            raise ValueError("early-stop reason is required")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE campaigns SET state='STOPPED', stop_reason=? WHERE campaign_id=? AND state='OPEN'",
                (reason, campaign_id),
            )
            if cursor.rowcount != 1:
                raise CampaignStateError("campaign cannot transition to stopped")

    def close_campaign(self, campaign_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if not row:
                raise CampaignStateError("campaign is not registered")
            connection.execute(
                "UPDATE campaigns SET state='CLOSED' WHERE campaign_id=?",
                (campaign_id,),
            )
        return self.counts(campaign_id).pending == 0

    def get_slot(self, campaign_id: str, slot_id: int) -> SlotSnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM slots WHERE campaign_id=? AND slot_id=?",
                (campaign_id, slot_id),
            ).fetchone()
        if not row:
            return None
        return SlotSnapshot(
            campaign_id=row["campaign_id"],
            slot_id=row["slot_id"],
            state=SlotState(row["state"]),
            run_id=row["run_id"],
            asset_identity=row["asset_identity"],
            environment_identity=row["environment_identity"],
            claim_metadata=json.loads(row["claim_metadata"]) if row["claim_metadata"] else None,
            claim_timestamp_utc=row["claim_timestamp_utc"],
            start_record=json.loads(row["start_record"]) if row["start_record"] else None,
            terminal_decision_identity=row["terminal_decision_identity"],
            verdict=Verdict(row["verdict"]) if row["verdict"] else None,
        )

    def resume_claim(self, campaign_id: str, slot_id: int, run_id: str) -> SlotClaim:
        slot = self.get_slot(campaign_id, slot_id)
        if (
            slot is None
            or slot.state is not SlotState.CLAIMED
            or slot.run_id != run_id
            or slot.asset_identity is None
            or slot.environment_identity is None
            or slot.claim_metadata is None
            or slot.claim_timestamp_utc is None
        ):
            raise CampaignStateError("only the original pending slot claim can be resumed")
        return SlotClaim(
            campaign_id=slot.campaign_id,
            slot_id=slot.slot_id,
            run_id=slot.run_id,
            asset_identity=slot.asset_identity,
            environment_identity=slot.environment_identity,
            claim_metadata=slot.claim_metadata,
            claim_timestamp_utc=slot.claim_timestamp_utc,
        )

    def get_start_record(self, campaign_id: str, slot_id: int) -> ExecutionStartRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT start_record FROM slots WHERE campaign_id=? AND slot_id=?",
                (campaign_id, slot_id),
            ).fetchone()
        if not row or row["start_record"] is None:
            return None
        return ExecutionStartRecord(
            **json.loads(row["start_record"]),
            committed=True,
            launch_permitted=False,
        )

    def get_terminal_decision(self, decision_identity: str) -> TerminalDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM terminal_decisions WHERE terminal_decision_identity=?",
                (decision_identity,),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row["payload_json"])
        data["verdict"] = Verdict(data["verdict"])
        data["evidence_ids"] = tuple(data["evidence_ids"])
        data["component_results"] = tuple(data.get("component_results", ()))
        for component in data["component_results"]:
            component["evidence_ids"] = tuple(component["evidence_ids"])
        if data.get("attribution_result") is not None:
            data["attribution_result"]["evidence_ids"] = tuple(
                data["attribution_result"]["evidence_ids"]
            )
            if "conditions" in data["attribution_result"]:
                data["attribution_result"]["conditions"] = tuple(
                    data["attribution_result"]["conditions"]
                )
        return TerminalDecision(**data)

    def slot_count(self, campaign_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM slots WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        return int(row["count"])

    def counts(self, campaign_id: str) -> CampaignCounts:
        with closing(self._connect()) as connection:
            campaign = connection.execute(
                "SELECT state, stop_reason FROM campaigns WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise CampaignStateError("campaign is not registered")
            rows = connection.execute(
                "SELECT state, verdict FROM slots WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
        preregistered = len(rows)
        if preregistered != SLOT_COUNT:
            raise CampaignStateError("campaign must contain exactly 32 preregistered slots")
        terminal_rows = [row for row in rows if row["state"] == SlotState.TERMINAL.value]
        claimed = [row for row in rows if row["state"] == SlotState.CLAIMED.value]
        unclaimed = [row for row in rows if row["state"] == SlotState.UNCLAIMED.value]
        verdicts = {value.value: 0 for value in Verdict}
        confirmed = 0
        unattributed = 0
        for row in terminal_rows:
            if row["verdict"] not in verdicts:
                raise CampaignStateError("terminal slot is missing a recognized verdict")
            verdicts[row["verdict"]] += 1
        with closing(self._connect()) as connection:
            failure_rows = connection.execute(
                """SELECT d.payload_json FROM terminal_decisions d
                   JOIN slots s ON s.terminal_decision_identity=d.terminal_decision_identity
                   WHERE s.campaign_id=? AND s.verdict='FAIL'""",
                (campaign_id,),
            ).fetchall()
        for row in failure_rows:
            attribution = json.loads(row["payload_json"])["fail_attribution"]
            if attribution == "confirmed_rejection":
                confirmed += 1
            elif attribution == "unattributed_fail":
                unattributed += 1
            else:
                raise CampaignStateError("FAIL slot has invalid failure attribution")
        attempted = len(claimed) + len(terminal_rows)
        pending = attempted - len(terminal_rows)
        if attempted + len(unclaimed) != SLOT_COUNT:
            raise CampaignStateError("campaign attempt accounting does not balance")
        if sum(verdicts.values()) != len(terminal_rows):
            raise CampaignStateError("terminal verdict accounting does not balance")
        if verdicts[Verdict.FAIL.value] != confirmed + unattributed:
            raise CampaignStateError("task failure attribution does not balance")
        identity = json.loads(
            self._identity_json_for_campaign(campaign_id)
        )
        profile_type = CampaignProfileType(identity.get("profile_type", "CORRECT"))
        if pending or unclaimed or campaign["state"] == "STOPPED" or campaign["stop_reason"]:
            admission = "INCOMPLETE"
        elif profile_type is CampaignProfileType.CORRECT:
            admission = (
                "PASS"
                if verdicts[Verdict.PASS.value] == SLOT_COUNT
                and verdicts[Verdict.FAIL.value] == 0
                and verdicts[Verdict.INVALID.value] == 0
                and verdicts[Verdict.NOT_PROVEN.value] == 0
                else "REJECT"
            )
        else:
            admission = (
                "PASS"
                if confirmed == SLOT_COUNT
                and verdicts[Verdict.PASS.value] == 0
                and verdicts[Verdict.INVALID.value] == 0
                and verdicts[Verdict.NOT_PROVEN.value] == 0
                and unattributed == 0
                else "REJECT"
            )
        return CampaignCounts(
            preregistered=preregistered,
            attempted=attempted,
            terminal=len(terminal_rows),
            pending=pending,
            unclaimed=len(unclaimed),
            verdicts=verdicts,
            confirmed_rejections=confirmed,
            unattributed_failures=unattributed,
            admission=admission,
        )

    def _identity_json_for_campaign(self, campaign_id: str) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT identity_json FROM campaigns WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
        if row is None:
            raise CampaignStateError("campaign is not registered")
        return row["identity_json"]

    def get_campaign_identity(self, campaign_id: str) -> CampaignIdentity:
        data = json.loads(self._identity_json_for_campaign(campaign_id))
        data["accepted_asset_identities"] = tuple(data["accepted_asset_identities"])
        data["profile_type"] = CampaignProfileType(data.get("profile_type", "CORRECT"))
        data.setdefault("profile_catalog_version", None)
        data.setdefault("profile_catalog_hash", None)
        data.setdefault("profile_id", None)
        return CampaignIdentity(**data)

    def diagnostic_count(self, terminal_decision_identity: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM diagnostics WHERE terminal_decision_identity=?",
                (terminal_decision_identity,),
            ).fetchone()
        return int(row["count"])


class Task3ScoringConsumer:
    def __init__(self, campaign_store: CampaignStore):
        self._campaign_store = campaign_store

    def score(self, claim: SlotClaim, terminal_decision_identity: str) -> TerminalDecision:
        if not terminal_decision_identity:
            raise CampaignStateError("terminal decision identity is required")
        return self._campaign_store._score_run_evidence(claim, terminal_decision_identity)

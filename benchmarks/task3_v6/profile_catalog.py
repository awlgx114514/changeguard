from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from benchmarks.task3_v6.campaign import CampaignProfileType, CampaignStore


PROFILE_CATALOG_VERSION = "task3-profile-catalog-v1"
_VERSION_BINDINGS = {
    "semantic_spec_version": "task3-semantics-v1",
    "benchmark_contract_version": "task3-benchmark-v6",
    "scoring_contract_version": "task3-scoring-v6",
    "calibration_campaign_version": "task3-calibration-v6",
}
DEFAULT_PROFILE_CATALOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "task3_v6"
    / "task3_profile_catalog_v1.json"
)
_TOP_LEVEL_FIELDS = {
    "catalog_version",
    "semantic_spec_version",
    "benchmark_contract_version",
    "scoring_contract_version",
    "calibration_campaign_version",
    "profiles",
}
_PROFILE_FIELDS = {
    "profile_id",
    "profile_class",
    "required",
    "formal_sample_eligible",
    "registration_status",
    "semantic_registration",
    "expected_admission_kind",
    "asset_registration",
    "acceptance_artifact_reference",
    "notes",
}
_REGISTRATION_STATUSES = {
    "REGISTERED",
    "REGISTERED_ASSET_MISSING",
    "IMPLEMENTED",
    "PREFREEZE_ACCEPTED",
    "FORMAL_ACCEPTED",
}
class ProfileCatalogError(ValueError):
    pass


class ProfileClass(str, Enum):
    CORRECT = "CORRECT"
    REQUIRED_WRONG = "REQUIRED_WRONG"
    EXCLUDED_LIMITATION = "EXCLUDED_LIMITATION"


_PROFILE_IDS_BY_CLASS = {
    ProfileClass.CORRECT: frozenset(
        {
            "KNOWN_FIX",
            "CA_DIFFERENT_TRANSPORT_HELPER",
            "CA_LEGAL_CLEANUP_ORDERING",
            "CA_LATE_CACHE_UPDATE",
            "CA_ASYNC_CLOSED_STREAM_PROBE",
            "CA_BUFFERING_SPOOLING",
        }
    ),
    ProfileClass.REQUIRED_WRONG: frozenset(
        {
            "W1",
            "W2",
            "W3",
            "W4",
            "W7",
            "DESTRUCTIVE_WAIT",
            "WAIT_FOR_DESCENDANT_EOF",
        }
    ),
    ProfileClass.EXCLUDED_LIMITATION: frozenset({"W5", "W6"}),
}


class CoverageStage(str, Enum):
    PREFREEZE = "PREFREEZE"
    FORMAL = "FORMAL"


@dataclass(frozen=True)
class ProfileEntry:
    profile_id: str
    profile_class: ProfileClass
    required: bool
    formal_sample_eligible: bool
    registration_status: str
    semantic_registration: str
    expected_admission_kind: str
    asset_registration: dict[str, Any]
    acceptance_artifact_reference: str | None
    notes: str


@dataclass(frozen=True)
class ProfileCatalog:
    version: str
    semantic_spec_version: str
    benchmark_contract_version: str
    scoring_contract_version: str
    calibration_campaign_version: str
    profiles: tuple[ProfileEntry, ...]
    sha256: str
    _canonical_document: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_document)


@dataclass(frozen=True)
class ProfileAcceptance:
    campaign_id: str
    profile_id: str
    profile_class: ProfileClass
    catalog_version: str | None
    catalog_hash: str | None
    stage: CoverageStage
    admission: str
    source_reference: str

    def __post_init__(self) -> None:
        if not self.campaign_id or not self.source_reference:
            raise ValueError("profile acceptance source identity is required")
        if not isinstance(self.profile_class, ProfileClass):
            object.__setattr__(self, "profile_class", ProfileClass(self.profile_class))
        if not isinstance(self.stage, CoverageStage):
            object.__setattr__(self, "stage", CoverageStage(self.stage))
        if not self.admission:
            raise ValueError("profile acceptance admission is required")


@dataclass(frozen=True)
class ProfileCoverageResult:
    profile_id: str
    status: str
    source_reference: str | None = None
    conflicting_acceptances: tuple[ProfileAcceptance, ...] = ()


@dataclass(frozen=True)
class ProfileCoverage:
    catalog_version: str
    catalog_hash: str
    stage: CoverageStage
    required_correct_total: int
    required_correct_completed: int
    required_correct_missing: tuple[str, ...]
    required_wrong_total: int
    required_wrong_completed: int
    required_wrong_missing: tuple[str, ...]
    excluded_total: int
    profile_results: dict[str, ProfileCoverageResult]
    coverage_status: str


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _catalog_identity_document(document: Mapping[str, Any]) -> dict[str, Any]:
    identity_fields = (
        "profile_id",
        "profile_class",
        "required",
        "formal_sample_eligible",
        "semantic_registration",
        "expected_admission_kind",
    )
    profiles = [
        {field: profile[field] for field in identity_fields}
        for profile in document["profiles"]
    ]
    profiles.sort(key=lambda profile: profile["profile_id"])
    return {
        "catalog_version": document["catalog_version"],
        "semantic_spec_version": document["semantic_spec_version"],
        "benchmark_contract_version": document["benchmark_contract_version"],
        "scoring_contract_version": document["scoring_contract_version"],
        "calibration_campaign_version": document["calibration_campaign_version"],
        "profiles": profiles,
    }


def catalog_sha256(document: Mapping[str, Any]) -> str:
    identity_document = _catalog_identity_document(document)
    return hashlib.sha256(_canonical_json(identity_document).encode("utf-8")).hexdigest()


def _parse_profile(raw: Any) -> ProfileEntry:
    if not isinstance(raw, dict) or set(raw) != _PROFILE_FIELDS:
        raise ProfileCatalogError("profile entry fields do not match the catalog schema")
    profile_id = raw["profile_id"]
    if not isinstance(profile_id, str) or not profile_id:
        raise ProfileCatalogError("profile_id must be a nonempty string")
    try:
        profile_class = ProfileClass(raw["profile_class"])
    except (TypeError, ValueError) as error:
        raise ProfileCatalogError(f"unknown profile class for {profile_id}") from error
    required = raw["required"]
    formal_sample_eligible = raw["formal_sample_eligible"]
    if not isinstance(required, bool) or not isinstance(formal_sample_eligible, bool):
        raise ProfileCatalogError(f"profile flags must be boolean for {profile_id}")
    if profile_class is ProfileClass.EXCLUDED_LIMITATION:
        if required or formal_sample_eligible:
            raise ProfileCatalogError(f"excluded profile {profile_id} cannot be required")
        expected_admission_kind = "EXCLUDED_FROM_FORMAL_ADMISSION"
    elif profile_class is ProfileClass.CORRECT:
        if not required or not formal_sample_eligible:
            raise ProfileCatalogError(f"correct profile {profile_id} must be required")
        expected_admission_kind = "ALL_FORMAL_RUNS_PASS"
    else:
        if not required or not formal_sample_eligible:
            raise ProfileCatalogError(f"wrong profile {profile_id} must be required")
        expected_admission_kind = "ALL_FORMAL_RUNS_CONFIRMED_REJECTION"
    if raw["expected_admission_kind"] != expected_admission_kind:
        raise ProfileCatalogError(f"expected admission kind is inconsistent for {profile_id}")
    registration_status = raw["registration_status"]
    if not isinstance(registration_status, str) or registration_status not in _REGISTRATION_STATUSES:
        raise ProfileCatalogError(f"unknown registration status for {profile_id}")
    semantic_registration = raw["semantic_registration"]
    notes = raw["notes"]
    if not isinstance(semantic_registration, str) or not semantic_registration:
        raise ProfileCatalogError(f"semantic registration is required for {profile_id}")
    if not isinstance(notes, str):
        raise ProfileCatalogError(f"notes must be a string for {profile_id}")
    asset_registration = raw["asset_registration"]
    if not isinstance(asset_registration, dict) or not isinstance(
        asset_registration.get("status"), str
    ):
        raise ProfileCatalogError(f"asset registration is invalid for {profile_id}")
    for field in ("path", "manifest_reference"):
        value = asset_registration.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            raise ProfileCatalogError(f"asset registration {field} is invalid for {profile_id}")
    acceptance_reference = raw["acceptance_artifact_reference"]
    if acceptance_reference is not None and (
        not isinstance(acceptance_reference, str) or not acceptance_reference
    ):
        raise ProfileCatalogError(f"acceptance reference is invalid for {profile_id}")
    return ProfileEntry(
        profile_id=profile_id,
        profile_class=profile_class,
        required=required,
        formal_sample_eligible=formal_sample_eligible,
        registration_status=registration_status,
        semantic_registration=semantic_registration,
        expected_admission_kind=expected_admission_kind,
        asset_registration=asset_registration,
        acceptance_artifact_reference=acceptance_reference,
        notes=notes,
    )


def profile_catalog_from_dict(document: Mapping[str, Any]) -> ProfileCatalog:
    if set(document) != _TOP_LEVEL_FIELDS:
        raise ProfileCatalogError("catalog fields do not match the versioned schema")
    if document["catalog_version"] != PROFILE_CATALOG_VERSION:
        raise ProfileCatalogError("unsupported profile catalog version")
    for field, expected_version in _VERSION_BINDINGS.items():
        if document[field] != expected_version:
            raise ProfileCatalogError(f"{field} must bind to {expected_version}")
    raw_profiles = document["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ProfileCatalogError("catalog profiles must be a nonempty list")
    profiles = tuple(_parse_profile(raw) for raw in raw_profiles)
    profile_ids = tuple(profile.profile_id for profile in profiles)
    if len(profile_ids) != len(set(profile_ids)):
        raise ProfileCatalogError("profile identifiers must be unique")
    for profile_class, expected_ids in _PROFILE_IDS_BY_CLASS.items():
        actual_ids = frozenset(
            profile.profile_id
            for profile in profiles
            if profile.profile_class is profile_class
        )
        if actual_ids != expected_ids:
            raise ProfileCatalogError(
                f"catalog v1 frozen profile universe changed for {profile_class.value}"
            )
    canonical_document = _canonical_json(document)
    return ProfileCatalog(
        version=document["catalog_version"],
        semantic_spec_version=document["semantic_spec_version"],
        benchmark_contract_version=document["benchmark_contract_version"],
        scoring_contract_version=document["scoring_contract_version"],
        calibration_campaign_version=document["calibration_campaign_version"],
        profiles=profiles,
        sha256=catalog_sha256(document),
        _canonical_document=canonical_document,
    )


def load_profile_catalog(path: str | Path = DEFAULT_PROFILE_CATALOG_PATH) -> ProfileCatalog:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ProfileCatalogError("profile catalog root must be an object")
    return profile_catalog_from_dict(document)


def profile_acceptance_from_campaign(
    catalog: ProfileCatalog,
    store: CampaignStore,
    campaign_id: str,
) -> ProfileAcceptance:
    identity = store.get_campaign_identity(campaign_id)
    counts = store.counts(campaign_id)
    profile_class = (
        ProfileClass.CORRECT
        if identity.profile_type is CampaignProfileType.CORRECT
        else ProfileClass.REQUIRED_WRONG
    )
    return ProfileAcceptance(
        campaign_id=campaign_id,
        profile_id=identity.profile_id or "",
        profile_class=profile_class,
        catalog_version=identity.profile_catalog_version,
        catalog_hash=identity.profile_catalog_hash,
        stage=CoverageStage.FORMAL,
        admission=counts.admission,
        source_reference=f"campaign:{campaign_id}",
    )


def evaluate_profile_coverage(
    catalog: ProfileCatalog,
    acceptances: Iterable[ProfileAcceptance],
    stage: CoverageStage,
) -> ProfileCoverage:
    if not isinstance(stage, CoverageStage):
        stage = CoverageStage(stage)
    entries = {entry.profile_id: entry for entry in catalog.profiles}
    current = tuple(
        sorted(
            (record for record in acceptances if record.stage is stage),
            key=lambda record: (
                record.profile_id,
                record.source_reference,
                record.campaign_id,
            ),
        )
    )
    grouped: dict[str, list[ProfileAcceptance]] = {}
    results = {
        entry.profile_id: ProfileCoverageResult(
            profile_id=entry.profile_id,
            status=(
                "EXCLUDED_FROM_FORMAL_ADMISSION"
                if entry.profile_class is ProfileClass.EXCLUDED_LIMITATION
                else "COVERAGE_MISSING"
            ),
        )
        for entry in catalog.profiles
    }
    for record in current:
        entry = entries.get(record.profile_id)
        if entry is None:
            results[record.profile_id] = ProfileCoverageResult(
                profile_id=record.profile_id,
                status="UNKNOWN_PROFILE",
                source_reference=record.source_reference,
            )
            continue
        grouped.setdefault(record.profile_id, []).append(record)
    for profile_id, records in grouped.items():
        entry = entries[profile_id]
        if entry.profile_class is ProfileClass.EXCLUDED_LIMITATION:
            results[profile_id] = ProfileCoverageResult(
                profile_id=profile_id,
                status="EXCLUDED_FROM_FORMAL_ADMISSION",
                source_reference=records[0].source_reference,
            )
            continue
        bound_records = tuple(
            record
            for record in records
            if record.catalog_version == catalog.version
            and record.catalog_hash == catalog.sha256
        )
        if not bound_records:
            results[profile_id] = ProfileCoverageResult(
                profile_id=profile_id,
                status="CATALOG_BINDING_MISMATCH",
            )
            continue
        class_matches = tuple(
            record for record in bound_records if record.profile_class is entry.profile_class
        )
        if not class_matches:
            results[profile_id] = ProfileCoverageResult(
                profile_id=profile_id,
                status="PROFILE_CLASS_MISMATCH",
            )
            continue
        unique_acceptances = tuple(dict.fromkeys(class_matches))
        if len(unique_acceptances) != 1:
            results[profile_id] = ProfileCoverageResult(
                profile_id=profile_id,
                status="COVERAGE_CONFLICT",
                conflicting_acceptances=unique_acceptances,
            )
            continue
        record = unique_acceptances[0]
        if stage is CoverageStage.PREFREEZE:
            status = (
                "PREFREEZE_ACCEPTED"
                if record.admission == "PREFREEZE_ACCEPTED"
                else "PREFREEZE_NOT_ACCEPTED"
            )
        elif record.admission == "PASS":
            status = "FORMAL_ACCEPTED"
        elif record.admission == "INCOMPLETE":
            status = "FORMAL_INCOMPLETE"
        else:
            status = "FORMAL_REJECTED"
        results[profile_id] = ProfileCoverageResult(
            profile_id=profile_id,
            status=status,
            source_reference=record.source_reference,
        )
    correct_entries = tuple(
        entry
        for entry in catalog.profiles
        if entry.required and entry.profile_class is ProfileClass.CORRECT
    )
    wrong_entries = tuple(
        entry
        for entry in catalog.profiles
        if entry.required and entry.profile_class is ProfileClass.REQUIRED_WRONG
    )
    completed_status = (
        "PREFREEZE_ACCEPTED"
        if stage is CoverageStage.PREFREEZE
        else "FORMAL_ACCEPTED"
    )
    correct_completed = tuple(
        entry.profile_id
        for entry in correct_entries
        if results[entry.profile_id].status == completed_status
    )
    wrong_completed = tuple(
        entry.profile_id
        for entry in wrong_entries
        if results[entry.profile_id].status == completed_status
    )
    correct_missing = tuple(
        entry.profile_id
        for entry in correct_entries
        if results[entry.profile_id].status != completed_status
    )
    wrong_missing = tuple(
        entry.profile_id
        for entry in wrong_entries
        if results[entry.profile_id].status != completed_status
    )
    return ProfileCoverage(
        catalog_version=catalog.version,
        catalog_hash=catalog.sha256,
        stage=stage,
        required_correct_total=len(correct_entries),
        required_correct_completed=len(correct_completed),
        required_correct_missing=correct_missing,
        required_wrong_total=len(wrong_entries),
        required_wrong_completed=len(wrong_completed),
        required_wrong_missing=wrong_missing,
        excluded_total=sum(
            entry.profile_class is ProfileClass.EXCLUDED_LIMITATION
            for entry in catalog.profiles
        ),
        profile_results=results,
        coverage_status=("PASS" if not correct_missing and not wrong_missing else "INCOMPLETE"),
    )

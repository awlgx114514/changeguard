from typing import Any, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .assertion import VerificationAssertion


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metadata(StrictModel):
    id: Optional[str] = None
    project: Optional[str] = None
    created_at: Optional[str] = None


class Intent(StrictModel):
    """Legacy V1 input shape. It is not used by the canonical runtime."""

    goal: str = Field(min_length=1)
    non_goal: List[str] = Field(default_factory=list)

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class Expected(StrictModel):
    """Legacy V1 input shape. It is normalized to :class:`Scope`."""

    files: List[str] = Field(default_factory=list)
    modules: List[str] = Field(default_factory=list)
    change_level: str

    @field_validator("change_level")
    @classmethod
    def validate_change_level(cls, value: str) -> str:
        if value not in {"L0", "L1", "L2", "L3"}:
            raise ValueError("must be one of L0, L1, L2, L3")
        return value


class Scope(StrictModel):
    """Expected impact range, not a write allowlist."""

    paths: List[str] = Field(default_factory=list)
    modules: List[str] = Field(default_factory=list)
    change_level: str = "L1"
    description: str = ""

    @field_validator("change_level")
    @classmethod
    def validate_change_level(cls, value: str) -> str:
        if value not in {"L0", "L1", "L2", "L3"}:
            raise ValueError("must be one of L0, L1, L2, L3")
        return value


class Constraints(StrictModel):
    forbidden: List[str] = Field(default_factory=list)


class ExpectedChange(StrictModel):
    """A declared intended outcome with an explicit verification requirement."""

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requirement: Literal["required", "planning_only"] = "required"

    @field_validator("id", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class VerificationRequestSpec(StrictModel):
    """A declared command invocation, never an opaque shell expression."""

    argv: Tuple[str, ...]

    @field_validator("argv", mode="before")
    @classmethod
    def reject_shell_strings(cls, value: Any) -> Any:
        if isinstance(value, (str, bytes)):
            raise ValueError("argv must be an explicit argument sequence, not a shell string")
        return value

    @field_validator("argv")
    @classmethod
    def validate_argv_items(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("argv must contain non-empty string arguments")
        return value


class VerificationExpectedResult(StrictModel):
    exit_code: int = 0


class ClaimCoverage(StrictModel):
    """The sole declared relation from a verification claim to a change."""

    claim_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    coverage_type: Literal["direct", "indirect"]

    @field_validator("claim_id", "change_id")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class VerificationClaim(StrictModel):
    """Expected verification outcome and the structured request producing it."""

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    request: VerificationRequestSpec
    expected_result: VerificationExpectedResult = Field(default_factory=VerificationExpectedResult)

    @field_validator("id", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    def to_request(self):
        """Build the internal request without interpreting or repairing argv."""
        from code_governance.verification.request import VerificationRequest

        return VerificationRequest(
            id=f"verification:{self.id}",
            claim_id=self.id,
            argv=self.request.argv,
            reason=self.description,
        )

    def to_action(self):
        from code_governance.verification.request import verification_action

        return verification_action(self.to_request())


class Verification(StrictModel):
    claims: List[VerificationClaim] = Field(default_factory=list)
    coverage: List[ClaimCoverage] = Field(default_factory=list)
    assertions: List[VerificationAssertion] = Field(default_factory=list)


class ChangeContract(StrictModel):
    """Canonical V3 expected-change contract.

    V1 and V2 payloads are accepted only at this boundary and deterministically
    normalized before any runtime component receives the Contract.
    """

    version: Literal[3] = 3
    metadata: Metadata = Field(default_factory=Metadata)
    goal: str = Field(default="", min_length=1)
    non_goals: List[str] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)
    changes: List[ExpectedChange] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    verification: Verification = Field(default_factory=Verification)
    needs_clarification: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_contract(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if value.get("version") in {2, "2"}:
            return cls._normalize_v2(value)
        if "intent" not in value and "expected" not in value:
            return value
        version = value.get("version", 1)
        if version not in {1, "1"}:
            return value
        required = {"metadata", "intent", "expected", "constraints", "verification"}
        missing = sorted(required.difference(value))
        if missing:
            raise ValueError(f"V1 Contract is missing required section(s): {', '.join(missing)}")
        intent = _mapping(value.get("intent"))
        expected = _mapping(value.get("expected"))
        if "change_level" not in expected:
            raise ValueError("V1 Contract expected.change_level is required")
        verification = _mapping(value.get("verification"))
        if "tests" in verification or "checks" in verification:
            raise ValueError(
                "V1 verification.tests/checks cannot be normalized: use structured verification claims with explicit argv"
            )
        return {
            "version": 3,
            "metadata": _mapping(value.get("metadata")),
            "goal": intent.get("goal", ""),
            "non_goals": intent.get("non_goal") or [],
            "scope": {
                "paths": expected.get("files") or [],
                "modules": expected.get("modules") or [],
                "change_level": expected.get("change_level", "L1"),
                "description": "Normalized from Change Contract V1.",
            },
            "changes": _legacy_changes([intent.get("goal")] if intent.get("goal") else []),
            "constraints": _mapping(value.get("constraints")),
            "verification": verification,
            "needs_clarification": False,
        }

    @staticmethod
    def _normalize_v2(value: dict) -> dict:
        normalized = dict(value)
        normalized["version"] = 3
        legacy_changes = normalized.pop("expected_changes", [])
        if "changes" not in normalized:
            normalized["changes"] = _legacy_changes(legacy_changes)
        verification = _mapping(normalized.get("verification"))
        verification.setdefault("coverage", [])
        normalized["verification"] = verification
        return normalized

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @property
    def intent(self) -> Intent:
        """Read-only V1 compatibility view; runtime code must use ``goal``."""
        return Intent(goal=self.goal, non_goal=list(self.non_goals))

    @property
    def expected(self) -> Expected:
        """Read-only V1 compatibility view; runtime code must use ``scope``."""
        return Expected(
            files=list(self.scope.paths),
            modules=list(self.scope.modules),
            change_level=self.scope.change_level,
        )

    @classmethod
    def from_mapping(cls, data: object) -> "ChangeContract":
        return cls.model_validate(data)


def validation_messages(error: ValidationError) -> str:
    messages = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "root"
        messages.append(f"{location}: {item['msg']}")
    return "; ".join(messages)


def _mapping(value: Any) -> dict:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value if isinstance(value, dict) else {}


def _legacy_changes(values: Any) -> list[dict]:
    if not isinstance(values, list):
        return values
    return [
        {
            "id": f"legacy_{index}",
            "description": item,
            "requirement": "planning_only",
        }
        for index, item in enumerate(values)
    ]

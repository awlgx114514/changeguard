"""Contract declarations for deterministic verification assertions."""

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssertionType(str, Enum):
    EXIT_CODE = "exit_code"
    FILE_CHANGED = "file_changed"
    DIFF_CONTAINS = "diff_contains"
    BEHAVIOR_PROBE = "behavior_probe"


class VerificationAssertion(BaseModel):
    """A declarative rule for interpreting existing validation facts.

    Assertions identify a Contract change and may reference a command claim,
    but intentionally hold neither execution results nor copied change text.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    claim_id: Optional[str] = None
    assertion_type: AssertionType
    config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "change_id", "claim_id")
    @classmethod
    def reject_blank_identity(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value.strip() if value is not None else value

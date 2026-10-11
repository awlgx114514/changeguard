from pathlib import Path
from typing import Union

import yaml
from pydantic import ValidationError

from .schema import ChangeContract, validation_messages


class ContractLoadError(ValueError):
    """Raised when a Change Contract cannot be parsed or validated."""


def parse_contract(text: str, source: str = "<string>") -> ChangeContract:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ContractLoadError(f"{source}: invalid YAML: {error}") from error

    if not isinstance(data, dict):
        raise ContractLoadError(f"{source}: root mapping is required")

    try:
        return ChangeContract.from_mapping(data)
    except ValidationError as error:
        raise ContractLoadError(f"{source}: {validation_messages(error)}") from error


def load_contract(path: Union[str, Path]) -> ChangeContract:
    contract_path = Path(path)
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ContractLoadError(f"{contract_path}: cannot read file: {error}") from error
    return parse_contract(text, source=str(contract_path))

from pathlib import Path

import pytest

from code_governance.contract.loader import ContractLoadError, load_contract


VALID_V3 = """
version: 3
metadata:
  id: demo-001
  project: demo
goal: update parser
non_goals: [no dependency changes]
scope:
  paths: [src/parser.py]
  modules: [parser]
  change_level: L1
  description: parser implementation
changes:
  - id: parser_input
    description: parser accepts the requested input
    requirement: required
constraints:
  forbidden: [new network call]
verification:
  claims:
    - id: parser_tests
      description: parser tests pass
      request:
        argv: [pytest]
      expected_result:
        exit_code: 0
  coverage:
    - claim_id: parser_tests
      change_id: parser_input
      coverage_type: direct
needs_clarification: false
"""


VALID_V1 = """
metadata:
  id: demo-001
  project: demo
intent:
  goal: update parser
  non_goal: [no dependency changes]
expected:
  files: [src/parser.py]
  modules: [parser]
  change_level: L1
constraints:
  forbidden: [new network call]
verification:
  tests: [pytest]
  checks: [git diff --check]
"""


def write_contract(tmp_path: Path, content: str = VALID_V3) -> Path:
    path = tmp_path / "contract.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_valid_v3_contract(tmp_path):
    contract = load_contract(write_contract(tmp_path))

    assert contract.version == 3
    assert contract.goal == "update parser"
    assert contract.scope.change_level == "L1"
    assert contract.constraints.forbidden == ["new network call"]


def test_rejects_legacy_v1_shell_verification_at_the_loader_boundary(tmp_path):
    with pytest.raises(ContractLoadError, match="cannot be normalized"):
        load_contract(write_contract(tmp_path, "version: 1\n" + VALID_V1))


def test_rejects_unknown_version(tmp_path):
    path = write_contract(tmp_path, VALID_V3.replace("version: 3", "version: 4"))

    with pytest.raises(ContractLoadError, match=r"version"):
        load_contract(path)


def test_v2_defaults_optional_lists_when_omitted(tmp_path):
    content = """
version: 2
goal: keep this goal
scope: {}
expected_changes: []
constraints: {}
verification: {}
"""

    contract = load_contract(write_contract(tmp_path, content))

    assert contract.version == 3
    assert contract.non_goals == []
    assert contract.scope.paths == []
    assert contract.scope.modules == []
    assert contract.constraints.forbidden == []
    assert contract.verification.claims == []


def test_rejects_empty_goal(tmp_path):
    path = write_contract(tmp_path, VALID_V3.replace("goal: update parser", "goal: '   '"))

    with pytest.raises(ContractLoadError, match=r"goal"):
        load_contract(path)


def test_rejects_v1_with_missing_required_section(tmp_path):
    path = write_contract(tmp_path, VALID_V1.replace("constraints:\n  forbidden: [new network call]\n", ""))

    with pytest.raises(ContractLoadError, match=r"constraints"):
        load_contract(path)


def test_rejects_unknown_field(tmp_path):
    path = write_contract(tmp_path, VALID_V3.replace("metadata:\n", "metadata:\n  owner: someone\n"))

    with pytest.raises(ContractLoadError, match=r"metadata\.owner"):
        load_contract(path)


def test_rejects_wrong_list_type(tmp_path):
    path = write_contract(tmp_path, VALID_V3.replace("paths: [src/parser.py]", "paths: src/parser.py"))

    with pytest.raises(ContractLoadError, match=r"scope\.paths"):
        load_contract(path)


def test_rejects_non_mapping_root(tmp_path):
    path = write_contract(tmp_path, "- not-a-contract\n")

    with pytest.raises(ContractLoadError, match="root mapping"):
        load_contract(path)


def test_rejects_invalid_yaml(tmp_path):
    path = write_contract(tmp_path, "metadata: [unterminated\n")

    with pytest.raises(ContractLoadError, match="invalid YAML"):
        load_contract(path)


def test_v1_document_example_with_shell_verification_is_rejected():
    with pytest.raises(ContractLoadError, match="cannot be normalized"):
        load_contract(Path("docs/examples/change-contract-v1.yaml"))

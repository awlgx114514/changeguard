# ChangeGuard

ChangeGuard is an agent harness for governed software engineering tasks. It keeps model decisions, runtime execution, policy checks, validation, recovery, traces, and evaluation within one auditable workflow.

> The model proposes actions. The runtime controls execution. Governance constrains and validates the result.

## Capabilities

- Run a coding agent against an isolated workspace.
- Validate proposed changes against a YAML change contract.
- Route filesystem, shell, and Git operations through governed tools.
- Record execution state, observations, traces, and validation results.
- Run benchmark datasets in baseline or hardened mode.

## Install

Python 3.9 or later is required.

```bash
python -m pip install -e .
```

## Use

Validate repository changes against a contract:

```bash
changeguard validate examples/contracts/simple-change.yaml --repo . --format json
```

Run a coding task under ChangeGuard:

```bash
changeguard run task.md --repo .
```

Run an evaluation dataset:

```bash
changeguard benchmark --dataset path/to/dataset.json --mode hardened --output report.json
```

Use `changeguard --help` and the command-specific `--help` options for available arguments.

## Tests

Run the test suite from the source checkout in a Python environment with `pytest` installed:

```bash
python -m pytest
```

The project design is documented in [`docs/project-design.md`](docs/project-design.md). A sample change contract is in [`examples/contracts/`](examples/contracts/).

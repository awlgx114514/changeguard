import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"


def execute(mode, directory=DEMO, cwd=ROOT):
    runner = directory / "run.py"
    assert runner.is_file(), "Demo entry point is required"
    return subprocess.run(
        [sys.executable, "-B", str(runner), mode],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("success", "success/expected_output.txt"),
        ("w2_failure", "w2_failure/expected_output.txt"),
        ("audit_replay", "audit_replay/expected_output.json"),
    ],
)
def test_demo_runs_with_stable_expected_output(mode, expected, tmp_path):
    first = execute(mode)
    assert first.returncode == 0, first.stderr
    assert first.stderr == ""
    assert first.stdout == (DEMO / expected).read_text(encoding="utf-8")
    relocated = tmp_path / "portable demo"
    shutil.copytree(DEMO, relocated)
    second = execute(mode, relocated, tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout


def test_w2_output_links_observation_evidence_and_stored_attribution():
    result = execute("w2_failure")
    assert result.returncode == 0, result.stderr
    for fact in (
        "TASK_RESULT=FAIL", "WAIT_TIMEOUT=258", "child_process: LIVE",
        "confirmed_rejection", "Lifecycle Observation", "cleanup-resource-release",
        "4f86de8a33e0171073aecd684aaf6763906dec929dd1b69c8fe4ffe1dbc4b6b2",
        "449f84734816f81e3b4a9cb15f5ddc4464e7be13413fb9c5c532c347ba76c719",
        "source_binding=UNVERIFIABLE", "producer_authority=UNVERIFIABLE",
    ):
        assert fact in result.stdout


@pytest.mark.parametrize("mode", ["success", "w2_failure", "audit_replay"])
def test_demo_only_reads_records_without_agent_tools_or_scoring(mode):
    files_before = {
        path.relative_to(DEMO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in DEMO.rglob("*") if path.is_file()
    }
    probe = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).with_name("readonly_probe.py")), mode],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    observed = json.loads(probe.stdout)
    assert observed["exit_code"] == 0
    assert observed["execution_calls"] == []
    assert observed["execution_imports"] == []
    assert observed["side_effect_events"] == []
    files_after = {
        path.relative_to(DEMO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in DEMO.rglob("*") if path.is_file()
    }
    assert files_after == files_before


@pytest.mark.parametrize("mode,case", [("success", "success"), ("w2_failure", "w2"), ("audit_replay", "w2")])
def test_demo_rejects_damaged_evidence_before_printing_success(mode, case, tmp_path):
    directory = tmp_path / "damaged demo"
    shutil.copytree(DEMO, directory)
    database = directory / "data" / case / "campaign.sqlite3"
    original = database.read_bytes()
    database.write_bytes(original[:100] + bytes([original[100] ^ 1]) + original[101:])
    result = execute(mode, directory)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Package digest mismatch" in result.stderr


def test_demo_rejects_missing_records(tmp_path):
    directory = tmp_path / "incomplete demo"
    shutil.copytree(DEMO, directory)
    (directory / "data" / "w2" / "campaign.sqlite3").unlink()
    result = execute("audit_replay", directory)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "table,message",
    [
        ("lifecycle_evidence", "Terminal decision integrity is invalid"),
        ("prepared_terminal_decisions", "Committed decision receipt is unverified"),
    ],
)
def test_demo_rejects_incomplete_record_chain_even_with_matching_package_digest(table, message, tmp_path):
    directory = tmp_path / "incomplete chain"
    shutil.copytree(DEMO, directory)
    database = directory / "data" / "w2" / "campaign.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=DELETE")
    # 删除真实记录，检查证据引用和提交记录缺失时的行为。
    connection.execute(f"DELETE FROM {table}")
    connection.commit()
    connection.close()
    manifest_path = directory / "data" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["w2"]["files"]["campaign.sqlite3"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = execute("audit_replay", directory)
    assert result.returncode != 0
    assert result.stdout == ""
    assert message in result.stderr


@pytest.mark.parametrize("mode", ["success", "w2_failure", "audit_replay"])
def test_shell_entrypoint_runs(mode):
    bash = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is unavailable; Python entry point is tested separately")
    result = subprocess.run(
        [bash, str(DEMO / mode / "run.sh")],
        cwd=ROOT, env={**os.environ, "PYTHON": sys.executable},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected = "expected_output.json" if mode == "audit_replay" else "expected_output.txt"
    assert result.stdout == (DEMO / mode / expected).read_text(encoding="utf-8")

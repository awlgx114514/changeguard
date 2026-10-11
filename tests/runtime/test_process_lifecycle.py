import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from code_governance.runtime.process_lifecycle import OwnedProcess


def _pid_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _long_lived_command(pid_file: Path) -> list[str]:
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        with open(sys.argv[1], 'w', encoding='ascii') as stream:
            stream.write(str(child.pid))
        time.sleep(30)
        """
    )
    return [sys.executable, "-c", script, str(pid_file)]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object semantics are required")
def test_owned_process_timeout_stops_root_and_descendant_before_return(tmp_path):
    child_pid_file = tmp_path / "child.pid"

    outcome = OwnedProcess.run(
        _long_lived_command(child_pid_file),
        cwd=tmp_path,
        timeout_seconds=0.5,
    )

    child_pid = int(child_pid_file.read_text(encoding="ascii"))
    assert outcome.timed_out is True
    assert outcome.quiescent is True
    assert outcome.returncode is not None
    assert _pid_exists(child_pid) is False


def test_owned_process_normal_completion_is_quiescent(tmp_path):
    outcome = OwnedProcess.run(
        [sys.executable, "-c", "print('ready')"],
        cwd=tmp_path,
        timeout_seconds=2,
    )

    assert outcome.timed_out is False
    assert outcome.quiescent is True
    assert outcome.returncode == 0
    assert outcome.stdout == "ready\n"
    assert outcome.stderr == ""

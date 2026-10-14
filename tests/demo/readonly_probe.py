import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys


FORBIDDEN = (
    "code_governance.agent", "code_governance.runtime", "code_governance.tools",
    "code_governance.verification", "code_governance.validator", "benchmarks.task3_v6",
)
calls = set()
side_effects = []


def profile(frame, event, arg):
    module = frame.f_globals.get("__name__", "")
    if event == "call" and module.startswith(FORBIDDEN):
        calls.add(f"{module}.{frame.f_code.co_name}")


def audit(event, arguments):
    if event.startswith(("subprocess.", "socket.", "os.exec", "os.spawn")) or event in {
        "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.truncate",
    }:
        side_effects.append(event)
    if event == "open":
        _, mode, flags = arguments
        if (mode and any(value in mode for value in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            side_effects.append("open_for_write")


sys.addaudithook(audit)
sys.setprofile(profile)
namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "demo" / "run.py"))
output = io.StringIO()
with contextlib.redirect_stdout(output):
    exit_code = namespace["main"]([sys.argv[1]])
sys.setprofile(None)
print(json.dumps({
    "exit_code": exit_code,
    "output": output.getvalue(),
    "execution_calls": sorted(calls),
    "execution_imports": sorted(name for name in sys.modules if name.startswith(FORBIDDEN)),
    "side_effect_events": side_effects,
}))

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.auth import check_password


print(json.dumps({
    "protocol": "changeguard.behavior_probe.v1",
    "probe_id": "empty_password_probe",
    "observed": {"accepted": bool(check_password(""))},
}))

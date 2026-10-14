#!/bin/sh
set -eu
demo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "${PYTHON:-python}" -B "$demo_root/run.py" w2_failure "$@"

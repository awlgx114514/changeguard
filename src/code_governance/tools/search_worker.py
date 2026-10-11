"""Single-purpose filesystem filename search worker."""

import json
from pathlib import Path
import sys


def main() -> int:
    request = json.loads(sys.stdin.read())
    workspace = Path(str(request["workspace"])).resolve()
    query = str(request.get("query", "")).lower()
    max_results = max(1, min(int(request.get("max_results", 1000)), 10000))
    paths = []
    for path in workspace.rglob("*"):
        if path.is_file() and query in path.name.lower():
            paths.append(path.relative_to(workspace).as_posix())
            if len(paths) >= max_results:
                break
    sys.stdout.write(json.dumps({"paths": sorted(paths)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

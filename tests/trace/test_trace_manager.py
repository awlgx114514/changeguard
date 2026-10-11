import json

from code_governance.trace.writer import TraceManager, TraceWriter


def test_trace_manager_writes_compact_event_list(tmp_path):
    manager = TraceManager("run-1", TraceWriter(tmp_path))
    manager.emit("task_started", {"task_file": "task.md"})
    manager.emit("context_created", {"files": ["app.py"]})

    path = manager.write()
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["run_id"] == "run-1"
    assert payload["schema_version"] == "v2"
    assert payload["runtime_version"] == "runtime-v1"
    assert [event["type"] for event in payload["events"]] == [
        "task_started",
        "context_created",
    ]
    assert "prompt" not in json.dumps(payload)
    assert payload["events"][0]["sequence"] == 1
    assert payload["events"][0]["timestamp"]

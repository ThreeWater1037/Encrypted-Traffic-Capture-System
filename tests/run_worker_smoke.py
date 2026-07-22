from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worker_agent.app import create_app  # noqa: E402
from worker_agent.config import WorkerConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker Flask 单 URL 真实冒烟测试")
    parser.add_argument("--task-python", required=True, type=Path)
    parser.add_argument(
        "--data-dir", type=Path, default=ROOT / "worker_data" / "smoke"
    )
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    config = WorkerConfig(
        worker_id="windows-smoke",
        host="127.0.0.1",
        port=5100,
        token="smoke-token",
        project_root=ROOT,
        python_executable=args.task_python.resolve(),
        data_dir=args.data_dir.resolve(),
        max_queue_size=2,
        max_items=5,
        task_timeout_seconds=args.timeout,
    )
    app = create_app(config)
    manager = app.extensions["task_manager"]
    task_id = f"worker-smoke-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    payload = {
        "task_id": task_id,
        "items": [{"id": "1", "name": "Example", "url": args.url}],
        "browsers": ["chrome"],
        "pcap": True,
        "analysis": {
            "steps": ["extract", "classify", "infer"],
            "with_coframe": True,
            "sni_suffixes": [],
        },
    }
    headers = {"Authorization": "Bearer smoke-token"}

    try:
        with app.test_client() as client:
            response = client.post("/api/v1/tasks", json=payload, headers=headers)
            print("submit", response.status_code, response.get_json())
            if response.status_code != 202:
                return 1

            deadline = time.monotonic() + args.timeout + 30
            previous = None
            while time.monotonic() < deadline:
                current = client.get(
                    f"/api/v1/tasks/{task_id}", headers=headers
                ).get_json()
                marker = (current["status"], current["stage"])
                if marker != previous:
                    print("status", *marker)
                    previous = marker
                if current["status"] in {
                    "SUCCEEDED",
                    "PARTIAL",
                    "FAILED",
                    "CANCELED",
                    "INTERRUPTED",
                }:
                    result = client.get(
                        f"/api/v1/tasks/{task_id}/result", headers=headers
                    )
                    print("result", result.status_code, result.get_json())
                    return 0 if current["status"] == "SUCCEEDED" else 2
                time.sleep(1)
            print("smoke test polling timed out")
            return 3
    finally:
        manager.shutdown(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())


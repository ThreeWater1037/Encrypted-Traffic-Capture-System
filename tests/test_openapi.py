from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


class WorkerOpenApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = Path(__file__).resolve().parents[1] / "worker_agent" / "openapi.yaml"
        cls.document = yaml.safe_load(cls.path.read_text(encoding="utf-8"))

    def test_document_has_expected_paths_and_methods(self) -> None:
        self.assertEqual(self.document["openapi"], "3.0.3")
        expected = {
            "/api/v1/health": {"get"},
            "/api/v1/capabilities": {"get"},
            "/api/v1/tasks": {"post"},
            "/api/v1/tasks/from-file": {"post"},
            "/api/v1/tasks/{task_id}": {"get"},
            "/api/v1/tasks/{task_id}/cancel": {"post"},
            "/api/v1/tasks/{task_id}/resume": {"post"},
            "/api/v1/tasks/{task_id}/result": {"get"},
            "/api/v1/tasks/{task_id}/log": {"get"},
        }
        actual = {
            path: {
                method
                for method in value
                if method in {"get", "post", "put", "patch", "delete"}
            }
            for path, value in self.document["paths"].items()
        }
        self.assertEqual(actual, expected)

    def test_all_local_references_resolve(self) -> None:
        def resolve(reference: str) -> Any:
            self.assertTrue(reference.startswith("#/"), reference)
            value: Any = self.document
            for part in reference[2:].split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
            return value

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if "$ref" in value:
                    resolve(value["$ref"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(self.document)

    def test_operation_ids_are_unique(self) -> None:
        operation_ids = []
        for path_item in self.document["paths"].values():
            for method in ("get", "post", "put", "patch", "delete"):
                operation = path_item.get(method)
                if operation:
                    operation_ids.append(operation["operationId"])
        self.assertEqual(len(operation_ids), len(set(operation_ids)))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml


class MasterOpenApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parents[1] / "master_server" / "openapi.yaml"
        cls.document = yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_expected_paths_are_documented(self) -> None:
        expected = {
            "/api/v1/health": {"get"},
            "/api/v1/machines": {"get", "post"},
            "/api/v1/machines/{machine_id}": {"delete"},
            "/api/v1/machines/{machine_id}/probe": {"post"},
            "/api/v1/jobs": {"get", "post"},
            "/api/v1/jobs/from-file": {"post"},
            "/api/v1/jobs/{job_id}": {"get"},
            "/api/v1/jobs/{job_id}/cancel": {"post"},
            "/api/v1/jobs/{job_id}/results": {"get"},
            "/api/v1/jobs/{job_id}/logs": {"get"},
        }
        actual = {
            path: {method for method in value if method in {"get", "post", "delete"}}
            for path, value in self.document["paths"].items()
        }
        self.assertEqual(actual, expected)

    def test_all_local_references_resolve(self) -> None:
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if "$ref" in value:
                    target: Any = self.document
                    for part in value["$ref"][2:].split("/"):
                        target = target[part]
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(self.document)


if __name__ == "__main__":
    unittest.main()

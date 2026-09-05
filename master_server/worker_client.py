"""主控到 Worker 的小型 HTTP 客户端。"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class WorkerRequestError(RuntimeError):
    """Worker 无法访问或返回非成功状态。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class WorkerClient:
    """只封装 Worker OpenAPI 中主控实际需要的调用。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if path != "/api/v1/health":
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                message = detail.get("message") or detail.get("error")
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = exc.reason
            raise WorkerRequestError(
                f"Worker 返回 HTTP {exc.code}：{message}", status_code=exc.code
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise WorkerRequestError(f"无法连接 Worker：{exc}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/health")

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/capabilities")

    def submit_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/tasks?include_request=false&include_result=false",
            payload,
            timeout=max(self.timeout, 600.0),
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/tasks/{task_id}?compact=true&include_result=false",
        )

    def get_capture_progress(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        after_position: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        query_values: dict[str, Any] = {
            "after_position": after_position,
            "limit": limit,
        }
        if run_id:
            query_values["run_id"] = run_id
        return self._request(
            "GET",
            f"/api/v1/tasks/{task_id}/progress?{urlencode(query_values)}",
        )

    def get_result(self, task_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/tasks/{task_id}/result?compact=true",
            timeout=max(self.timeout, 600.0),
        )

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/tasks/{task_id}/cancel")

    def resume_task(self, task_id: str, resume_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/tasks/{task_id}/resume",
            {"resume_token": resume_token},
        )

    def get_log(self, task_id: str, *, offset: int = 0, limit: int = 65_536) -> dict[str, Any]:
        query = urlencode({"offset": offset, "limit": limit})
        return self._request("GET", f"/api/v1/tasks/{task_id}/log?{query}")

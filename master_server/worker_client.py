"""主控到 Worker 的小型 HTTP 客户端。"""

from __future__ import annotations

import json
from http.client import HTTPException, IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class WorkerRequestError(RuntimeError):
    """Worker 无法访问、响应不完整或返回非成功状态。"""

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
                with exc:
                    detail = json.loads(exc.read().decode("utf-8"))
                message = (
                    detail.get("message") or detail.get("error") or exc.reason
                    if isinstance(detail, dict) else exc.reason
                )
            except (HTTPException, OSError, UnicodeDecodeError, json.JSONDecodeError):
                # 错误响应本身也可能被截断；仍保留 HTTP 状态供调用方判断。
                message = exc.reason
            raise WorkerRequestError(
                f"Worker 返回 HTTP {exc.code}（{method} {path}）：{message}",
                status_code=exc.code,
            ) from exc
        except IncompleteRead as exc:
            remaining = (
                f"，仍缺少 {exc.expected} 字节" if exc.expected is not None else ""
            )
            raise WorkerRequestError(
                f"Worker 响应传输中断（{method} {path}）："
                f"已读取 {len(exc.partial)} 字节{remaining}"
            ) from exc
        except HTTPException as exc:
            raise WorkerRequestError(
                f"Worker HTTP 响应异常（{method} {path}）：{type(exc).__name__}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerRequestError(
                f"Worker JSON 响应无效（{method} {path}）：{type(exc).__name__}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise WorkerRequestError(f"无法连接 Worker：{exc}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/health")

    def list_extractions(self, *, limit=20, offset=0):
        return self._request('GET', '/api/v1/extractions?' + urlencode({'limit': limit, 'offset': offset}))

    def submit_extraction(self, source_task_id, *, keep_pcap=False, keep_pcap_count=None, steps=None):
        return self._request('POST', '/api/v1/extractions', {
            'source_task_id': source_task_id, 'keep_pcap': keep_pcap, 'keep_pcap_count': keep_pcap_count,
            'steps': steps if steps is not None else ['flow', 'tls']})

    def get_extraction(self, extraction_id):
        return self._request('GET', '/api/v1/extractions/' + quote(extraction_id, safe=''))

    def cancel_extraction(self, extraction_id):
        return self._request('POST', '/api/v1/extractions/' + quote(extraction_id, safe='') + '/cancel')

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
            f"/api/v1/tasks/{quote(task_id, safe='')}?compact=true&include_result=false",
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
            f"/api/v1/tasks/{quote(task_id, safe='')}/progress?{urlencode(query_values)}",
        )

    def get_result(self, task_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/tasks/{quote(task_id, safe='')}/result?compact=true",
            timeout=max(self.timeout, 600.0),
        )

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/tasks/{quote(task_id, safe='')}/cancel")

    def resume_task(self, task_id: str, resume_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/tasks/{quote(task_id, safe='')}/resume",
            {"resume_token": resume_token},
        )

    def get_log(
        self, task_id: str, *, offset: int = 0, limit: int = 65_536,
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        parameters = {"offset": offset, "limit": limit}
        if tail_lines is not None:
            parameters["tail_lines"] = tail_lines
        query = urlencode(parameters)
        result = self._request("GET", f"/api/v1/tasks/{quote(task_id, safe='')}/log?{query}")
        if tail_lines is not None and result.get("tail_lines") != tail_lines:
            raise WorkerRequestError("Worker 尚不支持读取最新日志，请更新 Worker 后重试")
        return result

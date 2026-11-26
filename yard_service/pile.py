"""HTTP client for the project's pile/session peer interface."""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.parse import urlparse


class PileTransportError(RuntimeError):
    """Raised when the peer gives no usable HTTP response."""


class HttpPileClient:
    def __init__(self, base_url: str, *, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> tuple[int, Any]:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http":
            raise ValueError("仅支持 http 桩端地址")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        connection = http.client.HTTPConnection(
            parsed.hostname or "127.0.0.1",
            parsed.port or 80,
            timeout=self.timeout,
        )
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response = connection.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PileTransportError("桩端返回了无法解析的响应") from exc
            return response.status, data
        except Exception as exc:  # http.client raises several socket errors
            if isinstance(exc, PileTransportError):
                raise
            raise PileTransportError(str(exc)) from exc
        finally:
            connection.close()

    def stop(self, session_id: str) -> tuple[int, Any]:
        return self._request("POST", f"/sessions/{session_id}/stop", {})

    def get_session(self, session_id: str) -> tuple[int, Any]:
        return self._request("GET", f"/sessions/{session_id}", None)


class DictPileClient:
    """Test double returning queued responses without sockets."""

    def __init__(
        self,
        *,
        stop_result: tuple[int, Any] | Exception | None = None,
        get_results: list[tuple[int, Any] | Exception] | None = None,
    ) -> None:
        self.stop_result = stop_result
        self.get_results = list(get_results or [])
        self.calls: list[tuple[str, str, Any]] = []

    def stop(self, session_id: str) -> tuple[int, Any]:
        self.calls.append(("POST", f"/sessions/{session_id}/stop", {}))
        result = self.stop_result
        if isinstance(result, Exception):
            raise result
        if result is None:
            return 200, {}
        return result

    def get_session(self, session_id: str) -> tuple[int, Any]:
        self.calls.append(("GET", f"/sessions/{session_id}", None))
        if not self.get_results:
            return 404, {"error": "not found"}
        result = self.get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

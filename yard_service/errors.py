"""Domain error type used by both the service and HTTP layer."""

from __future__ import annotations

from typing import Any


class YardError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_request",
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

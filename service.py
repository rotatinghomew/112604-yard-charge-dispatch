"""Convenience import path: ``from service import YardService``."""

from yard_service.errors import YardError
from yard_service.service import SAMPLE_DIR, YardService

Service = YardService
YardServiceError = YardError

__all__ = ["YardService", "Service", "YardError", "YardServiceError", "SAMPLE_DIR"]

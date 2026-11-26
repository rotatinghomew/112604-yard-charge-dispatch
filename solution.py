"""Compatibility alias for harnesses that import ``solution``."""

from yard_service.errors import YardError
from yard_service.service import SAMPLE_DIR, YardService

__all__ = ["YardService", "YardError", "SAMPLE_DIR"]

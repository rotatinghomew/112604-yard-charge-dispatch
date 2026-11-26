"""Convenience entrypoint: ``python3 app.py``."""

from yard_service.server import create_server, main

__all__ = ["create_server", "main"]

if __name__ == "__main__":
    main()

"""`python -m rga`: start the server on the configured loopback host and port."""

from __future__ import annotations

import sys

from rga.config import ConfigError, load_settings


def main() -> int:
    """Run the server; return 2 (without starting) on an invalid configuration."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"rga: configuration error: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    from rga.web.app import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

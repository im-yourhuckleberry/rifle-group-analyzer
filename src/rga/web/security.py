"""Request guards for a loopback-only app: Host allowlist, Origin check, content-type rule,
and anti-framing/content-policy response headers.

`host_guard` rejects foreign Host headers (DNS rebinding). `OriginGuard` protects state-changing
methods against cross-site requests. `SecurityHeaders` adds `SECURITY_HEADERS` to every
response. `safe_return_to` validates redirect targets.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")
STATIC_PREFIX = "/static/"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_MEDIA_TYPES = frozenset({"application/json", "multipart/form-data"})
STATIC_DIR = Path(__file__).resolve().parent / "static"
BUILD_COOKIE = "rga_build"
SECURITY_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def host_guard(app: ASGIApp) -> ASGIApp:
    """Wrap `app` so any request whose Host is not loopback gets 400 'Invalid host header'."""
    return TrustedHostMiddleware(app, allowed_hosts=list(ALLOWED_HOSTS), www_redirect=False)


def static_build_id(root: Path = STATIC_DIR) -> str:
    """A short hash of every static file's path and bytes: it changes exactly when the scripts or
    styles do."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


class SecurityHeaders:
    """Add `SECURITY_HEADERS` to every HTTP response that does not already carry them.

    Install outermost so guard refusals (Host 400, Origin 403, media-type 415) are covered.
    A page served to a browser that last saw other static files (its `rga_build` cookie differs
    from `build_id`) also clears that browser's cache, so a copy cached before an upgrade (even
    one cached without `no-cache`) never runs against the new pages.
    """

    def __init__(self, app: ASGIApp, build_id: str | None = None) -> None:
        self.app = app
        self.build_id = build_id or static_build_id()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cookies = cookie_parser(Headers(scope=scope).get("cookie", ""))
        stale = cookies.get(BUILD_COOKIE) != self.build_id

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if scope["path"].startswith(STATIC_PREFIX):
                    # Revalidate (cheap: ETag) so an upgraded page never runs a stale script.
                    headers.setdefault("Cache-Control", "no-cache")
                elif stale and headers.get("content-type", "").startswith("text/html"):
                    headers["Clear-Site-Data"] = '"cache"'
                    headers.append(
                        "Set-Cookie",
                        f"{BUILD_COOKIE}={self.build_id}; Path=/; SameSite=Strict; HttpOnly",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class OriginGuard:
    """Reject unsafe-method requests with a foreign Origin (403) or a non-JSON/multipart body (415).

    A request without an Origin header (curl, tests) passes the origin check; browsers always
    send Origin on cross-origin writes. A body is "present" when Content-Length > 0 or
    Transfer-Encoding is set; bodyless requests need no Content-Type.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        refusal = self._refusal(headers)
        if refusal is None:
            await self.app(scope, receive, send)
            return
        status, detail, code = refusal
        await JSONResponse({"detail": detail, "code": code}, status_code=status)(
            scope, receive, send
        )

    @staticmethod
    def _refusal(headers: Headers) -> tuple[int, str, str] | None:
        origin = headers.get("origin")
        if origin is not None and origin.lower() != f"http://{headers.get('host', '').lower()}":
            return 403, "Cross-origin request refused", "bad_origin"
        content_type = headers.get("content-type")
        if content_type is None:
            if _has_body(headers):
                return (
                    415,
                    "Content-Type must be application/json or multipart/form-data",
                    ("unsupported_media_type"),
                )
            return None
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in ALLOWED_MEDIA_TYPES:
            return (
                415,
                "Content-Type must be application/json or multipart/form-data",
                ("unsupported_media_type"),
            )
        return None


def _has_body(headers: Headers) -> bool:
    if "transfer-encoding" in headers:
        return True
    length = headers.get("content-length", "0").strip()
    return not (length.isdigit() and int(length) == 0)


def safe_return_to(path: str | None) -> str | None:
    """Return `path` if it is a same-site relative path, else None.

    Accepts only strings starting with a single '/', with no backslash or control characters
    (browsers treat '/\\host' and '/<tab>/host' as protocol-relative URLs).
    """
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    if "\\" in path or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return None
    return path

"""Private console authentication; credentials are loaded from an owner-only file."""

import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import config


@dataclass(frozen=True)
class Credentials:
    username: bytes
    salt: bytes
    password_hash: bytes
    iterations: int
    public_origin: tuple[str, str, int] | None = None


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("invalid origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def load_credentials() -> Credentials:
    """Fail closed: no generated default password, plaintext password, or public file."""
    try:
        location = config.env("LONGFOR_AUTH_FILE", "AUX_AUTH_FILE").strip()
        if not location:
            raise ValueError("missing authentication file")
        path = Path(location)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise ValueError("invalid authentication file")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("authentication file must be owner-only")
        value = json.loads(path.read_text(encoding="utf-8"))
        username = value["username"]
        iterations = value.get("iterations", 600000)
        if (not isinstance(username, str) or not 1 <= len(username) <= 128
                or any(c in username for c in ":\r\n")):
            raise ValueError("invalid username")
        username_bytes = username.encode("utf-8")
        salt = bytes.fromhex(value["salt"])
        password_hash = bytes.fromhex(value["hash"])
        if not 16 <= len(salt) <= 64 or len(password_hash) != 32:
            raise ValueError("invalid password hash")
        if type(iterations) is not int or not 200000 <= iterations <= 2000000:
            raise ValueError("invalid password hash iterations")
        public = config.env("LONGFOR_PUBLIC_ORIGIN", "AUX_PUBLIC_ORIGIN").strip()
        if public:
            parsed = urlsplit(public)
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("public origin must not contain a path")
        return Credentials(username_bytes, salt, password_hash, iterations,
                           _origin(public) if public else None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("Console authentication is not configured correctly") from None


def _authenticated(header: str, credentials: Credentials) -> bool:
    try:
        method, value = header.split(" ", 1)
        if method.lower() != "basic" or len(value) > 4096:
            return False
        decoded = base64.b64decode(value, validate=True)
        username, separator, password = decoded.partition(b":")
        if not separator or len(password) > 1024:
            return False
    except (ValueError, binascii.Error):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password, credentials.salt, credentials.iterations)
    valid_user = hmac.compare_digest(username, credentials.username)
    valid_password = hmac.compare_digest(candidate, credentials.password_hash)
    return valid_user & valid_password


class ConsoleAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        prefix = scope.get("root_path", "").rstrip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix):] or "/"

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin"),
                ])
                message = {**message, "headers": headers}
            await send(message)

        if path == "/api/health" and scope["method"] == "GET":
            await self.app(scope, receive, secure_send)
            return

        credentials = getattr(scope["app"].state, "console_credentials", None)
        if credentials is None:
            await JSONResponse({"detail": "Console unavailable"}, status_code=503)(scope, receive, secure_send)
            return
        headers = Headers(scope=scope)
        if not await run_in_threadpool(_authenticated, headers.get("authorization", ""), credentials):
            await JSONResponse(
                {"detail": "Authentication required"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Longfor Auto Check-in", charset="UTF-8"'},
            )(scope, receive, secure_send)
            return

        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            allowed = credentials.public_origin
            try:
                allowed = allowed or _origin(f'{scope.get("scheme", "http")}://{headers.get("host", "")}')
                values = [headers[name] for name in ("origin", "referer") if name in headers]
                unsafe = headers.get("sec-fetch-site", "").lower() == "cross-site"
                unsafe = unsafe or any(_origin(value) != allowed for value in values)
            except ValueError:
                unsafe = True
            if unsafe:
                await JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)(
                    scope, receive, secure_send)
                return
        await self.app(scope, receive, secure_send)

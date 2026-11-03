"""Environment-driven configuration for the s7-author MCP server.

Configuration is loaded lazily (never at import time) so that importing the
server module — and therefore tool/resource registration — succeeds even when
the environment variables are not set yet.

Required: ``S7_BASE_URL`` (backend base URL; API paths resolve under
``{S7_BASE_URL}/api/``). ``S7_TOKEN`` (bearer token of a ``UserMCPToken``
— a per-user credential provisioned by a superuser in the admin; level 1 is
schema authoring, level 2 catalog authoring) is required on the stdio
transport — the environment is its only credential source — but optional
under streamable-http, where each request's ``Authorization`` header
carries the token and the env var is only a fallback. ``ProjectAPICredential``
remains as the B2B channel, but the MCP authoring tools assume a user token.
Optional: ``S7_MAX_PAYLOAD_KB`` (default 512), ``S7_TIMEOUT_S`` (default 30)
and ``S7_PROJECT_ID`` — the target project UUID injected as ``project_id``
into the validate/import request bodies and sent as the ``?project_id=``
query parameter on the schema reads and the draft delete. Under
streamable-http the per-request ``X-S7-Project-Id`` header overrides it.
It is required in practice when the credential is a user MCP token (the
backend rejects user-principal reads and writes without ``project_id``) and
ignored/harmless for project credentials — the
backend still enforces its own scope, so a mismatch gets a 403.

The server speaks MCP over stdio by default: nothing may ever be written to
stdout, so all diagnostics go to stderr and missing/invalid settings exit
non-zero — a rule that applies on both transports.
"""

from __future__ import annotations

import math
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Callable, NoReturn, TypeVar
from urllib.parse import urlparse

ENV_BASE_URL = "S7_BASE_URL"
ENV_TOKEN = "S7_TOKEN"
ENV_MAX_PAYLOAD_KB = "S7_MAX_PAYLOAD_KB"
ENV_TIMEOUT_S = "S7_TIMEOUT_S"
ENV_PROJECT_ID = "S7_PROJECT_ID"

DEFAULT_MAX_PAYLOAD_KB = 512
DEFAULT_TIMEOUT_S = 30.0

_T = TypeVar("_T", int, float)


@dataclass(frozen=True)
class S7Config:
    """Runtime configuration resolved from the process environment.

    ``token`` is ``None`` when no bearer credential is available — allowed
    under streamable-http, where a request may resolve one from its
    ``Authorization`` header or travel unauthenticated so the backend
    answers its own 401 verbatim.
    """

    base_url: str
    token: str | None
    max_payload_kb: int = DEFAULT_MAX_PAYLOAD_KB
    timeout_s: float = DEFAULT_TIMEOUT_S
    project_id: str | None = None


def _fatal(message: str) -> NoReturn:
    """Print a clear diagnostic to stderr and exit non-zero.

    Never writes to stdout: that channel carries the MCP protocol frames.
    """
    print(f"s7-author: {message}", file=sys.stderr)
    sys.exit(2)


def _positive_number(name: str, cast: Callable[[str], _T], default: _T) -> _T:
    """Parse an optional env var as a positive number, or return ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        _fatal(f"{name} must be a number (got {raw!r}).")
    if not math.isfinite(value) or value <= 0:
        _fatal(f"{name} must be a positive finite number (got {raw!r}).")
    return value


def get_config(require_token: bool = True) -> S7Config:
    """Resolve configuration from the environment.

    ``S7_BASE_URL`` is always required; ``S7_TOKEN`` is required only when
    ``require_token`` — i.e. on the stdio transport, whose only credential
    source is the environment. Under streamable-http each request's
    ``Authorization`` header supplies the token, so the env var is an
    optional fallback there.

    Exits the process with code 2 and a stderr diagnostic when a required
    variable is missing or an optional one is malformed.
    """
    required = [ENV_BASE_URL] + ([ENV_TOKEN] if require_token else [])
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        hint = (
            "Set S7_BASE_URL (backend base URL) and S7_TOKEN (user MCP "
            "bearer token) in the MCP client configuration."
            if require_token
            else "Set S7_BASE_URL (backend base URL) in the server environment."
        )
        _fatal(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + ". "
            + hint
        )
    base_url = os.environ[ENV_BASE_URL].strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        _fatal(
            f"{ENV_BASE_URL} must be an absolute http(s) URL "
            f"(got {os.environ[ENV_BASE_URL]!r})."
        )
    raw_project_id = os.environ.get(ENV_PROJECT_ID, "").strip()
    project_id = None
    if raw_project_id:
        try:
            project_id = str(uuid.UUID(raw_project_id))
        except ValueError:
            _fatal(f"{ENV_PROJECT_ID} must be a UUID (got {raw_project_id!r}).")
    return S7Config(
        base_url=base_url,
        token=os.environ.get(ENV_TOKEN, "").strip() or None,
        max_payload_kb=_positive_number(ENV_MAX_PAYLOAD_KB, int, DEFAULT_MAX_PAYLOAD_KB),
        timeout_s=_positive_number(ENV_TIMEOUT_S, float, DEFAULT_TIMEOUT_S),
        project_id=project_id,
    )

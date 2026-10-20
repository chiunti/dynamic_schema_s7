"""HTTP client for the dynamic-schema REST API.

Thin wrapper over ``httpx.Client``: bearer authentication, timeout, JSON
request/response handling and an outgoing-payload size guard. It contains no
business logic — every endpoint response body is propagated verbatim so the
``{valid, errors, warnings}`` envelopes reach the caller intact as context.

Error contract:

- Any HTTP response (any status): the JSON body is returned verbatim.
- Transport failure (timeout, DNS, refused, non-JSON body):
  ``{"error": {"code": "transport_error", "message": ..., "status": <HTTP
  status when a response exists, else null>}}``.
- Outgoing payload above ``max_payload_kb``:
  ``{"error": {"code": "payload_too_large", "message": ..., "status": null}}``
  and no request is sent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import S7Config

# All API routes are mounted below this prefix on the backend.
API_PREFIX = "/api/"


def _error(code: str, message: str, status: int | None = None) -> dict[str, Any]:
    """Build the structured client-side error envelope."""
    return {"error": {"code": code, "message": message, "status": status}}


class S7Client:
    """Minimal JSON client for the authenticated ``/api/`` endpoints."""

    def __init__(self, config: S7Config) -> None:
        self._project_id = config.project_id
        self._max_payload_bytes = config.max_payload_kb * 1024
        self._http = httpx.Client(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=httpx.Timeout(config.timeout_s),
            follow_redirects=True,
        )

    def get_json(self, path: str) -> dict[str, Any]:
        """GET ``{base}/api/{path}`` and return the decoded JSON body."""
        return self._request("GET", path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``{base}/api/{path}`` with a JSON body.

        When ``S7_PROJECT_ID`` is configured, ``project_id`` is injected into
        the body — the backend requires it on the schema write endpoints for
        user-principal tokens and tolerates it for project credentials (a
        scope mismatch is still answered with the backend's own error). The
        injection runs before the size check so the limit applies to the
        actual wire body, and the env value wins over any same-named key.

        Refuses to send bodies larger than the configured limit, returning the
        ``payload_too_large`` envelope instead.
        """
        if self._project_id:
            payload = {**payload, "project_id": self._project_id}
        encoded = json.dumps(payload)
        if len(encoded) > self._max_payload_bytes:
            return _error(
                "payload_too_large",
                f"outgoing JSON body is {len(encoded)} bytes, above the "
                f"{self._max_payload_bytes}-byte limit (S7_MAX_PAYLOAD_KB); "
                "split or trim the schema and retry",
            )
        return self._request("POST", path, content=encoded)

    def _request(self, method: str, path: str, content: str | None = None) -> dict[str, Any]:
        """Perform a request against ``/api/{path}`` honoring the error contract."""
        headers = {"Content-Type": "application/json"} if content is not None else None
        try:
            response = self._http.request(method, API_PREFIX + path.lstrip("/"),
                                          content=content, headers=headers)
        except httpx.HTTPError as exc:
            return _error("transport_error", f"{type(exc).__name__}: {exc}")

        try:
            body = response.json()
        except ValueError:
            return _error(
                "transport_error",
                f"HTTP {response.status_code} returned a non-JSON body",
                status=response.status_code,
            )
        # Endpoints always answer JSON objects; wrap scalars/arrays defensively
        # so callers can rely on a dict without altering real payloads.
        return body if isinstance(body, dict) else {"data": body}

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
from urllib.parse import urlencode

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

    @property
    def project_id(self) -> str | None:
        """The configured ``S7_PROJECT_ID``, or ``None`` when unset."""
        return self._project_id

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``{base}/api/{path}`` and return the decoded JSON body.

        When ``params`` is given, its entries are URL-encoded into a query
        string appended to the path; entries whose value is ``None`` are
        skipped, so optional parameters can be forwarded unconditionally.
        The error contract is identical to a param-less GET.
        """
        if params is not None:
            query = urlencode(
                {key: value for key, value in params.items() if value is not None}
            )
            if query:
                separator = "&" if "?" in path else "?"
                path = f"{path}{separator}{query}"
        return self._request("GET", path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``{base}/api/{path}`` with a JSON body.

        When ``S7_PROJECT_ID`` is configured, ``project_id`` is injected into
        every POST body — the client does not scope the injection by route.
        The schema import/validate endpoints require it for user-principal
        tokens and tolerate it for project credentials (a scope mismatch is
        still answered with the backend's own error); endpoints that do not
        declare the field simply ignore the extra key. The injection runs
        before the size check so the limit applies to the actual wire body,
        and the env value wins over any same-named key.

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

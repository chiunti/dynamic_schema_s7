"""HTTP client for the dynamic-schema REST API.

Thin wrapper over ``httpx.Client``: bearer authentication (omitted when the
resolved credential carries no token — the backend then answers its own
401 verbatim), timeout, JSON request/response handling and an
outgoing-payload size guard. It contains no
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
from urllib.parse import quote, urlencode

import httpx

from .config import S7Config

# All API routes are mounted below this prefix on the backend.
API_PREFIX = "/api/"


def _q(value: str) -> str:
    """Quote one URL path segment (same contract as ``server._q``)."""
    return quote(value, safe="")


def _error(code: str, message: str, status: int | None = None) -> dict[str, Any]:
    """Build the structured client-side error envelope."""
    return {"error": {"code": code, "message": message, "status": status}}


def _with_query(path: str, params: dict[str, Any] | None) -> str:
    """Append ``params`` to ``path`` as a URL-encoded query string.

    Entries whose value is ``None`` are skipped, so optional parameters
    can be forwarded unconditionally; an empty encoded result (or
    ``params=None``) leaves ``path`` untouched.
    """
    if params is not None:
        query = urlencode(
            {key: value for key, value in params.items() if value is not None}
        )
        if query:
            separator = "&" if "?" in path else "?"
            path = f"{path}{separator}{query}"
    return path


class S7Client:
    """Minimal JSON client for the authenticated ``/api/`` endpoints."""

    def __init__(self, config: S7Config) -> None:
        self._project_id = config.project_id
        self._max_payload_bytes = config.max_payload_kb * 1024
        # No ``Bearer None``: an unresolved token sends no Authorization
        # header at all, so the backend answers its own 401 verbatim.
        headers = (
            {"Authorization": f"Bearer {config.token}"} if config.token else {}
        )
        self._http = httpx.Client(
            base_url=config.base_url,
            headers=headers,
            timeout=httpx.Timeout(config.timeout_s),
            follow_redirects=True,
        )

    @property
    def project_id(self) -> str | None:
        """The resolved project id for this client's credential, or ``None``."""
        return self._project_id

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``{base}/api/{path}`` and return the decoded JSON body.

        When ``params`` is given, its entries are URL-encoded into a query
        string appended to the path; entries whose value is ``None`` are
        skipped, so optional parameters can be forwarded unconditionally.
        The error contract is identical to a param-less GET.
        """
        return self._request("GET", _with_query(path, params))

    def delete_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """DELETE ``{base}/api/{path}`` and return the decoded JSON body.

        ``params`` is encoded as a query string exactly like ``get_json`` —
        a DELETE carries no request body, so ``project_id`` travels as
        ``?project_id=`` (as on the schema reads), passed explicitly by the
        caller rather than injected into a payload the way ``post_json``
        does. Entries whose value is ``None`` are skipped. The error
        contract is identical to GET/POST.
        """
        return self._request("DELETE", _with_query(path, params))

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``{base}/api/{path}`` with a JSON body.

        When the client was built with a project id (``S7_PROJECT_ID`` env or
        the request's ``X-S7-Project-Id`` header), ``project_id`` is injected
        into every POST body — the client does not scope the injection by
        route.
        The schema import/validate endpoints require it for user-principal
        tokens and tolerate it for project credentials (a scope mismatch is
        still answered with the backend's own error); endpoints that do not
        declare the field simply ignore the extra key. The injection runs
        before the size check so the limit applies to the actual wire body,
        and the resolved value wins over any same-named key.

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

    def patch_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PATCH ``{base}/api/{path}`` with a JSON body.

        Same contract as ``post_json``: when the client was built with a
        project id, ``project_id`` is injected into the body before the
        size check; an encoded body above the configured limit returns
        the ``payload_too_large`` envelope without sending a request;
        every HTTP response body is returned verbatim.
        """
        if self._project_id:
            payload = {**payload, "project_id": self._project_id}
        encoded = json.dumps(payload)
        if len(encoded) > self._max_payload_bytes:
            return _error(
                "payload_too_large",
                f"outgoing JSON body is {len(encoded)} bytes, above the "
                f"{self._max_payload_bytes}-byte limit (S7_MAX_PAYLOAD_KB); "
                "split or trim the payload and retry",
            )
        return self._request("PATCH", path, content=encoded)

    def propose_changeset(self, ops: list[dict], rationale: str) -> dict[str, Any]:
        """Propose a governed catalog changeset — ``POST catalog/changesets/``.

        Sends ``{"ops": ops, "rationale": rationale}`` (``project_id`` is
        injected by ``post_json`` when configured). The endpoint persists
        the changeset and validates it synchronously, so the 201 envelope
        is the serialized changeset — ``{id, status, ops, rationale,
        scopes, base_revision, validation_report, proposed_by_credential,
        proposed_by_user, reviewed_by, review_note, applied_at,
        created_at,
        updated_at}`` — with ``status`` already ``valid`` or ``invalid``.
        When the token also carries the ``can_auto_apply_additive``
        capability and the validated payload is purely additive — only
        ``add_*`` ops, no ``warning``/``breaking`` findings — the
        backend chains approve+apply inside the same request, so
        ``status`` may answer ``applied`` directly (or ``stale`` when an
        apply-time gate detects catalog drift), with ``reviewed_by``
        set to the token's user. A validation failure is still a
        persisted 201, not an error:
        ``validation_report.findings`` (``{op_index, severity, entity,
        code, message, remediation?}`` entries) is the feedback channel.

        Error envelopes (all verbatim): 400 ``{"error":
        "invalid_request", "message"}`` on a malformed body (missing or
        blank ``rationale``, empty or non-list ``ops``); 401 without a
        bearer credential; 403 for a project credential, a level-1 user
        MCP token or a token without ``can_propose_changeset``; a second
        403 ``{"error": "changeset_scopes_not_covered", "message"}`` after
        validation when the token's ``allowed_scopes`` do not cover the
        server-side derived scopes — the proposal rolls back and nothing
        persists.
        """
        return self.post_json(
            "catalog/changesets/", {"ops": ops, "rationale": rationale}
        )

    def get_changeset(self, changeset_id: str) -> dict[str, Any]:
        """Fetch one changeset — ``GET catalog/changesets/<changeset_id>/``.

        The 200 envelope is the same serialized changeset shape as
        ``propose_changeset`` — ``status`` plus the full
        ``validation_report`` the proposer reads to correct and
        re-propose. An unknown id answers 404 ``{"error":
        "changeset_not_found", "changeset_id": "<id>"}`` echoing the
        addressed value; a token whose ``allowed_scopes`` do not cover
        the changeset's derived ``scopes`` answers 403 ``{"error":
        "changeset_scopes_not_covered", "scopes": [...]}``.
        """
        return self.get_json(f"catalog/changesets/{_q(changeset_id)}/")

    def fix_changeset(
        self, changeset_id: str, ops: list[dict], rationale: str
    ) -> dict[str, Any]:
        """Replace ops/rationale on a changeset — ``PATCH catalog/changesets/<id>/``.

        The correction arm of the feedback loop: the endpoint takes the
        same ``{"ops", "rationale"}`` body as the propose call — a fresh
        non-blank ``rationale`` is required on every PATCH — replaces
        ``ops`` wholesale (never merged) and re-validates synchronously.
        The 200 envelope is the re-validated serialized changeset at
        ``valid``/``invalid`` — or ``applied``/``stale`` under the same
        ``can_auto_apply_additive`` conditions as the propose, a purely
        additive revision auto-applying in-request — with a refreshed
        ``validation_report``.

        Error envelopes: 404 ``{"error": "changeset_not_found",
        "changeset_id"}`` on an unknown id; 409 ``{"error": "changeset
        cannot transition from '<status>' to 'validating'"}`` when the
        current status has no ``→ validating`` edge — only ``proposed``,
        ``invalid``, ``valid`` and ``stale`` accept PATCH (``draft``,
        ``validating``, ``approved``, ``applied``, ``rejected`` and
        ``superseded`` answer 409); 403 ``{"error":
        "changeset_scopes_not_covered", "message"}`` when the token's
        ``allowed_scopes`` do not cover the derived scopes (the patch
        rolls back); 400 ``{"error": "invalid_request", "message"}`` on a
        malformed body.
        """
        return self.patch_json(
            f"catalog/changesets/{_q(changeset_id)}/",
            {"ops": ops, "rationale": rationale},
        )

    def _request(self, method: str, path: str, content: str | None = None) -> dict[str, Any]:
        """Perform a request against ``/api/{path}`` honoring the error contract."""
        headers = {"Content-Type": "application/json"} if content is not None else None
        try:
            response = self._http.request(method, API_PREFIX + path.lstrip("/"),
                                          content=content, headers=headers)
        # httpx raises a plain RuntimeError — not an HTTPError — when a
        # request is dispatched on an already-closed client (e.g. a pool
        # evicted from the server's bounded LRU between resolving the
        # client and dispatching); it is still a transport failure for the
        # caller, so it maps to the same envelope.
        except (httpx.HTTPError, RuntimeError) as exc:
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

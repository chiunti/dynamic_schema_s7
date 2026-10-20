"""s7-author: MCP stdio server wrapping the dynamic-schema REST API.

Thin mapping only — each tool forwards to one authenticated ``/api/`` endpoint
and returns the JSON envelope verbatim. There is no business logic here, and
no publish/approve/apply tools by design: publishing is a human action.

Configuration is resolved lazily inside the handlers (never at import time),
so importing this module — which registers every tool, resource and prompt —
succeeds without any environment variables set.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import __version__
from .client import S7Client
from .config import S7Config, get_config
from .prompts import AUTHOR_CATALOG_PROMPT, CREATE_TRAMITE_FORM_PROMPT

try:
    mcp = MCPServer("s7-author", version=__version__)
except TypeError:  # pragma: no cover - mcp 1.x FastMCP has no version kwarg
    mcp = MCPServer("s7-author")

_client: S7Client | None = None


def _get_client() -> S7Client:
    """Return the shared API client, building it lazily on first use."""
    global _client
    if _client is None:
        _client = S7Client(get_config())
    return _client


def _q(value: str) -> str:
    """Quote one URL path segment."""
    return quote(value, safe="")


def _schema_payload(node_type: str, schema: dict | str,
                    schema_key: str | None, schema_version: str | None) -> dict[str, Any]:
    """Build the shared validate/import request body.

    ``schema`` may arrive as a JSON object or as a raw JSON string; objects are
    dumped to ``schema_text`` as the endpoint expects. No ``schema_status`` is
    ever sent — credential imports are persisted as ``draft`` by the backend.
    ``project_id`` is not part of the tool surface either: the client injects
    it from ``S7_PROJECT_ID`` when that variable is configured.
    """
    payload: dict[str, Any] = {
        "schema_text": schema if isinstance(schema, str) else json.dumps(schema),
        "node_type": node_type,
    }
    if schema_key:
        payload["schema_key"] = schema_key
    if schema_version:
        payload["schema_version"] = schema_version
    return payload


# ---------------------------------------------------------------------------
# Tools — one per authenticated endpoint. Envelopes are returned verbatim.
# ---------------------------------------------------------------------------


@mcp.tool()
def s7_get_catalog(scope: str) -> dict:
    """Fetch the resolved authoring catalog for a scope.

    Returns ``{scope, root_node_type, node_types, domains, example}`` where
    each node-type entry carries its attributes, resolved ``variants`` block
    and ``compositions`` grouped by collection_key. ``{"error": ...}`` when the
    scope does not exist.
    """
    return _get_client().get_json(f"catalog/{_q(scope)}/")


@mcp.tool()
def s7_get_schema(node_type: str, key: str, version: str) -> dict:
    """Fetch a published schema as ``{"data": <schema>, "meta": {...}}``.

    Use it for few-shot reference and to verify an import round-trips after a
    human publishes the draft. On 404 the endpoint returns an RFC-7807 body
    ``{"type": "schema_not_found", "title", "detail", "status": 404}``.
    """
    return _get_client().get_json(f"schema/{_q(node_type)}/{_q(key)}/{_q(version)}/")


@mcp.tool()
def s7_get_draft(node_type: str, key: str, version: str) -> dict:
    """Fetch a draft schema as ``{"data": <schema>, "meta": {...}}``.

    Main use: verify an ``s7_import_draft`` round-trip right after importing,
    without waiting for a human to publish — ``meta.status`` is always
    ``"draft"``. A schema that does not exist or is not a draft answers 404
    with the RFC-7807 body ``{"type": "schema_not_found", "title", "detail",
    "status": 404}``.

    Dual auth: project credentials work as-is; a user MCP token is not bound
    to a project, so the backend requires ``?project_id=<uuid>`` — set
    ``S7_PROJECT_ID`` and the client sends it automatically (missing answers
    ``400 {"error": "project_id is required"}``, a scope mismatch 403).
    """
    client = _get_client()
    return client.get_json(
        f"schema/draft/{_q(node_type)}/{_q(key)}/{_q(version)}/",
        params={"project_id": client.project_id},
    )


@mcp.tool()
def s7_list_datatypes() -> dict:
    """List the universal DataType catalog as ``{"datatypes": [...]}``."""
    return _get_client().get_json("catalog/datatypes/")


@mcp.tool()
def s7_validate_schema(node_type: str, schema: dict | str,
                       schema_key: str | None = None,
                       schema_version: str | None = None) -> dict:
    """Dry-run the import pipeline without persisting.

    ``schema`` accepts a JSON object or a JSON string. Returns
    ``{"valid": bool, "errors": [{code, message, path, expected, received}],
    "warnings": [...]}`` — iterate until ``valid`` is true before importing.
    """
    payload = _schema_payload(node_type, schema, schema_key, schema_version)
    return _get_client().post_json("schema/validate/", payload)


@mcp.tool()
def s7_import_draft(node_type: str, schema: dict | str,
                    schema_key: str | None = None,
                    schema_version: str | None = None,
                    overwrite: bool = False) -> dict:
    """Import a schema JSON; it is always persisted as a draft.

    ``schema`` accepts a JSON object or a JSON string. Returns the response
    body ``{"schema_id", "warning", "forced_status"?}`` (HTTP 201 on success).
    There is no status parameter on purpose — credential imports are forced
    to ``draft``.
    """
    payload = _schema_payload(node_type, schema, schema_key, schema_version)
    payload["overwrite"] = overwrite
    return _get_client().post_json("schema/import/", payload)


@mcp.tool()
def s7_import_catalog(objects: list[dict]) -> dict:
    """Apply a fixture-format catalog payload (MCP level 2).

    ``objects`` is a list of ``{"model": "schemas.<label>", "pk": <uuid?>?,
    "fields": {...}}`` objects — same shape as Django fixtures. Allowed
    labels: domain, domainitem, nodetype, nodetypecomposition,
    nodetypevariant, attributedef. Natural-key FKs are used inside
    ``fields`` exactly like fixture files. Natural-key match updates the
    existing row; no match creates it; deletes are not supported.

    Returns ``{"applied": true, "created": {...}, "updated": {...},
    "warnings": [...]}`` on success; ``{"applied": false, "errors": [...]}``
    on validation/apply failure. Requires a user MCP token with
    ``mcp_level >= 2`` plus per-model Django permissions — anything else
    gets a 401/403 envelope verbatim.
    """
    return _get_client().post_json("catalog/import/", {"objects": objects})


# ---------------------------------------------------------------------------
# Resources — read-only context for the agent.
# ---------------------------------------------------------------------------


@mcp.resource("s7://catalog/{scope}", mime_type="application/json")
def catalog_resource(scope: str) -> dict:
    """Resolved authoring catalog for ``scope`` (same payload as s7_get_catalog)."""
    return _get_client().get_json(f"catalog/{_q(scope)}/")


@mcp.resource("s7://datatypes", mime_type="application/json")
def datatypes_resource() -> dict:
    """Universal DataType catalog (same payload as s7_list_datatypes)."""
    return _get_client().get_json("catalog/datatypes/")


@mcp.resource("s7://examples/{scope}", mime_type="application/json")
def example_resource(scope: str) -> Any:
    """Generated JSON example for ``scope`` — always current with the engine."""
    catalog = _get_client().get_json(f"catalog/{_q(scope)}/")
    if "example" not in catalog:
        return catalog  # error envelope — propagate verbatim
    # Explicit marker when the engine produced no example for the scope.
    return catalog["example"] if catalog["example"] is not None else {"example": None}


@mcp.resource("s7://schemas/{node_type}/{key}/{version}", mime_type="application/json")
def schema_resource(node_type: str, key: str, version: str) -> Any:
    """Published schema JSON of the project (few-shot reference)."""
    body = _get_client().get_json(f"schema/{_q(node_type)}/{_q(key)}/{_q(version)}/")
    return body.get("data", body)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def create_tramite_form() -> str:
    """Step-by-step workflow to author a government-procedure form schema."""
    return CREATE_TRAMITE_FORM_PROMPT


@mcp.prompt()
def author_catalog() -> str:
    """Step-by-step workflow to author catalog grammar (level 2)."""
    return AUTHOR_CATALOG_PROMPT


def main() -> None:
    """Entry point: validate configuration, then serve MCP over stdio."""
    get_config()  # fail fast on missing env before serving the protocol
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()

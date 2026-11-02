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
from .catalog_grammar import CATALOG_GRAMMAR
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

    Returns ``{scope, root_node_type, node_types, domains, datatypes,
    example}`` where each node-type entry carries its attributes, resolved
    ``variants`` block and ``compositions`` grouped by collection_key, and
    ``datatypes`` carries grammar docs for the non-trivial data types used
    by the scope. ``scope`` is the root node type's ``json_scope``, not its
    ``name`` — an unknown scope answers
    ``{"error": "scope_not_found", "available_scopes": [...]}``, where
    ``available_scopes`` lists every live root scope.
    """
    return _get_client().get_json(f"catalog/{_q(scope)}/")


@mcp.tool()
def s7_get_schema(node_type: str, key: str, version: str) -> dict:
    """Fetch a published schema as ``{"data": <schema>, "meta": {...}}``.

    Use it for few-shot reference and to verify an import round-trips after a
    human publishes the draft. On 404 the endpoint returns an RFC-7807 body
    ``{"type": "schema_not_found", "title", "detail", "status": 404}``.

    Dual auth: project credentials work as-is; a user MCP token is not bound
    to a project, so the backend requires ``?project_id=<uuid>`` — set
    ``S7_PROJECT_ID`` and the client sends it automatically (missing answers
    ``400 {"error": "project_id is required"}``, an inaccessible project or
    scope mismatch 403).
    """
    client = _get_client()
    return client.get_json(
        f"schema/{_q(node_type)}/{_q(key)}/{_q(version)}/",
        params={"project_id": client.project_id},
    )


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
def s7_delete_draft(node_type: str, key: str, version: str) -> dict:
    """Delete a draft schema — destructive and irreversible.

    Deleting the draft frees the node types it implemented (the catalog
    usage gate counts any existing schema). Success returns
    ``{"deleted": true, "meta": {...}}``. Draft-only: a schema that does
    not exist or is not a draft answers 404 with the same RFC-7807 body
    as the read — ``{"type": "schema_not_found", "title", "detail",
    "status": 404}`` — so published/archived schemas can never be
    deleted through this endpoint.

    **User MCP token only (any level)** — this is *not* dual-auth:
    project credentials are rejected with 403. A user MCP token is not
    bound to a project, so the backend requires ``?project_id=<uuid>`` —
    set ``S7_PROJECT_ID`` and the client sends it automatically (missing
    answers ``400 {"error": "project_id is required"}``, a project the
    user cannot edit 403).
    """
    client = _get_client()
    return client.delete_json(
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
    existing row; no match creates it. An update replaces the whole row:
    omitting a field that holds a non-default value on the live row is
    rejected, so resubmit the full object on every update. An update
    that changes at least one field on a row implemented by existing
    schemas (``Node``/``NodeAttribute`` rows in any project) is rejected
    with ``catalog_object_in_use`` plus a ``usage`` detail — creates and
    no-op updates (every payload field equal to the live row) are
    exempt, so idempotent re-imports keep working.

    A delete object is ``{"model": "schemas.<label>", "delete": true}``
    plus exactly one identifier — ``pk`` (uuid) XOR ``fields`` holding
    the complete natural key, never both. ``delete`` must be literal
    ``true`` (any other value is a validation error) and object keys
    outside ``model``/``pk``/``fields``/``delete`` are rejected. Every
    delete requires the ``schemas.delete_<label>`` permission; deleting
    a ``nodetype`` first removes its own dependents — the attributedef,
    variant and composition rows touching it, each gated and each
    requiring its own ``delete_<label>`` — while a variant of another
    node type bound to it through ``props_node_type`` is detached
    (``SET_NULL``), not deleted: still usage-gated, so it can block the
    delete with ``catalog_object_in_use`` naming a row the caller did
    not ask to delete. A row implemented by existing
    schemas (``Node``/``NodeAttribute`` in any project) rejects the
    delete with ``catalog_object_in_use`` plus a ``usage`` detail —
    deletes get no no-op exemption, and the check is an advisory read
    under READ COMMITTED. Upserts apply first by
    dependency rank, then deletes in reverse rank (children before
    parents), so a rename is a create with the new natural key plus a
    delete of the old one in the same atomic payload. Deleting a row
    that does not exist is a per-object ``invalid_catalog_object``
    error at ``$.objects[i]``.

    Returns ``{"applied": true, "created": {...}, "updated": {...},
    "deleted": {...}, "results": [{index, model, natural_key, action,
    changed_fields}], "warnings": [...]}`` on success — ``deleted``
    counts rows per label (dependents included) and ``results[]``
    reports ``action`` ``created``/``updated``/``deleted``;
    ``{"applied": false, "errors": [...]}`` on validation/apply failure.
    Requires a user MCP token with ``mcp_level >= 2`` plus per-model
    Django permissions — anything else gets a 401/403 envelope verbatim.
    """
    return _get_client().post_json("catalog/import/", {"objects": objects})


@mcp.tool()
def s7_catalog_usage(model: str, pk: str | None = None,
                     fields: dict | None = None) -> dict:
    """Pre-check the ``catalog_object_in_use`` gate for one catalog row.

    Call it on each target row BEFORE proposing field-changing updates
    or delete objects in ``s7_import_catalog``: a row implemented by
    existing schemas (``Node``/``NodeAttribute`` rows in any project)
    is rejected there with ``catalog_object_in_use`` — this read
    returns the same evidence without spending the transaction. The
    200 body is ``{"implemented": bool, "usage": {...}}`` mirroring
    the rejection's ``usage`` detail — counts of the implementing
    rows (``nodes``/``node_attributes``) plus the slugs of the
    projects that own them.

    ``model`` is one authorizable ``schemas.<label>`` — domain,
    domainitem, nodetype, nodetypecomposition, nodetypevariant,
    attributedef. The row takes exactly one identifier — ``pk``
    (uuid) XOR ``fields`` carrying the natural-key components as
    flat entries, the same vocabulary as the delete objects:
    ``{"name": "field"}`` for a nodetype, ``{"domain":
    "<domain_name>", "value": "<value>"}`` for a domainitem,
    ``{"node_type": ..., "json_key": ..., "variant_key": ...}`` for
    an attributedef. An omitted nullable component resolves as NULL;
    omitting a required component is an incomplete natural key. A
    ``pk=None`` never travels — the client drops None-valued params.

    A missing row answers 404 ``{"error":
    "catalog_object_not_found", "model": ..., "identifier": {...}}``;
    a missing ``model``, a non-authorizable label, both or neither
    identifier, an unknown param or an incomplete natural key
    answers 400 ``{"error": "invalid_request", "message": ...}``.

    Dual auth: a user MCP token with ``mcp_level >= 2`` or a project
    credential with ``can_read``. Advisory read under READ COMMITTED
    — the same TOCTOU window as the gate applies, so the answer can
    go stale before the import runs.
    """
    params = {"model": model, "pk": pk}
    # ``fields`` carries natural-key components only — a stray
    # ``model``/``pk`` key must not silently override the base args.
    params.update(
        {k: v for k, v in (fields or {}).items() if k not in params}
    )
    return _get_client().get_json("catalog/usage/", params=params)


# ---------------------------------------------------------------------------
# Resources — read-only context for the agent.
# ---------------------------------------------------------------------------


@mcp.resource("s7://catalog/{scope}", mime_type="application/json")
def catalog_resource(scope: str) -> dict:
    """Resolved authoring catalog for ``scope`` (same payload as s7_get_catalog).

    ``scope`` is the root node type's ``json_scope``; an unknown scope
    answers ``{"error": "scope_not_found", "available_scopes": [...]}``.
    """
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
    """Published schema JSON of the project (few-shot reference).

    Same endpoint as ``s7_get_schema`` — dual auth: a user MCP token needs
    ``S7_PROJECT_ID`` set (sent as ``?project_id=``).
    """
    client = _get_client()
    body = client.get_json(
        f"schema/{_q(node_type)}/{_q(key)}/{_q(version)}/",
        params={"project_id": client.project_id},
    )
    return body.get("data", body)


@mcp.resource("s7://catalog-grammar", mime_type="application/json")
def catalog_grammar_resource() -> dict:
    """Per-model field reference for the s7_import_catalog fixture payload.

    Static document shipped with the package — catalog-grammar metadata has
    no REST endpoint; it is versioned here and kept honest by a backend-side
    drift test.
    """
    return CATALOG_GRAMMAR


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

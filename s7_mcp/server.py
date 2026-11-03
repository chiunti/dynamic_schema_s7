"""s7-author: MCP server wrapping the dynamic-schema REST API.

Thin mapping only — each tool forwards to one authenticated ``/api/`` endpoint
and returns the JSON envelope verbatim. There is no business logic here, and
no publish/approve/apply tools by design: publishing is a human action.

Two transports share this surface: stdio (the default, one process per local
agent — credentials come from the ``S7_*`` environment) and streamable-http
(``--transport streamable-http``, serving ``/mcp``): each inbound request's
``Authorization`` and ``X-S7-Project-Id`` headers supply the credential,
which is forwarded verbatim — the backend authenticates per request.

Configuration is resolved lazily inside the handlers (never at import time),
so importing this module — which registers every tool, resource and prompt —
succeeds without any environment variables set.
"""

from __future__ import annotations

import argparse
import json
import threading
from collections import OrderedDict
from dataclasses import replace
from typing import Any
from urllib.parse import quote

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Context, MCPServer
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as MCPServer

from . import __version__
from .catalog_grammar import CATALOG_GRAMMAR
from .client import S7Client
from .config import S7Config, get_config
from .prompts import AUTHOR_CATALOG_PROMPT, CREATE_TRAMITE_FORM_PROMPT

try:
    mcp = MCPServer("s7-author", version=__version__)
except TypeError:  # pragma: no cover - mcp 1.x FastMCP has no version kwarg
    mcp = MCPServer("s7-author")

# API clients are cached per resolved credential, not per process: under
# streamable-http each request can carry its own bearer/project pair, so
# ``(token, project_id)`` is the cache key. The env-only credential is simply
# the entry whose key holds the configured (or absent) values — the stdio
# path's old singleton.
#
# The cache is a bounded LRU: a hit moves the entry to the most-recently-used
# end and inserting past ``_MAX_CACHED_CLIENTS`` evicts the least-recently-used
# client and closes its ``httpx`` pool, so a long-lived hosted process does
# not accumulate pools of rotated credentials. ``_clients_lock`` covers the
# whole get-or-create in ``_get_client`` — two concurrent first requests for
# the same credential build exactly one client (no orphaned pool).
_MAX_CACHED_CLIENTS = 128
_clients: OrderedDict[tuple[str | None, str | None], S7Client] = OrderedDict()
_clients_lock = threading.Lock()

# Inbound header names carrying the per-request credential (case-insensitive
# on the wire; Starlette's Headers already normalize lookup).
_HEADER_AUTHORIZATION = "authorization"
_HEADER_PROJECT_ID = "x-s7-project-id"


def _request_headers(ctx: Context | None) -> Any:
    """Return the inbound HTTP headers of the current request, or ``None``.

    Under streamable-http ``ctx.request_context.request`` is the Starlette
    ``Request`` of the POST that carried the MCP message; under stdio the
    request is ``None`` — as it is for a bare ``Context`` built outside a
    request — so both resolve to ``None`` (env-only credentials).
    """
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError):
        return None
    return getattr(request, "headers", None)


def _resolve_credentials(
    ctx: Context | None, config: S7Config
) -> tuple[str | None, str | None]:
    """Resolve ``(token, project_id)`` for the current request.

    Per-request headers win over the environment field by field:
    ``Authorization: Bearer <token>`` over ``S7_TOKEN`` and
    ``X-S7-Project-Id`` over ``S7_PROJECT_ID``. A present-but-unresolvable
    ``Authorization`` (non-Bearer scheme or empty value) resolves to
    ``None`` — never silently promoted to the env credential — so the
    backend answers its own 401 verbatim. ``X-S7-Project-Id`` travels
    verbatim too: the backend validates its shape. With no inbound headers
    (stdio) both fields are the env values.
    """
    headers = _request_headers(ctx)
    if headers is None:
        return config.token, config.project_id

    token = config.token
    authorization = headers.get(_HEADER_AUTHORIZATION)
    if authorization is not None:
        parts = authorization.strip().split(None, 1)
        token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None
    project_id = config.project_id
    header_project = headers.get(_HEADER_PROJECT_ID)
    if header_project is not None:
        project_id = header_project.strip() or None
    return token, project_id


def _get_client(ctx: Context | None = None) -> S7Client:
    """Return the API client for the current request's credential.

    Under streamable-http the credential is resolved from the inbound
    headers (falling back to the environment field by field); under stdio
    it is the env credential. Clients are cached per resolved
    ``(token, project_id)`` so distinct tenants never share a connection
    pool.

    The cache is a bounded LRU (``_MAX_CACHED_CLIENTS`` entries): a hit
    refreshes the entry's position and inserting past the bound evicts the
    least-recently-used client, closing its ``httpx`` pool. The whole
    get-or-create runs under ``_clients_lock``, so concurrent first
    requests for one credential build a single client. The env-only
    credential follows the same policy — it is one entry like any other.

    An entry may be evicted — and its pool closed — while a caller still
    holds it: a request in flight can see its client go stale if enough
    newer credentials arrive meanwhile. The failed request surfaces as a
    transport error and self-heals on the next call, which resolves a
    fresh client — an inherent trade-off of pooling without
    refcounting.
    """
    config = get_config(require_token=False)
    credentials = _resolve_credentials(ctx, config)
    with _clients_lock:
        client = _clients.get(credentials)
        if client is not None:
            _clients.move_to_end(credentials)
            return client
        if _clients and len(_clients) >= _MAX_CACHED_CLIENTS:
            _, evicted = _clients.popitem(last=False)
            try:
                evicted._http.close()
            except Exception:  # a broken pool must not break the request
                pass
        client = S7Client(
            replace(config, token=credentials[0], project_id=credentials[1])
        )
        _clients[credentials] = client
        return client


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
    the resolved project id — the ``X-S7-Project-Id`` header value under
    streamable-http, ``S7_PROJECT_ID`` otherwise.
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
def s7_get_catalog(scope: str, ctx: Context | None = None) -> dict:
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
    return _get_client(ctx).get_json(f"catalog/{_q(scope)}/")


@mcp.tool()
def s7_get_schema(node_type: str, key: str, version: str,
                  ctx: Context | None = None) -> dict:
    """Fetch a published schema as ``{"data": <schema>, "meta": {...}}``.

    Use it for few-shot reference and to verify an import round-trips after a
    human publishes the draft. On 404 the endpoint returns an RFC-7807 body
    ``{"type": "schema_not_found", "title", "detail", "status": 404}``.

    Dual auth: project credentials work as-is; a user MCP token is not bound
    to a project, so the backend requires ``?project_id=<uuid>`` — resolved
    from the ``X-S7-Project-Id`` header under streamable-http or from
    ``S7_PROJECT_ID`` otherwise, and sent automatically (missing answers
    ``400 {"error": "project_id is required"}``, an inaccessible project or
    scope mismatch 403).
    """
    client = _get_client(ctx)
    return client.get_json(
        f"schema/{_q(node_type)}/{_q(key)}/{_q(version)}/",
        params={"project_id": client.project_id},
    )


@mcp.tool()
def s7_get_draft(node_type: str, key: str, version: str,
                 ctx: Context | None = None) -> dict:
    """Fetch a draft schema as ``{"data": <schema>, "meta": {...}}``.

    Main use: verify an ``s7_import_draft`` round-trip right after importing,
    without waiting for a human to publish — ``meta.status`` is always
    ``"draft"``. A schema that does not exist or is not a draft answers 404
    with the RFC-7807 body ``{"type": "schema_not_found", "title", "detail",
    "status": 404}``.

    Dual auth: project credentials work as-is; a user MCP token is not bound
    to a project, so the backend requires ``?project_id=<uuid>`` — resolved
    from the ``X-S7-Project-Id`` header under streamable-http or from
    ``S7_PROJECT_ID`` otherwise, and sent automatically (missing answers
    ``400 {"error": "project_id is required"}``, a scope mismatch 403).
    """
    client = _get_client(ctx)
    return client.get_json(
        f"schema/draft/{_q(node_type)}/{_q(key)}/{_q(version)}/",
        params={"project_id": client.project_id},
    )


@mcp.tool()
def s7_delete_draft(node_type: str, key: str, version: str,
                    ctx: Context | None = None) -> dict:
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
    resolved from the ``X-S7-Project-Id`` header under streamable-http or
    from ``S7_PROJECT_ID`` otherwise, and sent automatically (missing
    answers ``400 {"error": "project_id is required"}``, a project the
    user cannot edit 403).
    """
    client = _get_client(ctx)
    return client.delete_json(
        f"schema/draft/{_q(node_type)}/{_q(key)}/{_q(version)}/",
        params={"project_id": client.project_id},
    )


@mcp.tool()
def s7_list_datatypes(ctx: Context | None = None) -> dict:
    """List the universal DataType catalog as ``{"datatypes": [...]}``."""
    return _get_client(ctx).get_json("catalog/datatypes/")


@mcp.tool()
def s7_validate_schema(node_type: str, schema: dict | str,
                       schema_key: str | None = None,
                       schema_version: str | None = None,
                       ctx: Context | None = None) -> dict:
    """Dry-run the import pipeline without persisting.

    ``schema`` accepts a JSON object or a JSON string. Returns
    ``{"valid": bool, "errors": [{code, message, path, expected, received}],
    "warnings": [...]}`` — iterate until ``valid`` is true before importing.
    """
    payload = _schema_payload(node_type, schema, schema_key, schema_version)
    return _get_client(ctx).post_json("schema/validate/", payload)


@mcp.tool()
def s7_import_draft(node_type: str, schema: dict | str,
                    schema_key: str | None = None,
                    schema_version: str | None = None,
                    overwrite: bool = False,
                    ctx: Context | None = None) -> dict:
    """Import a schema JSON; it is always persisted as a draft.

    ``schema`` accepts a JSON object or a JSON string. Returns the response
    body ``{"schema_id", "warning", "forced_status"?}`` (HTTP 201 on success).
    There is no status parameter on purpose — credential imports are forced
    to ``draft``.
    """
    payload = _schema_payload(node_type, schema, schema_key, schema_version)
    payload["overwrite"] = overwrite
    return _get_client(ctx).post_json("schema/import/", payload)


@mcp.tool()
def s7_import_catalog(objects: list[dict], ctx: Context | None = None) -> dict:
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
    return _get_client(ctx).post_json("catalog/import/", {"objects": objects})


@mcp.tool()
def s7_catalog_usage(model: str, pk: str | None = None,
                     fields: dict | None = None,
                     ctx: Context | None = None) -> dict:
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
    return _get_client(ctx).get_json("catalog/usage/", params=params)


# ---------------------------------------------------------------------------
# Resources — read-only context for the agent.
# ---------------------------------------------------------------------------


@mcp.resource("s7://catalog/{scope}", mime_type="application/json")
def catalog_resource(scope: str, ctx: Context | None = None) -> dict:
    """Resolved authoring catalog for ``scope`` (same payload as s7_get_catalog).

    ``scope`` is the root node type's ``json_scope``; an unknown scope
    answers ``{"error": "scope_not_found", "available_scopes": [...]}``.
    """
    return _get_client(ctx).get_json(f"catalog/{_q(scope)}/")


@mcp.resource("s7://datatypes", mime_type="application/json")
def datatypes_resource() -> dict:
    """Universal DataType catalog (same payload as s7_list_datatypes).

    Static resource — the SDK injects no per-request context into
    URI-template-less resources, so under streamable-http credentials
    resolve from the environment only (``S7_TOKEN`` / ``S7_PROJECT_ID``).
    """
    return _get_client().get_json("catalog/datatypes/")


@mcp.resource("s7://examples/{scope}", mime_type="application/json")
def example_resource(scope: str, ctx: Context | None = None) -> Any:
    """Generated JSON example for ``scope`` — always current with the engine."""
    catalog = _get_client(ctx).get_json(f"catalog/{_q(scope)}/")
    if "example" not in catalog:
        return catalog  # error envelope — propagate verbatim
    # Explicit marker when the engine produced no example for the scope.
    return catalog["example"] if catalog["example"] is not None else {"example": None}


@mcp.resource("s7://schemas/{node_type}/{key}/{version}", mime_type="application/json")
def schema_resource(node_type: str, key: str, version: str,
                    ctx: Context | None = None) -> Any:
    """Published schema JSON of the project (few-shot reference).

    Same endpoint as ``s7_get_schema`` — dual auth: a user MCP token needs
    the resolved project id (``X-S7-Project-Id`` header under
    streamable-http, ``S7_PROJECT_ID`` otherwise; sent as ``?project_id=``).
    """
    client = _get_client(ctx)
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
    drift test. Static resources receive no per-request context, so this one
    performs no credential resolution at all (no backend call is made).
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


def _run_http(host: str, port: int) -> None:
    """Serve MCP over streamable-http at ``/mcp`` (SDK v1/v2 compat).

    mcp 2.x ``MCPServer.run`` forwards ``host``/``port`` kwargs to
    ``run_streamable_http_async``; mcp 1.x ``FastMCP.run`` only accepts
    ``transport``, so the bind address goes on ``mcp.settings`` instead —
    same try/except shape as the import shim (the broad ``TypeError`` also
    swallows a genuine runtime type error into a v1 retry — an accepted
    compat trade-off). The path is ``/mcp`` in both generations.

    v1 caveat: ``settings.host`` is mutated after construction, but the
    SDK's DNS-rebinding protection was already armed with a localhost-only
    allowlist — a v1 server bound to ``0.0.0.0`` rejects non-localhost
    ``Host`` headers. Edge-routed deployments therefore require mcp 2.x
    (the resolved version in practice; the floor is ``mcp>=1.19``).
    """
    try:
        mcp.run(transport="streamable-http", host=host, port=port)
    except TypeError:  # pragma: no cover - mcp 1.x run() takes no kwargs
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse the transport, validate config, then serve MCP.

    ``--transport stdio`` (default) keeps the original behavior: env-only
    credentials, ``S7_TOKEN`` required. ``--transport streamable-http``
    serves ``/mcp`` over HTTP: each request's ``Authorization`` /
    ``X-S7-Project-Id`` headers supply the credential, so ``S7_TOKEN``
    becomes an optional fallback while ``S7_BASE_URL`` stays required.
    """
    parser = argparse.ArgumentParser(
        prog="python -m s7_mcp",
        description=(
            "s7-author: MCP server exposing the dynamic-schema REST API — "
            "a thin proxy that returns every endpoint envelope verbatim."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="transport to serve (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address for streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="bind port for streamable-http (default: 8000)",
    )
    args = parser.parse_args(argv)

    get_config(require_token=args.transport == "stdio")
    if args.transport == "stdio":
        mcp.run()
    else:
        _run_http(args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()

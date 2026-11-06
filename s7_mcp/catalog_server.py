"""s7-catalog: MCP server wrapping the governed catalog changeset API.

The governed catalog mutation surface — a thin mapping over the
``/api/catalog/changesets/`` endpoints, one tool per verb. ``s7-author``
(the package's other entry point) covers schema authoring and the direct
catalog import; this server exposes only the changeset loop — propose,
read the validation report, fix — so registering ``s7-author`` alone in
an agent's MCP configuration grants it no propose capability.

Thin mapping only, same contract as ``s7-author``: each tool forwards to
one authenticated endpoint and returns the JSON envelope verbatim. There
is no business logic here and no approve/apply/reject/supersede tools by
design — review verdicts and the governed apply are human actions in the
admin, except that a token carrying the ``can_auto_apply_additive``
capability may see a purely additive proposal approve+apply inside the
propose/fix request itself (``status`` ``applied``, or ``stale`` when an
apply-time gate detects catalog drift).

Two transports share this surface: stdio (the default, one process per
local agent — credentials come from the ``S7_*`` environment) and
streamable-http (``--transport streamable-http``, serving ``/mcp``):
each inbound request's ``Authorization`` and ``X-S7-Project-Id`` headers
supply the credential, which is forwarded verbatim — the backend
authenticates per request.

Configuration is resolved lazily inside the handlers (never at import
time), so importing this module — which registers the tools and the
prompt — succeeds without any environment variables set. The shared
machinery (per-request credential resolution and the bounded-LRU client
cache) lives in ``s7_mcp.server`` and is imported, not duplicated.
"""

from __future__ import annotations

import argparse
import sys

try:
    try:  # mcp >= 2.0
        from mcp.server.mcpserver import Context, MCPServer
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp import Context, FastMCP as MCPServer

    from . import __version__
    from .config import get_config
    from .prompts import PROPOSE_CATALOG_CHANGE_PROMPT
    # Importing ``.server`` builds its own ``MCPServer("s7-author")`` and
    # registers the s7-author tools on that instance — a dead object in this
    # process. What is reused is the credential machinery: ``_get_client``
    # (per-request header resolution + the bounded-LRU client cache); path
    # quoting lives inside the client's changeset methods. The
    # ``@mcp.tool()`` decorators below bind to this module's own ``mcp``
    # instance, so the surfaces never mix.
    from .server import _get_client
except ImportError as exc:
    # Missing optional dependencies (mcp/httpx): under ``python -m
    # s7_mcp.catalog_server`` this module runs as ``__main__`` from its
    # first line, so a ``__name__`` check converts the ImportError into
    # the same friendly hint as ``s7_mcp.__main__`` — stderr only, exit
    # 2. Imported as a library it must propagate instead: tests import
    # this module under the ``_DEPS`` guard convention.
    if (
        __name__ == "__main__"
        and exc.name
        and exc.name.split(".", 1)[0] in ("mcp", "httpx")
    ):
        print(
            f"s7-catalog: missing dependency {exc.name!r} — install the "
            "package requirements first: "
            "pip install -r s7_mcp/requirements.txt",
            file=sys.stderr,
        )
        sys.exit(2)
    raise

try:
    mcp = MCPServer("s7-catalog", version=__version__)
except TypeError:  # pragma: no cover - mcp 1.x FastMCP has no version kwarg
    mcp = MCPServer("s7-catalog")


# ---------------------------------------------------------------------------
# Tools — one per changeset endpoint. Envelopes are returned verbatim.
# ---------------------------------------------------------------------------


@mcp.tool()
def s7_propose_changeset(ops: list[dict], rationale: str,
                         ctx: Context | None = None) -> dict:
    """Propose a governed catalog changeset — ``POST catalog/changesets/``.

    ``ops`` is an ordered list of mutation ops over the six writable
    catalog models (``nodetype``, ``attributedef``, ``domain``,
    ``domainitem``, ``nodetypevariant``, ``nodetypecomposition``). Each
    op is a dict carrying an ``op`` name plus its fields — additive
    ``add_*`` ops (e.g. ``{"op": "add_node_type", "name": ..., "label":
    ...}``), mutative ``update_*``/``rename_*`` ops (updates take a
    ``set`` dict of field overrides) and destructive ``remove_*`` ops
    (``remove_domain_item`` is mutative — it warns on implemented rows
    rather than breaking).
    ``rationale`` is required non-blank — it is the context the human
    reviewer reads, so state honestly what changes and why.

    The endpoint persists the changeset and validates it synchronously,
    so the 201 envelope is the serialized changeset — ``{id, status,
    ops, rationale, scopes, base_revision, validation_report,
    proposed_by_credential, proposed_by_user, reviewed_by, review_note,
    applied_at, created_at, updated_at}`` — with ``status`` already
    ``valid`` or ``invalid``. When the token also carries the
    ``can_auto_apply_additive`` capability and the validated payload is
    purely additive — only ``add_*`` ops, no ``warning``/``breaking``
    findings — the backend chains approve+apply inside the same
    request, so ``status`` may answer ``applied`` directly (or
    ``stale`` when an apply-time gate detects catalog drift), with
    ``reviewed_by`` set to the token's user. A validation failure is
    still a persisted 201, not an
    error: ``validation_report.findings`` — entries ``{op_index,
    severity, entity, code, message, remediation?}`` classified
    ``breaking``/``warning``/``info`` — is the feedback channel; fix the
    ops and re-propose on the same changeset via ``s7_fix_changeset``.

    Gates — every error envelope propagates verbatim: no bearer
    credential answers 401; a project credential, a level-1 user MCP
    token or a token without ``can_propose_changeset`` answers 403. A
    second 403 ``{"error": "changeset_scopes_not_covered", "message"}``
    may fire after validation, when the derived ``scopes`` escape the
    token's ``allowed_scopes`` — the proposal then rolls back and
    nothing persists. A malformed body (missing/blank ``rationale``,
    empty or non-list ``ops``) answers 400 ``{"error":
    "invalid_request", "message"}``.
    """
    return _get_client(ctx).propose_changeset(ops, rationale)


@mcp.tool()
def s7_get_changeset(changeset_id: str, ctx: Context | None = None) -> dict:
    """Fetch one changeset — ``GET catalog/changesets/<changeset_id>/``.

    The 200 envelope is the serialized changeset — ``status`` plus the
    full ``validation_report`` (``tiers_passed``, ``findings`` and
    ``impact``), which is
    the feedback channel of the correction loop. An unknown id answers
    404 ``{"error": "changeset_not_found", "changeset_id": "<id>"}``; a
    non-UUID id never reaches the endpoint (the route converter rejects
    it, surfacing through the transport-error envelope). A token whose
    ``allowed_scopes`` do not cover the changeset's derived ``scopes``
    answers 403 ``{"error": "changeset_scopes_not_covered", "scopes":
    [...]}`` — same capability gate as the propose (``mcp_level >= 2``
    + ``can_propose_changeset``; 401 without a token).
    """
    return _get_client(ctx).get_changeset(changeset_id)


@mcp.tool()
def s7_fix_changeset(changeset_id: str, ops: list[dict], rationale: str,
                     ctx: Context | None = None) -> dict:
    """Replace the ops of a changeset — ``PATCH catalog/changesets/<id>/``.

    The correction arm of the feedback loop: ``ops`` replaces the
    mutation document wholesale — it is never merged — and validation
    re-runs synchronously, so the 200 envelope is the same serialized
    changeset shape at ``valid``/``invalid`` — or ``applied``/``stale``
    under the same ``can_auto_apply_additive`` conditions as the
    propose, a purely additive revision auto-applying in-request —
    with a refreshed
    ``validation_report`` (and recomputed ``scopes``/``base_revision``).
    The endpoint requires a fresh non-blank ``rationale`` on every PATCH
    — the thin mapping keeps it mandatory: write one for the reviewer,
    e.g. which findings this revision addresses.

    Only a status carrying a ``→ validating`` edge accepts PATCH —
    ``proposed``, ``invalid``, ``valid`` and ``stale``. ``draft``,
    ``validating``, ``approved``, ``applied``, ``rejected`` and
    ``superseded`` answer 409 ``{"error": "changeset cannot transition
    from '<status>' to 'validating'"}``. The patch is also
    proposer-only: a token whose user did not propose the changeset
    answers 403 ``{"error": "changeset_not_proposer"}`` — and a
    changeset proposed by a project credential
    (``proposed_by_credential``, no ``proposed_by_user``) has no API
    owner to patch as, so it always answers the same 403. The 404/400
    envelopes match the read and the propose verbatim; the scope-check
    403 carries ``message``, as on the propose (the read echoes
    ``scopes`` instead).
    """
    return _get_client(ctx).fix_changeset(changeset_id, ops, rationale)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def propose_catalog_change() -> str:
    """Step-by-step workflow for the governed catalog changeset loop."""
    return PROPOSE_CATALOG_CHANGE_PROMPT


def _run_http(host: str, port: int) -> None:
    """Serve MCP over streamable-http at ``/mcp`` (SDK v1/v2 compat).

    Same compatibility shape as ``s7_mcp.server._run_http`` — kept
    local because ``mcp.run`` binds to this module's own server
    instance: mcp 2.x forwards ``host``/``port`` kwargs on ``run()``;
    mcp 1.x takes them on ``mcp.settings`` before ``run``. The path is
    ``/mcp`` in both generations. The broad ``TypeError`` also swallows
    a genuine runtime type error into a v1 retry — the same accepted
    compat trade-off as ``s7_mcp.server._run_http``.

    v1 caveat (inherited): ``settings.host`` is mutated after
    construction, but the SDK's DNS-rebinding protection was already
    armed with a localhost-only allowlist — a v1 server bound to
    ``0.0.0.0`` rejects non-localhost ``Host`` headers. Edge-routed
    deployments therefore require mcp 2.x (the resolved version in
    practice; the floor is ``mcp>=1.19``).
    """
    try:
        mcp.run(transport="streamable-http", host=host, port=port)
    except TypeError:  # pragma: no cover - mcp 1.x run() takes no kwargs
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse the transport, validate config, then serve MCP.

    ``--transport stdio`` (default) keeps env-only credentials with
    ``S7_TOKEN`` required. ``--transport streamable-http`` serves
    ``/mcp`` over HTTP: each request's ``Authorization`` /
    ``X-S7-Project-Id`` headers supply the credential, so ``S7_TOKEN``
    becomes an optional fallback while ``S7_BASE_URL`` stays required.
    """
    parser = argparse.ArgumentParser(
        prog="python -m s7_mcp.catalog_server",
        description=(
            "s7-catalog: MCP server exposing the governed catalog "
            "changeset API — a thin proxy that returns every endpoint "
            "envelope verbatim (propose/read/fix; no approve/apply)."
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

    get_config(require_token=args.transport == "stdio", prog="s7-catalog")
    if args.transport == "stdio":
        mcp.run()
    else:
        _run_http(args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()

# s7-author

`s7-author` is a thin [MCP](https://modelcontextprotocol.io) server that
exposes the dynamic-schema REST API to AI agents. Every tool maps 1:1 to an
already-authenticated `/api/` endpoint and returns its JSON envelope verbatim —
there is **no business logic in this server** and no Django dependency.

Two transports share the same surface:

- **stdio** (default) — one process per local agent; the credential comes from
  the process environment (`S7_TOKEN` + `S7_PROJECT_ID`).
- **streamable-http** (`--transport streamable-http`) — a hosted endpoint at
  `/mcp`; each request's `Authorization` and `X-S7-Project-Id` headers supply
  the credential, so every call operates as the connecting user.

The package ships a **second entry point**, `s7-catalog`
(`python -m s7_mcp.catalog_server`), which exposes only the governed
catalog-changeset surface — propose / read / fix — so registering
`s7-author` alone grants an agent no propose capability. See
[s7-catalog](#s7-catalog--governed-catalog-changesets) below.

## Install

```bash
pip install -r s7_mcp/requirements.txt
# or: pip install "mcp>=1.19" "httpx>=0.27"
```

Requires Python 3.10+.

## Configuration

The server is configured exclusively through environment variables:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `S7_BASE_URL` | yes (both transports) | — | Backend base URL, e.g. `http://localhost:8000`. API paths resolve under `{S7_BASE_URL}/api/`. |
| `S7_TOKEN` | stdio: yes; streamable-http: fallback only | — | Bearer token of a `UserMCPToken` — a per-user MCP credential (see below). Under streamable-http the request's `Authorization` header wins; the env var is only the fallback when the header is absent. |
| `S7_PROJECT_ID` | no | — | Target project UUID, injected as `project_id` into the validate/import request bodies and sent as the `?project_id=` query parameter on the schema reads (draft and published) and the draft delete. Set it when the credential is a user MCP token — the backend requires `project_id` on those endpoints for user principals. Under streamable-http the request's `X-S7-Project-Id` header wins over this variable. |
| `S7_MAX_PAYLOAD_KB` | no | `512` | Outgoing JSON body limit for the validate/import/changeset request bodies (KB). |
| `S7_TIMEOUT_S` | no | `30` | HTTP timeout in seconds. |

### User MCP tokens

`S7_TOKEN` is a **user MCP token** — a per-user credential provisioned by an
S7 administrator (superuser → *User MCP tokens* → **Add**); the raw secret is
shown once and only its digest is stored. Each token carries an MCP **level**
enforced by the backend: level **1** covers schema authoring (the catalog
reads + validate/import surface below), level **2** adds catalog-authoring
capabilities — the direct import plus, for tokens also flagged
`can_propose_changeset`, the governed changeset surface (see
[s7-catalog](#s7-catalog--governed-catalog-changesets)).

The server does not distinguish token types or levels — it forwards the
bearer verbatim and the backend enforces permissions, so a level-1 token
calling a level-2 capability simply returns the backend's error envelope.
Imports are always persisted as `draft`, regardless of level: publishing is
a human action in the admin.

Because a user token is not bound to a project, set `S7_PROJECT_ID` to the
target project UUID: the client injects it as `project_id` on
`schema/validate/` and `schema/import/` and sends it as the `project_id`
query parameter on `schema/` reads (draft and published) and on the draft
delete — all required by the backend for user principals (without it they
answer `400 {"error": "project_id is required"}`; a project the user cannot
access gets `403` — and the draft delete additionally requires edit
rights on the project). The value is validated as a UUID
at startup — a malformed one exits with a stderr diagnostic. For project
credentials the variable is optional and harmless: the backend enforces its
own credential scope, so a mismatch is answered with `403`.

Project API credentials still exist as the B2B channel for non-MCP
consumers, but the authoring tools here assume a user token.

## Client setup

Register the server in your MCP client configuration. The command is
`python -m s7_mcp` run from the repository root (or anywhere the `s7_mcp`
package is importable, e.g. inside the project's virtualenv).

### Windsurf — `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "s7-author": {
      "command": "python",
      "args": ["-m", "s7_mcp"],
      "env": {
        "S7_BASE_URL": "http://localhost:8000",
        "S7_TOKEN": "<user-mcp-token>",
        "S7_PROJECT_ID": "<project-uuid>"
      }
    }
  }
}
```

### Claude Desktop — `claude_desktop_config.json`

Same `mcpServers` block as above. Use the absolute path to the interpreter that
has `mcp` + `httpx` installed, e.g. `"command": "C:/path/to/venv/Scripts/python.exe"`.

### Devin / other MCP clients

Same shape: `command` = the Python interpreter, `args` = `["-m", "s7_mcp"]`,
`env` = `S7_BASE_URL` + `S7_TOKEN` + `S7_PROJECT_ID` (plus the optional
tuning variables). Some clients name the top-level key `mcp_config` instead
of `mcpServers`.

To also give the agent the governed changeset surface, register a second
entry with `args` = `["-m", "s7_mcp.catalog_server"]` — same env vars,
same flags (see [s7-catalog](#s7-catalog--governed-catalog-changesets)).

## Hosted endpoint (streamable-http)

For remote agents the same package serves MCP over HTTP instead of stdio:

```bash
python -m s7_mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

The endpoint lives at `/mcp`. There is no server-side MCP authentication of
its own — the credential travels on each request and the backend applies
its per-request auth, so the process only needs `S7_BASE_URL`:

- `Authorization: Bearer <user-mcp-token>` — wins over `S7_TOKEN` whenever
  present. A non-Bearer value resolves to "no credential" (never silently
  promoted to the env token), so the backend answers its own 401 verbatim.
- `X-S7-Project-Id: <project-uuid>` — wins over `S7_PROJECT_ID`, forwarded
  verbatim (the backend validates its shape).

Clients are cached per resolved `(token, project_id)` pair, so two tenants
on the same endpoint never share a connection or a credential. The cache is
a bounded LRU (128 entries): a hit refreshes the entry, and an insert past
the bound evicts the least-recently-used client and closes its HTTP
connection pool — a long-lived endpoint does not accumulate pools of
rotated credentials. The static
resources `s7://datatypes` and `s7://catalog-grammar` are an exception to
per-request resolution — the SDK injects no request context into
URI-template-less resources — and resolve from the environment only.

Client configuration (any user holding an MCP token):

```jsonc
{
  "mcpServers": {
    "s7-author": {
      "url": "https://<host>/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer <user-mcp-token>",
        "X-S7-Project-Id": "<project-uuid>"
      }
    }
  }
}
```

**Deployment notes.** In development bind to localhost (`--host 127.0.0.1`,
the default); the endpoint then only serves the local machine. The dev
compose service maps it to `127.0.0.1:${S7_MCP_PORT:-8010}` — override
`S7_MCP_PORT` to pick a different host port. In
production the service goes behind the existing edge (TLS termination,
allowlisting), which is also where the bearer would otherwise travel in
plain HTTP. `S7_TOKEN`/`S7_PROJECT_ID` may still be set on the server as
fallbacks for requests that arrive without headers — e.g. health probes or
a shared default — but any `Authorization` header, even a malformed one,
always wins. Edge-routed deployments require `mcp` 2.x: under `mcp` 1.x the
SDK's DNS-rebinding protection keeps a localhost-only allowlist when the
bind host is changed after construction, so requests with a public `Host`
header are rejected — another reason the floor pins `mcp>=1.19` with 2.x
resolved in practice.

## Surface

### Tools

| Tool | Endpoint | Purpose |
|---|---|---|
| `s7_get_catalog(scope)` | `GET catalog/<scope>/` | Resolved authoring catalog: node types, attributes, variants, compositions, domains, grammar docs for non-trivial datatypes, generated example. |
| `s7_get_schema(node_type, key, version)` | `GET schema/<node_type>/<key>/<version>/` | Published schema `{"data", "meta"}` — few-shot reference and post-publish verification. **Dual auth**: project credentials work as-is; a user MCP token needs `S7_PROJECT_ID` (sent as `?project_id=`). |
| `s7_get_draft(node_type, key, version)` | `GET schema/draft/<node_type>/<key>/<version>/` | Draft schema `{"data", "meta"}` with `meta.status: "draft"` — verify an import round-trips right after `s7_import_draft`, without waiting for a human to publish. **Dual auth**: project credentials work as-is; a user MCP token needs `S7_PROJECT_ID` (sent as `?project_id=`). |
| `s7_list_datatypes()` | `GET catalog/datatypes/` | Universal DataType catalog. |
| `s7_validate_schema(node_type, schema, schema_key?, schema_version?)` | `POST schema/validate/` | Dry-run the import pipeline → `{valid, errors, warnings}`. |
| `s7_import_draft(node_type, schema, schema_key?, schema_version?, overwrite?)` | `POST schema/import/` | Persist the schema — always as `draft`. |
| `s7_delete_draft(node_type, key, version)` | `DELETE schema/draft/<node_type>/<key>/<version>/` | Delete a draft schema — destructive and irreversible; frees the node types the draft implemented. Draft-only: a missing or non-draft schema answers the same RFC-7807 404 as the read. **User token only (any level) with edit rights on the project** — project credentials get 403; `S7_PROJECT_ID` is sent as `?project_id=`. |
| `s7_import_catalog(objects)` | `POST catalog/import/` | Apply a fixture-format catalog payload (`{"objects": [{model, pk?, fields, delete?}]}`) → `{applied, created?, updated?, deleted?, results?, errors?, warnings?}`. Updates replace the whole row — resubmit every field you want to keep, and object keys outside `model`/`pk`/`fields`/`delete` are rejected. A `{"delete": true}` object identified by `pk` XOR natural-key `fields` removes a row: it needs the `delete_<label>` permission and is usage-gated — a row implemented by existing schemas in any project answers `catalog_object_in_use`, and a field-changing update on such a row is rejected the same way (creates and no-op updates are exempt). Upserts apply first, then deletes children-before-parents, so a rename is a create + delete in one atomic payload. **User token level ≥ 2 + per-model catalog permissions** — anything else gets the 401/403 envelope verbatim. |
| `s7_catalog_usage(model, pk?, fields?)` | `GET catalog/usage/` | Pre-check of the `catalog_object_in_use` gate → `{implemented, usage}` — call it on update/delete targets before `s7_import_catalog` to get the same `usage` evidence a rejection would carry. The row is identified by `pk` XOR natural-key `fields` sent as flat params (an omitted nullable component resolves as NULL). Global read — no `project_id` scoping. **User token level ≥ 2 or project credential `can_read`**; advisory read — the gate still re-checks at apply time. |

`schema` accepts a JSON object or a JSON string. `schema_key` and
`schema_version` are explicit overrides for the values carried inside the
payload. Version precedence: the `schema_version` argument > the payload
attribute whose `data_type` is `natural_version` (typically
`metadata[].version` on a child node) > a root-level `version` field
(tolerated alias — when both are present the declared attribute wins, with
the divergence reported as a warning) > `"1"`.

### Resources

| URI | Content |
|---|---|
| `s7://catalog/{scope}` | Resolved catalog JSON — `scope` is the root node type's `json_scope`, not its `name`. |
| `s7://datatypes` | DataType catalog JSON. |
| `s7://examples/{scope}` | Generated example JSON for the scope. |
| `s7://schemas/{node_type}/{key}/{version}` | Published schema JSON (few-shot). Dual auth — user tokens need `S7_PROJECT_ID`; see `s7_get_schema`. |
| `s7://catalog-grammar` | Field reference for the six level-2-authorizable catalog models: writable fields, natural keys, FK `[natural_key]` shapes, payload semantics, plus a `resolved_vs_import` table mapping the resolved-catalog read shape to the fixture objects the import accepts, an `expressions` section with usage guidance for `{{ expr }}` token values on `expression`-typed attributes, and a `usage_check` section documenting the `s7_catalog_usage` pre-check (static, shipped with the package). |

### Prompts

- `create_tramite_form` — guided workflow: read the catalog → draft the root
  JSON (`key` plus the version on the declared `natural_version` attribute;
  `expression`-typed attributes may embed `{{expr}}` tokens; no `status`
  field) → validate until `valid: true` → import draft → verify the draft
  via `s7_get_draft` → `s7_get_schema` after a human publishes (both reads
  work with user tokens when `S7_PROJECT_ID` is set).
- `author_catalog` — guided workflow for grammar authoring (level 2): read
  `s7://catalog-grammar` (per-model field table plus the `resolved_vs_import`
  mapping — the resolved catalog is a *read* shape, not the import shape)
  and `s7://datatypes` → build a fixture-format `objects` payload with
  natural-key FKs (upserts and usage-gated `delete` objects) →
  `s7_import_catalog` → verify via `s7_get_catalog` on the new scope.

## s7-catalog — governed catalog changesets

`s7-catalog` is the package's second entry point — the governed catalog
mutation surface. It exposes only the changeset loop (propose → read the
validation report → fix → hand off to a human), so an agent can be granted
propose capability without the direct catalog import: register it as a
separate MCP server, or omit it to keep the agent out of the changeset
surface entirely. Same transports, flags and env vars as `s7-author` —
both entries may run side by side from the same package. One env nuance:
`S7_PROJECT_ID` is inert on this surface — the changeset endpoints are
scope-gated, not project-gated (an injected `project_id` in the body is
ignored by the backend; no `?project_id=` travels on the GET).

```bash
python -m s7_mcp.catalog_server                      # stdio
python -m s7_mcp.catalog_server --transport streamable-http --host 0.0.0.0 --port 8000
```

### Tools

| Tool | Endpoint | Purpose |
|---|---|---|
| `s7_propose_changeset(ops, rationale)` | `POST catalog/changesets/` | Create a changeset from ordered mutation `ops` + a non-blank `rationale` (the human reviewer's context) → 201 serialized changeset `{id, status, ops, rationale, scopes, base_revision, validation_report, proposed_by_credential, proposed_by_user, reviewed_by, review_note, applied_at, created_at, updated_at}` with `status` already `valid`/`invalid` — a validation failure is still a persisted 201; `validation_report.findings` (`{op_index, severity, entity, code, message, remediation?}`) is the feedback channel. |
| `s7_get_changeset(changeset_id)` | `GET catalog/changesets/<id>/` | Read `status` + `validation_report` → serialized changeset. Unknown id → 404 `{error: "changeset_not_found", changeset_id}`; uncovered scopes → 403 `{error: "changeset_scopes_not_covered", scopes}`. |
| `s7_fix_changeset(changeset_id, ops, rationale)` | `PATCH catalog/changesets/<id>/` | Replace `ops` wholesale (never merged) with a fresh non-blank `rationale` — required on every PATCH — and re-validate synchronously → 200 re-validated changeset. Only `proposed`/`invalid`/`valid`/`stale` accept PATCH; `draft`/`validating`/`approved`/`applied`/`rejected`/`superseded` → 409 `{error: "changeset cannot transition from '<status>' to 'validating'"}`. |

### Required capability

All three tools need a **user MCP token with `mcp_level >= 2` plus the
`can_propose_changeset` flag**: no token → 401; a project credential, a
level-1 token or a flag-less token → 403 (the backend's envelope, verbatim).
When the token also carries `allowed_scopes`, the `scopes` the ops derive
server-side must be covered by it — an uncovered proposal or read answers
403 `changeset_scopes_not_covered`, and a refused proposal/patch rolls back
without persisting.

`approve`, `apply` and `reject` are **never** tools — by design. Review
verdicts and the governed apply are human actions in the admin; a `valid`
changeset is the agent's finished work.

### Prompts

- `propose_catalog_change` — guided workflow: discover the live catalog (and
  pre-check usage when `s7-author` is also registered) → propose with an
  honest rationale → read `validation_report.findings` → fix with a fresh
  rationale until `status: "valid"` → hand off to the human reviewer.

## Troubleshooting

- **Server exits immediately / "missing required environment variable(s)" on
  stderr** — set `S7_BASE_URL` and `S7_TOKEN` in the client's `env` block
  (under streamable-http only `S7_BASE_URL` is required).
  Everything is logged to stderr, never stdout (stdout carries the protocol).
- **`{"error": {"code": "transport_error", ...}}`** — the backend is
  unreachable (timeout, DNS, connection refused) or answered non-JSON. Check
  `S7_BASE_URL` and that the backend is running.
- **HTTP 401** — invalid, expired or revoked `S7_TOKEN` (stdio) or
  `Authorization` bearer (streamable-http). Under streamable-http a missing
  or non-Bearer `Authorization` header also lands here: the request goes
  unauthenticated — never silently promoted to the env token — and the
  backend answers its own 401 verbatim.
- **`400 {"error": "project_id is required"}`** — a user MCP token reached
  an endpoint without a project id. Set `S7_PROJECT_ID` (stdio) or send
  `X-S7-Project-Id` (streamable-http).
- **HTTP 403** — the token's owner lacks permission over the target
  project/organization, or the token's MCP level does not cover the
  capability — e.g. a level-1 token calling `s7_import_catalog` (level 2).
  The backend's error envelope is returned verbatim.
- **Static resources ignore per-request headers** — `s7://datatypes` and
  `s7://catalog-grammar` resolve credentials from the environment only
  (the SDK cannot inject request context into resources without URI
  template variables). Tools and template resources do forward them.
- **HTTP 404** — unknown catalog scope
  (`{"error": "scope_not_found", "available_scopes": [...]}` —
  `available_scopes` lists every live root scope), or the schema is
  not found/not in the requested status — published for `s7_get_schema`,
  draft for `s7_get_draft`; `s7_delete_draft` on a missing or non-draft
  schema answers the same RFC-7807 body
  `{"type": "schema_not_found", "title", "detail", "status": 404}`;
  `s7_catalog_usage` on a catalog row that does not exist answers
  `{"error": "catalog_object_not_found", "model", "identifier"}`.
- **`{"error": {"code": "payload_too_large", ...}}`** — the outgoing JSON body
  exceeded `S7_MAX_PAYLOAD_KB`; the request was not sent. Split/trim the schema
  or raise the limit.
- **No `publish`/`approve`/`apply`/`reject` tools** — by design. Imports are
  forced to `draft`; publishing is a human action in the admin, and on the
  `s7-catalog` surface review verdicts and the changeset apply are too —
  the agent's loop ends at `status: "valid"`.

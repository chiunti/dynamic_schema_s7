# s7-author

`s7-author` is a thin [MCP](https://modelcontextprotocol.io) stdio server that
exposes the dynamic-schema REST API to AI agents. Every tool maps 1:1 to an
already-authenticated `/api/` endpoint and returns its JSON envelope verbatim —
there is **no business logic in this server** and no Django dependency. It runs
standalone on the agent's machine.

## Install

```bash
pip install -r s7_mcp/requirements.txt
# or: pip install "mcp>=1.0" "httpx>=0.27"
```

Requires Python 3.10+.

## Configuration

The server is configured exclusively through environment variables:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `S7_BASE_URL` | yes | — | Backend base URL, e.g. `http://localhost:8000`. API paths resolve under `{S7_BASE_URL}/api/`. |
| `S7_TOKEN` | yes | — | Bearer token of a `UserMCPToken` — a per-user MCP credential (see below). |
| `S7_PROJECT_ID` | no | — | Target project UUID, injected as `project_id` into the validate/import request bodies and sent as the `?project_id=` query parameter on the schema reads (draft and published). Set it when `S7_TOKEN` is a user MCP token — the backend requires `project_id` on those endpoints for user principals. |
| `S7_MAX_PAYLOAD_KB` | no | `512` | Outgoing JSON body limit for validate/import (KB). |
| `S7_TIMEOUT_S` | no | `30` | HTTP timeout in seconds. |

### User MCP tokens

`S7_TOKEN` is a **user MCP token** — a per-user credential provisioned by an
S7 administrator (superuser → *User MCP tokens* → **Add**); the raw secret is
shown once and only its digest is stored. Each token carries an MCP **level**
enforced by the backend: level **1** covers schema authoring (the catalog
reads + validate/import surface below), level **2** adds catalog-authoring
capabilities.

The server does not distinguish token types or levels — it forwards the
bearer verbatim and the backend enforces permissions, so a level-1 token
calling a level-2 capability simply returns the backend's error envelope.
Imports are always persisted as `draft`, regardless of level: publishing is
a human action in the admin.

Because a user token is not bound to a project, set `S7_PROJECT_ID` to the
target project UUID: the client injects it as `project_id` on
`schema/validate/` and `schema/import/` and sends it as the `project_id`
query parameter on `schema/` reads (draft and published) — all required by
the backend for user principals (without it they answer
`400 {"error": "project_id is required"}`; a project the user cannot access
gets `403`). The value is validated as a UUID
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
| `s7_import_catalog(objects)` | `POST catalog/import/` | Apply a fixture-format catalog payload (`{"objects": [{model, pk?, fields}]}`) → `{applied, created?, updated?, results?, errors?, warnings?}`. Updates replace the whole row — resubmit every field you want to keep, and object keys outside `model`/`pk`/`fields` are rejected. **User token level ≥ 2 + per-model catalog permissions** — anything else gets the 401/403 envelope verbatim. |

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
| `s7://catalog-grammar` | Field reference for the six level-2-authorizable catalog models: writable fields, natural keys, FK `[natural_key]` shapes, payload semantics, plus a `resolved_vs_import` table mapping the resolved-catalog read shape to the fixture objects the import accepts (static, shipped with the package). |

### Prompts

- `create_tramite_form` — guided workflow: read the catalog → draft the root
  JSON (`key` plus the version on the declared `natural_version` attribute;
  no `status` field) → validate until `valid: true` → import draft → verify
  the draft via `s7_get_draft` → `s7_get_schema` after a human publishes
  (both reads work with user tokens when `S7_PROJECT_ID` is set).
- `author_catalog` — guided workflow for grammar authoring (level 2): read
  `s7://catalog-grammar` (per-model field table plus the `resolved_vs_import`
  mapping — the resolved catalog is a *read* shape, not the import shape)
  and `s7://datatypes` → build a fixture-format `objects` payload with
  natural-key FKs → `s7_import_catalog` → verify via `s7_get_catalog` on
  the new scope.

## Troubleshooting

- **Server exits immediately / "missing required environment variable(s)" on
  stderr** — set `S7_BASE_URL` and `S7_TOKEN` in the client's `env` block.
  Everything is logged to stderr, never stdout (stdout carries the protocol).
- **`{"error": {"code": "transport_error", ...}}`** — the backend is
  unreachable (timeout, DNS, connection refused) or answered non-JSON. Check
  `S7_BASE_URL` and that the backend is running.
- **HTTP 401** — invalid, expired or revoked `S7_TOKEN`.
- **HTTP 403** — the token's owner lacks permission over the target
  project/organization, or the token's MCP level does not cover the
  capability — the backend's error envelope is returned verbatim.
- **HTTP 404** — unknown catalog scope
  (`{"error": "scope_not_found", "available_scopes": [...]}` —
  `available_scopes` lists every live root scope), or the schema is
  not found/not in the requested status — published for `s7_get_schema`,
  draft for `s7_get_draft` (RFC-7807 body
  `{"type": "schema_not_found", "title", "detail", "status": 404}`).
- **`{"error": {"code": "payload_too_large", ...}}`** — the outgoing JSON body
  exceeded `S7_MAX_PAYLOAD_KB`; the request was not sent. Split/trim the schema
  or raise the limit.
- **No `publish`/`approve`/`apply` tools** — by design. Imports are forced to
  `draft`; publishing is a human action in the admin.

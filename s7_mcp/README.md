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
| `S7_TOKEN` | yes | — | Bearer token of a `ProjectAPICredential`. |
| `S7_MAX_PAYLOAD_KB` | no | `512` | Outgoing JSON body limit for validate/import (KB). |
| `S7_TIMEOUT_S` | no | `30` | HTTP timeout in seconds. |

### Credential scopes

The token needs **`can_read` + `can_import`**. `can_publish` is never required:
credential imports are forced to `draft` by the backend, and publishing remains
a human action in the admin.

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
        "S7_TOKEN": "<project-credential-token>"
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
`env` = `S7_BASE_URL` + `S7_TOKEN` (plus the optional tuning variables). Some
clients name the top-level key `mcp_config` instead of `mcpServers`.

## Surface

### Tools

| Tool | Endpoint | Purpose |
|---|---|---|
| `s7_get_catalog(scope)` | `GET catalog/<scope>/` | Resolved authoring catalog: node types, attributes, variants, compositions, domains, generated example. |
| `s7_get_schema(node_type, key, version)` | `GET schema/<node_type>/<key>/<version>/` | Published schema `{"data", "meta"}` — few-shot reference and post-publish verification. |
| `s7_list_datatypes()` | `GET catalog/datatypes/` | Universal DataType catalog. |
| `s7_validate_schema(node_type, schema, schema_key?, schema_version?)` | `POST schema/validate/` | Dry-run the import pipeline → `{valid, errors, warnings}`. |
| `s7_import_draft(node_type, schema, schema_key?, schema_version?, overwrite?)` | `POST schema/import/` | Persist the schema — always as `draft`. |

`schema` accepts a JSON object or a JSON string.

### Resources

| URI | Content |
|---|---|
| `s7://catalog/{scope}` | Resolved catalog JSON. |
| `s7://datatypes` | DataType catalog JSON. |
| `s7://examples/{scope}` | Generated example JSON for the scope. |
| `s7://schemas/{node_type}/{key}/{version}` | Published schema JSON (few-shot). |

### Prompts

- `create_tramite_form` — guided workflow: read the catalog → draft the root
  JSON (`key`, `version`, `"status": "draft"`) → validate until `valid: true` →
  import draft → verify via `s7_get_schema` after a human publishes.

## Troubleshooting

- **Server exits immediately / "missing required environment variable(s)" on
  stderr** — set `S7_BASE_URL` and `S7_TOKEN` in the client's `env` block.
  Everything is logged to stderr, never stdout (stdout carries the protocol).
- **`{"error": {"code": "transport_error", ...}}`** — the backend is
  unreachable (timeout, DNS, connection refused) or answered non-JSON. Check
  `S7_BASE_URL` and that the backend is running.
- **HTTP 401** — invalid/expired `S7_TOKEN`.
- **HTTP 403** — the credential lacks the required scope (`can_read` for
  catalog/schema reads, `can_import` for validate/import) or a request-level
  `project_id`/`organization_id` mismatched the credential.
- **HTTP 404** — unknown catalog scope (`{"error": ...}`), or the schema is
  not found/not published for the credential's project (RFC-7807 body
  `{"type": "schema_not_found", "title", "detail", "status": 404}`).
- **`{"error": {"code": "payload_too_large", ...}}`** — the outgoing JSON body
  exceeded `S7_MAX_PAYLOAD_KB`; the request was not sent. Split/trim the schema
  or raise the limit.
- **No `publish`/`approve`/`apply` tools** — by design. Imports are forced to
  `draft`; publishing is a human action in the admin.

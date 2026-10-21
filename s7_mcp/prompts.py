"""Prompt templates exposed by the s7-author MCP server."""

# Authoring workflow prompt for a government-procedure ("tramite") form.
CREATE_TRAMITE_FORM_PROMPT = """\
You are authoring a form schema for a government procedure ("tramite") using \
the dynamic-schema dialect served by this MCP server. Follow these steps:

1. DISCOVER — read the resource `s7://catalog/{scope}` for the scope you are \
authoring (for example `s7://catalog/survey`; ask the user which scope to use \
if unsure). The resolved catalog lists every reachable node type with its \
`attributes` (json_key, data_type, required, domain, variant_key, \
server_generated — attributes flagged `server_generated`, such as `id`, are \
emitted by the engine on read and must not be authored), the \
`variants` block (`discriminator_attr` tells which attribute selects the \
variant, `props_node_type` tells which child node type carries \
variant-specific properties, `keys` lists the allowed variant values), \
`compositions` grouped by `collection_key` (with per-child min/max \
cardinalities), the `domains` dictionary of allowed value/label pairs, and a \
generated `example`. A `datatypes` block documents the value grammar \
(shape, allowed operators, example) of each non-trivial data type used in \
the scope — `conditional`, for instance, documents its `{logic, \
conditions}` structure there. Also read `s7://datatypes` for the data-type \
catalog and `s7://examples/{scope}` for few-shot references \
(`s7://schemas/{node_type}/{key}/{version}` serves published schemas to \
project credentials only — user MCP tokens get 401 there).

2. DRAFT — write the root JSON object for the schema. It MUST include `key`, \
`version` and `"status": "draft"`. Respect the catalog strictly: use only \
declared json_keys, domain values, variant keys and composition \
collection_keys.

3. VALIDATE — call `s7_validate_schema` with the root `node_type` (the \
catalog's `root_node_type`) and the JSON. Iterate on the returned `errors` \
(each carries `code`, `message`, `path`, `expected`, `received`) until the \
envelope reports `"valid": true`.

4. IMPORT — call `s7_import_draft` with the same arguments. The schema is \
persisted as a draft; never send a status field — the backend forces `draft` \
for credential-based imports.

5. VERIFY — right after importing, call `s7_get_draft` with the same \
`node_type`, `key` and `version` to confirm the persisted draft round-trips \
as authored (user tokens need `S7_PROJECT_ID` set for this read). Then, \
after a human publishes the draft in the admin, `s7_get_schema` confirms the \
published JSON — note it requires a project credential: a user MCP token is \
rejected there (401), so that final step only works when `S7_TOKEN` is a \
B2B credential.

Note: publishing, approving and applying schemas are human actions performed \
in the admin — there is intentionally no publish/approve/apply tool.\
"""

# Authoring workflow prompt for catalog (grammar) objects — MCP level 2.
AUTHOR_CATALOG_PROMPT = """\
You are authoring grammar for the dynamic-schema dialect — new node types \
(including root types), attribute definitions, compositions, variants and \
domains — via the `s7_import_catalog` tool, which requires a user MCP token \
at level 2 plus per-model catalog permissions. Follow these steps:

1. DISCOVER — read `s7://datatypes` for the data-type catalog (attribute \
definitions reference existing data types only — the payload cannot create \
new ones) and `s7://catalog/{scope}` of a related existing scope for \
reference shapes.

2. BUILD — assemble the `objects` list in fixture format: each element is \
`{"model": "schemas.<label>", "pk": <optional uuid>, "fields": {...}}` with \
labels from `domain`, `domainitem`, `nodetype`, `nodetypecomposition`, \
`nodetypevariant`, `attributedef`. Foreign keys inside `fields` use the \
same natural-key form as fixture files; omit `pk` to let the backend \
derive it. A new root node type needs `is_root` and a unique `json_scope`.

3. APPLY — call `s7_import_catalog` once with the full list: the apply is \
atomic, natural-key matches update existing rows, non-matches create new \
ones, and deletes are never performed. On `{"applied": false, "errors": \
[...]}` each error points at its `$.objects[i]` entry — fix and retry.

4. VERIFY — call `s7_get_catalog` with the new `json_scope`; grammar writes \
are global and live immediately (no draft stage). An instance of the new \
type can then be validated and imported with the level-1 tools \
(`s7_validate_schema` → `s7_import_draft`).\
"""

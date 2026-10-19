"""Prompt templates exposed by the s7-author MCP server."""

# Authoring workflow prompt for a government-procedure ("tramite") form.
CREATE_TRAMITE_FORM_PROMPT = """\
You are authoring a form schema for a government procedure ("tramite") using \
the dynamic-schema dialect served by this MCP server. Follow these steps:

1. DISCOVER — read the resource `s7://catalog/{scope}` for the scope you are \
authoring (for example `s7://catalog/survey`; ask the user which scope to use \
if unsure). The resolved catalog lists every reachable node type with its \
`attributes` (json_key, data_type, required, domain, variant_key), the \
`variants` block (`discriminator_attr` tells which attribute selects the \
variant, `props_node_type` tells which child node type carries \
variant-specific properties, `keys` lists the allowed variant values), \
`compositions` grouped by `collection_key` (with per-child min/max \
cardinalities), the `domains` dictionary of allowed value/label pairs, and a \
generated `example`. Also read `s7://datatypes` for the data-type catalog and \
`s7://examples/{scope}` / `s7://schemas/{node_type}/{key}/{version}` for \
few-shot references.

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

5. VERIFY — after a human publishes the draft in the admin, call \
`s7_get_schema` with the same `node_type`, `key` and `version` to confirm the \
published JSON round-trips as authored.

Note: publishing, approving and applying schemas are human actions performed \
in the admin — there is intentionally no publish/approve/apply tool.\
"""

"""Versioned field reference for the level-2 catalog-import fixture payload.

Static document shipped with the package — the package is standalone (no
Django), so the reference lives here and is guarded by a drift test on the
backend side that keeps it honest against the real model fields.

``CATALOG_GRAMMAR`` is served verbatim by the ``s7://catalog-grammar``
resource. Shape:

- ``format`` — one-line description of the payload format.
- ``payload`` — envelope semantics (``objects``/``pk``/``fields``, the
  update rules and the ``deletes`` delete-object reference).
- ``models`` — one entry per authorizable ``schemas.<label>`` model:

  - ``natural_key`` — the field names that identify a row.
  - ``fields`` — scalar writable fields. Each entry carries ``type``
    (``string``/``integer``/``boolean``), ``required`` (must be present in
    ``fields``), ``nullable`` (``null`` is an accepted value) and optional
    ``default``/``notes``.
  - ``fks`` — ForeignKey fields. Each entry carries the target ``model``,
    the target's ``natural_key``, the ``[natural_key]`` list ``shape`` the
    value takes inside ``fields``, ``required``, ``nullable`` and optional
    ``notes``.
  - ``notes`` — optional model-level remarks.

  ``schemas.datatype`` is deliberately absent: it is not authorizable
  through this payload — attribute definitions may only reference existing
  data types.

- ``resolved_vs_import`` — read-vs-write shape table. The resolved
  catalog served by ``s7://catalog/{scope}`` is a *read* shape, not this
  payload: ``mapping`` translates each resolved block into the fixture
  objects it becomes, ``example`` pairs a resolved fragment with its
  import equivalent, and ``notes`` covers what does not translate.

- ``expressions`` — usage guidance for ``{{ expr }}`` token values on
  ``expression``-typed attributes: when they apply, the supported
  syntax subset, explicitly unsupported constructs, worked examples
  and where errors surface. The canonical grammar lives in the
  datatype documentation — this section summarizes usage, it is not a
  second grammar.

``validate_example_import_objects`` lives next to the document: the
single check both drift suites run against
``resolved_vs_import.example.import.objects[]``, so the shipped grammar
and its validator cannot drift apart.
"""

CATALOG_GRAMMAR: dict = {
    "format": (
        "Django fixture-style objects payload accepted by "
        "s7_import_catalog (POST catalog/import/)."
    ),
    "payload": {
        "objects": (
            "list of {'model': 'schemas.<label>', 'pk': <uuid, optional>, "
            "'fields': {...}, 'delete': <true, optional>} — an object "
            "with 'delete': true removes a row instead of upserting it "
            "(see 'deletes')"
        ),
        "pk": (
            "optional — deterministic uuid5 by natural key is derived "
            "when omitted"
        ),
        "fields": (
            "writable model fields only; 'id' is forbidden here (it "
            "belongs at top-level 'pk'); on a delete object 'fields' "
            "only carries the complete natural key identifying the row"
        ),
        "deletes": (
            "an object {'model': 'schemas.<label>', 'delete': true} "
            "plus exactly one identifier — 'pk' (uuid) XOR 'fields' "
            "holding the complete natural key, never both — removes the "
            "row. 'delete' must be literal true; any other value is a "
            "validation error. Each delete requires the "
            "schemas.delete_<label> permission; deleting a nodetype "
            "first removes its own dependents (the attributedef, "
            "variant and composition rows touching it — each gated and "
            "each requiring its own delete_<label>), while a variant of "
            "ANOTHER node type bound to it through props_node_type is "
            "detached (SET_NULL), not deleted — still usage-gated, so it "
            "can block the delete with catalog_object_in_use naming a "
            "row the caller did not ask to delete. A row implemented "
            "by existing schemas (Node/NodeAttribute rows in any "
            "project) rejects the delete with catalog_object_in_use "
            "plus a 'usage' detail — deletes have no no-op exemption, "
            "and the check is an advisory read under READ COMMITTED. "
            "Deletes apply after all upserts in reverse dependency "
            "order (children before parents), so a rename is a create "
            "with the new natural key plus a delete of the old one in "
            "the same atomic payload. Deleting a row that does not "
            "exist is a per-object invalid_catalog_object error at "
            "$.objects[i]"
        ),
        "semantics": (
            "a row whose pk or natural key matches is updated, no match "
            "creates it (pk and natural key pointing at different rows "
            "is an error); an object carrying 'delete': true removes "
            "the row instead (see 'deletes'), and object keys outside "
            "model/pk/fields/delete are rejected. An update replaces "
            "the whole row — omitting a field that holds a non-default "
            "value on the live row is rejected, so resubmit the full "
            "object on every update. The success envelope lists "
            "results[] per object (index, model, natural_key, action "
            "created|updated|deleted, changed_fields) and counts rows "
            "per label under created/updated/deleted — deleted includes "
            "the dependents removed with a nodetype. Matching reads the "
            "serialized natural key, so write every natural-key "
            "component explicitly — omitting a component that carries "
            "a model default (e.g. discriminator_attr) matches no row "
            "and the insert collides on uniqueness"
        ),
    },
    "models": {
        "schemas.domain": {
            "natural_key": ["domain_name"],
            "fields": {
                "domain_name": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "description": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                },
            },
            "fks": {},
        },
        "schemas.domainitem": {
            "natural_key": ["domain", "value"],
            "fields": {
                "value": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "label": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
            },
            "fks": {
                "domain": {
                    "model": "schemas.domain",
                    "natural_key": ["domain_name"],
                    "shape": "[natural_key]",
                    "required": True,
                    "nullable": False,
                },
            },
            "notes": [
                "'domain' is part of the natural key and lives under 'fks' "
                "— write it as a natural-key list, e.g. "
                "[\"<domain_name>\"].",
            ],
        },
        "schemas.nodetype": {
            "natural_key": ["name"],
            "fields": {
                "name": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "label": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "is_container": {
                    "type": "boolean",
                    "required": False,
                    "nullable": False,
                    "default": False,
                },
                "is_root": {
                    "type": "boolean",
                    "required": False,
                    "nullable": False,
                    "default": False,
                },
                "json_scope": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                    "notes": (
                        "required when is_root is true (a root type owns "
                        "a scope)"
                    ),
                },
                "default_json_key": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                },
                "is_json_map": {
                    "type": "boolean",
                    "required": False,
                    "nullable": False,
                    "default": False,
                },
                "json_scalar_attribute": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                },
            },
            "fks": {},
            "notes": [
                "A new root node type sets is_root=true plus a unique "
                "json_scope.",
            ],
        },
        "schemas.nodetypecomposition": {
            "natural_key": ["parent_type", "child_type", "collection_key"],
            "fields": {
                "collection_key": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                    "notes": "part of the natural key; may be null",
                },
                "min_children": {
                    "type": "integer",
                    "required": False,
                    "nullable": True,
                },
                "max_children": {
                    "type": "integer",
                    "required": False,
                    "nullable": True,
                },
            },
            "fks": {
                "parent_type": {
                    "model": "schemas.nodetype",
                    "natural_key": ["name"],
                    "shape": "[natural_key]",
                    "required": True,
                    "nullable": False,
                },
                "child_type": {
                    "model": "schemas.nodetype",
                    "natural_key": ["name"],
                    "shape": "[natural_key]",
                    "required": True,
                    "nullable": False,
                },
            },
            "notes": [
                "'parent_type' and 'child_type' are part of the natural "
                "key and live under 'fks' — write each as a natural-key "
                "list, e.g. [\"<node_type_name>\"].",
            ],
        },
        "schemas.nodetypevariant": {
            "natural_key": [
                "node_type",
                "variant_key",
                "discriminator_attr",
            ],
            "fields": {
                "variant_key": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "discriminator_attr": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                    "default": "type",
                    "notes": (
                        "null marks a props/inherited variant resolved "
                        "through props_node_type; write it explicitly — "
                        "it is part of the natural key and omitting it "
                        "matches no row"
                    ),
                },
            },
            "fks": {
                "node_type": {
                    "model": "schemas.nodetype",
                    "natural_key": ["name"],
                    "shape": "[natural_key]",
                    "required": True,
                    "nullable": False,
                },
                "props_node_type": {
                    "model": "schemas.nodetype",
                    "natural_key": ["name"],
                    "shape": "[natural_key]",
                    "required": False,
                    "nullable": True,
                    "notes": (
                        "child node type holding variant-specific props; "
                        "null = the parent node itself"
                    ),
                },
            },
        },
        "schemas.attributedef": {
            "natural_key": ["node_type", "json_key", "variant_key"],
            "fields": {
                "json_key": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "variant_key": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                    "notes": "part of the natural key; may be null",
                },
                "name": {
                    "type": "string",
                    "required": True,
                    "nullable": False,
                },
                "is_required": {
                    "type": "boolean",
                    "required": False,
                    "nullable": False,
                    "default": False,
                },
                "is_common": {
                    "type": "boolean",
                    "required": False,
                    "nullable": False,
                    "default": False,
                },
                "group": {
                    "type": "string",
                    "required": False,
                    "nullable": True,
                },
            },
            "fks": {
                "node_type": {
                    "model": "schemas.nodetype",
                    "natural_key": ["name"],
                    "shape": "[natural_key]",
                    "required": False,
                    "nullable": True,
                    "notes": (
                        "null = attribute not bound to a node type"
                    ),
                },
                "data_type": {
                    "model": "schemas.datatype",
                    "natural_key": ["name"],
                    "shape": "[natural_key]",
                    "required": True,
                    "nullable": False,
                    "notes": (
                        "schemas.datatype is NOT authorizable via this "
                        "payload — the reference must point at an "
                        "existing data type"
                    ),
                },
                "domain": {
                    "model": "schemas.domain",
                    "natural_key": ["domain_name"],
                    "shape": "[natural_key]",
                    "required": False,
                    "nullable": True,
                },
            },
            "notes": [
                "'node_type' is part of the natural key and may be null — "
                "the natural key of an unbound attribute starts with null.",
            ],
        },
    },
    "resolved_vs_import": {
        "summary": (
            "The resolved catalog served by s7://catalog/{scope} "
            "(GET catalog/<scope>/) is a denormalized read shape built "
            "for authoring schema instances — it is NOT the shape "
            "s7_import_catalog accepts. The import payload carries one "
            "fixture object per catalog row; the mapping below "
            "translates each resolved block into the objects it "
            "becomes."
        ),
        "mapping": [
            {
                "resolved": "node_types[]",
                "fixture": "schemas.nodetype",
                "notes": (
                    "one object per entry — name/label/is_container/"
                    "default_json_key/is_json_map/json_scalar_attribute "
                    "keep their names inside fields."
                ),
            },
            {
                "resolved": "root_node_type / node_types[].is_root",
                "fixture": "schemas.nodetype fields is_root + json_scope",
                "notes": (
                    "the resolved payload puts the scope name at top "
                    "level and marks the root node type is_root, but "
                    "never emits json_scope — a root object sets "
                    "fields.is_root=true plus fields.json_scope (the "
                    "scope it owns)."
                ),
            },
            {
                "resolved": "node_types[].attributes[]",
                "fixture": "schemas.attributedef",
                "notes": (
                    "nested attribute entries become separate objects "
                    "bound to their node type by the node_type FK — a "
                    "natural-key list like [\"example_root\"], not an "
                    "embedded block. json_key/name keep their names; "
                    "variant_key is part of the natural key — write it "
                    "explicitly even when null."
                ),
            },
            {
                "resolved": "attributes[].required / attributes[].common",
                "fixture": "attributedef fields is_required / is_common",
                "notes": "plain boolean renames.",
            },
            {
                "resolved": (
                    "attributes[].data_type / attributes[].domain "
                    "(flat strings; domain may be null)"
                ),
                "fixture": (
                    "attributedef fks data_type / domain — natural-key "
                    "lists inside fields, e.g. [\"string\"] / "
                    "[\"yes_no\"]; a null domain serializes as null"
                ),
                "notes": (
                    "data_type is required and must reference an "
                    "existing datatype — schemas.datatype is not "
                    "authorizable through this payload."
                ),
            },
            {
                "resolved": "attributes[].server_generated=true",
                "fixture": "schemas.attributedef",
                "notes": (
                    "the flag describes VALUE emission, not "
                    "authoring: the engine derives the value "
                    "(display_order = node.sort_order + 1, "
                    "natural_uuid = node.id), forces required "
                    "to false on read and ignores any non-null "
                    "value an instance payload sends "
                    "(attribute_ignored) — the attributedef row "
                    "itself IS authored like any other; declaring "
                    "it is what makes the key exist in the "
                    "dialect. Never send the value in schema "
                    "payloads."
                ),
            },
            {
                "resolved": (
                    "node_types[].variants — {discriminator_attr, "
                    "props_node_type, keys[]}"
                ),
                "fixture": "schemas.nodetypevariant",
                "notes": (
                    "the digested block expands back to one object per "
                    "key in keys[]: node_type=[\"<node type name>\"], "
                    "variant_key=<the key>, discriminator_attr=<the "
                    "shared discriminator> (part of the natural key — "
                    "write it explicitly; null marks a props variant "
                    "resolved through props_node_type), props_node_type="
                    "[\"<node type name>\"] or null."
                ),
            },
            {
                "resolved": "domains — {domain_name: [{value, label}]}",
                "fixture": "schemas.domain + schemas.domainitem",
                "notes": (
                    "one domain object per dict key (fields."
                    "domain_name) plus one domainitem per {value, "
                    "label} entry with fields.domain="
                    "[\"<domain_name>\"]."
                ),
            },
            {
                "resolved": (
                    "node_types[].compositions[] — {collection_key, "
                    "child_types[{name, min_children, max_children}]}"
                ),
                "fixture": "schemas.nodetypecomposition",
                "notes": (
                    "one object per child_types entry — parent_type "
                    "and child_type are natural-key lists, "
                    "collection_key/min_children/max_children go in "
                    "fields. collection_key is part of the natural "
                    "key — write it explicitly, null for the "
                    "structural (ungrouped) children. child "
                    "default_json_key is read context; it belongs to "
                    "the child's nodetype object."
                ),
            },
        ],
        "example": {
            "resolved": {
                "scope": "example_scope",
                "root_node_type": "example_root",
                "node_types": [
                    {
                        "name": "example_root",
                        "label": "Example root",
                        "is_root": True,
                        "is_container": True,
                        "default_json_key": None,
                        "is_json_map": False,
                        "json_scalar_attribute": None,
                        "attributes": [
                            {
                                "json_key": "answer",
                                "name": "answer",
                                "data_type": "string",
                                "domain": "yes_no",
                                "required": True,
                                "server_generated": False,
                                "common": True,
                                "variant_key": None,
                            },
                            {
                                "json_key": "natural_uuid",
                                "name": "natural_uuid",
                                "data_type": "natural_uuid",
                                "domain": None,
                                "required": False,
                                "server_generated": True,
                                "common": True,
                                "variant_key": None,
                            },
                        ],
                        "variants": None,
                        "compositions": [],
                    },
                ],
                "domains": {
                    "yes_no": [
                        {"value": "yes", "label": "Yes"},
                        {"value": "no", "label": "No"},
                    ],
                },
                "datatypes": {},
                "example": {"example_scope": {"answer": "yes"}},
            },
            "import": {
                "objects": [
                    {
                        "model": "schemas.nodetype",
                        "fields": {
                            "name": "example_root",
                            "label": "Example root",
                            "is_container": True,
                            "is_root": True,
                            "json_scope": "example_scope",
                        },
                    },
                    {
                        "model": "schemas.domain",
                        "fields": {
                            "domain_name": "yes_no",
                        },
                    },
                    {
                        "model": "schemas.domainitem",
                        "fields": {
                            "domain": ["yes_no"],
                            "value": "yes",
                            "label": "Yes",
                        },
                    },
                    {
                        "model": "schemas.domainitem",
                        "fields": {
                            "domain": ["yes_no"],
                            "value": "no",
                            "label": "No",
                        },
                    },
                    {
                        "model": "schemas.attributedef",
                        "fields": {
                            "node_type": ["example_root"],
                            "json_key": "answer",
                            "variant_key": None,
                            "name": "answer",
                            "data_type": ["string"],
                            "domain": ["yes_no"],
                            "is_required": True,
                            "is_common": True,
                        },
                    },
                    {
                        "model": "schemas.attributedef",
                        "fields": {
                            "node_type": ["example_root"],
                            "json_key": "natural_uuid",
                            "variant_key": None,
                            "name": "natural_uuid",
                            "data_type": ["natural_uuid"],
                            "domain": None,
                            "is_required": True,
                            "is_common": True,
                        },
                    },
                ],
            },
        },
        "notes": [
            (
                "datatypes — the resolved 'datatypes' block documents "
                "the value grammar of the non-trivial datatypes in "
                "use; it is instance-authoring guidance, not fixture "
                "rows."
            ),
            (
                "no catalog row carries a 'version' field — schema "
                "'version' belongs to instance documents (the "
                "s7_import_draft payload), never to s7_import_catalog "
                "objects."
            ),
        ],
    },
    "expressions": {
        "summary": (
            "Usage guidance for attributes whose data_type is "
            "'expression': their string value may embed {{ expr }} "
            "tokens that the consumer evaluates at render time. A "
            "token spanning the whole value resolves to the "
            "expression's result type (a boolean, a number), inline "
            "tokens interpolate into the surrounding text, text "
            "without tokens is a plain literal, and non-string "
            "scalars (bool/int/float) are degenerate literals "
            "coerced to text."
        ),
        "when_to_use": (
            "Only expression-typed attributes interpret {{expr}} "
            "tokens — no other datatype evaluates them; {{expr}} "
            "is not a generic templating feature. A "
            "{{expr}}-leading value on a domain-bound attribute "
            "of any datatype is treated as dynamic input, though: "
            "it skips literal domain membership and its token "
            "syntax is still validated. Reach for it when a "
            "context flag or value "
            "customizes that single prop value. Keep "
            "conditional-typed attributes (show_if / enabled_if) "
            "structured — conditional varies the tree's structure "
            "or visibility, {{ expr }} varies one value."
        ),
        "syntax": {
            "paths": (
                "context paths — dotted identifiers with optional "
                "integer list indices, e.g. features.flag or "
                "items.0.name"
            ),
            "literals": (
                "'single-quoted strings' (no escapes, no double "
                "quotes), numbers, and the keywords true / false / "
                "null"
            ),
            "operators": (
                "ternary ? :, logic && ||, unary !, equality == "
                "!=, comparisons > < >= <= (non-associative — a < "
                "b < c is invalid), null-coalescing ??"
            ),
            "grouping": (
                "parentheses; precedence loosest to tightest: "
                "? :, ??, ||, &&, == / !=, comparisons, !, primary"
            ),
        },
        "not_supported": [
            "function calls — there is no name(arg) form",
            "arithmetic operators (+ - * / and the like)",
            "a nested {{ token inside another token",
            "assignment or any statement form",
            "member access beyond dotted paths (no brackets, no calls)",
            "double-quoted or escaped string literals",
            "an unclosed {{ token — validation fails outright",
            "an empty token — {{}} carries no expression",
            "negative number literals — no unary minus, a > -1 fails",
            "chained comparisons such as a < b < c",
        ],
        "examples": [
            {
                "prop": "logo_asset",
                "value": (
                    "{{features.dark_mode ? 'logo_dark' : 'logo_light'}}"
                ),
                "notes": (
                    "whole-value ternary — the prop resolves to one "
                    "of the two literals, picked by a context flag."
                ),
            },
            {
                "prop": "title",
                "value": "Welcome, {{user.display_name ?? 'guest'}}",
                "notes": (
                    "inline token — a ?? fallback interpolated into "
                    "surrounding text; the result is a string."
                ),
            },
            {
                "prop": "enabled",
                "value": "{{features.chat && !user.anonymous}}",
                "notes": (
                    "whole-value boolean combining && and ! — the "
                    "prop resolves to a boolean, not text."
                ),
            },
            {
                "prop": "status_label",
                "value": (
                    "{{items.0.score >= pass_mark ? 'pass' : 'fail'}}"
                ),
                "notes": (
                    "integer list index in a path feeding a "
                    "comparison inside a ternary."
                ),
            },
        ],
        "validation": (
            "Malformed {{expr}} values fail the datatype's plugin "
            "validator and surface through s7_validate_schema / "
            "s7_import_draft errors[] like any other validation "
            "error — there is no separate expression-check "
            "endpoint. Validation is syntax-only: operand types "
            "resolve in the consumer runtime, not at authoring "
            "time."
        ),
        "canonical_grammar": (
            "The 'expression' entry of the resolved catalog's "
            "'datatypes' block — served by s7://catalog/{scope} "
            "(s7_get_catalog) for scopes that use the datatype — "
            "is the canonical grammar (operator and literal "
            "tables); s7://datatypes lists datatype names and "
            "descriptions only, not the grammar. This section "
            "summarizes usage and does not redefine it."
        ),
    },
}


def validate_example_import_objects(
    objects: list,
    models: dict,
    *,
    allowed_models: "set | frozenset | list | None" = None,
) -> list[str]:
    """Validate the ``example.import.objects[]`` worked example.

    Shared guard living next to the document it checks, so a single
    implementation governs the example's import validity — both test
    suites that pin the fragment consume it:

    - ``s7_mcp/tests/test_server.py::CatalogGrammarDataTests`` passes
      ``objects`` and ``models`` straight from ``CATALOG_GRAMMAR`` plus
      its static ``EXPECTED_MODELS`` set as ``allowed_models``.
    - the backend-side drift suite passes the same fragment plus the
      live ``CATALOG_MODELS`` whitelist from the catalog import service
      — bare labels (``"domain"``), not the ``schemas.<label>`` form
      the grammar cites.

    ``allowed_models`` is normalized with ``removeprefix("schemas.")``
    on both sides, so either label form works transparently; ``None``
    skips the authorizability check.

    Per object the check is the strict union of the axes the two
    original guards enforced: ``model`` resolves under ``models`` and
    (when a whitelist is given) is authorizable; ``fields`` carries no
    ``id``/``version`` key, holds every ``natural_key`` component and
    every ``required`` field/fk, and names only keys documented under
    ``fields``/``fks``; an fk value is ``None`` only where the fk entry
    is ``nullable`` and is otherwise a list of natural-key strings; a
    scalar ``fields`` position never takes a list. An empty ``objects``
    is an error — the example must teach.

    Returns descriptive error strings — each naming ``object[<index>]``,
    the ``(<model>)`` and the field when relevant — so callers assert
    ``errors == []``; payload-level problems are reported, never raised.
    """
    if not objects:
        return [
            "example.import.objects[] is empty — nothing to validate"
        ]
    allowed = (
        None
        if allowed_models is None
        else {label.removeprefix("schemas.") for label in allowed_models}
    )
    errors: list[str] = []
    for index, obj in enumerate(objects):
        label = obj.get("model") if isinstance(obj, dict) else None
        if not isinstance(label, str):
            errors.append(
                f"object[{index}] carries no 'model' label to resolve"
            )
            continue
        where = f"object[{index}] ({label})"
        if label not in models:
            errors.append(
                f"object[{index}] cites model {label!r} which has no "
                "entry under 'models'"
            )
            continue
        if allowed is not None:
            bare = label.removeprefix("schemas.")
            if bare not in allowed:
                errors.append(
                    f"object[{index}] cites '{label}' — "
                    f"'{bare}' is not an authorizable catalog model"
                )
        model = models[label]
        fields = obj.get("fields")
        if not isinstance(fields, dict):
            errors.append(f"{where} carries no 'fields' dict to check")
            continue
        # 'id' is forbidden inside fields (it belongs at top-level 'pk');
        # 'version' is an instance-document field, never a catalog row.
        for forbidden in ("id", "version"):
            if forbidden in fields:
                errors.append(
                    f"{where} carries forbidden key '{forbidden}' "
                    "inside 'fields'"
                )
        for name in model["natural_key"]:
            if name not in fields:
                errors.append(
                    f"{where} is missing natural-key component '{name}'"
                )
        for name, spec in model["fields"].items():
            if spec["required"] and name not in fields:
                errors.append(
                    f"{where} is missing required field '{name}'"
                )
        for name, spec in model["fks"].items():
            if spec["required"] and name not in fields:
                errors.append(
                    f"{where} is missing required fk '{name}'"
                )
        documented = set(model["fields"]) | set(model["fks"])
        for name, value in fields.items():
            if name not in documented:
                errors.append(
                    f"field '{name}' in {where} is not documented under "
                    f"'fields' or 'fks' — documented: {sorted(documented)}"
                )
                continue
            if name in model["fks"]:
                if value is None:
                    if not model["fks"][name]["nullable"]:
                        errors.append(
                            f"fk '{name}' in {where} is documented "
                            "non-nullable but the example sets null"
                        )
                elif not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    errors.append(
                        f"fk '{name}' in {where} is documented with "
                        "shape '[natural_key]' — a list of strings — "
                        f"but the example carries {value!r}"
                    )
            elif isinstance(value, list):
                errors.append(
                    f"field '{name}' in {where} carries a natural-key "
                    "list but is documented as a scalar under 'fields' "
                    "— list values belong only in fks positions"
                )
    return errors

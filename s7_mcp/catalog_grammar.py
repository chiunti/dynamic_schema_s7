"""Versioned field reference for the level-2 catalog-import fixture payload.

Static document shipped with the package — the package is standalone (no
Django), so the reference lives here and is guarded by a drift test on the
backend side that keeps it honest against the real model fields.

``CATALOG_GRAMMAR`` is served verbatim by the ``s7://catalog-grammar``
resource. Shape:

- ``format`` — one-line description of the payload format.
- ``payload`` — envelope semantics (``objects``/``pk``/``fields`` and the
  update rules).
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
"""

CATALOG_GRAMMAR: dict = {
    "format": (
        "Django fixture-style objects payload accepted by "
        "s7_import_catalog (POST catalog/import/)."
    ),
    "payload": {
        "objects": (
            "list of {'model': 'schemas.<label>', 'pk': <uuid, optional>, "
            "'fields': {...}}"
        ),
        "pk": (
            "optional — deterministic uuid5 by natural key is derived "
            "when omitted"
        ),
        "fields": (
            "writable model fields only; 'id' is forbidden here (it "
            "belongs at top-level 'pk')"
        ),
        "semantics": (
            "a row whose pk or natural key matches is updated, no match "
            "creates it (pk and natural key pointing at different rows "
            "is an error); deletes are not supported; an update replaces "
            "the row — omitted fields fall back to their defaults. "
            "Matching reads the serialized natural key, so write every "
            "natural-key component explicitly — omitting a component "
            "that carries a model default (e.g. discriminator_attr) "
            "matches no row and the insert collides on uniqueness"
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
                "fixture": "(no object — read-only)",
                "notes": (
                    "engine-managed attributes (the natural_* family) "
                    "are emitted on read only — never author an "
                    "attributedef for them."
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
}

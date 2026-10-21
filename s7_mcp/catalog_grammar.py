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
}

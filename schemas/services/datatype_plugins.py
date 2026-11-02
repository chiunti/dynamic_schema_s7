"""
Datatype Plugins Module

Registry and dispatch for datatype-specific value validators.

This module provides a plugin system where datatype authors can register
validators for complex value shapes (e.g., conditional expressions). The
core contract:

- Validators are callables with signature ``(value: Any) -> None``.
- Validators MUST raise ``django.core.exceptions.ValidationError`` for
  invalid input. ``SchemaValidationError`` raised by the validator is also
  routed through the invalid-input channel — but the import pipeline
  re-wraps it, rooting the reported path at the attribute's json_key, so a
  path the plugin set on the exception does not survive.
- Any other exception type (ValueError, RuntimeError, NameError, etc.) is
  treated as a bug in the validator and propagates as a 500 error or falls
  into a different error channel (e.g., the per-key wrapper in the import
  pipeline converts ValueError to SchemaValidationError even in permissive
  mode).

This contract ensures that validators signal input invalidity through the
two recognized exception types, which the import pipeline handles
according to mode (strict vs. permissive).
"""

from decimal import Decimal
from typing import Any, Optional

from django.utils.module_loading import import_string

from .conditional_validator import (
    ALLOWED_LOGIC_OPERATORS,
    ALLOWED_OPERAND_KINDS,
    ALLOWED_OPERATORS,
)
from .expression_validator import (
    ALLOWED_COALESCE_OPERATORS,
    ALLOWED_COMPARISON_OPERATORS,
    ALLOWED_EQUALITY_OPERATORS,
    ALLOWED_LOGIC_OPERATORS as ALLOWED_EXPRESSION_LOGIC_OPERATORS,
    ALLOWED_UNARY_OPERATORS,
    KEYWORD_LITERALS,
)

DATATYPE_VALIDATORS = {
    'conditional': 'schemas.services.conditional_validator.validate_conditional_value',
    'expression': 'schemas.services.expression_validator.validate_expression_value',
}

# Declarative per-datatype documentation surfaced by the resolved catalog
# (CatalogService.get_resolved_catalog -> "datatypes" section). Parallel to
# DATATYPE_VALIDATORS: register an entry here for datatypes whose JSON value
# grammar is not self-evident; plain scalars (string, number, bool, ...) are
# intentionally undocumented.
#
# Entry keys:
#   - description: what the value expresses and where it is used.
#   - shape: structural skeleton of the JSON value. List values are item
#     templates ("list of objects like this"); leaf strings are placeholders
#     that reference the enum keys below ('one of operators', ...) or spell
#     out the operand rule — never duplicate the allowed tokens literally so
#     the docs cannot drift from the validator.
#   - operators / logic_operators / operand_kinds: valid token sets, derived
#     from the validator module at import time.
#   - example: a concrete value that must pass the datatype's validator.
DATATYPE_DOCS: dict[str, dict[str, Any]] = {
    'conditional': {
        'description': (
            'Boolean expression evaluated over sibling node values, used by '
            'conditional properties such as show_if / enabled_if. The value '
            'is either a JSON boolean or an object with "logic" and a '
            'non-empty "conditions" list. A {"field": "<node key>"} operand '
            'references the key of the sibling node whose value is compared '
            '(conditionals are resolved against node.key of the siblings).'
        ),
        'shape': {
            'logic': 'one of logic_operators',
            'conditions': [
                {
                    'left': 'operand — exactly one of {"field": "<node key>"} | {"value": <JSON literal>}',
                    'op': 'one of operators',
                    'right': 'operand — exactly one of {"field": "<node key>"} | {"value": <JSON literal>}',
                },
            ],
        },
        'operators': sorted(ALLOWED_OPERATORS),
        'logic_operators': sorted(ALLOWED_LOGIC_OPERATORS),
        'operand_kinds': sorted(ALLOWED_OPERAND_KINDS),
        'example': {
            'logic': 'and',
            'conditions': [
                {'left': {'field': 'age'}, 'op': '>=', 'right': {'value': 18}},
            ],
        },
    },
    'expression': {
        'description': (
            'Client-side expression string evaluated by the consumer at '
            'render time, stored verbatim in value_string. The value is a '
            'plain string that may embed {{ expression }} tokens: a token '
            'occupying the whole value resolves to the expression result '
            'type, inline tokens interpolate into the surrounding text, and '
            'text without tokens is a literal. Inside a token, expressions '
            'combine context paths (dotted identifiers with optional '
            'integer list indices), literals and the operators below; '
            'validation is syntax-only — operand types resolve in the '
            'consumer runtime.'
        ),
        'shape': {
            'value': 'string — plain text, or text embedding one or more {{ expression }} tokens',
            'token': '{{ expression }} — whitespace-insensitive; a token spanning the whole value preserves the result type',
            'expression': (
                'literal | path | ( expression ) | unary_operators expression '
                '| expression coalesce_operators expression '
                '| expression logic_operators expression '
                '| expression equality_operators expression '
                '| expression comparison_operators expression '
                "| expression '?' expression ':' expression"
            ),
            'path': "identifier ('.' identifier | '.' integer)* — e.g. features.flag or items.0.name",
            'literal': "'string' | number | one of keyword_literals",
        },
        'comparison_operators': sorted(ALLOWED_COMPARISON_OPERATORS),
        'equality_operators': sorted(ALLOWED_EQUALITY_OPERATORS),
        'logic_operators': sorted(ALLOWED_EXPRESSION_LOGIC_OPERATORS),
        'unary_operators': sorted(ALLOWED_UNARY_OPERATORS),
        'coalesce_operators': sorted(ALLOWED_COALESCE_OPERATORS),
        'keyword_literals': sorted(KEYWORD_LITERALS),
        'example': "{{wallet.count ?? 0}} {{features.compact ? 'items' : 'rows'}}",
    },
}


def get_datatype_doc(name: str) -> Optional[dict[str, Any]]:
    """
    Returns the registered documentation for a datatype name, or None when
    the datatype carries no documented grammar (e.g. plain scalars).
    """
    return DATATYPE_DOCS.get(name)


def validate_datatype_value(data_type, value):
    """
    Validates a value against the validator registered for a datatype.

    This function dispatches to the plugin validator registered in
    ``DATATYPE_VALIDATORS`` for the given datatype name. Validators MUST
    raise ``django.core.exceptions.ValidationError`` for invalid input;
    ``SchemaValidationError`` is also routed through the invalid-input
    channel (re-wrapped by the pipeline — see module docstring). Any other
    exception type is treated as a bug in the validator (see module
    docstring for the full contract).

    Args:
        data_type: The DataType instance (its ``name`` attribute is used for
            lookup in ``DATATYPE_VALIDATORS``).
        value: The value to validate.

    Raises:
        ValidationError: If the registered validator raises it (invalid input).
        SchemaValidationError: If the registered validator raises it (routed
            through the invalid-input channel).
        ImportError/other: Propagates if the validator path is invalid or the
            validator itself raises a non-ValidationError exception (bug surface).

    Returns:
        None. This is a no-op if no validator is registered or value is None.
    """
    if value is None:
        return

    validator_path = DATATYPE_VALIDATORS.get(data_type.name)
    if validator_path:
        validator = import_string(validator_path)
        validator(value)


def datatype_validates_expressions(data_type) -> bool:
    """True when the datatype's registered plugin validator IS the
    expression validator — ``validate_datatype_value`` already gates
    ``{{...}}`` syntax for it, so callers must not re-validate.

    Used by the domain-check bypass in the import pipeline: a
    ``{{``-leading value on a domain-bound attribute skips literal domain
    membership because its semantics resolve client-side. That skip is
    only safe when some layer validates the token's syntax — for
    datatypes whose plugin is not the expression validator, the pipeline
    re-runs ``validate_expression_value`` itself before allowing the
    bypass.

    Args:
        data_type: The DataType instance, or None (``None`` → False —
            no datatype means no plugin gates the syntax).

    Returns:
        True when ``DATATYPE_VALIDATORS`` maps the datatype's name to the
        same validator registered under ``'expression'``.
    """
    name = getattr(data_type, 'name', None)
    return DATATYPE_VALIDATORS.get(name) == DATATYPE_VALIDATORS['expression']


def get_storage_type(data_type):
    """
    Returns the effective primary storage type for a DataType.

    Resolution order: the explicit ``primary_storage_type`` field wins; an
    empty value falls back to name-based tuples for legacy datatypes that
    predate the field, and finally to ``'json'``.

    An empty ``primary_storage_type`` is also the declared convention for
    column-mapped datatypes (``natural_*``, ``display_order``): they write
    ``schema_nodes`` columns — never ``NodeAttribute`` rows — so every write
    path intercepts them before storage resolution and the fallback result
    is never consulted for them.
    """
    storage = data_type.primary_storage_type
    if storage:
        return storage

    name = data_type.name
    if name in ('string', 'date', 'color', 'internal', 'uuid', 'url', 'auto_uuid', 'expression'):
        return 'string'
    if name in ('number', 'int', 'float', 'integer', 'decimal'):
        return 'number'
    if name in ('bool', 'boolean'):
        return 'bool'
    return 'json'


def get_storage_defaults(data_type, value):
    """
    Build a defaults dict for update_or_create_node_attribute based on the
    DataType's primary_storage_type.
    """
    storage = get_storage_type(data_type)

    if storage == 'string':
        return {"value_string": str(value), "value_number": None, "value_bool": None, "value_json": None}
    elif storage == 'number':
        # DecimalField coercion is storage-specific: Decimal(str(...)) keeps
        # floats clean. Numbers routed to 'json' storage must NOT be converted —
        # Decimal is not JSON-serializable and crashes the INSERT.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = Decimal(str(value))
        return {"value_string": None, "value_number": value, "value_bool": None, "value_json": None}
    elif storage == 'bool':
        return {"value_string": None, "value_number": None, "value_bool": bool(value), "value_json": None}
    elif storage == 'json':
        return {"value_string": None, "value_number": None, "value_bool": None, "value_json": value}
    else:
        raise ValueError(f"Unsupported primary_storage_type: {storage}")


def is_column_mapped_datatype(data_type_name: str) -> bool:
    """True when a datatype is column-mapped: it writes ``schema_nodes``
    columns or is derived from them at build time, so it NEVER produces a
    ``NodeAttribute`` row.

    Covers ``display_order`` and every ``natural_*`` datatype (including
    ``natural_version``). Any check that counts stored ``NodeAttribute``
    rows — e.g. ``SchemaValidationService.collect_required_warnings`` —
    can never be satisfied by a def bound to a column-mapped datatype, so
    it must exclude this whole set, not just the server-managed subset.

    See ``is_server_managed_attribute`` for the related but distinct
    question: server-managed attributes are the subset of column-mapped
    datatypes whose payload value the engine ignores. ``natural_version``
    and ``natural_*`` attributes named after author-settable Node fields
    (``key``/``name``/``sort_order``/``version``) are column-mapped but
    NOT server-managed — the payload writes through to the column.
    """
    return data_type_name == 'display_order' or data_type_name.startswith('natural_')


_AUTHOR_SETTABLE_NODE_FIELDS = frozenset({"sort_order", "name", "key", "version"})
"""Node columns an author can write through a ``natural_*`` attribute.

Mirrors the ``node_fields`` set in
``SchemaService._build_json_key_to_field_map`` so the server-managed check
stays a pure string predicate — no model imports needed by either consumer
(CatalogService emission, SchemaService import).
"""


def is_server_managed_attribute(data_type_name: str, attribute_name: str) -> bool:
    """True when an attribute is server-managed: it does not write through
    to an author-settable Node column on import, so the engine derives its
    emitted value and ignores whatever the payload carries.

    ``display_order`` is unconditionally server-managed:
    ``SchemaService._build_json_key_to_field_map`` excludes it from the
    json_key→column map regardless of the attribute name, and output always
    rebuilds it from ``node.sort_order``.

    For ``natural_*`` attributes the check mirrors the same map:
    ``natural_version`` always writes ``node.version`` and attributes named
    after a writable Node field (``key``/``name``/``sort_order``/``version``)
    write that column; every other ``natural_*`` attribute (e.g.
    ``natural_uuid`` bound to ``id``) is generated by the engine and ignored
    in the payload.

    See ``is_column_mapped_datatype`` for the broader set this is a subset
    of: every column-mapped datatype (``natural_*`` + ``display_order``)
    skips ``NodeAttribute`` persistence, including the author-settable
    attributes this predicate excludes.
    """
    if data_type_name == 'display_order':
        return True
    if data_type_name == 'natural_version':
        return False
    if attribute_name in _AUTHOR_SETTABLE_NODE_FIELDS:
        return False
    return data_type_name.startswith('natural_')

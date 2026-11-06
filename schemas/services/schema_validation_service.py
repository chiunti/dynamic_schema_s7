"""
Service for schema validation logic.
"""

import uuid
from collections import Counter
from decimal import Decimal
from typing import List, Dict, Any, Optional, TYPE_CHECKING

from django.core.exceptions import ValidationError

from ..constants import (
    ERR_CARDINALITY_VIOLATION,
    ERR_INVALID_DATATYPE_VALUE,
    ERR_INVALID_DOMAIN_VALUE,
    ERR_INVALID_SCHEMA,
    ERR_STORAGE_COLUMN_MISMATCH,
    WARN_MISSING_REQUIRED_ATTRIBUTES,
)
from ..repositories.schema_repository import SchemaRepository
from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.composition_repository import CompositionRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..utils import domain_lookup_value
from .datatype_plugins import (
    datatype_validates_expressions,
    get_storage_type,
    is_column_mapped_datatype,
    validate_datatype_value,
)
from .exceptions import SchemaValidationError
from .expression_validator import (
    is_expression_string,
    validate_expression_value,
)

if TYPE_CHECKING:
    from ..models import Node


class SchemaValidationService:
    """Service for schema validation operations."""

    def __init__(self):
        self.schema_repository = SchemaRepository()
        self.attribute_def_repository = AttributeDefRepository()
        self.node_type_repository = NodeTypeRepository()
        self.composition_repository = CompositionRepository()

    def _read_discriminator_value(self, node: 'Node', discriminator_key: str, existing_def_ids) -> Optional[str]:
        """Read the stored value of ``node``'s discriminator attribute."""
        disc_def = self.schema_repository.get_attribute_def(node.node_type, discriminator_key)
        if not disc_def or disc_def.id not in existing_def_ids:
            return None
        disc_attr = self.schema_repository.get_node_attribute_by_node_attr_def(node, disc_def)
        return disc_attr.value_string if disc_attr else None

    def _resolve_inherited_variant(self, node: 'Node') -> Optional[str]:
        """Resolve the variant a props node inherits from its parent.

        A props node type declares NodeTypeVariant rows with
        discriminator_attr=None: it has no discriminator of its own, so the
        active variant is read from the parent node's declared discriminator.
        Returns None when there is no parent, no parent variant config, or no
        stored discriminator value.
        """
        parent = node.parent
        if parent is None:
            return None
        parent_ntv = self.node_type_repository.get_node_type_variant_by_node_type(parent.node_type)
        if parent_ntv is None or parent_ntv.discriminator_attr is None:
            return None
        parent_def_ids = set(
            na.attribute_def_id
            for na in self.schema_repository.get_node_attributes_by_node(parent)
        )
        return self._read_discriminator_value(parent, parent_ntv.discriminator_attr, parent_def_ids)

    def _active_variant(self, node: 'Node', existing_def_ids) -> Optional[str]:
        """Active variant of ``node`` per the declarative discriminator.

        A node type with no ``NodeTypeVariant`` rows has no variants at
        all; one declaring ``discriminator_attr`` reads the variant
        from the node's own stored discriminator value, and a props
        node type (variant rows with ``discriminator_attr=None``)
        inherits its variant from the parent node's discriminator.
        """
        ntv = self.node_type_repository.get_node_type_variant_by_node_type(node.node_type)
        if ntv is not None and ntv.discriminator_attr is not None:
            return self._read_discriminator_value(node, ntv.discriminator_attr, existing_def_ids)
        if ntv is not None:
            return self._resolve_inherited_variant(node)
        return None

    def _missing_required_for_node(self, node: 'Node') -> List[str]:
        """Required AttributeDef json_keys missing on ``node``.

        Only defs applicable to the node's active variant count — a node
        type with no NodeTypeVariant rows has no variants at all, and a
        props node type (variant rows with discriminator_attr=None)
        inherits its variant from the parent node's discriminator.

        Column-mapped datatypes (natural_*, display_order) never produce
        NodeAttribute rows — they write schema_nodes columns or are
        derived at build time — so a required def bound to one is
        unsatisfiable; the whole column-mapped set is excluded, not just
        natural_*.

        Read-only — runs inside the caller's transaction.
        """
        all_defs = self.attribute_def_repository.get_attribute_defs_by_node_type_required(node.node_type)
        existing_def_ids = set(
            na.attribute_def_id
            for na in self.schema_repository.get_node_attributes_by_node(node)
        )

        # Determine active variant from the declarative discriminator.
        current_variant = self._active_variant(node, existing_def_ids)

        # Only check defs that apply to active variant
        applicable = [
            d for d in all_defs if d.variant_key is None or d.variant_key == current_variant
        ]

        return [
            d.json_key
            for d in applicable
            if d.id not in existing_def_ids
            and not is_column_mapped_datatype(d.data_type.name)
        ]

    def collect_required_warnings(self, root_node_id: uuid.UUID) -> List[Dict[str, Any]]:
        """
        Walk the node tree and return missing required AttributeDefs per node.

        Args:
            root_node_id: UUID of the root node to check

        Returns:
            List of dicts with missing required attributes per node:
            [{"node_id": str, "node_name": str, "node_type": str, "missing": [str]}]
        """
        warnings = []
        stack = [root_node_id]
        visited = set()

        while stack:
            nid = stack.pop()
            if nid in visited:
                continue
            visited.add(nid)

            node = self.schema_repository.get_node_by_id_with_node_type(nid)
            if not node:
                continue

            missing = self._missing_required_for_node(node)
            if missing:
                warnings.append({
                    "node_id": str(node.id),
                    "node_name": node.name,
                    "node_type": node.node_type.name,
                    "missing": missing,
                })

            for child_id in self.schema_repository.get_children_ids_by_parent(nid):
                stack.append(child_id)

        return warnings

    def revalidate_schema(self, root_node_id: uuid.UUID) -> List[Dict[str, Any]]:
        """Deep re-validation of a persisted schema against the CURRENT catalog.

        Single DFS from ``root_node_id`` — same traversal pattern as
        :meth:`collect_required_warnings`; every reachable node is checked
        for:

        a. missing required attributes (same per-node logic as the
           warnings collector, via :meth:`_missing_required_for_node`),
        b. stored attribute values that no longer satisfy their def's
           domain or the datatype's plugin validator — the read-side
           mirror of the import pipeline's strict checks, and
        c. child-count violations of the current composition rules.

        Read-only — runs inside the caller's transaction, so it observes
        the catalog exactly as an uncommitted apply left it. Findings
        describe consequences, never gate: invalid content yields finding
        dicts instead of raising (only infrastructure failures propagate).
        """
        findings: List[Dict[str, Any]] = []
        stack = [root_node_id]
        visited = set()

        while stack:
            nid = stack.pop()
            if nid in visited:
                continue
            visited.add(nid)

            node = self.schema_repository.get_node_by_id_with_node_type(nid)
            if not node:
                continue

            missing = self._missing_required_for_node(node)
            if missing:
                findings.append(self._finding(
                    node,
                    WARN_MISSING_REQUIRED_ATTRIBUTES,
                    f"missing required attributes: {', '.join(missing)}",
                ))

            findings.extend(self._validate_stored_attributes(node))
            findings.extend(self._check_cardinality(node))

            for child_id in self.schema_repository.get_children_ids_by_parent(nid):
                stack.append(child_id)

        return findings

    def _validate_stored_attributes(self, node: 'Node') -> List[Dict[str, Any]]:
        """Domain/datatype findings over ``node``'s stored NodeAttributes.

        The logical value is reconstructed from the ``value_*`` column
        the def's datatype storage selects — the same resolution the
        write path applies through ``get_storage_type``: string →
        ``value_string``, number → ``value_number``, bool →
        ``value_bool``, json → ``value_json``. A row whose selected
        column is NULL while another column carries the payload —
        e.g. the def's datatype changed after the write — reports a
        ``storage_column_mismatch`` finding: the current contract
        wins, so the stranded value is surfaced instead of silently
        skipped or re-validated under the old type. An all-NULL row
        carries no checkable payload and is skipped cleanly.
        """
        findings: List[Dict[str, Any]] = []
        node_attributes = self.schema_repository.get_node_attributes_with_defs(node)
        existing_def_ids = {
            attribute.attribute_def.id for attribute in node_attributes
        }
        variant_resolved = False
        current_variant: Optional[str] = None
        for attr in node_attributes:
            attr_def = attr.attribute_def
            storage = get_storage_type(attr_def.data_type)
            columns = {
                'string': attr.value_string,
                'number': attr.value_number,
                'bool': attr.value_bool,
                'json': attr.value_json,
            }
            value = columns.get(storage)
            if value is None:
                occupied = next(
                    (
                        column_name
                        for column_name, column_value in columns.items()
                        if column_value is not None
                    ),
                    None,
                )
                if occupied is not None:
                    findings.append(self._finding(
                        node,
                        ERR_STORAGE_COLUMN_MISMATCH,
                        f"value for '{attr_def.json_key}' is stored in "
                        f"'value_{occupied}' but the def's datatype "
                        f"declares '{storage}' storage "
                        f"('value_{storage}')",
                    ))
                continue

            # The effective domain — mirror of the write path's
            # SchemaService._effective_domain: a materialized def may
            # carry domain=NULL (a stale def, the shape the 0007
            # backfill repairs), so fall back to the domain-bearing
            # declaration for (node_type, json_key, variant). The
            # active variant resolves lazily — only defs without a
            # materialized domain need it.
            effective_domain = attr_def.domain
            if effective_domain is None:
                if not variant_resolved:
                    current_variant = self._active_variant(
                        node, existing_def_ids,
                    )
                    variant_resolved = True
                declaration = (
                    self.attribute_def_repository
                    .get_domain_declaration(
                        node.node_type, attr_def.json_key, current_variant,
                    )
                )
                effective_domain = (
                    declaration.domain if declaration else None
                )

            is_dynamic = is_expression_string(value)

            # A '{{'-leading value asserts expression intent — it
            # resolves client-side, so literal domain membership is
            # skipped (mirror of the import pipeline's domain-check
            # bypass). The bypass is only safe when the token's syntax
            # is gated elsewhere: defs whose datatype does not run the
            # expression plugin re-validate the token here, and a
            # malformed token reports a finding — never a free pass
            # (the import pipeline reports ERR_INVALID_SCHEMA for the
            # same case).
            if (is_dynamic
                    and effective_domain is not None
                    and not datatype_validates_expressions(
                        attr_def.data_type,
                    )):
                try:
                    validate_expression_value(value)
                except (ValidationError, SchemaValidationError) as exc:
                    findings.append(self._finding(
                        node,
                        ERR_INVALID_SCHEMA,
                        f"invalid value for '{attr_def.json_key}': {exc}",
                    ))
                    continue

            if (effective_domain is not None
                    and isinstance(value, (str, int, float, bool, Decimal))
                    and not is_dynamic
                    and not self.attribute_def_repository.domain_item_exists(
                        effective_domain, self._domain_lookup_value(value),
                    )):
                findings.append(self._finding(
                    node,
                    ERR_INVALID_DOMAIN_VALUE,
                    f"value {value!r} for '{attr_def.json_key}' is not a "
                    f"member of domain '{effective_domain.domain_name}'",
                ))

            # Plugin validators gate per-datatype value shapes
            # (conditional structures, expression syntax) — the import
            # pipeline's strict-mode check, accumulated as a finding
            # instead of raised.
            try:
                validate_datatype_value(attr_def.data_type, value)
            except (ValidationError, SchemaValidationError) as exc:
                findings.append(self._finding(
                    node,
                    ERR_INVALID_DATATYPE_VALUE,
                    f"invalid value for '{attr_def.json_key}': {exc}",
                ))

        return findings

    def _check_cardinality(self, node: 'Node') -> List[Dict[str, Any]]:
        """Child-count findings against the current composition rules.

        Only compositions declaring a bound participate — a composition
        with both ``min_children`` and ``max_children`` NULL admits any
        count. The node's children are fetched once and counted per
        ``node_type_id`` in Python instead of running one COUNT query
        per bounded composition; with no bounded composition at all the
        node needs no child read.
        """
        findings: List[Dict[str, Any]] = []
        bounded = [
            composition
            for composition in self.composition_repository
                .get_compositions_by_parent_type(node.node_type)
            if (composition.min_children is not None
                or composition.max_children is not None)
        ]
        if not bounded:
            return findings
        counts = Counter(
            child['node_type_id']
            for child in self.schema_repository
                .get_children_with_type_by_parent(node.id)
        )
        for composition in bounded:
            count = counts.get(composition.child_type_id, 0)
            if (composition.min_children is not None
                    and count < composition.min_children):
                findings.append(self._finding(
                    node,
                    ERR_CARDINALITY_VIOLATION,
                    f"children of type '{composition.child_type.name}': "
                    f"{count} found, at least "
                    f"{composition.min_children} required",
                ))
            if (composition.max_children is not None
                    and count > composition.max_children):
                findings.append(self._finding(
                    node,
                    ERR_CARDINALITY_VIOLATION,
                    f"children of type '{composition.child_type.name}': "
                    f"{count} found, at most "
                    f"{composition.max_children} allowed",
                ))
        return findings

    @staticmethod
    def _finding(node: 'Node', code: str, message: str) -> Dict[str, Any]:
        """Assemble one revalidation finding dict."""
        return {
            'code': code,
            'message': message,
            'node_id': str(node.id),
            'node_key': node.key,
            'node_type': node.node_type.name,
        }

    @staticmethod
    def _domain_lookup_value(value: Any) -> str:
        """Coerce a scalar to the string form stored in ``DomainItem.value``.

        Shared normalization lives in :func:`schemas.utils.domain_lookup_value`
        (bool → 'true'/'false'; integral ``Decimal``/``float`` → ``str(int(v))``)
        — the write path and the post-apply revalidator must never drift again
        (an un-normalized integral float caused a false ``invalid_domain_value``).
        """
        return domain_lookup_value(value)

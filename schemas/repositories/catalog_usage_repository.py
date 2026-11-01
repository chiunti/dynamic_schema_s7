"""Implementation-usage lookups for catalog grammar objects.

The catalog import usage gate rejects updates to catalog rows that live
schemas already instantiate. This repository answers "is this catalog
row implemented?" for each writable catalog model, counting the schema
rows that realize it and listing the projects that own them.

Semantics shared by every lookup:

- **Global scope** — the grammar is shared across organizations and
  projects, so a schema belonging to *any* project blocks the update.
- **Status-agnostic** — lifecycle state lives in attribute values, not
  in row existence; the mere presence of ``Node``/``NodeAttribute``
  rows is the complete implementation condition.
- **Row existence, not value** — a ``NodeAttribute`` whose ``value_*``
  columns are all NULL still implements its AttributeDef: the property
  is already referenced by a node.
- **Precise depth** — each model is gated on its own instantiation:
  a DomainItem only when its ``value`` is stored, a NodeTypeVariant
  only when a node carries its ``variant_key`` in the discriminator,
  a NodeTypeComposition only when the declared parent→child edge
  exists. An implemented parent does not cascade the gate onto catalog
  children that were never instantiated.
"""

import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db.models import Count, Q, QuerySet

from ..constants import (
    ERR_PK_MUST_BE_VALID_UUID,
    ERR_UNKNOWN_CATALOG_MODEL_LABEL,
)
from ..models import (
    AttributeDef,
    Domain,
    DomainItem,
    Node,
    NodeAttribute,
    NodeType,
    NodeTypeComposition,
    NodeTypeVariant,
)

# Bool columns can only hold true/false; catalog string values are
# mapped through these literals so 'True', 'FALSE', ... compare
# correctly and anything else simply cannot match that storage type.
_BOOL_LITERALS = {'true': True, 'false': False}


class CatalogUsageRepository:
    """Implementation-usage lookups for catalog grammar objects.

    ``usage_for`` is the single entry point consumed by the catalog
    import usage gate: it resolves the addressed catalog row once and
    dispatches to the per-model lookup. All lookups are read-only and
    side-effect free.

    Timing: the gate runs inside the caller's transaction, but under
    READ COMMITTED a schema import committed between this check and the
    outer commit escapes verification — a residual TOCTOU window
    accepted for v1 (the gate is an advisory read, not a lock).
    """

    # Catalog import label -> (catalog model, per-model handler name).
    # Must stay in sync with CATALOG_MODELS in
    # catalog_import_service.py — a label missing here raises the
    # defensive ValueError below mid-gate.
    _DISPATCH = {
        'nodetype': (NodeType, '_node_type_usage'),
        'attributedef': (AttributeDef, '_attribute_def_usage'),
        'nodetypecomposition': (
            NodeTypeComposition, '_composition_usage',
        ),
        'nodetypevariant': (NodeTypeVariant, '_variant_usage'),
        'domain': (Domain, '_domain_usage'),
        'domainitem': (DomainItem, '_domain_item_usage'),
    }

    def usage_for(self, label: str, pk: 'str | uuid.UUID') -> dict:
        """Usage of one catalog row addressed by model label + pk.

        Args:
            label: the catalog model label as carried in the import
                payload (the second component of ``schemas.<label>``);
                one of ``domain``, ``domainitem``, ``nodetype``,
                ``nodetypecomposition``, ``nodetypevariant``,
                ``attributedef``.
            pk: primary key of the catalog row, as resolved by the
                caller's classification pass.

        Returns:
            ``{"implemented": bool, "usage": {<metric>: int,
            "projects": [str, ...]}}`` where ``<metric>`` is
            ``"nodes"`` for nodetype, nodetypecomposition and
            nodetypevariant, or ``"node_attributes"`` for attributedef,
            domain and domainitem; ``projects`` is the sorted list of
            distinct non-null ``Project.slug`` values owning the
            implementing rows.

            A well-formed pk whose row no longer exists (deleted
            between the caller's classification and this check)
            returns ``implemented=False`` with zeroed usage instead of
            raising: the caller reclassifies the object as an insert,
            and inserts are never gated — "not implemented" is the
            semantically correct answer.

        Raises:
            ValueError: ``label`` is not a writable catalog model
                label, or ``pk`` is not a valid UUID. Defensive input
                validation — the caller only reaches this lookup with
                values it produced itself.
        """
        if not isinstance(label, str) or label not in self._DISPATCH:
            raise ValueError(
                ERR_UNKNOWN_CATALOG_MODEL_LABEL.format(label=label),
            )
        if not isinstance(pk, (str, uuid.UUID)):
            raise ValueError(
                ERR_PK_MUST_BE_VALID_UUID.format(pk=pk),
            )
        try:
            canonical_pk = str(uuid.UUID(str(pk)))
        except (ValueError, AttributeError, TypeError) as e:
            raise ValueError(
                ERR_PK_MUST_BE_VALID_UUID.format(pk=pk),
            ) from e

        model, handler_name = self._DISPATCH[label]
        row = model.objects.filter(pk=canonical_pk).first()
        return getattr(self, handler_name)(row)

    def _node_type_usage(self, node_type: Optional[NodeType]) -> dict:
        """A NodeType is implemented once any Node instantiates it."""
        if node_type is None:
            return self._empty_usage('nodes')
        nodes = Node.objects.filter(node_type_id=node_type.id)
        return self._result('nodes', nodes.count(), self._node_projects(nodes))

    def _attribute_def_usage(
        self, attribute_def: Optional[AttributeDef],
    ) -> dict:
        """Any referencing NodeAttribute row implements the def.

        Rows whose ``value_*`` columns are all NULL count too: the
        attribute is already referenced by a node even with no value
        set, so modifying the def would change the meaning of existing
        schema data.
        """
        if attribute_def is None:
            return self._empty_usage('node_attributes')
        attributes = NodeAttribute.objects.filter(
            attribute_def_id=attribute_def.id,
        )
        return self._result(
            'node_attributes',
            attributes.count(),
            self._attribute_projects(attributes),
        )

    def _composition_usage(
        self, composition: Optional[NodeTypeComposition],
    ) -> dict:
        """Implemented once the declared parent→child edge is instantiated.

        Only the existence of a ``child_type`` node under a
        ``parent_type`` node gates the row — ``collection_key`` is
        catalog metadata, not part of the instantiation edge, so an
        update to it is gated by edge usage alone.
        """
        if composition is None:
            return self._empty_usage('nodes')
        nodes = Node.objects.filter(
            node_type_id=composition.child_type_id,
            parent__node_type_id=composition.parent_type_id,
        )
        return self._result('nodes', nodes.count(), self._node_projects(nodes))

    def _variant_usage(self, variant: Optional[NodeTypeVariant]) -> dict:
        """Implemented once a node stores ``variant_key`` in the discriminator.

        The discriminator is resolved from the variant row itself —
        every ``AttributeDef`` with ``node_type=variant.node_type`` and
        ``json_key=variant.discriminator_attr`` is a candidate: sibling
        defs may share that pair under different ``variant_key`` values
        (``uq_sattr_defs_key`` allows it), so matching only one def
        could attribute the implementing rows to a sibling and report
        an implemented variant as free.

        When the row declares no discriminator (``discriminator_attr``
        NULL marks a props-node-type variant inherited from its parent,
        which has no variant-specific instantiation of its own) or no
        AttributeDef matches, the variant cannot be instantiated *as a
        variant* — zeroed usage is returned.

        The comparison targets the ``value_*`` column selected by each
        def's ``data_type.primary_storage_type``; see ``_value_q`` for
        the string→column conversion policy.
        """
        if variant is None or not variant.discriminator_attr:
            return self._empty_usage('nodes')
        value_q = self._defs_value_q(
            variant.variant_key,
            self._defs_by_storage(
                node_type_id=variant.node_type_id,
                json_key=variant.discriminator_attr,
            ),
        )
        if value_q is None:
            return self._empty_usage('nodes')
        attributes = NodeAttribute.objects.filter(
            value_q,
            node__node_type_id=variant.node_type_id,
        )
        # (node, attribute_def) is unique so each row is already one
        # node, but the metric is distinct nodes — the aggregate keeps
        # the count honest by construction.
        node_count = attributes.aggregate(
            n=Count('node_id', distinct=True),
        )['n']
        return self._result(
            'nodes', node_count, self._attribute_projects(attributes),
        )

    def _domain_usage(self, domain: Optional[Domain]) -> dict:
        """Implemented once any AttributeDef bound to the domain has rows."""
        if domain is None:
            return self._empty_usage('node_attributes')
        attributes = NodeAttribute.objects.filter(
            attribute_def__domain_id=domain.id,
        )
        return self._result(
            'node_attributes',
            attributes.count(),
            self._attribute_projects(attributes),
        )

    def _domain_item_usage(self, item: Optional[DomainItem]) -> dict:
        """Implemented when ``item.value`` is stored by a def of its domain.

        The item's value is a catalog string, but NodeAttribute rows
        persist it under the ``value_*`` column chosen by each def's
        ``data_type.primary_storage_type`` — and defs bound to the same
        domain may disagree on storage type. The defs are therefore
        grouped by storage type and OR'd into a single query, each
        group with the item value converted for its column (see
        ``_value_q``); a storage type the value cannot convert to
        simply cannot contain it and is skipped.
        """
        if item is None:
            return self._empty_usage('node_attributes')
        value_q = self._defs_value_q(
            item.value,
            self._defs_by_storage(domain_id=item.domain_id),
        )
        if value_q is None:
            return self._empty_usage('node_attributes')

        attributes = NodeAttribute.objects.filter(value_q)
        return self._result(
            'node_attributes',
            attributes.count(),
            self._attribute_projects(attributes),
        )

    @staticmethod
    def _defs_by_storage(**filters) -> dict:
        """AttributeDef ids grouped by their data type's storage type.

        Maps each ``primary_storage_type`` to the ids of the matching
        AttributeDefs so the caller can AND every storage-homogeneous
        group with the ``value_*`` lookup its column supports.
        """
        ids_by_storage: dict[str, list] = {}
        for row in (
            AttributeDef.objects
            .filter(**filters)
            .values('id', 'data_type__primary_storage_type')
        ):
            ids_by_storage.setdefault(
                row['data_type__primary_storage_type'], [],
            ).append(row['id'])
        return ids_by_storage

    def _defs_value_q(
        self, raw: str, ids_by_storage: dict,
    ) -> Optional[Q]:
        """OR'ed ``Q`` matching ``raw`` stored by any of the defs.

        Each storage-homogeneous def group is ANDed with the
        ``value_*`` lookup ``raw`` converts to for that column (see
        ``_value_q``); groups whose column cannot hold ``raw`` are
        skipped. Returns None when no group can contain the value —
        the caller treats it as "not implemented".
        """
        query = Q()
        matched_storage = False
        for storage_type, def_ids in ids_by_storage.items():
            value_q = self._value_q(raw, storage_type)
            if value_q is None:
                continue
            matched_storage = True
            query |= Q(attribute_def_id__in=def_ids) & value_q
        return query if matched_storage else None

    @staticmethod
    def _value_q(raw: str, storage_type: Optional[str]) -> Optional[Q]:
        """``Q`` matching ``raw`` in the column for ``storage_type``.

        Catalog strings (``DomainItem.value``, ``NodeTypeVariant.
        variant_key``) are compared against the typed ``value_*``
        columns; conversion policy per storage type:

        - ``string`` — verbatim comparison.
        - ``number`` — parsed via ``Decimal``; values that do not parse
          or are non-finite (``NaN``, ``Infinity`` — DecimalField
          columns cannot hold them) return None, i.e. this storage
          type cannot contain the value.
        - ``bool`` — only ``'true'``/``'false'`` (case-insensitive)
          map; anything else returns None — a bool column cannot hold
          any other rendering.
        - ``json`` — compared against the raw string *and* the
          ``json.loads``-parsed value when it parses: a stored JSON
          string matches the first arm while a natively stored scalar,
          object or array (e.g. catalog ``'42'`` persisted as JSON
          ``42``) matches the second. A parsed ``None`` needs no
          special-casing: on JSONField an exact ``None`` lookup
          matches stored JSON null, not SQL NULL
          (``can_use_none_as_rhs``).
        - anything else (unknown or empty storage type) — None; the
          caller skips the group, so an undecidable column can never
          produce a false implementation.
        """
        if storage_type == 'string':
            return Q(value_string=raw)
        if storage_type == 'number':
            try:
                parsed = Decimal(raw)
            except (InvalidOperation, TypeError, ValueError):
                return None
            return Q(value_number=parsed) if parsed.is_finite() else None
        if storage_type == 'bool':
            parsed_bool = _BOOL_LITERALS.get(raw.strip().lower())
            return (
                Q(value_bool=parsed_bool)
                if parsed_bool is not None
                else None
            )
        if storage_type == 'json':
            query = Q(value_json=raw)
            try:
                parsed_json = json.loads(raw)
            except (ValueError, TypeError):
                return query
            query |= Q(value_json=parsed_json)
            return query
        return None

    @staticmethod
    def _empty_usage(metric: str) -> dict:
        """Zeroed usage dict — the shape of "nothing implements this"."""
        return {
            'implemented': False,
            'usage': {metric: 0, 'projects': []},
        }

    @staticmethod
    def _result(metric: str, count: int, projects: list[str]) -> dict:
        """Assemble the public ``{implemented, usage}`` result dict."""
        return {
            'implemented': count > 0,
            'usage': {metric: count, 'projects': projects},
        }

    @staticmethod
    def _node_projects(nodes: QuerySet) -> list[str]:
        """Sorted, distinct, non-null slugs owning the given nodes."""
        slugs = nodes.values_list('project__slug', flat=True).distinct()
        return sorted(slug for slug in slugs if slug is not None)

    @staticmethod
    def _attribute_projects(attributes: QuerySet) -> list[str]:
        """Sorted, distinct, non-null slugs owning the attributes' nodes."""
        slugs = attributes.values_list(
            'node__project__slug', flat=True,
        ).distinct()
        return sorted(slug for slug in slugs if slug is not None)

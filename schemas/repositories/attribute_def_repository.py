"""
Data access layer for AttributeDef, Domain, DomainItem, NodeType and NodeTypeVariant entities.
"""

import uuid
from typing import Optional

from django.db.models import Q

from ..models import (
    AttributeDef,
    DataType,
    Domain,
    DomainItem,
    NodeType,
    NodeTypeComposition,
    NodeTypeVariant,
)


class AttributeDefRepository:
    """Repository for attribute definition and component property data access."""

    def get_type_attribute_defs_for_type_ids(self, type_ids: list[uuid.UUID]) -> list[AttributeDef]:
        """Return every domain-bound discriminator AttributeDef for the given node type IDs.

        The discriminator json_keys are read from the declarative
        NodeTypeVariant.discriminator_attr values of the given node types
        (usually a single key, e.g. 'type'), instead of assuming a
        fixed attribute name.

        Multi-def semantics: ``uq_sattr_defs_key`` scopes uniqueness to
        ``(node_type, json_key, variant_key)``, so the same
        ``(node_type, json_key)`` can legitimately be declared by
        sibling defs — the ``variant_key=None`` template plus
        materialized variant-scoped copies. Each sibling may carry its
        own domain, and several node types may qualify at once, so
        resolving a single def via an unordered ``.first()`` could
        inspect the wrong sibling's (or the wrong node type's) domain
        and silently produce a wrong result. Callers must therefore
        evaluate every returned candidate's domain.

        The discriminator correlation is per node type: a def only
        qualifies when ITS OWN node type declares its ``json_key`` as
        ``discriminator_attr`` in a ``NodeTypeVariant`` row. A
        domain-bound def on a non-discriminating type (e.g. a props
        type with ``discriminator_attr=None``) whose ``json_key``
        happens to equal another scope type's discriminator is NOT a
        candidate — its domain does not describe component types.

        Args:
            type_ids: List of node type IDs

        Returns:
            List of domain-bound discriminator AttributeDef instances
            with domain select_related, ordered by ``variant_key`` then
            ``node_type_id`` then ``pk`` for determinism (PostgreSQL ASC
            sorts the ``variant_key=None`` template last; ``pk`` fully
            stabilizes the order should duplicate NULL-variant_key rows
            ever exist, which ``uq_sattr_defs_key`` cannot rule out)
        """
        discriminator_pairs = list(
            NodeTypeVariant.objects
            .filter(node_type_id__in=type_ids, discriminator_attr__isnull=False)
            .values_list("node_type_id", "discriminator_attr")
            .distinct()
        )
        def_filter = Q()
        for pair_node_type_id, json_key in discriminator_pairs:
            def_filter |= Q(node_type_id=pair_node_type_id, json_key=json_key)
        if not def_filter:
            return []
        return list(
            AttributeDef.objects.filter(
                def_filter,
                domain__isnull=False,
            )
            .select_related("domain")
            .order_by("variant_key", "node_type_id", "pk")
        )

    def get_component_types_for_domain(self, domain: Domain) -> list[dict]:
        """Return all DomainItems for a given domain, ordered by value."""
        return list(
            DomainItem.objects.filter(domain=domain).order_by("value").values("value", "label")
        )

    def get_domain_by_name(self, domain_name: str) -> Optional[Domain]:
        """Return a Domain instance by its domain_name, or None."""
        return Domain.objects.filter(domain_name=domain_name).first()

    def get_or_create_domain(self, domain_name: str, description: str) -> tuple[Domain, bool]:
        """Get or create a Domain by name."""
        return Domain.objects.get_or_create(
            domain_name=domain_name,
            defaults={"description": description},
        )

    def get_domain_items(self, domain: Domain) -> list[dict]:
        """Return all DomainItems for a given domain, ordered by value."""
        return list(DomainItem.objects.filter(domain=domain).order_by("value").values("value", "label"))

    def delete_domain_items(self, domain: Domain) -> None:
        """Delete all DomainItems for a given domain."""
        DomainItem.objects.filter(domain=domain).delete()

    def bulk_create_domain_items(self, domain: Domain, items: list[dict]) -> None:
        """Bulk create DomainItems for the given domain from {"value", "label"} dicts."""
        DomainItem.objects.bulk_create(
            DomainItem(domain=domain, value=item["value"], label=item["label"])
            for item in items
        )

    def get_all_variants(self, type_ids: Optional[list[uuid.UUID]] = None) -> list[dict]:
        """Return all NodeTypeVariants, optionally filtered to the given node type IDs."""
        qs = NodeTypeVariant.objects.select_related("node_type").order_by("node_type__name", "variant_key")
        if type_ids is not None:
            qs = qs.filter(node_type_id__in=type_ids)
        return list(qs.values("id", "variant_key", "node_type__name"))

    def get_variants_for_node_type(self, node_type_id: uuid.UUID) -> list[str]:
        """Return variant keys for a specific node_type."""
        return list(
            NodeTypeVariant.objects
            .filter(node_type_id=node_type_id)
            .values_list("variant_key", flat=True)
            .order_by("variant_key")
        )

    def get_type_ids_with_catalog_attrs(self, type_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Return the subset of type_ids that have at least one catalog attr (is_common=False, variant_key=None)."""
        return list(
            AttributeDef.objects.filter(
                node_type_id__in=type_ids, is_common=False, variant_key=None
            ).values_list("node_type_id", flat=True).distinct()
        )

    def get_type_ids_with_common_attrs(self, type_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Return the subset of type_ids that have at least one common attr (is_common=True, variant_key=None)."""
        return list(
            AttributeDef.objects.filter(
                node_type_id__in=type_ids, is_common=True, variant_key=None
            ).values_list("node_type_id", flat=True).distinct()
        )

    def get_parent_node_type_ids(self, child_type_ids: list[uuid.UUID], scope_type_ids: list[uuid.UUID] = None) -> list[uuid.UUID]:
        """Return NodeType IDs that are direct composition parents of the given child type IDs.
        Optionally restricted to a scope (pass scope_type_ids to limit results)."""
        qs = NodeTypeComposition.objects.filter(child_type_id__in=child_type_ids)
        if scope_type_ids:
            qs = qs.filter(parent_type_id__in=scope_type_ids)
        return list(qs.values_list("parent_type_id", flat=True).distinct())

    def get_node_type_ids_for_variant(self, variant_key: str, target_node_type_names: list[str] = None) -> list[uuid.UUID]:
        """Return NodeType IDs that have a given variant_key, optionally filtered by name list."""
        qs = NodeTypeVariant.objects.filter(variant_key=variant_key)
        if target_node_type_names:
            qs = qs.filter(node_type__name__in=target_node_type_names)
        return list(qs.values_list("node_type_id", flat=True))

    def get_node_type_names_for_ids(self, type_ids: list[uuid.UUID]) -> list[str]:
        """Return node type names for given IDs."""
        return list(NodeType.objects.filter(id__in=type_ids).values_list("name", flat=True))

    def get_node_type_by_variant(self, variant_key: str) -> Optional[NodeType]:
        """Return the first NodeType that has the given variant_key."""
        nt_variant = NodeTypeVariant.objects.filter(variant_key=variant_key).first()
        return nt_variant.node_type if nt_variant else None

    def variant_key_exists(self, variant_key: str) -> bool:
        """Return True if any NodeTypeVariant has the given variant_key."""
        return NodeTypeVariant.objects.filter(variant_key=variant_key).exists()

    def get_attribute_def_by_id(self, attr_id: str) -> Optional[AttributeDef]:
        """Return an AttributeDef by its PK, or None."""
        return AttributeDef.objects.filter(id=attr_id).first()

    def attribute_def_exists(self, node_type: NodeType, json_key: str, variant_key: Optional[str]) -> bool:
        """Return True if an AttributeDef with the given node_type, json_key and variant_key exists."""
        return AttributeDef.objects.filter(
            node_type=node_type, json_key=json_key, variant_key=variant_key
        ).exists()

    def update_attribute_def_to_common(self, attr_def: AttributeDef) -> None:
        """Set is_common=True and variant_key=None on an AttributeDef."""
        attr_def.is_common = True
        attr_def.variant_key = None
        attr_def.save(update_fields=["is_common", "variant_key"])

    def update_attribute_def_to_specific(self, attr_def: AttributeDef) -> None:
        """Set is_common=False on an AttributeDef."""
        attr_def.is_common = False
        attr_def.save(update_fields=["is_common"])

    def delete_attribute_def(self, attr_def: AttributeDef) -> None:
        """Delete a single AttributeDef."""
        attr_def.delete()

    def get_catalog_attribute_defs(self, node_type_ids: list[uuid.UUID]) -> dict[str, AttributeDef]:
        """Return catalog AttributeDefs (variant_key=None, is_common=False) keyed by json_key."""
        return {
            ad.json_key: ad
            for ad in AttributeDef.objects.filter(
                node_type_id__in=node_type_ids, variant_key=None, is_common=False
            )
        }

    def get_variant_attribute_defs(self, node_type_ids: list[uuid.UUID], variant_key: str) -> list[AttributeDef]:
        """Return all AttributeDefs assigned to a specific variant_key."""
        return list(
            AttributeDef.objects.filter(
                node_type_id__in=node_type_ids,
                variant_key=variant_key,
            )
            .select_related("node_type")
            .values("id", "json_key", "name", "node_type__name", "is_required", "is_common", "variant_key")
        )

    def get_common_attribute_defs(self, node_type_ids: list[uuid.UUID]) -> list[dict]:
        """Return all common AttributeDefs (is_common=True, variant_key=None)."""
        return list(
            AttributeDef.objects.filter(
                node_type_id__in=node_type_ids, is_common=True, variant_key=None
            )
            .select_related("node_type")
            .values("id", "json_key", "name", "node_type__name", "is_required", "is_common", "variant_key")
        )

    def get_catalog_attribute_defs_list(self, node_type_ids: list[uuid.UUID]) -> list[dict]:
        """Return catalog AttributeDefs (is_common=False, variant_key=None) as a list."""
        return list(
            AttributeDef.objects.filter(
                node_type_id__in=node_type_ids, is_common=False, variant_key=None
            )
            .select_related("node_type")
            .values("id", "json_key", "name", "node_type__name", "is_required", "is_common", "variant_key")
        )

    def get_all_attribute_defs_for_variant(self, node_type_ids: list[uuid.UUID], variant_key: str) -> list[dict]:
        """Return all AttributeDefs for node_type_ids, ordered for deduplication by json_key."""
        return list(
            AttributeDef.objects.filter(node_type_id__in=node_type_ids)
            .select_related("node_type")
            .order_by("json_key", "variant_key")
            .values("id", "json_key", "name", "node_type__name", "is_required", "variant_key", "is_common")
        )

    def get_attribute_defs_by_node_type_ids(self, node_type_ids: list[uuid.UUID]) -> list[dict]:
        """
        Get all AttributeDefs for the given node types (any variant_key),
        with data type and domain names resolved.

        Args:
            node_type_ids: List of node type IDs

        Returns:
            List of dicts with node_type_id, json_key, name, is_required,
            is_common, variant_key, data_type__name, domain__domain_name and
            group, ordered by node_type_id, json_key then variant_key
            (PostgreSQL ASC — NULL variant_key rows sort last)
        """
        return list(
            AttributeDef.objects.filter(node_type_id__in=node_type_ids)
            .order_by("node_type_id", "json_key", "variant_key")
            .values(
                "node_type_id", "json_key", "name", "is_required", "is_common",
                "variant_key", "data_type__name", "domain__domain_name", "group",
            )
        )

    def get_active_variant_ids(self, variant_key: str) -> set[str]:
        """Return a set of string IDs for AttributeDefs assigned to a variant_key."""
        return set(
            str(i)
            for i in AttributeDef.objects.filter(variant_key=variant_key).values_list("id", flat=True)
        )

    def get_variant_attr_ids(self, node_type_ids: list[uuid.UUID], variant_key: str) -> set[uuid.UUID]:
        """Return a set of UUIDs for AttributeDefs with a specific variant_key on given node types."""
        return set(
            AttributeDef.objects.filter(
                node_type_id__in=node_type_ids, variant_key=variant_key
            ).values_list("id", flat=True)
        )

    def delete_attribute_defs_by_ids(self, ids: list[uuid.UUID]) -> None:
        """Delete AttributeDefs by a set of IDs."""
        AttributeDef.objects.filter(id__in=ids).delete()

    def delete_variant_attribute_defs(self, node_type_ids: list[uuid.UUID], variant_key: str, json_keys: list[str]) -> None:
        """Delete variant-specific AttributeDefs by json_key."""
        AttributeDef.objects.filter(
            node_type_id__in=node_type_ids,
            variant_key=variant_key,
            json_key__in=json_keys,
        ).delete()

    def get_catalog_attribute_def_by_id(self, node_type_ids: list[uuid.UUID], attr_id: str) -> Optional[AttributeDef]:
        """Return a catalog AttributeDef (variant_key=None, is_common=False) by ID."""
        return AttributeDef.objects.filter(
            node_type_id__in=node_type_ids,
            variant_key=None,
            is_common=False,
            id=attr_id,
        ).first()

    def get_default_data_type(self) -> Optional[DataType]:
        """Return the default 'string' DataType."""
        return DataType.objects.filter(name="string").first()

    def get_all_node_type_ids(self) -> list[uuid.UUID]:
        """Return all NodeType IDs."""
        return list(NodeType.objects.values_list("id", flat=True))

    def get_any_node_type(self) -> Optional[NodeType]:
        """Return any NodeType instance."""
        return NodeType.objects.first()

    def get_schema_cache_keys_by_node_type(self, node_type_name: str) -> list[tuple]:
        """
        Get (key, version, project_id) tuples for nodes of a given node type name.

        Args:
            node_type_name: Name of the node type to filter by

        Returns:
            List of (key, version, project_id) tuples from NodeAttribute/Node
            matching the node type, so callers can scope filters per project
            and version instead of matching on key alone.
        """
        from ..models import NodeAttribute
        return list(
            NodeAttribute.objects.filter(
                node__node_type__name=node_type_name,
                attribute_def__json_key='key'
            ).values_list('value_string', 'node__version', 'node__project_id')
        )

    def get_node_type_variant_config(
        self,
        node_type_ids: list[uuid.UUID],
        variant_key: str
    ) -> list[dict]:
        """Get NodeTypeVariant configs for given node types and variant key."""
        return list(
            NodeTypeVariant.objects.filter(
                node_type_id__in=node_type_ids,
                variant_key=variant_key
            ).values('node_type_id', 'discriminator_attr', 'props_node_type_id')
        )

    def get_attribute_defs_by_node_type_required(self, node_type: NodeType, variant_key: Optional[str] = None) -> list[AttributeDef]:
        """
        Get required attribute definitions for a node type, optionally filtered by variant.

        Args:
            node_type: NodeType instance
            variant_key: Optional variant key to filter by

        Returns:
            List of required AttributeDef instances
        """
        qs = AttributeDef.objects.filter(node_type=node_type, is_required=True)
        if variant_key:
            qs = qs.filter(variant_key=variant_key)
        return list(qs.select_related("data_type"))

    def get_compositions_by_node_type(self, node_type: NodeType) -> list[dict]:
        """
        Get compositions for a given node type.

        Args:
            node_type: NodeType instance

        Returns:
            List of composition dicts
        """
        return list(
            NodeTypeComposition.objects.filter(parent_type=node_type)
            .values("id", "parent_type_id", "child_type_id", "collection_key", "min_children", "max_children")
            .order_by("child_type__name")
        )

    def get_domain_items_by_domain_ids(self, domain_ids):
        """
        Get domain items by domain IDs.

        Args:
            domain_ids: List of domain IDs

        Returns:
            QuerySet of DomainItem instances ordered by domain_id and value
        """
        return DomainItem.objects.filter(domain_id__in=domain_ids).order_by("domain_id", "value")

    def get_domain_items_by_domain_names(self, domain_names: list[str]) -> list[dict]:
        """
        Get DomainItems for the given domain names.

        Args:
            domain_names: List of Domain.domain_name values

        Returns:
            List of dicts with domain__domain_name, value and label,
            ordered by domain name then value
        """
        return list(
            DomainItem.objects.filter(domain__domain_name__in=domain_names)
            .order_by("domain__domain_name", "value")
            .values("domain__domain_name", "value", "label")
        )

    def get_or_create_attribute_def(self, node_type, json_key, variant_key, defaults):
        """
        Get or create an AttributeDef.

        Args:
            node_type: NodeType instance
            json_key: JSON key
            variant_key: Variant key (can be None)
            defaults: Dict of default values

        Returns:
            Tuple of (AttributeDef, created)
        """
        return AttributeDef.objects.get_or_create(
            node_type=node_type,
            json_key=json_key,
            variant_key=variant_key,
            defaults=defaults
        )

    def get_data_type_by_name(self, name):
        """
        Get DataType by name.

        Args:
            name: DataType name

        Returns:
            DataType instance or None
        """
        return DataType.objects.filter(name=name).first()

    def get_attribute_defs_with_domain(self, node_type: NodeType, json_key: str) -> list[AttributeDef]:
        """Return every domain-bound AttributeDef for ``(node_type, json_key)``.

        Multi-def semantics: ``uq_sattr_defs_key`` scopes uniqueness to
        ``(node_type, json_key, variant_key)``, so the same
        ``(node_type, json_key)`` can legitimately be declared by
        sibling defs — the ``variant_key=None`` template plus
        materialized variant-scoped copies. Each sibling may carry its
        own domain, so resolving a single def via an unordered
        ``.first()`` could inspect the wrong sibling's domain and
        silently produce a wrong result. Callers must therefore
        evaluate every returned candidate's domain.

        Args:
            node_type: NodeType instance
            json_key: JSON key

        Returns:
            List of domain-bound AttributeDef instances with domain
            select_related, ordered by ``variant_key`` then ``pk`` for
            determinism (PostgreSQL ASC sorts the ``variant_key=None``
            template last; ``pk`` fully stabilizes the order should
            duplicate NULL-variant_key rows ever exist)
        """
        return list(
            AttributeDef.objects.filter(
                node_type=node_type,
                json_key=json_key,
                domain__isnull=False,
            )
            .select_related("domain")
            .order_by("variant_key", "pk")
        )

    def domain_item_exists(self, domain, value):
        """
        Check if a DomainItem exists for a domain and value.

        Args:
            domain: Domain instance
            value: Value to check

        Returns:
            Boolean indicating if DomainItem exists
        """
        return DomainItem.objects.filter(domain=domain, value=value).exists()

    def get_attribute_defs_by_node_type(self, node_type):
        """
        Get all AttributeDefs by node type (any variant).

        Args:
            node_type: NodeType instance

        Returns:
            QuerySet of AttributeDef instances with data_type and
            domain select_related
        """
        return AttributeDef.objects.filter(node_type=node_type).select_related('data_type', 'domain')

    def get_attribute_defs_by_node_type_variant_key(self, node_type, variant_key):
        """
        Get AttributeDefs by node type and variant key.

        Args:
            node_type: NodeType instance
            variant_key: Variant key (can be None)

        Returns:
            QuerySet of AttributeDef instances
        """
        return AttributeDef.objects.filter(node_type=node_type, variant_key=variant_key)

    def get_attribute_def_by_node_type_json_key_variant(self, node_type, json_key, variant_key):
        """
        Get AttributeDef by node type, json_key and variant key.

        Args:
            node_type: NodeType instance
            json_key: JSON key
            variant_key: Variant key (can be None)

        Returns:
            AttributeDef instance or None
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            variant_key=variant_key
        ).first()

    def get_attribute_defs_by_data_type(self, node_type, data_type_name, variant_key=None):
        """
        Get AttributeDefs of a node type carrying a given data type name.

        ``variant_key=None`` filters to the IS NULL set — covering both
        common attributes (is_common=True) and catalog templates
        (is_common=False), same convention as
        get_attribute_def_by_node_type_json_key_variant.

        Args:
            node_type: NodeType instance
            data_type_name: DataType name (e.g. 'natural_version')
            variant_key: Variant key (can be None)

        Returns:
            QuerySet of AttributeDef instances ordered by json_key so
            'first' is deterministic, with data_type and domain
            select_related
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            data_type__name=data_type_name,
            variant_key=variant_key,
        ).select_related('data_type', 'domain').order_by('json_key')

    def create_attribute_def(self, **kwargs):
        """
        Create an AttributeDef.

        Args:
            **kwargs: Keyword arguments for AttributeDef creation

        Returns:
            AttributeDef instance
        """
        return AttributeDef.objects.create(**kwargs)

    def get_discriminator_attribute_defs(self, node_type: NodeType) -> list[AttributeDef]:
        """
        Return every candidate discriminator AttributeDef for a node type.

        Multi-def semantics: ``uq_sattr_defs_key`` scopes uniqueness to
        ``(node_type, json_key, variant_key)``, so the same
        ``(node_type, json_key)`` can legitimately be declared by
        sibling defs — the ``variant_key=None`` template plus
        materialized variant-scoped copies. NodeAttribute rows may live
        under ANY of those siblings, and each sibling may carry its own
        domain: resolving a single def via an unordered ``.first()``
        could inspect the wrong sibling's domain and silently fail to
        match. Callers must therefore iterate the returned candidates
        and check each def's domain/json_key.

        Prefers the declarative NodeTypeVariant.discriminator_attr
        declared for ``node_type``: the NTV branch returns ALL sibling
        defs sharing the declared ``discriminator_attr`` json_key,
        ordered by ``variant_key`` for determinism. Only when the node
        type has no variant rows at all does it fall back to the legacy
        heuristic (first required + domain-bound + non-natural
        attribute), so catalogs without variant declarations still
        resolve. The configured discriminator may legitimately be
        'type', so the fallback does not exclude any literal key;
        deterministic ordering keeps the heuristic stable when several
        attributes qualify. The fallback intentionally preserves its
        legacy single-candidate semantics — it returns a list of at
        most one def — while the NTV branch covers every sibling.

        Args:
            node_type: NodeType instance

        Returns:
            List of candidate AttributeDef instances with domain
            select_related (empty when the node type is a
            props/inherited-variant node or no discriminator applies)
        """
        variants = NodeTypeVariant.objects.filter(node_type=node_type)
        if variants.exists():
            discriminator_attr = (
                variants.filter(discriminator_attr__isnull=False)
                .values_list("discriminator_attr", flat=True)
                .first()
            )
            if not discriminator_attr:
                # Variant rows exist but declare discriminator_attr=None:
                # this is a props node type whose variant is inherited
                # from the parent — it has no own discriminator.
                return []
            return list(
                AttributeDef.objects.filter(
                    node_type=node_type,
                    json_key=discriminator_attr,
                )
                .select_related("domain")
                .order_by("variant_key")
            )

        # Legacy fallback for catalogs without any NodeTypeVariant rows:
        # first required, domain-bound, non-natural attribute.
        fallback = (
            AttributeDef.objects.filter(
                node_type=node_type,
                variant_key__isnull=True,
                is_required=True,
                domain__isnull=False,
            )
            .exclude(data_type__name__startswith='natural_')
            .order_by('json_key')
            .first()
        )
        return [fallback] if fallback is not None else []

    def get_domain_items_by_domain(self, domain):
        """
        Get DomainItems by domain.

        Args:
            domain: Domain instance

        Returns:
            QuerySet of DomainItem instances
        """
        return DomainItem.objects.filter(domain=domain)

    def attribute_def_exists_with_display_order(self, node_type, json_key):
        """
        Check if an AttributeDef exists with display_order data type.

        Args:
            node_type: NodeType instance
            json_key: JSON key

        Returns:
            Boolean indicating if AttributeDef exists with display_order data type
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            data_type__name='display_order',
        ).exists()

    def get_attribute_def_by_node_type_json_key_variant_common(self, node_type, json_key, variant_key, is_common):
        """
        Get AttributeDef by node type, json_key, variant key and is_common.

        Args:
            node_type: NodeType instance
            json_key: JSON key
            variant_key: Variant key (can be None)
            is_common: Boolean for is_common

        Returns:
            AttributeDef instance or None
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            variant_key=variant_key,
            is_common=is_common
        ).first()

    def universal_attribute_def_exists(self, node_type, json_key):
        """
        Check if a universal AttributeDef exists (variant_key=None, is_common=True).

        Args:
            node_type: NodeType instance
            json_key: JSON key

        Returns:
            Boolean indicating if universal AttributeDef exists
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            variant_key__isnull=True,
            is_common=True
        ).exists()

    def get_attribute_def_declaration(
        self,
        node_type: NodeType,
        json_key: str,
        variant_key: Optional[str],
    ) -> Optional[AttributeDef]:
        """
        Return the AttributeDef declaring ``(node_type, json_key)`` applicable
        to ``variant_key``, or None.

        ``variant_key=None`` rows cover both common attributes (is_common=True)
        and catalog templates (is_common=False) — templates are declarations
        that the import pipeline materializes variant-scoped. A key declared
        only under a different variant_key does NOT apply here. When both an
        exact-variant and a ``variant_key=None`` declaration exist, the exact
        one wins (more specific).

        Args:
            node_type: NodeType instance
            json_key: JSON key
            variant_key: Variant key of the current node context (None allowed)

        Returns:
            The applicable AttributeDef declaration, or None
        """
        qs = AttributeDef.objects.filter(node_type=node_type, json_key=json_key)
        if variant_key is not None:
            declared = qs.filter(variant_key=variant_key).first()
            if declared:
                return declared
        return qs.filter(variant_key__isnull=True).first()

    def get_domain_declaration(
        self,
        node_type: NodeType,
        json_key: str,
        variant_key: Optional[str],
    ) -> Optional[AttributeDef]:
        """
        Return the domain-bearing AttributeDef declaring ``(node_type, json_key)``
        applicable to ``variant_key``, or None.

        Mirrors ``get_attribute_def_declaration`` — exact-variant row first,
        then the ``variant_key=None`` template — but only considers rows with
        a non-NULL ``domain``. This lookup exists *to resolve a domain*, so a
        materialized def without one must not shadow the domain-bearing
        declaration behind it (a stale variant-scoped row would otherwise
        resolve as its own declaration and silently disable the domain
        check). Declaration *existence* semantics (undeclared-key checks)
        stay with ``get_attribute_def_declaration``, which is unchanged: a
        def without ``domain`` is still a valid declaration there.

        Args:
            node_type: NodeType instance
            json_key: JSON key
            variant_key: Variant key of the current node context (None allowed)

        Returns:
            The applicable domain-bearing AttributeDef declaration, or None
        """
        qs = AttributeDef.objects.filter(
            node_type=node_type, json_key=json_key, domain__isnull=False
        )
        if variant_key is not None:
            declared = qs.filter(variant_key=variant_key).first()
            if declared:
                return declared
        return qs.filter(variant_key__isnull=True).first()

    def get_variant_keys_for_json_key(self, node_type: NodeType, json_key: str) -> list:
        """
        Return the distinct variant_keys under which (node_type, json_key) is
        declared (None entries meaning common/catalog declarations). Used to
        enrich 'undeclared key' error details.

        Args:
            node_type: NodeType instance
            json_key: JSON key

        Returns:
            List of variant_key values (may contain None)
        """
        return list(
            AttributeDef.objects.filter(node_type=node_type, json_key=json_key)
            .order_by("variant_key")
            .values_list("variant_key", flat=True)
            .distinct()
        )

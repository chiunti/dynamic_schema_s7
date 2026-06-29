"""
Data access layer for AttributeDef, Domain, DomainItem, NodeType and NodeTypeVariant entities.
"""

import uuid
from typing import Optional

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

    def get_node_type_ids_for_scope(self, scope: str) -> list[uuid.UUID]:
        """Return all NodeType IDs reachable from the root type with the given json_scope via BFS."""
        root = NodeType.objects.filter(json_scope=scope, is_root=True).first()
        if not root:
            return []
        visited = set()
        queue = [root.id]
        while queue:
            current_id = queue.pop()
            if current_id in visited:
                continue
            visited.add(current_id)
            children = NodeTypeComposition.objects.filter(
                parent_type_id=current_id
            ).values_list("child_type_id", flat=True)
            queue.extend(children)
        return list(visited)

    def get_root_node_type_by_scope(self, scope: str) -> Optional[NodeType]:
        """Return the root NodeType for a given json_scope, or None."""
        return NodeType.objects.filter(json_scope=scope, is_root=True).first()

    def get_all_root_node_types(self) -> list[NodeType]:
        """Return all root NodeTypes ordered by name."""
        return list(NodeType.objects.filter(is_root=True).order_by("name"))

    def get_type_attribute_def_for_scope(self, scope: str) -> Optional[AttributeDef]:
        """Return the first AttributeDef with json_key='type' that has a domain, reachable from scope."""
        type_ids = self.get_node_type_ids_for_scope(scope)
        return (
            AttributeDef.objects.filter(
                node_type_id__in=type_ids,
                json_key="type",
                domain__isnull=False,
            )
            .select_related("domain")
            .first()
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

    def bulk_create_domain_items(self, items: list[dict]) -> None:
        """Bulk create DomainItem instances."""
        DomainItem.objects.bulk_create(items)

    def get_all_variants(self, scope: str = "") -> list[dict]:
        """Return all NodeTypeVariants, optionally filtered to node types reachable from scope."""
        qs = NodeTypeVariant.objects.select_related("node_type").order_by("node_type__name", "variant_key")
        if scope:
            type_ids = self.get_node_type_ids_for_scope(scope)
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

    def create_attribute_def(
        self,
        node_type: NodeType,
        name: str,
        json_key: str,
        data_type: DataType,
        is_required: bool,
        is_common: bool,
        variant_key: Optional[str],
        domain: Optional[Domain] = None,
        group: Optional[str] = None,
    ) -> AttributeDef:
        """Create and return a new AttributeDef."""
        kwargs = dict(
            node_type=node_type,
            name=name,
            json_key=json_key,
            data_type=data_type,
            is_required=is_required,
            is_common=is_common,
            variant_key=variant_key,
        )
        if domain is not None:
            kwargs["domain"] = domain
        if group is not None:
            kwargs["group"] = group
        return AttributeDef.objects.create(**kwargs)

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

    def get_schema_cache_keys_by_node_type(self, node_type_name: str) -> list[str]:
        """
        Get schema cache keys for a given node type name.

        Args:
            node_type_name: Name of the node type to filter by

        Returns:
            List of key strings from NodeAttribute matching the node type
        """
        from ..models import NodeAttribute
        return list(
            NodeAttribute.objects.filter(
                node__node_type__name=node_type_name,
                attribute_def__json_key='key'
            ).values_list('value_string', flat=True)
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

    def get_attribute_def_with_domain(self, node_type, json_key):
        """
        Get AttributeDef with domain by node type and json_key.

        Args:
            node_type: NodeType instance
            json_key: JSON key

        Returns:
            AttributeDef instance or None
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            domain__isnull=False,
        ).first()

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
            QuerySet of AttributeDef instances
        """
        return AttributeDef.objects.filter(node_type=node_type)

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

    def create_attribute_def(self, **kwargs):
        """
        Create an AttributeDef.

        Args:
            **kwargs: Keyword arguments for AttributeDef creation

        Returns:
            AttributeDef instance
        """
        return AttributeDef.objects.create(**kwargs)

    def get_discriminator_attribute_def(self, node_type):
        """
        Get discriminator AttributeDef for a node type.

        Args:
            node_type: NodeType instance

        Returns:
            AttributeDef instance or None
        """
        return AttributeDef.objects.filter(
            node_type=node_type,
            variant_key__isnull=True,
            is_required=True,
            domain__isnull=False,
        ).exclude(data_type__name__startswith='natural_').exclude(json_key='type').first()

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

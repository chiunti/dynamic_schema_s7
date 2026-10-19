"""
Repository for NodeType-related database operations.

This repository centralizes all NodeType data access operations — including
root-scope lookups and NodeTypeComposition graph traversal — to eliminate
direct model access from views and admin classes, following the four-layer
architecture pattern.
"""

import uuid
from typing import Optional

from django.db.models import QuerySet

from ..models import NodeType, NodeTypeComposition


class NodeTypeRepository:
    """Repository for NodeType lookups and NodeTypeComposition graph traversal."""

    def get_root_node_type_by_scope(self, scope: str) -> Optional[NodeType]:
        """
        Get root node type by json_scope.

        Args:
            scope: json_scope value to filter by

        Returns:
            NodeType instance or None if not found
        """
        return NodeType.objects.filter(json_scope=scope, is_root=True).first()

    def get_node_type_ids_for_scope(self, scope: str) -> list[uuid.UUID]:
        """Return all NodeType IDs reachable from the root type with the given json_scope via BFS."""
        root = self.get_root_node_type_by_scope(scope)
        if not root:
            return []
        return self.get_node_type_ids_for_root(root.id)

    def get_node_type_ids_for_root(self, root_id: uuid.UUID) -> list[uuid.UUID]:
        """Return all NodeType IDs reachable from the given root type ID via BFS."""
        visited = set()
        queue = [root_id]
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

    def get_node_type_by_name(self, name: str) -> Optional[NodeType]:
        """
        Get node type by name.

        Args:
            name: Node type name

        Returns:
            NodeType instance or None if not found
        """
        return NodeType.objects.filter(name=name).first()

    def get_node_type_by_id(self, node_type_id: uuid.UUID) -> Optional[NodeType]:
        """
        Get node type by ID.

        Args:
            node_type_id: UUID of the node type

        Returns:
            NodeType instance or None if not found
        """
        return NodeType.objects.filter(id=node_type_id).first()

    def get_all_root_node_types(self) -> QuerySet:
        """
        Get all root node types.

        Returns:
            QuerySet of NodeType instances with is_root=True
        """
        return NodeType.objects.filter(is_root=True)

    def get_scopeless_root_node_type(self) -> Optional[NodeType]:
        """
        Get the root node type without a json_scope (json_scope IS NULL).

        At most one is expected — the uq_snode_types_root_scope constraint
        enforces it (NULLS NOT DISTINCT); the order_by keeps the pick
        deterministic even if that invariant is violated.

        Returns:
            NodeType instance or None if not found
        """
        return (
            NodeType.objects.filter(is_root=True, json_scope__isnull=True)
            .order_by("name")
            .first()
        )

    def get_node_types_by_scope(self, scope: str):
        """
        Get node types by json_scope.

        Args:
            scope: json_scope value to filter by

        Returns:
            QuerySet of NodeType instances
        """
        return NodeType.objects.filter(json_scope=scope)

    def get_node_types_by_scope_endswith(self, suffix: str):
        """
        Get node types where json_scope ends with a given suffix.

        Args:
            suffix: Suffix to match in json_scope

        Returns:
            QuerySet of NodeType instances
        """
        return NodeType.objects.filter(json_scope__endswith=suffix)

    def get_node_types_by_ids(self, type_ids: list[uuid.UUID]) -> QuerySet:
        """
        Get node types by IDs.

        Args:
            type_ids: List of node type IDs

        Returns:
            Values QuerySet of dicts with id, name, label, is_root,
            is_container, json_scope, default_json_key, is_json_map and
            json_scalar_attribute, ordered by name
        """
        return (
            NodeType.objects.filter(id__in=type_ids)
            .values(
                "id", "name", "label", "is_root", "is_container",
                "json_scope", "default_json_key", "is_json_map",
                "json_scalar_attribute",
            )
            .order_by("name")
        )

    def has_parent_inferred_variants(self, node_type):
        """
        Check if a node type has variants with discriminator_attr=None.

        Args:
            node_type: NodeType instance

        Returns:
            Boolean indicating if node type has parent-inferred variants
        """
        from ..models import NodeTypeVariant
        return NodeTypeVariant.objects.filter(
            node_type=node_type,
            discriminator_attr__isnull=True
        ).exists()

    def get_variant_keys_by_node_type(self, node_type):
        """
        Get variant keys for a node type.

        Args:
            node_type: NodeType instance

        Returns:
            List of variant keys
        """
        from ..models import NodeTypeVariant
        return list(
            NodeTypeVariant.objects.filter(node_type=node_type)
            .order_by("variant_key")
            .values_list("variant_key", flat=True)
        )

    def get_container_node_type(self):
        """
        Get the first container node type.

        Returns:
            NodeType instance or None
        """
        return NodeType.objects.filter(is_container=True).first()

    def get_node_type_variant_by_props_node_type(self, props_node_type):
        """
        Get NodeTypeVariant by props_node_type.

        Args:
            props_node_type: NodeType instance for props

        Returns:
            NodeTypeVariant instance or None
        """
        from ..models import NodeTypeVariant
        return NodeTypeVariant.objects.filter(props_node_type=props_node_type).first()

    def get_node_type_variant_by_node_type(self, node_type):
        """
        Get NodeTypeVariant by node_type.

        Args:
            node_type: NodeType instance

        Returns:
            NodeTypeVariant instance or None
        """
        from ..models import NodeTypeVariant
        return NodeTypeVariant.objects.filter(node_type=node_type).first()

    def get_node_type_variant_by_node_type_and_variant_key(self, node_type, variant_key):
        """
        Get NodeTypeVariant by node_type and variant_key.

        Args:
            node_type: NodeType instance
            variant_key: Variant key

        Returns:
            NodeTypeVariant instance or None
        """
        from ..models import NodeTypeVariant
        return NodeTypeVariant.objects.filter(
            node_type=node_type,
            variant_key=variant_key
        ).first()

    def get_node_type_variants_by_node_type_ids_and_variant_key(self, node_type_ids, variant_key):
        """
        Get NodeTypeVariants by node_type_ids and variant_key.

        Args:
            node_type_ids: List of node type IDs
            variant_key: Variant key

        Returns:
            QuerySet of NodeTypeVariant instances
        """
        from ..models import NodeTypeVariant
        return NodeTypeVariant.objects.filter(
            node_type_id__in=node_type_ids,
            variant_key=variant_key
        ).select_related('props_node_type')

    def is_props_node_type(self, node_type) -> bool:
        """
        Check if this node_type is a props_node_type according to NodeTypeVariant.

        A node_type is considered a props_node_type if it appears as props_node_type
        in any NodeTypeVariant configuration (meaning it stores properties for other
        node types that have a discriminator).

        Args:
            node_type: NodeType instance

        Returns:
            Boolean indicating if node_type is a props_node_type
        """
        from ..models import NodeTypeVariant
        return NodeTypeVariant.objects.filter(
            props_node_type=node_type
        ).exists()

    def get_props_node_type_ids(self) -> set:
        """
        Get IDs of all node types used as props_node_type in NodeTypeVariant.

        Returns:
            Set of NodeType UUIDs declared as props containers
        """
        from ..models import NodeTypeVariant
        return set(
            NodeTypeVariant.objects.filter(
                props_node_type__isnull=False
            ).values_list("props_node_type_id", flat=True)
        )

    def get_discriminator_attr(self, node_type) -> Optional[str]:
        """
        Get discriminator attribute from NodeTypeVariant configuration.

        This method implements the Declarative Variant Model by reading the
        discriminator_attr from NodeTypeVariant instead of hardcoding 'type'.

        Args:
            node_type: NodeType instance

        Returns:
            Discriminator attribute name or None if not configured
        """
        from ..models import NodeTypeVariant
        ntv = NodeTypeVariant.objects.filter(node_type=node_type).first()
        return ntv.discriminator_attr if ntv else None

    def get_discriminator_attrs_by_node_type_ids(self, node_type_ids) -> dict:
        """
        Get declared discriminator attributes for many node types at once.

        Args:
            node_type_ids: Iterable of NodeType UUIDs

        Returns:
            Dict mapping node_type_id -> discriminator_attr for node types
            whose NodeTypeVariant rows declare a (non-null) discriminator.
        """
        from ..models import NodeTypeVariant
        return dict(
            NodeTypeVariant.objects
            .filter(node_type_id__in=node_type_ids, discriminator_attr__isnull=False)
            .values_list("node_type_id", "discriminator_attr")
            .distinct()
        )

    def get_node_type_variants_by_node_type_ids(self, node_type_ids: list[uuid.UUID]) -> list[dict]:
        """
        Get all NodeTypeVariant rows for the given node type IDs.

        Args:
            node_type_ids: List of node type IDs

        Returns:
            List of dicts with node_type_id, variant_key, discriminator_attr
            and props_node_type__name, ordered by node_type_id then variant_key
        """
        from ..models import NodeTypeVariant
        return list(
            NodeTypeVariant.objects.filter(node_type_id__in=node_type_ids)
            .order_by("node_type_id", "variant_key")
            .values(
                "node_type_id", "variant_key", "discriminator_attr",
                "props_node_type__name",
            )
        )

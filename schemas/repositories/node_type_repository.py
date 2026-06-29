"""
Repository for NodeType-related database operations.

This repository centralizes all NodeType data access operations to eliminate
direct model access from views and admin classes, following the four-layer
architecture pattern.
"""

import uuid
from typing import Optional
from ..models import NodeType


class NodeTypeRepository:
    """Repository for NodeType-related database operations"""

    def get_root_node_type_by_scope(self, scope: str) -> Optional[NodeType]:
        """
        Get root node type by json_scope.

        Args:
            scope: json_scope value to filter by

        Returns:
            NodeType instance or None if not found
        """
        return NodeType.objects.filter(json_scope=scope, is_root=True).first()

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

    def get_all_root_node_types(self):
        """
        Get all root node types.

        Returns:
            QuerySet of NodeType instances with is_root=True
        """
        return NodeType.objects.filter(is_root=True)

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

    def get_node_types_by_ids(self, type_ids):
        """
        Get node types by IDs.

        Args:
            type_ids: List of node type IDs

        Returns:
            QuerySet of NodeType instances
        """
        return NodeType.objects.filter(id__in=type_ids).values("id", "name", "label", "is_root", "is_container", "json_scope").order_by("name")

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

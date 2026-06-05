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

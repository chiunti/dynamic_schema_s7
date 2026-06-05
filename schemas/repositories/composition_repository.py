"""
Repository for NodeTypeComposition data access.
"""

import uuid
from typing import Optional, Type, Any

from ..models import NodeType


class CompositionRepository:
    """Repository for composition-related database operations."""

    def get_composition_by_id(self, comp_id: uuid.UUID, composition_model: Type[Any]) -> Optional[Any]:
        """
        Get composition by ID.

        Args:
            comp_id: UUID of the composition
            composition_model: Model class (NodeTypeComposition)

        Returns:
            Composition instance or None if not found
        """
        return composition_model.objects.filter(id=comp_id).first()

    def get_composition_by_id_with_relations(self, comp_id: uuid.UUID, composition_model: Type[Any]) -> Optional[Any]:
        """
        Get composition by ID with parent_type and child_type select_related.

        Args:
            comp_id: UUID of the composition
            composition_model: Model class (NodeTypeComposition)

        Returns:
            Composition instance or None if not found
        """
        return composition_model.objects.select_related("parent_type", "child_type").filter(
            id=comp_id
        ).first()

    def get_node_type_by_id(self, node_type_id: uuid.UUID) -> Optional[NodeType]:
        """
        Get node type by ID.

        Args:
            node_type_id: UUID of the node type

        Returns:
            NodeType instance or None if not found
        """
        return NodeType.objects.filter(id=node_type_id).first()

    def composition_exists(self, parent_type: NodeType, child_type: NodeType, composition_model: Type[Any]) -> bool:
        """
        Check if composition exists for given parent and child types.

        Args:
            parent_type: Parent NodeType
            child_type: Child NodeType
            composition_model: Model class (NodeTypeComposition)

        Returns:
            Boolean indicating if composition exists
        """
        return composition_model.objects.filter(parent_type=parent_type, child_type=child_type).exists()

    def create_composition(
        self,
        parent_type: NodeType,
        child_type: NodeType,
        composition_model: Type[Any],
        collection_key: Optional[str] = None
    ) -> Any:
        """
        Create a new composition.

        Args:
            parent_type: Parent NodeType
            child_type: Child NodeType
            composition_model: Model class (NodeTypeComposition)
            collection_key: Optional collection key

        Returns:
            Created composition instance
        """
        return composition_model.objects.create(
            parent_type=parent_type,
            child_type=child_type,
            collection_key=str(collection_key).strip() if collection_key else None,
        )

    def delete_composition(self, comp_id: uuid.UUID, composition_model: Type[Any]) -> bool:
        """
        Delete composition by ID.

        Args:
            comp_id: UUID of the composition
            composition_model: Model class (NodeTypeComposition)

        Returns:
            Boolean indicating if deletion was successful
        """
        deleted, _ = composition_model.objects.filter(id=comp_id).delete()
        return deleted > 0

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

    def get_compositions_by_parent_type_ids(self, parent_type_ids: list[uuid.UUID], composition_model: Type[Any]):
        """
        Get compositions by parent type IDs.

        Args:
            parent_type_ids: List of parent NodeType IDs
            composition_model: Model class (NodeTypeComposition)

        Returns:
            QuerySet of compositions
        """
        return composition_model.objects.filter(parent_type_id__in=parent_type_ids).order_by("parent_type__name", "child_type__name")

    def get_child_type_ids_by_parent_type_id(self, parent_type_id: uuid.UUID, composition_model: Type[Any]) -> list[uuid.UUID]:
        """
        Get child type IDs for a given parent type ID.

        Args:
            parent_type_id: UUID of the parent node type
            composition_model: Model class (NodeTypeComposition)

        Returns:
            List of child node type IDs
        """
        return list(
            composition_model.objects.filter(parent_type_id=parent_type_id)
            .values_list("child_type_id", flat=True)
        )

    def get_composition_by_parent_child_collection_key(self, parent_type, child_type, collection_key: str):
        """
        Get composition by parent type, child type, and collection key.

        Args:
            parent_type: Parent NodeType instance
            child_type: Child NodeType instance
            collection_key: Collection key to filter by

        Returns:
            Composition instance or None
        """
        from ..models import NodeTypeComposition
        return NodeTypeComposition.objects.filter(
            parent_type=parent_type,
            child_type=child_type,
            collection_key=collection_key
        ).first()

    def get_child_type_names_by_parent_no_collection_key(self, parent_type):
        """
        Get child type names for compositions without collection_key.

        Args:
            parent_type: Parent NodeType instance

        Returns:
            Set of child type names
        """
        from ..models import NodeTypeComposition
        return set(
            NodeTypeComposition.objects
            .filter(parent_type=parent_type, collection_key__isnull=True)
            .values_list('child_type__name', flat=True)
        )

    def get_compositions_by_parent_type_select_related(self, node_type):
        """
        Get compositions for a parent type with child_type select_related.

        Args:
            node_type: Parent NodeType instance

        Returns:
            QuerySet of NodeTypeComposition instances
        """
        from ..models import NodeTypeComposition
        return NodeTypeComposition.objects.filter(parent_type=node_type).select_related('child_type')

    def get_compositions_by_parent_type(self, node_type):
        """
        Get compositions for a parent type with child_type select_related.

        Args:
            node_type: Parent NodeType instance

        Returns:
            QuerySet of NodeTypeComposition instances
        """
        return self.get_compositions_by_parent_type_select_related(node_type)

    def get_compositions_by_node_type(self, node_type):
        """
        Get compositions for a node type (alias for get_compositions_by_parent_type).

        Args:
            node_type: Parent NodeType instance

        Returns:
            QuerySet of NodeTypeComposition instances
        """
        return self.get_compositions_by_parent_type(node_type)

    def composition_exists_by_collection_key(self, node_type, collection_key):
        """
        Check if a composition exists by parent type and collection key.

        Args:
            node_type: Parent NodeType instance
            collection_key: Collection key

        Returns:
            Boolean indicating if composition exists
        """
        from ..models import NodeTypeComposition
        return NodeTypeComposition.objects.filter(
            parent_type=node_type,
            collection_key=collection_key
        ).exists()

    def update_composition(self, comp_id: uuid.UUID, collection_key: Optional[str] = None, min_children: Optional[int] = None, max_children: Optional[int] = None):
        """
        Update a composition.

        Args:
            comp_id: Composition UUID
            collection_key: New collection key (optional)
            min_children: New min children (optional)
            max_children: New max children (optional)

        Returns:
            Number of rows updated
        """
        from ..models import NodeTypeComposition
        update_fields = {}
        if collection_key is not None:
            update_fields['collection_key'] = collection_key
        if min_children is not None:
            update_fields['min_children'] = min_children
        if max_children is not None:
            update_fields['max_children'] = max_children

        return NodeTypeComposition.objects.filter(id=comp_id).update(**update_fields)

"""
Service layer for NodeTypeComposition business logic.
"""

import uuid
from typing import Optional, Dict, Any

from django.db import transaction

from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.composition_repository import CompositionRepository
from ..constants import (
    ERR_COMPOSITION_NOT_FOUND,
    ERR_INVALID_PARENT_OR_CHILD_TYPE,
    ERR_COMPOSITION_ALREADY_EXISTS,
)


class CompositionService:
    """Service for managing composition relationships between node types."""

    def __init__(self):
        self.repository = AttributeDefRepository()
        self.composition_repository = CompositionRepository()

    @transaction.atomic
    def update_composition(
        self,
        comp_id: uuid.UUID,
        composition_model,
        payload: Dict[str, Any]
    ) -> bool:
        """
        Update a composition with the given payload.

        Args:
            comp_id: UUID of the composition to update
            composition_model: Model class (NodeTypeComposition)
            payload: Dictionary with fields to update (collection_key, min_children, max_children)

        Returns:
            True if update successful

        Raises:
            ValueError: If composition not found
        """
        comp = self.composition_repository.get_composition_by_id(comp_id, composition_model)
        if not comp:
            raise ValueError(ERR_COMPOSITION_NOT_FOUND)

        if "collection_key" in payload:
            comp.collection_key = str(payload["collection_key"]).strip()
        if "min_children" in payload:
            try:
                val = int(payload["min_children"])
                comp.min_children = val if val >= 0 else None
            except (TypeError, ValueError):
                comp.min_children = None
        if "max_children" in payload:
            try:
                val = int(payload["max_children"])
                comp.max_children = val if val >= 0 else None
            except (TypeError, ValueError):
                comp.max_children = None
        comp.save()
        return True

    @transaction.atomic
    def create_composition(
        self,
        parent_type_id: uuid.UUID,
        child_type_id: uuid.UUID,
        composition_model,
        collection_key: Optional[str] = None
    ) -> uuid.UUID:
        """
        Create a new composition relationship.

        Args:
            parent_type_id: UUID of parent NodeType
            child_type_id: UUID of child NodeType
            composition_model: Model class (NodeTypeComposition)
            collection_key: Optional collection key for the composition

        Returns:
            UUID of the created composition

        Raises:
            ValueError: If parent or child type not found, or composition already exists
        """
        parent_type = self.composition_repository.get_node_type_by_id(parent_type_id)
        child_type = self.composition_repository.get_node_type_by_id(child_type_id)

        if not parent_type or not child_type:
            raise ValueError(ERR_INVALID_PARENT_OR_CHILD_TYPE)

        if self.composition_repository.composition_exists(parent_type, child_type, composition_model):
            raise ValueError(ERR_COMPOSITION_ALREADY_EXISTS)

        comp = self.composition_repository.create_composition(
            parent_type, child_type, composition_model, collection_key
        )
        return comp.id

    @transaction.atomic
    def delete_composition(self, comp_id: uuid.UUID, composition_model) -> None:
        """
        Delete a composition by ID.

        Args:
            comp_id: UUID of the composition to delete
            composition_model: Model class (NodeTypeComposition)

        Raises:
            ValueError: If composition not found
        """
        comp = self.composition_repository.get_composition_by_id(comp_id, composition_model)
        if not comp:
            raise ValueError(ERR_COMPOSITION_NOT_FOUND)
        self.composition_repository.delete_composition(comp_id, composition_model)

    def get_composition(self, comp_id: uuid.UUID, composition_model) -> Optional[Dict[str, Any]]:
        """
        Get composition details by ID.

        Args:
            comp_id: UUID of the composition
            composition_model: Model class (NodeTypeComposition)

        Returns:
            Dictionary with composition details or None if not found
        """
        comp = self.composition_repository.get_composition_by_id_with_relations(comp_id, composition_model)
        if not comp:
            return None

        return {
            "id": str(comp.id),
            "parent_type_id": str(comp.parent_type_id),
            "child_type_id": str(comp.child_type_id),
            "parent_type": {"name": comp.parent_type.name, "label": comp.parent_type.label},
            "child_type": {"name": comp.child_type.name, "label": comp.child_type.label},
            "collection_key": comp.collection_key,
            "min_children": comp.min_children,
            "max_children": comp.max_children,
        }

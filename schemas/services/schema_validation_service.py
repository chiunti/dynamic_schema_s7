"""
Service for schema validation logic.
"""

import uuid
from typing import List, Dict, Any

from ..repositories.schema_repository import SchemaRepository
from ..repositories.attribute_def_repository import AttributeDefRepository


class SchemaValidationService:
    """Service for schema validation operations."""

    def __init__(self):
        self.schema_repository = SchemaRepository()
        self.attribute_def_repository = AttributeDefRepository()

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

            all_defs = self.attribute_def_repository.get_attribute_defs_by_node_type_required(node.node_type)
            existing_def_ids = set(
                na.attribute_def_id
                for na in self.schema_repository.get_node_attributes_by_node(node)
            )

            # Determine active variant
            type_def = self.schema_repository.get_attribute_def(node.node_type, 'type')
            current_variant = None
            if type_def and type_def.id in existing_def_ids:
                type_attr = self.schema_repository.get_node_attribute_by_node_attr_def(node, type_def)
                if type_attr:
                    current_variant = type_attr.value_string

            # Only check defs that apply to active variant
            applicable = [
                d for d in all_defs if d.variant_key is None or d.variant_key == current_variant
            ]

            missing = [
                d.json_key
                for d in applicable
                if d.id not in existing_def_ids
                and not d.data_type.name.startswith("natural_")
            ]

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

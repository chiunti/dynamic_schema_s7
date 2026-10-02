"""
Service for schema validation logic.
"""

import uuid
from typing import List, Dict, Any, Optional

from ..repositories.schema_repository import SchemaRepository
from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.node_type_repository import NodeTypeRepository


class SchemaValidationService:
    """Service for schema validation operations."""

    def __init__(self):
        self.schema_repository = SchemaRepository()
        self.attribute_def_repository = AttributeDefRepository()
        self.node_type_repository = NodeTypeRepository()

    def _read_discriminator_value(self, node, discriminator_key: str, existing_def_ids) -> Optional[str]:
        """Read the stored value of ``node``'s discriminator attribute."""
        disc_def = self.schema_repository.get_attribute_def(node.node_type, discriminator_key)
        if not disc_def or disc_def.id not in existing_def_ids:
            return None
        disc_attr = self.schema_repository.get_node_attribute_by_node_attr_def(node, disc_def)
        return disc_attr.value_string if disc_attr else None

    def _resolve_inherited_variant(self, node) -> Optional[str]:
        """Resolve the variant a props node inherits from its parent.

        A props node type declares NodeTypeVariant rows with
        discriminator_attr=None: it has no discriminator of its own, so the
        active variant is read from the parent node's declared discriminator.
        Returns None when there is no parent, no parent variant config, or no
        stored discriminator value.
        """
        parent = node.parent
        if parent is None:
            return None
        parent_ntv = self.node_type_repository.get_node_type_variant_by_node_type(parent.node_type)
        if parent_ntv is None or parent_ntv.discriminator_attr is None:
            return None
        parent_def_ids = set(
            na.attribute_def_id
            for na in self.schema_repository.get_node_attributes_by_node(parent)
        )
        return self._read_discriminator_value(parent, parent_ntv.discriminator_attr, parent_def_ids)

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

            # Determine active variant from the declarative discriminator.
            # A node type with no NodeTypeVariant rows has no variants at all.
            # A props node type (variant rows with discriminator_attr=None)
            # inherits its variant from the parent node's discriminator.
            ntv = self.node_type_repository.get_node_type_variant_by_node_type(node.node_type)
            current_variant = None
            if ntv is not None and ntv.discriminator_attr is not None:
                current_variant = self._read_discriminator_value(node, ntv.discriminator_attr, existing_def_ids)
            elif ntv is not None:
                current_variant = self._resolve_inherited_variant(node)

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

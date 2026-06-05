"""
Service for schema validation logic.
"""

import uuid
from typing import List, Dict, Any

from ..models import Node, NodeAttribute, AttributeDef


class SchemaValidationService:
    """Service for schema validation operations."""

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

            node = Node.objects.select_related("node_type").filter(id=nid).first()
            if not node:
                continue

            all_defs = list(
                AttributeDef.objects.select_related("data_type").filter(
                    node_type=node.node_type, is_required=True
                )
            )
            existing_def_ids = set(
                NodeAttribute.objects.filter(node=node).values_list(
                    "attribute_def_id", flat=True
                )
            )

            # Determine active variant
            type_def = next(
                (
                    d
                    for d in AttributeDef.objects.filter(
                        node_type=node.node_type, json_key="type", variant_key=None
                    )
                ),
                None,
            )
            current_variant = None
            if type_def and type_def.id in existing_def_ids:
                type_attr = NodeAttribute.objects.filter(
                    node=node, attribute_def=type_def
                ).first()
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

            for child_id in Node.objects.filter(parent_id=nid).values_list("id", flat=True):
                stack.append(child_id)

        return warnings

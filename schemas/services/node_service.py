import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import Node, NodeType
from ..repositories.schema_repository import NodeRepository, SchemaRepository
from ..repositories.composition_repository import CompositionRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.attribute_def_repository import AttributeDefRepository
from ..constants import (
    ERR_PROPERTIES_REQUIRED,
    ERR_PARENT_NOT_FOUND,
    ERR_NODE_TYPE_NOT_FOUND,
    ERR_COMPOSITION_NOT_ALLOWED,
    ERR_MAX_CHILDREN_VIOLATION,
    ERR_NOT_FOUND,
    ERR_NAME_REQUIRED,
    ERR_SCHEMA_KEY_VERSION_EXISTS,
    ERR_MIN_CHILDREN_VIOLATION,
)
from .schema_service import SchemaService
from .attribute_def_service import AttributeDefService
from .datatype_plugins import validate_datatype_value, get_storage_defaults


class NodeService:
    """Service for node-related business logic"""
    
    def __init__(self):
        self.node_repository = NodeRepository()
        self.schema_repository = SchemaRepository()
        self.composition_repository = CompositionRepository()
        self.node_type_repository = NodeTypeRepository()
        self.attribute_def_repository = AttributeDefRepository()
        self.attribute_def_service = AttributeDefService()
    
    def get_node_tree(self, root_id):
        """Get recursive tree of nodes"""
        return self.node_repository.get_node_tree(root_id)

    def get_node_with_parent(self, node_id: str | uuid.UUID) -> Node | None:
        """Return a node by ID with parent and node_type prefetched, or None."""
        return self.schema_repository.get_node_by_id_with_parent(node_id)

    def get_node_with_node_type(self, node_id: str | uuid.UUID) -> Node | None:
        """Return a node by ID with node_type prefetched, or None."""
        return self.schema_repository.get_node_by_id_with_node_type(node_id)

    def get_node_type(self, node_type_name: str) -> NodeType | None:
        """Return a node type by name, or None."""
        return self.node_type_repository.get_node_type_by_name(node_type_name)

    def get_node_detail(self, node: Node) -> dict:
        """Return the serialized detail payload for a node (editor side panel)."""
        children = list(self.schema_repository.get_children_by_parent(node))
        return {
            "id": node.id,
            "name": node.name,
            "sort_order": node.sort_order,
            "parent_id": node.parent_id,
            "node_type": node.node_type.name,
            "children": [
                {"id": str(c["id"]), "name": c["name"], "node_type": c["node_type__name"]}
                for c in children
            ],
        }

    def resolve_root_node_id(self, node_id=None, key: str | None = None, version: str | None = None) -> uuid.UUID | None:
        """Resolve a root node ID from an explicit node_id or a key+version pair."""
        if node_id:
            try:
                node = self.schema_repository.get_node_by_id(node_id)
                return node.id if node else None
            except (Node.DoesNotExist, ValidationError, ValueError):
                return None
        if not key or not version:
            return None
        root_node = self.schema_repository.get_root_node_by_key_version(key, version)
        return root_node.id if root_node else None

    def get_effective_variant_key(self, variant_key: str | None, parent_node_id=None) -> str | None:
        """
        Resolve the effective variant_key for attribute lookups.

        ``parent_node_id`` may refer either to a props child node (variant
        inherited from its parent via NodeTypeVariant.props_node_type) or to
        the semantic parent node itself — in the latter case the variant is
        read directly from that node's declared discriminator attribute
        (NodeTypeVariant.discriminator_attr). If the node cannot be resolved,
        the provided variant_key is kept.
        """
        if not parent_node_id:
            return variant_key
        node = self.schema_repository.get_node_by_id_with_node_type(parent_node_id)
        if node is None:
            return variant_key
        # Case 1: the node is itself a props child — infer from its parent.
        inferred = self.infer_variant_from_parent(node)
        if inferred:
            return inferred
        # Case 2: the node is the semantic parent — read its own discriminator.
        ntv = self.node_type_repository.get_node_type_variant_by_node_type(node.node_type)
        if ntv and ntv.discriminator_attr:
            disc_def = self.schema_repository.get_attribute_def(node.node_type, ntv.discriminator_attr)
            if disc_def:
                disc_attr = self.schema_repository.get_node_attribute_by_node_attr_def(node, disc_def)
                if disc_attr and disc_attr.value_string:
                    return disc_attr.value_string
        return variant_key

    def update_node_name(self, node_id, name) -> None:
        """Validate and update a node's display name."""
        cleaned = str(name or "").strip()
        if not cleaned:
            raise ValueError(ERR_NAME_REQUIRED)
        self.schema_repository.update_node_name(node_id, cleaned)

    @transaction.atomic
    def move_node(self, node_id, new_parent_id, new_position=None) -> None:
        """
        Move a node under a new parent, optionally at a given position.

        Validates that the target parent accepts children of the node's type,
        reassigns the parent, normalizes positions in both old and new parents,
        and inserts the node at the requested position (clamped to bounds).

        Raises LookupError if the node or the new parent does not exist.
        Raises ValueError if the composition is not allowed.
        """
        node = self.schema_repository.get_node_by_id_with_parent(node_id)
        if not node:
            raise LookupError(ERR_NOT_FOUND)

        new_parent = self.schema_repository.get_node_by_id_with_node_type(new_parent_id)
        if not new_parent:
            raise LookupError(ERR_PARENT_NOT_FOUND)

        if not self.schema_repository.composition_exists(new_parent.node_type, node.node_type):
            raise ValueError(ERR_COMPOSITION_NOT_ALLOWED)

        old_parent_id = node.parent_id
        self.schema_repository.update_node_parent(node.id, new_parent.id)
        self.schema_repository.normalize_positions(old_parent_id)
        self.schema_repository.normalize_positions(new_parent.id)

        if new_position is None:
            return
        try:
            pos = int(new_position)
        except (TypeError, ValueError):
            return
        siblings = list(self.schema_repository.get_children_by_parent_full(new_parent.id))
        pos = max(0, min(pos, max(0, len(siblings) - 1)))
        ordered = [n for n in siblings if n.id != node.id]
        ordered.insert(pos, self.schema_repository.get_node_by_id(node_id))
        for idx, n in enumerate(ordered):
            n.sort_order = idx
        self.schema_repository.bulk_update_nodes_sort_order(ordered)

    @transaction.atomic
    def reorder_node(self, node_id, direction: str) -> None:
        """
        Swap a node's sort_order with its previous ('up') or next ('down') sibling.

        Positions are normalized before and after the swap.

        Raises LookupError if the node does not exist or is not among its
        parent's children.
        """
        node = self.schema_repository.get_node_by_id_with_parent(node_id)
        if not node:
            raise LookupError(ERR_NOT_FOUND)

        parent_id = node.parent_id
        self.schema_repository.normalize_positions(parent_id)
        siblings = list(self.schema_repository.get_children_by_parent_full(parent_id))
        ids = [n.id for n in siblings]
        try:
            idx = ids.index(node.id)
        except ValueError:
            raise LookupError(ERR_NOT_FOUND)

        if direction == "up" and idx > 0:
            siblings[idx - 1].sort_order, siblings[idx].sort_order = siblings[idx].sort_order, siblings[idx - 1].sort_order
            self.schema_repository.bulk_update_nodes_sort_order([siblings[idx - 1], siblings[idx]])
        elif direction == "down" and idx < len(siblings) - 1:
            siblings[idx + 1].sort_order, siblings[idx].sort_order = siblings[idx].sort_order, siblings[idx + 1].sort_order
            self.schema_repository.bulk_update_nodes_sort_order([siblings[idx + 1], siblings[idx]])

        self.schema_repository.normalize_positions(parent_id)

    def infer_variant_from_parent(self, node) -> str:
        """
        Infer variant_key from parent's discriminator attribute for props nodes.
        
        For node types that are props_node_type (e.g., sdui_props), the variant_key
        is inherited from the parent's discriminator attribute (e.g., 'type' for sdui_widget).
        
        Args:
            node: Node instance

        Returns:
            Variant key string or None if not applicable
        """
        # Check if this node_type is a props_node_type
        if not self.node_type_repository.is_props_node_type(node.node_type):
            return None
        
        # Props nodes must have a parent
        if not node.parent:
            return None
        
        # Get the parent node
        parent = self.schema_repository.get_node_by_id_with_node_type(node.parent_id)
        if not parent:
            return None

        # Resolve the discriminator from the ACTUAL parent's variant config —
        # several parent types may share the same props_node_type with
        # different discriminators, so looking the variant up by
        # props_node_type alone would be ambiguous.
        parent_ntv = self.node_type_repository.get_node_type_variant_by_node_type(parent.node_type)
        if not parent_ntv:
            import logging
            logging.getLogger(__name__).warning(
                f"No NodeTypeVariant found for parent node type {parent.node_type.name} "
                f"of props node type {node.node_type.name}"
            )
            return None

        discriminator = parent_ntv.discriminator_attr
        if not discriminator:
            import logging
            logging.getLogger(__name__).warning(
                f"NodeTypeVariant for {parent.node_type.name} has no discriminator_attr configured"
            )
            return None

        # Get the parent's discriminator attribute definition
        parent_type_attr_def = self.schema_repository.get_attribute_def(parent.node_type, discriminator)
        if not parent_type_attr_def:
            return None
        
        # Get the parent's discriminator attribute value
        parent_type_attr = self.schema_repository.get_node_attribute_by_node_attr_def(parent, parent_type_attr_def)
        if not parent_type_attr:
            return None
        
        return parent_type_attr.value_string

    def _extract_discriminator_value(
        self,
        defs: list,
        existing: dict,
        discriminator: str
    ) -> str | None:
        """
        Extract the discriminator value from a node's attributes.

        First tries to find the discriminator AttributeDef with variant_key=None (common).
        If not found, falls back to any AttributeDef with the discriminator json_key.

        Args:
            defs: List of AttributeDefs for the node type
            existing: Dict mapping attribute_def_id to NodeAttribute
            discriminator: The discriminator attribute name (e.g., 'type')

        Returns:
            The discriminator value, or None if not found
        """
        # Try to find discriminator AttributeDef with variant_key=None (common)
        type_def = next((d for d in defs if d.json_key == discriminator and d.variant_key is None), None)
        if type_def:
            type_attr = existing.get(type_def.id)
            if type_attr and type_attr.value_string:
                return type_attr.value_string

        # Fallback: find any discriminator NodeAttribute regardless of variant_key scoping
        for d in defs:
            if d.json_key == discriminator:
                type_attr = existing.get(d.id)
                if type_attr and type_attr.value_string:
                    return type_attr.value_string
                break

        return None

    def _cleanup_obsolete_attributes(
        self,
        node: 'Node',
        variant_value: str
    ) -> int:
        """
        Clean up obsolete node attributes for a given variant.

        Deletes all NodeAttributes whose AttributeDef is not valid for the given variant.
        Valid AttributeDefs are:
        - Common properties (variant_key is None and is_common=True)
        - Properties specific to the variant (variant_key == variant_value)

        Args:
            node: Node instance
            variant_value: The variant value to keep attributes for

        Returns:
            Number of deleted attributes
        """
        # Get all AttributeDefs for this node type
        all_defs = self.schema_repository.get_attribute_defs_by_node_type(node.node_type)

        # Determine valid AttributeDefs for the variant
        valid_attr_def_ids = {
            d.id for d in all_defs
            if (d.variant_key is None and d.is_common) or d.variant_key == variant_value
        }

        # Get all existing NodeAttributes for this node
        existing_attrs = self.schema_repository.get_node_attributes_by_node(node)

        # Find obsolete NodeAttributes (those whose AttributeDef is not in valid_attr_def_ids)
        obsolete_attr_def_ids = {
            na.attribute_def_id for na in existing_attrs
            if na.attribute_def_id not in valid_attr_def_ids
        }

        # Delete obsolete NodeAttributes
        if obsolete_attr_def_ids:
            return self.schema_repository.delete_node_attributes_by_attr_defs(node, list(obsolete_attr_def_ids))
        return 0

    def _get_effective_variant_for_cleanup(
        self,
        node: 'Node',
        discriminator: str | None,
        current_variant: str | None
    ) -> str | None:
        """
        Determine which variant value should be used when cleaning up obsolete attributes.

        This method handles two distinct scenarios:
        1. Nodes with a discriminator attribute: The variant will be determined later
           from the update payload, so we return None here to defer cleanup.
        2. Props nodes (no discriminator): The variant is always inherited from the
           parent node's discriminator value, so we infer it from the parent.

        Args:
            node: The Node instance being updated
            discriminator: The discriminator attribute name for this node type,
                          or None if the node type uses parent-inferred variants
            current_variant: The currently active variant value (may be None)

        Returns:
            The variant key to use for attribute cleanup, or None if cleanup
            should be deferred until after discriminator updates are applied
        """
        if discriminator is not None:
            # Will be set from updates later
            return None
        elif node.parent:
            return self.infer_variant_from_parent(node)
        return None

    def update_node_properties(self, node, updates):
        """Update node properties with validation"""
        import logging
        logger = logging.getLogger(__name__)
        defs = self.schema_repository.get_attribute_defs_by_node_type(node.node_type)
        existing = {
            na.attribute_def_id: na
            for na in self.schema_repository.get_node_attributes_by_node(node)
        }

        if not isinstance(updates, dict):
            raise ValueError(ERR_PROPERTIES_REQUIRED)
        
        # Resolve current variant from the discriminator attribute (if present).
        # For props nodes, the variant is inherited from the parent's component type.
        current_variant = self.infer_variant_from_parent(node)

        # If not inherited, read discriminator attribute from the current node
        if current_variant is None:
            discriminator = self.node_type_repository.get_discriminator_attr(node.node_type)
            if discriminator:
                current_variant = self._extract_discriminator_value(defs, existing, discriminator)
        
        # Filter by variant_key and is_common: show universal common (NULL + is_common=True) + current variant only.
        # For variant-specific defs (variant_key=current_variant), is_common=False is allowed.
        # Catalog defs (variant_key=NULL + is_common=False) are templates and should not be shown.
        has_variant_defs = any(d.variant_key is not None for d in defs)
        if has_variant_defs:
            if current_variant:
                defs = [d for d in defs if (d.variant_key is None and d.is_common) or d.variant_key == current_variant]
            else:
                defs = [d for d in defs if d.variant_key is None and d.is_common]

        # Handle discriminator attribute separately if it exists for this node type
        discriminator = self.node_type_repository.get_discriminator_attr(node.node_type)
        previous_variant = current_variant  # Store variant value before update
        new_discriminator_value = None  # Track the new discriminator value

        if discriminator and discriminator in updates:
            discriminator_def = self.schema_repository.get_attribute_def(node.node_type, discriminator)
            if discriminator_def:
                new_discriminator_value = updates.pop(discriminator)
            else:
                # discriminator_def is None, cannot delete - log warning
                logger.warning(f"Discriminator attribute '{discriminator}' not found for node type {node.node_type.name}")

        # For props nodes (no discriminator), always use parent's current variant
        # to clean up obsolete properties from previous parent variants
        # Note: previous_variant comes from parent inference for props nodes
        if discriminator is None and previous_variant is not None:
            new_discriminator_value = self._get_effective_variant_for_cleanup(node, discriminator, previous_variant)
        
        defs_by_key = {d.json_key: d for d in defs}
        
        # Validate key+version uniqueness if editing these attributes in a root schema node
        if node.node_type.is_root and node.parent_id is None:
            new_key = updates.get('key')
            new_version = updates.get('version')
            
            if new_key is not None or new_version is not None:
                key_def = defs_by_key.get('key')
                
                current_key = None
                if key_def:
                    key_attr = existing.get(key_def.id)
                    if key_attr:
                        current_key = key_attr.value_string
                
                # Version is now a native column on schema_nodes
                current_version = node.version
                
                check_key = new_key if new_key is not None else current_key
                check_version = new_version if new_version is not None else current_version
                
                if check_key and check_version:
                    if not self.node_repository.check_key_version_unique(check_key, check_version, node.id):
                        raise ValueError(
                            ERR_SCHEMA_KEY_VERSION_EXISTS.format(key=check_key, version=check_version)
                        )
        
        with transaction.atomic():
            # Update discriminator attribute if it was in updates
            if discriminator and new_discriminator_value and discriminator_def:
                self.schema_repository.set_node_attribute_from_json(
                    node.id,
                    node.node_type.name,
                    discriminator_def.json_key,
                    new_discriminator_value,
                    discriminator_def.domain.domain_name if discriminator_def.domain_id else None
                )

            # If discriminator changed (or for props nodes, always clean up based on parent's current variant)
            # This must happen BEFORE processing other property updates
            should_cleanup = False
            if discriminator is not None:
                # For nodes with discriminator: clean up only if discriminator changed
                should_cleanup = new_discriminator_value != previous_variant
            else:
                # For props nodes (no discriminator): always clean up if we have parent variant
                should_cleanup = new_discriminator_value is not None

            if should_cleanup:
                # Clean up obsolete attributes for the current node
                deleted_count = self._cleanup_obsolete_attributes(node, new_discriminator_value)
                if deleted_count > 0:
                    logger.info(
                        f"Cleaning up {deleted_count} obsolete attributes for node {node.id} "
                        f"(variant changed from {previous_variant} to {new_discriminator_value})"
                    )

                # If this node has a discriminator that changed, also clean up props node children
                if discriminator is not None and new_discriminator_value != previous_variant:
                    children = self.schema_repository.get_children_by_parent_full(node.id)
                    for child in children:
                        # Only props node types (declared via NodeTypeVariant.props_node_type)
                        # inherit the parent's variant — a missing discriminator on a
                        # non-props child must NOT trigger variant cleanup.
                        if self.node_type_repository.is_props_node_type(child.node_type):
                            child_deleted_count = self._cleanup_obsolete_attributes(child, new_discriminator_value)
                            if child_deleted_count > 0:
                                logger.info(
                                    f"Cleaning up {child_deleted_count} obsolete attributes from props node child {child.id}"
                                )

            # Process all other property updates
            for json_key, value in updates.items():
                    d = defs_by_key.get(json_key)
                    if not d:
                        continue
                    
                    is_empty = value is None or value == "" or value == [] or value == {}
                    if is_empty:
                        self.schema_repository.delete_node_attributes(node, d)
                        continue
                    
                    domain_name = d.domain.domain_name if d.domain_id else None
                    
                    # Handle boolean null values
                    if d.data_type.name == 'bool' and value is None:
                        self.node_repository.insert_node_attribute_bool_null(node.id, d.id)
                        continue
                    
                    # natural_uuid is read-only (maps to PK) — never write
                    if d.data_type.name == 'natural_uuid':
                        continue

                    # natural_key maps to schema_nodes.key column — update directly
                    if d.data_type.name == 'natural_key':
                        node.key = str(value).strip() if value else node.key
                        self.schema_repository.update_node_key(node.id, node.key)
                        continue

                    # natural_version maps to schema_nodes.version column — update directly
                    # Also propagates to the root if called on a metadata child
                    if d.data_type.name == 'natural_version':
                        v = str(value).strip() if value else None
                        if v:
                            node.version = v
                            self.schema_repository.update_node_version(node.id, v)
                            if node.parent_id is not None:
                                self.schema_repository.update_node_version_by_parent(node.parent_id, v)
                        continue

                    # natural_order maps to schema_nodes.sort_order column — update directly
                    if d.data_type.name == 'natural_order':
                        node.sort_order = int(value) if value is not None else 0
                        self.schema_repository.update_node_sort_order(node.id, node.sort_order)
                        continue

                    # display_order maps to schema_nodes.sort_order column — update directly (1-based to 0-based)
                    if d.data_type.name == 'display_order':
                        node.sort_order = (int(value) - 1) if value is not None else 0
                        self.schema_repository.update_node_sort_order(node.id, node.sort_order)
                        continue

                    # Validate datatype-specific constraints through the plugin registry
                    validate_datatype_value(d.data_type, value)

                    # Write directly via ORM to avoid s7_ensure_domain_item adding domain items.
                    # Storage column is determined by DataType.primary_storage_type.
                    defaults = get_storage_defaults(d.data_type, value)
                    self.schema_repository.update_or_create_node_attribute(node.id, d, defaults=defaults)
    
    @transaction.atomic
    def create_node(self, parent_id, node_type_name, name=None, variant_key=None, key=None, collection_key=None):
        """Create a new node with automatic property assignment.

        Validates parent existence, node type, composition rules and
        max_children constraints, generates a default name when none is
        provided, and infers the variant key for props node types.
        """
        parent = self.schema_repository.get_node_by_id_with_node_type(parent_id)
        if not parent:
            raise ValueError(ERR_PARENT_NOT_FOUND)
        
        child_type = self.schema_repository.get_node_type_by_name(node_type_name)
        if not child_type:
            raise ValueError(ERR_NODE_TYPE_NOT_FOUND)

        # Filter composition by collection_key if provided
        if collection_key:
            composition = self.composition_repository.get_composition_by_parent_child_collection_key(
                parent.node_type, child_type, collection_key
            )
        else:
            composition = self.schema_repository.get_composition_by_parent_child(parent.node_type, child_type)
        
        if not composition:
            raise ValueError(ERR_COMPOSITION_NOT_ALLOWED)
        
        # For collection_key-based compositions, check if a child with that key already exists
        # Only apply this check for singleton slots (max_children=1)
        if composition.collection_key and composition.max_children == 1:
            existing = self.schema_repository.get_node_by_parent_type_key(parent, child_type, composition.collection_key)
            if existing:
                raise ValueError(f"A child with key '{composition.collection_key}' already exists")
        # For non-collection_key compositions or non-singleton compositions, use count-based check
        elif composition.max_children is not None:
            current_children_count = self.schema_repository.count_children_by_parent_and_type(parent, child_type)
            if current_children_count >= composition.max_children:
                raise ValueError(ERR_MAX_CHILDREN_VIOLATION)

        # Calculate next position
        siblings = self.schema_repository.get_siblings_by_parent(parent)
        next_pos = (siblings.first().sort_order + 1) if siblings.exists() else 0

        # Generate a default name when none is provided
        if name is None or str(name).strip() == "":
            name = f"{node_type_name}_{next_pos}"

        # Use provided key, or collection_key if available, or use default_json_key from NodeType, or use name as key
        node_key = key if key is not None else (composition.collection_key if composition.collection_key else (child_type.default_json_key if child_type.default_json_key else str(name)))

        node = self.schema_repository.create_node(
            parent=parent,
            node_type=child_type,
            sort_order=next_pos,
            name=str(name),
            key=node_key,
            project_id=parent.project_id,
            organization_id=parent.organization_id,
        )

        # For props node types without an explicit variant, infer the variant
        # key from the parent's discriminator attribute. infer_variant_from_parent
        # takes the CHILD (props) node — it reads node.parent's discriminator.
        if not variant_key and self.node_type_repository.is_props_node_type(child_type):
            variant_key = self.infer_variant_from_parent(node)

        # Determine the variant value declaratively:
        # 1. If variant_key is provided (or inferred for props node types), use it.
        # 2. Otherwise, if the composition's collection_key is itself a declared
        #    variant_key for the child type, use the collection_key — this covers
        #    slot names that double as component variants in seeded catalogs
        #    without any hardcoded node-type names.
        variant_value = variant_key
        if not variant_value and composition.collection_key:
            slot_variant = self.node_type_repository.get_node_type_variant_by_node_type_and_variant_key(
                child_type,
                composition.collection_key
            )
            if slot_variant:
                variant_value = composition.collection_key

        # Assign the discriminator attribute when a variant value was resolved
        if variant_value:
            ntv = self.node_type_repository.get_node_type_variant_by_node_type_and_variant_key(
                child_type,
                variant_value
            )

            if ntv is None:
                # No NodeTypeVariant configured — the catalog should declare one.
                import logging
                logging.getLogger(__name__).warning(
                    f"No NodeTypeVariant found for node_type {child_type.name} with variant_key {variant_value}"
                )
            elif ntv.discriminator_attr:
                attr_def = self.schema_repository.get_attribute_def(child_type, ntv.discriminator_attr)
                if attr_def:
                    self.schema_repository.set_node_attribute_from_json(
                        node.id,
                        child_type.name,
                        attr_def.json_key,
                        variant_value,
                        attr_def.domain.domain_name if attr_def.domain_id else None
                    )
            # discriminator_attr=None: inherited-variant props node type —
            # there is no own discriminator attribute to set.

        # Assign automatic properties for root nodes — delegate to SchemaService
        if child_type.is_root and node.parent_id is None:
            SchemaService().initialize_schema_attributes(node)

        # Auto-create minimum required children
        self._auto_create_min_children(node)
        
        return node
    
    def _auto_create_min_children(self, node):
        """Auto-create minimum required children based on min_children constraint"""
        child_compositions = self.schema_repository.get_compositions_by_parent_type(node.node_type, min_children_gt=0)

        for comp in child_compositions:
            if comp.min_children is None:
                continue
            current_count = self.schema_repository.count_children_by_parent_and_type(node, comp.child_type)
            if current_count < comp.min_children:
                for i in range(current_count, comp.min_children):
                    # For singleton slots (max_children=1), use collection_key as node key
                    if comp.max_children == 1 and comp.collection_key:
                        node_key = comp.collection_key
                        node_name = comp.collection_key
                    else:
                        node_key = f"{comp.collection_key or comp.child_type.name}_{i}" if comp.collection_key else f"{comp.child_type.name}_{i}"
                        node_name = node_key
                    child_node = self.schema_repository.create_node(
                        parent=node,
                        node_type=comp.child_type,
                        sort_order=i,
                        name=node_name,
                        key=node_key,
                        project_id=node.project_id,
                        organization_id=node.organization_id,
                    )
    
    def delete_node(self, node_id):
        """Delete node with validation of min_children constraints"""
        node = self.schema_repository.get_node_by_id_with_parent(node_id)
        if not node:
            raise ValueError(ERR_NOT_FOUND)
        
        # Check min_children constraint
        if node.parent:
            composition = self.schema_repository.get_composition_by_parent_child(node.parent.node_type, node.node_type)
            if composition and composition.min_children is not None:
                current_children_count = self.schema_repository.count_children_by_parent_and_type(node.parent, node.node_type)
                if current_children_count <= composition.min_children:
                    raise ValueError(
                        ERR_MIN_CHILDREN_VIOLATION.format(
                            min_children=composition.min_children,
                            node_type=node.node_type.name,
                            parent_type=node.parent.node_type.name
                        )
                    )
        
        with transaction.atomic():
            self.node_repository.delete_node_tree(node_id)
    
    def build_node_json(self, node_id):
        """Build JSON representation of a node tree"""
        jsonb_result = self.node_repository.build_node_json(node_id)
        if jsonb_result is None:
            raise ValueError(ERR_NOT_FOUND)
        return jsonb_result

    def get_allowed_children_with_variant_info(self, parent_node: Node) -> dict:
        """
        Get allowed children for a parent node with variant inference information.

        This method orchestrates the business logic for determining which child node types
        are allowed for a given parent, including validation of max_children constraints
        and identification of node types that inherit their variant from the parent.

        Args:
            parent_node: The parent Node instance

        Returns:
            Dictionary with keys:
                - allowed: List of allowed child node types with metadata
                - infer_variant_from_parent: List of node type names that inherit variant from parent
        """
        compositions = self.composition_repository.get_compositions_by_parent_type(parent_node.node_type)
        child_type_ids = [c.child_type_id for c in compositions]
        existing_counts = self.schema_repository.get_children_counts_by_parent_type_ids(parent_node, child_type_ids)

        # For collection_key-based compositions, check if a child with that key already exists
        # Build a set of existing keys (regardless of node_type) for singleton slots
        # Exclude keys that match collection_key of non-singleton compositions (e.g., 'children')
        non_singleton_collection_keys = {
            c.collection_key for c in compositions
            if c.collection_key and c.max_children != 1
        }
        existing_keys = self.schema_repository.get_children_keys_by_parent_type_ids(parent_node, child_type_ids) - non_singleton_collection_keys

        # Also build a map of collection_key to existing node for compositions
        # This handles the case where imported nodes have custom keys (e.g., 'home_body' instead of 'body')
        collection_key_to_node = {}
        for c in compositions:
            if c.collection_key and c.max_children == 1:
                # For singleton slots, check if there's a child of this type with this parent
                existing_child = self.schema_repository.get_node_by_parent_type(parent_node, c.child_type_id)
                if existing_child:
                    collection_key_to_node[c.collection_key] = existing_child

        # Build allowed list with validation
        allowed = []
        for c in compositions:
            max_c = c.max_children

            # For collection_key-based compositions:
            # 1. Check if a child with that collection_key already exists (singleton)
            # 2. If NOT, check max_children for that specific collection_key
            if c.collection_key:
                # For singleton slots (max_children=1), check if slot is already occupied
                if max_c == 1 and c.collection_key in collection_key_to_node:
                    # A child with this collection_key already exists (singleton slot)
                    continue
                # Check max_children for this specific collection_key
                if max_c is not None and existing_counts.get(c.child_type_id, 0) >= max_c:
                    continue
            # For non-collection_key compositions: check max_children by node_type
            elif max_c is not None and existing_counts.get(c.child_type_id, 0) >= max_c:
                continue

            allowed.append({
                "node_type": c.child_type.name,
                "label": c.child_type.label,
                "collection_key": c.collection_key,
                "min_children": c.min_children,
                "max_children": max_c,
            })

        # Check if any allowed child type should infer variant from parent
        # (e.g., sdui_props inherits variant from parent's component type)
        infer_variant_from_parent = []
        for item in allowed:
            child_type = self.node_type_repository.get_node_type_by_name(item["node_type"])
            if child_type:
                # Check if this node type is a props_node_type (variant inherited from parent via props_node_type)
                is_props_type = self.node_type_repository.is_props_node_type(child_type)
                if is_props_type:
                    infer_variant_from_parent.append(item["node_type"])

        return {
            "parent_id": parent_node.id,
            "parent_type": parent_node.node_type.name,
            "allowed": allowed,
            "infer_variant_from_parent": infer_variant_from_parent
        }

    def get_node_properties_with_variant_filtering(self, node: Node) -> dict:
        """
        Get node properties with variant-based filtering and variant options.

        This method orchestrates the business logic for retrieving and filtering
        node attributes based on the current variant, including resolving the
        variant from the parent (for props nodes) or from the node's discriminator
        attribute.

        Args:
            node: The Node instance

        Returns:
            Dictionary with keys:
                - properties: List of attribute definitions with current values
                - variant_options: List of variant options (if applicable)
                - current_variant: The current variant key
        """
        all_defs = self.attribute_def_repository.get_attribute_defs_by_node_type(node.node_type)
        node_attributes = self.schema_repository.get_node_attributes_by_node(node)
        existing = {na.attribute_def_id: na for na in node_attributes}

        # Resolve current variant from the discriminator attribute (if present).
        # For props nodes, the variant is inherited from the parent's component type.
        current_variant = self.infer_variant_from_parent(node)

        # If not inherited, read discriminator attribute from the current node
        if current_variant is None:
            discriminator = self.node_type_repository.get_discriminator_attr(node.node_type)
            if discriminator:
                type_def = next((d for d in all_defs if d.json_key == discriminator and d.variant_key is None), None)
                if type_def:
                    type_attr = existing.get(type_def.id)
                    if type_attr and type_attr.value_string:
                        current_variant = type_attr.value_string
                if current_variant is None:
                    # Fallback: find any discriminator NodeAttribute regardless of variant_key scoping
                    for d in all_defs:
                        if d.json_key == discriminator:
                            type_attr = existing.get(d.id)
                            if type_attr and type_attr.value_string:
                                current_variant = type_attr.value_string
                            break

        # Filter by variant_key and is_common: show universal common (NULL + is_common=True) + current variant only.
        # For variant-specific defs (variant_key=current_variant), is_common=False is allowed.
        # Catalog defs (variant_key=NULL + is_common=False) are templates and should not be shown.
        # If the node type has variant-scoped defs but no variant is selected yet,
        # show only the universal common defs so the user sets 'type' first.
        has_variant_defs = any(d.variant_key is not None for d in all_defs)
        
        if has_variant_defs:
            if current_variant:
                defs = [d for d in all_defs if (d.variant_key is None and d.is_common) or d.variant_key == current_variant]
            else:
                defs = [d for d in all_defs if d.variant_key is None and d.is_common]
        else:
            defs = all_defs

        # Filter out json_keys that correspond to structural child nodes
        # (compositions without collection_key, e.g. a props or layout child).
        # A json_key is structural when it equals the child type's declared
        # NodeType.default_json_key or the child type's own name — declarative
        # catalog data, never name-mangling.
        structural_keys = set()
        for child_type in self.composition_repository.get_structural_child_types(node.node_type):
            structural_keys.add(child_type.name)
            if child_type.default_json_key:
                structural_keys.add(child_type.default_json_key)

        defs = [d for d in defs if d.json_key not in structural_keys]

        # Load domain items for all domains used by the filtered defs
        domain_ids = [d.domain_id for d in defs if d.domain_id]
        domain_items = self.attribute_def_repository.get_domain_items_by_domain_ids(domain_ids)
        items_by_domain = {}
        for di in domain_items:
            items_by_domain.setdefault(di.domain_id, []).append({
                "value": di.value,
                "label": di.label,
            })

        # Build properties list with special handling for natural_* data types
        props = []
        for d in defs:
            na = existing.get(d.id)
            dt_name = d.data_type.name

            if dt_name == 'natural_uuid':
                prop_value = {"value_string": str(node.id), "value_number": None, "value_bool": None, "value_json": None}
                prop_has_value = True
            elif dt_name == 'natural_key':
                prop_value = {"value_string": node.key, "value_number": None, "value_bool": None, "value_json": None}
                prop_has_value = bool(node.key)
            elif dt_name == 'natural_version':
                prop_value = {"value_string": node.version, "value_number": None, "value_bool": None, "value_json": None}
                prop_has_value = bool(node.version)
            elif dt_name == 'natural_order':
                prop_value = {"value_string": str(node.sort_order), "value_number": None, "value_bool": None, "value_json": None}
                prop_has_value = True
            elif dt_name == 'display_order':
                prop_value = {"value_string": str(node.sort_order + 1), "value_number": None, "value_bool": None, "value_json": None}
                prop_has_value = True
            else:
                prop_value = {
                    "value_string": na.value_string if na else None,
                    "value_number": float(na.value_number) if (na and na.value_number is not None) else None,
                    "value_bool": na.value_bool if na else None,
                    "value_json": na.value_json if na else None,
                }
                prop_has_value = bool(na)

            props.append({
                "attribute_def_id": d.id,
                "json_key": d.json_key,
                "label": d.name,
                "data_type": dt_name,
                "editor_extension": d.data_type.editor_extension,
                "is_required": d.is_required,
                "variant_key": d.variant_key,
                "domain": d.domain.domain_name if d.domain_id else None,
                "domain_items": items_by_domain.get(d.domain_id, []) if d.domain_id else [],
                "value": prop_value,
                "has_value": prop_has_value,
                "group": d.group,
            })

        response_data = {
            "node_id": node.id,
            "node_type": node.node_type.name,
            "current_variant": current_variant,
            "properties": props,
        }

        # Populate variant selector options for nodes that have variants.
        # Declarative suppression rules:
        #  - props node types inherit the variant from the parent -> no selector
        #  - node types declaring a single variant have nothing to choose
        #    (the variant is fixed by the catalog) -> no selector
        if has_variant_defs:
            is_props_type = self.node_type_repository.is_props_node_type(node.node_type)
            if not is_props_type:
                variants = self.node_type_repository.get_variant_keys_by_node_type(node.node_type)
                if len(variants) > 1:
                    options = [{"value": v, "label": v} for v in variants]
                    response_data["variant_options"] = options

        return response_data

    def get_node_type_variants_with_props_check(self, node_type_name: str) -> dict:
        """
        Get variant options for a given node type, with special handling for props nodes.

        For props nodes (variant inherited from parent), returns empty options since
        the variant should be inferred from the parent node.

        Args:
            node_type_name: The name of the node type

        Returns:
            Dictionary with key "options" containing list of variant options
        """
        node_type = self.node_type_repository.get_node_type_by_name(node_type_name)
        if not node_type:
            raise ValueError(ERR_NODE_TYPE_NOT_FOUND)

        # For props nodes (variant inherited from parent), return empty options
        is_props_type = self.node_type_repository.is_props_node_type(node_type)
        if is_props_type:
            return {"options": []}

        variants = self.attribute_def_service.get_variants_for_node_type(str(node_type.id))
        options = [{"value": v, "label": v} for v in variants]
        return {"options": options}

from django.db import transaction

from ..repositories.schema_repository import NodeRepository, SchemaRepository
from ..repositories.composition_repository import CompositionRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..utils import normalize_variant_key
from ..constants import (
    ERR_PROPERTIES_REQUIRED,
    ERR_PARENT_NOT_FOUND,
    ERR_NODE_TYPE_NOT_FOUND,
    ERR_COMPOSITION_NOT_ALLOWED,
    ERR_MAX_CHILDREN_VIOLATION,
    ERR_NOT_FOUND,
    ERR_SCHEMA_KEY_VERSION_EXISTS,
    ERR_MIN_CHILDREN_VIOLATION,
)
from .schema_service import SchemaService


class NodeService:
    """Service for node-related business logic"""
    
    def __init__(self):
        self.node_repository = NodeRepository()
        self.schema_repository = SchemaRepository()
        self.composition_repository = CompositionRepository()
        self.node_type_repository = NodeTypeRepository()
    
    def get_node_tree(self, root_id):
        """Get recursive tree of nodes"""
        return self.node_repository.get_node_tree(root_id)
    
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

        # Get the NodeTypeVariant configuration to find the discriminator attribute
        ntv = self.node_type_repository.get_node_type_variant_by_props_node_type(node.node_type)
        if not ntv:
            # This should not happen in a properly configured system
            # All props node types should have NodeTypeVariant configured
            import logging
            logging.warning(f"No NodeTypeVariant found for props node type {node.node_type.name}")
            return None

        discriminator = ntv.discriminator_attr
        if not discriminator:
            import logging
            logging.warning(f"NodeTypeVariant for {node.node_type.name} has no discriminator_attr configured")
            return None

        # Get the parent's discriminator attribute definition
        parent_type_attr_def = self.schema_repository.get_attribute_def(parent.node_type, discriminator)
        if not parent_type_attr_def:
            return None
        
        # Get the parent's discriminator attribute value
        parent_type_attr = self.schema_repository.get_node_attribute_by_node_attr_def(parent, parent_type_attr_def)
        if not parent_type_attr:
            return None
        
        # Normalize to camelCase to match SDUI_PROPS_DEFS variant_keys
        from ..utils import snake_to_camel
        return snake_to_camel(parent_type_attr.value_string)
    
    def update_node_properties(self, node, updates):
        """Update node properties with validation"""
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
                type_def = next((d for d in defs if d.json_key == discriminator and d.variant_key is None), None)
                if type_def:
                    type_attr = existing.get(type_def.id)
                    if type_attr and type_attr.value_string:
                        current_variant = type_attr.value_string
                if current_variant is None:
                    # Fallback: find any discriminator NodeAttribute regardless of variant_key scoping
                    for d in defs:
                        if d.json_key == discriminator:
                            type_attr = existing.get(d.id)
                            if type_attr and type_attr.value_string:
                                current_variant = type_attr.value_string
                            break
        
        # Filter by variant_key and is_common: show universal common (NULL + is_common=True) + current variant only.
        # For variant-specific defs (variant_key=current_variant), is_common=False is allowed.
        # Catalog defs (variant_key=NULL + is_common=False) are templates and should not be shown.
        has_variant_defs = any(d.variant_key is not None for d in defs)
        if has_variant_defs:
            normalized_current_variant = normalize_variant_key(current_variant) if current_variant else None
            if normalized_current_variant:
                defs = [d for d in defs if (d.variant_key is None and d.is_common) or normalize_variant_key(d.variant_key) == normalized_current_variant]
            else:
                defs = [d for d in defs if d.variant_key is None and d.is_common]
        else:
            defs = defs
        
        # Handle discriminator attribute separately if it exists for this node type
        discriminator = self.node_type_repository.get_discriminator_attr(node.node_type)
        if discriminator and discriminator in updates:
            discriminator_def = self.schema_repository.get_attribute_def(node.node_type, discriminator)
            if discriminator_def:
                discriminator_value = updates.pop(discriminator)
                if discriminator_value:
                    self.schema_repository.set_node_attribute_from_json(
                        node.id,
                        node.node_type.name,
                        discriminator_def.json_key,
                        discriminator_value,
                        discriminator_def.domain.domain_name if discriminator_def.domain_id else None
                    )
            else:
                self.schema_repository.delete_node_attributes(node, discriminator_def)
        
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

                    # Write directly via ORM to avoid s7_ensure_domain_item adding domain items
                    if d.data_type.name in ('string', 'date', 'color', 'uuid', 'auto_uuid'):
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": str(value), "value_number": None, "value_bool": None, "value_json": None},
                        )
                    elif d.data_type.name in ('number', 'int', 'float'):
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": None, "value_number": value, "value_bool": None, "value_json": None},
                        )
                    elif d.data_type.name == 'bool':
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": None, "value_number": None, "value_bool": bool(value), "value_json": None},
                        )
                    elif d.data_type.name == 'json':
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                        )
                    elif d.data_type.name == 'conditional':
                        # Conditional datatype stores as JSON in value_json
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                        )
                    elif d.data_type.name in ('int_tuple', 'list_string', 'list_int', 'dict'):
                        # All these types store as JSON in value_json
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                        )
                    elif d.data_type.name == 'domain_list':
                        # domain_list stores array of domain item values as JSON
                        self.schema_repository.update_or_create_node_attribute(
                            node.id, d,
                            defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                        )
                    else:
                        pass
    
    def create_node(self, parent_id, node_type_name, name, variant_key=None, key=None, collection_key=None):
        """Create a new node with automatic property assignment"""
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

        # Use provided key, or collection_key if available, or use default_json_key from NodeType, or use name as key
        node_key = key if key is not None else (composition.collection_key if composition.collection_key else (child_type.default_json_key if child_type.default_json_key else str(name)))

        node = self.schema_repository.create_node(
            parent=parent,
            node_type=child_type,
            sort_order=next_pos,
            name=str(name),
            key=node_key,
        )

        # Determine the variant value:
        # 1. If variant_key is provided, use it
        # 2. If collection_key is present and no variant_key, use collection_key as the variant value
        variant_value = variant_key
        if not variant_value and composition.collection_key:
            variant_value = composition.collection_key

        # Assign discriminator attribute when variant_value is provided
        # For screen nodes, collection_key is the slot (body, appbar, etc.), not the component type
        # Special handling for sdui_container and sdui_widget when created with collection_key
        if composition.collection_key and not variant_key:
            # For screen slots: use NodeTypeVariant to determine the default type
            # Only apply this when no explicit variant_key is provided
            if child_type.name == 'sdui_container':
                # Try to find a NodeTypeVariant for sdui_container with variant_key matching collection_key
                ntv = self.node_type_repository.get_node_type_variant_by_node_type_and_variant_key(
                    child_type,
                    composition.collection_key
                )
                if ntv and ntv.discriminator_attr:
                    discriminator_attr = ntv.discriminator_attr
                    attr_def = self.schema_repository.get_attribute_def(child_type, discriminator_attr)
                    if attr_def:
                        self.schema_repository.set_node_attribute_from_json(
                            node.id,
                            child_type.name,
                            attr_def.json_key,
                            composition.collection_key,
                            attr_def.domain.domain_name if attr_def.domain_id else None
                        )
            # For sdui_widget, type should remain null (not set) when no variant_key is provided
        elif variant_value:
            # For types that use discriminator attributes (e.g., field with 'type')
            # Try to find the discriminator attribute from NodeTypeVariant
            ntv = self.node_type_repository.get_node_type_variant_by_node_type_and_variant_key(
                child_type,
                variant_value
            )
            
            if ntv and ntv.discriminator_attr:
                discriminator_attr = ntv.discriminator_attr
                attr_def = self.schema_repository.get_attribute_def(child_type, discriminator_attr)
                if attr_def:
                    self.schema_repository.set_node_attribute_from_json(
                        node.id,
                        child_type.name,
                        attr_def.json_key,
                        variant_value,
                        attr_def.domain.domain_name if attr_def.domain_id else None
                    )
            else:
                # No NodeTypeVariant configured - this should not happen in a properly configured system
                import logging
                logging.warning(f"No NodeTypeVariant found for node_type {child_type.name} with variant_key {variant_value}")
                # Do not set any attribute - the system should be configured with NodeTypeVariant

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

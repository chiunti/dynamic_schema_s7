import json

from django.db import transaction

from ..repositories.schema_repository import NodeRepository, SchemaRepository
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
    
    def get_node_tree(self, root_id):
        """Get recursive tree of nodes"""
        return self.node_repository.get_node_tree(root_id)
    
    def update_node_properties(self, node, updates):
        """Update node properties with validation"""
        defs = self.schema_repository.get_attribute_defs_by_node_type(node.node_type)
        existing = {
            na.attribute_def_id: na
            for na in self.schema_repository.get_node_attributes_by_node(node)
        }
        
        if not isinstance(updates, dict):
            raise ValueError(ERR_PROPERTIES_REQUIRED)
        
        # Handle 'type' attribute separately if it exists for this node type
        type_def = self.schema_repository.get_attribute_def(node.node_type, 'type')
        if type_def and 'type' in updates:
            type_value = updates.pop('type')
            if type_value:
                self.schema_repository.set_node_attribute_from_json(
                    node.id, 
                    node.node_type.name, 
                    type_def.json_key, 
                    type_value,
                    type_def.domain.domain_name if type_def.domain_id else None
                )
            else:
                self.schema_repository.delete_node_attributes(node, type_def)
        
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
                    node.save(update_fields=['key'])
                    continue

                # natural_version maps to schema_nodes.version column — update directly
                # Also propagates to the root if called on a metadata child
                if d.data_type.name == 'natural_version':
                    v = str(value).strip() if value else None
                    if v:
                        node.version = v
                        node.save(update_fields=['version'])
                        if node.parent_id is not None:
                            self.schema_repository.update_node_version_by_parent(node.parent_id, v)
                    continue

                # natural_order maps to schema_nodes.sort_order column — update directly
                if d.data_type.name == 'natural_order':
                    node.sort_order = int(value) if value is not None else 0
                    node.save(update_fields=['sort_order'])
                    continue

                # display_order maps to schema_nodes.sort_order column — update directly (1-based to 0-based)
                if d.data_type.name == 'display_order':
                    node.sort_order = (int(value) - 1) if value is not None else 0
                    node.save(update_fields=['sort_order'])
                    continue

                # Write directly via ORM to avoid s7_ensure_domain_item adding domain items
                if d.data_type.name in ('string', 'date', 'color', 'uuid', 'auto_uuid'):
                    self.schema_repository.update_or_create_node_attribute(
                        node, d,
                        defaults={"value_string": str(value), "value_number": None, "value_bool": None, "value_json": None},
                    )
                elif d.data_type.name in ('number', 'int', 'float'):
                    self.schema_repository.update_or_create_node_attribute(
                        node, d,
                        defaults={"value_string": None, "value_number": value, "value_bool": None, "value_json": None},
                    )
                elif d.data_type.name == 'bool':
                    self.schema_repository.update_or_create_node_attribute(
                        node, d,
                        defaults={"value_string": None, "value_number": None, "value_bool": bool(value), "value_json": None},
                    )
                elif d.data_type.name == 'json':
                    self.schema_repository.update_or_create_node_attribute(
                        node, d,
                        defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                    )
                elif d.data_type.name in ('int_tuple', 'list_string', 'list_int', 'dict'):
                    # All these types store as JSON in value_json
                    self.schema_repository.update_or_create_node_attribute(
                        node, d,
                        defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                    )
                elif d.data_type.name == 'domain_list':
                    # domain_list stores array of domain item values as JSON
                    self.schema_repository.update_or_create_node_attribute(
                        node, d,
                        defaults={"value_string": None, "value_number": None, "value_bool": None, "value_json": value},
                    )
    
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
            from schemas.models import NodeTypeComposition
            composition = NodeTypeComposition.objects.filter(
                parent_type=parent.node_type,
                child_type=child_type,
                collection_key=collection_key
            ).first()
        else:
            composition = self.schema_repository.get_composition_by_parent_child(parent.node_type, child_type)
        
        if not composition:
            raise ValueError(ERR_COMPOSITION_NOT_ALLOWED)
        
        # For collection_key-based compositions, check if a child with that key already exists
        if composition.collection_key:
            existing = self.schema_repository.get_node_by_parent_type_key(parent, child_type, composition.collection_key)
            if existing:
                raise ValueError(f"A child with key '{composition.collection_key}' already exists")
        # For non-collection_key compositions, use count-based check
        elif composition.max_children is not None:
            current_children_count = self.schema_repository.count_children_by_parent_and_type(parent, child_type)
            if current_children_count >= composition.max_children:
                raise ValueError(ERR_MAX_CHILDREN_VIOLATION)

        # Calculate next position
        siblings = self.schema_repository.get_siblings_by_parent(parent)
        next_pos = (siblings.first().sort_order + 1) if siblings.exists() else 0
        
        # Use provided key, or collection_key if available, or None
        node_key = key if key is not None else (composition.collection_key if composition.collection_key else None)
        
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
        if composition.collection_key:
            # For screen slots: sdui_container should have type="container", sdui_widget should have type=null
            if child_type.name == 'sdui_container':
                type_def = self.schema_repository.get_attribute_def(child_type, 'type')
                if type_def:
                    self.schema_repository.set_node_attribute_from_json(
                        node.id,
                        child_type.name,
                        type_def.json_key,
                        'container',
                        type_def.domain.domain_name if type_def.domain_id else None
                    )
            # For sdui_widget, type should remain null (not set)
        elif variant_value:
            # For types that use discriminator attributes (e.g., field with 'type')
            # Try to find the discriminator attribute from NodeTypeVariant
            from schemas.models import NodeTypeVariant
            ntv = NodeTypeVariant.objects.filter(
                node_type=child_type,
                variant_key=variant_value
            ).first()
            
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
                # Fallback to 'type' attribute for backward compatibility
                type_def = self.schema_repository.get_attribute_def(child_type, 'type')
                if type_def:
                    self.schema_repository.set_node_attribute_from_json(
                        node.id,
                        child_type.name,
                        type_def.json_key,
                        variant_value,
                        type_def.domain.domain_name if type_def.domain_id else None
                    )

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

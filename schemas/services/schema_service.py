import uuid
from typing import Optional

from django.db import DatabaseError, transaction

from ..models import Node
from ..repositories.schema_repository import SchemaRepository
from .permission_service import PermissionService
from ..repositories.project_repository import ProjectRepository
from .conditional_validator import validate_conditional_structure
from ..constants import (
    ERR_VERSION_NOT_SET,
    ERR_STATUS_ATTRIBUTE_NOT_FOUND,
    ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND,
    ERR_VERSION_NOT_AVAILABLE,
    ERR_PERMISSION_DENIED,
    ERR_DATABASE_ERROR_ARCHIVE,
    ERR_UNEXPECTED_ERROR_ARCHIVE,
    ERR_DATABASE_ERROR_DRAFT,
    ERR_UNEXPECTED_ERROR_DRAFT,
    ERR_DATABASE_ERROR_BUILD,
    ERR_UNEXPECTED_ERROR_BUILD,
    ERR_DATABASE_ERROR_CACHE_REBUILD,
    ERR_UNEXPECTED_ERROR_CACHE_REBUILD,
    ERR_PUBLISHED_STATUS_NOT_AVAILABLE,
    ERR_ARCHIVED_STATUS_NOT_AVAILABLE,
    ERR_DRAFT_STATUS_NOT_AVAILABLE,
    ERR_SCHEMA_NOT_IN_DRAFT_STATUS,
    ERR_SCHEMA_NOT_PUBLISHED_STATUS,
    ERR_SCHEMA_NOT_ARCHIVED_STATUS,
    MAX_VERSION_AUTO_INCREMENT_ATTEMPTS,
    SCHEMA_KEY_SUFFIX,
    SCHEMA_METADATA_SUFFIX,
)


class SchemaService:
    """Service for schema-related business logic"""
    
    def __init__(self):
        self.repository = SchemaRepository()
    
    def import_schema(
        self,
        validated_schema: dict,
        schema_key: str,
        schema_version: Optional[str],
        schema_status: str,
        overwrite: bool,
        project_id: Optional[uuid.UUID] = None,
        organization_id: Optional[uuid.UUID] = None,
        user=None,
    ) -> uuid.UUID:
        """Import a schema with validation"""
        if project_id and user:
            perm = PermissionService()
            project = ProjectRepository().get_project_by_id(project_id)
            if project and project.organization_id:
                if not perm.can_edit_organization(user, project.organization_id):
                    raise PermissionError(ERR_PERMISSION_DENIED)
        
        # Track if schema_version was explicitly specified by user
        # Empty string or None means user didn't specify a version
        form_version_is_explicit = schema_version is not None and schema_version != ""
        
        # Import the root node using s7_import_schema
        # If schema_version is None, repository will use "1" as fallback
        schema_id = self.repository.import_schema(
            validated_schema,
            schema_key,
            schema_version,
            schema_status,
            overwrite,
            project_id=project_id,
            organization_id=organization_id,
        )
        
        # Process the complete JSON structure to create child nodes
        self._process_schema_structure(validated_schema, schema_id, project_id, organization_id)
        
        # Sync Node.version from child nodes that have natural_version attributes
        # This allows schemas like 'screen' to define their version in metadata.version
        version_warning = self._sync_version_from_metadata(schema_id, validated_schema, schema_version, form_version_is_explicit)
        
        # Ensure key and status are set as attributes on the root node
        self._set_schema_metadata(schema_id, schema_key, schema_status)
        
        return schema_id, version_warning
    
    def _sync_version_from_metadata(self, schema_id: uuid.UUID, validated_schema: dict, form_version: Optional[str], form_version_is_explicit: bool) -> dict:
        """Sync Node.version from child nodes that have natural_version attributes.
        
        This allows schemas to define their version in a child node (e.g., metadata.version)
        instead of the root level. The logic is schema-driven:
        1. Find child compositions of the root node type
        2. Check if any child type has a 'version' attribute with natural_version datatype
        3. If the JSON has that child with a version value, update Node.version
        
        Priority:
        - If user specified version in form: use form version (warn if JSON differs)
        - If form is empty/default: use JSON version
        
        Args:
            schema_id: Root node ID
            validated_schema: The complete JSON schema
            form_version: Version from the import form (may be None)
            form_version_is_explicit: True if user explicitly specified version in form
            
        Returns:
            dict with warning information if versions mismatched, empty dict otherwise
        """
        from schemas.models import Node, NodeTypeComposition, AttributeDef, DataType
        import logging
        
        logger = logging.getLogger(__name__)
        
        root_node = Node.objects.get(id=schema_id)
        root_type = root_node.node_type
        
        # Get all child compositions of the root node type
        compositions = NodeTypeComposition.objects.filter(parent_type=root_type)
        
        for comp in compositions:
            child_type = comp.child_type
            
            # Check if this child type has a 'version' attribute with natural_version datatype
            version_attr = AttributeDef.objects.filter(
                node_type=child_type,
                json_key='version',
                data_type__name='natural_version'
            ).first()
            
            if not version_attr:
                continue
            
            # Get the collection_key to find the child in the JSON
            collection_key = comp.collection_key
            
            # Extract the root object from the JSON
            root_key = next(iter(validated_schema.keys())) if validated_schema else None
            if not root_key:
                continue
            
            root_obj = validated_schema[root_key] if isinstance(validated_schema[root_key], dict) else {}
            
            # Find the child in the JSON by collection_key
            child_obj = None
            if collection_key and collection_key in root_obj:
                child_obj = root_obj[collection_key]
            elif isinstance(root_obj, dict):
                # Try to find the child by type name as fallback
                child_obj = root_obj.get(child_type.name)
            
            if not child_obj or not isinstance(child_obj, dict):
                continue
            
            # Extract version from the child object
            json_version = child_obj.get('version')
            
            if not json_version:
                continue
            
            # Get the actual form version (may be empty string if not specified)
            actual_form_version = form_version if form_version else "1"
            
            # Priority: form version > JSON version
            if json_version != actual_form_version:
                # If user didn't explicitly specify a version, use JSON version without warning
                if not form_version_is_explicit:
                    # User didn't specify a version, use JSON version
                    self.repository.update_node_fields(schema_id, version=json_version)
                    # Find and update the metadata child node as well
                    metadata_node = Node.objects.filter(
                        parent=schema_id,
                        node_type=child_type
                    ).first()
                    if metadata_node:
                        self.repository.update_node_fields(metadata_node.id, version=json_version)
                    return {}  # No warning, this is expected behavior
                
                # User specified a version in the form that differs from JSON
                # Log warning but respect user's choice
                warning_msg = (
                    f"Version mismatch for schema {root_node.key}: "
                    f"form version='{actual_form_version}', JSON version='{json_version}'. "
                    f"Using form version as specified by user."
                )
                logger.warning(warning_msg)
                
                # Update both root and metadata node to form version for consistency
                self.repository.update_node_fields(schema_id, version=actual_form_version)
                # Find and update the metadata child node as well
                metadata_node = Node.objects.filter(
                    parent=schema_id,
                    node_type=child_type
                ).first()
                if metadata_node:
                    self.repository.update_node_fields(metadata_node.id, version=actual_form_version)
                
                return {
                    'type': 'version_mismatch',
                    'message': warning_msg,
                    'form_version': actual_form_version,
                    'json_version': json_version
                }
            
            # Form version matches JSON version or form was empty/default
            # Sync from JSON to ensure consistency
            self.repository.update_node_fields(schema_id, version=json_version)
            return {}  # No warning
    
    def _set_schema_metadata(self, schema_id: uuid.UUID, schema_key: str, schema_status: str):
        """Set key and status as attributes on the root node"""
        from schemas.models import Node, AttributeDef, DataType, NodeAttribute
        from schemas.repositories.node_type_repository import NodeTypeRepository
        
        root_node = Node.objects.get(id=schema_id)
        root_node_type = root_node.node_type
        
        # Get AttributeDefs for key and status
        key_attr_def = AttributeDef.objects.filter(
            node_type=root_node_type,
            json_key='key',
            variant_key__isnull=True
        ).first()
        
        status_attr_def = AttributeDef.objects.filter(
            node_type=root_node_type,
            json_key='status',
            variant_key__isnull=True
        ).first()
        
        # Set key attribute — create AttributeDef dynamically if the node type lacks one
        if not key_attr_def:
            string_type = DataType.objects.filter(name='string').first()
            if string_type:
                key_attr_def, _ = AttributeDef.objects.get_or_create(
                    node_type=root_node_type,
                    json_key='key',
                    variant_key=None,
                    defaults={
                        'name': 'key',
                        'is_required': False,
                        'is_common': True,
                        'data_type': string_type,
                    },
                )
        if key_attr_def:
            NodeAttribute.objects.update_or_create(
                node_id=schema_id,
                attribute_def=key_attr_def,
                defaults={'value_string': schema_key}
            )
        
        # Set status attribute
        if status_attr_def:
            NodeAttribute.objects.update_or_create(
                node_id=schema_id,
                attribute_def=status_attr_def,
                defaults={'value_string': schema_status}
            )
    
    def _process_schema_structure(self, schema: dict, root_id: uuid.UUID, project_id: Optional[uuid.UUID], organization_id: Optional[uuid.UUID]):
        """Process the complete schema JSON structure and create child nodes recursively.
        
        Delegates the entire root object to _process_node_attributes, which contains
        the unified dispatch logic for all cases (scalars, collections, shorthand sections,
        and composition-inferred child nodes).
        """
        from schemas.models import Node
        
        root_key = next(iter(schema.keys())) if schema else None
        if not root_key:
            return
        
        root_obj = schema[root_key] if isinstance(schema[root_key], dict) else {}
        
        root_node = Node.objects.get(id=root_id)
        root_node_type = root_node.node_type
        
        self._process_node_attributes(root_id, root_node_type, root_obj, project_id, organization_id)
    
    def _resolve_child_type_for_item(self, item: dict, compositions):
        """Select the correct child NodeType for a JSON item from a set of candidate compositions.

        When multiple compositions share the same collection_key (e.g. both sdui_container and
        sdui_widget are valid children under 'children'), the item's 'type' field is used to
        discriminate:
          - If a child_type has a domain-constrained 'type' AttributeDef whose domain contains
            the item's type value, that child_type wins.
          - If only one composition exists, it is used directly.
          - If no domain match is found, fall back to the first composition.
        """
        from schemas.models import AttributeDef, DomainItem

        if len(compositions) == 1:
            return compositions[0].child_type

        item_type_value = item.get('type')
        if item_type_value:
            for comp in compositions:
                type_attr = AttributeDef.objects.filter(
                    node_type=comp.child_type,
                    json_key='type',
                    domain__isnull=False,
                ).first()
                if type_attr and DomainItem.objects.filter(
                    domain=type_attr.domain,
                    value=item_type_value,
                ).exists():
                    return comp.child_type

        return compositions[0].child_type

    def _process_collection(self, collection_key: str, items: list, parent_id: uuid.UUID, parent_node_type, project_id: Optional[uuid.UUID], organization_id: Optional[uuid.UUID], sort_order_offset: int = 0):
        """Process a collection of items as child nodes using NodeTypeComposition"""
        from schemas.models import Node, NodeType, NodeTypeComposition, AttributeDef, DataType
        from schemas.repositories.node_type_repository import NodeTypeRepository

        # Find ALL valid child types for this parent type matching the collection_key
        compositions = list(
            NodeTypeComposition.objects.filter(
                parent_type=parent_node_type,
                collection_key=collection_key,
            ).select_related('child_type')
        )

        if not compositions:
            # No composition rule — try to infer from collection key
            singular_key = collection_key.rstrip('s')
            fallback_type = NodeTypeRepository().get_node_type_by_name(singular_key)
            if not fallback_type:
                fallback_type = NodeType.objects.filter(is_container=True).first()
            if not fallback_type:
                return
            compositions = [type('_Comp', (), {'child_type': fallback_type})()]

        # Pre-build field maps per child_type to avoid repeated DB queries
        field_maps = {}
        for comp in compositions:
            ct = comp.child_type
            if ct.id not in field_maps:
                attr_defs = AttributeDef.objects.filter(node_type=ct, variant_key__isnull=True)
                field_maps[ct.id] = (ct, self._build_json_key_to_field_map(attr_defs))

        # Get max_children from composition to determine if this is a singleton slot
        max_children = compositions[0].max_children if compositions and hasattr(compositions[0], 'max_children') else None
        
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            # Validate conditional structures within the item
            for item_key, item_value in item.items():
                if isinstance(item_value, dict) and self._is_conditional_structure(item_value):
                    try:
                        validate_conditional_structure(item_value)
                    except Exception as e:
                        raise ValueError(f"Invalid conditional structure for '{item_key}' in item {idx} of '{collection_key}': {str(e)}")

            child_node_type = self._resolve_child_type_for_item(item, compositions)
            _, json_key_to_field_map = field_maps[child_node_type.id]

            # For singleton slots (max_children=1), use collection_key as node key.
            # This enables the SQL s7_build_node_json to join: ch.key = ntc.collection_key
            # For collections (max_children != 1), generate unique keys from item id or index.
            if max_children == 1:
                # Map JSON properties to Node fields (including natural attributes like version)
                node_fields = self._map_json_to_node_fields(item, json_key_to_field_map, child_node_type, sort_order_offset + idx, collection_key)
                # Override name/key with collection_key for singleton slots
                node_fields['name'] = collection_key
                node_fields['key'] = collection_key
            else:
                node_fields = self._map_json_to_node_fields(item, json_key_to_field_map, child_node_type, sort_order_offset + idx)
                # Collections: prefer explicit 'key' from JSON, then 'id', then generate
                json_key_val = item.get('key')
                json_id = item.get('id')
                if json_key_val:
                    node_fields['key'] = json_key_val
                    node_fields['name'] = json_key_val
                elif json_id:
                    node_fields['key'] = json_id
                    node_fields['name'] = json_id
                elif node_fields['key'] is None:
                    node_fields['key'] = f"{collection_key}_{idx}"
                    node_fields['name'] = f"{collection_key}_{idx}"

            child_node = Node(
                node_type=child_node_type,
                parent_id=parent_id,
                project_id=project_id,
                organization_id=organization_id,
                **node_fields
            )
            child_node.save()

            # Process attributes for this node (recursion into children is handled inside)
            self._process_node_attributes(child_node.id, child_node_type, item, project_id, organization_id)
    
    def _build_json_key_to_field_map(self, attr_defs):
        """Build a mapping from json_key to Node field names based on AttributeDefs"""
        # Node model fields that can be mapped from JSON
        node_fields = {'sort_order', 'name', 'key', 'version'}
        
        # Build map from AttributeDefs - map based on AttributeDef.name to Node field
        json_key_map = {}
        for attr_def in attr_defs:
            # natural_version AttributeDefs must write to node.version column
            if attr_def.data_type.name == 'natural_version':
                json_key_map[attr_def.json_key] = 'version'
                continue
            # display_order attrs read from node.sort_order at reconstruction time;
            # do NOT map the JSON value to node.sort_order (it would add 1 extra on output).
            if attr_def.data_type.name == 'display_order':
                continue
            if attr_def.name in node_fields:
                # If AttributeDef.name matches a Node field, map the json_key to that field
                json_key_map[attr_def.json_key] = attr_def.name
        
        return json_key_map
    
    def _map_json_to_node_fields(self, item: dict, json_key_map: dict, node_type, idx: int, json_key: Optional[str] = None) -> dict:
        """Map JSON properties to Node fields using the key map"""
        node_fields = {
            'sort_order': idx,
            'name': f'{node_type.name}_{idx}',
            'key': None,  # Will be generated if not in JSON
        }
        
        for json_key, field_name in json_key_map.items():
            if json_key in item and item[json_key] is not None:
                node_fields[field_name] = item[json_key]
        
        # Handle key assignment: if key exists in JSON, assign it to both key and name
        if 'key' in item and item['key']:
            node_fields['key'] = item['key']
            node_fields['name'] = item['key']
        elif node_fields['key'] is None:
            # Use provided json_key for structural matches (e.g., 'props' instead of 'sdui_props_0')
            if json_key:
                node_fields['key'] = json_key
                node_fields['name'] = json_key
            elif 'name' in item and item['name']:
                node_fields['key'] = item['name']
            elif 'id' in item and item['id']:
                node_fields['key'] = str(item['id'])
            else:
                # Generate a unique key based on node type and index
                node_fields['key'] = f'{node_type.name}_{idx}'
        
        return node_fields
    
    def _process_single_child_node(self, parent_id: uuid.UUID, json_key: str, value: dict, child_type, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None, index: Optional[int] = None, sort_order: int = 0, inherited_variant: Optional[str] = None):
        """Process a single child node from a dict value (composition without collection_key)"""
        from schemas.models import Node, AttributeDef

        # Validate conditional structure if value has conditional structure
        if self._is_conditional_structure(value):
            try:
                validate_conditional_structure(value)
            except Exception as e:
                raise ValueError(f"Invalid conditional structure for '{json_key}': {str(e)}")

        # Generate unique name/key based on index
        if index is not None:
            name = f'{json_key}_{index}'
            key = f'{json_key}_{index}'
        else:
            name = json_key
            key = json_key

        # Build map of natural attributes to node fields
        from schemas.models import AttributeDef
        attr_defs = AttributeDef.objects.filter(node_type=child_type)
        json_key_map = self._build_json_key_to_field_map(attr_defs)
        
        # Map JSON properties to Node fields (including natural attributes like version)
        node_fields = self._map_json_to_node_fields(value, json_key_map, child_type, sort_order, json_key)
        
        # Override name/key with generated values if not set by mapping
        # For structural matches (no collection_key), always use json_key as the node key
        # to preserve original key names like 'props' instead of 'sdui_props_0'
        if 'name' not in node_fields or not node_fields['name']:
            node_fields['name'] = name
        if 'key' not in node_fields or not node_fields['key']:
            node_fields['key'] = key
        node_fields['sort_order'] = sort_order

        # Create child node
        child_node = Node(
            node_type=child_type,
            parent_id=parent_id,
            project_id=project_id,
            organization_id=organization_id,
            **node_fields
        )
        child_node.save()

        # Generic: if child_type has an AttributeDef with json_key 'usage', set it to the parent json_key
        # This allows mapping show_if -> condition_group with usage='show_if' without hardcoding
        usage_attr_def = AttributeDef.objects.filter(
            node_type=child_type,
            json_key='usage',
            variant_key__isnull=True
        ).first()
        if usage_attr_def:
            self._set_node_attribute(child_node.id, 'usage', json_key, child_type, inherited_variant)

        # Process the dict as attributes and nested structures
        # For props nodes, pass inherited_variant to _process_node_attributes
        # so it can resolve variant-scoped AttributeDefs (keyed by parent component type)
        # Check if this child_type is the props_node_type according to NodeTypeVariant
        from schemas.models import NodeTypeVariant
        parent_ntv = NodeTypeVariant.objects.filter(props_node_type=child_type).first()
        if parent_ntv and inherited_variant:
            discriminator = parent_ntv.discriminator_attr
            if discriminator and discriminator not in value:
                # Inject discriminator for variant resolution in _process_node_attributes,
                # but it should NOT be stored as a NodeAttribute.
                value = dict(value, **{discriminator: inherited_variant})
        self._process_node_attributes(child_node.id, child_type, value, project_id, organization_id, inherited_variant)
    
    def _set_node_attribute(self, node_id: uuid.UUID, json_key: str, value, node_type, variant_key: Optional[str] = None):
        """Set a node attribute value, creating AttributeDef if needed"""
        from schemas.models import AttributeDef, DataType, NodeAttribute
        from schemas.repositories.schema_repository import SchemaRepository

        # Disable triggers to bypass s7 validation
        repository = SchemaRepository()
        repository.disable_triggers()

        try:
            # Determine data type
            if isinstance(value, bool):
                data_type_name = 'bool'
            elif isinstance(value, (int, float)):
                data_type_name = 'number'
            elif isinstance(value, str):
                data_type_name = 'string'
            elif isinstance(value, (list, dict)):
                data_type_name = 'json'
            else:
                data_type_name = 'string'

            data_type = DataType.objects.filter(name=data_type_name).first()
            if not data_type:
                data_type = DataType.objects.filter(name='string').first()

            if not data_type:
                return

            # Validate conditional structure if value has conditional structure
            if self._is_conditional_structure(value):
                try:
                    validate_conditional_structure(value)
                except Exception as e:
                    raise ValueError(f"Invalid conditional structure for '{json_key}': {str(e)}")

            # Decide whether to create as universal or variant-specific
            final_variant_key = self._determine_variant_key(node_type, json_key, variant_key)

            # Check if an AttributeDef already exists with the determined variant_key
            attr_def = AttributeDef.objects.filter(
                node_type=node_type,
                json_key=json_key,
                variant_key=final_variant_key
            ).first()

            if not attr_def:
                # Create AttributeDef with the determined variant_key
                is_common = (final_variant_key is None)
                attr_def = AttributeDef.objects.create(
                    name=json_key,
                    json_key=json_key,
                    is_required=False,
                    is_common=is_common,
                    data_type=data_type,
                    node_type=node_type,
                    variant_key=final_variant_key,
                )
            
            if attr_def:
                # Set the attribute value
                if value is None:
                    return
                elif isinstance(value, bool):
                    NodeAttribute.objects.update_or_create(
                        node_id=node_id,
                        attribute_def=attr_def,
                        defaults={'value_bool': value}
                    )
                elif isinstance(value, str):
                    NodeAttribute.objects.update_or_create(
                        node_id=node_id,
                        attribute_def=attr_def,
                        defaults={'value_string': value}
                    )
                elif isinstance(value, (int, float)):
                    from decimal import Decimal
                    decimal_value = Decimal(str(value))
                    NodeAttribute.objects.update_or_create(
                        node_id=node_id,
                        attribute_def=attr_def,
                        defaults={'value_number': decimal_value}
                    )
                elif isinstance(value, (list, dict)):
                    NodeAttribute.objects.update_or_create(
                        node_id=node_id,
                        attribute_def=attr_def,
                        defaults={'value_json': value}
                    )
        finally:
            # Re-enable triggers
            repository.enable_triggers()
    
    def _process_node_attributes(self, node_id: uuid.UUID, node_type, attributes: dict, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None, inherited_variant: Optional[str] = None):
        """Process attributes for a node, creating AttributeDefs dynamically if needed.
        
        Dispatch priority for each json_key in attributes:
          1. List value whose key matches a composition collection_key -> _process_collection
             Also: dict value whose key matches a collection_key with max_children=1 -> wrapped as [value]
          2. Dict/list value whose key is a domain-discriminator shorthand for a collection
             (e.g. 'body' -> screen_section with section_key='body') -> create child node
          3. Dict/list value that structurally matches a composition without collection_key
             (e.g. 'show_if' -> sdui_show_if) -> _process_single_child_node
          4. Scalar/dict value with no composition match -> store as NodeAttribute
        
        Args:
            inherited_variant: Variant key inherited from parent node (e.g., for sdui_props)
        """
        from schemas.models import AttributeDef, DataType, NodeAttribute, NodeTypeComposition, DomainItem, Node
        from schemas.repositories.schema_repository import SchemaRepository
        
        # Get Node model field names (these are not attributes)
        node_field_names = {f.name for f in Node._meta.get_fields()}
        
        compositions = NodeTypeComposition.objects.filter(parent_type=node_type).select_related('child_type')
        
        # Case 1 — collection keys (list or singleton dict values)
        collection_keys = {comp.collection_key for comp in compositions if comp.collection_key}
        # Map collection_key -> composition for max_children lookup (singleton dict dispatch)
        collection_key_to_comp = {comp.collection_key: comp for comp in compositions if comp.collection_key}
        
        # Case 2 — shorthand discriminator map:
        #   For compositions with collection_key whose child type has a required domain-constrained
        #   AttributeDef, map each domain value -> (child_type, discriminator_json_key).
        #   Example: screen -> screen_section (sections), section_key in ScreenSectionKey
        #   -> {'body': (screen_section, 'section_key'), 'appbar': (screen_section, 'section_key'), ...}
        shorthand_map = {}  # domain_value -> (child_type, discriminator_json_key)
        for comp in compositions:
            if not comp.collection_key:
                continue
            discriminator = AttributeDef.objects.filter(
                node_type=comp.child_type,
                variant_key__isnull=True,
                is_required=True,
                domain__isnull=False,
            ).exclude(data_type__name__startswith='natural_').exclude(json_key='type').first()
            if not discriminator:
                continue
            for di in DomainItem.objects.filter(domain=discriminator.domain):
                shorthand_map[di.value] = (comp.child_type, discriminator.json_key)
        
        # Case 3 — structural inference for compositions without collection_key
        #   (e.g. show_if -> sdui_show_if, layout -> sdui_layout)
        # The matching is now based exclusively on NodeTypeComposition and key_overlap heuristics.
        # No hardcoded prefixes are needed - the composition is the source of truth.
        json_key_to_child_type = {}
        for comp in compositions:
            if comp.collection_key is not None:
                continue
            child_name = comp.child_type.name
            canonical = child_name
            child_attr_keys = {
                ad.json_key for ad in AttributeDef.objects.filter(
                    node_type=comp.child_type, variant_key__isnull=True
                )
            }
            # Match if json_key equals canonical name OR if ≥2 attribute keys overlap
            # (single-key overlap is too weak and causes false positives like 'props' ~ sdui_layout)
            # Fallback: suffix match (e.g., layout -> sdui_layout, show_if -> sdui_show_if)
            for json_key, value in attributes.items():
                if json_key in node_field_names or json_key in collection_keys or json_key in shorthand_map:
                    continue
                val_keys = set(value.keys()) if isinstance(value, dict) else (
                    set(value[0].keys()) if isinstance(value, list) and value and isinstance(value[0], dict) else set()
                )
                exact_match = (json_key == canonical)
                key_overlap = len(val_keys & child_attr_keys) >= 2
                suffix_match = child_name.endswith('_' + json_key)  # e.g., sdui_layout ends with _layout
                if not (exact_match or key_overlap or suffix_match):
                    continue
                if isinstance(value, dict):
                    json_key_to_child_type[json_key] = comp.child_type
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    json_key_to_child_type[json_key] = comp.child_type
        
        # Disable triggers to bypass s7 validation
        repository = SchemaRepository()
        repository.disable_triggers()
        
        try:
            # Determine the node's variant using the discriminator from NodeTypeVariant
            from schemas.models import NodeTypeVariant
            ntv = NodeTypeVariant.objects.filter(node_type=node_type).first()
            discriminator = ntv.discriminator_attr if ntv else 'type'
            
            # If this node_type is a props_node_type (discriminator_attr=None), use inherited_variant
            # This allows sdui_props to resolve variant-scoped AttributeDefs based on parent component type
            if ntv and ntv.discriminator_attr is None and inherited_variant:
                node_variant = inherited_variant
            else:
                node_variant = attributes.get(discriminator)
            
            # Counter to assign unique, sequential sort_orders to all child nodes
            # created by any dispatch path within this call.
            child_sort_order = 0
            
            # Process ALL attributes (no filtering of empty/null/zero values)
            for json_key, value in attributes.items():
                # Skip Node model fields (handled separately), except sort_order which
                # may be expressed explicitly in JSON and must be stored as an attribute
                # when no display_order AttributeDef is present on this node type.
                if json_key in node_field_names and json_key != 'sort_order':
                    continue
                # Skip natural type attributes (natural_version, natural_key, etc.) 
                # These are mapped to node columns and handled separately
                attr_def = AttributeDef.objects.filter(node_type=node_type, json_key=json_key).first()
                if attr_def and attr_def.data_type.name.startswith('natural_'):
                    continue
                if json_key == 'sort_order':
                    # If a display_order AttributeDef exists, skip: the value is driven by
                    # node.sort_order (loop index) and reconstructed as sort_order+1 in SQL.
                    has_display_order = AttributeDef.objects.filter(
                        node_type=node_type,
                        json_key='sort_order',
                        data_type__name='display_order',
                    ).exists()
                    if has_display_order:
                        continue
                
                # Skip the discriminator attribute if it's the one we inherited
                # Check if this node_type is a props_node_type for some parent variant
                from schemas.models import NodeTypeVariant
                parent_ntv = NodeTypeVariant.objects.filter(props_node_type=node_type).first()
                if parent_ntv and json_key == parent_ntv.discriminator_attr:
                    continue
                
                # Case 1: value whose key matches a composition collection_key
                if json_key in collection_keys:
                    if isinstance(value, list):
                        self._process_collection(json_key, value, node_id, node_type, project_id, organization_id, sort_order_offset=child_sort_order)
                        child_sort_order += len(value)
                    elif isinstance(value, dict):
                        # Singleton dict: only dispatch if composition allows exactly one child (max_children=1)
                        comp = collection_key_to_comp.get(json_key)
                        if comp and comp.max_children == 1:
                            # Inject discriminator value if this slot has a shorthand mapping
                            # (e.g. section_key='body' for the 'body' slot of screen_section)
                            if json_key in shorthand_map:
                                _, discriminator_json_key = shorthand_map[json_key]
                                item = dict(value)
                                item.setdefault(discriminator_json_key, json_key)
                            else:
                                item = value
                            self._process_collection(json_key, [item], node_id, node_type, project_id, organization_id, sort_order_offset=child_sort_order)
                            child_sort_order += 1
                    continue
                
                # Case 2: shorthand discriminator (e.g. 'body' dict -> screen_section node)
                if json_key in shorthand_map and isinstance(value, dict):
                    child_type, discriminator_json_key = shorthand_map[json_key]
                    name = value.get('id') or json_key
                    child_node = Node(
                        node_type=child_type,
                        parent_id=node_id,
                        project_id=project_id,
                        organization_id=organization_id,
                        sort_order=child_sort_order,
                        name=name,
                        key=json_key,
                    )
                    child_node.save()
                    child_sort_order += 1
                    item_with_discriminator = dict(value)
                    item_with_discriminator.setdefault(discriminator_json_key, json_key)
                    self._process_node_attributes(child_node.id, child_type, item_with_discriminator, project_id, organization_id)
                    continue
                
                # Case 3: structural match -> composition without collection_key (e.g. show_if, sdui_props)
                if json_key in json_key_to_child_type:
                    child_type = json_key_to_child_type[json_key]
                    # For sdui_props, pass the parent's variant (component type) so child
                    # can resolve variant-scoped AttributeDefs (keyed by parent component type)
                    inherited_variant = node_variant if child_type.name == 'sdui_props' else None
                    if isinstance(value, dict):
                        self._process_single_child_node(node_id, json_key, value, child_type, project_id, organization_id, index=None, sort_order=child_sort_order, inherited_variant=inherited_variant)
                        child_sort_order += 1
                    elif isinstance(value, list):
                        for idx, item in enumerate(value):
                            if isinstance(item, dict):
                                self._process_single_child_node(node_id, json_key, item, child_type, project_id, organization_id, index=idx, sort_order=child_sort_order, inherited_variant=inherited_variant)
                                child_sort_order += 1
                    continue
                
                # Handle nested objects: if value is a dict with a single key that matches a pattern like {field: "x"} or {value: "y"}
                # Expand it to flat attributes: {left_kind: "field", left_field_key: "x"}
                if isinstance(value, dict) and len(value) == 1:
                    kind_key = list(value.keys())[0]
                    kind_value = value[kind_key]
                    # Check if there are AttributeDefs that match the pattern {json_key}_kind and {json_key}_{kind_key}_key or {json_key}_{kind_key}
                    kind_attr_def = AttributeDef.objects.filter(
                        node_type=node_type,
                        json_key=f'{json_key}_kind',
                        variant_key__isnull=True
                    ).first()
                    if kind_attr_def:
                        # This is a nested object that should be expanded
                        # Set the kind attribute
                        self._set_node_attribute(node_id, f'{json_key}_kind', kind_key, node_type, node_variant)
                        # Set the value attribute based on the kind
                        # Try both patterns: {json_key}_{kind_key}_key (for field) and {json_key}_{kind_key} (for value)
                        value_attr_def = AttributeDef.objects.filter(
                            node_type=node_type,
                            json_key=f'{json_key}_{kind_key}_key',
                            variant_key__isnull=True
                        ).first()
                        if not value_attr_def:
                            value_attr_def = AttributeDef.objects.filter(
                                node_type=node_type,
                                json_key=f'{json_key}_{kind_key}',
                                variant_key__isnull=True
                            ).first()
                        if value_attr_def:
                            self._set_node_attribute(node_id, value_attr_def.json_key, kind_value, node_type, node_variant)
                        continue
                
                # Try to find existing AttributeDef
                # First, try the variant-specific one (based on 'type' attribute)
                attr_def = None
                if node_variant:
                    attr_def = AttributeDef.objects.filter(
                        node_type=node_type,
                        json_key=json_key,
                        variant_key=node_variant
                    ).first()

                # If not found, try the generic one (variant_key=None, is_common=True only)
                # Catalog entries (is_common=False) are templates, not real properties
                if not attr_def:
                    attr_def = AttributeDef.objects.filter(
                        node_type=node_type,
                        json_key=json_key,
                        variant_key__isnull=True,
                        is_common=True
                    ).first()

                # If not found, create AttributeDef dynamically
                if not attr_def:
                    attr_def = self._create_attribute_def(node_type, json_key, value, node_variant)
                
                if attr_def:
                    # Validate conditional structures recursively in the value
                    # Do this before the try-except to ensure validation errors propagate
                    if isinstance(value, (list, dict)):
                        try:
                            self._validate_conditional_recursively(value, json_key)
                        except Exception as e:
                            raise ValueError(f"Invalid conditional structure for '{json_key}': {str(e)}")
                    
                    # Set the attribute value using direct ORM to bypass s7 validation
                    try:
                        if value is None:
                            # Skip None values (no attribute row needed)
                            continue
                        elif isinstance(value, bool):
                            # Boolean value - use direct insert (must check before int since bool is subclass of int)
                            NodeAttribute.objects.update_or_create(
                                node_id=node_id,
                                attribute_def=attr_def,
                                defaults={'value_bool': value}
                            )
                        elif isinstance(value, str):
                            # String value - use direct insert
                            NodeAttribute.objects.update_or_create(
                                node_id=node_id,
                                attribute_def=attr_def,
                                defaults={'value_string': value}
                            )
                        elif isinstance(value, (int, float)):
                            # Convert to Decimal
                            from decimal import Decimal
                            decimal_value = Decimal(str(value))
                            NodeAttribute.objects.update_or_create(
                                node_id=node_id,
                                attribute_def=attr_def,
                                defaults={'value_number': decimal_value}
                            )
                        elif isinstance(value, (list, dict)):
                            # JSON value - use direct insert with value_json
                            NodeAttribute.objects.update_or_create(
                                node_id=node_id,
                                attribute_def=attr_def,
                                defaults={'value_json': value}
                            )
                        else:
                            # Unknown type - skip
                            pass
                    except Exception as e:
                        # Log error but continue processing other attributes
                        pass
        finally:
            # Re-enable triggers
            repository.enable_triggers()

    def _create_attribute_def(self, node_type, json_key: str, value, variant_key=None):
        """Create an AttributeDef dynamically based on the value type

        New properties are created as variant-specific by default (for the current node variant).
        Only create as universal if:
        1. A universal version already exists (is_common=True, variant_key=None)
        2. A catalog template exists (is_common=False, variant_key=None) - then convert to universal
        """
        from schemas.models import AttributeDef, DataType, NodeTypeComposition

        # Check if json_key corresponds to a collection_key in NodeTypeComposition
        composition_exists = NodeTypeComposition.objects.filter(
            parent_type=node_type,
            collection_key=json_key
        ).exists()
        if composition_exists:
            return None

        # Determine data type from value
        data_type_name = self._infer_data_type(value)
        data_type = DataType.objects.filter(name=data_type_name).first()

        if not data_type:
            # Fallback to string type
            data_type = DataType.objects.filter(name='string').first()

        if not data_type:
            return None

        # Validate conditional structure if value has conditional structure
        if self._is_conditional_structure(value):
            try:
                validate_conditional_structure(value)
            except Exception as e:
                raise ValueError(f"Invalid conditional structure for '{json_key}': {str(e)}")

        # Decide whether to create as universal or variant-specific
        final_variant_key = self._determine_variant_key(node_type, json_key, variant_key)

        # Check if an AttributeDef already exists with the determined variant_key
        existing = AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            variant_key=final_variant_key
        ).first()

        if existing:
            return existing

        # Create AttributeDef with the determined variant_key
        is_common = (final_variant_key is None)
        attr_def = AttributeDef.objects.create(
            name=json_key,
            json_key=json_key,
            is_required=False,
            is_common=is_common,
            data_type=data_type,
            node_type=node_type,
            variant_key=final_variant_key,
        )

        return attr_def

    def _determine_variant_key(self, node_type, json_key: str, requested_variant_key) -> Optional[str]:
        """Determine whether a property should be universal (None) or variant-specific

        Logic:
        1. If there's already a universal version with is_common=True -> return None (use universal)
        2. Otherwise -> return requested_variant_key (variant-specific)
           New properties are variant-specific by default to match seed behavior.
           Catalog entries (is_common=False) are just templates, not real properties.
        """
        from schemas.models import AttributeDef

        # Check if a real universal version already exists
        real_universal_exists = AttributeDef.objects.filter(
            node_type=node_type,
            json_key=json_key,
            variant_key__isnull=True,
            is_common=True
        ).exists()

        if real_universal_exists:
            return None

        # No real universal version exists - create as variant-specific by default
        return requested_variant_key
    
    def _is_conditional_structure(self, value) -> bool:
        """Check if a value has the structure of a conditional expression."""
        if not isinstance(value, dict):
            return False
        return 'logic' in value and 'conditions' in value
    
    def _validate_conditional_recursively(self, value, path: str = ""):
        """Recursively validate all conditional structures in a value."""
        if isinstance(value, dict):
            # Check if this dict itself is a conditional structure
            if self._is_conditional_structure(value):
                try:
                    validate_conditional_structure(value)
                except Exception as e:
                    raise ValueError(f"Invalid conditional structure at '{path}': {str(e)}")
            # Recursively validate nested values
            for key, nested_value in value.items():
                nested_path = f"{path}.{key}" if path else key
                self._validate_conditional_recursively(nested_value, nested_path)
        elif isinstance(value, list):
            # Recursively validate list items
            for idx, item in enumerate(value):
                nested_path = f"{path}[{idx}]"
                self._validate_conditional_recursively(item, nested_path)
    
    def _infer_data_type(self, value) -> str:
        """Infer DataType from value"""
        if isinstance(value, bool):
            return 'bool'
        elif isinstance(value, int):
            return 'int'
        elif isinstance(value, float):
            return 'number'
        elif isinstance(value, str):
            return 'string'
        elif isinstance(value, list):
            return 'list_string'
        elif isinstance(value, dict):
            return 'json'
        else:
            return 'string'
    
    @transaction.atomic
    def publish_schema(self, node: Node) -> None:
        """Publish a schema node of any root type"""
        key = self._get_schema_attribute(node, 'key')
        status = self._get_schema_attribute(node, 'status')
        version = node.version

        if not version:
            raise ValueError(ERR_VERSION_NOT_SET)

        # Check if node type has a 'status' attribute with a 'published' value in its domain
        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        if status_def and status_def.domain:
            published_value = self.repository.get_domain_item_by_value(status_def.domain, 'published')
            if not published_value:
                raise ValueError(ERR_PUBLISHED_STATUS_NOT_AVAILABLE)
            
            draft_value = self.repository.get_domain_item_by_value(status_def.domain, 'draft')
            if draft_value and status != 'draft':
                raise ValueError(ERR_SCHEMA_NOT_IN_DRAFT_STATUS.format(status=status))
        elif status and status != 'draft':
            raise ValueError(ERR_SCHEMA_NOT_IN_DRAFT_STATUS.format(status=status))

        self.repository.publish_schema(key, version)
    
    @transaction.atomic
    def archive_schema(self, node: Node) -> None:
        """Archive a schema node of any root type"""
        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        if not status_def:
            raise ValueError(ERR_STATUS_ATTRIBUTE_NOT_FOUND)

        status_attr = self.repository.get_node_attribute(node, status_def)
        if not status_attr:
            raise ValueError(ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND)

        current_status = status_attr.value_string
        
        # Check if 'published' value exists in the domain
        if status_def.domain:
            published_value = self.repository.get_domain_item_by_value(status_def.domain, 'published')
            if published_value and current_status != 'published':
                raise ValueError(ERR_SCHEMA_NOT_PUBLISHED_STATUS.format(current_status=current_status))
        elif current_status != 'published':
            raise ValueError(ERR_SCHEMA_NOT_PUBLISHED_STATUS.format(current_status=current_status))

        # Check if 'archived' value exists in the domain
        archived_value = None
        if status_def.domain:
            archived_value = self.repository.get_domain_item_by_value(status_def.domain, 'archived')
        if not archived_value:
            raise ValueError(ERR_ARCHIVED_STATUS_NOT_AVAILABLE)

        try:
            self.repository.set_node_attribute_from_json(
                node.id,
                node.node_type.name,
                status_def.json_key,
                "archived",
                status_def.domain.domain_name if status_def.domain_id else None
            )
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_ARCHIVE.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_ARCHIVE.format(error=e)) from e
    
    @transaction.atomic
    def draft_schema(self, node: Node) -> None:
        """Move a schema node back to draft status"""
        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        if not status_def:
            raise ValueError(ERR_STATUS_ATTRIBUTE_NOT_FOUND)

        status_attr = self.repository.get_node_attribute(node, status_def)
        if not status_attr:
            raise ValueError(ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND)

        current_status = status_attr.value_string
        
        # Check if 'archived' value exists in the domain
        if status_def.domain:
            archived_value = self.repository.get_domain_item_by_value(status_def.domain, 'archived')
            if archived_value and current_status != 'archived':
                raise ValueError(ERR_SCHEMA_NOT_ARCHIVED_STATUS.format(current_status=current_status))
        elif current_status != 'archived':
            raise ValueError(ERR_SCHEMA_NOT_ARCHIVED_STATUS.format(current_status=current_status))

        # Check if 'draft' value exists in the domain
        draft_value = None
        if status_def.domain:
            draft_value = self.repository.get_domain_item_by_value(status_def.domain, 'draft')
        if not draft_value:
            raise ValueError(ERR_DRAFT_STATUS_NOT_AVAILABLE)

        try:
            self.repository.set_node_attribute_from_json(
                node.id,
                node.node_type.name,
                status_def.json_key,
                "draft",
                status_def.domain.domain_name if status_def.domain_id else None
            )
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_DRAFT.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_DRAFT.format(error=e)) from e
    
    def increment_build(self, key: str, version: str, project_id: uuid.UUID) -> None:
        """Increment build counter"""
        try:
            self.repository.increment_build(key, version, project_id)
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_BUILD.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_BUILD.format(error=e)) from e

    def build_schema_cached(self, key: str, version: str, schema_type: Optional[str] = None) -> None:
        """Build schema cache"""
        try:
            self.repository.build_schema_cached(key, version, schema_type)
        except DatabaseError as e:
            raise RuntimeError(ERR_DATABASE_ERROR_CACHE_REBUILD.format(error=e)) from e
        except ValueError as e:
            raise  # Re-raise ValueError as-is
        except Exception as e:
            raise RuntimeError(ERR_UNEXPECTED_ERROR_CACHE_REBUILD.format(error=e)) from e
    
    @transaction.atomic
    def initialize_schema_attributes(self, node: Node, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None) -> None:
        """Initialize default attributes for a new schema node of any root type"""
        key_value = f"{node.name}{SCHEMA_KEY_SUFFIX}"
        version_value = "1"

        # Auto-increment version if key+version already exists
        for attempt in range(MAX_VERSION_AUTO_INCREMENT_ATTEMPTS):
            if not self.repository.check_key_version_exists(key_value, version_value):
                break
            version_value = str(int(version_value) + 1)
        else:
            raise ValueError(
                ERR_VERSION_NOT_AVAILABLE.format(key=key_value, attempts=MAX_VERSION_AUTO_INCREMENT_ATTEMPTS)
            )

        # Stamp project/organization on the root node
        if project_id or organization_id:
            update_fields = {}
            if project_id:
                update_fields["project_id"] = project_id
            if organization_id:
                update_fields["organization_id"] = organization_id
            self.repository.update_node_fields(node.id, **update_fields)

        # Ensure schema_build_state record exists
        self.repository.ensure_schema_build_state(key_value, version_value, project_id)

        # Disable triggers temporarily
        self.repository.disable_triggers()

        try:
            # Default values to bootstrap the root node — applied regardless of is_required flag
            root_defaults = {
                'id':      None,  # auto_uuid will auto-generate
                'key':     key_value,
                'title':   node.name,
            }
            # Only set status if the node type has a 'status' attribute
            status_def = self.repository.get_attribute_def(node.node_type, 'status')
            if status_def and status_def.domain:
                # Try to get default value from domain items, fallback to 'draft'
                default_status = self.repository.get_domain_item_by_value(status_def.domain, 'draft')
                if default_status:
                    root_defaults['status'] = 'draft'
            
            # Get attribute defs for the keys we need to set
            attr_defs = self.repository.get_attribute_defs_by_node_type_and_keys(node.node_type, root_defaults.keys())
            for attr_def in attr_defs:
                default_value = root_defaults.get(attr_def.json_key)
                if default_value is not None or attr_def.data_type.name == 'auto_uuid':
                    self.repository.set_node_attribute_from_json(
                        node.id,
                        node.node_type.name,
                        attr_def.json_key,
                        default_value,
                        attr_def.domain.domain_name if attr_def.domain_id else None
                    )
        finally:
            self.repository.enable_triggers()

        # Auto-create the mandatory {root_type}_metadata child node if it exists
        self._initialize_metadata(node, version_value, project_id=project_id, organization_id=organization_id)

        # Mark as dirty since new nodes have uncommitted changes
        self.repository.mark_build_state_dirty(key_value, version_value)

    @transaction.atomic
    def _initialize_metadata(self, root_node: Node, version_value: str, project_id: Optional[uuid.UUID] = None, organization_id: Optional[uuid.UUID] = None) -> None:
        """Create the {root_type}_metadata child node for any root schema type."""
        metadata_type_name = f"{root_node.node_type.name}{SCHEMA_METADATA_SUFFIX}"
        metadata_type = self.repository.get_node_type_by_name(metadata_type_name)
        if not metadata_type:
            return
        if self.repository.node_exists(root_node, metadata_type):
            return
        last = self.repository.get_last_child_node(root_node)
        meta_pos = (last.sort_order + 1) if last else 0
        
        # Get composition to determine collection_key for singleton slots
        composition = self.repository.get_composition_by_parent_child(root_node.node_type, metadata_type)
        node_key = None
        if composition and composition.collection_key and composition.max_children == 1:
            node_key = composition.collection_key
        
        metadata_node = self.repository.create_node(
            parent=root_node,
            node_type=metadata_type,
            sort_order=meta_pos,
            name='metadata',
            key=node_key,
            version=version_value,
            project_id=project_id,
            organization_id=organization_id,
        )
        self.repository.update_node_fields(root_node.id, version=version_value)
        metadata_defaults = {'version': version_value}
        self.repository.disable_triggers()
        try:
            for attr_def in self.repository.get_attribute_defs_by_node_type_and_keys(metadata_type, metadata_defaults.keys()):
                default_value = metadata_defaults.get(attr_def.json_key)
                if default_value is not None:
                    self.repository.set_node_attribute_from_json(
                        metadata_node.id,
                        metadata_type.name,
                        attr_def.json_key,
                        default_value,
                        attr_def.domain.domain_name if attr_def.domain_id else None
                    )
        finally:
            self.repository.enable_triggers()
    
    def _get_schema_attribute(self, node: Node, attribute_key: str) -> Optional[str]:
        """Get a schema node attribute value by key"""
        if attribute_key == 'key' and node.key:
            return node.key
        attr_def = self.repository.get_attribute_def(node.node_type, attribute_key)
        if not attr_def:
            return None

        attr = self.repository.get_node_attribute(node, attr_def)
        if not attr:
            return None

        return attr.value_string

    @transaction.atomic
    def delete_schema_cache(self, key: str, version: str) -> None:
        """Delete build state and schema cache for a given key and version
        
        This method orchestrates the deletion of both build state and schema cache
        within a single transaction to ensure data consistency.
        """
        self.repository.delete_build_state(key, version)
        self.repository.delete_schema_cache_by_key_version(key, version)

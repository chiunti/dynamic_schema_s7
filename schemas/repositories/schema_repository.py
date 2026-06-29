from django.db import connection
import json
import uuid
from ..models import BuildState
from ..constants import (
    ERR_ROOT_ID_MUST_BE_UUID,
    ERR_KEY_MUST_BE_NON_EMPTY_MAX_255,
    ERR_VERSION_MUST_BE_NON_EMPTY_MAX_255,
    ERR_EXCLUDE_NODE_ID_MUST_BE_UUID_OR_NONE,
    ERR_NODE_ID_MUST_BE_UUID,
    ERR_ATTRIBUTE_DEF_ID_MUST_BE_UUID,
    ERR_JSON_VALUE_MUST_BE_JSON_SERIALIZABLE,
    ERR_SCHEMA_ID_MUST_BE_UUID,
    ERR_VALIDATED_SCHEMA_MUST_BE_DICT,
    ERR_SCHEMA_KEY_MUST_BE_NON_EMPTY_MAX_30,
    ERR_SCHEMA_VERSION_MUST_BE_NON_EMPTY_MAX_20,
    ERR_SCHEMA_STATUS_MUST_BE_VALID,
    ERR_OVERWRITE_MUST_BE_BOOLEAN,
    ERR_KEY_MUST_BE_NON_EMPTY_MAX_30,
    ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20,
    ERR_PROJECT_ID_REQUIRED,
    ERR_NODE_TYPE_NAME_MUST_BE_NON_EMPTY_MAX_255,
    ERR_JSON_KEY_MUST_BE_NON_EMPTY_MAX_255,
    ERR_VALUE_MUST_BE_JSON_SERIALIZABLE,
    ERR_DOMAIN_NAME_MUST_BE_NON_EMPTY_MAX_255_OR_NONE,
    ERR_NODE_TYPE_MUST_BE_NON_EMPTY_MAX_255,
)


class NodeRepository:
    """Repository for node-related database operations"""
    
    def get_node_tree(self, root_id):
        """
        Get recursive tree of nodes for a given schema root node.
        
        Args:
            root_id: UUID of the root node
            
        Returns:
            List of dicts with node data including id, parent_id, sort_order, name, node_type
        """
        if not isinstance(root_id, (str, uuid.UUID)):
            raise ValueError(ERR_ROOT_ID_MUST_BE_UUID)
        root_id = str(root_id)
        
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM s7.s7_get_node_tree(%s::uuid)",
                [root_id],
            )
            rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "parent_id": r[1],
                "sort_order": r[2],
                "name": r[3],
                "node_type": r[4],
            }
            for r in rows
        ]
    
    def check_key_version_unique(self, key, version, exclude_node_id=None, project_id=None, organization_id=None):
        """
        Check if key+version combination is unique, optionally excluding a node.
        
        Args:
            key: Attribute key to check
            version: Attribute version to check
            exclude_node_id: Optional UUID of node to exclude from check
            
        Returns:
            Boolean indicating if the combination is unique
        """
        # Validate inputs
        if not isinstance(key, str) or len(key) == 0 or len(key) > 255:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_255)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 255:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_255)
        if exclude_node_id is not None and not isinstance(exclude_node_id, (str, uuid.UUID)):
            raise ValueError(ERR_EXCLUDE_NODE_ID_MUST_BE_UUID_OR_NONE)
        
        with connection.cursor() as cursor:
            if exclude_node_id:
                cursor.execute(
                    "SELECT s7.s7_check_key_version_unique(%s, %s, %s::uuid)",
                    [key, version, str(exclude_node_id)]
                )
            else:
                cursor.execute(
                    "SELECT s7.s7_check_key_version_unique(%s, %s)",
                    [key, version]
                )
            is_unique = cursor.fetchone()[0]
        return is_unique
    
    def insert_node_attribute_bool_null(self, node_id, attribute_def_id):
        """
        Insert node attribute with NULL bool value.

        ARCHITECTURAL EXCEPTION: This method bypasses s7.s7_set_node_attribute_from_json and writes
        directly to s7.schema_node_attributes. This is a documented exception to the architecture pattern
        that all database operations should go through s7 functions.

        Justification:
        1. The s7 function does not support NULL bool values (it requires a non-null JSON value)
        2. We need to explicitly set value_bool=NULL to represent "no value" for optional bool fields
        3. This is an internal repository operation that does not need s7 triggers (domain validation, etc.)
        4. The operation is idempotent and safe - it only sets NULL values without business logic

        Review Requirements:
        - This exception should be reviewed if s7_set_node_attribute_from_json is enhanced to support NULL values
        - Any changes to schema_node_attributes triggers should verify this operation remains safe
        - This method should only be called from service layer, never directly from views

        Args:
            node_id: UUID of the node
            attribute_def_id: UUID of the attribute definition
        """
        # Validate UUID parameters
        if not isinstance(node_id, (str, uuid.UUID)):
            raise ValueError(ERR_NODE_ID_MUST_BE_UUID)
        if not isinstance(attribute_def_id, (str, uuid.UUID)):
            raise ValueError(ERR_ATTRIBUTE_DEF_ID_MUST_BE_UUID)
        
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO s7.schema_node_attributes (node_id, attribute_def_id, value_bool)
                VALUES (%s, %s, NULL)
                ON CONFLICT (node_id, attribute_def_id)
                DO UPDATE SET value_bool = NULL, value_string = NULL, value_number = NULL, value_json = NULL
                """,
                [str(node_id), attribute_def_id],
            )
    
    def insert_node_attribute_json(self, node_id, attribute_def_id, json_value):
        """
        Insert or update node attribute with JSON value.

        ARCHITECTURAL EXCEPTION: This method bypasses s7.s7_set_node_attribute_from_json and writes
        directly to s7.schema_node_attributes. This is a documented exception to the architecture pattern
        that all database operations should go through s7 functions.

        Justification:
        1. Bulk JSON operations are more efficient with direct SQL (performance optimization)
        2. NULL JSON values need special handling not provided by s7_set_node_attribute_from_json
        3. This is used for internal schema management operations where s7 triggers are not required
        4. The operation is idempotent and safe - it only sets JSON values without business logic

        Review Requirements:
        - This exception should be reviewed if bulk operations are moved to s7 layer
        - Any changes to schema_node_attributes triggers should verify this operation remains safe
        - This method should only be called from service layer, never directly from views
        - Performance impact should be monitored if s7 functions are enhanced for bulk operations

        Args:
            node_id: UUID of the node
            attribute_def_id: UUID of the attribute definition
            json_value: JSON-serializable value or None
        """
        # Validate UUID parameters
        if not isinstance(node_id, (str, uuid.UUID)):
            raise ValueError(ERR_NODE_ID_MUST_BE_UUID)
        if not isinstance(attribute_def_id, (str, uuid.UUID)):
            raise ValueError(ERR_ATTRIBUTE_DEF_ID_MUST_BE_UUID)
        # Validate JSON value
        if json_value is not None and not isinstance(json_value, (dict, list, str, int, float, bool)):
            raise ValueError(ERR_JSON_VALUE_MUST_BE_JSON_SERIALIZABLE)
        
        with connection.cursor() as cursor:
            if json_value is not None:
                cursor.execute(
                    """
                    INSERT INTO s7.schema_node_attributes (node_id, attribute_def_id, value_json)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (node_id, attribute_def_id)
                    DO UPDATE SET value_json = EXCLUDED.value_json,
                                  value_string = NULL,
                                  value_number = NULL,
                                  value_bool = NULL
                    """,
                    [str(node_id), attribute_def_id, json_value],
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO s7.schema_node_attributes (node_id, attribute_def_id, value_json)
                    VALUES (%s, %s, NULL)
                    ON CONFLICT (node_id, attribute_def_id)
                    DO UPDATE SET value_json = NULL,
                                  value_string = NULL,
                                  value_number = NULL,
                                  value_bool = NULL
                    """,
                    [str(node_id), attribute_def_id],
                )
    
    def delete_node_tree(self, node_id):
        """
        Delete node and all its descendants recursively.
        
        Args:
            node_id: UUID of the node to delete
        """
        # Validate UUID parameter
        if not isinstance(node_id, (str, uuid.UUID)):
            raise ValueError(ERR_NODE_ID_MUST_BE_UUID)
        node_id = str(node_id)
        
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_delete_node_tree(%s::uuid)",
                [node_id],
            )
    
    def build_node_json(self, node_id):
        """
        Build JSON representation of a node.
        
        Args:
            node_id: UUID of the node
            
        Returns:
            JSONB representation of the node
        """
        # Validate UUID parameter
        if not isinstance(node_id, (str, uuid.UUID)):
            raise ValueError(ERR_SCHEMA_ID_MUST_BE_UUID)
        node_id = str(node_id)
        
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_build_node_json(%s::uuid)",
                [str(node_id)],
            )
            jsonb_result = cursor.fetchone()[0]
        return jsonb_result


class SchemaRepository:
    """Repository for schema-related database operations"""
    
    def import_schema(self, validated_schema, schema_key, schema_version, schema_status, overwrite, project_id=None, organization_id=None):
        """
        Import a schema using PostgreSQL function.
        
        Args:
            validated_schema: Dict containing the schema data
            schema_key: Schema identifier key (max 30 chars)
            schema_version: Schema version (max 20 chars)
            schema_status: Status (draft, published, archived)
            overwrite: Whether to overwrite existing schema
        """
        # Validate inputs
        if not isinstance(validated_schema, dict):
            raise ValueError(ERR_VALIDATED_SCHEMA_MUST_BE_DICT)
        if not isinstance(schema_key, str) or len(schema_key) == 0 or len(schema_key) > 30:
            raise ValueError(ERR_SCHEMA_KEY_MUST_BE_NON_EMPTY_MAX_30)
        # Allow None or empty string for schema_version (will use "1" as fallback)
        if schema_version is not None and schema_version != "" and (not isinstance(schema_version, str) or len(schema_version) == 0 or len(schema_version) > 20):
            raise ValueError(ERR_SCHEMA_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        if not isinstance(schema_status, str) or schema_status not in ['draft', 'published', 'archived']:
            raise ValueError(ERR_SCHEMA_STATUS_MUST_BE_VALID)
        if not isinstance(overwrite, bool):
            raise ValueError(ERR_OVERWRITE_MUST_BE_BOOLEAN)

        # Use "1" as fallback if schema_version is None or empty string
        if schema_version is None or schema_version == "":
            schema_version = "1"

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_import_schema(%s::jsonb, %s, %s, %s, %s, %s::uuid, %s::uuid)",
                [json.dumps(validated_schema), schema_key, schema_version, schema_status, overwrite, str(project_id) if project_id else None, str(organization_id) if organization_id else None]
            )
            schema_id = cursor.fetchone()[0]
        return schema_id
    
    def publish_schema(self, key, version):
        """
        Publish a schema using PostgreSQL function.
        
        Args:
            key: Schema identifier key (max 30 chars)
            version: Schema version (max 20 chars)
        """
        # Validate inputs
        if not isinstance(key, str) or len(key) == 0 or len(key) > 30:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_30)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 20:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        
        with connection.cursor() as cursor:
            cursor.execute("SELECT s7.s7_publish_schema(%s, %s)", [key, version])
    
    def set_node_attribute_from_json(self, node_id, node_type_name, json_key, value, domain_name=None):
        """
        Set node attribute using PostgreSQL function.
        
        Args:
            node_id: UUID of the node
            node_type_name: Name of the node type (max 255 chars)
            json_key: JSON key for the attribute (max 255 chars)
            value: JSON-serializable value
            domain_name: Optional domain name (max 255 chars)
        """
        # Validate inputs
        if not isinstance(node_id, (str, uuid.UUID)):
            raise ValueError(ERR_NODE_ID_MUST_BE_UUID)
        if not isinstance(node_type_name, str) or len(node_type_name) == 0 or len(node_type_name) > 255:
            raise ValueError(ERR_NODE_TYPE_NAME_MUST_BE_NON_EMPTY_MAX_255)
        if not isinstance(json_key, str) or len(json_key) == 0 or len(json_key) > 255:
            raise ValueError(ERR_JSON_KEY_MUST_BE_NON_EMPTY_MAX_255)
        if not isinstance(value, (dict, list, str, int, float, bool, type(None))):
            raise ValueError(ERR_VALUE_MUST_BE_JSON_SERIALIZABLE)
        if domain_name is not None and (not isinstance(domain_name, str) or len(domain_name) == 0 or len(domain_name) > 255):
            raise ValueError(ERR_DOMAIN_NAME_MUST_BE_NON_EMPTY_MAX_255_OR_NONE)
        
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_set_node_attribute_from_json(%s, %s, %s, %s::jsonb, %s)",
                [str(node_id), node_type_name, json_key, json.dumps(value), domain_name]
            )
    
    def increment_build(self, key, version, project_id):
        """
        Increment build counter using PostgreSQL function.

        Args:
            key: Schema identifier key (max 30 chars)
            version: Schema version (max 20 chars)
            project_id: Project UUID for multi-tenancy (required)
        """
        # Validate inputs
        if not isinstance(key, str) or len(key) == 0 or len(key) > 30:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_30)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 20:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        if not project_id:
            raise ValueError(ERR_PROJECT_ID_REQUIRED)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_increment_build(%s, %s, %s::uuid)",
                [key, version, str(project_id)]
            )

    def ensure_schema_build_state(self, key, version, project_id):
        """
        Ensure schema build state exists using PostgreSQL function.

        Args:
            key: Schema identifier key (max 30 chars)
            version: Schema version (max 20 chars)
            project_id: Project UUID for multi-tenancy (required)
        """
        # Validate inputs
        if not isinstance(key, str) or len(key) == 0 or len(key) > 30:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_30)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 20:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        if not project_id:
            raise ValueError(ERR_PROJECT_ID_REQUIRED)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_ensure_schema_build_state(%s, %s, %s::uuid)",
                [key, version, str(project_id)]
            )
    
    def check_key_version_exists(self, key, version, project_id=None, organization_id=None):
        """
        Check if a key+version combination already exists.
        
        Args:
            key: Attribute key to check (max 255 chars)
            version: Attribute version to check (max 255 chars)
            
        Returns:
            Boolean indicating if the combination exists
        """
        # Validate inputs
        if not isinstance(key, str) or len(key) == 0 or len(key) > 255:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_255)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 255:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_255)
        
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT s7.s7_check_key_version_exists(%s, %s)",
                [key, version]
            )
            exists = cursor.fetchone()[0]
        return exists
    
    def mark_build_state_dirty(self, key, version, project_id=None, organization_id=None):
        """Mark build state as dirty using Django ORM"""
        # Validate inputs
        if not isinstance(key, str) or len(key) == 0 or len(key) > 30:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_30)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 20:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        
        qs = BuildState.objects.filter(key=key, version=version)
        if project_id is not None:
            qs = qs.filter(project_id=project_id)
        if organization_id is not None:
            qs = qs.filter(organization_id=organization_id)
        qs.update(dirty=True)
    
    def build_schema_cached(self, key, version, schema_type=None, project_id=None, organization_id=None):
        """
        Build schema cache using PostgreSQL function.
        
        Args:
            key: Schema identifier key (max 30 chars)
            version: Schema version (max 20 chars)
            schema_type: Optional override; if None, derived automatically from json_scope
        """
        if not isinstance(key, str) or len(key) == 0 or len(key) > 30:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_30)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 20:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        
        with connection.cursor() as cursor:
            cursor.execute("SELECT s7.s7_build_schema_cached(%s, %s, %s)", [key, version, schema_type])
    
    def get_published_schema(self, node_type, key, version, project_id=None, organization_id=None):
        """
        Get schema JSON for published schemas.
        
        Args:
            node_type: Type of node
            key: Schema identifier key (max 30 chars)
            version: Schema version (max 20 chars)
            
        Returns:
            JSON schema or None if not found
        """
        # Validate inputs
        if not isinstance(node_type, str) or len(node_type) == 0 or len(node_type) > 255:
            raise ValueError(ERR_NODE_TYPE_MUST_BE_NON_EMPTY_MAX_255)
        if not isinstance(key, str) or len(key) == 0 or len(key) > 30:
            raise ValueError(ERR_KEY_MUST_BE_NON_EMPTY_MAX_30)
        if not isinstance(version, str) or len(version) == 0 or len(version) > 20:
            raise ValueError(ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20)
        
        # Build query using shared published view
        # node_type is passed to the view which filters by node_type_name
        query = """
            SELECT sc.schema_json
            FROM s7.schema_cache sc
            JOIN s7.v_schema_published v
              ON v.key = sc.key
              AND v.version = sc.version
            WHERE sc.key = %s
              AND sc.version = %s
              AND v.node_type_name = %s
        """

        with connection.cursor() as cursor:
            cursor.execute(query, [key, version, node_type])
            result = cursor.fetchone()
            return result[0] if result else None
    
    def disable_triggers(self):
        """
        Disable triggers temporarily for bulk operations.
        """
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica;")
    
    def enable_triggers(self):
        """
        Re-enable triggers after bulk operations.
        """
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = DEFAULT;")

    def get_attribute_def(self, node_type, json_key):
        """Get attribute definition by node type and json key"""
        from ..models import AttributeDef
        return AttributeDef.objects.filter(node_type=node_type, json_key=json_key).first()

    def get_node_attribute(self, node, attribute_def):
        """Get node attribute value"""
        from ..models import NodeAttribute
        return NodeAttribute.objects.filter(node=node, attribute_def=attribute_def).first()

    def get_domain_item_by_value(self, domain, value):
        """Get domain item by domain and value"""
        from ..models import DomainItem
        return DomainItem.objects.filter(domain=domain, value=value).first()

    def update_node_fields(self, node_id, **fields):
        """Update node fields directly"""
        from ..models import Node
        Node.objects.filter(id=node_id).update(**fields)

    def get_node_type_by_name(self, name):
        """Get node type by name"""
        from ..models import NodeType
        return NodeType.objects.filter(name=name).first()

    def node_exists(self, parent, node_type):
        """Check if node exists with given parent and node type"""
        from ..models import Node
        return Node.objects.filter(parent=parent, node_type=node_type).exists()

    def get_last_child_node(self, parent):
        """Get last child node by sort order"""
        from ..models import Node
        return Node.objects.filter(parent=parent).order_by('-sort_order').first()

    def create_node(self, parent, node_type, sort_order, name, key=None, version=None, project_id=None, organization_id=None):
        """Create a new node"""
        from ..models import Node
        return Node.objects.create(
            parent=parent,
            node_type=node_type,
            sort_order=sort_order,
            name=name,
            key=key,
            version=version,
            project_id=project_id,
            organization_id=organization_id,
        )

    def get_build_state(self, key, version):
        """Get build state by key and version"""
        from ..models import BuildState
        return BuildState.objects.filter(key=key, version=version).first()

    def delete_build_state(self, key, version):
        """Delete build state by key and version"""
        from ..models import BuildState
        BuildState.objects.filter(key=key, version=version).delete()

    def delete_schema_cache_by_key_version(self, key, version):
        """Delete schema cache by key and version"""
        from ..models import SchemaCache
        SchemaCache.objects.filter(key=key, version=version).delete()

    def get_node_attributes(self, node, attribute_def):
        """Get node attribute by node and attribute def"""
        from ..models import NodeAttribute
        return NodeAttribute.objects.filter(node=node, attribute_def=attribute_def).first()

    def get_node_by_id(self, node_id):
        """Get node by ID"""
        from ..models import Node
        return Node.objects.filter(id=node_id).first()

    def get_root_node_by_key_version(self, key, version):
        """Get root node by key and version"""
        from ..models import Node
        return Node.objects.filter(
            node_type__is_root=True,
            parent__isnull=True,
            key=key,
            version=version,
        ).first()

    def get_attribute_defs_by_node_type_and_keys(self, node_type, json_keys):
        """Get attribute definitions by node type and json keys"""
        from ..models import AttributeDef
        return AttributeDef.objects.filter(node_type=node_type, json_key__in=json_keys)

    def get_attribute_defs_by_node_type(self, node_type):
        """Get all attribute definitions for a node type with domain prefetch"""
        from ..models import AttributeDef
        return AttributeDef.objects.filter(node_type=node_type).prefetch_related('domain')

    def get_node_attributes_by_node(self, node):
        """Get all node attributes for a node with attribute_def select_related"""
        from ..models import NodeAttribute
        return NodeAttribute.objects.filter(node=node).select_related('attribute_def')

    def delete_node_attributes(self, node, attribute_def):
        """Delete node attributes for a given node and attribute def"""
        from ..models import NodeAttribute
        NodeAttribute.objects.filter(node=node, attribute_def=attribute_def).delete()

    def update_or_create_node_attribute(self, node, attribute_def, defaults):
        """Update or create node attribute with given defaults"""
        from ..models import NodeAttribute
        return NodeAttribute.objects.update_or_create(
            node=node, attribute_def=attribute_def,
            defaults=defaults
        )

    def update_node_version_by_parent(self, parent_id, version):
        """Update version field for root node by parent ID"""
        from ..models import Node
        Node.objects.filter(id=parent_id, parent_id__isnull=True).update(version=version)

    def get_node_by_id_with_parent(self, node_id):
        """Get node by ID with parent and node_type select_related"""
        from ..models import Node
        return Node.objects.select_related("parent", "node_type", "parent__node_type").filter(id=node_id).first()

    def get_node_by_id_with_node_type(self, node_id):
        """Get node by ID with node_type select_related"""
        from ..models import Node
        return Node.objects.select_related("node_type").filter(id=node_id).first()

    def get_composition_by_parent_child(self, parent_type, child_type):
        """Get composition by parent and child node types"""
        from ..models import NodeTypeComposition
        return NodeTypeComposition.objects.filter(
            parent_type=parent_type,
            child_type=child_type
        ).first()

    def count_children_by_parent_and_type(self, parent, node_type):
        """Count children nodes by parent and node type"""
        from ..models import Node
        return Node.objects.filter(parent=parent, node_type=node_type).count()

    def get_node_by_parent_type_key(self, parent, node_type, key):
        """Get node by parent, node type, and key"""
        from ..models import Node
        # For collection_key-based compositions (singleton slots), check by key only
        # to prevent multiple node types with the same slot key
        return Node.objects.filter(parent=parent, key=key).first()

    def get_siblings_by_parent(self, parent):
        """Get sibling nodes by parent ordered by sort_order descending"""
        from ..models import Node
        return Node.objects.filter(parent=parent).order_by('-sort_order')

    def get_children_by_parent(self, parent_id):
        """Get children nodes by parent ID ordered by sort_order"""
        from ..models import Node
        return Node.objects.filter(parent_id=parent_id).order_by("sort_order").values("id", "name", "node_type__name")

    def get_compositions_by_parent_type(self, node_type, min_children_gt=0):
        """Get compositions by parent type with optional min_children filter"""
        from ..models import NodeTypeComposition
        qs = NodeTypeComposition.objects.filter(parent_type=node_type)
        if min_children_gt > 0:
            qs = qs.filter(min_children__gt=min_children_gt)
        return qs.order_by('child_type__name')

    def normalize_positions(self, parent_id):
        """
        Normalize sort_order values for children of a parent node.

        Args:
            parent_id: UUID of the parent node
        """
        from ..models import Node
        qs = Node.objects.filter(parent_id=parent_id).order_by("sort_order", "id").only("id", "sort_order")
        updates = []
        for idx, n in enumerate(qs):
            if n.sort_order != idx:
                n.sort_order = idx
                updates.append(n)
        if updates:
            Node.objects.bulk_update(updates, ["sort_order"])

    def get_max_sort_order(self, parent_id):
        """
        Get the maximum sort_order value for children of a parent node.

        Args:
            parent_id: UUID of the parent node

        Returns:
            Maximum sort_order value or None if no children exist
        """
        from ..models import Node
        from django.db.models import Max
        result = Node.objects.filter(parent=parent_id).aggregate(Max("sort_order"))
        return result.get("sort_order__max")

    def get_subtree_nodes_with_type(self, root_id):
        """
        Get all nodes in the subtree with their node_type information.

        Args:
            root_id: UUID of the root node

        Returns:
            List of dicts with node data including id, parent_id, node_type__name, sort_order, key
        """
        from ..models import Node
        node_ids = self._get_subtree_ids(root_id)
        return list(Node.objects.select_related("node_type").filter(
            id__in=node_ids
        ).values("id", "parent_id", "node_type__name", "sort_order", "key"))

    def get_nodes_with_attrs(self, node_ids):
        """
        Get nodes that have attributes from a list of node IDs.

        Args:
            node_ids: List of node UUIDs

        Returns:
            Set of node IDs that have attributes
        """
        from ..models import NodeAttribute
        return set(
            NodeAttribute.objects.filter(
                node_id__in=node_ids
            ).values_list("node_id", flat=True)
        )

    def get_nodes_with_children(self, node_ids):
        """
        Get parent IDs from a list of node IDs that have children.

        Args:
            node_ids: List of node UUIDs

        Returns:
            Set of parent IDs that have children
        """
        from ..models import Node
        return set(
            Node.objects.filter(
                parent_id__in=node_ids
            ).values_list("parent_id", flat=True)
        )

    def _get_subtree_ids(self, root_id):
        """
        Return all node IDs in the subtree rooted at root_id (BFS).

        Args:
            root_id: UUID of the root node

        Returns:
            List of node IDs
        """
        from collections import deque
        from ..models import Node
        ids = []
        queue = deque([root_id])
        while queue:
            current = queue.popleft()
            ids.append(current)
            children = Node.objects.filter(parent_id=current).values_list("id", flat=True)
            queue.extend(children)
        return ids

    def get_root_node_by_key_version(self, key: str, version: str):
        """
        Get root node by key and version.

        Args:
            key: Schema key
            version: Schema version

        Returns:
            Node instance or None
        """
        from ..models import Node
        return Node.objects.filter(
            node_type__is_root=True,
            parent__isnull=True,
            key=key,
            version=version,
        ).select_related('node_type').first()

    def get_attribute_def_by_node_type_key(self, node_type, json_key: str):
        """
        Get attribute definition by node type and json_key.

        Args:
            node_type: NodeType instance
            json_key: JSON key to search for

        Returns:
            AttributeDef instance or None
        """
        from ..models import AttributeDef
        return AttributeDef.objects.filter(node_type=node_type, json_key=json_key).first()

    def get_node_attribute_by_node_attr_def(self, node, attribute_def):
        """
        Get node attribute by node and attribute definition.

        Args:
            node: Node instance
            attribute_def: AttributeDef instance

        Returns:
            NodeAttribute instance or None
        """
        from ..models import NodeAttribute
        return NodeAttribute.objects.filter(node=node, attribute_def=attribute_def).first()

    def get_root_nodes_by_node_type_name(self, node_type_name: str):
        """
        Get root nodes by node type name.

        Args:
            node_type_name: Name of the node type

        Returns:
            List of (key, version) tuples
        """
        from ..models import Node
        return Node.objects.filter(
            node_type__name=node_type_name,
            parent__isnull=True,
            key__isnull=False,
            version__isnull=False,
        ).values_list('key', 'version')

    def get_children_ids_by_parent(self, parent_id):
        """
        Get child node IDs for a given parent node.

        Args:
            parent_id: UUID of the parent node

        Returns:
            List of child node IDs
        """
        from ..models import Node
        return list(Node.objects.filter(parent_id=parent_id).values_list("id", flat=True))

    def get_node_by_id(self, node_id):
        """
        Get node by ID.

        Args:
            node_id: UUID of the node

        Returns:
            Node instance or None
        """
        from ..models import Node
        return Node.objects.get(id=node_id)

    def get_node_by_parent_and_type(self, parent_id, node_type):
        """
        Get node by parent and node type.

        Args:
            parent_id: UUID of the parent node
            node_type: NodeType instance

        Returns:
            Node instance or None
        """
        from ..models import Node
        return Node.objects.filter(parent=parent_id, node_type=node_type).first()

    def node_exists_by_parent_key(self, parent, key):
        """
        Check if a node with given parent and key exists.

        Args:
            parent: Parent Node instance
            key: Key to check

        Returns:
            Boolean indicating if node exists
        """
        from ..models import Node
        return Node.objects.filter(parent=parent, key=key).exists()

    def count_children_by_parent_type(self, parent, node_type):
        """
        Count children of a parent by node type.

        Args:
            parent: Parent Node instance
            node_type: NodeType instance

        Returns:
            Count of children
        """
        from ..models import Node
        return Node.objects.filter(parent=parent, node_type=node_type).count()

    def composition_exists(self, parent_type, child_type):
        """
        Check if a composition exists between parent and child types.

        Args:
            parent_type: Parent NodeType instance
            child_type: Child NodeType instance

        Returns:
            Boolean indicating if composition exists
        """
        from ..models import NodeTypeComposition
        return NodeTypeComposition.objects.filter(parent_type=parent_type, child_type=child_type).exists()

    def get_children_counts_by_parent_type_ids(self, parent, child_type_ids):
        """
        Get count of children by node type IDs for a given parent.

        Args:
            parent: Parent Node instance
            child_type_ids: List of child node type IDs

        Returns:
            Dict mapping node_type_id to count
        """
        from ..models import Node
        from django.db.models import Count
        return dict(
            Node.objects.filter(parent=parent, node_type_id__in=child_type_ids)
            .values_list("node_type_id")
            .annotate(cnt=Count("id"))
            .values_list("node_type_id", "cnt")
        )

    def get_children_keys_by_parent_type_ids(self, parent, child_type_ids):
        """
        Get keys of children by node type IDs for a given parent.

        Args:
            parent: Parent Node instance
            child_type_ids: List of child node type IDs

        Returns:
            Set of keys
        """
        from ..models import Node
        return set(
            Node.objects.filter(parent=parent, node_type_id__in=child_type_ids, key__isnull=False)
            .values_list("key", flat=True)
        )

    def get_node_by_parent_type(self, parent, child_type_id):
        """
        Get node by parent and child type ID.

        Args:
            parent: Parent Node instance
            child_type_id: Child node type ID

        Returns:
            Node instance or None
        """
        from ..models import Node
        return Node.objects.filter(parent=parent, node_type_id=child_type_id).first()

    def bulk_update_nodes_sort_order(self, nodes):
        """
        Bulk update sort_order for multiple nodes.

        Args:
            nodes: List of Node instances with updated sort_order
        """
        from ..models import Node
        Node.objects.bulk_update(nodes, ["sort_order"])

    def update_or_create_node_attribute(self, node_id, attribute_def, defaults):
        """
        Update or create a NodeAttribute.

        Args:
            node_id: UUID of the node
            attribute_def: AttributeDef instance
            defaults: Dict of default values

        Returns:
            Tuple of (NodeAttribute, created)
        """
        from ..models import NodeAttribute
        return NodeAttribute.objects.update_or_create(
            node_id=node_id,
            attribute_def=attribute_def,
            defaults=defaults
        )

    def update_node_name(self, node_id: uuid.UUID, name: str):
        """
        Update node name.

        Args:
            node_id: Node UUID
            name: New name

        Returns:
            Number of rows updated
        """
        from ..models import Node
        return Node.objects.filter(id=node_id).update(name=name)

    def update_node_parent(self, node_id: uuid.UUID, parent_id: uuid.UUID):
        """
        Update node parent.

        Args:
            node_id: Node UUID
            parent_id: New parent UUID

        Returns:
            Number of rows updated
        """
        from ..models import Node
        return Node.objects.filter(id=node_id).update(parent_id=parent_id)

    def update_node_key(self, node_id: uuid.UUID, key: str):
        """
        Update node key.

        Args:
            node_id: Node UUID
            key: New key

        Returns:
            Number of rows updated
        """
        from ..models import Node
        return Node.objects.filter(id=node_id).update(key=key)

    def update_node_version(self, node_id: uuid.UUID, version: str):
        """
        Update node version.

        Args:
            node_id: Node UUID
            version: New version

        Returns:
            Number of rows updated
        """
        from ..models import Node
        return Node.objects.filter(id=node_id).update(version=version)

    def update_node_sort_order(self, node_id: uuid.UUID, sort_order: int):
        """
        Update node sort order.

        Args:
            node_id: Node UUID
            sort_order: New sort order

        Returns:
            Number of rows updated
        """
        from ..models import Node
        return Node.objects.filter(id=node_id).update(sort_order=sort_order)

    def create_child_node(self, node_type, parent_id: uuid.UUID, key: str, **kwargs):
        """
        Create a child node.

        Args:
            node_type: NodeType instance
            parent_id: Parent node UUID
            key: Node key
            **kwargs: Additional node fields

        Returns:
            Node instance
        """
        from ..models import Node
        node = Node(
            node_type=node_type,
            parent_id=parent_id,
            key=key,
            **kwargs
        )
        node.save()
        return node

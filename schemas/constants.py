"""
Shared error code constants for API responses.
Used by admin views, node editor endpoints, and service layer exceptions.
"""

ERR_METHOD_NOT_ALLOWED = "method_not_allowed"
ERR_INVALID_JSON = "invalid_json"
ERR_NOT_FOUND = "not_found"
ERR_PARENT_NOT_FOUND = "parent_not_found"
ERR_NODE_TYPE_NOT_FOUND = "node_type_not_found"
ERR_COMPOSITION_NOT_ALLOWED = "composition_not_allowed"
ERR_ATTRIBUTE_DEF_NOT_FOUND = "attribute_def_not_found"
ERR_VARIANT_KEY_NOT_FOUND = "variant_key_not_found"
ERR_SCOPE_NOT_FOUND = "scope_not_found"
ERR_SCOPE_REQUIRED = "scope_required"
ERR_COMPONENT_TYPE_DOMAIN_NOT_FOUND = "component_type_domain_not_found"
ERR_DEFAULT_DATA_TYPE_NOT_FOUND = "default_data_type_not_found"
ERR_ALREADY_SPECIFIC = "attribute_def_is_already_specific"
ERR_JSON_KEY_DUPLICATE = "json_key_already_exists_for_this_node_type_and_variant"

# Service layer error messages
ERR_VERSION_NOT_SET = "Version not set on this schema node"
ERR_SCHEMA_NOT_DRAFT = "Schema is not in draft status"
ERR_STATUS_ATTRIBUTE_NOT_FOUND = "Status attribute not found"
ERR_STATUS_ATTRIBUTE_VALUE_NOT_FOUND = "Status attribute value not found"
ERR_SCHEMA_NOT_PUBLISHED = "Schema is not in published status"
ERR_SCHEMA_NOT_ARCHIVED = "Schema is not in archived status"
ERR_COMPOSITION_NOT_FOUND = "Composition not found"
ERR_INVALID_PARENT_OR_CHILD_TYPE = "Invalid parent or child type"
ERR_COMPOSITION_ALREADY_EXISTS = "Composition already exists"
ERR_JSON_KEY_AND_NAME_REQUIRED = "json_key and name are required"
ERR_SCHEMA_MUST_BE_JSON_OBJECT = "Schema must be a JSON object"
ERR_SCHEMA_MUST_HAVE_NAME_ID_OR_KEY = "Schema must have a 'name', 'id', or 'key' field"
ERR_CANNOT_DELETE_PROJECT_WITH_SCHEMAS = "Cannot delete a project that still contains schemas"
ERR_MEMBER_NOT_FOUND = "Member not found in this organization"
ERR_PROPERTIES_REQUIRED = "properties_required"
ERR_MAX_CHILDREN_VIOLATION = "max_children_violation"
ERR_VERSION_NOT_AVAILABLE = "Could not find available version for schema with key='{key}' after {attempts} attempts"
ERR_PERMISSION_DENIED = "You do not have permission to import schemas in this project."

# Generic error messages for RuntimeError
ERR_DATABASE_ERROR_ARCHIVE = "Database error during archive: {error}"
ERR_UNEXPECTED_ERROR_ARCHIVE = "Unexpected error during archive: {error}"
ERR_DATABASE_ERROR_DRAFT = "Database error during draft: {error}"
ERR_UNEXPECTED_ERROR_DRAFT = "Unexpected error during draft: {error}"
ERR_DATABASE_ERROR_BUILD = "Database error during build: {error}"
ERR_UNEXPECTED_ERROR_BUILD = "Unexpected error during build: {error}"
ERR_DATABASE_ERROR_CACHE_REBUILD = "Database error during cache rebuild: {error}"
ERR_UNEXPECTED_ERROR_CACHE_REBUILD = "Unexpected error during cache rebuild: {error}"

# API and validation error messages
ERR_INVALID_JSON_BODY = "Invalid JSON body."
ERR_INVALID_UUID = "Invalid UUID for '{field}': {value}"
ERR_PROPERTIES_MUST_BE_ARRAY = "properties_must_be_array"
ERR_ORGANIZATION_SLUG_EXISTS = "An organization with slug '{slug}' already exists."
ERR_PROJECT_SLUG_EXISTS = "Slug '{slug}' is already taken in this organization."
ERR_ONLY_SUPERUSERS_CAN_CREATE_ORGANIZATIONS = "Only superusers can create organizations."
ERR_ONLY_SUPERUSERS_CAN_UPDATE_ORGANIZATIONS = "Only superusers can update organizations."
ERR_ONLY_SUPERUSERS_CAN_DELETE_ORGANIZATIONS = "Only superusers can delete organizations."
ERR_ONLY_SUPERUSERS_CAN_MANAGE_MEMBERS = "You must be an organization admin to manage members."
ERR_NO_ACCESS_TO_ORGANIZATION = "You do not have access to this organization."
ERR_NO_PERMISSION_TO_DELETE_PROJECT = "You do not have permission to delete this project."
ERR_NO_ACCESS_TO_PROJECT = "You do not have access to this project."
ERR_PROJECT_NOT_FOUND = "Project {project_id} not found."
ERR_NO_PERMISSION_TO_CREATE_PROJECTS = "You do not have permission to create projects in this organization."
ERR_NO_PERMISSION_TO_EDIT_PROJECT = "You do not have permission to edit this project."
ERR_ORGANIZATION_NOT_FOUND = "Organization {organization_id} not found."
ERR_USER_NOT_FOUND = "User with email '{email}' not found."
ERR_INVALID_ROLE = "Invalid role '{role}'. Must be admin, editor, or viewer."

# Repository validation error messages
ERR_ROOT_ID_MUST_BE_UUID = "root_id must be a valid UUID"
ERR_KEY_MUST_BE_NON_EMPTY_MAX_255 = "key must be a non-empty string with max 255 characters"
ERR_VERSION_MUST_BE_NON_EMPTY_MAX_255 = "version must be a non-empty string with max 255 characters"
ERR_EXCLUDE_NODE_ID_MUST_BE_UUID_OR_NONE = "exclude_node_id must be a valid UUID or None"
ERR_NODE_ID_MUST_BE_UUID = "node_id must be a valid UUID"
ERR_ATTRIBUTE_DEF_ID_MUST_BE_UUID = "attribute_def_id must be a valid UUID"
ERR_JSON_VALUE_MUST_BE_JSON_SERIALIZABLE = "json_value must be a JSON-serializable value or None"
ERR_SCHEMA_ID_MUST_BE_UUID = "schema_id must be a valid UUID"
ERR_VALIDATED_SCHEMA_MUST_BE_DICT = "validated_schema must be a dict"
ERR_SCHEMA_KEY_MUST_BE_NON_EMPTY_MAX_30 = "schema_key must be a non-empty string with max 30 characters"
ERR_SCHEMA_VERSION_MUST_BE_NON_EMPTY_MAX_20 = "schema_version must be a non-empty string with max 20 characters"
ERR_SCHEMA_STATUS_MUST_BE_VALID = "schema_status must be one of: draft, published, archived"
ERR_OVERWRITE_MUST_BE_BOOLEAN = "overwrite must be a boolean"
ERR_KEY_MUST_BE_NON_EMPTY_MAX_30 = "key must be a non-empty string with max 30 characters"
ERR_VERSION_MUST_BE_NON_EMPTY_MAX_20 = "version must be a non-empty string with max 20 characters"
ERR_PROJECT_ID_REQUIRED = "project_id is required"
ERR_NODE_TYPE_NAME_MUST_BE_NON_EMPTY_MAX_255 = "node_type_name must be a non-empty string with max 255 characters"
ERR_JSON_KEY_MUST_BE_NON_EMPTY_MAX_255 = "json_key must be a non-empty string with max 255 characters"
ERR_VALUE_MUST_BE_JSON_SERIALIZABLE = "value must be a JSON-serializable value"
ERR_DOMAIN_NAME_MUST_BE_NON_EMPTY_MAX_255_OR_NONE = "domain_name must be a non-empty string with max 255 characters or None"
ERR_NODE_TYPE_MUST_BE_NON_EMPTY_MAX_255 = "node_type must be a non-empty string with max 255 characters"

# Status domain validation error messages
ERR_PUBLISHED_STATUS_NOT_AVAILABLE = "published_status_not_available: 'published' value not defined in status domain"
ERR_ARCHIVED_STATUS_NOT_AVAILABLE = "archived_status_not_available: 'archived' value not defined in status domain"
ERR_DRAFT_STATUS_NOT_AVAILABLE = "draft_status_not_available: 'draft' value not defined in status domain"
ERR_SCHEMA_NOT_IN_DRAFT_STATUS = "schema_not_in_draft_status: Current status is '{status}', expected 'draft'"
ERR_SCHEMA_NOT_PUBLISHED_STATUS = "schema_not_published: Current status is '{current_status}', expected 'published'"
ERR_SCHEMA_NOT_ARCHIVED_STATUS = "schema_not_archived: Current status is '{current_status}', expected 'archived'"

# Admin view error messages
ERR_NODE_ID_REQUIRED = "node_id_required"
ERR_ID_REQUIRED = "id is required"
ERR_KEY_AND_VERSION_REQUIRED = "key_and_version_required"
ERR_UNEXPECTED_ERROR = "Unexpected error: {error}"

# BuildState admin error messages
ERR_INVALID_NODE_ID = "invalid_node_id"
ERR_NODE_NOT_FOUND = "node_not_found"
ERR_INCOMPLETE_SCHEMA = "incomplete_schema"
ERR_NOT_A_ROOT_NODE = "not_a_root_node"
ERR_PUBLISH_FAILED = "publish_failed"
ERR_REBUILD_FAILED = "rebuild_failed"

# Composition admin error messages
ERR_PARENT_AND_CHILD_REQUIRED = "parent_and_child_required"
ERR_ALREADY_EXISTS = "already_exists"

# Node editor error messages
ERR_SCHEMA_NOT_FOUND = "schema_not_found"
ERR_INTERNAL_SERVER_ERROR = "internal_server_error"
ERR_NAME_REQUIRED = "name_required"
ERR_PARENT_ID_AND_NODE_TYPE_REQUIRED = "parent_id_and_node_type_required"
ERR_MAX_CHILDREN_REACHED = "max_children_reached"
ERR_NODE_TYPE_REQUIRED = "node_type_required"
ERR_NODE_ID_AND_NEW_PARENT_ID_REQUIRED = "node_id_and_new_parent_id_required"
ERR_NODE_ID_AND_DIRECTION_REQUIRED = "node_id_and_direction_required"
ERR_NODE_ID_REQUIRED_MSG = "node_id required"

# Schema admin error messages
ERR_SCHEMA_TEXT_REQUIRED = "schema_text_required"
ERR_SCHEMA_TYPE_REQUIRED = "schema_type_required"
ERR_INVALID_SCHEMA_TYPE = "invalid_schema_type"
ERR_INVALID_SCHEMA = "invalid_schema"
ERR_IMPORT_FAILED = "import_failed"

# Logging error messages
ERR_INVALID_JSON_MSG = "Invalid JSON: {error}"
ERR_ERROR_IN_SCHEMA_VIEW = "Error in schema_view: {error}"
ERR_UNEXPECTED_ERROR_IN_API_TREE = "Unexpected error in api_tree: {error}"
ERR_UNEXPECTED_ERROR_IN_API_PROPERTIES = "Unexpected error in api_properties: {error}"
ERR_UNEXPECTED_ERROR_IN_API_CREATE = "Unexpected error in api_create: {error}"
ERR_UNEXPECTED_ERROR_IN_API_DELETE = "Unexpected error in api_delete: {error}"

# Configuration constants
MAX_VERSION_AUTO_INCREMENT_ATTEMPTS = 100

# Schema naming suffixes
SCHEMA_KEY_SUFFIX = "_key"
SCHEMA_METADATA_SUFFIX = "_metadata"

# Node service error messages
ERR_SCHEMA_KEY_VERSION_EXISTS = "A schema with key='{key}' and version='{version}' already exists"
ERR_MIN_CHILDREN_VIOLATION = "min_children_violation: Cannot delete, minimum {min_children} {node_type}(s) required per {parent_type}"

import uuid
from typing import Optional

from django.db import DatabaseError, transaction

from ..models import Node
from ..repositories.schema_repository import SchemaRepository
from .permission_service import PermissionService
from ..repositories.project_repository import ProjectRepository
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
        schema_version: str,
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
        schema_id = self.repository.import_schema(
            validated_schema,
            schema_key,
            schema_version,
            schema_status,
            overwrite,
            project_id=project_id,
            organization_id=organization_id,
        )
        return schema_id
    
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
        metadata_node = self.repository.create_node(
            parent=root_node,
            node_type=metadata_type,
            sort_order=meta_pos,
            name='metadata',
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

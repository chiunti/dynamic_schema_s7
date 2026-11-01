"""Delete schemas for API consumers: persisted draft root trees and their caches."""

import uuid

from django.db import transaction

from ..constants import ERR_NO_PERMISSION_TO_EDIT_PROJECT, ERR_PROJECT_ID_REQUIRED, STATUS_DRAFT
from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.schema_repository import NodeRepository, SchemaRepository
from .permission_service import PermissionService


class SchemaDeleteService:
    """Service that deletes a draft schema identified by node type, key and version."""

    def __init__(self) -> None:
        self.repository = SchemaRepository()
        self.node_type_repository = NodeTypeRepository()
        self.node_repository = NodeRepository()
        self.permission_service = PermissionService()

    @transaction.atomic
    def delete_draft(self, node_type_value: str, key: str, version: str,
                     project_id: uuid.UUID, user=None) -> bool:
        """Delete the persisted root node tree of a draft schema.

        Resolves the root node type (by name, falling back to the
        ``json_scope`` like the publish flow), then loads the root node for
        ``key``/``version`` scoped to ``project_id`` and verifies its
        ``status`` attribute is exactly ``draft`` before deleting the whole
        node tree via ``s7_delete_node_tree``. The ``BuildState`` and
        ``SchemaCache`` rows of the ``(key, version, project)`` tuple are
        removed alongside so no servable leftovers remain — deleting the
        draft also releases the node types it implemented.

        Args:
            node_type_value: NodeType name or ``json_scope`` of the schema.
            key: Schema key.
            version: Schema version.
            project_id: Owning project that scopes the lookup and delete.
            user: Optional end user (user MCP token principal). When
                provided, project edit permission is checked before the
                node lookup so out-of-scope projects and read-only
                members fail deterministically.

        Returns:
            ``True`` when the draft was deleted, ``False`` when the node
            type is not a root type, the schema does not exist in the
            project, or its status is not ``draft`` — the caller maps
            ``False`` to the same RFC-7807 404 envelope as an absent
            schema, so published/archived schemas are indistinguishable
            from missing ones and are never deleted through this path.

        Raises:
            PermissionError: ``user`` lacks edit permission on
                ``project_id``.
            ValueError: ``project_id`` is missing.
        """
        if not project_id:
            raise ValueError(ERR_PROJECT_ID_REQUIRED)

        node_type = self.node_type_repository.get_node_type_by_name(node_type_value)
        if not node_type:
            node_type = self.node_type_repository.get_root_node_type_by_scope(node_type_value)
        if not node_type or not node_type.is_root:
            return False

        if user is not None and not self.permission_service.can_edit_project(user, project_id):
            raise PermissionError(ERR_NO_PERMISSION_TO_EDIT_PROJECT)

        node = self.repository.get_root_node_by_key_version(key, version, project_id=project_id, node_type=node_type)
        if not node or node.node_type_id != node_type.id:
            return False

        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        status_attr = self.repository.get_node_attribute(node, status_def) if status_def else None
        if not status_attr or status_attr.value_string != STATUS_DRAFT:
            return False

        self.node_repository.delete_node_tree(node.id)
        self.repository.delete_build_state(key, version, project_id=project_id)
        self.repository.delete_schema_cache_by_key_version(key, version, project_id=project_id)
        return True

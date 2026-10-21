"""Read schemas for API consumers: published cache entries and persisted drafts."""

import uuid
from typing import Any, Optional

from ..constants import ERR_NO_ACCESS_TO_PROJECT, ERR_PROJECT_ID_REQUIRED, STATUS_DRAFT
from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.schema_repository import NodeRepository, SchemaRepository
from .permission_service import PermissionService


class SchemaReadService:
    def __init__(self) -> None:
        self.repository = SchemaRepository()
        self.node_type_repository = NodeTypeRepository()
        self.node_repository = NodeRepository()
        self.permission_service = PermissionService()

    def get_published(self, node_type: str, key: str, version: str,
                      project_id: uuid.UUID, user=None) -> Optional[dict[str, Any] | str]:
        """Return the cached JSON of a published schema.

        Serves the pre-built ``SchemaCache`` entry for ``key``/``version``
        scoped to ``project_id``, joined against the published view so
        non-published schemas and node-type mismatches resolve to
        ``None`` — the same RFC-7807 404 envelope as an absent schema.

        Args:
            node_type: NodeType name of the schema.
            key: Schema key.
            version: Schema version.
            project_id: Owning project that scopes the lookup.
            user: Optional end user (user MCP token principal). When
                provided, project access is checked before the cache
                lookup so out-of-scope projects fail deterministically.

        Returns:
            The cached published JSON (dict or JSON string), or ``None``
            when the schema does not exist, is not published, or its
            node type does not match.

        Raises:
            PermissionError: ``user`` has no access to ``project_id``.
            ValueError: ``project_id`` is missing.
        """
        if not project_id:
            raise ValueError(ERR_PROJECT_ID_REQUIRED)

        if user is not None and not self.permission_service.can_access_project(user, project_id):
            raise PermissionError(ERR_NO_ACCESS_TO_PROJECT)

        return self.repository.get_published_schema(node_type, key, version, project_id)

    def get_draft(self, node_type_value: str, key: str, version: str,
                  project_id: uuid.UUID, user=None) -> Optional[dict[str, Any] | str]:
        """Return the persisted JSON of a draft schema root node.

        Resolves the root node type (by name, falling back to the
        ``json_scope`` like the publish flow), then loads the root node for
        ``key``/``version`` scoped to ``project_id`` and verifies its
        ``status`` attribute is exactly ``draft`` before rebuilding the
        JSON with ``s7_build_node_json`` — which works on any node and does
        not depend on the published cache.

        Args:
            node_type_value: NodeType name or ``json_scope`` of the schema.
            key: Schema key.
            version: Schema version.
            project_id: Owning project that scopes the lookup.
            user: Optional end user (user MCP token principal). When
                provided, project access is checked before the node
                lookup so out-of-scope projects fail deterministically.

        Returns:
            The built node JSON (dict or JSON string), or ``None`` when
            the node type is not a root type, the schema does not exist
            in the project, or its status is not ``draft``.

        Raises:
            PermissionError: ``user`` has no access to ``project_id``.
        """
        if not project_id:
            raise ValueError(ERR_PROJECT_ID_REQUIRED)

        node_type = self.node_type_repository.get_node_type_by_name(node_type_value)
        if not node_type:
            node_type = self.node_type_repository.get_root_node_type_by_scope(node_type_value)
        if not node_type or not node_type.is_root:
            return None

        if user is not None and not self.permission_service.can_access_project(user, project_id):
            raise PermissionError(ERR_NO_ACCESS_TO_PROJECT)

        node = self.repository.get_root_node_by_key_version(key, version, project_id=project_id)
        if not node or node.node_type_id != node_type.id:
            return None

        status_def = self.repository.get_attribute_def(node.node_type, 'status')
        status_attr = self.repository.get_node_attribute(node, status_def) if status_def else None
        if not status_attr or status_attr.value_string != STATUS_DRAFT:
            return None

        return self.node_repository.build_node_json(node.id)

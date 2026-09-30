"""Service for schema publish API requests."""

from django.db import transaction

from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.project_repository import ProjectRepository
from ..repositories.schema_repository import SchemaRepository
from ..services.permission_service import PermissionService
from ..services.schema_service import SchemaService


class SchemaPublishService:
    """Service that publishes a schema identified by node type, key and version."""

    def __init__(self):
        self.node_type_repository = NodeTypeRepository()
        self.schema_repository = SchemaRepository()
        self.project_repository = ProjectRepository()
        self.permission_service = PermissionService()
        self.schema_service = SchemaService()

    @transaction.atomic
    def publish(self, node_type_value: str, key: str, version: str, user=None) -> None:
        """Publish a schema by node type, key and version."""
        node_type = self.node_type_repository.get_node_type_by_name(node_type_value)
        if not node_type:
            node_type = self.node_type_repository.get_root_node_type_by_scope(
                node_type_value
            )
        if not node_type or not node_type.is_root:
            raise ValueError(f"Root node type not found for '{node_type_value}'")

        node = self.schema_repository.get_root_node_by_key_version(key, version)
        if not node or node.node_type_id != node_type.id:
            raise ValueError(f"Schema not found for {node_type_value}/{key}/{version}")

        if user is not None and node.project_id:
            project = self.project_repository.get_project_by_id(node.project_id)
            if project and project.organization_id:
                if not self.permission_service.can_edit_organization(
                    user, project.organization_id
                ):
                    raise PermissionError(
                        "You do not have permission to publish this schema."
                    )

        self.schema_service.publish_schema(node)

"""Read published schemas within the authenticated project."""

import uuid
from typing import Any, Optional

from ..repositories.schema_repository import SchemaRepository


class SchemaReadService:
    def __init__(self) -> None:
        self.repository = SchemaRepository()

    def get_published(self, node_type: str, key: str, version: str,
                      project_id: uuid.UUID) -> Optional[dict[str, Any] | str]:
        return self.repository.get_published_schema(node_type, key, version, project_id)

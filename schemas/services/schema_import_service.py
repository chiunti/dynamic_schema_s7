"""Service for schema import API requests."""

import json
import uuid
from typing import Any, Optional

from django.db import transaction

from ..repositories.node_type_repository import NodeTypeRepository
from ..services.schema_service import SchemaService


class SchemaImportService:
    """Service that prepares and executes a schema import from API request data."""

    VALID_STATUSES = ("draft", "published", "archived")

    def __init__(self):
        self.node_type_repository = NodeTypeRepository()
        self.schema_service = SchemaService()

    def _resolve_node_type(self, node_type_value: str):
        """Resolve a root node type by name or json_scope."""
        node_type = self.node_type_repository.get_node_type_by_name(node_type_value)
        if not node_type:
            node_type = self.node_type_repository.get_root_node_type_by_scope(
                node_type_value
            )
        if not node_type or not node_type.is_root:
            raise ValueError(f"Root node type not found for '{node_type_value}'")
        return node_type

    def _resolve_schema(self, raw_schema: Any, node_type) -> dict:
        """Return a schema dict whose single top-level key is the node type name.

        If the supplied JSON is already wrapped under a key that matches the
        resolved node type (by name or json_scope), it is returned as-is.
        A single non-matching top-level key is rejected (likely a payload
        wrapped for a different node type). Otherwise the JSON is treated as
        an unwrapped root object and wrapped under the node type name.
        """
        if not isinstance(raw_schema, dict):
            raise ValueError("schema_text must contain a JSON object")

        if not raw_schema:
            raise ValueError("schema_text cannot be an empty JSON object")

        matching_names = {node_type.name}
        if node_type.json_scope:
            matching_names.add(node_type.json_scope)

        if len(raw_schema) == 1:
            top_key = next(iter(raw_schema.keys()))
            if top_key in matching_names:
                if not isinstance(raw_schema[top_key], dict):
                    raise ValueError("The schema root value must be a JSON object")
                return raw_schema
            raise ValueError(
                f"schema_text root key '{top_key}' does not match node type "
                f"'{node_type.name}' (json_scope: '{node_type.json_scope}')"
            )

        return {node_type.name: raw_schema}

    def _extract_string(self, value: Any, default: str) -> str:
        """Coerce a value to a stripped string, falling back to default."""
        if value is None:
            return default
        result = str(value).strip()
        return result if result else default

    @transaction.atomic
    def import_from_request(self, data: dict, user) -> tuple[uuid.UUID, str]:
        """Import a schema from validated API request data.

        Returns the imported schema root node ID and a warning string.
        """
        schema_text = data["schema_text"]
        try:
            raw_schema = json.loads(schema_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in schema_text: {exc}") from exc

        node_type = self._resolve_node_type(data["node_type"].strip())
        resolved_schema = self._resolve_schema(raw_schema, node_type)

        root_key = next(iter(resolved_schema.keys()))
        inner = resolved_schema[root_key]
        if not isinstance(inner, dict):
            raise ValueError("The schema root value must be a JSON object")

        schema_key = self._extract_string(
            data.get("schema_key") or inner.get("key"),
            "",
        )
        if not schema_key:
            # Fall back to the root object's name, truncated to the
            # schema_nodes.key column limit (30 chars).
            schema_key = self._extract_string(inner.get("name"), "")[:30]
        if not schema_key:
            raise ValueError(
                "schema_key is required and cannot be determined from the schema"
            )

        schema_version = self._extract_string(
            data.get("schema_version") or inner.get("version"),
            "",
        )

        schema_status = self._extract_string(
            data.get("schema_status") or inner.get("status"),
            "draft",
        )
        if schema_status not in self.VALID_STATUSES:
            raise ValueError(
                f"schema_status must be one of: {', '.join(self.VALID_STATUSES)}"
            )

        overwrite = bool(data.get("overwrite", False))
        project_id: Optional[uuid.UUID] = data.get("project_id")
        organization_id: Optional[uuid.UUID] = data.get("organization_id")

        schema_id, version_warning = self.schema_service.import_schema(
            resolved_schema,
            schema_key,
            schema_version,
            schema_status,
            overwrite,
            project_id=project_id,
            organization_id=organization_id,
            user=user,
        )

        warning = ""
        if version_warning:
            if isinstance(version_warning, dict):
                warning = version_warning.get("message", "") or json.dumps(
                    version_warning
                )
            else:
                warning = str(version_warning)

        return schema_id, warning

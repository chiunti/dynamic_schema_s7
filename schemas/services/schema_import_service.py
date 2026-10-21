"""Service for schema import API requests."""

import json
import uuid
from typing import Any, Optional

from django.db import transaction

from ..constants import (
    ERR_INVALID_JSON,
    ERR_INVALID_REQUEST,
    ERR_INVALID_SCHEMA,
    ERR_SCHEMA_STATUS_MUST_BE_VALID,
    STATUS_DRAFT,
    VALID_SCHEMA_STATUSES,
    WARN_DROPPED_ATTRIBUTE,
    WARN_IMPORT,
    WARN_MISSING_REQUIRED_ATTRIBUTES,
)
from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.project_repository import ProjectRepository
from .exceptions import SchemaValidationError
from .schema_service import SchemaService
from .schema_validation_service import SchemaValidationService


class SchemaImportService:
    """Service that prepares and executes a schema import from API request data."""

    VALID_STATUSES = VALID_SCHEMA_STATUSES

    def __init__(self):
        self.node_type_repository = NodeTypeRepository()
        self.project_repository = ProjectRepository()
        self.schema_service = SchemaService()
        self.validation_service = SchemaValidationService()

    def get_import_form_options(self) -> dict:
        """Return the options needed to render the schema import form.

        Returns:
            Dict with 'root_types' (root NodeTypes) and 'projects'
            (projects ordered by name).
        """
        return {
            "root_types": self.node_type_repository.get_all_root_node_types(),
            "projects": self.project_repository.get_all_projects_ordered(),
        }

    def _resolve_node_type(self, node_type_value: str):
        """Resolve a root node type by name or json_scope."""
        node_type = self.node_type_repository.get_node_type_by_name(node_type_value)
        if not node_type:
            node_type = self.node_type_repository.get_root_node_type_by_scope(
                node_type_value
            )
        if not node_type or not node_type.is_root:
            raise SchemaValidationError(
                f"Root node type not found for '{node_type_value}'",
                code=ERR_INVALID_SCHEMA,
                path="$.node_type",
                expected="existing root node type name or json_scope",
                received=node_type_value,
            )
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
    def import_from_request(
        self, data: dict, user, enforced_status: Optional[str] = None, strict_keys: bool = False,
        dropped_attributes: Optional[list] = None,
    ) -> tuple[uuid.UUID, str, str]:
        """Import a schema from validated API request data.

        ``enforced_status`` (when not ``None``) replaces the caller-requested
        status; ``None`` persists the requested status as-is.

        ``strict_keys`` enables strict mode for credential imports and the
        validate dry-run: the pipeline rejects undeclared JSON keys,
        non-collection values on collection keys and out-of-domain scalars
        with ``SchemaValidationError`` carrying a JSON path.

        ``dropped_attributes`` is an optional out-list: in permissive mode the
        pipeline appends ``{"code", "path", "message"}`` dicts for attribute
        values rejected by datatype validators. The same drops are also
        folded into the returned warning string.

        Returns the imported schema root node ID, a warning string, and the
        status originally requested by the caller (before enforcement).
        """
        schema_text = data.get("schema_text")
        if not isinstance(schema_text, str):
            raise SchemaValidationError(
                "schema_text is required and must be a string",
                code=ERR_INVALID_REQUEST,
                path="$.schema_text",
                expected="non-empty string containing a JSON object",
            )
        try:
            raw_schema = json.loads(schema_text)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                f"Invalid JSON in schema_text: {exc}",
                code=ERR_INVALID_JSON,
                path="$.schema_text",
            ) from exc

        node_type_value = data.get("node_type")
        if not isinstance(node_type_value, str) or not node_type_value.strip():
            raise SchemaValidationError(
                "node_type is required",
                code=ERR_INVALID_REQUEST,
                path="$.node_type",
                expected="existing root node type name or json_scope",
            )
        node_type = self._resolve_node_type(node_type_value.strip())
        try:
            resolved_schema = self._resolve_schema(raw_schema, node_type)
        except SchemaValidationError:
            raise
        except ValueError as exc:
            raise SchemaValidationError(
                str(exc), code=ERR_INVALID_SCHEMA, path="$.schema_text"
            ) from exc

        root_key = next(iter(resolved_schema.keys()))
        inner = resolved_schema[root_key]
        if not isinstance(inner, dict):
            raise SchemaValidationError(
                "The schema root value must be a JSON object",
                code=ERR_INVALID_SCHEMA,
                path="$.schema_text",
            )

        schema_key = self._extract_string(
            data.get("schema_key") or inner.get("key"),
            "",
        )
        if not schema_key:
            # Fall back to the root object's name or id, truncated to the
            # schema_nodes.key column limit (30 chars).
            schema_key = self._extract_string(inner.get("name") or inner.get("id"), "")[:30]
        if not schema_key:
            raise SchemaValidationError(
                "schema_key is required and cannot be determined from the schema",
                code=ERR_INVALID_SCHEMA,
                path="$.schema_key",
                expected="non-empty schema key",
            )

        schema_version = self._extract_string(
            data.get("schema_version") or inner.get("version"),
            "",
        )

        requested_status = self._extract_string(
            data.get("schema_status") or inner.get("status"),
            STATUS_DRAFT,
        )
        if requested_status not in self.VALID_STATUSES:
            raise ValueError(ERR_SCHEMA_STATUS_MUST_BE_VALID)
        if enforced_status is not None and enforced_status not in self.VALID_STATUSES:
            raise ValueError(ERR_SCHEMA_STATUS_MUST_BE_VALID)
        schema_status = enforced_status or requested_status

        overwrite = bool(data.get("overwrite", False))
        project_id: Optional[uuid.UUID] = data.get("project_id")
        organization_id: Optional[uuid.UUID] = data.get("organization_id")

        # Derive the organization from the project when it is not supplied
        # explicitly (e.g. the admin import form only submits a project_id).
        if project_id and not organization_id:
            project = self.project_repository.get_project_by_id(project_id)
            organization_id = project.organization_id if project else None

        # Attribute values rejected by datatype validators land here in
        # permissive mode; under strict_keys they raise instead.
        if dropped_attributes is None:
            dropped_attributes = []

        schema_id, version_warning = self.schema_service.import_schema(
            resolved_schema,
            schema_key,
            schema_version,
            schema_status,
            overwrite,
            project_id=project_id,
            organization_id=organization_id,
            user=user,
            strict_keys=strict_keys,
            dropped_attributes=dropped_attributes,
        )

        warning_parts = []
        if version_warning:
            if isinstance(version_warning, dict):
                warning_parts.append(
                    version_warning.get("message", "") or json.dumps(version_warning)
                )
            else:
                warning_parts.append(str(version_warning))
        warning_parts.extend(
            f"[{WARN_DROPPED_ATTRIBUTE}] {drop['path']}: {drop['message']}"
            for drop in dropped_attributes
        )
        warning = "; ".join(part for part in warning_parts if part)

        return schema_id, warning, requested_status

    def validate_from_request(self, data: dict, user=None) -> dict:
        """Dry-run a schema import and return the structured validation envelope.

        Runs the real import pipeline inside a transaction that is always
        rolled back, so nothing is persisted. The dry-run forces
        ``overwrite=True`` and ``schema_status='draft'`` (it measures
        structural validity, not identity/publication conflicts) and enables
        strict-key mode, as every credential-facing import does.

        Returns:
            ``{"valid": bool, "errors": [...], "warnings": [...]}`` where each
            error carries ``code``, ``message``, ``path``, ``expected`` and
            ``received`` (``None`` when not applicable). Warnings carry the
            import/required-attribute notices plus any ``dropped_attribute``
            dicts collected by the pipeline.
        """
        schema_text = data.get("schema_text")
        if not isinstance(schema_text, str):
            return self._failure(
                code=ERR_INVALID_REQUEST,
                message="schema_text is required and must be a string",
                path="$.schema_text",
                expected="non-empty string containing a JSON object",
            )
        try:
            json.loads(schema_text)
        except json.JSONDecodeError as exc:
            return self._failure(
                code=ERR_INVALID_JSON,
                message=str(exc),
                path="$.schema_text",
                expected="valid JSON object",
            )

        errors: list[dict] = []
        warnings: list[dict] = []
        dropped_attributes: list = []
        with transaction.atomic():
            try:
                schema_id, warning, _requested_status = self.import_from_request(
                    dict(data, overwrite=True),
                    user,
                    enforced_status=STATUS_DRAFT,
                    strict_keys=True,
                    dropped_attributes=dropped_attributes,
                )
            except SchemaValidationError as exc:
                errors.append(exc.to_dict())
            except ValueError as exc:
                errors.append(
                    {
                        "code": ERR_INVALID_SCHEMA,
                        "message": str(exc),
                        "path": None,
                        "expected": None,
                        "received": None,
                    }
                )
            else:
                if warning:
                    warnings.append({"code": WARN_IMPORT, "message": warning})
                # Strict mode raises on validator-rejected attributes, so this
                # stays empty today; appended defensively so the envelope keeps
                # reporting drops as structured warnings if that ever changes.
                warnings.extend(dropped_attributes)
                # collect_required_warnings reads the tree built above; the
                # inner atomic of import_from_request acted as a savepoint,
                # so the outer transaction is still usable after a managed
                # failure as well.
                for item in self.validation_service.collect_required_warnings(schema_id):
                    warnings.append(
                        {
                            "code": WARN_MISSING_REQUIRED_ATTRIBUTES,
                            "node_id": item["node_id"],
                            "node_name": item["node_name"],
                            "node_type": item["node_type"],
                            "missing": item["missing"],
                            "message": f"Missing required attributes: {', '.join(item['missing'])}",
                        }
                    )
            # Dry-run: never persist — discard everything written above, on
            # success and on managed failure alike. Unhandled exceptions
            # propagate and roll back implicitly.
            transaction.set_rollback(True)

        return {"valid": not errors, "errors": errors, "warnings": warnings}

    def _failure(
        self,
        code: str,
        message: str,
        path: Optional[str] = None,
        expected: Any = None,
        received: Any = None,
    ) -> dict:
        """Build a failed validation envelope holding a single structured error."""
        return {
            "valid": False,
            "errors": [
                {
                    "code": code,
                    "message": message,
                    "path": path,
                    "expected": expected,
                    "received": received,
                }
            ],
            "warnings": [],
        }

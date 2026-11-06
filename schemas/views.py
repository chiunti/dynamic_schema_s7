import json
import logging
from uuid import UUID

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import (
    CanAuthorCatalog,
    CanImportSchema,
    CanProposeChangeset,
    CanPublishSchema,
    CanReadSchema,
    ProjectPrincipal,
    ProjectTokenAuthentication,
    UserMCPTokenAuthentication,
    UserMCPTokenPermission,
)
from .constants import (
    ERR_CHANGESET_SCOPES_NOT_COVERED,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_INVALID_REQUEST,
    ERR_PROJECT_CREDENTIAL_MISMATCH,
    ERR_PROJECT_ID_REQUIRED,
    STATUS_DRAFT,
)
from .serializers import (
    CatalogChangeSetProposeSerializer,
    CatalogImportSerializer,
    SchemaImportSerializer,
)
from .services.catalog_changeset_service import CatalogChangeSetService
from .services.catalog_import_service import CatalogImportService
from .services.catalog_service import CatalogService
from .services.exceptions import (
    CatalogObjectNotFoundError,
    ChangesetNotFoundError,
    ChangesetStateConflictError,
    ScopeNotFoundError,
)
from .services.schema_delete_service import SchemaDeleteService
from .services.schema_import_service import SchemaImportService
from .services.schema_publish_service import SchemaPublishService
from .services.schema_read_service import SchemaReadService
from .utils import parse_uuid

logger = logging.getLogger(__name__)


def _resolve_project_scope(request: Request) -> tuple | Response:
    """Resolve the project scope for schema read and draft-delete endpoints.

    Project credentials are bound to their own project: an optional
    ``project_id`` query param is accepted only as an echo-check and must
    match the credential's project. User MCP tokens delegate to the real
    user and must declare ``project_id`` explicitly so the service can
    verify access on the target project.

    Returns ``(project_id, user)`` on success — ``user`` is ``None`` for
    project credentials and ``request.user`` for MCP tokens — or a
    ``Response`` (400/403) the caller must return unchanged.
    """
    if isinstance(request.user, ProjectPrincipal):
        project_id = request.user.project_id
        requested_project_id = request.query_params.get('project_id')
        if requested_project_id:
            try:
                requested_uuid = parse_uuid(requested_project_id, "project_id")
            except ValueError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            if requested_uuid != project_id:
                return Response({'error': ERR_PROJECT_CREDENTIAL_MISMATCH},
                                status=status.HTTP_403_FORBIDDEN)
        return project_id, None
    # User MCP tokens delegate to the real user: project_id is
    # required so the service can check access on the target project.
    requested_project_id = request.query_params.get('project_id')
    if not requested_project_id:
        return Response({'error': ERR_PROJECT_ID_REQUIRED},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        project_id = parse_uuid(requested_project_id, "project_id")
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return project_id, request.user


def _first_serializer_error(serializer) -> str:
    """Format the first serializer failure as ``<field>: <message>``.

    ListField children report per-item failures as
    ``{item_index: [messages]}`` — those render as ``field[index]`` so the
    caller can point at the offending entry.
    """
    field, field_errors = next(iter(serializer.errors.items()))
    if isinstance(field_errors, dict):
        index = next(iter(field_errors))
        item_errors = field_errors[index]
        detail = (
            item_errors[0]
            if isinstance(item_errors, (list, tuple))
            else item_errors
        )
        return f"{field}[{index}]: {detail}"
    detail = (
        field_errors[0]
        if isinstance(field_errors, (list, tuple))
        else field_errors
    )
    return f"{field}: {detail}"


def _serialize_changeset(changeset) -> dict:
    """Serialize a changeset instance for the REST envelope.

    Reads the ``*_id`` FK attributes rather than the related objects so no
    extra queries fire; datetimes render ISO-8601 and nullable fields emit
    ``None``.
    """
    return {
        "id": str(changeset.id),
        "status": changeset.status,
        "ops": changeset.ops,
        "rationale": changeset.rationale,
        "scopes": changeset.scopes,
        "base_revision": changeset.base_revision,
        "validation_report": changeset.validation_report,
        "post_apply_report": changeset.post_apply_report,
        "proposed_by_credential": (
            str(changeset.proposed_by_credential_id)
            if changeset.proposed_by_credential_id
            else None
        ),
        "proposed_by_user": (
            str(changeset.proposed_by_user_id)
            if changeset.proposed_by_user_id
            else None
        ),
        "reviewed_by": (
            str(changeset.reviewed_by_id)
            if changeset.reviewed_by_id
            else None
        ),
        "review_note": changeset.review_note,
        "applied_at": (
            changeset.applied_at.isoformat() if changeset.applied_at else None
        ),
        "created_at": (
            changeset.created_at.isoformat() if changeset.created_at else None
        ),
        "updated_at": (
            changeset.updated_at.isoformat() if changeset.updated_at else None
        ),
    }


class SchemaView(APIView):
    """Serve the published schema JSON to credentials or user MCP tokens.

    Counterpart of ``SchemaDraftView`` for the ``published`` lifecycle
    status: while the draft endpoint rebuilds the persisted node JSON on
    every call, this endpoint serves the pre-built ``SchemaCache`` entry
    maintained by the publish flow. ``project_id`` is a security scope,
    not a cosmetic filter — project credentials are bound to their own
    project, and user MCP tokens must declare it explicitly via the
    ``project_id`` query param so the service can verify project access
    before the lookup. A schema missing, not published, or under a
    different node type resolves to the same RFC-7807 404 envelope.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanReadSchema | UserMCPTokenPermission]

    def get(self, request, node_type, key, version):
        resolved = _resolve_project_scope(request)
        if isinstance(resolved, Response):
            return resolved
        project_id, user = resolved
        try:
            schema_json = SchemaReadService().get_published(
                node_type, key, version, project_id, user=user
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Schema retrieval failed")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if not schema_json:
            return Response({
                "type": "schema_not_found",
                "title": "Schema Not Found",
                "detail": "Schema not found, not published, or node type mismatch",
                "status": 404,
            }, status=status.HTTP_404_NOT_FOUND)

        # Convert JSONB string to Python dict to avoid double serialization
        schema_dict = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
        return Response({
            "data": schema_dict,
            "meta": {"node_type": node_type, "key": key, "version": version},
        })


class SchemaDraftView(APIView):
    """Serve the persisted JSON of a draft schema to credentials or user MCP tokens.

    Counterpart of ``SchemaView`` for the ``draft`` lifecycle status: while
    the published endpoint serves the pre-built cache, this endpoint
    rebuilds the persisted node JSON via ``s7_build_node_json`` so API
    consumers can verify what an import actually stored (round-trip check).
    A schema missing or in any other status resolves to the same RFC-7807
    404 envelope — drafts are not distinguishable from absent schemas.

    DELETE is the destructive counterpart of the draft import: it removes
    the root node tree plus the ``BuildState``/``SchemaCache`` rows of the
    ``(key, version, project)`` tuple, releasing the node types the draft
    implements. The verb is restricted to user MCP tokens holding edit
    rights on the target project — project credentials fail the
    permission check — and non-draft schemas resolve to the same 404
    envelope without being touched.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanReadSchema | UserMCPTokenPermission]

    def get_permissions(self):
        # DELETE is destructive and user-token only: project credentials
        # carry no ``mcp_level`` and fail the duck-typed check (403).
        if self.request.method == "DELETE":
            return [UserMCPTokenPermission()]
        return super().get_permissions()

    def get(self, request, node_type, key, version):
        resolved = _resolve_project_scope(request)
        if isinstance(resolved, Response):
            return resolved
        project_id, user = resolved
        try:
            schema_json = SchemaReadService().get_draft(
                node_type, key, version, project_id, user=user
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Draft schema retrieval failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        if not schema_json:
            return Response({
                "type": "schema_not_found",
                "title": "Schema Not Found",
                "detail": "Schema not found, not in draft status, or node type mismatch",
                "status": 404,
            }, status=status.HTTP_404_NOT_FOUND)

        # Convert JSONB string to Python dict to avoid double serialization
        schema_dict = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
        return Response({
            "data": schema_dict,
            "meta": {
                "node_type": node_type,
                "key": key,
                "version": version,
                "status": STATUS_DRAFT,
            },
        })

    def delete(self, request, node_type, key, version):
        resolved = _resolve_project_scope(request)
        if isinstance(resolved, Response):
            return resolved
        project_id, user = resolved
        try:
            deleted = SchemaDeleteService().delete_draft(
                node_type, key, version, project_id, user=user
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Draft schema deletion failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        if not deleted:
            return Response({
                "type": "schema_not_found",
                "title": "Schema Not Found",
                "detail": "Schema not found, not in draft status, or node type mismatch",
                "status": 404,
            }, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "deleted": True,
            "meta": {"node_type": node_type, "key": key, "version": version},
        })


class SchemaImportView(APIView):
    """DRF endpoint to import a schema JSON via project credential or user MCP token.

    Credential imports are persisted as ``draft`` unless the credential
    carries publish scope; user MCP token imports are always persisted as
    ``draft`` — MCP never publishes. Publishing remains exclusive to the
    publish endpoint.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanImportSchema | UserMCPTokenPermission]

    def post(self, request):
        serializer = SchemaImportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        if isinstance(request.user, ProjectPrincipal):
            if ((data.get('project_id') and data['project_id'] != request.user.project_id)
                    or (data.get('organization_id') and data['organization_id'] != request.user.organization_id)):
                return Response({'error': ERR_PROJECT_CREDENTIAL_MISMATCH},
                                status=status.HTTP_403_FORBIDDEN)
            data['project_id'] = request.user.project_id
            data['organization_id'] = request.user.organization_id
            # Credentials without publish scope import as draft; publishing requires /publish/.
            enforced_status = None if getattr(request.auth, "can_publish", False) else STATUS_DRAFT
            user = None
        else:
            # User MCP tokens delegate to the real user: project_id is required
            # (the service derives the organization from it and checks edit
            # permission) and MCP never publishes — always forced to draft.
            if not data.get('project_id'):
                return Response({'error': ERR_PROJECT_ID_REQUIRED},
                                status=status.HTTP_400_BAD_REQUEST)
            enforced_status = STATUS_DRAFT
            user = request.user
        try:
            schema_id, warning, requested_status = SchemaImportService().import_from_request(
                data, user=user, enforced_status=enforced_status, strict_keys=True
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except (ValueError, RuntimeError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Schema import failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        payload = {"schema_id": str(schema_id), "warning": warning or ""}
        if enforced_status is not None and requested_status != enforced_status:
            payload["forced_status"] = enforced_status
        return Response(payload, status=status.HTTP_201_CREATED)


class SchemaValidateView(APIView):
    """DRF endpoint to validate a schema JSON without persisting it.

    Runs the import pipeline as a dry-run and returns the structured
    ``{valid, errors, warnings}`` envelope so programmatic consumers can
    iterate on a payload before importing it.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanImportSchema | UserMCPTokenPermission]

    def post(self, request):
        serializer = SchemaImportSerializer(data=request.data)
        if not serializer.is_valid():
            errors = []
            for field, field_errors in serializer.errors.items():
                received = request.data.get(field) if isinstance(request.data, dict) else None
                errors.append({
                    "code": ERR_INVALID_REQUEST,
                    "message": f"{field}: {field_errors[0]}",
                    "path": f"$.{field}",
                    "expected": None,
                    # Truncate: schema_text can be a whole JSON document.
                    "received": str(received)[:200] if received is not None else None,
                })
            return Response(
                {"valid": False, "errors": errors, "warnings": []},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        if isinstance(request.user, ProjectPrincipal):
            if ((data.get('project_id') and data['project_id'] != request.user.project_id)
                    or (data.get('organization_id') and data['organization_id'] != request.user.organization_id)):
                return Response({'error': ERR_PROJECT_CREDENTIAL_MISMATCH},
                                status=status.HTTP_403_FORBIDDEN)
            data['project_id'] = request.user.project_id
            data['organization_id'] = request.user.organization_id
            user = None
        else:
            # User MCP tokens delegate to the real user; project_id is required
            # so the dry-run can evaluate org edit permission on the target.
            if not data.get('project_id'):
                return Response({'error': ERR_PROJECT_ID_REQUIRED},
                                status=status.HTTP_400_BAD_REQUEST)
            user = request.user
        try:
            result = SchemaImportService().validate_from_request(data, user=user)
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except Exception:
            logger.exception("Schema validation failed")
            return Response(
                {
                    "valid": False,
                    "errors": [{
                        "code": ERR_INTERNAL_SERVER_ERROR,
                        "message": "Internal server error",
                        "path": None,
                        "expected": None,
                        "received": None,
                    }],
                    "warnings": [],
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(result, status=status.HTTP_200_OK)


class SchemaPublishView(APIView):
    """DRF endpoint to publish a schema using project credentials."""

    authentication_classes = [ProjectTokenAuthentication]
    permission_classes = [CanPublishSchema]

    def post(self, request, node_type, key, version):
        try:
            SchemaPublishService().publish(
                node_type, key, version, project_id=request.user.project_id
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except (ValueError, RuntimeError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Schema publish failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({"ok": True}, status=status.HTTP_200_OK)


class CatalogScopeView(APIView):
    """Serve the resolved catalog for a json_scope to project credentials or user MCP tokens.

    An unknown scope answers 404 with ``{"error": "scope_not_found",
    "available_scopes": [...]}`` — ``available_scopes`` lists every live
    root scope so agents can self-correct.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanReadSchema | UserMCPTokenPermission]

    def get(self, request, scope):
        try:
            catalog = CatalogService().get_resolved_catalog(scope)
        except ScopeNotFoundError as exc:
            return Response(
                {"error": str(exc), "available_scopes": exc.available_scopes},
                status=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.exception("Catalog retrieval failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        return Response(catalog, status=status.HTTP_200_OK)


class CatalogDatatypesView(APIView):
    """Serve the universal DataType catalog to project credentials or user MCP tokens."""

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanReadSchema | UserMCPTokenPermission]

    def get(self, request):
        try:
            datatypes = CatalogService().get_datatypes()
        except Exception:
            logger.exception("Datatypes catalog retrieval failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        return Response(datatypes, status=status.HTTP_200_OK)


class CatalogImportView(APIView):
    """MCP level-2 endpoint for catalog grammar authoring.

    Accepts a non-empty list of fixture-format objects and applies them
    atomically: upsert objects (``{"model": ..., "pk"?: ..., "fields":
    ...}``) are matched by natural key — a match updates the row by
    full-row replace, no match creates it — while delete objects
    (``{"model": ..., "delete": true}`` with ``pk`` XOR natural-key
    ``fields``) remove the row they identify. Deletes reuse the update
    implementation gate: a row already instantiated by schema data is
    rejected as ``catalog_object_in_use``. Every delete also requires
    the ``schemas.delete_<label>`` permission — a ``nodetype`` delete
    demands it for each own dependent (attribute defs, variants,
    compositions) removed alongside. The grammar is global and live
    immediately — there is no draft staging. Success relays the service
    envelope verbatim: ``{applied, created, updated, deleted, results,
    warnings}``.

    Authentication is restricted to user MCP tokens: a project credential
    Bearer token is not resolved by this authenticator, so the request
    falls through to anonymous and is rejected, and the permission
    requires ``mcp_level >= 2`` which only user tokens carry.
    """

    authentication_classes = [UserMCPTokenAuthentication]
    permission_classes = [CanAuthorCatalog]

    def post(self, request: Request) -> Response:
        serializer = CatalogImportSerializer(data=request.data)
        if not serializer.is_valid():
            errors = []
            for field, field_errors in serializer.errors.items():
                if isinstance(field_errors, dict):
                    # ListField reports per-item failures as
                    # {item_index: [messages]} — point at the entry.
                    index = next(iter(field_errors))
                    item_errors = field_errors[index]
                    detail = (
                        item_errors[0]
                        if isinstance(item_errors, (list, tuple))
                        else item_errors
                    )
                    message = f"{field}[{index}]: {detail}"
                    path = f"$.{field}[{index}]"
                else:
                    message = f"{field}: {field_errors[0]}"
                    path = f"$.{field}"
                errors.append({
                    "code": ERR_INVALID_REQUEST,
                    "message": message,
                    "path": path,
                })
            return Response(
                {"applied": False, "errors": errors, "warnings": []},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        try:
            result = CatalogImportService().import_objects(
                data["objects"], user=request.user
            )
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except Exception:
            logger.exception("Catalog import failed")
            return Response(
                {
                    "applied": False,
                    "errors": [{
                        "code": ERR_INTERNAL_SERVER_ERROR,
                        "message": "Internal server error",
                        "path": None,
                    }],
                    "warnings": [],
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            result,
            status=(
                status.HTTP_200_OK if result["applied"]
                else status.HTTP_400_BAD_REQUEST
            ),
        )


class CatalogUsageView(APIView):
    """Usage pre-check for one catalog object (AI-143).

    Exposes the ``catalog_object_in_use`` gate's calculation as a read so
    an L2 agent can check before proposing updates/deletes instead of
    discovering implementation by rejection. Identification mirrors the
    delete objects of ``CatalogImportView``:
    ``?model=schemas.<label>&pk=<uuid>`` XOR
    ``?model=schemas.<label>&<natural-key components>`` (each NK position
    travels as its own query param; omit it to address a NULL position).
    Success relays the service result verbatim — ``{"implemented": bool,
    "usage": {<metric>: int, "projects": [...]}}``, the same evidence a
    rejected import would report.

    Authentication is dual: user MCP tokens require ``mcp_level >= 2``
    (``CanAuthorCatalog`` — the usage check is catalog-authoring
    metadata) and project credentials require ``can_read``. The
    calculation is global and advisory — no project scoping, same
    TOCTOU caveat as the import gate.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanReadSchema | CanAuthorCatalog]

    def get(self, request: Request) -> Response:
        params = request.query_params
        try:
            result = CatalogImportService().get_object_usage(
                params.get('model'),
                pk=params.get('pk'),
                nk_params={
                    name: params[name]
                    for name in params
                    if name not in ('model', 'pk')
                },
            )
        except CatalogObjectNotFoundError as exc:
            return Response(
                {
                    "error": str(exc),
                    "model": exc.model,
                    "identifier": exc.identifier,
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        except ValueError as exc:
            return Response(
                {"error": ERR_INVALID_REQUEST, "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Catalog usage check failed")
            return Response(
                {"error": "Internal server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(result, status=status.HTTP_200_OK)


class CatalogChangeSetListCreateView(APIView):
    """Propose a governed catalog mutation as a changeset (AI-22).

    ``POST`` accepts ``{"ops": [...], "rationale": "..."}``, persists the
    changeset and runs validation synchronously in-request — the 201
    response carries the final ``status`` (``valid``/``invalid``) and the
    full ``validation_report``, which is the agent's feedback channel: a
    validation failure is still a persisted 201, not a 400. Only a
    malformed request body (missing ``rationale``, empty/non-list
    ``ops``) answers 400 with the first serializer error.

    When the token also carries ``can_auto_apply_additive`` (AI-33) and
    the validated payload is purely additive — every op an ``add_*``
    with no ``warning``/``breaking`` findings — the service chains
    approve+apply inside the same transaction, so the 201 may carry
    ``status`` ``applied`` directly (or ``stale`` when an apply-time
    gate detects catalog drift), with ``reviewed_by`` set to the
    token's user and an ``auto`` marker on the review note. Every
    other outcome still waits for human review — the envelope shape
    is unchanged.

    The gate is ``CanProposeChangeset``: no token answers DRF's JSON
    401; a project credential, an L1 token or a token without the
    ``can_propose_changeset`` flag fails the duck-typed ``request.auth``
    checks with 403. A second 403 may fire after validation, once the
    changeset's ``scopes`` are derived server-side, when the token's
    ``allowed_scopes`` do not cover them — the service raises
    ``PermissionError`` through ``scope_check`` and the proposal rolls
    back, so nothing persists.

    The collection implements ``POST`` only — a paginated list is future
    work. ``approve``/``apply``/``reject`` are intentionally absent
    here: they belong to the human review surface (admin — AI-23) and
    are never exposed to credentials.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanProposeChangeset]

    def post(self, request: Request) -> Response:
        serializer = CatalogChangeSetProposeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error": ERR_INVALID_REQUEST,
                    "message": _first_serializer_error(serializer),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        try:
            changeset = CatalogChangeSetService().propose(
                data["ops"],
                data["rationale"],
                proposed_by_user=request.user,
                scope_check=lambda scopes: CanProposeChangeset.covers_scopes(
                    request.auth, scopes
                ),
                auto_apply_additive=CanProposeChangeset.can_auto_apply(
                    request.auth
                ),
            )
        except PermissionError as exc:
            return Response(
                {
                    "error": ERR_CHANGESET_SCOPES_NOT_COVERED,
                    "message": str(exc),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValueError as exc:
            return Response(
                {"error": ERR_INVALID_REQUEST, "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Changeset propose failed")
            return Response(
                {"error": "Internal server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            _serialize_changeset(changeset), status=status.HTTP_201_CREATED
        )


class CatalogChangeSetDetailView(APIView):
    """Read and re-propose a single changeset (AI-22).

    ``GET`` returns the serialized changeset — ``status`` plus the full
    ``validation_report`` so the proposing agent can read its findings;
    an unknown id answers 404 echoing the addressed ``changeset_id``.

    ``PATCH`` takes the same ``{"ops", "rationale"}`` body as the
    propose endpoint: it replaces the mutation document on the same
    changeset and re-runs validation synchronously (the corrected-ops
    feedback loop) — and the same ``can_auto_apply_additive`` arm as
    the propose applies (AI-33): a purely-additive revision may chain
    approve+apply in-request, so the 200 may carry ``applied`` (or
    ``stale`` on apply-gate drift) instead of ``valid``. The
    auto-apply only runs when the acting user IS the changeset's
    proposer — PATCH stays a shared document (any admitted principal
    may correct another proposer's payload), but a cross-proposer
    patch's result waits for human review. PATCH requires
    a ``→ validating`` edge on the current status — ``draft``,
    ``validating``, ``approved``, ``applied``, ``rejected`` and
    ``superseded`` answer 409 with the invalid-transition message.

    Same gate as the collection view (``CanProposeChangeset``): 401
    without a token, 403 for credentials/L1/flag-less tokens, plus a
    per-instance 403 when the token's ``allowed_scopes`` do not cover
    the changeset's derived ``scopes`` — enforced on ``GET`` after the
    fetch (``ERR_CHANGESET_SCOPES_NOT_COVERED`` envelope) and through
    ``scope_check`` inside ``PATCH``.
    """

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanProposeChangeset]

    def get(self, request: Request, changeset_id: UUID) -> Response:
        try:
            changeset = CatalogChangeSetService().get_changeset(changeset_id)
        except ChangesetNotFoundError as exc:
            return Response(
                {"error": str(exc), "changeset_id": str(exc.changeset_id)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except ValueError as exc:
            return Response(
                {"error": ERR_INVALID_REQUEST, "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Changeset retrieval failed")
            return Response(
                {"error": "Internal server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if not CanProposeChangeset.covers_scopes(request.auth, changeset.scopes):
            return Response(
                {
                    "error": ERR_CHANGESET_SCOPES_NOT_COVERED,
                    "scopes": changeset.scopes,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            _serialize_changeset(changeset), status=status.HTTP_200_OK
        )

    def patch(self, request: Request, changeset_id: UUID) -> Response:
        serializer = CatalogChangeSetProposeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "error": ERR_INVALID_REQUEST,
                    "message": _first_serializer_error(serializer),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        try:
            changeset = CatalogChangeSetService().patch(
                changeset_id,
                ops=data["ops"],
                rationale=data["rationale"],
                scope_check=lambda scopes: CanProposeChangeset.covers_scopes(
                    request.auth, scopes
                ),
                auto_apply_additive=CanProposeChangeset.can_auto_apply(
                    request.auth
                ),
                actor=request.user,
            )
        except ChangesetNotFoundError as exc:
            return Response(
                {"error": str(exc), "changeset_id": str(exc.changeset_id)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except ChangesetStateConflictError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        except PermissionError as exc:
            return Response(
                {
                    "error": ERR_CHANGESET_SCOPES_NOT_COVERED,
                    "message": str(exc),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        except ValueError as exc:
            return Response(
                {"error": ERR_INVALID_REQUEST, "message": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception("Changeset patch failed")
            return Response(
                {"error": "Internal server error"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            _serialize_changeset(changeset), status=status.HTTP_200_OK
        )

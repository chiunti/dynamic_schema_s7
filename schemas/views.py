import json
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import (
    CanImportSchema,
    CanPublishSchema,
    CanReadSchema,
    ProjectPrincipal,
    ProjectTokenAuthentication,
    UserMCPTokenAuthentication,
    UserMCPTokenPermission,
)
from .constants import (
    ERR_INTERNAL_SERVER_ERROR,
    ERR_INVALID_REQUEST,
    ERR_PROJECT_ID_REQUIRED,
    STATUS_DRAFT,
)
from .serializers import SchemaImportSerializer
from .services.catalog_service import CatalogService
from .services.schema_import_service import SchemaImportService
from .services.schema_publish_service import SchemaPublishService
from .services.schema_read_service import SchemaReadService

logger = logging.getLogger(__name__)


class SchemaView(APIView):
    """Serve published schema JSON only to the owning project's credential."""

    authentication_classes = [ProjectTokenAuthentication]
    permission_classes = [CanReadSchema]

    def get(self, request, node_type, key, version):
        try:
            schema_json = SchemaReadService().get_published(
                node_type, key, version, request.user.project_id
            )
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
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Schema retrieval failed")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
                return Response({'error': 'Project credential does not match request'},
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
                return Response({'error': 'Project credential does not match request'},
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
    """Serve the resolved catalog for a json_scope to project credentials or user MCP tokens."""

    authentication_classes = [UserMCPTokenAuthentication, ProjectTokenAuthentication]
    permission_classes = [CanReadSchema | UserMCPTokenPermission]

    def get(self, request, scope):
        try:
            catalog = CatalogService().get_resolved_catalog(scope)
        except LookupError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
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

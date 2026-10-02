import json
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import CanImportSchema, CanPublishSchema, CanReadSchema, ProjectTokenAuthentication
from .serializers import SchemaImportSerializer
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
    """DRF endpoint to import a schema JSON using project credentials."""

    authentication_classes = [ProjectTokenAuthentication]
    permission_classes = [CanImportSchema]

    def post(self, request):
        serializer = SchemaImportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        if ((data.get('project_id') and data['project_id'] != request.user.project_id)
                or (data.get('organization_id') and data['organization_id'] != request.user.organization_id)):
            return Response({'error': 'Project credential does not match request'},
                            status=status.HTTP_403_FORBIDDEN)
        data['project_id'] = request.user.project_id
        data['organization_id'] = request.user.organization_id
        try:
            schema_id, warning = SchemaImportService().import_from_request(data, None)
        except PermissionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except (ValueError, RuntimeError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Schema import failed")
            return Response(
                {"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(
            {"schema_id": str(schema_id), "warning": warning or ""},
            status=status.HTTP_201_CREATED,
        )


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

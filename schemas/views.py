import json
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.cache import cache_page

from .repositories.schema_repository import SchemaRepository
from .constants import ERR_ERROR_IN_SCHEMA_VIEW

logger = logging.getLogger(__name__)


@require_GET
@cache_page(60 * 5)  # Cache for 5 minutes
def schema_view(request, node_type, key, version):
    """
    Public endpoint to serve schema JSON for published schemas.
    URL: /api/schema/<node_type>/<key>/<version>/
    Validates:
    - Status is 'published' (via v_schema_published view)
    - node_type matches the parameter
    - Schema exists in cache
    """
    logger.debug(f"schema_view called with node_type={node_type}, key={key}, version={version}")

    try:
        repository = SchemaRepository()
        schema_json = repository.get_published_schema(node_type, key, version)

        logger.info(f"Query result: {schema_json is not None}")

        if not schema_json:
            return JsonResponse(
                {
                    "type": "schema_not_found",
                    "title": "Schema Not Found",
                    "detail": "Schema not found, not published, or node type mismatch",
                    "status": 404
                },
                status=404
            )

        # Convert JSONB string to Python dict to avoid double serialization
        schema_dict = json.loads(schema_json) if isinstance(schema_json, str) else schema_json

        response_data = {
            "data": schema_dict,
            "meta": {
                "node_type": node_type,
                "key": key,
                "version": version
            }
        }

        return JsonResponse(response_data, json_dumps_params={'ensure_ascii': False})

    except Exception as e:
        logger.error(ERR_ERROR_IN_SCHEMA_VIEW.format(error=str(e)))
        return JsonResponse(
            {
                "type": "internal_error",
                "title": "Internal Server Error",
                "detail": str(e),
                "status": 500
            },
            status=500
        )

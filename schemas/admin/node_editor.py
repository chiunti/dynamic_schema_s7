"""
NodeEditorMixin - API endpoints for the visual node editor

This mixin provides all the AJAX endpoints for the tree editor:
- Tree loading and navigation
- Node CRUD operations
- Property management
- Move and reorder operations

Views are thin wrappers: they parse requests, delegate to services, and map
errors to HTTP status codes. All business logic and data access live in
services (NodeService, SchemaValidationService).
"""

import json
import logging

from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.urls import path
from django.shortcuts import render

from ..services.editor_extension_service import EditorExtensionService
from ..services.node_service import NodeService
from ..services.node_json_service import NodeJsonService
from ..services.schema_validation_service import SchemaValidationService
from ..constants import (
    ERR_METHOD_NOT_ALLOWED,
    ERR_INVALID_JSON,
    ERR_NOT_FOUND,
    ERR_PARENT_NOT_FOUND,
    ERR_NODE_TYPE_NOT_FOUND,
    ERR_SCHEMA_NOT_FOUND,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_PARENT_ID_AND_NODE_TYPE_REQUIRED,
    ERR_NODE_TYPE_REQUIRED,
    ERR_NODE_ID_AND_NEW_PARENT_ID_REQUIRED,
    ERR_NODE_ID_AND_DIRECTION_REQUIRED,
    ERR_NODE_ID_REQUIRED_MSG,
    ERR_PROPERTIES_REQUIRED,
    ERR_UNEXPECTED_ERROR_IN_API_TREE,
    ERR_UNEXPECTED_ERROR_IN_API_PROPERTIES,
    ERR_UNEXPECTED_ERROR_IN_API_CREATE,
    ERR_UNEXPECTED_ERROR_IN_API_DELETE,
)


class NodeEditorMixin:
    """Mixin providing node editor API endpoints"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validation_service = SchemaValidationService()
        self.node_service = NodeService()
        self.node_json_service = NodeJsonService()
        self.extension_service = EditorExtensionService()

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("editor/", self.admin_site.admin_view(self.editor_view), name="schemas_node_editor"),
            path("editor/api/tree/", self.admin_site.admin_view(self.api_tree), name="schemas_node_editor_tree"),
            path("editor/api/node/<uuid:node_id>/", self.admin_site.admin_view(self.api_node), name="schemas_node_editor_node"),
            path("editor/api/node/<uuid:node_id>/properties/", self.admin_site.admin_view(self.api_properties), name="schemas_node_editor_properties"),
            path("editor/api/node/<uuid:node_id>/allowed-children/", self.admin_site.admin_view(self.api_allowed_children), name="schemas_node_editor_allowed_children"),
            path("editor/api/create/", self.admin_site.admin_view(self.api_create), name="schemas_node_editor_create"),
            path("editor/api/node-type-variants/", self.admin_site.admin_view(self.api_node_type_variants), name="schemas_node_editor_node_type_variants"),
            path("editor/api/delete/<uuid:node_id>/", self.admin_site.admin_view(self.api_delete), name="schemas_node_editor_delete"),
            path("editor/api/move/", self.admin_site.admin_view(self.api_move), name="schemas_node_editor_move"),
            path("editor/api/reorder/", self.admin_site.admin_view(self.api_reorder), name="schemas_node_editor_reorder"),
            path("editor/api/node-json/", self.admin_site.admin_view(self.api_node_json), name="schemas_node_editor_node_json"),
            path("editor/api/extensions/", self.admin_site.admin_view(self.api_editor_extensions), name="schemas_node_editor_extensions"),
            path("editor/api/extensions/<str:name>", self.admin_site.admin_view(self.api_editor_extension_source), name="schemas_node_editor_extension_source"),
        ]
        return custom_urls + urls

    def editor_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Node editor",
        }
        return render(request, "admin/schemas/node/editor.html", context)

    def _resolve_root_node_id(self, request):
        return self.node_service.resolve_root_node_id(
            node_id=request.GET.get("node_id"),
            key=request.GET.get("key"),
            version=request.GET.get("version"),
        )

    def _resolve_schema_root_id(self, request):
        return self._resolve_root_node_id(request)

    def api_tree(self, request):
        root_id = self._resolve_schema_root_id(request)
        if not root_id:
            return JsonResponse({"error": ERR_SCHEMA_NOT_FOUND}, status=404)

        try:
            nodes = self.node_service.get_node_tree(root_id)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            logging.error(ERR_UNEXPECTED_ERROR_IN_API_TREE.format(error=e), exc_info=True)
            return JsonResponse({"error": ERR_INTERNAL_SERVER_ERROR}, status=500)

        return JsonResponse({"root_id": root_id, "nodes": nodes})

    def api_node(self, request, node_id):
        node = self.node_service.get_node_with_parent(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        if request.method == "GET":
            return JsonResponse(self.node_service.get_node_detail(node))

        if request.method != "PATCH":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            if not request.body:
                payload = {}
            else:
                payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

        if "name" in payload:
            try:
                self.node_service.update_node_name(node.id, payload.get("name"))
            except ValueError as e:
                return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({"ok": True})

    def api_allowed_children(self, request, node_id):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        node = self.node_service.get_node_with_node_type(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        result = self.node_service.get_allowed_children_with_variant_info(node)
        return JsonResponse(result)

    def api_properties(self, request, node_id):
        node = self.node_service.get_node_with_node_type(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        if request.method == "GET":
            result = self.node_service.get_node_properties_with_variant_filtering(node)
            return JsonResponse(result)

        if request.method != "PATCH":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            if not request.body:
                payload = {}
            else:
                payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

        updates = payload.get("properties")
        if not isinstance(updates, dict):
            return JsonResponse({"error": ERR_PROPERTIES_REQUIRED}, status=400)

        try:
            self.node_service.update_node_properties(node, updates)
        except ValueError as e:
            logging.error(f"Validation error saving properties for node {node_id}: {e}")
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            logging.error(ERR_UNEXPECTED_ERROR_IN_API_PROPERTIES.format(error=e), exc_info=True)
            return JsonResponse({"error": ERR_INTERNAL_SERVER_ERROR}, status=500)

        return JsonResponse({"ok": True})

    def api_create(self, request):
        if request.method != "POST":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            if not request.body:
                payload = {}
            else:
                payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

        parent_id = payload.get("parent_id")
        node_type_name = payload.get("node_type")
        if not parent_id or not node_type_name:
            return JsonResponse({"error": ERR_PARENT_ID_AND_NODE_TYPE_REQUIRED}, status=400)

        if not self.node_service.get_node_with_node_type(parent_id):
            return JsonResponse({"error": ERR_PARENT_NOT_FOUND}, status=404)
        if not self.node_service.get_node_type(node_type_name):
            return JsonResponse({"error": ERR_NODE_TYPE_NOT_FOUND}, status=404)

        try:
            node = self.node_service.create_node(
                parent_id,
                node_type_name,
                payload.get("name"),
                variant_key=payload.get("variant_key"),
                collection_key=payload.get("collection_key"),
            )
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            logging.error(ERR_UNEXPECTED_ERROR_IN_API_CREATE.format(error=e), exc_info=True)
            return JsonResponse({"error": ERR_INTERNAL_SERVER_ERROR}, status=500)

        return JsonResponse({"ok": True, "node_id": node.id})

    def api_node_type_variants(self, request):
        """Return variant options for a given node_type"""
        node_type_name = request.GET.get("node_type")
        if not node_type_name:
            return JsonResponse({"error": ERR_NODE_TYPE_REQUIRED}, status=400)

        result = self.node_service.get_node_type_variants_with_props_check(node_type_name)
        return JsonResponse(result)

    def api_move(self, request):
        if request.method != "POST":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            if not request.body:
                payload = {}
            else:
                payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

        node_id = payload.get("node_id")
        new_parent_id = payload.get("new_parent_id")

        if not node_id or not new_parent_id:
            return JsonResponse({"error": ERR_NODE_ID_AND_NEW_PARENT_ID_REQUIRED}, status=400)

        try:
            self.node_service.move_node(node_id, new_parent_id, payload.get("new_position"))
        except LookupError as e:
            return JsonResponse({"error": str(e)}, status=404)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse({"ok": True})

    def api_reorder(self, request):
        if request.method != "POST":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            if not request.body:
                payload = {}
            else:
                payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

        node_id = payload.get("node_id")
        direction = payload.get("direction")
        if not node_id or direction not in {"up", "down"}:
            return JsonResponse({"error": ERR_NODE_ID_AND_DIRECTION_REQUIRED}, status=400)

        try:
            self.node_service.reorder_node(node_id, direction)
        except LookupError as e:
            return JsonResponse({"error": str(e)}, status=404)

        return JsonResponse({"ok": True})

    def api_delete(self, request, node_id):
        if request.method != "DELETE":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            self.node_service.delete_node(node_id)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            logging.error(ERR_UNEXPECTED_ERROR_IN_API_DELETE.format(error=e), exc_info=True)
            return JsonResponse({"error": ERR_INTERNAL_SERVER_ERROR}, status=500)

        return JsonResponse({"ok": True})

    def _collect_required_warnings(self, root_node_id):
        """Walk the node tree and return missing required AttributeDefs per node."""
        return self.validation_service.collect_required_warnings(root_node_id)

    def api_node_json(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        node_id = request.GET.get("node_id")
        if not node_id:
            return JsonResponse({"error": ERR_NODE_ID_REQUIRED_MSG}, status=400)

        try:
            jsonb_result = self.node_service.build_node_json(node_id)
            # jsonb_result may be a dict (from psycopg2 JSONB adapter) or a string
            if isinstance(jsonb_result, dict):
                json_text = json.dumps(jsonb_result, indent=2, ensure_ascii=False)
            else:
                json_text = json.dumps(json.loads(jsonb_result), indent=2, ensure_ascii=False)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=404)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

        warnings = self._collect_required_warnings(node_id)
        node_line_map = self._build_node_line_map(json_text, node_id)
        return JsonResponse({"json": json_text, "warnings": warnings, "node_line_map": node_line_map})

    def _build_node_line_map(self, json_text, root_id):
        """
        Build a map of { node_id: [startLine, endLine] } for every node in the subtree.

        Delegates to NodeJsonService.build_node_line_map for implementation.
        """
        return self.node_json_service.build_node_line_map(json_text, root_id)

    def api_editor_extensions(self, request):
        """Manifest of enabled extensions — each entry carries its serve URL."""
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            manifest = self.extension_service.list_manifest(request.path)
            return JsonResponse({"extensions": manifest})
        except Exception as e:
            logging.error(f"Error listing editor extensions: {e}", exc_info=True)
            return JsonResponse({"extensions": []})

    def api_editor_extension_source(self, request, name):
        """Serve an enabled extension's JS source (static file or DB)."""
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        stem = name[:-3] if name.endswith('.js') else name
        source = self.extension_service.get_source(stem)
        if source is None:
            return HttpResponseNotFound(
                '// extension not found or disabled\n',
                content_type='application/javascript',
            )
        return HttpResponse(source, content_type='application/javascript')

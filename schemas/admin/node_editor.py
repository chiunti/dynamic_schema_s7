"""
NodeEditorMixin - API endpoints for the visual node editor

This mixin provides all the AJAX endpoints for the tree editor:
- Tree loading and navigation
- Node CRUD operations
- Property management
- Move and reorder operations
"""

import json
import logging
import os
from collections import deque

from django.http import JsonResponse
from django.db import transaction
from django.db.models import Count, Max
from django.urls import path
from django.shortcuts import render
from django.conf import settings

from ..models import (
    Node,
    NodeType,
    NodeTypeVariant,
    AttributeDef,
    NodeAttribute,
    NodeTypeComposition,
    Domain,
    DomainItem,
)
from ..services.node_service import NodeService
from ..services.schema_validation_service import SchemaValidationService
from ..services.attribute_def_service import AttributeDefService
from ..repositories.schema_repository import SchemaRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.composition_repository import CompositionRepository
from ..constants import (
    ERR_METHOD_NOT_ALLOWED,
    ERR_INVALID_JSON,
    ERR_NOT_FOUND,
    ERR_PARENT_NOT_FOUND,
    ERR_NODE_TYPE_NOT_FOUND,
    ERR_COMPOSITION_NOT_ALLOWED,
    ERR_SCHEMA_NOT_FOUND,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_NAME_REQUIRED,
    ERR_PARENT_ID_AND_NODE_TYPE_REQUIRED,
    ERR_MAX_CHILDREN_REACHED,
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
        self.attribute_def_service = AttributeDefService()
        self.schema_repository = SchemaRepository()
        self.node_type_repository = NodeTypeRepository()
        self.attribute_def_repository = AttributeDefRepository()
        self.composition_repository = CompositionRepository()

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
        ]
        return custom_urls + urls

    def editor_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Node editor",
        }
        return render(request, "admin/schemas/node/editor.html", context)

    def _normalize_positions(self, parent_id):
        self.schema_repository.normalize_positions(parent_id)

    def _resolve_root_node_id(self, request):
        node_id = request.GET.get("node_id")
        if node_id:
            try:
                node = self.schema_repository.get_node_by_id(node_id)
                return node.id if node else None
            except Node.DoesNotExist:
                return None

        key = request.GET.get("key")
        version = request.GET.get("version")
        if not key or not version:
            return None

        root_node = self.schema_repository.get_root_node_by_key_version(key, version)
        return root_node.id if root_node else None

    def _resolve_schema_root_id(self, request):
        return self._resolve_root_node_id(request)

    def api_tree(self, request):
        root_id = self._resolve_schema_root_id(request)
        if not root_id:
            return JsonResponse({"error": ERR_SCHEMA_NOT_FOUND}, status=404)

        try:
            node_service = NodeService()
            nodes = node_service.get_node_tree(root_id)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            logging.error(ERR_UNEXPECTED_ERROR_IN_API_TREE.format(error=e), exc_info=True)
            return JsonResponse({"error": ERR_INTERNAL_SERVER_ERROR}, status=500)

        return JsonResponse({"root_id": root_id, "nodes": nodes})

    def api_node(self, request, node_id):
        node = self.schema_repository.get_node_by_id_with_parent(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        if request.method == "GET":
            children = list(self.schema_repository.get_children_by_parent(node))
            return JsonResponse({
                "id": node.id,
                "name": node.name,
                "sort_order": node.sort_order,
                "parent_id": node.parent_id,
                "node_type": node.node_type.name,
                "children": [{"id": str(c["id"]), "name": c["name"], "node_type": c["node_type__name"]} for c in children],
            })

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
            node.name = str(payload.get("name") or "").strip()
            if not node.name:
                return JsonResponse({"error": ERR_NAME_REQUIRED}, status=400)
            self.schema_repository.update_node_name(node.id, node.name)

        return JsonResponse({"ok": True})

    def api_allowed_children(self, request, node_id):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        node = self.schema_repository.get_node_by_id_with_node_type(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        result = self.node_service.get_allowed_children_with_variant_info(node)
        return JsonResponse(result)

    def api_properties(self, request, node_id):
        import logging
        logger = logging.getLogger(__name__)

        node = self.schema_repository.get_node_by_id_with_node_type(node_id)
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
            node_service = NodeService()
            node_service.update_node_properties(node, updates)
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
        variant_key = payload.get("variant_key")
        collection_key = payload.get("collection_key")
        if not parent_id or not node_type_name:
            return JsonResponse({"error": ERR_PARENT_ID_AND_NODE_TYPE_REQUIRED}, status=400)

        parent = self.schema_repository.get_node_by_id_with_node_type(parent_id)
        if not parent:
            return JsonResponse({"error": ERR_PARENT_NOT_FOUND}, status=404)

        child_type = self.node_type_repository.get_node_type_by_name(node_type_name)
        if not child_type:
            return JsonResponse({"error": ERR_NODE_TYPE_NOT_FOUND}, status=404)

        # Filter composition by collection_key if provided
        if collection_key:
            composition = self.composition_repository.get_composition_by_parent_child_collection_key(
                parent.node_type, child_type, collection_key
            )
        else:
            composition = self.schema_repository.get_composition_by_parent_child(parent.node_type, child_type)

        if not composition:
            return JsonResponse({"error": ERR_COMPOSITION_NOT_ALLOWED}, status=400)

        # For collection_key-based compositions, check if a child with that key already exists
        # Only apply this check for singleton slots (max_children=1)
        if composition.collection_key and composition.max_children == 1:
            existing = self.schema_repository.node_exists_by_parent_key(parent, composition.collection_key)
            if existing:
                return JsonResponse({"error": f"A child with key '{composition.collection_key}' already exists"}, status=400)
        # For non-collection_key compositions or non-singleton compositions, use count-based check
        elif composition.max_children is not None:
            current_children_count = self.schema_repository.count_children_by_parent_type(parent, child_type)
            if current_children_count >= composition.max_children:
                return JsonResponse({"error": ERR_MAX_CHILDREN_REACHED}, status=400)

        logging.info(f"Creating node: parent={parent_id}, node_type={node_type_name}, collection_key={collection_key}, composition.collection_key={composition.collection_key}")

        max_pos = self.schema_repository.get_max_sort_order(parent.id)
        next_pos = 0 if max_pos is None else int(max_pos) + 1
        name = payload.get("name")
        if name is None or str(name).strip() == "":
            name = f"{node_type_name}_{next_pos}"

        # Use collection_key as the node key only for singleton slots (max_children=1)
        # For compositions that allow multiple children (max_children=None or >1), don't use collection_key as key
        if composition.collection_key and composition.max_children == 1:
            node_key = composition.collection_key
        else:
            node_key = None

        # For props nodes, infer variant_key from parent's type attribute
        if not variant_key:
            child_type = self.node_type_repository.get_node_type_by_name(node_type_name)
            if child_type and self.node_type_repository.is_props_node_type(child_type):
                variant_key = self.node_service.infer_variant_from_parent(parent)

        try:
            node_service = NodeService()
            node = node_service.create_node(parent_id, node_type_name, name, variant_key, key=node_key, collection_key=collection_key)
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
        new_position = payload.get("new_position")

        if not node_id or not new_parent_id:
            return JsonResponse({"error": ERR_NODE_ID_AND_NEW_PARENT_ID_REQUIRED}, status=400)

        node = self.schema_repository.get_node_by_id_with_parent(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        new_parent = self.schema_repository.get_node_by_id_with_node_type(new_parent_id)
        if not new_parent:
            return JsonResponse({"error": ERR_PARENT_NOT_FOUND}, status=404)

        allowed = self.schema_repository.composition_exists(new_parent.node_type, node.node_type)
        if not allowed:
            return JsonResponse({"error": ERR_COMPOSITION_NOT_ALLOWED}, status=400)

        old_parent_id = node.parent_id
        with transaction.atomic():
            self.schema_repository.update_node_parent(node.id, new_parent.id)
            self._normalize_positions(old_parent_id)
            self._normalize_positions(new_parent.id)

            if new_position is not None:
                try:
                    pos = int(new_position)
                except (TypeError, ValueError):
                    pos = None
                if pos is not None:
                    siblings = list(self.schema_repository.get_children_by_parent_full(new_parent.id))
                    pos = max(0, min(pos, max(0, len(siblings) - 1)))
                    ordered = [n for n in siblings if n.id != node.id]
                    ordered.insert(pos, self.schema_repository.get_node_by_id(node_id))
                    for idx, n in enumerate(ordered):
                        n.sort_order = idx
                    self.schema_repository.bulk_update_nodes_sort_order(ordered)

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

        node = self.schema_repository.get_node_by_id_with_parent(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        parent_id = node.parent_id
        with transaction.atomic():
            self._normalize_positions(parent_id)
            siblings = list(self.schema_repository.get_children_by_parent_full(parent_id))
            ids = [n.id for n in siblings]
            try:
                idx = ids.index(node.id)
            except ValueError:
                return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

            if direction == "up" and idx > 0:
                siblings[idx - 1].sort_order, siblings[idx].sort_order = siblings[idx].sort_order, siblings[idx - 1].sort_order
                self.schema_repository.bulk_update_nodes_sort_order([siblings[idx - 1], siblings[idx]])
            elif direction == "down" and idx < len(siblings) - 1:
                siblings[idx + 1].sort_order, siblings[idx].sort_order = siblings[idx].sort_order, siblings[idx + 1].sort_order
                self.schema_repository.bulk_update_nodes_sort_order([siblings[idx + 1], siblings[idx]])

            self._normalize_positions(parent_id)

        return JsonResponse({"ok": True})

    def api_delete(self, request, node_id):
        if request.method != "DELETE":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            node_service = NodeService()
            node_service.delete_node(node_id)
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
            node_service = NodeService()
            jsonb_result = node_service.build_node_json(node_id)
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

        Delegates to utils.build_node_line_map for implementation.
        """
        from .utils import build_node_line_map
        return build_node_line_map(json_text, root_id)

    def api_editor_extensions(self, request):
        """List all JavaScript files in the extensions_editor directory."""
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            # In development, use STATICFILES_DIRS (source files)
            # In production, use STATIC_ROOT (collected files)
            static_dirs = getattr(settings, 'STATICFILES_DIRS', [])
            static_root = getattr(settings, 'STATIC_ROOT', None)
            
            # Try STATICFILES_DIRS first (development)
            extensions_dir = None
            for static_dir in static_dirs:
                potential_path = os.path.join(static_dir, 'admin', 'js', 'extensions_editor')
                if os.path.exists(potential_path):
                    extensions_dir = potential_path
                    break
            
            # If not found in STATICFILES_DIRS, try STATIC_ROOT (production)
            if not extensions_dir and static_root:
                potential_path = os.path.join(static_root, 'admin', 'js', 'extensions_editor')
                if os.path.exists(potential_path):
                    extensions_dir = potential_path

            if not extensions_dir or not os.path.exists(extensions_dir):
                return JsonResponse({"extensions": []})

            # List all .js files in the directory
            extensions = []
            for filename in os.listdir(extensions_dir):
                if filename.endswith('.js') and not filename.startswith('.'):
                    extensions.append(filename)
            
            return JsonResponse({"extensions": extensions})
        except Exception as e:
            logging.error(f"Error listing editor extensions: {e}", exc_info=True)
            return JsonResponse({"extensions": []})


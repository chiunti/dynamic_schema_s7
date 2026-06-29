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
from ..utils import normalize_variant_key
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

        compositions = self.composition_repository.get_compositions_by_parent_type(node.node_type)

        child_type_ids = [c.child_type_id for c in compositions]
        existing_counts = self.schema_repository.get_children_counts_by_parent_type_ids(node, child_type_ids)

        # For collection_key-based compositions, check if a child with that key already exists
        # Build a set of existing keys (regardless of node_type) for singleton slots
        # Exclude keys that match collection_key of non-singleton compositions (e.g., 'children')
        non_singleton_collection_keys = {
            c.collection_key for c in compositions
            if c.collection_key and c.max_children != 1
        }
        existing_keys = self.schema_repository.get_children_keys_by_parent_type_ids(node, child_type_ids) - non_singleton_collection_keys

        # Also build a map of collection_key to existing node for compositions
        # This handles the case where imported nodes have custom keys (e.g., 'home_body' instead of 'body')
        collection_key_to_node = {}
        for c in compositions:
            if c.collection_key and c.max_children == 1:
                # For singleton slots, check if there's a child of this type with this parent
                existing_child = self.schema_repository.get_node_by_parent_type(node, c.child_type_id)
                if existing_child:
                    collection_key_to_node[c.collection_key] = existing_child

        allowed = []
        for c in compositions:
            max_c = c.max_children

            # For collection_key-based compositions:
            # 1. Check if a child with that collection_key already exists (singleton)
            # 2. If NOT, check max_children for that specific collection_key
            if c.collection_key:
                # For singleton slots (max_children=1), check if slot is already occupied
                if max_c == 1 and c.collection_key in collection_key_to_node:
                    # A child with this collection_key already exists (singleton slot)
                    continue
                # Check max_children for this specific collection_key
                if max_c is not None and existing_counts.get(c.child_type_id, 0) >= max_c:
                    continue
            # For non-collection_key compositions: check max_children by node_type
            elif max_c is not None and existing_counts.get(c.child_type_id, 0) >= max_c:
                continue

            allowed.append({
                "node_type": c.child_type.name,
                "label": c.child_type.label,
                "collection_key": c.collection_key,
                "min_children": c.min_children,
                "max_children": max_c,
            })

        # Check if any allowed child type should infer variant from parent
        # (e.g., sdui_props inherits variant from parent's component type)
        infer_variant_from_parent = []
        for item in allowed:
            child_type = self.node_type_repository.get_node_type_by_name(item["node_type"])
            if child_type:
                # Check if this node type has NodeTypeVariants with discriminator_attr=None
                # This pattern indicates the variant comes from parent
                has_parent_inferred_variants = self.node_type_repository.has_parent_inferred_variants(child_type)
                if has_parent_inferred_variants:
                    infer_variant_from_parent.append(item["node_type"])

        return JsonResponse({
            "parent_id": node.id,
            "parent_type": node.node_type.name,
            "allowed": allowed,
            "infer_variant_from_parent": infer_variant_from_parent
        })

    def api_properties(self, request, node_id):
        import logging
        logger = logging.getLogger(__name__)

        node = self.schema_repository.get_node_by_id_with_node_type(node_id)
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        all_defs = self.attribute_def_repository.get_attribute_defs_by_node_type(node.node_type)
        node_attributes = self.schema_repository.get_node_attributes_by_node(node)
        existing = {na.attribute_def_id: na for na in node_attributes}

        # Resolve current variant from the discriminator attribute (if present).
        # For props nodes, the variant is inherited from the parent's component type.
        current_variant = self.node_service.infer_variant_from_parent(node)

        # If not inherited, read discriminator attribute from the current node
        if current_variant is None:
            discriminator = self.node_type_repository.get_discriminator_attr(node.node_type)
            if discriminator:
                type_def = next((d for d in all_defs if d.json_key == discriminator and d.variant_key is None), None)
                if type_def:
                    type_attr = existing.get(type_def.id)
                    if type_attr and type_attr.value_string:
                        current_variant = type_attr.value_string
                if current_variant is None:
                    # Fallback: find any discriminator NodeAttribute regardless of variant_key scoping
                    for d in all_defs:
                        if d.json_key == discriminator:
                            type_attr = existing.get(d.id)
                            if type_attr and type_attr.value_string:
                                current_variant = type_attr.value_string
                            break

        # Filter by variant_key and is_common: show universal common (NULL + is_common=True) + current variant only.
        # For variant-specific defs (variant_key=current_variant), is_common=False is allowed.
        # Catalog defs (variant_key=NULL + is_common=False) are templates and should not be shown.
        # If the node type has variant-scoped defs but no variant is selected yet,
        # show only the universal common defs so the user sets 'type' first.
        has_variant_defs = any(d.variant_key is not None for d in all_defs)
        
        # Normalize current_variant to snake_case for comparison
        normalized_current_variant = normalize_variant_key(current_variant) if current_variant else None
        
        if has_variant_defs:
            if normalized_current_variant:
                defs = [d for d in all_defs if (d.variant_key is None and d.is_common) or normalize_variant_key(d.variant_key) == normalized_current_variant]
            else:
                defs = [d for d in all_defs if d.variant_key is None and d.is_common]
        else:
            defs = all_defs

        # Filter out json_keys that correspond to child nodes (without collection_key)
        # These are structural child nodes like layout, props, show_if, etc.
        # Check if there's a composition where child_type.name ends with _json_key
        # (e.g., sdui_layout ends with _layout, sdui_show_if ends with _show_if)
        child_type_names = set(
            self.composition_repository.get_child_type_names_by_parent_no_collection_key(node.node_type)
        )
        
        filtered_defs = []
        for d in defs:
            json_key = d.json_key
            # Check if child_type.name ends with _json_key (suffix match with underscore)
            is_child_node = any(
                child_name == json_key  # Exact match
                or child_name.endswith('_' + json_key)  # Suffix match (e.g., sdui_layout -> layout)
                for child_name in child_type_names
            )
            if not is_child_node:
                filtered_defs.append(d)
        defs = filtered_defs

        if request.method == "GET":
            domain_ids = [d.domain_id for d in defs if d.domain_id]
            domain_items = self.attribute_def_repository.get_domain_items_by_domain_ids(domain_ids)
            items_by_domain = {}
            for di in domain_items:
                items_by_domain.setdefault(di.domain_id, []).append({
                    "value": di.value,
                    "label": di.label,
                })

            props = []
            for d in defs:
                na = existing.get(d.id)
                dt_name = d.data_type.name

                if dt_name == 'natural_uuid':
                    prop_value = {"value_string": str(node.id), "value_number": None, "value_bool": None, "value_json": None}
                    prop_has_value = True
                elif dt_name == 'natural_key':
                    prop_value = {"value_string": node.key, "value_number": None, "value_bool": None, "value_json": None}
                    prop_has_value = bool(node.key)
                elif dt_name == 'natural_version':
                    prop_value = {"value_string": node.version, "value_number": None, "value_bool": None, "value_json": None}
                    prop_has_value = bool(node.version)
                elif dt_name == 'natural_order':
                    prop_value = {"value_string": str(node.sort_order), "value_number": None, "value_bool": None, "value_json": None}
                    prop_has_value = True
                elif dt_name == 'display_order':
                    prop_value = {"value_string": str(node.sort_order + 1), "value_number": None, "value_bool": None, "value_json": None}
                    prop_has_value = True
                else:
                    prop_value = {
                        "value_string": na.value_string if na else None,
                        "value_number": float(na.value_number) if (na and na.value_number is not None) else None,
                        "value_bool": na.value_bool if na else None,
                        "value_json": na.value_json if na else None,
                    }
                    prop_has_value = bool(na)

                props.append({
                    "attribute_def_id": d.id,
                    "json_key": d.json_key,
                    "label": d.name,
                    "data_type": dt_name,
                    "is_required": d.is_required,
                    "variant_key": d.variant_key,
                    "domain": d.domain.domain_name if d.domain_id else None,
                    "domain_items": items_by_domain.get(d.domain_id, []) if d.domain_id else [],
                    "value": prop_value,
                    "has_value": prop_has_value,
                    "group": d.group,
                })

            response_data = {
                "node_id": node.id,
                "node_type": node.node_type.name,
                "current_variant": current_variant,
                "properties": props,
            }

            # Populate type selector options for nodes that have variants
            # For sdui_container with collection_key, type is already "container" - don't show variants
            # For sdui_widget with collection_key, type is null - allow variant selection
            if has_variant_defs:
                # Skip variant options for sdui_container nodes with collection_key (type is fixed to "container")
                if not (node.node_type.name == 'sdui_container' and node.key):
                    variants = self.node_type_repository.get_variant_keys_by_node_type(node.node_type)
                    options = [{"value": v, "label": v} for v in variants]
                    response_data["variant_options"] = options

            return JsonResponse(response_data)

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

        node_type = self.node_type_repository.get_node_type_by_name(node_type_name)
        if not node_type:
            return JsonResponse({"error": ERR_NODE_TYPE_NOT_FOUND}, status=404)

        variants = self.attribute_def_service.get_variants_for_node_type(str(node_type.id))
        options = [{"value": v, "label": v} for v in variants]
        return JsonResponse({"options": options})

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
                    siblings = list(self.schema_repository.get_children_by_parent(new_parent))
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
            siblings = list(self.schema_repository.get_children_by_parent(node))
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


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
        qs = Node.objects.filter(parent_id=parent_id).order_by("sort_order", "id").only("id", "sort_order")
        updates = []
        for idx, n in enumerate(qs):
            if n.sort_order != idx:
                n.sort_order = idx
                updates.append(n)
        if updates:
            Node.objects.bulk_update(updates, ["sort_order"])

    def _resolve_root_node_id(self, request):
        node_id = request.GET.get("node_id")
        if node_id:
            try:
                return Node.objects.only("id").get(id=node_id).id
            except Node.DoesNotExist:
                return None

        key = request.GET.get("key")
        version = request.GET.get("version")
        if not key or not version:
            return None

        root_node = Node.objects.filter(
            node_type__is_root=True,
            parent__isnull=True,
            key=key,
            version=version,
        ).first()
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
        node = Node.objects.select_related("node_type", "parent").filter(id=node_id).first()
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        if request.method == "GET":
            children = list(Node.objects.filter(parent=node).select_related("node_type").order_by("sort_order").values("id", "name", "sort_order", "node_type__name"))
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
            node.save(update_fields=["name"])

        return JsonResponse({"ok": True})

    def api_allowed_children(self, request, node_id):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        node = Node.objects.select_related("node_type").filter(id=node_id).first()
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        compositions = NodeTypeComposition.objects.select_related("child_type").filter(parent_type=node.node_type).order_by("child_type__name")

        child_type_ids = [c.child_type_id for c in compositions]
        existing_counts = dict(
            Node.objects.filter(parent=node, node_type_id__in=child_type_ids)
            .values_list("node_type_id")
            .annotate(cnt=Count("id"))
            .values_list("node_type_id", "cnt")
        )

        # For collection_key-based compositions, check if a child with that key already exists
        # Build a set of existing keys (regardless of node_type) for singleton slots
        existing_keys = set(
            Node.objects.filter(parent=node, node_type_id__in=child_type_ids, key__isnull=False)
            .values_list("key", flat=True)
        )

        allowed = []
        for c in compositions:
            max_c = c.max_children

            # For collection_key-based compositions (singleton slots), check if key already exists
            if c.collection_key:
                if c.collection_key in existing_keys:
                    # A child with this collection_key already exists (any node type)
                    continue
            # For non-collection_key compositions, use count-based check
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
            child_type = NodeType.objects.filter(name=item["node_type"]).first()
            if child_type:
                # Check if this node type has NodeTypeVariants with discriminator_attr=None
                # This pattern indicates the variant comes from parent
                has_parent_inferred_variants = NodeTypeVariant.objects.filter(
                    node_type=child_type,
                    discriminator_attr__isnull=True
                ).exists()
                if has_parent_inferred_variants:
                    infer_variant_from_parent.append(item["node_type"])

        return JsonResponse({
            "parent_id": node.id,
            "parent_type": node.node_type.name,
            "allowed": allowed,
            "infer_variant_from_parent": infer_variant_from_parent
        })

    def api_properties(self, request, node_id):
        node = Node.objects.select_related("node_type").filter(id=node_id).first()
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        all_defs = list(
            AttributeDef.objects.select_related("data_type", "domain")
            .filter(node_type=node.node_type)
            .order_by("json_key")
        )
        existing = {na.attribute_def_id: na for na in NodeAttribute.objects.filter(node=node).select_related("attribute_def")}

        # Resolve current variant from the 'type' attribute (if present).
        # For sdui_props nodes, the variant is inherited from the parent's component type.
        current_variant = None
        if node.node_type.name == 'sdui_props' and node.parent:
            # Read the parent's type attribute to determine the variant for props
            parent = Node.objects.select_related("node_type").filter(id=node.parent_id).first()
            if parent:
                parent_type_attr = NodeAttribute.objects.filter(
                    node=parent,
                    attribute_def__json_key='type'
                ).select_related('attribute_def').first()
                if parent_type_attr and parent_type_attr.value_string:
                    current_variant = parent_type_attr.value_string
        else:
            # Standard variant resolution: read 'type' from the current node
            type_def = next((d for d in all_defs if d.json_key == "type" and d.variant_key is None), None)
            if type_def:
                type_attr = existing.get(type_def.id)
                if type_attr and type_attr.value_string:
                    current_variant = type_attr.value_string
            if current_variant is None:
                # Fallback: find any 'type' NodeAttribute regardless of variant_key scoping
                for d in all_defs:
                    if d.json_key == "type":
                        type_attr = existing.get(d.id)
                        if type_attr and type_attr.value_string:
                            current_variant = type_attr.value_string
                            break

        # Filter by variant_key and is_common: show universal common (NULL + is_common=True) + current variant only.
        # Catalog defs (variant_key=NULL + is_common=False) are templates and should not be shown.
        # If the node type has variant-scoped defs but no variant is selected yet,
        # show only the universal common defs so the user sets 'type' first.
        has_variant_defs = any(d.variant_key is not None for d in all_defs)
        if has_variant_defs:
            if current_variant:
                defs = [d for d in all_defs if (d.variant_key is None and d.is_common) or d.variant_key == current_variant]
            else:
                defs = [d for d in all_defs if d.variant_key is None and d.is_common]
        else:
            defs = all_defs

        # Filter out json_keys that correspond to child nodes (without collection_key)
        # These are structural child nodes like layout, props, show_if, etc.
        # Check if there's a composition where child_type.name ends with _json_key
        # (e.g., sdui_layout ends with _layout, sdui_show_if ends with _show_if)
        from schemas.models import NodeTypeComposition
        child_type_names = set(
            NodeTypeComposition.objects
            .filter(parent_type=node.node_type, collection_key__isnull=True)
            .values_list('child_type__name', flat=True)
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
            domain_items = DomainItem.objects.filter(domain_id__in=domain_ids).order_by("domain_id", "value")
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
                    variants = list(
                        NodeTypeVariant.objects.filter(node_type=node.node_type)
                        .order_by("variant_key")
                        .values_list("variant_key", flat=True)
                    )
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

        parent = Node.objects.select_related("node_type").filter(id=parent_id).first()
        if not parent:
            return JsonResponse({"error": ERR_PARENT_NOT_FOUND}, status=404)

        child_type = NodeType.objects.filter(name=node_type_name).first()
        if not child_type:
            return JsonResponse({"error": ERR_NODE_TYPE_NOT_FOUND}, status=404)

        # Filter composition by collection_key if provided
        if collection_key:
            composition = NodeTypeComposition.objects.filter(
                parent_type=parent.node_type,
                child_type=child_type,
                collection_key=collection_key
            ).first()
        else:
            composition = NodeTypeComposition.objects.filter(
                parent_type=parent.node_type,
                child_type=child_type
            ).first()
        
        if not composition:
            return JsonResponse({"error": ERR_COMPOSITION_NOT_ALLOWED}, status=400)

        # For collection_key-based compositions, check if a child with that key already exists
        if composition.collection_key:
            existing = Node.objects.filter(parent=parent, key=composition.collection_key).exists()
            if existing:
                return JsonResponse({"error": f"A child with key '{composition.collection_key}' already exists"}, status=400)
        # For non-collection_key compositions, use count-based check
        elif composition.max_children is not None:
            current_children_count = Node.objects.filter(parent=parent, node_type=child_type).count()
            if current_children_count >= composition.max_children:
                return JsonResponse({"error": ERR_MAX_CHILDREN_REACHED}, status=400)
        
        logging.info(f"Creating node: parent={parent_id}, node_type={node_type_name}, collection_key={collection_key}, composition.collection_key={composition.collection_key}")

        max_pos = Node.objects.filter(parent=parent).aggregate(Max("sort_order")).get("sort_order__max")
        next_pos = 0 if max_pos is None else int(max_pos) + 1
        name = payload.get("name")
        if name is None or str(name).strip() == "":
            name = f"{node_type_name}_{next_pos}"

        # Use collection_key as the node key if available
        node_key = composition.collection_key if composition.collection_key else None

        # For sdui_props nodes, infer variant_key from parent's type attribute
        if node_type_name == 'sdui_props' and not variant_key:
            parent_type_attr = NodeAttribute.objects.filter(
                node=parent,
                attribute_def__json_key='type'
            ).select_related('attribute_def').first()
            if parent_type_attr and parent_type_attr.value_string:
                variant_key = parent_type_attr.value_string

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

        node_type = NodeType.objects.filter(name=node_type_name).first()
        if not node_type:
            return JsonResponse({"error": ERR_NODE_TYPE_NOT_FOUND}, status=404)

        variants = list(
            NodeTypeVariant.objects.filter(node_type=node_type)
            .order_by("variant_key")
            .values_list("variant_key", flat=True)
        )
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

        node = Node.objects.select_related("parent", "node_type").filter(id=node_id).first()
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        new_parent = Node.objects.select_related("node_type").filter(id=new_parent_id).first()
        if not new_parent:
            return JsonResponse({"error": ERR_PARENT_NOT_FOUND}, status=404)

        allowed = NodeTypeComposition.objects.filter(parent_type=new_parent.node_type, child_type=node.node_type).exists()
        if not allowed:
            return JsonResponse({"error": ERR_COMPOSITION_NOT_ALLOWED}, status=400)

        old_parent_id = node.parent_id
        with transaction.atomic():
            node.parent = new_parent
            node.save(update_fields=["parent"])
            self._normalize_positions(old_parent_id)
            self._normalize_positions(new_parent.id)

            if new_position is not None:
                try:
                    pos = int(new_position)
                except (TypeError, ValueError):
                    pos = None
                if pos is not None:
                    siblings = list(Node.objects.filter(parent=new_parent).order_by("sort_order", "id").only("id", "sort_order"))
                    pos = max(0, min(pos, max(0, len(siblings) - 1)))
                    ordered = [n for n in siblings if n.id != node.id]
                    ordered.insert(pos, Node.objects.only("id", "sort_order").get(id=node.id))
                    for idx, n in enumerate(ordered):
                        n.sort_order = idx
                    Node.objects.bulk_update(ordered, ["sort_order"])

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

        node = Node.objects.filter(id=node_id).only("id", "parent_id", "sort_order").first()
        if not node:
            return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

        parent_id = node.parent_id
        with transaction.atomic():
            self._normalize_positions(parent_id)
            siblings = list(Node.objects.filter(parent_id=parent_id).order_by("sort_order", "id").only("id", "sort_order"))
            ids = [n.id for n in siblings]
            try:
                idx = ids.index(node.id)
            except ValueError:
                return JsonResponse({"error": ERR_NOT_FOUND}, status=404)

            if direction == "up" and idx > 0:
                siblings[idx - 1].sort_order, siblings[idx].sort_order = siblings[idx].sort_order, siblings[idx - 1].sort_order
                Node.objects.bulk_update([siblings[idx - 1], siblings[idx]], ["sort_order"])
            elif direction == "down" and idx < len(siblings) - 1:
                siblings[idx + 1].sort_order, siblings[idx].sort_order = siblings[idx].sort_order, siblings[idx + 1].sort_order
                Node.objects.bulk_update([siblings[idx + 1], siblings[idx]], ["sort_order"])

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


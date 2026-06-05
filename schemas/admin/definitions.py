"""
Admin classes for definition models (NodeType, AttributeDef, DataType, Domain, etc.)
"""

import json

from django.contrib import admin
from django.contrib.admin.views.main import IncorrectLookupParameters
from django.http import JsonResponse, QueryDict, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, re_path
from django.utils.html import format_html

from ..models import (
    Domain,
    DomainItem,
    DataType,
    NodeType,
    NodeTypeComposition,
    NodeTypeVariant,
    AttributeDef,
    ComponentPropertiesProxy,
)
from ..repositories.node_type_repository import NodeTypeRepository
from ..services.composition_service import CompositionService
from ..constants import (
    ERR_METHOD_NOT_ALLOWED,
    ERR_INVALID_JSON,
    ERR_NOT_FOUND,
    ERR_PARENT_AND_CHILD_REQUIRED,
    ERR_ALREADY_EXISTS,
)


class DomainItemInline(admin.TabularInline):
    model = DomainItem
    extra = 0


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain_name", "description")
    search_fields = ("domain_name", "description")
    inlines = (DomainItemInline,)


@admin.register(DomainItem)
class DomainItemAdmin(admin.ModelAdmin):
    list_display = ("domain", "value", "label")
    list_filter = ("domain",)
    search_fields = ("value", "label", "domain__domain_name")


@admin.register(DataType)
class DataTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name", "description")


class NodeTypeCompositionInline(admin.TabularInline):
    model = NodeTypeComposition
    fk_name = "parent_type"
    extra = 0
    autocomplete_fields = ("child_type",)


class NodeTypeVariantInline(admin.TabularInline):
    model = NodeTypeVariant
    extra = 0
    fields = ("variant_key",)


@admin.register(NodeType)
class NodeTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "label", "is_root", "is_container", "json_scope")
    list_filter = ("is_root", "is_container", "json_scope")
    search_fields = ("name", "label", "json_scope")
    inlines = (NodeTypeCompositionInline, NodeTypeVariantInline)


def _get_tree_node_type_ids(root_scope):
    """Return all NodeType IDs reachable from the root type with the given json_scope,
    by walking the NodeTypeComposition tree. No hardcoded names needed."""
    root = NodeTypeRepository().get_root_node_type_by_scope(root_scope)
    if not root:
        return []
    visited = set()
    queue = [root.id]
    while queue:
        current_id = queue.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        children = NodeTypeComposition.objects.filter(
            parent_type_id=current_id
        ).values_list('child_type_id', flat=True)
        queue.extend(children)
    return list(visited)


class CompositionAdminMixin:
    """Shared editor logic for composition admins. Subclasses must define:
    - composition_model: Model class (e.g. NodeTypeComposition)
    - editor_title: str
    - editor_tree_mode: str
    - url_namespace: str  (e.g. 'schemas_nodetypecomposition')
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.composition_service = CompositionService()

    def _active_scope(self, request):
        return getattr(self, '_scope_filter', None) or request.GET.get('scope', '')

    def _type_ids(self, scope=None):
        if scope:
            return _get_tree_node_type_ids(scope)
        all_ids = []
        for nt in NodeTypeRepository().get_all_root_node_types():
            all_ids.extend(_get_tree_node_type_ids(nt.json_scope))
        return list(set(all_ids))

    def get_changelist_instance(self, request):
        self._scope_filter = request.GET.get('scope')
        if self._scope_filter is not None:
            original_get = request.GET
            modified_get = QueryDict(mutable=True)
            modified_get.update(original_get)
            if 'scope' in modified_get:
                del modified_get['scope']
            request._original_get = original_get
            request.GET = modified_get
            try:
                cl = super().get_changelist_instance(request)
            finally:
                request.GET = request._original_get
                del request._original_get
            return cl
        return super().get_changelist_instance(request)

    def get_queryset(self, request):
        scope = getattr(self, '_scope_filter', None) or request.GET.get('scope', '')
        type_ids = self._type_ids(scope if scope else None)
        return super().get_queryset(request).filter(parent_type_id__in=type_ids)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        root_types = NodeTypeRepository().get_all_root_node_types().order_by('name')
        tabs = [{'label': 'All', 'scope': ''}]
        for nt in root_types:
            label = nt.name.replace('_', ' ').title()
            tabs.append({'label': label, 'scope': nt.json_scope or ''})
        extra_context['composition_tabs'] = tabs
        extra_context['current_scope'] = getattr(self, '_scope_filter', None) or request.GET.get('scope', '')
        try:
            return super().changelist_view(request, extra_context=extra_context)
        except IncorrectLookupParameters:
            new_params = QueryDict(mutable=True)
            new_params.update(request.GET)
            if 'scope' in new_params:
                del new_params['scope']
            return HttpResponseRedirect(request.path + '?' + new_params.urlencode())

    def get_urls(self):
        urls = super().get_urls()
        ns = self.url_namespace
        custom_urls = [
            path("editor/", self.admin_site.admin_view(self.editor_view), name=f"{ns}_editor"),
            path("editor/api/graph/", self.admin_site.admin_view(self.api_graph), name=f"{ns}_graph"),
            path("editor/api/composition/<uuid:comp_id>/", self.admin_site.admin_view(self.api_composition), name=f"{ns}_composition"),
            path("editor/api/create/", self.admin_site.admin_view(self.api_create), name=f"{ns}_create"),
            path("editor/api/delete/<uuid:comp_id>/", self.admin_site.admin_view(self.api_delete), name=f"{ns}_delete"),
        ]
        return custom_urls + urls

    def editor_view(self, request):
        scope = request.GET.get("scope", "")
        context = {
            **self.admin_site.each_context(request),
            "title": self.editor_title,
            "tree_mode": self.editor_tree_mode,
            "current_scope": scope,
        }
        return render(request, "admin/schemas/nodetypecomposition/editor.html", context)

    def api_graph(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        scope = request.GET.get("scope", "")
        type_ids = self._type_ids(scope if scope else None)
        node_types = [
            {**nt, "id": str(nt["id"])}
            for nt in NodeType.objects.filter(id__in=type_ids)
            .values("id", "name", "label", "is_root", "is_container", "json_scope")
            .order_by("name")
        ]
        compositions = [
            {**c, "id": str(c["id"]), "parent_type_id": str(c["parent_type_id"]), "child_type_id": str(c["child_type_id"])}
            for c in self.composition_model.objects.filter(parent_type_id__in=type_ids)
            .values("id", "parent_type_id", "child_type_id", "collection_key", "min_children", "max_children")
            .order_by("parent_type__name", "child_type__name")
        ]
        return JsonResponse({"node_types": node_types, "compositions": compositions})

    def api_composition(self, request, comp_id):
        if request.method == "GET":
            comp = self.composition_service.get_composition(comp_id, self.composition_model)
            if not comp:
                return JsonResponse({"error": ERR_NOT_FOUND}, status=404)
            return JsonResponse(comp)

        if request.method == "PATCH":
            try:
                payload = json.loads(request.body.decode("utf-8")) if request.body else {}
            except json.JSONDecodeError:
                return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

            try:
                self.composition_service.update_composition(comp_id, self.composition_model, payload)
                return JsonResponse({"ok": True})
            except ValueError as e:
                return JsonResponse({"error": str(e)}, status=404)

        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

    def api_create(self, request):
        if request.method != "POST":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)

        parent_type_id = payload.get("parent_type_id")
        child_type_id = payload.get("child_type_id")

        if not parent_type_id or not child_type_id:
            return JsonResponse({"error": ERR_PARENT_AND_CHILD_REQUIRED}, status=400)

        try:
            comp_id = self.composition_service.create_composition(
                parent_type_id,
                child_type_id,
                self.composition_model,
                payload.get("collection_key")
            )
            return JsonResponse({"ok": True, "id": str(comp_id)})
        except ValueError as e:
            error_msg = str(e)
            if "already exists" in error_msg:
                return JsonResponse({"error": ERR_ALREADY_EXISTS}, status=409)
            return JsonResponse({"error": error_msg}, status=400)

    def api_delete(self, request, comp_id):
        if request.method != "DELETE":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

        try:
            self.composition_service.delete_composition(comp_id, self.composition_model)
            return JsonResponse({"ok": True})
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=404)


@admin.register(NodeTypeComposition)
class NodeTypeCompositionAdmin(CompositionAdminMixin, admin.ModelAdmin):
    composition_model = NodeTypeComposition
    editor_title = "Compositions Editor"
    editor_tree_mode = "compositions"
    url_namespace = "schemas_nodetypecomposition"

    list_display = ("parent_type", "child_type", "collection_key", "min_children", "max_children")
    list_filter = ("parent_type", "child_type")
    search_fields = ("parent_type__name", "child_type__name", "collection_key")
    autocomplete_fields = ("parent_type", "child_type")
    change_list_template = "admin/schemas/nodetypecomposition/change_list.html"


@admin.register(NodeTypeVariant)
class NodeTypeVariantAdmin(admin.ModelAdmin):
    list_display = ("node_type", "variant_key")
    list_filter = ("node_type",)
    search_fields = ("variant_key", "node_type__name")
    autocomplete_fields = ("node_type",)


@admin.register(AttributeDef)
class AttributeDefAdmin(admin.ModelAdmin):
    list_display = ("node_type", "name", "json_key", "variant_key", "data_type", "domain", "is_common", "is_required")
    list_filter = ("node_type__json_scope", "data_type", "domain", "is_required", "is_common", "variant_key")
    search_fields = ("name", "json_key", "node_type__name", "variant_key")
    autocomplete_fields = ("node_type", "data_type", "domain")




@admin.register(ComponentPropertiesProxy)
class ComponentPropertiesAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['title'] = "Component Properties Manager"
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        # View route only
        custom_urls = [
            re_path(r'^component-properties/$', self.admin_site.admin_view(self.component_properties_view), name="schemas_component_properties"),
        ]
        return custom_urls + urls

    def component_properties_view(self, request):
        root_types = NodeTypeRepository().get_all_root_node_types().order_by('name')
        tabs = [{'label': nt.name.replace('_', ' ').title(), 'scope': nt.json_scope or ''} for nt in root_types]
        current_scope = request.GET.get('scope', '')
        context = {
            **self.admin_site.each_context(request),
            "title": "Component Properties",
            "scope_tabs": tabs,
            "current_scope": current_scope,
        }
        return render(request, "admin/schemas/component_properties.html", context)


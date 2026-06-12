"""
SchemaAdmin - Single admin for ALL root-level schema types.

Adding a new schema type (e.g. cfdi, web_page) requires ONLY a seed migration:
  - NodeType(name='cfdi', is_root=True, json_scope='cfdi_root')
  - NodeTypeCompositions, AttributeDefs as needed

Zero code changes. The admin auto-discovers root types from the DB at runtime.
"""

import json

from django.contrib import admin
from django.contrib.admin.views.main import IncorrectLookupParameters
from django.contrib import messages
from django.http import JsonResponse, HttpResponseRedirect, QueryDict
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html

from ..models import Schema, Project
from ..repositories.node_type_repository import NodeTypeRepository
from ..services.schema_service import SchemaService
from ..services.node_service import NodeService
from ..constants import (
    ERR_SCHEMA_MUST_BE_JSON_OBJECT,
    ERR_SCHEMA_MUST_HAVE_NAME_ID_OR_KEY,
    ERR_METHOD_NOT_ALLOWED,
    ERR_INCOMPLETE_SCHEMA,
    ERR_SCHEMA_TEXT_REQUIRED,
    ERR_SCHEMA_TYPE_REQUIRED,
    ERR_PROJECT_ID_REQUIRED,
    ERR_INVALID_SCHEMA_TYPE,
    ERR_INVALID_SCHEMA,
    ERR_IMPORT_FAILED,
    ERR_INVALID_JSON_MSG,
)
from .base import RootNodeAdminMixin
from .node_editor import NodeEditorMixin


def _validate_schema_json(schema_text):
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as e:
        raise ValueError(ERR_INVALID_JSON_MSG.format(error=e))
    if not isinstance(schema, dict):
        raise ValueError(ERR_SCHEMA_MUST_BE_JSON_OBJECT)
    # Accept 'id' or 'key' as name field for schemas
    if "name" not in schema and "id" not in schema and "key" not in schema:
        raise ValueError(ERR_SCHEMA_MUST_HAVE_NAME_ID_OR_KEY)
    return schema


def _root_node_type_name_for(node_type):
    """Derive root_node_type_name from a NodeType instance (strips '_root' suffix)."""
    if node_type.json_scope and node_type.json_scope.endswith("_root"):
        return node_type.json_scope[: -len("_root")]
    return node_type.name


@admin.register(Schema)
class SchemaAdmin(RootNodeAdminMixin, NodeEditorMixin, admin.ModelAdmin):
    """
    Single admin entry point for all root-level schema nodes.

    - Tabs are generated dynamically from NodeType.is_root=True records.
    - Adding a new schema type needs only a seed migration.
    - root_node_type_name / metadata_node_name are resolved per-object at runtime.
    """

    # RootNodeAdminMixin fields — resolved dynamically per request/object
    root_node_type_name: str = ""
    metadata_node_name: str = ""
    changelist_url_name: str = "schemas_schema_changelist"
    url_prefix: str = "schemas_schema"

    list_display = ("name", "schema_type_display", "project_display", "version", "status", "dirty", "warnings_display", "acciones")
    search_fields = ("name", "node_type__name", "project__name")
    list_filter = ("project",)
    readonly_fields = ("node_type", "parent")
    change_list_template = "admin/schemas/schema/change_list.html"

    def get_form(self, request, obj=None, **kwargs):
        from django import forms
        if obj is None:
            root_types = NodeTypeRepository().get_all_root_node_types()
            choices = [
                (nt.json_scope, _root_node_type_name_for(nt).replace("_", " ").title())
                for nt in root_types
            ]
            class SchemaAddForm(forms.ModelForm):
                schema_type = forms.ChoiceField(choices=choices, label="Schema type", required=True)
                project = forms.ModelChoiceField(
                    queryset=Project.objects.all(),
                    label="Project",
                    required=True,
                    help_text="Every schema must belong to a project"
                )
                class Meta:
                    model = Schema
                    fields = ("name",)
            return SchemaAddForm
        else:
            # Edit form - include project field
            class SchemaEditForm(forms.ModelForm):
                project = forms.ModelChoiceField(
                    queryset=Project.objects.all(),
                    label="Project",
                    required=True,
                    help_text="Every schema must belong to a project"
                )
                class Meta:
                    model = Schema
                    fields = ("name", "project")
            return SchemaEditForm

    # ------------------------------------------------------------------ #
    # Queryset — all root nodes of any root NodeType                      #
    # ------------------------------------------------------------------ #

    def get_queryset(self, request):
        qs = super().get_queryset(request).filter(
            node_type__is_root=True,
            parent__isnull=True,
        )
        scope = getattr(self, "_scope_filter", None) or request.GET.get("scope")
        if scope:
            qs = qs.filter(node_type__json_scope=scope)
        return qs

    # ------------------------------------------------------------------ #
    # Dynamic root_node_type_name / metadata_node_name per object        #
    # ------------------------------------------------------------------ #

    def _root_name_for_obj(self, obj):
        if obj and obj.node_type_id:
            return _root_node_type_name_for(obj.node_type)
        return self.root_node_type_name

    def _meta_name_for_obj(self, obj):
        return f"{self._root_name_for_obj(obj)}_metadata"

    # Override mixin helpers to resolve per-object instead of per-class
    def _get_version(self, obj):
        return obj.version or None

    # ------------------------------------------------------------------ #
    # Changelist tabs — auto-built from DB                                #
    # ------------------------------------------------------------------ #

    def get_changelist_instance(self, request):
        self._scope_filter = request.GET.get("scope")
        if self._scope_filter is not None:
            original_get = request.GET
            modified_get = QueryDict(mutable=True)
            modified_get.update(original_get)
            if "scope" in modified_get:
                del modified_get["scope"]
            request._original_get = original_get
            request.GET = modified_get
            try:
                cl = super().get_changelist_instance(request)
            finally:
                request.GET = request._original_get
                del request._original_get
            return cl
        return super().get_changelist_instance(request)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        root_types = NodeTypeRepository().get_all_root_node_types()
        tabs = [{"label": "All", "scope": ""}]
        for nt in root_types:
            label = _root_node_type_name_for(nt).replace("_", " ").title()
            tabs.append({"label": label, "scope": nt.json_scope or ""})
        extra_context["schema_tabs"] = tabs
        extra_context["current_scope"] = getattr(self, "_scope_filter", None) or request.GET.get("scope", "")
        try:
            return super().changelist_view(request, extra_context=extra_context)
        except IncorrectLookupParameters:
            new_params = QueryDict(mutable=True)
            new_params.update(request.GET)
            if "scope" in new_params:
                del new_params["scope"]
            return HttpResponseRedirect(request.path + "?" + new_params.urlencode())

    # ------------------------------------------------------------------ #
    # save_model — infer node_type from selected tab / POST scope         #
    # ------------------------------------------------------------------ #

    def save_model(self, request, obj, form, change):
        if not change:
            scope = (
                request.GET.get("scope")
                or request.POST.get("scope")
                or form.cleaned_data.get("schema_type")
            )
            node_type = NodeTypeRepository().get_root_node_type_by_scope(scope) if scope else None
            if not node_type:
                root_types = NodeTypeRepository().get_all_root_node_types()
                node_type = root_types.first() if root_types else None
            obj.node_type = node_type
            obj.parent = None
        else:
            obj.parent = None
        # call grandparent save (skip RootNodeAdminMixin.save_model which uses root_node_type_name)
        admin.ModelAdmin.save_model(self, request, obj, form, change)

        if not change and obj.node_type:
            try:
                project_id = form.cleaned_data.get("project").id if form.cleaned_data.get("project") else None
                SchemaService().initialize_schema_attributes(obj, project_id=project_id)
            except ValueError as e:
                messages.error(request, str(e))

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        scope = request.GET.get("scope")
        if scope:
            nt = NodeTypeRepository().get_root_node_type_by_scope(scope)
            if nt:
                initial["node_type"] = nt.pk
        return initial

    # ------------------------------------------------------------------ #
    # Lifecycle actions — resolve names dynamically                       #
    # ------------------------------------------------------------------ #

    def has_change_permission(self, request, obj=None):
        if obj is not None and self._get_attr_value(obj, "status") == "archived":
            return False
        return admin.ModelAdmin.has_change_permission(self, request, obj)

    def delete_model(self, request, obj):
        key = self._get_attr_value(obj, "key")
        version = self._get_version(obj)
        NodeService().delete_node(obj.id)
        if key and version:
            SchemaService().delete_schema_cache(key, version)

    def delete_queryset(self, request, queryset):
        svc = NodeService()
        schema_svc = SchemaService()
        for obj in queryset:
            key = self._get_attr_value(obj, "key")
            version = self._get_version(obj)
            svc.delete_node(obj.id)
            if key and version:
                schema_svc.delete_schema_cache(key, version)

    def publish_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        obj, err = self._get_node_or_error(request, Schema)
        if err:
            return err
        
        # Validate no missing required properties before publishing
        warnings = self._collect_required_warnings(obj.id)
        if warnings:
            warning_details = [
                f"{w['node_name']} ({w['node_type']}): missing {', '.join(w['missing'])}"
                for w in warnings
            ]
            return JsonResponse({
                "error": ERR_INCOMPLETE_SCHEMA,
                "detail": f"Cannot publish: {len(warnings)} node(s) have missing required properties",
                "warnings": warnings,
                "message": "Complete all required properties before publishing. " + "; ".join(warning_details)
            }, status=400)
        
        try:
            SchemaService().publish_schema(obj)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
        return HttpResponseRedirect(reverse("admin:schemas_schema_changelist"))

    def archive_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        obj, err = self._get_node_or_error(request, Schema)
        if err:
            return err
        try:
            SchemaService().archive_schema(obj)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
        return HttpResponseRedirect(reverse("admin:schemas_schema_changelist"))

    def draft_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        obj, err = self._get_node_or_error(request, Schema)
        if err:
            return err
        try:
            SchemaService().draft_schema(obj)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
        return HttpResponseRedirect(reverse("admin:schemas_schema_changelist"))

    # ------------------------------------------------------------------ #
    # Import                                                              #
    # ------------------------------------------------------------------ #

    def get_urls(self):
        urls = super().get_urls()
        urls.insert(
            0,
            path(
                "import/",
                self.admin_site.admin_view(self.import_view),
                name="schemas_schema_import",
            ),
        )
        return urls

    def import_view(self, request):
        root_types = [
            {"scope": nt.json_scope, "label": _root_node_type_name_for(nt).replace("_", " ").title()}
            for nt in NodeTypeRepository().get_all_root_node_types()
        ]
        if request.method == "GET":
            context = {
                **self.admin_site.each_context(request),
                "title": "Import Schema",
                "root_types": root_types,
                "projects": Project.objects.all(),
            }
            return render(request, "admin/schemas/schema/import_schema.html", context)

        if request.method == "POST":
            schema_text = request.POST.get("schema_text")
            schema_key = request.POST.get("schema_key")
            schema_version = request.POST.get("schema_version")
            schema_status = request.POST.get("schema_status", "draft")
            schema_type = request.POST.get("schema_type", "")
            project_id = request.POST.get("project_id")
            overwrite = request.POST.get("overwrite") == "true"

            if not schema_text:
                return JsonResponse({"error": ERR_SCHEMA_TEXT_REQUIRED}, status=400)
            if not schema_type:
                return JsonResponse({"error": ERR_SCHEMA_TYPE_REQUIRED}, status=400)
            if not project_id:
                return JsonResponse({"error": ERR_PROJECT_ID_REQUIRED}, status=400)
            root_node_type = NodeTypeRepository().get_root_node_type_by_scope(schema_type)
            if not root_node_type:
                return JsonResponse({"error": ERR_INVALID_SCHEMA_TYPE}, status=400)
            try:
                validated_schema = _validate_schema_json(schema_text)
            except ValueError as e:
                return JsonResponse({"error": ERR_INVALID_SCHEMA, "detail": str(e)}, status=400)
            validated_schema["node_type"] = root_node_type.name
            try:
                SchemaService().import_schema(
                    validated_schema, schema_key, schema_version, schema_status, overwrite,
                    project_id=project_id
                )
            except Exception as e:
                return JsonResponse({"error": ERR_IMPORT_FAILED, "detail": str(e)}, status=500)

            return HttpResponseRedirect(reverse("admin:schemas_schema_changelist"))

        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)

    # ------------------------------------------------------------------ #
    # Extra display column                                                #
    # ------------------------------------------------------------------ #

    @admin.display(description="Project")
    def project_display(self, obj):
        if obj.project:
            return format_html(
                '<span title="Org: {}">{}</span>',
                obj.project.organization.name if obj.project.organization else "N/A",
                obj.project.name
            )
        return format_html('<span style="color:#999;">—</span>')

    @admin.display(description="Type")
    def schema_type_display(self, obj):
        if obj.node_type_id:
            label = _root_node_type_name_for(obj.node_type).replace("_", " ").title()
            return format_html('<span style="font-size:0.85em;color:#666;">{}</span>', label)
        return "—"

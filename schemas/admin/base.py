"""
Base classes, inlines and shared utilities for Dynamic Schema admin
"""

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import path, reverse
from django.http import HttpResponseRedirect, JsonResponse
from django.utils.html import format_html

from ..models import (
    NodeAttribute,
    NodeTypeComposition,
)
from ..services.node_service import NodeService
from ..services.schema_service import SchemaService
from ..services.schema_validation_service import SchemaValidationService
from ..repositories.schema_repository import SchemaRepository
from ..constants import (
    ERR_METHOD_NOT_ALLOWED,
    ERR_NOT_FOUND,
    ERR_NODE_ID_REQUIRED,
    ERR_KEY_AND_VERSION_REQUIRED,
    ERR_SCHEMA_NOT_FOUND,
    ERR_UNEXPECTED_ERROR,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    STATUS_ARCHIVED,
)


class NodeAttributeInline(admin.TabularInline):
    model = NodeAttribute
    extra = 0
    autocomplete_fields = ['attribute_def']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.repository = SchemaRepository()

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj and hasattr(obj, 'node_type') and obj.node_type:
            # Restrict attribute_def choices to those for this node_type
            base_fields = list(formset.form.base_fields.items())
            for name, field in base_fields:
                if name == 'attribute_def':
                    field.queryset = self.repository.get_attribute_defs_by_node_type(obj.node_type)
            formset.form.base_fields = dict(base_fields)
        return formset


class NodeCompositionInline(admin.TabularInline):
    model = NodeTypeComposition
    fk_name = 'parent_type'
    extra = 0
    autocomplete_fields = ['child_type']


class NodeCompositionReverseInline(admin.TabularInline):
    model = NodeTypeComposition
    fk_name = 'child_type'
    extra = 0
    autocomplete_fields = ['parent_type']
    verbose_name = "Parent Composition"
    verbose_name_plural = "Parent Compositions"


class BaseNodeAdmin(admin.ModelAdmin):
    """Base admin class for nodes with common functionality"""
    list_display = ("name", "node_type", "parent", "sort_order")
    list_filter = ("node_type",)
    search_fields = ("name",)
    autocomplete_fields = ['parent', 'node_type']
    readonly_fields = ('id',)
    inlines = [NodeAttributeInline]


class RootNodeAdminMixin:
    """
    Mixin for root node admin classes that share lifecycle actions:
    Edit, Build, Publish, Archive, Draft.

    Subclasses must define:
        root_node_type_name  : str  — Node type name for the root node
        metadata_node_name   : str  — Node type name for the metadata node
        changelist_url_name  : str  — URL name for the changelist view
        url_prefix           : str  — URL prefix for admin views
    """

    root_node_type_name: str = ''
    metadata_node_name: str = ''
    changelist_url_name: str = ''
    url_prefix: str = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema_service = SchemaService()
        self.node_service = NodeService()
        self.repository = SchemaRepository()
        self.validation_service = SchemaValidationService()

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        nt = self.node_service.get_node_type(self.root_node_type_name)
        if nt:
            initial['node_type'] = nt.pk
        return initial

    def save_model(self, request, obj, form, change):
        if not change:
            obj.node_type = self.node_service.get_node_type(self.root_node_type_name)
            obj.parent = None
        else:
            obj.parent = None
        if not change and obj.node_type and obj.node_type.name == self.root_node_type_name:
            # Save the node and initialize its attributes atomically: on
            # failure the node is rolled back instead of reporting the
            # error after a commit.
            try:
                with transaction.atomic():
                    super().save_model(request, obj, form, change)
                    self.schema_service.initialize_schema_attributes(obj)
            except ValueError as e:
                form.add_error('name', str(e))
                messages.error(request, str(e))
                return
        else:
            super().save_model(request, obj, form, change)

    def _get_version(self, obj):
        return obj.version or None

    def _get_attr_value(self, obj, json_key):
        if json_key == 'key' and obj.key:
            return obj.key
        attr_def = self.repository.get_attribute_def(obj.node_type, json_key)
        if attr_def:
            attr = self.repository.get_node_attributes(obj, attr_def)
            if attr and attr.value_string:
                return attr.value_string
        return None

    @admin.display(description="Key")
    def key_display(self, obj):
        return self._get_attr_value(obj, 'key') or "N/A"

    @admin.display(description="Version")
    def version(self, obj):
        return self._get_version(obj) or "N/A"

    @admin.display(description="Status")
    def status(self, obj):
        status_value = self._get_attr_value(obj, 'status')
        if not status_value:
            return "N/A"
        # Try to get color from domain item if defined
        status_def = self.repository.get_attribute_def(obj.node_type, 'status')
        if status_def and status_def.domain:
            domain_item = self.repository.get_domain_item_by_value(status_def.domain, status_value)
            if domain_item and hasattr(domain_item, 'color') and domain_item.color:
                return format_html('<span style="color: {};">{}</span>', domain_item.color, status_value)
        # Fallback to default colors for common status values
        colors = {STATUS_ARCHIVED: 'red', STATUS_PUBLISHED: 'green', STATUS_DRAFT: 'orange'}
        color = colors.get(status_value)
        if color:
            return format_html('<span style="color: {};">{}</span>', color, status_value)
        return status_value

    @admin.display(description="Dirty")
    def dirty(self, obj):
        key_value = self._get_attr_value(obj, 'key')
        version_value = obj.version
        if key_value and version_value:
            build_state = self.repository.get_build_state(key_value, version_value)
            if build_state and build_state.dirty:
                return format_html('<span style="color: orange;">Modified</span>')
            elif build_state:
                return format_html('<span style="color: green;">Clean</span>')
        return "-"

    def _collect_required_warnings(self, node_id):
        """Walk the node tree and return missing required AttributeDefs per node.

        Args:
            node_id: UUID (as string or UUID object) of the root node to check
        """
        return self.validation_service.collect_required_warnings(node_id)

    @admin.display(description="Warnings")
    def warnings_display(self, obj):
        """Display warning indicator if schema has missing required properties."""
        status_value = self._get_attr_value(obj, 'status')
        if status_value != STATUS_DRAFT:
            return format_html('<span style="color:#999;">—</span>')
        
        warnings = self._collect_required_warnings(obj.id)
        if not warnings:
            return format_html('<span style="color:green;">✓ Complete</span>')
        
        total_props = sum(len(w['missing']) for w in warnings)
        return format_html(
            '<span style="color:#c00;font-weight:bold;" title="{} node(s), {} properties missing">'
            '⚠️ {} node(s)'
            '</span>'
            '<span style="color:#666;font-size:11px;margin-left:4px;">({} props)</span>',
            len(warnings), total_props,
            len(warnings),
            total_props
        )

    @admin.display(description="Actions")
    def acciones(self, obj):
        # Lifecycle transition rules (which actions exist for which status)
        # live in SchemaService.get_available_actions; this display method
        # only maps each action to its URL/button markup.
        info = self.schema_service.get_available_actions(obj)
        buttons = []
        for action in info["actions"]:
            if action == 'edit':
                editor_url = reverse("admin:schemas_node_editor")
                buttons.append(format_html('<a href="{}?node_id={}" class="button">Edit</a>', editor_url, obj.id))
            elif action == 'build':
                build_url = reverse(f"admin:{self.url_prefix}_build")
                buttons.append(format_html('<a href="{}?key={}&version={}" class="button">Build</a>', build_url, info["key"], info["version"]))
            elif action == 'publish_blocked':
                buttons.append(format_html(
                    '<span class="button" disabled style="opacity:0.6;cursor:not-allowed;" '
                    'title="Cannot publish: {} node(s) have missing required properties">'
                    'Publish 🔒'
                    '</span>',
                    info["blocked_nodes"]
                ))
            elif action == 'publish':
                publish_url = reverse(f"admin:{self.url_prefix}_publish")
                buttons.append(format_html('<a href="{}?node_id={}" class="button">Publish</a>', publish_url, obj.id))
            elif action == 'archive':
                archive_url = reverse(f"admin:{self.url_prefix}_archive")
                buttons.append(format_html('<a href="{}?node_id={}" class="button">Archive</a>', archive_url, obj.id))
            elif action == 'draft':
                draft_url = reverse(f"admin:{self.url_prefix}_draft")
                buttons.append(format_html('<a href="{}?node_id={}" class="button">Draft</a>', draft_url, obj.id))

        return format_html(' '.join(buttons))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("publish/", self.admin_site.admin_view(self.publish_view), name=f"{self.url_prefix}_publish"),
            path("archive/", self.admin_site.admin_view(self.archive_view), name=f"{self.url_prefix}_archive"),
            path("draft/", self.admin_site.admin_view(self.draft_view), name=f"{self.url_prefix}_draft"),
            path("build/", self.admin_site.admin_view(self.build_view), name=f"{self.url_prefix}_build"),
        ]
        return custom_urls + urls

    def _get_node_or_error(self, request, model_class=None):
        node_id = request.GET.get("node_id")
        if not node_id:
            return None, JsonResponse({"error": ERR_NODE_ID_REQUIRED}, status=400)
        try:
            obj = self.node_service.get_node_with_parent(node_id)
        except (ValidationError, ValueError):
            return None, JsonResponse({"error": ERR_NOT_FOUND}, status=404)
        if obj is None:
            return None, JsonResponse({"error": ERR_NOT_FOUND}, status=404)
        return obj, None

    def publish_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        obj, err = self._get_node_or_error(request, self.model)
        if err:
            return err
        # Shared publish gate: missing required properties block publishing
        blockers = self.schema_service.get_publish_blockers(obj)
        if blockers:
            return JsonResponse(blockers, status=400)
        try:
            self.schema_service.publish_schema(obj)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": ERR_UNEXPECTED_ERROR.format(error=e)}, status=500)
        return HttpResponseRedirect(reverse(f"admin:{self.changelist_url_name}"))

    def archive_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        obj, err = self._get_node_or_error(request, self.model)
        if err:
            return err
        try:
            self.schema_service.archive_schema(obj)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": ERR_UNEXPECTED_ERROR.format(error=e)}, status=500)
        return HttpResponseRedirect(reverse(f"admin:{self.changelist_url_name}"))

    def draft_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        obj, err = self._get_node_or_error(request, self.model)
        if err:
            return err
        try:
            self.schema_service.draft_schema(obj)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": ERR_UNEXPECTED_ERROR.format(error=e)}, status=500)
        return HttpResponseRedirect(reverse(f"admin:{self.changelist_url_name}"))

    def build_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
        key = request.GET.get("key")
        version = request.GET.get("version")
        if not key or not version:
            return JsonResponse({"error": ERR_KEY_AND_VERSION_REQUIRED}, status=400)
        try:
            # Look up the root node and derive project_id inside the service
            self.schema_service.increment_build_for(key, version)
        except LookupError:
            return JsonResponse({"error": ERR_SCHEMA_NOT_FOUND}, status=404)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
        return HttpResponseRedirect(reverse(f"admin:{self.changelist_url_name}"))

"""
SchemaCacheAdmin with dynamic tabs for filtering by node type
"""

from django.contrib import admin
from django.utils.html import format_html
from django.http import QueryDict

from ..models import (
    SchemaCache,
    NodeType,
    AttributeDef,
    Node,
    NodeAttribute,
    Project,
)
from ..repositories.attribute_def_repository import AttributeDefRepository


class ProjectListFilter(admin.SimpleListFilter):
    title = 'project'
    parameter_name = 'project'

    def lookups(self, request, model_admin):
        projects = Project.objects.all().order_by('name')
        return [(str(p.id), p.name) for p in projects]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(project_id=self.value())
        return queryset


@admin.register(SchemaCache)
class SchemaCacheAdmin(admin.ModelAdmin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.repository = AttributeDefRepository()

    change_list_template = "admin/schemas/schemacache/change_list.html"
    list_display = ("key", "schema_type", "project_display", "version", "generated_at")
    search_fields = ("key", "version", "project__name")
    list_filter = (ProjectListFilter,)
    exclude = ('schema_json',)
    readonly_fields = ('schema_json_pretty',)

    @admin.display(description="Schema JSON")
    def schema_json_pretty(self, obj):
        import json
        if obj.schema_json:
            return format_html('<pre style="white-space: pre-wrap; word-wrap: break-word;">{}</pre>', json.dumps(obj.schema_json, indent=2, ensure_ascii=False))
        return "N/A"

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
    def schema_type(self, obj):
        if obj.schema_type:
            return format_html('<span style="color: blue;">{}</span>', obj.schema_type.capitalize())
        return "-"

    def get_changelist_instance(self, request):
        """Override to capture node_type filter before Django's changelist processes it."""
        self._node_type_filter = request.GET.get('node_type')
        return super().get_changelist_instance(request)

    def changelist_view(self, request, extra_context=None):
        """Override to add dynamic node type tabs for root types only"""
        extra_context = extra_context or {}
        
        node_types = NodeType.objects.filter(json_scope__endswith='_root')
        
        tabs = [{'label': 'All', 'value': ''}]
        for nt in node_types:
            label = nt.name.replace('_', ' ').title()
            tabs.append({'label': label, 'value': nt.name})
        
        extra_context['node_type_tabs'] = tabs
        extra_context['current_node_type'] = request.GET.get('node_type', '')
        
        return super().changelist_view(request, extra_context=extra_context)

    def get_queryset(self, request):
        """Filter by node_type if selected in tab"""
        qs = super().get_queryset(request)
        node_type_name = getattr(self, '_node_type_filter', None) or request.GET.get('node_type')

        if node_type_name:
            keys_list = self.repository.get_schema_cache_keys_by_node_type(node_type_name)
            if keys_list:
                qs = qs.filter(key__in=keys_list)

        return qs
